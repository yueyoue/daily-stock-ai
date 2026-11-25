"""每日增量归档：把缓存里基线归档之后的新交易日日线导出到 results/daily/。
每个交易日一个小文件（write-once）：results/daily/YYYYMMDD_kline.pkl.gz / YYYYMMDD_kline_raw.pkl.gz，
列格式与 batch_*_kline 归档一致（code, date int32, OHLCV+amount float32）。
基线全量归档 batch_*_kline*（截至 2026-11-24）保持不变；读取时按 (code,date) 去重合并基线+增量即可。
用法: python3 15_export_daily.py   # 幂等：已导出日期跳过，缺口自动补齐；当日未收盘不导出
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import pandas as pd

RES = Path(__file__).resolve().parent.parent / 'results'
DAILY = RES / 'daily'
FLOAT_COLS = ['open', 'close', 'high', 'low', 'volume', 'amount']


def read_archives(res, patterns):
    dfs = []
    for pat in patterns:
        for p in sorted(res.glob(pat)):
            dfs.append(pd.read_parquet(p) if p.suffix == '.parquet' else pd.read_pickle(p))
    return pd.concat(dfs, ignore_index=True) if dfs else None


def cover_key(s):
    return s.astype(str).str.replace('-', '')


def main():
    DAILY.mkdir(exist_ok=True)
    uni = load_universe()
    sym2code = dict(zip(uni.symbol, uni.code))
    # 交易日历口径：只导出北京时间已收盘的日期（当日盘中/未收盘不导出，防半日数据冻结）
    today_bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime('%Y%m%d')

    made = 0
    for sub, tag in (('kline', 'kline'), ('raw_kline', 'kline_raw')):
        # 已覆盖 = 全量基线归档的最新日期 ∪ 已导出的 daily 日期
        covered = set()
        base_max = ''
        arch = read_archives(RES, [f'batch_{n}_{tag}.pkl.gz' for n in range(1, 5)] +
                                  [f'batch_{n}_{tag}.parquet' for n in range(1, 5)])
        if arch is not None and len(arch):
            base_max = cover_key(arch['date']).max()
        for p in DAILY.glob(f'*_{tag}.pkl.gz'):
            covered.add(p.name.split('_')[0])

        parts = []
        for s in uni.symbol:
            p = CACHE / sub / f'{s}.pkl'
            if not p.exists():
                continue
            k = pd.read_pickle(p)
            if 'amount' not in k.columns or not len(k):
                continue
            k = k.copy()
            k['code'] = sym2code[s]
            parts.append(k)
        if not parts:
            print(f'{tag}: no cache data')
            continue
        df = pd.concat(parts, ignore_index=True)
        dk = cover_key(df['date'])
        need = df[(dk > base_max) & (dk < today_bj) & (~dk.isin(covered))]
        if not len(need):
            print(f'{tag}: nothing new (baseline max {base_max}, exported {len(covered)} days)')
            continue
        need = need.assign(_d=cover_key(need['date']))
        for d, g in need.groupby('_d'):
            g = g.drop(columns=['_d']).copy()
            g['date'] = g['date'].astype(str).str.replace('-', '').astype('int32')
            for c in FLOAT_COLS:
                g[c] = g[c].astype('float32')
            out = DAILY / f'{d}_{tag}.pkl.gz'
            g.to_pickle(out, compression='gzip')
            made += 1
            print(out.name, 'codes', g.code.nunique(), 'rows', len(g))
    print('DONE export_daily, files made:', made)


if __name__ == '__main__':
    main()
