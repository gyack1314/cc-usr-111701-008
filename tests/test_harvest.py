"""核对可审计收割引擎：冻结查询、页序、去重与续页语义。"""
import json
import unittest

from capture_data.harvest import (
    ReplayTransport, corroborate_overlap, digest, follow_next,
    freeze_query, harvest, is_credential_header, item_identity,
    apply_overlap_evidence)


def feature(item_id, dt='2026-10-05T04:00:00Z', collection='regional',
            asset_href=None, assets=None):
    body = {
        'type': 'Feature',
        'stac_version': '1.0.0',
        'id': item_id,
        'collection': collection,
        'properties': {'datetime': dt},
        'assets': assets if assets is not None else {
            'visual': {'href': asset_href or
                       'https://cdn.example.test/%s.tif' % item_id,
                       'type': 'image/tiff'}},
    }
    return body


def response(features, links=None, headers=None, status=200):
    return {'status': status,
            'headers': headers or {},
            'body': {'type': 'FeatureCollection',
                     'links': links or [],
                     'features': features}}


def exchange(method, url, response_body, body=None, headers=None,
             forbidden=None):
    req = {'method': method, 'url': url}
    if headers is not None:
        req['headers'] = headers
    if body is not None or method == 'POST':
        req['body'] = body
    if forbidden:
        req['forbidden_headers'] = forbidden
    return {'request': req, 'response': response_body}


def basic_query(url='https://h.test/search', method='GET',
                body=None, headers=None, window=None):
    return freeze_query(method, url, headers=headers, body=body,
                        window=window or {'collections': ['regional'],
                                          'start': '2026-10-01T00:00:00Z',
                                          'end': '2026-11-01T00:00:00Z'})


class QueryFreezeTest(unittest.TestCase):
    def test_query_is_frozen_with_digest_and_credential_marking(self):
        q = freeze_query('GET', 'https://h.test/search',
                         headers={'Authorization': 'Bearer x',
                                  'Accept': 'application/geo+json'})
        self.assertEqual(q['protocol'], 'stac-search')
        self.assertTrue(q['query_digest'].startswith('sha256:'))
        self.assertEqual(q['credential_headers'], ['Authorization'])
        self.assertEqual(
            digest(q['method']), digest('GET'))  # 规范化稳定

    def test_extra_credential_headers_recognized(self):
        self.assertTrue(is_credential_header('X-Auth', ['x-auth']))
        self.assertFalse(is_credential_header('Accept'))


class PaginationTest(unittest.TestCase):
    def test_post_next_merges_body_and_keeps_credentials_same_zone(self):
        origin = {'method': 'POST', 'url': 'https://h.test/search',
                  'headers': {'Authorization': 'Bearer t'},
                  'body': {'collections': ['r'], 'limit': 2}}
        link = {'rel': 'next', 'href': 'https://h.test/search?c=2',
                'method': 'POST', 'body': {'cursor': '2'}, 'merge': True}
        req, audit = follow_next(origin, link)
        self.assertEqual(req['method'], 'POST')
        self.assertEqual(req['body'],
                         {'collections': ['r'], 'limit': 2, 'cursor': '2'})
        self.assertEqual(req['headers']['Authorization'], 'Bearer t')
        self.assertFalse(audit['cross_credential_zone'])

    def test_post_next_without_body_carries_original_body(self):
        origin = {'method': 'POST', 'url': 'https://h.test/search',
                  'headers': {}, 'body': {'collections': ['r']}}
        req, _ = follow_next(origin, {'href': 'https://h.test/n',
                                      'method': 'POST'})
        self.assertEqual(req['body'], {'collections': ['r']})

    def test_cross_zone_next_strips_credentials(self):
        origin = {'method': 'GET', 'url': 'https://h.test/search',
                  'headers': {'Authorization': 'Bearer t',
                              'X-Auth': 'secret', 'Keep': '1'},
                  'body': None}
        req, audit = follow_next(
            origin, {'href': 'https://mirror.example.net/n'},
            extra_credential_headers=('x-auth',))
        self.assertNotIn('Authorization', req['headers'])
        self.assertNotIn('X-Auth', req['headers'])
        self.assertEqual(req['headers']['Keep'], '1')
        self.assertTrue(audit['cross_credential_zone'])
        self.assertEqual(audit['stripped_credential_headers'],
                         ['Authorization', 'X-Auth'])

    def test_same_name_different_port_is_cross_zone(self):
        origin = {'method': 'GET', 'url': 'https://h.test:8443/search',
                  'headers': {'Authorization': 't'}, 'body': None}
        req, audit = follow_next(origin, {'href': 'https://h.test/n'})
        self.assertTrue(audit['cross_credential_zone'])
        self.assertNotIn('Authorization', req['headers'])

    def test_non_http_next_rejected(self):
        origin = {'method': 'GET', 'url': 'https://h.test/search',
                  'headers': {}, 'body': None}
        with self.assertRaises(ValueError):
            follow_next(origin, {'href': 'file:///etc/passwd'})


