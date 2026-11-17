"""收割账本与不可变交付单（只追加，禁止用当前目录覆盖历史订单）。

与 journal.py 的来源账本相互独立：journal 保存冻结来源的记录版本，
本模块保存收割运行的页序、item 内容版本与缺席判定，并在交付时
把资产地址及元信息与同版 item 绑定后输出可复现清单。
"""
import json
import sqlite3

from .harvest import digest, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS harvest_runs(
  harvest_id TEXT PRIMARY KEY,
  collection TEXT NOT NULL,
  query_digest TEXT NOT NULL,
  query_json TEXT NOT NULL,
  service_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  terminated INTEGER NOT NULL,
  interruption_json TEXT,
  page_count INTEGER NOT NULL,
  coverage_json TEXT NOT NULL,
  receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS harvest_pages(
  harvest_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  method TEXT NOT NULL,
  url TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  next_audit_json TEXT,
  PRIMARY KEY(harvest_id, sequence)
);
CREATE TABLE IF NOT EXISTS item_versions(
  collection TEXT NOT NULL,
  item_id TEXT NOT NULL,
  content_version TEXT NOT NULL,
  first_harvest_id TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  feature_digest TEXT NOT NULL,
  PRIMARY KEY(collection, item_id, content_version)
);
CREATE TABLE IF NOT EXISTS item_observations(
  collection TEXT NOT NULL,
  item_id TEXT NOT NULL,
  content_version TEXT NOT NULL,
  harvest_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  PRIMARY KEY(collection, item_id, content_version, harvest_id)
);
CREATE TABLE IF NOT EXISTS item_absences(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  collection TEXT NOT NULL,
  item_id TEXT NOT NULL,
  harvest_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('deleted', 'absent_unknown')),
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_assets(
  collection TEXT NOT NULL,
  item_id TEXT NOT NULL,
  content_version TEXT NOT NULL,
  asset_key TEXT NOT NULL,
  href TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  PRIMARY KEY(collection, item_id, content_version, asset_key)
);
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY,
  harvest_id TEXT NOT NULL,
  collection TEXT NOT NULL,
  coverage_state TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def connect(path):
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA)
    return c


def _row_exists(c, table, key_col, key):
    return c.execute(
        'SELECT 1 FROM %s WHERE %s=?' % (table, key_col), (key,)).fetchone()


def commit_harvest(c, receipt):
    """把一次收割收据提交进只追加账本，返回收据中 item 的缺席判定。

    已存在的 harvest_id 原样重放（幂等），不产生重复缺席记录。
    缺席判定需要跨收割基线：曾见过的 id 本次完全未出现 → deleted，
    并附证据；没有覆盖证明或无基线 → absent_unknown，绝不断言删除。
    """
    hid = receipt['harvest_id']
    if _row_exists(c, 'harvest_runs', 'harvest_id', hid):
        return _absence_view(c, hid)

    coverage = receipt['coverage']
    with c:
        c.execute(
            'INSERT INTO harvest_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (hid, receipt['collection'], receipt['query']['query_digest'],
             json.dumps(receipt['query'], ensure_ascii=False, sort_keys=True),
             json.dumps(receipt['service'], ensure_ascii=False, sort_keys=True),
             receipt['started_at'], receipt['finished_at'],
             1 if receipt['terminated'] else 0,
             json.dumps(receipt['interruption'], ensure_ascii=False),
             len(receipt['pages']),
             json.dumps(coverage, ensure_ascii=False, sort_keys=True),
             json.dumps(receipt, ensure_ascii=False, sort_keys=True)))

        for page in receipt['pages']:
            c.execute('INSERT INTO harvest_pages VALUES(?,?,?,?,?,?,?)', (
                hid, page['sequence'], page['request']['method'],
                page['request']['url'],
                json.dumps(page['request'], ensure_ascii=False,
                           sort_keys=True),
                json.dumps(page['response'], ensure_ascii=False,
                           sort_keys=True),
                json.dumps(page['next'], ensure_ascii=False,
                           sort_keys=True)))

        seen_pairs = set()
        for item in receipt['items']:
            seen_pairs.add((item['collection'], item['id']))
            for version in item['versions']:
                if not version['assets']:
                    raise ValueError(
                        'item 缺少资产，无法同版绑定: %s/%s'
                        % (item['collection'], item['id']))
                exists = c.execute(
                    'SELECT 1 FROM item_versions WHERE collection=? AND '
                    'item_id=? AND content_version=?',
                    (item['collection'], item['id'],
                     version['content_version'])).fetchone()
                if not exists:
                    c.execute('INSERT INTO item_versions VALUES(?,?,?,?,?,?)',
                              (item['collection'], item['id'],
                               version['content_version'], hid,
                               receipt['started_at'],
                               version['feature_digest']))
                for sequence in version['pages']:
                    c.execute(
                        'INSERT OR IGNORE INTO item_observations '
                        'VALUES(?,?,?,?,?)',
                        (item['collection'], item['id'],
                         version['content_version'], hid, sequence))
                for asset in version['assets']:
                    c.execute(
                        'INSERT OR IGNORE INTO item_assets VALUES(?,?,?,?,?,?)',
                        (item['collection'], item['id'],
                         version['content_version'], asset['key'],
                         asset['href'],
                         json.dumps(asset['metadata'], ensure_ascii=False,
                                    sort_keys=True)))

        # 跨收割缺席判定：仅针对此前账本里出现过的 id。
        known = c.execute(
            'SELECT DISTINCT collection,item_id FROM item_versions '
            'WHERE collection=?', (receipt['collection'],)).fetchall()
        for coll, item_id in known:
            if (coll, item_id) in seen_pairs:
                continue
            prior = c.execute(
                'SELECT content_version,first_harvest_id,first_seen_at '
                'FROM item_versions WHERE collection=? AND item_id=? '
                'ORDER BY first_seen_at', (coll, item_id)).fetchall()
            if coverage['state'].startswith('proven'):
                status = 'deleted'
                evidence = {
                    'rule': 'absent-from-proven-coverage-run',
                    'last_seen_versions': [p[0] for p in prior],
                    'last_harvest_ids': [p[1] for p in prior],
                }
            else:
                status = 'absent_unknown'
                evidence = {
                    'rule': 'no-coverage-proof-deletion-not-asserted',
                    'last_seen_versions': [p[0] for p in prior],
                    'last_harvest_ids': [p[1] for p in prior],
                    'coverage': coverage,
                }
            c.execute('INSERT INTO item_absences VALUES(NULL,?,?,?,?,?,?)',
                      (coll, item_id, hid, status,
                       json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                       now_iso()))
    return _absence_view(c, hid)


