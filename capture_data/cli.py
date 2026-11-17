"""查看已接收资料，或按冻结查询执行可审计收割与交付。"""
import argparse
import json
from pathlib import Path

from .io import load
from .domain import summarize
from .journal import connect, receive
from .projection import overview
from .harvest import (
    ReplayTransport, freeze_query, harvest, apply_overlap_evidence)
from .harvest_store import build_delivery, commit_harvest, connect as harvest_connect, list_deliveries


def _read_json(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode('utf8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('非有限JSON常量')))


def _resolve(base, value):
    """夹具中的相对路径一律相对计划文件目录解析。"""
    p = Path(value)
    return str(p if p.is_absolute() else (base / p))


def cmd_inspect(args):
    path = Path(args.file)
    v, digest_value = load(path)
    summary = summarize(v, path.parent)
    c = connect(':memory:')
    source = receive(c, v, path.parent)
    print(json.dumps(
        {'manifest_sha256': digest_value, 'source_sha256': source,
         'summary': summary, 'overview': overview(c, v['collection'])},
        ensure_ascii=False, indent=2))


def cmd_harvest(args):
    plan_path = Path(args.plan).resolve()
    base = plan_path.parent
    plan = _read_json(plan_path)
    transcript = _read_json(_resolve(base, plan['transcript']))

    query = freeze_query(
        plan['method'], plan['url'],
        headers=plan.get('headers'), body=plan.get('body'),
        window=plan.get('window'))
    receipt = harvest(
        query, ReplayTransport(transcript['exchanges']),
        max_pages=plan.get('max_pages', 1000),
        extra_credential_headers=plan.get('extra_credential_headers', ()))

    # 可选：用第二个冻结收割做重叠窗口印证；先印证再提交，
    # 使缺席判定基于最终覆盖结论（删除 vs 暂时未出现）。
    if bool(args.overlap_transcript) != bool(args.overlap_window):
        raise SystemExit('--overlap-transcript 与 --overlap-window 必须同时提供')
    if args.overlap_transcript:
        other_transcript = _read_json(
            _resolve(base, args.overlap_transcript))
        other = harvest(
            query, ReplayTransport(other_transcript['exchanges']),
            max_pages=plan.get('max_pages', 1000),
            extra_credential_headers=plan.get(
                'extra_credential_headers', ()))
        apply_overlap_evidence(
            receipt, other, _read_json(_resolve(base, args.overlap_window)))
        commit_harvest(args.db, other)

    absences = commit_harvest(args.db, receipt)

    output = {
        'harvest_id': receipt['harvest_id'],
        'collection': receipt['collection'],
        'terminated': receipt['terminated'],
        'interruption': receipt['interruption'],
        'page_count': len(receipt['pages']),
        'item_count': len(receipt['items']),
        'duplicate_occurrences': sum(i['duplicate_occurrences']
                                     for i in receipt['items']),
        'mid_run_revisions': [
            '%s/%s' % (i['collection'], i['id'])
            for i in receipt['items'] if i['mid_run_revision']],
        'coverage': receipt['coverage'],
        'absences': absences,
    }
    if args.deliver:
        try:
            delivery_id, manifest = build_delivery(args.db, receipt)
            output['delivery'] = {
                'delivery_id': delivery_id,
                'scene_count': manifest['scene_count'],
                'coverage_state': manifest['coverage']['state']}
        except ValueError as exc:
            output['delivery_blocked'] = str(exc)
    args.db.close()
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_deliver(args):
    row = args.db.execute(
        'SELECT receipt_json FROM harvest_runs WHERE harvest_id=?',
        (args.harvest_id,)).fetchone()
    if row is None:
        raise SystemExit('未知 harvest_id: ' + args.harvest_id)
    receipt = json.loads(row[0])
    delivery_id, manifest = build_delivery(args.db, receipt)
    args.db.close()
    print(json.dumps(
        {'delivery_id': delivery_id, 'manifest': manifest},
        ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='command', required=True)

    p_inspect = sub.add_parser('inspect', help='查看冻结来源（原能力）')
    p_inspect.add_argument('file')
    p_inspect.set_defaults(func=cmd_inspect)

    p_harvest = sub.add_parser('harvest', help='按冻结计划执行搜索收割')
    p_harvest.add_argument('plan')
    p_harvest.add_argument('--db', type=lambda x: harvest_connect(x),
                           required=True, help='收割账本 SQLite 路径')
    p_harvest.add_argument('--overlap-transcript')
    p_harvest.add_argument('--overlap-window')
    p_harvest.add_argument('--deliver', action='store_true',
                           help='满足条件时同时生成交付单')
    p_harvest.set_defaults(func=cmd_harvest)

    p_deliver = sub.add_parser('deliver', help='对已提交收割生成不可变交付单')
    p_deliver.add_argument('--db', type=lambda x: harvest_connect(x),
                           required=True)
    p_deliver.add_argument('--harvest-id', required=True)
    p_deliver.set_defaults(func=cmd_deliver)

    a = p.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
