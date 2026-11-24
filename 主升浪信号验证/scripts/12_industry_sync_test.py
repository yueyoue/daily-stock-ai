"""前瞻检验：行业同步性作为选股/确认条件是否成立。
对每个交易日，统计同行业处于「缩量+回调」状态的股票数，比较不同强度下的未来60日命中率。
输出 tables/industry_sync_fwd.csv
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE
import pandas as pd
import numpy as np

OUT = Path(__file__).resolve().parent.parent / 'tables'

def main():
    # 日级特征（批次1）
    files = sorted(CACHE.glob('batch_days_backup*.pkl.gz'))
    d = pd.read_parquet(CACHE.parent.parent.parent / 'workspace' / 'x') if False else None
    day_parts = []
    for p in sorted((CACHE / 'features').glob('*.pkl')):
        x = pd.read_pickle(p)
        x['code'] = p.stem
        day_parts.append(x)
    d = pd.concat(day_parts, ignore_index=True)
    d['date'] = d['date'].astype(str)
    # 行业映射
    ind = {}
    for p in (CACHE / 'val').glob('*.pkl'):
        v = pd.read_pickle(p)
        if 'BOARD_NAME' in v.columns and len(v):
            b = v['BOARD_NAME'].dropna()
            if len(b):
                ind[p.stem] = b.iloc[-1]
    d['industry'] = d['code'].map(ind)
    d = d.dropna(subset=['industry', 'fwd60'])

    sig = (d.dist250 <= -0.15) & (d.dist250 >= -0.40) & (d.amt_ratio < 0.9)   # 信号二状态
    d['sig'] = sig.fillna(False).astype(int)

    # 同行业当日处于信号状态的股票数
    grp = d.groupby(['industry', 'date'])['sig'].transform('sum')
    d['ind_sig_n'] = grp

    rows = []
    def add(name, sub):
        rows.append(dict(条件=name, n=len(sub),
                         命中率=round((sub.fwd60 >= 0.40).mean() * 100, 1) if len(sub) else np.nan,
                         fwd60中位=round(sub.fwd60.median() * 100, 1) if len(sub) else np.nan))
    add('全部（基准）', d)
    add('信号二状态（行业内<3只同时）', d[(d.sig == 1) & (d.ind_sig_n < 3)])
    add('信号二状态（行业内3~4只）', d[(d.sig == 1) & (d.ind_sig_n.between(3, 4))])
    add('信号二状态（行业内>=5只）', d[(d.sig == 1) & (d.ind_sig_n >= 5)])
    add('非信号股·同行业>=5只在信号态（扩散效应）',
        d[(d.sig == 0) & (d.ind_sig_n >= 5)])
    add('缩量+回调(宽松) 行业内>=5只', d[(d.sig == 1) & (d.ind_sig_n >= 5) & (d.pe_pct_2y < 0.5)])
    # 纯缩量+回调 对照（不看行业）
    s2 = (d.dist250 <= -0.15) & (d.amt_ratio < 0.8)
    add('缩量<0.8+回调（不看行业）', d[s2.fillna(False)])
    t = pd.DataFrame(rows)
    t.to_csv(OUT / 'industry_sync_fwd.csv', index=False)
    print(t.to_string())

if __name__ == '__main__':
    main()
