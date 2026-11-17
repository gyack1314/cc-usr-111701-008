"""核对收割账本的缺席分类、同版资产绑定与不可变交付。"""
import json
import tempfile
import unittest
from pathlib import Path

from capture_data.harvest import ReplayTransport, freeze_query, harvest
from capture_data.harvest_store import (
    build_delivery, commit_harvest, connect, list_deliveries)


def feature(item_id, dt='2026-10-05T04:00:00Z', collection='regional',
            asset_href=None):
    return {
        'type': 'Feature', 'stac_version': '1.0.0',
        'id': item_id, 'collection': collection,
        'properties': {'datetime': dt},
        'assets': {'visual': {
            'href': asset_href or 'https://cdn.example.test/%s.tif' % item_id,
            'type': 'image/tiff'}},
    }


def response(features, links=None, headers=None):
    return {'status': 200, 'headers': headers or {},
            'body': {'type': 'FeatureCollection',
                     'links': links or [], 'features': features}}


def make_receipt(features_by_page, query=None, headers=None):
    exchanges = []
    for seq, features in enumerate(features_by_page):
        links = []
        if seq < len(features_by_page) - 1:
            links = [{'rel': 'next',
                      'href': 'https://h.test/p%d' % (seq + 1)}]
        exchanges.append({
            'request': {'method': 'GET',
                        'url': 'https://h.test/search' if seq == 0
                        else 'https://h.test/p%d' % seq},
            'response': response(features, links),
        })
    q = query or freeze_query(
        'GET', 'https://h.test/search',
        window={'collections': ['regional'],
                'start': '2026-10-01T00:00:00Z',
                'end': '2026-11-01T00:00:00Z'})
    return harvest(q, ReplayTransport(exchanges))


