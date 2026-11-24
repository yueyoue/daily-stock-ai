"""扩展特征统计（10 的产物）：
  1) 区分度：主升浪启动日 vs 对照日 的特征分布差异（AUC = P(启动日特征值 > 对照日)）
  2) 命中率提升：对照日按特征五分位，看未来60日涨≥40%的概率变化（选股模型依据）
输出 tables/extras_separation.csv、tables/extras_lift.csv，并更新 EXTRA_FEATURES.md
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE
import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / 'tables'
OUT.mkdir(exist_ok=True)

FEATURES = ['ma60_dist', 'ma120_dist', 'ma200_dist', 'ma60_slope20', 'vol20', 'vol60',
            'atr20_pct', 'rsi14', 'ret60', 'ret120', 'gap', 'amt20', 'days_since_high250',
            'days_since_low250', 'pos_in_range250', 'down_streak', 'days_below_ma60',
            'turnover20', 'log_mktcap', 'idx_ret60', 'idx_above_ma200', 'rs_vs_idx60']


def load_extras():
    p = CACHE / 'extras_days.parquet'
    if p.exists():
        return pd.read_parquet(p)
    return pd.read_pickle(CACHE / 'extras_days.pkl.gz')


def main():
    d = load_extras()
    w = d[d.is_wave == 1]
    c = d[d.is_wave == 0].dropna(subset=['fwd60'])
    print('wave days:', len(w), 'control days:', len(c))

    # ---- 1) 区分度 ----
    rows = []
    for f in FEATURES:
        a = w[f].dropna().to_numpy()
        b = c[f].dropna().to_numpy()
        if len(a) < 50 or len(b) < 100:
            continue
        # AUC：随机取一个启动日与一个对照日，启动日特征更高的概率
        rng = np.random.default_rng(0)
        bs = rng.choice(b, size=min(20000, len(b)), replace=False)
        auc = float((a[:, None] > bs[None, :]).mean() + 0.5 * (a[:, None] == bs[None, :]).mean())
        rows.append(dict(
            feature=f,
            wave_median=round(float(np.median(a)), 4),
            ctrl_median=round(float(np.median(b)), 4),
            wave_p25=round(float(np.percentile(a, 25)), 4),
            wave_p75=round(float(np.percentile(a, 75)), 4),
            ctrl_p25=round(float(np.percentile(b, 25)), 4),
            ctrl_p75=round(float(np.percentile(b, 75)), 4),
            auc=round(max(auc, 1 - auc), 3),
            direction='启动日更高' if auc > 0.5 else '启动日更低'))
    sep = pd.DataFrame(rows).sort_values('auc', ascending=False)
    sep.to_csv(OUT / 'extras_separation.csv', index=False)
    print(sep.to_string())

    # ---- 2) 命中率提升（对照日五分位）----
    rows = []
    base = (c.fwd60 >= 0.40).mean() * 100
    rows.append(dict(feature='（基准）', q='全部', n=len(c), hit_pct=round(base, 1),
                     fwd60_med=round(c.fwd60.median() * 100, 1)))
    for f in FEATURES:
        s = c.dropna(subset=[f])
        if len(s) < 500:
            continue
        try:
            qs = pd.qcut(s[f], 5, duplicates='drop')
        except Exception:
            continue
        for qv, g in s.groupby(qs, observed=True):
            rows.append(dict(feature=f, q=str(qv), n=len(g),
                             hit_pct=round((g.fwd60 >= 0.40).mean() * 100, 1),
                             fwd60_med=round(g.fwd60.median() * 100, 1)))
    lift = pd.DataFrame(rows)
    lift.to_csv(OUT / 'extras_lift.csv', index=False)

    # ---- 摘要写入 EXTRA_FEATURES.md ----
    top = sep.head(10)
    lines = ['# 扩展特征挖掘结果（EXTRA_FEATURES.md）', '',
             '> 方法：主升浪启动日 vs 全市场对照日（每15交易日抽1）分布对比 + 对照日五分位命中率。',
             '> 命中 = 未来60交易日收盘最高涨幅 ≥ 40%。样本含 Batch1 起累计（每次运行全量重算）。', '',
             '## 区分度排名（AUC，0.5=无区分度）', '',
             top.to_string(index=False), '',
             '## 命中率基准', '',
             f'- 全市场对照日基准命中率：**{base:.1f}%**', '',
             '## 对未来选股模型的启示（自动累积更新）', '',
             '- AUC ≥ 0.6 的特征值得进入模型候选；0.55~0.6 可作辅助条件',
             '- 注意区分「区分度」与「预测力」：某特征能识别启动日（如 ret60 动量）不等于在任意时点买入有超额收益',
             '- 五分位命中率的单调性比绝对值更重要（见 extras_lift.csv）', '']
    (Path(__file__).resolve().parent.parent / 'EXTRA_FEATURES.md').write_text('\n'.join(lines), encoding='utf-8')
    print('saved extras_separation.csv / extras_lift.csv / EXTRA_FEATURES.md')


if __name__ == '__main__':
    main()
