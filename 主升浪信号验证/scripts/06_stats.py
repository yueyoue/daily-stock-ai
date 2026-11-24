"""扩大样本后的验证统计：
  1) 主升浪启动前信号分布（位置/量能/PE/业绩）vs 原报告 15 案例口径
  2) 信号命中率表（全市场交易日）vs 原报告（15 只票交易日）
  3) 行业同步性检验（置换基准）
输出 tables/*.csv 与控制台摘要。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / 'tables'
OUT.mkdir(exist_ok=True)
CASE_CODES = ['300390', '002558', '002517', '300438', '600487', '002602', '688353',
              '300223', '601138', '300475', '603986', '002008', '001311', '002812']


def load_days(codes=None):
    feats = CACHE / 'features'
    dfs = []
    for p in sorted(feats.glob('*.pkl')):
        if codes and p.stem not in codes:
            continue
        d = pd.read_pickle(p)
        d['code'] = p.stem
        dfs.append(d)
    return pd.concat(dfs, ignore_index=True)


def hit_table(days, label):
    """报告口径的命中率表：未来60日收盘最高涨幅>=40% 的概率与中位数。"""
    def stat(sub):
        if len(sub) == 0:
            return dict(条件=label, 样本交易日=0, 命中率=np.nan, fwd60中位=np.nan, fwd60均值=np.nan)
        return dict(条件=label, 样本交易日=len(sub),
                    命中率=round((sub.fwd60 >= 0.40).mean() * 100, 1),
                    fwd60中位=round(sub.fwd60.median() * 100, 1),
                    fwd60均值=round(sub.fwd60.mean() * 100, 1))
    rows = []
    amt, dist, pe = days.amt_ratio, days.dist250, days.pe_pct_2y
    defs = [
        ('全部交易日（基准）', pd.Series(True, index=days.index)),
        ('缩量：20日额/前120日<0.8', amt < 0.8),
        ('回调：距250日高点<=-15%', dist <= -0.15),
        ('PE两年分位<30%', pe < 0.30),
        ('PE两年分位>80%', pe > 0.80),
        ('缩量+回调', (amt < 0.8) & (dist <= -0.15)),
        ('缩量+回调+PE分位<50%', (amt < 0.8) & (dist <= -0.15) & (pe < 0.50)),
        ('放量(>=1.5)+回调', (amt >= 1.5) & (dist <= -0.15)),
        ('信号2规则：-40%<=距高点<=-15%且量比<0.9',
         (dist <= -0.15) & (dist >= -0.40) & (amt < 0.9)),
        ('信号2+PE分位<50%', (dist <= -0.15) & (dist >= -0.40) & (amt < 0.9) & (pe < 0.50)),
        ('信号2+PE分位>80%且(亏损/负增长proxy: PE>80)',
         (dist <= -0.15) & (dist >= -0.40) & (amt < 0.9) & (pe > 0.80)),
    ]
    for name, mask in defs:
        r = stat(days[mask.fillna(False)])
        r['条件'] = name
        rows.append(r)
    d = days.copy()
    d['year'] = d.date.str[:4]
    for y in ['2023', '2024', '2025', '2026']:
        sub = d[d.year == y]
        r = stat(sub); r['条件'] = f'{y}年 全部（基准）'; rows.append(r)
        m = (sub.amt_ratio < 0.8) & (sub.dist250 <= -0.15)
        r = stat(sub[m.fillna(False)]); r['条件'] = f'{y}年 缩量+回调'; rows.append(r)
    t = pd.DataFrame(rows)
    t.insert(0, '样本', label)
    return t


def main():
    uni = load_universe()
    waves = pd.read_csv(CACHE / 'waves.csv', dtype={'code': str})
    print('waves:', len(waves), 'stocks:', waves.code.nunique())

    # ---------- 1. 启动前信号分布 ----------
    w = waves.copy()
    w['year'] = w.start_date.str[:4]
    q = lambda s: [round(float(np.nanpercentile(s, p)), 2) for p in (25, 50, 75)]
    overview = pd.DataFrame([
        {'指标': '波段数', '值': len(w)},
        {'指标': '涉及股票数', '值': w.code.nunique()},
        {'指标': '涨幅(高点/起点-1)中位%', '值': round(w.gain_high.median() * 100, 1)},
        {'指标': '持续交易日中位', '值': int(w.dur.median())},
        {'指标': '距250日高点 P25/中位/P75 %', '值': q(w.dist250 * 100)},
        {'指标': '距250日高点 ∈[-40%,-13%] 占比%', '值': round(((w.dist250 >= -0.404) & (w.dist250 <= -0.134)).mean() * 100, 1)},
        {'指标': '距250日高点 >-13% 占比%', '值': round((w.dist250 > -0.134).mean() * 100, 1)},
        {'指标': '距250日高点 <-40% 占比%', '值': round((w.dist250 < -0.404).mean() * 100, 1)},
        {'指标': '启动前20日/120日量比 中位', '值': round(w.amt20_120.median(), 2)},
        {'指标': '量比<1 占比%', '值': round((w.amt20_120 < 1).mean() * 100, 1)},
        {'指标': '量比<0.8 占比%', '值': round((w.amt20_120 < 0.8).mean() * 100, 1)},
        {'指标': '前史反弹(距250日低点) 中位%', '值': round(w.rebound250.median() * 100, 1)},
        {'指标': 'PE两年分位<30% 占比%', '值': round((w.pe_pct < 0.30).mean() * 100, 1)},
        {'指标': 'PE两年分位30-80% 占比%', '值': round(((w.pe_pct >= 0.30) & (w.pe_pct <= 0.80)).mean() * 100, 1)},
        {'指标': 'PE两年分位>80% 占比%', '值': round((w.pe_pct > 0.80).mean() * 100, 1)},
        {'指标': 'PE(TTM)为负 占比%', '值': round((w.pe_ttm < 0).mean() * 100, 1)},
        {'指标': '净利同比<0 占比%(有数据)', '值': round((w.profit_yoy < 0).mean() * 100, 1)},
        {'指标': '净利同比>=0 占比%(有数据)', '值': round((w.profit_yoy >= 0).mean() * 100, 1)},
        {'指标': '净利同比缺失 占比%', '值': round(w.profit_yoy.isna().mean() * 100, 1)},
    ])
    # 业绩与涨幅关系
    v = w.dropna(subset=['profit_yoy'])
    if len(v) > 30:
        rho = v.profit_yoy.rank().corr(v.gain_high.rank())   # Spearman（免 scipy）
        overview.loc[len(overview)] = {'指标': 'Spearman(净利同比, 波段涨幅)', '值': round(rho, 3), '备注': f'n={len(v)}'}
    for tag, m in [('净利同比<0', v.profit_yoy < 0), ('净利同比0~30%', (v.profit_yoy >= 0) & (v.profit_yoy < 30)),
                   ('净利同比>=30%', v.profit_yoy >= 30)]:
        overview.loc[len(overview)] = {'指标': f'涨幅中位% | {tag}',
                                       '值': round(v[m].gain_high.median() * 100, 1), '备注': f'n={m.sum()}'}
    overview.to_csv(OUT / 'waves_overview.csv', index=False)
    print(overview.to_string())
    w.to_csv(OUT / 'waves_all.csv', index=False)

    # ---------- 2. 命中率表 ----------
    days = load_days()
    print('day rows:', len(days), 'stocks:', days.code.nunique())
    t_all = hit_table(days, '全市场')
    t_case = hit_table(load_days(set(CASE_CODES)), '原报告15只案例股')
    both = pd.concat([t_case, t_all], ignore_index=True)
    both.to_csv(OUT / 'hit_table.csv', index=False)
    print(both.to_string())

    # ---------- 3. 行业同步性 ----------
    ww = w.dropna(subset=['industry']).copy()
    ww['t'] = pd.to_datetime(ww.start_date).astype('int64') // 10**9
    days_win = 21 * 86400          # ±15 交易日 ≈ ±21 日历日
    cnts = []
    for ind, g in ww.groupby('industry'):
        ts = np.sort(g.t.values)
        for i, t in enumerate(ts):
            cnts.append(int(((ts > t - days_win) & (ts < t + days_win) & (ts != t)).sum()))
    ww['near_same_ind'] = cnts
    share3 = (ww.near_same_ind >= 2).mean()
    share1 = (ww.near_same_ind >= 1).mean()
    # 置换基准：日期在行业内随机平移
    rng = np.random.default_rng(7)
    perm = []
    for _ in range(300):
        c = []
        for ind, g in ww.groupby('industry'):
            ts = np.sort(g.t.values + rng.integers(-300 * 86400, 300 * 86400, len(g)))
            for i, t in enumerate(ts):
                c.append(int(((ts > t - days_win) & (ts < t + days_win) & (ts != t)).sum()))
        perm.append((np.mean(np.array(c) >= 2), np.mean(np.array(c) >= 1)))
    perm = np.array(perm)
    sync = pd.DataFrame([
        {'指标': '波段数(有行业)', '值': len(ww)},
        {'指标': '±15交易日内同行业>=1段 占比%', '值': round(share1 * 100, 1),
         '备注': f'置换基准 {perm[:,1].mean()*100:.1f}%±{perm[:,1].std()*100:.1f}'},
        {'指标': '±15交易日内同行业>=2段 占比%', '值': round(share3 * 100, 1),
         '备注': f'置换基准 {perm[:,0].mean()*100:.1f}%±{perm[:,0].std()*100:.1f}'},
    ])
    sync.to_csv(OUT / 'sector_sync.csv', index=False)
    print(sync.to_string())

    # 行业聚集 top：波段数最多的行业与启动月份分布
    top = (ww.groupby(['industry', ww.start_date.str[:7]])
             .size().reset_index(name='n').sort_values('n', ascending=False).head(25))
    top.to_csv(OUT / 'sector_month_top.csv', index=False)
    print(top.to_string())


if __name__ == '__main__':
    main()