class HarvestDedupTest(unittest.TestCase):
    def test_cursor_drift_duplicates_dedup_by_content_version(self):
        f1, f2, f3 = feature('s1'), feature('s2'), feature('s3')
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([f1, f2], [{'rel': 'next',
                                          'href': 'https://h.test/p2'}])),
            exchange('GET', 'https://h.test/p2',
                     response([f2, f3], [])),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        ids = [(i['collection'], i['id']) for i in receipt['items']]
        self.assertEqual(ids, [('regional', 's1'), ('regional', 's2'),
                               ('regional', 's3')])
        s2 = next(i for i in receipt['items'] if i['id'] == 's2')
        self.assertEqual(len(s2['versions']), 1)
        self.assertEqual(s2['duplicate_occurrences'], 1)
        self.assertEqual(s2['occurrences'], [0, 1])
        self.assertEqual(s2['versions'][0]['pages'], [0, 1])
        self.assertTrue(receipt['terminated'])

    def test_content_change_is_revision_not_duplicate(self):
        old = feature('s1', asset_href='https://cdn/old.tif')
        new = feature('s1', asset_href='https://cdn/revised.tif')
        self.assertNotEqual(digest(old), digest(new))
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([old], [{'rel': 'next',
                                       'href': 'https://h.test/p2'}])),
            exchange('GET', 'https://h.test/p2', response([new], [])),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        item = receipt['items'][0]
        self.assertTrue(item['mid_run_revision'])
        self.assertEqual(len(item['versions']), 2)
        hrefs = {v['assets'][0]['href'] for v in item['versions']}
        self.assertEqual(hrefs, {'https://cdn/old.tif',
                                 'https://cdn/revised.tif'})

    def test_asset_bound_to_content_version(self):
        coll, item_id, version, dt, assets = item_identity(feature('s9'))
        self.assertEqual((coll, item_id), ('regional', 's9'))
        self.assertEqual(dt, '2026-10-05T04:00:00Z')
        self.assertEqual(assets[0]['key'], 'visual')
        self.assertEqual(assets[0]['metadata']['type'], 'image/tiff')

    def test_replay_rejects_lost_post_body_and_credential_leak(self):
        exchanges = [
            {'request': {'method': 'POST', 'url': 'https://h.test/search',
                         'headers': {'Authorization': 't'},
                         'body': {'q': 1},
                         'forbidden_headers': ['Authorization']},
             'response': response([])},
        ]
        t = ReplayTransport(exchanges)
        with self.assertRaises(ValueError):  # 越权带凭据
            t.request('POST', 'https://h.test/search',
                      {'Authorization': 't'}, {'q': 1})
        t2 = ReplayTransport(exchanges)
        with self.assertRaises(ValueError):  # body 语义丢失
            t2.request('POST', 'https://h.test/search', {}, {'q': 2})

    def test_pagination_loop_and_ambiguous_next_interrupt(self):
        loop_link = {'rel': 'next', 'href': 'https://h.test/search'}
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([feature('s1')], [loop_link])),
            exchange('GET', 'https://h.test/search',
                     response([feature('s1')], [loop_link])),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertFalse(receipt['terminated'])
        self.assertEqual(receipt['interruption']['reason'],
                         'pagination-loop')

    def test_page_order_and_service_identity_are_recorded(self):
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([feature('s1')],
                              [{'rel': 'next', 'href': 'https://h.test/p2'}],
                              headers={'server': 'stac/9'})),
            exchange('GET', 'https://h.test/p2',
                     response([], [], headers={'server': 'stac/9'})),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertEqual([p['sequence'] for p in receipt['pages']], [0, 1])
        self.assertEqual(receipt['service']['server'], 'stac/9')
        self.assertEqual(receipt['service']['stac_versions'], ['1.0.0'])
        # 凭据只留脱敏痕迹。
        self.assertEqual(
            receipt['pages'][0]['request']['headers'].get('Authorization'),
            None)


