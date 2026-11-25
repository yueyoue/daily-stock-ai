"""兜底抓取：东财 push2his 被临时断连时，用腾讯/新浪补抓缺失的前复权日线。
目标：CACHE/kline/{sym}.pkl 中缺失或旧格式（无 amount 列）的股票。
口径对齐东财缓存：date(YYYY-MM-DD str) open close high low volume(手 int) amount(元 float)。
优先腾讯 stock_zh_a_hist_tx（带 amount、窗口查询轻量），失败退回新浪 stock_zh_a_daily。
用法: python3 14_fallback_sina.py [--check]   # --check 只对比腾讯/东财口径差异不写盘
"""
import sys, time, random
sys.path.insert(0, __file__.rsplit('/', 1)[0])
import pandas as pd
from utils import CACHE, load_universe

COLS = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount']
CHECK_ONLY = '--check' in sys.argv


def good(path):
    if not path.exists() or path.stat().st_size <= 200:
        return False
    try:
        return 'amount' in pd.read_pickle(path).columns
    except Exception:
        return False


def fetch_tx(sym):
    import akshare as ak
    df = ak.stock_zh_a_hist_tx(symbol=sym, start_date='20220401', end_date='20261124', adjust='qfq')
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        'date': df['date'].astype(str),
        'open': pd.to_numeric(df['open'], errors='coerce'),
        'close': pd.to_numeric(df['close'], errors='coerce'),
        'high': pd.to_numeric(df['high'], errors='coerce'),
        'low': pd.to_numeric(df['low'], errors='coerce'),
        'volume': (pd.to_numeric(df['volume'], errors='coerce') / 100.0).round().astype('int64'),  # 股->手
        'amount': pd.to_numeric(df['amount'], errors='coerce'),
    })
    return out.dropna().drop_duplicates('date').sort_values('date').reset_index(drop=True)


def fetch_sina(sym):
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=sym, start_date='20220401', end_date='20261124', adjust='qfq')
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        'date': df['date'].astype(str),
        'open': pd.to_numeric(df['open'], errors='coerce'),
        'close': pd.to_numeric(df['close'], errors='coerce'),
        'high': pd.to_numeric(df['high'], errors='coerce'),
        'low': pd.to_numeric(df['low'], errors='coerce'),
        'volume': (pd.to_numeric(df['volume'], errors='coerce') / 100.0).round().astype('int64'),
        'amount': pd.to_numeric(df['amount'], errors='coerce'),
    })
    return out.dropna().drop_duplicates('date').sort_values('date').reset_index(drop=True)


def check_consistency(syms):
    """抽样对比腾讯 vs 东财缓存的日线口径差异"""
    for sym in syms:
        old = pd.read_pickle(CACHE / 'kline' / f'{sym}.pkl')
        new = fetch_tx(sym)
        if new is None:
            print(sym, 'tx empty')
            continue
        m = old.merge(new, on='date', suffixes=('_em', '_tx'))
        m = m[m['volume_em'] > 0]
        rel = ((m['close_em'] - m['close_tx']).abs() / m['close_em']).max()
        amrel = ((m['amount_em'] - m['amount_tx']).abs() / m['amount_em']).max()
        print(f'{sym}: overlap {len(m)} days, max|dclose|={rel:.5f}, max|damount|={amrel:.5f}')
        time.sleep(0.5)


def main():
    uni = load_universe()
    syms = uni.symbol.tolist()

    if CHECK_ONLY:
        check_consistency(['sh600000', 'sz000002', 'sh601011'])
        return

    todo = [s for s in syms if not good(CACHE / 'kline' / f'{s}.pkl')]
    print(f'needed {len(todo)}')
    ok = empty = fail = 0
    for i, sym in enumerate(todo, 1):
        df = None
        try:
            df = fetch_tx(sym)
        except Exception as e:
            print(f'  tx fail {sym}: {repr(e)[:120]}')
        if df is None or df.empty:
            try:
                df = fetch_sina(sym)
            except Exception as e:
                print(f'  sina fail {sym}: {repr(e)[:120]}')
        if df is None or df.empty:
            empty += 1
            print(f'  {sym} empty (退市/新股无行情?)')
        else:
            df.to_pickle(CACHE / 'kline' / f'{sym}.pkl')
            ok += 1
            if i % 20 == 0:
                print(f'progress {i}/{len(todo)} ok={ok} empty={empty} fail={fail}')
        time.sleep(0.35 + random.random() * 0.3)
    print(f'DONE needed={len(todo)} ok={ok} empty={empty} fail={fail}')


if __name__ == '__main__':
    main()
