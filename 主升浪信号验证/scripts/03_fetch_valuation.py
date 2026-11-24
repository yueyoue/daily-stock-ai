"""抓取每日估值（东财 datacenter RPT_VALUEANALYSIS_DET）：PE_TTM / PB_MRQ / 收盘 / 总股本 / 行业
cache/val/{code}.pkl，2021-06-01 起（满足启动前 500 交易日 PE 分位）。
"""
import sys, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from utils import session, get_json, CACHE, load_universe
import pandas as pd

URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
COLS = 'SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ,CLOSE_PRICE,TOTAL_SHARES,BOARD_NAME'
REF = 'https://data.eastmoney.com/'

def fetch_one(code):
    out = CACHE / 'val' / f'{code}.pkl'
    if out.exists() and out.stat().st_size > 200:
        return code, 'skip'
    s = session()
    rows, page = [], 1
    while True:
        js = get_json(s, URL, params={
            'reportName': 'RPT_VALUEANALYSIS_DET', 'columns': COLS,
            'filter': f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'2021-06-01\')',
            'pageSize': 500, 'pageNumber': page,
            'sortColumns': 'TRADE_DATE', 'sortTypes': 1}, referer=REF)
        res = (js or {}).get('result') or {}
        data = res.get('data') or []
        rows += data
        pages = res.get('pages') or 1
        if page >= pages or not data:
            break
        page += 1
        time.sleep(0.05 + random.random() * 0.1)
    if not rows:
        return code, 'empty'
    df = pd.DataFrame(rows)
    df['TRADE_DATE'] = df['TRADE_DATE'].str[:10]
    df = df.drop_duplicates('TRADE_DATE').sort_values('TRADE_DATE').reset_index(drop=True)
    df.to_pickle(out)
    return code, f'ok {len(df)}'

def main():
    uni = load_universe()
    codes = uni.code.tolist()
    print('stocks:', len(codes))
    done = err = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_one, c) for c in codes]
        for i, f in enumerate(as_completed(futs), 1):
            code, st = f.result()
            if st.startswith('err') or st == 'empty':
                err += 1
                print('  ', code, st)
            done += 1
            if i % 200 == 0:
                print(f'progress {i}/{len(codes)} err={err}', flush=True)
    print('DONE val', done, 'err', err)

if __name__ == '__main__':
    main()