class CoverageTest(unittest.TestCase):
    def _terminated_receipt(self, items_a, items_b, window):
        def run(features):
            ex = [exchange('GET', 'https://h.test/search',
                           response(features, []))]
            return harvest(basic_query(window=window),
                           ReplayTransport(ex))
        return run(items_a), run(items_b)

    def test_unknown_gap_when_no_snapshot_and_no_overlap(self):
        exchanges = [exchange('GET', 'https://h.test/search',
                              response([feature('s1')], []))]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertEqual(receipt['coverage']['state'], 'unknown')
        self.assertEqual(receipt['coverage']['gaps'][0]['reason'],
                         'no-snapshot-or-overlap-evidence')

    def test_stable_snapshot_with_conformance_proves_coverage(self):
        headers = {'x-snapshot-id': 'snap-1',
                   'x-stac-conformance':
                       'https://stac-api.example.com/extensions/snapshot/v1'}
        exchanges = [exchange('GET', 'https://h.test/search',
                              response([feature('s1')], [], headers))]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertEqual(receipt['coverage']['state'], 'proven-snapshot')
        self.assertEqual(receipt['coverage']['evidence']['snapshot_id'],
                         'snap-1')

    def test_snapshot_changing_mid_pagination_is_unknown(self):
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([feature('s1')],
                              [{'rel': 'next', 'href': 'https://h.test/p2'}],
                              headers={'x-snapshot-id': 'snap-1'})),
            exchange('GET', 'https://h.test/p2',
                     response([], [], headers={'x-snapshot-id': 'snap-2'})),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertEqual(receipt['coverage']['state'], 'unknown')
        self.assertEqual(
            receipt['coverage']['gaps'][0]['reason'],
            'snapshot-id-changed-mid-pagination')

    def test_overlap_window_proves_coverage_when_versions_agree(self):
        window = {'collections': ['regional'],
                  'start': '2026-10-01T00:00:00Z',
                  'end': '2026-11-01T00:00:00Z'}
        # 重叠窗口内两个收割看到完全相同的 (id, 内容版本) 集合。
        a, b = self._terminated_receipt(
            [feature('s1'), feature('s2')],
            [feature('s2'), feature('s1')], window)
        verdict = corroborate_overlap(a, b, window)
        self.assertEqual(verdict['state'], 'proven-overlap')
        self.assertEqual(verdict['evidence']['overlap_item_versions'], 2)
        apply_overlap_evidence(a, b, window)
        self.assertEqual(a['coverage']['state'], 'proven-overlap')

    def test_overlap_disagreement_stays_unknown_with_explicit_gap(self):
        window = {'collections': ['regional'],
                  'start': '2026-10-01T00:00:00Z',
                  'end': '2026-11-01T00:00:00Z'}
        a, b = self._terminated_receipt(
            [feature('s1')], [feature('s2')], window)
        verdict = corroborate_overlap(a, b, window)
        self.assertEqual(verdict['state'], 'unknown')
        self.assertTrue(verdict['gaps'])
        self.assertIn('regional/s1',
                      verdict['gaps'][0]['only_in_first'])

    def test_revision_inside_overlap_breaks_proof(self):
        window = {'collections': ['regional'],
                  'start': '2026-10-01T00:00:00Z',
                  'end': '2026-11-01T00:00:00Z'}
        a, b = self._terminated_receipt(
            [feature('s1', asset_href='https://cdn/a.tif')],
            [feature('s1', asset_href='https://cdn/b.tif')], window)
        verdict = corroborate_overlap(a, b, window)
        self.assertEqual(verdict['state'], 'unknown')


class FixtureReplayTest(unittest.TestCase):
    def test_demo_transcript_follows_post_merge_and_cross_host_strip(self):
        from pathlib import Path
        base = Path(__file__).resolve().parents[1] / 'data' / 'harvest-demo'
        plan = json.loads((base / 'plan.json').read_bytes())
        transcript = json.loads(
            (base / plan['transcript']).read_bytes())
        query = freeze_query(plan['method'], plan['url'],
                             headers=plan['headers'], body=plan['body'],
                             window=plan['window'])
        transport = ReplayTransport(transcript['exchanges'])
        receipt = harvest(query, transport,
                          extra_credential_headers=plan[
                              'extra_credential_headers'])
        self.assertTrue(transport.exhausted)
        self.assertEqual(len(receipt['items']), 4)
        self.assertEqual(
            sum(i['duplicate_occurrences'] for i in receipt['items']), 1)
        # 第二页的 next 指向另一台主机：审计记录跨域与凭据剥离。
        cross = receipt['pages'][1]['next']
        self.assertTrue(cross['cross_credential_zone'])
        self.assertIn('Authorization', cross['stripped_credential_headers'])
        self.assertTrue(receipt['terminated'])
        # 无快照且未做重叠印证时，覆盖范围明确标示未知。
        self.assertEqual(receipt['coverage']['state'], 'unknown')

    def test_empty_harvest_does_not_crash_and_marks_unknown(self):
        exchanges = [exchange('GET', 'https://h.test/search',
                              response([], []))]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertTrue(receipt['terminated'])
        self.assertEqual(receipt['items'], [])
        self.assertEqual(receipt['coverage']['state'], 'unknown')

    def test_exhausted_transcript_is_an_explicit_interruption(self):
        # 转录只给了首页，但首页声称还有续页。
        exchanges = [exchange('GET', 'https://h.test/search',
                              response([feature('s1')],
                                       [{'rel': 'next',
                                         'href': 'https://h.test/p2'}]))]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertFalse(receipt['terminated'])
        self.assertEqual(receipt['interruption']['reason'],
                         'transcript-exhausted')
        self.assertEqual(receipt['coverage']['state'], 'unknown')

    def test_ambiguous_next_links_interrupt_and_mark_unknown_gap(self):
        exchanges = [
            exchange('GET', 'https://h.test/search',
                     response([feature('s1')], [
                         {'rel': 'next', 'href': 'https://h.test/a'},
                         {'rel': 'next', 'href': 'https://h.test/b'},
                     ])),
        ]
        receipt = harvest(basic_query(), ReplayTransport(exchanges))
        self.assertFalse(receipt['terminated'])
        self.assertEqual(receipt['interruption']['reason'],
                         'ambiguous-next-links')
        self.assertEqual(receipt['coverage']['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
