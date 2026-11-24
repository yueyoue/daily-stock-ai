"""扩展特征挖掘（为选股模型储备）：
对「主升浪启动日」与「全市场对照日（每 15 个交易日抽 1 日）」计算扩展技术/结构特征，
输出 extras_days.parquet，供 11_extra_stats.py 做区分度与命中率提升分析。
特征全部只用当日及以前数据。
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import session, get_json, CACHE, load_universe
import numpy as np
import pandas as pd

IDX_FILE = CACHE / 'index_sh.pkl'
OUT = CACHE / 'extras_days.parquet'


def fetch_index():
    if IDX_FILE.exists():
        return
    for _ in range(5):
        js = get_json(session(), 'https://1.push2his.eastmoney.com/api/qt/stock/kline/get', params={
            'secid': '1.000001', 'fields1': 'f1,f2', 'fields2': 'f51,f52,f53,f54,f55,f56,f57',
            'klt': 101, 'fqt': 1, 'beg': '20210101', 'end': '20261124'})
        rows = [k.split(',') for k in ((js or {}).get('data') or {}).get('klines', [])]
        if rows:
            break
        time.sleep(2)
    if not rows:
        raise RuntimeError('index fetch failed')
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ['date', 'open', 'close', 'high', 'low', 'volume']
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df.to_pickle(IDX_FILE)
    print('index bars', len(df))


def stock_features(df, val, idx):
    """一次性算好逐日特征数组，返回 DataFrame（与 df 行对齐）。"""
    c = df['close'].to_numpy(float)
    o = df['open'].to_numpy(float)
    h = df['high'].to_numpy(float)
    l = df['low'].to_numpy(float)
    v = df['volume'].to_numpy(float)
    amt = df['amount'].to_numpy(float) if 'amount' in df.columns else v * (o + h + l + c) / 4
    s = pd.Series(c)
    F = pd.DataFrame({'date': df['date'].values})
    for k in (60, 120, 200):
        F[f'ma{k}_dist'] = c / s.rolling(k).mean().to_numpy() - 1
    ma60 = s.rolling(60).mean().to_numpy()
    ma60_20 = pd.Series(ma60).shift(20).to_numpy()
    F['ma60_slope20'] = ma60 / ma60_20 - 1
    ret = pd.Series(c).pct_change()
    F['vol20'] = ret.rolling(20).std().to_numpy()
    F['vol60'] = ret.rolling(60).std().to_numpy()
    tr = pd.concat([pd.Series(h - l), (pd.Series(h) - pd.Series(c).shift()).abs(),
                    (pd.Series(l) - pd.Series(c).shift()).abs()], axis=1).max(axis=1)
    F['atr20_pct'] = (tr.rolling(20).mean() / c)
    # RSI14
    up = ret.clip(lower=0).rolling(14).mean()
    dn = (-ret.clip(upper=0)).rolling(14).mean()
    F['rsi14'] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    F['ret60'] = s / s.shift(60) - 1
    F['ret120'] = s / s.shift(120) - 1
    F['gap'] = o / pd.Series(c).shift() - 1
    F['amt20'] = pd.Series(amt).rolling(20).mean().to_numpy()
    # 距 250 日高/低点的天数与位置
    n = len(c)
    dsh = np.full(n, np.nan); dsl = np.full(n, np.nan); posr = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - 249)
        win_h = h[lo:i + 1]; win_l = l[lo:i + 1]
        dsh[i] = i - (lo + int(np.argmax(win_h)))
        dsl[i] = i - (lo + int(np.argmin(win_l)))
        hh, ll = win_h.max(), win_l.min()
        posr[i] = (c[i] - ll) / (hh - ll) if hh > ll else np.nan
    F['days_since_high250'] = dsh
    F['days_since_low250'] = dsl
    F['pos_in_range250'] = posr
    # 连阴天数（截至当日）
    down = (np.diff(c, prepend=c[0]) < 0).astype(int)
    streak = np.zeros(n, int); run = 0
    for i in range(n):
        run = run + 1 if down[i] else 0
        streak[i] = run
    F['down_streak'] = streak
    below = (c < ma60).astype(int)
    run = np.zeros(n, int)
    for i in range(n):
        run[i] = (run[i - 1] + 1) if below[i] and i > 0 else (1 if below[i] else 0)
    F['days_below_ma60'] = run
    # 换手率（手=100股）
    if val is not None and len(val):
        shares = pd.to_numeric(val['TOTAL_SHARES'], errors='coerce').ffill().bfill()
        sh_map = dict(zip(val['TRADE_DATE'], shares))
        sh = np.array([sh_map.get(d, np.nan) for d in df['date']], float)
        if np.isnan(sh).all():
            sh = np.full(n, shares.dropna().iloc[-1] if len(shares.dropna()) else np.nan)
        else:
            sh = pd.Series(sh).ffill().bfill().to_numpy()
        F['turnover20'] = (pd.Series(v * 100 / sh).rolling(20).mean().to_numpy())
        F['log_mktcap'] = np.log(c * sh)
    else:
        F['turnover20'] = np.nan
        F['log_mktcap'] = np.nan
    # 指数环境
    ic = idx.set_index('date')['close']
    ic = ic.reindex(df['date']).ffill()
    icma200 = pd.Series(ic.to_numpy(float)).rolling(200).mean().to_numpy()
    F['idx_ret60'] = pd.Series(ic.to_numpy(float)) / pd.Series(ic.to_numpy(float)).shift(60) - 1
    F['idx_above_ma200'] = (ic.to_numpy(float) > icma200).astype(int)
    F['rs_vs_idx60'] = F['ret60'] - F['idx_ret60']
    return F


def main():
    fetch_index()
    idx = pd.read_pickle(IDX_FILE)
    uni = load_universe().sort_values('symbol').reset_index(drop=True)
    waves = pd.read_csv(CACHE / 'waves.csv', dtype={'code': str})
    wkeys = set(zip(waves.code, waves.start_date))
    parts = []
    for k, (sym, code) in enumerate(zip(uni.symbol, uni.code)):
        kp = CACHE / 'kline' / f'{sym}.pkl'
        if not kp.exists():
            continue
        df = pd.read_pickle(kp)
        if len(df) < 260:
            continue
        val_p = CACHE / 'val' / f'{code}.pkl'
        val = pd.read_pickle(val_p) if val_p.exists() else None
        F = stock_features(df, val, idx)
        closes = df['close'].to_numpy(float)
        fwd = np.full(len(df), np.nan)
        for t in range(len(df) - 61):
            fwd[t] = closes[t + 1:t + 61].max() / closes[t] - 1
        F['fwd60'] = fwd
        F['code'] = code
        n = len(df)
        wave_idx = {i for i in range(n) if (code, df['date'].iloc[i]) in wkeys}
        ctrl_idx = set(range(0, n, 15))
        sel = sorted(wave_idx | ctrl_idx)
        sel = [i for i in sel if '2023-06-01' <= df['date'].iloc[i] <= '2026-08-25']
        if not sel:
            continue
        part = F.iloc[sel].copy()
        part['is_wave'] = [int(i in wave_idx) for i in sel]
        parts.append(part)
        if (k + 1) % 500 == 0:
            print(f'{k+1}/{len(uni)} rows={sum(len(p) for p in parts)}', flush=True)
    out = pd.concat(parts, ignore_index=True)
    try:
        out.to_parquet(OUT, index=False, compression='zstd')
    except ImportError:
        out.to_pickle(CACHE / 'extras_days.pkl.gz', compression='gzip')
    print('DONE extras rows:', len(out), 'wave days:', int(out.is_wave.sum()))


if __name__ == '__main__':
    main()
