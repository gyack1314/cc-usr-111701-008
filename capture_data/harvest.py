"""可审计的 STAC 搜索收割引擎。

引擎不直接访问网络：传输层以参数注入，生产环境可接 urllib 实现，
测试与离线审计使用冻结请求/响应转录的重放传输。本模块负责：

* 冻结查询（方法、URL、请求头、body、采集窗口）并给出查询摘要；
* 记录服务身份与每一页的请求/响应顺序、字节摘要；
* 按 item 的 collection/id 与内容版本（整条 Feature 规范化哈希）去重，
  同一内容版本重复出现只留痕不改账，内容变化记为修订；
* 续页保留 STAC next 链接的 GET/POST、body、merge、headers 语义，
  跨凭据域（scheme/host/port 变化）时剥离凭据，不越权携带；
* 通过服务快照能力或重叠窗口证明覆盖范围，证明不了时明确给出未知缺口。
"""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

PROTOCOL = 'stac-search'
# 合成环境使用的快照一致性标识；真实服务以其 conformance 声明为准。
SNAPSHOT_CONFORMANCE = 'https://stac-api.example.com/extensions/snapshot/v1'
CREDENTIAL_HEADERS = frozenset({
    'authorization', 'proxy-authorization', 'cookie', 'set-cookie',
    'x-api-key', 'api-key',
})


