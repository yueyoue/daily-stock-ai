"""用户两个命题的检验（13_belief_test.py）：
  A. 「板块热度高 → 该板块票易走主升」：构造行业热度指标（行业60日动量、站上60日线占比、
     贴近250日高点占比、成交额份额），看热度分位对成员股未来60日命中率的前瞻提升。
  B. 「板块里涨的都是基本面比较好的」：
     B1 条件于有浪：行业内 ROE/毛利率/净利增速排名与波段涨幅的关系；
     B2 浪的发生：启动日 vs 对照日 的 ROE/毛利率分布。
输出 tables/belief_sector_heat.csv、tables/belief_fund.csv
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / 'tables'


def load_val_map():
    ind, roe_map = {}, {}
    for p in (CACHE / 'val').glob('*.pkl'):
        v = pd.read_pickle(p)
        if 'BOARD_NAME' in v.columns and len(v):
            b = v['BOARD_NAME'].dropna()
            if len(b):
                ind[p.stem] = b.iloc[-1]
    return ind


def main():
    ind = load_val_map()
    uni = load_universe().sort_values('symbol').reset_index(drop=True)

    # ---------- A. 行业热度 ----------
    # 逐股逐日：ret60、是否站上60日线、是否贴近250日高点(±5%)、20日均额
    per = []
    for sym, code in zip(uni.symbol, uni.code):
        kp = CACHE / 'kline' / f'{sym}.pkl'
        if not kp.exists():
            continue
        df = pd.read_pickle(kp)
        if len(df) < 260:
            continue
        c = df['close'].to_numpy(float)
        s = pd.Series(c)
        ma60 = s.rolling(60).mean().to_numpy()
        ret60 = (s / s.shift(60) - 1).to_numpy()
        hh = s.rolling(250).max().to_numpy()
        amt20 = pd.Series(df['amount'].to_numpy(float) if 'amount' in df.columns
                          else df['volume'].to_numpy(float) * c).rolling(20).mean().to_numpy()
        per.append(pd.DataFrame({
            'date': df['date'].values, 'code': code,
            'industry': ind.get(code),
            'above60': (c > ma60).astype(float),
            'ret60': ret60,
            'nearhigh': (c > 0.95 * hh).astype(float),
            'amt20': amt20}))
    P = pd.concat(per, ignore_index=True)
    P = P.dropna(subset=['industry'])
    grp = P.groupby(['industry', 'date'])
    heat = pd.DataFrame({
        'ind_ret60': grp['ret60'].mean(),
        'ind_above60': grp['above60'].mean(),
        'ind_nearhigh': grp['nearhigh'].mean(),
        'ind_amt20': grp['amt20'].sum()}).reset_index()
    mkt = P.groupby('date')['amt20'].sum().rename('mkt_amt20')
    heat = heat.merge(mkt, on='date')
    heat['ind_amt_share'] = heat.ind_amt20 / heat.mkt_amt20
    print('heat rows:', len(heat), flush=True)

    # 成员股日级命中率（batch features）
    day_parts = []
    for p in sorted((CACHE / 'features').glob('*.pkl')):
        x = pd.read_pickle(p)
        x['code'] = p.stem
        day_parts.append(x)
    D = pd.concat(day_parts, ignore_index=True)
    D['date'] = D['date'].astype(str)
    D['industry'] = D['code'].map(ind)
    D = D.dropna(subset=['industry', 'fwd60'])
    D = D.merge(heat, on=['industry', 'date'], how='left')

    rows = []
    def add(name, sub):
        rows.append(dict(条件=name, n=len(sub),
                         hit40=round((sub.fwd60 >= 0.40).mean() * 100, 1) if len(sub) else np.nan,
                         fwd60_med=round(sub.fwd60.median() * 100, 1) if len(sub) else np.nan))
    add('全部（基准）', D)
    for f in ['ind_ret60', 'ind_above60', 'ind_nearhigh', 'ind_amt_share']:
        try:
            q = pd.qcut(D[f], 5, duplicates='drop')
        except Exception:
            continue
        for qv, g in D.groupby(q, observed=True):
            add(f'{f} {str(qv)[:24]}', g)
    tA = pd.DataFrame(rows)
    tA.to_csv(OUT / 'belief_sector_heat.csv', index=False)
    print(tA.to_string(), flush=True)

    # ---------- B1. 条件于浪：行业内基本面排名 vs 涨幅 ----------
    w = pd.read_csv(CACHE / 'waves.csv', dtype={'code': str})
    w['industry'] = w['code'].map(ind)
    w = w.dropna(subset=['industry'])
    rows = []
    for f in ['roe', 'gpr', 'profit_yoy', 'rev_yoy']:
        s = w.dropna(subset=[f, 'gain_high'])
        if len(s) < 200:
            continue
        rk = s.groupby('industry')[f].rank(pct=True)      # 行业内分位
        rho = rk.corr(s.gain_high.rank())
        ter = pd.qcut(rk, 3, labels=['行业内后1/3', '行业内中1/3', '行业内前1/3'], duplicates='drop')
        med = s.groupby(ter, observed=True).gain_high.median() * 100
        occ = s.groupby(ter, observed=True).size()
        rows.append(dict(factor=f, spearman_within_ind=round(rho, 3),
                         **{f'涨幅中位_{k}': round(v, 1) for k, v in med.items()},
                         **{f'n_{k}': int(v) for k, v in occ.items()}))
    tB = pd.DataFrame(rows)
    tB.to_csv(OUT / 'belief_fund_gain.csv', index=False)
    print(tB.to_string(), flush=True)

    # ---------- B2. 浪发生 vs 基本面质量（启动日 vs 对照日） ----------
    import importlib
    ex = importlib.import_module('10_extra_features')
    ex.fetch_index()
    fin = pd.read_csv(CACHE / 'fin' / 'fin_all.csv', dtype={'code': str})
    extras_p = CACHE / 'extras_days.parquet'
    E = pd.read_parquet(extras_p) if extras_p.exists() else pd.read_pickle(CACHE / 'extras_days.pkl.gz')
    E['date'] = E['date'].astype(str)
    fin['nd'] = fin['notice_date'].astype(str)
    rows = []
    for code, g in E.groupby('code'):
        f = fin[fin.code == code].sort_values('nd')
        if not len(f):
            continue
        keys = f['nd'].to_numpy()
        for col, out in [('roe', 'fin_roe'), ('xsmll', 'fin_gpr'), ('sjltz', 'fin_profit_yoy')]:
            vals = pd.to_numeric(f[col], errors='coerce').to_numpy()
            j = np.searchsorted(keys, g['date'].to_numpy(), side='right') - 1
            g[out] = np.where(j >= 0, vals[np.clip(j, 0, None)], np.nan)
        rows.append(g)
    E2 = pd.concat(rows, ignore_index=True)
    rows = [dict(factor=f,
                 wave_median=round(E2[E2.is_wave == 1][f].median(), 2),
                 ctrl_median=round(E2[E2.is_wave == 0][f].median(), 2),
                 wave_n=int(E2[E2.is_wave == 1][f].notna().sum()))
            for f in ['fin_roe', 'fin_gpr', 'fin_profit_yoy']]
    tC = pd.DataFrame(rows)
    tC.to_csv(OUT / 'belief_fund_occur.csv', index=False)
    print(tC.to_string(), flush=True)


if __name__ == '__main__':
    main()
