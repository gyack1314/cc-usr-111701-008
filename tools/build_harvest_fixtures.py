"""生成收割演示用的冻结转录（合成数据，不访问网络）。

transcript-a：服务新增影像后游标漂移的收割（含跨主机续页、POST body、
重复 item 与歧义 next）；transcript-b：重叠窗口复收，用于覆盖印证。
用 python -I tools/build_harvest_fixtures.py 重建。
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'data' / 'harvest-demo'


def feature(item_id, datetime_value, asset_name):
    return {
        'type': 'Feature',
        'stac_version': '1.0.0',
        'id': item_id,
        'collection': 'regional',
        'properties': {'datetime': datetime_value},
        'assets': {
            'visual': {
                'href': 'https://stac.example.test/assets/%s.tif' % asset_name,
                'type': 'image/tiff',
                'roles': ['visual'],
            }
        },
    }


A = feature('scene-01', '2026-10-05T04:00:00Z', 'scene-01')
B = feature('scene-02', '2026-10-12T04:00:00Z', 'scene-02')
C = feature('scene-03', '2026-10-19T04:00:00Z', 'scene-03')
D = feature('scene-04', '2026-10-26T04:00:00Z', 'scene-04')

COMMON_HEADERS = {
    'Authorization': 'Bearer synthetic-token',
    'X-Stac-Partition': 'region-east',
    'X-Auth': 'synthetic-credential',
}
STRIPPED_HEADERS = {
    'X-Stac-Partition': 'region-east',
}


def page_response(features, links, snapshot=None):
    headers = {'server': 'synthetic-stac/2.4'}
    if snapshot:
        headers['x-snapshot-id'] = snapshot
    return {
        'status': 200,
        'headers': headers,
        'body': {
            'type': 'FeatureCollection',
            'links': links,
            'features': features,
        },
    }


def transcript_a():
    return {'exchanges': [
        {
            'request': {
                'method': 'POST',
                'url': 'https://stac.example.test/search',
                'headers': COMMON_HEADERS,
                'body': {
                    'collections': ['regional'],
                    'datetime': '2026-10-01T00:00:00Z/2026-11-01T00:00:00Z',
                    'bbox': [116.0, 39.0, 117.5, 40.5],
                    'limit': 2,
                },
            },
            'response': page_response(
                [A, B],
                [{'rel': 'next',
                  'href': 'https://stac.example.test/search?cursor=p2',
                  'method': 'POST',
                  'body': {'cursor': 'p2'},
                  'merge': True}]),
        },
        {
            'request': {
                'method': 'POST',
                'url': 'https://stac.example.test/search?cursor=p2',
                'headers': COMMON_HEADERS,
                'body': {
                    'collections': ['regional'],
                    'datetime': '2026-10-01T00:00:00Z/2026-11-01T00:00:00Z',
                    'bbox': [116.0, 39.0, 117.5, 40.5],
                    'limit': 2,
                    'cursor': 'p2',
                },
            },
            'response': page_response(
                # 游标漂移：B 重复出现，服务新增了 scene-03。
                [B, C],
                [{'rel': 'next',
                  'href': 'https://mirror.example.net/stac/search?cursor=p3',
                  'method': 'GET'}]),
        },
        {
            'request': {
                'method': 'GET',
                'url': 'https://mirror.example.net/stac/search?cursor=p3',
                # 跨凭据域：Authorization / X-Auth 不得带过来。
                'headers': STRIPPED_HEADERS,
                'forbidden_headers': ['Authorization', 'X-Auth'],
            },
            'response': page_response(
                [D],
                []),
        },
    ]}


def transcript_b():
    return {'exchanges': [
        {
            'request': {
                'method': 'POST',
                'url': 'https://stac.example.test/search',
                'headers': COMMON_HEADERS,
                'body': {
                    'collections': ['regional'],
                    'datetime': '2026-10-01T00:00:00Z/2026-11-01T00:00:00Z',
                    'bbox': [116.0, 39.0, 117.5, 40.5],
                    'limit': 2,
                },
            },
            'response': page_response(
                [A, B],
                [{'rel': 'next',
                  'href': 'https://stac.example.test/search?cursor=p2',
                  'method': 'POST',
                  'body': {'cursor': 'p2'},
                  'merge': True}]),
        },
        {
            'request': {
                'method': 'POST',
                'url': 'https://stac.example.test/search?cursor=p2',
                'headers': COMMON_HEADERS,
                'body': {
                    'collections': ['regional'],
                    'datetime': '2026-10-01T00:00:00Z/2026-11-01T00:00:00Z',
                    'bbox': [116.0, 39.0, 117.5, 40.5],
                    'limit': 2,
                    'cursor': 'p2',
                },
            },
            'response': page_response([B, C], []),
        },
    ]}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'transcript-a.json').write_text(
        json.dumps(transcript_a(), ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    (OUT / 'transcript-b.json').write_text(
        json.dumps(transcript_b(), ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')
    (OUT / 'overlap-window.json').write_text(json.dumps({
        'start': '2026-10-01T00:00:00Z',
        'end': '2026-10-25T23:59:59Z',
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
