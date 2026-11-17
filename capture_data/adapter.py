import json
def decode(raw, manifest):
    doc=json.loads(raw.decode('utf-8'))
    if doc.get('type')!='FeatureCollection':raise ValueError('搜索响应类型非法')
    records=[{'kind':'query','id':manifest['source_url'],'version':'received','refs':[],
              'payload':{'next':[link for link in doc.get('links',[]) if link.get('rel')=='next']}}]
    for feature in doc['features']:
        item=feature['collection']+'/'+feature['id'];version=feature['properties']['datetime']
        records.append({'kind':'collection','id':feature['collection'],'version':'received','refs':[],
                        'payload':{'source_url':manifest['source_url']}})
        records.append({'kind':'item','id':item,'version':version,
                        'refs':[feature['collection'],manifest['source_url']],
                        'payload':{'stac_version':feature['stac_version']}})
        for key,asset in feature['assets'].items():
            records.append({'kind':'asset','id':item+'/'+key,'version':version,'refs':[item],
                            'payload':asset})
    return {'protocol':manifest['protocol'],'collection':manifest['collection'],'records':records}