def canonical_bytes(obj):
    """跨进程稳定的规范化字节，用于内容版本与摘要。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(obj):
    return 'sha256:' + hashlib.sha256(canonical_bytes(obj)).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def is_credential_header(name, extra=()):
    lowered = name.lower()
    return lowered in CREDENTIAL_HEADERS or lowered in {x.lower() for x in extra}


def credential_zone(url):
    """凭据域 = scheme/host/port；跨域的 next 不得继续携带凭据。"""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        raise ValueError('仅支持 http/https 续页地址: ' + url)
    if not parts.hostname:
        raise ValueError('续页地址缺少主机: ' + url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    return parts.scheme, parts.hostname.lower(), port


def freeze_query(method, url, headers=None, body=None, window=None):
    """冻结一次搜索查询的完整请求语义。"""
    headers = dict(headers or {})
    frozen = {
        'protocol': PROTOCOL,
        'method': method.upper(),
        'url': url,
        'headers': headers,
        'body': body,
        'window': window or {},
        'credential_headers': sorted(
            h for h in headers if is_credential_header(h)),
        'created_at': now_iso(),
    }
    frozen['query_digest'] = digest({
        'protocol': frozen['protocol'], 'method': frozen['method'],
        'url': frozen['url'], 'headers': frozen['headers'],
        'body': frozen['body'], 'window': frozen['window'],
    })
    return frozen


def _header(headers, name):
    for key, value in (headers or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def follow_next(origin, link, extra_credential_headers=()):
    """按 STAC next 链接构造下一页请求，保留请求语义并隔离凭据。

    返回 (request, audit)。audit 记录跨域与凭据剥离事实，供收割收据审计。
    """
    href = link.get('href')
    if not href:
        raise ValueError('next 链接缺少 href')
    new_zone = credential_zone(href)
    old_zone = credential_zone(origin['url'])
    cross_zone = new_zone != old_zone

    method = (link.get('method') or 'GET').upper()
    if method not in ('GET', 'POST'):
        raise ValueError('不支持的续页方法: ' + method)

    headers = dict(origin.get('headers') or {})
    if link.get('headers'):
        if not isinstance(link['headers'], dict):
            raise ValueError('next 链接 headers 必须是对象')
        headers.update(link['headers'])

    stripped = []
    if cross_zone:
        for name in list(headers):
            if is_credential_header(name, extra_credential_headers):
                stripped.append(name)
                del headers[name]

    body = None
    if method == 'POST':
        if link.get('body') is not None:
            sent_body = link['body']
            if isinstance(sent_body, str):
                sent_body = json.loads(sent_body)
            if not isinstance(sent_body, dict):
                raise ValueError('POST 续页 body 必须是 JSON 对象')
            if link.get('merge'):
                body = {**(origin.get('body') or {}), **sent_body}
            else:
                body = sent_body
        else:
            # 服务器未显式给 body 时，POST 续页携带原查询 body，避免丢语义。
            body = origin.get('body')

    request = {'method': method, 'url': href, 'headers': headers, 'body': body}
    audit = {
        'href': href,
        'declared_method': link.get('method'),
        'cross_credential_zone': cross_zone,
        'stripped_credential_headers': sorted(stripped),
        'carried_post_body': body is not None,
    }
    return request, audit


def redact_headers(headers):
    """审计留痕时不写明文凭据，只保留值摘要。"""
    out = {}
    for name, value in (headers or {}).items():
        if is_credential_header(name):
            out[name] = 'redacted:' + digest(str(value))[:23]
        else:
            out[name] = value
    return out


def item_identity(feature):
    """提取 item 身份与内容版本；资产地址随同版内容一起绑定。"""
    collection = feature.get('collection')
    item_id = feature.get('id')
    if not collection or not item_id:
        raise ValueError('Feature 缺少 collection/id 身份')
    content_version = digest(feature)
    datetime_value = feature.get('properties', {}).get('datetime')
    assets = []
    for key in sorted((feature.get('assets') or {}).keys()):
        asset = feature['assets'][key]
        assets.append({
            'key': key,
            'href': asset.get('href', ''),
            'metadata': {k: v for k, v in asset.items() if k != 'href'},
        })
    return collection, item_id, content_version, datetime_value, assets


class ReplayTransport:
    """按冻结转录依次返回响应，并核对实际请求与冻结语义是否一致。"""

    def __init__(self, exchanges):
        self.exchanges = list(exchanges)
        self.position = 0
        self.sent = []

    def request(self, method, url, headers=None, body=None):
        if self.position >= len(self.exchanges):
            raise EOFError('续页超出冻结转录范围')
        expected = self.exchanges[self.position]['request']
        self.position += 1
        if (method, url) != (expected['method'], expected['url']):
            raise ValueError('冻结请求序列不匹配: %s %s' % (method, url))
        if 'body' in expected and expected['body'] != body:
            raise ValueError('续页请求 body 语义丢失')
        actual_headers = dict(headers or {})
        for name, value in (expected.get('headers') or {}).items():
            if actual_headers.get(name) != value:
                raise ValueError('续页请求头不匹配: ' + name)
        for name in expected.get('forbidden_headers') or []:
            if name in actual_headers:
                raise ValueError('越权向续页携带凭据: ' + name)
        self.sent.append({
            'method': method, 'url': url,
            'headers': actual_headers, 'body': body,
        })
        response = self.exchanges[self.position - 1]['response']
        if not (200 <= int(response.get('status', 200)) < 300):
            raise ValueError('服务响应状态异常: %s' % response.get('status'))
        return response

    @property
    def exhausted(self):
        return self.position == len(self.exchanges)


def _conformance(body, headers):
    declared = list(body.get('conformsTo') or [])
    raw = _header(headers, 'x-stac-conformance')
    if raw:
        declared.extend(part.strip() for part in raw.split(',') if part.strip())
    return sorted(set(declared))


def _next_links(body):
    return [link for link in body.get('links', []) if link.get('rel') == 'next']


def harvest(query, transport, max_pages=1000, extra_credential_headers=()):
    """执行一次收割，返回可审计的收割收据（纯数据，可 JSON 序列化）。"""
    started = now_iso()
    request = {
        'method': query['method'], 'url': query['url'],
        'headers': dict(query.get('headers') or {}),
        'body': query.get('body'),
    }
    collection = ','.join(query.get('window', {}).get('collections') or []) or \
        ','.join(sorted((query.get('body') or {}).get('collections') or []))
    pages = []
    # (collection,id) -> 聚合条目；同一 id 的多个内容版本全部保留。
    observed = {}
    seen_requests = set()
    terminated = False
    interruption = None

    for sequence in range(max_pages):
        request_key = digest(request)
        if request_key in seen_requests:
            interruption = {'reason': 'pagination-loop', 'page': sequence,
                            'url': request['url']}
            break
        seen_requests.add(request_key)

        try:
            response = transport.request(
                request['method'], request['url'],
                request['headers'], request['body'])
        except EOFError:
            interruption = {'reason': 'transcript-exhausted',
                            'page': sequence, 'url': request['url']}
            break
        body = response.get('body')
        if not isinstance(body, dict) or body.get('type') != 'FeatureCollection':
            raise ValueError('响应不是 STAC FeatureCollection')
        headers = response.get('headers') or {}
        snapshot_id = _header(headers, 'x-snapshot-id')
        page = {
            'sequence': sequence,
            'request': {
                'method': request['method'],
                'url': request['url'],
                'headers': redact_headers(request['headers']),
                'body': request['body'],
                'credential_headers_sent': sorted(
                    h for h in request['headers']
                    if is_credential_header(h, extra_credential_headers)),
            },
            'response': {
                'status': int(response.get('status', 200)),
                'headers': dict(headers),
                'body_digest': digest(body),
                'snapshot_id': snapshot_id,
                'feature_count': len(body.get('features') or []),
            },
            'response_body': body,
            'next': None,
        }

        for feature in body.get('features') or []:
            coll, item_id, version, dt, assets = item_identity(feature)
            entry = observed.setdefault((coll, item_id), {
                'collection': coll, 'id': item_id, 'datetime': dt,
                'occurrences': [], 'versions': [],
            })
            entry['occurrences'].append(sequence)
            version_entry = next(
                (v for v in entry['versions']
                 if v['content_version'] == version), None)
            if version_entry is None:
                version_entry = {
                    'content_version': version,
                    'assets': assets,
                    'pages': [sequence],
                    'feature_digest': digest(feature),
                }
                entry['versions'].append(version_entry)
            else:
                # 翻页顺序漂移导致的同版重复：只记页码，不新增版本。
                version_entry['pages'].append(sequence)

        links = _next_links(body)
        if not links:
            terminated = True
            pages.append(page)
            break
        if len(links) > 1:
            interruption = {'reason': 'ambiguous-next-links', 'page': sequence}
            pages.append(page)
            break
        request, next_audit = follow_next(
            request, links[0], extra_credential_headers)
        page['next'] = next_audit
        pages.append(page)
    else:
        interruption = {'reason': 'max-pages-exceeded', 'limit': max_pages}

    snapshot_ids = [p['response']['snapshot_id'] for p in pages]
    stable_snapshot = None
    if snapshot_ids and all(snapshot_ids) and len(set(snapshot_ids)) == 1:
        stable_snapshot = snapshot_ids[0]
    service = {
        'origin': query['url'],
        'credential_zone': list(credential_zone(query['url'])),
        'server': _header(pages[0]['response']['headers'], 'server')
                  if pages else None,
        'stac_versions': sorted({
            feat.get('stac_version')
            for page in pages
            for feat in (page.get('response_body', {})
                         .get('features') or [])
            if feat.get('stac_version')
        }),
        'conforms_to': _conformance(
            pages[-1].get('response_body', {}) if pages else {},
            pages[0]['response']['headers'] if pages else {}),
        'snapshot_ids': snapshot_ids,
        'stable_snapshot_id': stable_snapshot,
    }
    items = []
    for entry in observed.values():
        versions = entry['versions']
        items.append({
            'collection': entry['collection'],
            'id': entry['id'],
            'datetime': entry['datetime'],
            'occurrences': entry['occurrences'],
            'duplicate_occurrences': len(entry['occurrences']) - 1,
            'mid_run_revision': len(versions) > 1,
            'versions': versions,
        })
    items.sort(key=lambda x: (x['collection'], x['id']))

    receipt = {
        'harvest_id': 'hv_' + digest({
            'query': query['query_digest'], 'started': started,
            'pages': [p['response']['body_digest'] for p in pages],
        })[7:23],
        'protocol': PROTOCOL,
        'collection': collection,
        'query': query,
        'service': service,
        'started_at': started,
        'finished_at': now_iso(),
        'terminated': terminated,
        'interruption': interruption,
        'pages': pages,
        'items': items,
        'coverage': _initial_coverage(terminated, pages, service, query),
    }
    return receipt


def _initial_coverage(terminated, pages, service, query):
    """无外部证据时的覆盖判断：快照能力可证明，否则一律标示未知缺口。"""
    if not terminated:
        return {
            'state': 'unknown',
            'evidence': {},
            'gaps': [{
                'window': query.get('window') or {},
                'reason': 'pagination-did-not-terminate',
            }],
        }
    if (service['stable_snapshot_id']
            and SNAPSHOT_CONFORMANCE in service['conforms_to']):
        return {
            'state': 'proven-snapshot',
            'evidence': {
                'snapshot_id': service['stable_snapshot_id'],
                'pages': len(pages),
            },
            'gaps': [],
        }
    if service['snapshot_ids'] and any(service['snapshot_ids']) \
            and not service['stable_snapshot_id']:
        reason = 'snapshot-id-changed-mid-pagination'
    else:
        reason = 'no-snapshot-or-overlap-evidence'
    return {
        'state': 'unknown',
        'evidence': {'snapshot_ids': service['snapshot_ids']},
        'gaps': [{'window': query.get('window') or {}, 'reason': reason}],
    }


def _items_in_window(receipt, start, end):
    placed = {}
    for item in receipt['items']:
        dt = item['datetime']
        if dt is None or not (start <= dt <= end):
            continue
        for version in item['versions']:
            placed[(item['collection'], item['id'],
                    version['content_version'])] = dt
    return placed


def corroborate_overlap(receipt_a, receipt_b, overlap_window):
    """用两个收割的重叠窗口互相印证覆盖范围。

    两个收割都必须自然终止，且重叠窗口内 (collection,id,内容版本) 完全一致，
    才能证明重叠区覆盖；任何不一致都回退为未知缺口。
    """
    start = overlap_window['start']
    end = overlap_window['end']
    if not (receipt_a['terminated'] and receipt_b['terminated']):
        return {
            'state': 'unknown', 'evidence': {},
            'gaps': [{'window': overlap_window,
                      'reason': 'overlap-run-did-not-terminate'}],
        }
    set_a = _items_in_window(receipt_a, start, end)
    set_b = _items_in_window(receipt_b, start, end)
    keys_a, keys_b = set(set_a), set(set_b)
    if set_a and set_a == set_b:
        return {
            'state': 'proven-overlap',
            'evidence': {
                'window': overlap_window,
                'overlap_item_versions': len(set_a),
                'harvests': [receipt_a['harvest_id'],
                             receipt_b['harvest_id']],
            },
            'gaps': [],
        }
    only_a = sorted('%s/%s' % (c, i) for c, i, _ in keys_a - keys_b)
    only_b = sorted('%s/%s' % (c, i) for c, i, _ in keys_b - keys_a)
    return {
        'state': 'unknown',
        'evidence': {'only_in_first': only_a, 'only_in_second': only_b},
        'gaps': [{
            'window': overlap_window,
            'reason': 'overlap-windows-disagree',
            'only_in_first': only_a,
            'only_in_second': only_b,
        }],
    }


def apply_overlap_evidence(receipt, other, overlap_window):
    """把重叠窗口印证结果写回收据；印证失败不会伪装成已覆盖。"""
    receipt['coverage'] = corroborate_overlap(receipt, other, overlap_window)
    return receipt['coverage']
