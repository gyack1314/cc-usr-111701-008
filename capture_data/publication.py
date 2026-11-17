"""目录当前页与可交付场景全集分开。"""
def assess(rows):
    next_pages=[x for r in rows if r['kind']=='query' for x in r['payload'].get('next',[])]
    assets=[r for r in rows if r['kind']=='asset']
    missing=[r['id'] for r in assets if not r['payload'].get('content_verified')]
    return {'publishable':not next_pages and not missing,
            'reasons':['搜索还有续页:'+x['href'] for x in next_pages]+['资产未核验:'+x for x in missing],
            'item_count':len([r for r in rows if r['kind']=='item'])}
