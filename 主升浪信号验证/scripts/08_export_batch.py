"""批次产物导出（存入 GitHub 仓库文件夹，保证环境销毁后结果与数据不丢）：
  results/batch_{n}_waves.csv   该批主升浪波段及特征
  results/batch_{n}_days.parquet 该批日级特征（命中率测算用）
  results/batch_{n}_kline.parquet 该批前复权日线（避免重复抓取的核心数据）
用法: python3 08_export_batch.py <start> <end> <batch_no>
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import pandas as pd
import numpy as np

RES = Path(__file__).resolve().parent.parent / 'results'
RES.mkdir(exist_ok=True)


def save_df(df, stem):
    """优先 parquet，无引擎时回退 pickle.gz（09_restore 两种都认）。"""
    try:
        df.to_parquet(stem.with_suffix('.parquet'), index=False, compression='zstd')
    except ImportError:
        df.to_pickle(stem.with_suffix('.pkl.gz'), compression='gzip')


def main():
    a, b, n = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    uni = load_universe().sort_values('symbol').reset_index(drop=True).iloc[a:b]
    codes = uni.code.tolist()
    syms = uni.symbol.tolist()

    # 1) 波段
    w = pd.read_csv(CACHE / 'waves.csv', dtype={'code': str})
    wb = w[w.code.isin(codes)]
    wb.to_csv(RES / f'batch_{n}_waves.csv', index=False)

    # 2) 日级特征
    day_parts = []
    for c in codes:
        p = CACHE / 'features' / f'{c}.pkl'
        if p.exists():
            d = pd.read_pickle(p)
            d['code'] = c
            day_parts.append(d)
    if day_parts:
        dd = pd.concat(day_parts, ignore_index=True)
        dd['date'] = dd['date'].str.replace('-', '').astype('int32')
        for c in ['fwd60', 'dist250', 'amt_ratio', 'pe_pct_2y']:
            dd[c] = dd[c].astype('float32')
        save_df(dd, RES / f'batch_{n}_days')

    # 3) 前复权日线 + 不复权日线
    for sub, tag in (('kline', 'kline'), ('raw_kline', 'kline_raw')):
        kl = []
        for s in syms:
            p = CACHE / sub / f'{s}.pkl'
            if p.exists():
                k = pd.read_pickle(p)
                k['code'] = s[2:]
                kl.append(k)
        if kl:
            kk = pd.concat(kl, ignore_index=True)
            kk['date'] = kk['date'].str.replace('-', '').astype('int32')
            for c in ['open', 'close', 'high', 'low', 'volume', 'amount']:
                kk[c] = kk[c].astype('float32')
            save_df(kk, RES / f'batch_{n}_{tag}')

    # 4) 估值历史（PE/PB/市值/股本/行业）
    vv = []
    for c in codes:
        p = CACHE / 'val' / f'{c}.pkl'
        if p.exists():
            v = pd.read_pickle(p)
            keep = ['TRADE_DATE', 'PE_TTM', 'PB_MRQ', 'CLOSE_PRICE', 'TOTAL_SHARES', 'BOARD_NAME']
            v = v[[k for k in keep if k in v.columns]].copy()
            v['code'] = c
            vv.append(v)
    if vv:
        vd = pd.concat(vv, ignore_index=True)
        vd['TRADE_DATE'] = vd['TRADE_DATE'].str.replace('-', '').astype('int32')
        for c in ['PE_TTM', 'PB_MRQ', 'CLOSE_PRICE', 'TOTAL_SHARES']:
            vd[c] = pd.to_numeric(vd[c], errors='coerce').astype('float32')
        save_df(vd, RES / f'batch_{n}_val')

    # 5) 财务全量（一次性，含公告日）
    fin_src = CACHE / 'fin' / 'fin_all.csv'
    if fin_src.exists() and not (RES / 'fin_all.csv.gz').exists():
        pd.read_csv(fin_src, dtype={'code': str}).to_pickle(RES / 'fin_all.pkl.gz', compression='gzip')

    import os
    for f in sorted(RES.glob(f'batch_{n}_*')):
        print(f.name, round(os.path.getsize(f) / 1e6, 1), 'MB')
    if (RES / 'fin_all.pkl.gz').exists():
        print('fin_all.pkl.gz', round(os.path.getsize(RES / 'fin_all.pkl.gz') / 1e6, 1), 'MB')
    print('waves rows:', len(wb), 'day rows:', (len(dd) if day_parts else 0), 'kline stocks:', len(kl))


if __name__ == '__main__':
    main()