def snapshot_receipt(features, snapshot_id='snap-1'):
    headers = {'x-snapshot-id': snapshot_id,
               'x-stac-conformance':
                   'https://stac-api.example.com/extensions/snapshot/v1'}
    exchanges = [{
        'request': {'method': 'GET', 'url': 'https://h.test/search'},
        'response': response(features, [], headers),
    }]
    q = freeze_query('GET', 'https://h.test/search',
                     window={'collections': ['regional'],
                             'start': '2026-10-01T00:00:00Z',
                             'end': '2026-11-01T00:00:00Z'})
    return harvest(q, ReplayTransport(exchanges))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / 'harvest.sqlite3'))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_commit_is_idempotent_and_appends_versions(self):
        r1 = make_receipt([[feature('s1')]])
        commit_harvest(self.db, r1)
        commit_harvest(self.db, r1)  # 重放不产生重复数据
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM harvest_runs').fetchone()[0],
            1)
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM item_versions').fetchone()[0],
            1)

    def test_absent_without_proof_is_unknown_not_deleted(self):
        r1 = make_receipt([[feature('s1')]])
        commit_harvest(self.db, r1)
        r2 = make_receipt([[feature('s2')]])
        absences = commit_harvest(self.db, r2)
        row = next(a for a in absences if a['id'] == 's1')
        self.assertEqual(row['status'], 'absent_unknown')
        self.assertIn('no-coverage-proof', row['evidence']['rule'])

    def test_absent_with_snapshot_proof_is_deleted_with_evidence(self):
        r1 = snapshot_receipt([feature('s1'), feature('s2')])
        commit_harvest(self.db, r1)
        r2 = snapshot_receipt([feature('s2')])
        absences = commit_harvest(self.db, r2)
        row = next(a for a in absences if a['id'] == 's1')
        self.assertEqual(row['status'], 'deleted')
        self.assertEqual(row['evidence']['rule'],
                         'absent-from-proven-coverage-run')
        self.assertTrue(row['evidence']['last_seen_versions'])

    def test_revision_appends_version_and_keeps_first_immutable(self):
        r1 = make_receipt([[feature('s1', asset_href='https://cdn/old.tif')]])
        commit_harvest(self.db, r1)
        r2 = make_receipt(
            [[feature('s1', asset_href='https://cdn/new.tif')]])
        commit_harvest(self.db, r2)
        rows = self.db.execute(
            'SELECT content_version,feature_digest FROM item_versions '
            'WHERE item_id=? ORDER BY first_seen_at', ('s1',)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_assets_bound_to_specific_content_version(self):
        r1 = make_receipt(
            [[feature('s1', asset_href='https://cdn/old.tif')]])
        commit_harvest(self.db, r1)
        r2 = make_receipt(
            [[feature('s1', asset_href='https://cdn/new.tif')]])
        commit_harvest(self.db, r2)
        bindings = self.db.execute(
            'SELECT content_version,href FROM item_assets WHERE item_id=?',
            ('s1',)).fetchall()
        hrefs = {href for _, href in bindings}
        self.assertEqual(hrefs, {'https://cdn/old.tif',
                                 'https://cdn/new.tif'})

    def test_delivery_blocked_on_unknown_coverage(self):
        r1 = make_receipt([[feature('s1')]])
        commit_harvest(self.db, r1)
        with self.assertRaises(ValueError):
            build_delivery(self.db, r1)

    def test_delivery_blocked_on_mid_run_revision_even_with_snapshot(self):
        old = feature('s1', asset_href='https://cdn/old.tif')
        new = feature('s1', asset_href='https://cdn/new.tif')
        r1 = snapshot_receipt([old, new])
        commit_harvest(self.db, r1)
        with self.assertRaises(ValueError):
            build_delivery(self.db, r1)

    def test_cross_run_revision_blocks_delivery(self):
        r1 = snapshot_receipt(
            [feature('s1', asset_href='https://cdn/old.tif')])
        commit_harvest(self.db, r1)
        r2 = snapshot_receipt(
            [feature('s1', asset_href='https://cdn/new.tif')],
            snapshot_id='snap-2')
        commit_harvest(self.db, r2)
        with self.assertRaises(ValueError) as ctx:
            build_delivery(self.db, r2)
        self.assertIn('跨收割服务修订', str(ctx.exception))

    def test_delivery_manifest_is_reproducible_and_immutable(self):
        r1 = snapshot_receipt([feature('s1'), feature('s2')])
        commit_harvest(self.db, r1)
        delivery_id, manifest = build_delivery(self.db, r1)
        again_id, again = build_delivery(self.db, r1)
        self.assertEqual(delivery_id, again_id)
        self.assertEqual(manifest, again)
        self.assertEqual(manifest['scene_count'], 2)
        self.assertEqual(manifest['coverage']['state'], 'proven-snapshot')
        # 交付单内容绑定到具体内容版本与资产元信息。
        scene = next(s for s in manifest['scenes'] if s['id'] == 's1')
        self.assertEqual(scene['assets'][0]['metadata']['type'],
                         'image/tiff')
        self.assertEqual(len(list_deliveries(self.db)), 1)

        # 后续收割产生新交付，历史交付单仍原样保留（不被当前目录覆盖）。
        r2 = snapshot_receipt([feature('s2'), feature('s3')],
                              snapshot_id='snap-2')
        commit_harvest(self.db, r2)
        id2, manifest2 = build_delivery(self.db, r2)
        self.assertNotEqual(delivery_id, id2)
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],
            2)
        old_manifest = json.loads(
            self.db.execute(
                'SELECT manifest_json FROM deliveries WHERE delivery_id=?',
                (delivery_id,)).fetchone()[0])
        self.assertEqual(old_manifest['scene_count'], 2)
        self.assertNotEqual(old_manifest, manifest2)

    def test_delivery_requires_committed_harvest(self):
        r1 = snapshot_receipt([feature('s1')])
        with self.assertRaises(ValueError):
            build_delivery(self.db, r1)

    def test_page_chain_persisted_for_audit(self):
        r1 = make_receipt([[feature('s1')], [feature('s2')]])
        commit_harvest(self.db, r1)
        pages = self.db.execute(
            'SELECT sequence,url FROM harvest_pages ORDER BY sequence'
        ).fetchall()
        self.assertEqual([p[0] for p in pages], [0, 1])
        self.assertTrue(all(p[1].startswith('https://h.test/') for p in pages))


if __name__ == '__main__':
    unittest.main()
