"""抓取全市场日线（东财 push2his 分片，前复权 fqt=1 + 不复权 fqt=0，含真实成交额）。
输出 cache/kline/{sym}.pkl（qfq）与 cache/raw_kline/{sym}.pkl（不复权）。
可断点续跑；旧格式（无 amount 列）自动重抓。
"""
import sys, time, random
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from utils import session, get_json, CACHE, load_universe
import pandas as pd

SHARDS = ['1', '2', '3', '5', '11', '23', '33', '66', '77', '92']
COLS = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount']

def fetch_one(sym, fq='qfq'):
    sub = 'kline' if fq == 'qfq' else 'raw_kline'
    out = CACHE / sub / f'{sym}.pkl'
    if out.exists() and out.stat().st_size > 200:
        try:
            if 'amount' in pd.read_pickle(out).columns:
                return sym, 'skip'
        except Exception:
            pass
    code = sym[2:]
    secid = ('1.' if sym.startswith('sh') else '0.') + code
    fqt = 1 if fq == 'qfq' else 0
    url_tpl = 'https://{}.push2his.eastmoney.com/api/qt/stock/kline/get'
    klines = []
    for attempt in range(8):
        shard = random.choice(SHARDS)
        js = get_json(session(), url_tpl.format(shard), params={
            'secid': secid,
            'fields1': 'f1,f2,f3,f4,f5,f6',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57',
            'klt': 101, 'fqt': fqt, 'beg': '20220401', 'end': '20261124'}, tries=1)
        node = (js or {}).get('data') or {}
        klines = node.get('klines') or []
        if klines:
            break
        time.sleep(1.0 + attempt * 0.8)
    time.sleep(0.25 + random.random() * 0.25)
    if not klines:
        return sym, 'empty'
    rows = [k.split(',') for k in klines]
    df = pd.DataFrame(rows)
    df = df.iloc[:, :7]
    df.columns = COLS
    for c in COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna().drop_duplicates('date').sort_values('date').reset_index(drop=True)
    df.to_pickle(out)
    return sym, f'ok {len(df)}'


def main():
    uni = load_universe()
    syms = uni.symbol.tolist()
    only = sys.argv[1] if len(sys.argv) > 1 else None     # qfq / raw / both
    jobs = []
    if only in (None, 'both', 'qfq'):
        jobs += [(s, 'qfq') for s in syms]
    if only in (None, 'both', 'raw'):
        jobs += [(s, 'raw') for s in syms]
    print('jobs:', len(jobs), flush=True)
    done = err = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(fetch_one, s, f) for s, f in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            sym, st = f.result()
            if st in ('empty', 'err') or st.startswith('err'):
                err += 1
                print('  ', sym, st, flush=True)
            done += 1
            if i % 200 == 0:
                print(f'progress {i}/{len(jobs)} err={err}', flush=True)
    print('DONE kline', done, 'err', err, flush=True)


if __name__ == '__main__':
    main()