def _absence_view(c, harvest_id):
    rows = c.execute(
        'SELECT collection,item_id,status,evidence_json FROM item_absences '
        'WHERE harvest_id=? ORDER BY id DESC', (harvest_id,)).fetchall()
    return [
        {'collection': coll, 'id': item_id, 'status': status,
         'evidence': json.loads(evidence)}
        for coll, item_id, status, evidence in rows
    ]


def build_delivery(c, receipt):
    """生成可复现场景清单；覆盖未证明或缺资产时拒绝交付。

    交付单只追加、永不更新：同一 harvest 的重复调用返回同一 delivery_id，
    历史订单不会被新收割覆盖。
    """
    hid = receipt['harvest_id']
    if not _row_exists(c, 'harvest_runs', 'harvest_id', hid):
        raise ValueError('收割尚未提交账本，不能交付: ' + hid)

    coverage = receipt['coverage']
    reasons = []
    if not coverage['state'].startswith('proven'):
        reasons.append('覆盖范围未证明（存在未知缺口）')
    for item in receipt['items']:
        if item['mid_run_revision']:
            reasons.append(
                '收割期间发生服务修订: %s/%s 有 %d 个内容版本'
                % (item['collection'], item['id'], len(item['versions'])))
        for version in item['versions']:
            if not version['assets']:
                reasons.append(
                    '资产未与同版 item 绑定: %s/%s'
                    % (item['collection'], item['id']))
    # 跨收割修订：账本中该 item 若存在本次未出现的历史内容版本，
    # 说明服务在两次收割之间发生过修订，不能静默选用当前版本交付。
    for item in receipt['items']:
        stored = {row[0] for row in c.execute(
            'SELECT content_version FROM item_versions '
            'WHERE collection=? AND item_id=?',
            (item['collection'], item['id'])).fetchall()}
        current = {v['content_version'] for v in item['versions']}
        superseded = stored - current
        if superseded:
            reasons.append(
                '存在跨收割服务修订（历史内容版本未消解）: %s/%s'
                % (item['collection'], item['id']))
    if not receipt['terminated']:
        reasons.append('分页未自然终止: ' + json.dumps(
            receipt['interruption'], ensure_ascii=False))
    if reasons:
        raise ValueError('交付条件不满足: ' + '; '.join(reasons))

    existing = c.execute(
        'SELECT delivery_id,manifest_json FROM deliveries WHERE harvest_id=?',
        (hid,)).fetchone()
    if existing:
        return existing[0], json.loads(existing[1])

    scenes = []
    for item in sorted(receipt['items'], key=lambda x: (x['collection'], x['id'])):
        if len(item['versions']) != 1:
            raise ValueError('服务修订未消解，不能选择唯一版本交付')
        version = item['versions'][0]
        scenes.append({
            'collection': item['collection'],
            'id': item['id'],
            'datetime': item['datetime'],
            'content_version': version['content_version'],
            'feature_digest': version['feature_digest'],
            'assets': [
                {'key': a['key'], 'href': a['href'],
                 'metadata': a['metadata']}
                for a in version['assets']
            ],
        })
    manifest = {
        'protocol': receipt['protocol'],
        'collection': receipt['collection'],
        'harvest_id': hid,
        'query_digest': receipt['query']['query_digest'],
        'frozen_query': receipt['query'],
        'service': receipt['service'],
        'coverage': coverage,
        'pages': [
            {'sequence': p['sequence'], 'url': p['request']['url'],
             'body_digest': p['response']['body_digest']}
            for p in receipt['pages']
        ],
        'scenes': scenes,
        'scene_count': len(scenes),
        'created_at': now_iso(),
    }
    delivery_id = 'dlv_' + digest(manifest)[7:23]
    c.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',
              (delivery_id, hid, receipt['collection'], coverage['state'],
               json.dumps(manifest, ensure_ascii=False, sort_keys=True),
               manifest['created_at']))
    c.commit()
    return delivery_id, manifest


def list_deliveries(c):
    rows = c.execute(
        'SELECT delivery_id,harvest_id,collection,coverage_state,created_at '
        'FROM deliveries ORDER BY rowid').fetchall()
    return [
        {'delivery_id': r[0], 'harvest_id': r[1], 'collection': r[2],
         'coverage_state': r[3], 'created_at': r[4]} for r in rows]
