"""主升浪识别 + 启动前特征计算（全市场）。

主升浪口径（对齐原复盘报告）：
  起点 = 局部最低收盘（±10 交易日内最低），终点 = 段内最高价日；
  区间涨幅（最高价/起点收盘-1）>= 50%；段内收盘最大回撤 < 20%；持续 20~400 个交易日。

输出：
  cache/waves.csv            —— 每段主升浪一行，含启动前特征
  cache/features/{code}.pkl  —— 日级特征（命中率检验用）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import CACHE, load_universe
import numpy as np
import pandas as pd

MIN_GAIN = 0.50
MAX_DD = 0.20
MIN_DUR, MAX_DUR = 20, 400
LOCAL_W = 10                 # 局部低点窗口 ±10
PE_PCT_WIN = 500             # PE 两年分位窗口


def load_val(code):
    p = CACHE / 'val' / f'{code}.pkl'
    return pd.read_pickle(p) if p.exists() else None


def feat_arrays(df, val):
    o, c, h, l, v = (df[x].to_numpy(float) for x in ['open', 'close', 'high', 'low', 'volume'])
    if 'amount' in df.columns:
        amt = df['amount'].to_numpy(float)      # 真实成交额（东财）
    else:
        amt = v * (o + h + l + c) / 4.0          # 成交额代理 = 成交量 × 均价
    n = len(df)
    maxh250 = np.full(n, np.nan)
    minl250 = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - 249)
        maxh250[i] = h[lo:i + 1].max()
        minl250[i] = l[lo:i + 1].min()
    out = dict(dates=df['date'].values, open=o, close=c, high=h, low=l, vol=v, amt=amt,
               maxh250=maxh250, minl250=minl250)
    n = len(df)
    if val is not None and len(val):
        v2 = val.drop_duplicates('TRADE_DATE').set_index('TRADE_DATE')
        re = v2.reindex(pd.Index(out['dates']))
        out['pe'] = pd.to_numeric(re['PE_TTM'], errors='coerce').to_numpy()
        out['pb'] = pd.to_numeric(re['PB_MRQ'], errors='coerce').to_numpy()
        ind = re['BOARD_NAME'].ffill().bfill()
        out['industry'] = ind.to_numpy()
    else:
        out['pe'] = np.full(n, np.nan)
        out['pb'] = np.full(n, np.nan)
        out['industry'] = np.array([None] * n)
    return out


def pct_rank_window(arr, i, win=PE_PCT_WIN):
    lo = max(0, i - win + 1)
    seg = arr[lo:i + 1]
    seg = seg[~np.isnan(seg)]
    if len(seg) < 60 or np.isnan(arr[i]):
        return np.nan
    return float((seg <= arr[i]).sum()) / len(seg)


def detect_waves(A, date_min, date_max):
    """返回 [(start_i, peak_i, end_i)]，end_i = 首个回撤>20% 的位置（或扫描末尾）。"""
    c, h = A['close'], A['high']
    n = len(c)
    dates = A['dates']
    cand_idx = []
    for i in range(LOCAL_W, n - 5):
        if dates[i] < date_min or dates[i] > date_max:
            continue
        w = c[i - LOCAL_W:i + LOCAL_W + 1]
        if c[i] == w.min():
            cand_idx.append(i)
    waves = []
    for i in cand_idx:
        m = min(n - 1 - i, MAX_DUR + 200)
        seg_c = c[i:i + m + 1]
        seg_h = h[i:i + m + 1]
        run = np.maximum.accumulate(seg_h)
        breach = np.nonzero(seg_c < run * (1 - MAX_DD))[0]
        end_rel = int(breach[0]) if len(breach) else m
        peak_rel = int(np.argmax(seg_h[:end_rel + 1]))
        dur = peak_rel
        gain = seg_h[peak_rel] / c[i] - 1
        if gain >= MIN_GAIN and MIN_DUR <= dur <= MAX_DUR:
            waves.append((i, i + peak_rel, i + end_rel))
    # 去重：重叠波段保留涨幅最大的
    waves.sort(key=lambda t: -(h[t[1]] / c[t[0]]))
    kept = []
    for t in waves:
        if all(t[0] > k[1] or t[1] < k[0] for k in kept):
            kept.append(t)
    kept.sort(key=lambda t: t[0])
    return kept


class FinLookup:
    """按公告日期 asof 查询财务（只用启动前已公告的报告期）。"""

    def __init__(self, fin_df, code):
        sub = fin_df[fin_df.code == code].dropna(subset=['notice_date'])
        sub = sub.sort_values('notice_date')
        self.rows = list(sub.to_dict('records'))
        self.keys = [r['notice_date'] for r in self.rows]

    def asof(self, date):
        j = np.searchsorted(np.array(self.keys, dtype=object), date, side='right') - 1
        # searchsorted 对 object 数组按字典序可用（ISO 日期）
        return self.rows[j] if j >= 0 else None


def wave_features(A, i, peak, end, fin):
    c, h, amt = A['close'], A['high'], A['amt']
    run = np.maximum.accumulate(h[i:peak + 1])
    dd_seg = float(np.max(1 - c[i:peak + 1] / run))
    amt20 = float(np.nanmean(amt[max(0, i - 20):i])) if i >= 20 else np.nan
    amt120 = float(np.nanmean(amt[max(0, i - 140):i - 20])) if i >= 140 else np.nan
    amt120b = float(np.nanmean(amt[max(0, i - 120):i])) if i >= 120 else np.nan
    return dict(
        start_date=A['dates'][i], peak_date=A['dates'][peak], end_scan_date=A['dates'][end],
        close_start=float(c[i]), price_peak=float(h[peak]),
        gain_high=float(h[peak] / c[i] - 1), gain_close=float(c[peak] / c[i] - 1),
        dur=int(peak - i), dd_seg=dd_seg,
        dist250=float(c[i] / A['maxh250'][i] - 1),
        rebound250=float(c[i] / A['minl250'][i] - 1),
        amt20_120=(amt20 / amt120 if amt120 and amt120 > 0 else np.nan),
        amt20_120_ovl=(amt20 / amt120b if amt120b and amt120b > 0 else np.nan),
        pe_ttm=float(A['pe'][i]) if not np.isnan(A['pe'][i]) else np.nan,
        pb=float(A['pb'][i]) if not np.isnan(A['pb'][i]) else np.nan,
        pe_pct=pct_rank_window(A['pe'], i), pb_pct=pct_rank_window(A['pb'], i),
        profit_yoy=(fin.get('sjltz') if fin else np.nan),
        rev_yoy=(fin.get('ystz') if fin else np.nan),
        roe=(fin.get('roe') if fin else np.nan),
        gpr=(fin.get('xsmll') if fin else np.nan),
        fin_reportdate=(fin.get('reportdate') if fin else None),
        fin_notice=(fin.get('notice_date') if fin else None),
        industry=A['industry'][i] if A['industry'][i] else None,
    )


def process_stock(sym, code, fin_df):
    kp = CACHE / 'kline' / f'{sym}.pkl'
    if not kp.exists():
        return [], None
    df = pd.read_pickle(kp)
    if len(df) < 260:
        return [], None
    val = load_val(code)
    A = feat_arrays(df, val)
    fl = FinLookup(fin_df, code)
    waves = detect_waves(A, '2023-06-01', '2026-08-25')
    rows = []
    for (i, peak, end) in waves:
        fin = fl.asof(A['dates'][i])
        rows.append(wave_features(A, i, peak, end, fin))
    day_df = None
    n = len(df)
    dates = A['dates']
    t_idx = [t for t in range(n) if '2023-06-01' <= dates[t] <= '2026-08-25' and t + 60 < n]
    if t_idx:
        fwd = np.full(n, np.nan)
        closes = A['close']
        for t in t_idx:
            fwd[t] = closes[t + 1:t + 61].max() / closes[t] - 1
        amt20 = pd.Series(A['amt']).rolling(20).mean().to_numpy()
        amt120p = pd.Series(A['amt']).shift(20).rolling(120).mean().to_numpy()
        day_df = pd.DataFrame({
            'date': [dates[t] for t in t_idx],
            'fwd60': fwd[t_idx],
            'dist250': A['close'][t_idx] / A['maxh250'][t_idx] - 1,
            'amt_ratio': (amt20 / amt120p)[t_idx],
            'pe_pct_2y': [pct_rank_window(A['pe'], t) for t in t_idx],
        })
    return rows, day_df


def main():
    uni = load_universe()
    fin_df = pd.read_csv(CACHE / 'fin' / 'fin_all.csv', dtype={'code': str})
    all_rows, n_day = [], 0
    for k, (sym, code) in enumerate(zip(uni.symbol, uni.code)):
        try:
            rows, day_df = process_stock(sym, code, fin_df)
        except Exception as e:
            print('ERR', sym, repr(e)[:120])
            continue
        for r in rows:
            r['symbol'] = sym
        all_rows += rows
        if day_df is not None:
            day_df.to_pickle(CACHE / 'features' / f'{code}.pkl')
            n_day += 1
        if (k + 1) % 300 == 0:
            print(f'{k+1}/{len(uni)} waves={len(all_rows)} dayfiles={n_day}', flush=True)
    w = pd.DataFrame(all_rows)
    if len(w):
        name_map = dict(zip(uni.code, uni.name))
        w['name'] = w['code'].map(name_map)
        w.to_csv(CACHE / 'waves.csv', index=False)
    print('DONE waves:', len(w), 'stocks_with_waves:', (w.code.nunique() if len(w) else 0))


if __name__ == '__main__':
    main()
