"""抓取全 A 股票清单（新浪 Market_Center），输出 cache/universe.csv"""
import sys, time, json
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from utils import session, get_json, CACHE

URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
       'Market_Center.getHQNodeData')

def main():
    s = session()
    rows = []
    page = 1
    while True:
        js = get_json(s, URL, params={
            'page': page, 'num': 100, 'node': 'hs_a',
            'sort': 'symbol', 'asc': 1})
        if not js:
            break
        rows += js
        print('page', page, 'total', len(rows))
        if len(js) < 100:
            break
        page += 1
        time.sleep(0.15)
    out = []
    for r in rows:
        sym = r['symbol']                      # sh600000 / sz000001 / bj920000
        if not (sym.startswith('sh6') or sym.startswith('sz0') or sym.startswith('sz3')):
            continue
        name = r['name']
        out.append({'symbol': sym, 'code': r['code'], 'name': name,
                    'is_st': int('ST' in name.upper()),
                    'mktcap': float(r.get('mktcap') or 0)})
    import pandas as pd
    df = pd.DataFrame(out).sort_values('symbol').reset_index(drop=True)
    df.to_csv(CACHE / 'universe.csv', index=False)
    print('universe:', len(df), 'st:', df.is_st.sum())
    print(df.head())

if __name__ == '__main__':
    main()
