"""从 GitHub 上的 batch_*_kline.parquet 恢复逐票日线缓存（环境销毁后免重抓）。
用法: python3 09_restore_cache.py [results目录]  # 默认 ../results
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import pandas as pd

def main():
    res = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / 'results'
    # 新环境缓存为空时补种 universe.csv（load_universe 依赖；results 归档里有）——否则 FileNotFoundError
    if not (CACHE / 'universe.csv').exists():
        src = res / 'universe.csv'
        if src.exists():
            import shutil
            shutil.copy(src, CACHE / 'universe.csv')
            print('seeded cache/universe.csv from', src)
    uni = load_universe()
    sym_of = dict(zip(uni.code, uni.symbol))
    n = 0
    for sub, patterns in (('kline', ['batch_*_kline.parquet', 'batch_*_kline.pkl.gz']),
                          ('raw_kline', ['batch_*_kline_raw.parquet', 'batch_*_kline_raw.pkl.gz'])):
        files = []
        for pat in patterns:
            files += sorted(res.glob(pat))
        for p in files:
            df = pd.read_parquet(p) if p.suffix == '.parquet' else pd.read_pickle(p)
            for code, g in df.groupby('code'):
                sym = sym_of.get(str(code))
                if not sym:
                    continue
                out = CACHE / sub / f'{sym}.pkl'
                if out.exists():
                    continue
                g = g.drop(columns=['code']).copy()
                # 归档日期 int32(20220401) -> 流水线内部 ISO(2022-04-01)（FinLookup/估值reindex/窗口比较依赖）
                g['date'] = pd.to_datetime(g['date'].astype(str)).dt.strftime('%Y-%m-%d')
                g = g.sort_values('date').reset_index(drop=True)
                g.to_pickle(out)
                n += 1
            print(p.name, 'restored cumulative', n)
    print('DONE restored', n)

if __name__ == '__main__':
    main()
