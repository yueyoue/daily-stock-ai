"""复现原报告 15 个案例：在报告给定的启动低点日/高点日上重算特征，与报告数值对照。
同时用同一套识别器在案例窗口内检测主升浪，检验能否复现报告的日期与涨幅。
需要时按需抓取案例股的日线（前复权+不复权）与估值。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import session, get_json, CACHE
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module
det = import_module('05_detect')

URL_K = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
URL_V = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
import importlib as _il
_f02 = _il.import_module('02_fetch_kline')

# name, code, 方向, 报告启动低点日, 报告高点日, 高点价, 涨幅%, PE, PE分位, PB, PB分位,
# 净利同比, ROE, 距250高点%, 启动前20/120, 启动后20/120
CASES = [
 ('天华新能','300390','锂电材料','2026-02-10','2026-05-06',119.06,167.6,91.917950608217,89.2,3.2563755935941,75.6,-96.4,0.2954,-31.5,0.97,0.75),
 ('巨人网络','002558','游戏','2025-05-15','2025-09-18',47.66,247.9,18.661519346953,52.8,2.0371598885277,66.0,-1.3,2.6931,-22.1,0.67,1.23),
 ('恺英网络','002517','游戏','2025-05-23','2025-09-18',30.5,93.4,19.583411771092,72.4,4.6510621633235,55.2,21.6,7.5137,-15.3,0.69,0.81),
 ('鹏辉能源','300438','锂电材料','2026-02-26','2026-05-20',92.77,113.4,106.6706748655,86.8,4.0990448304731,69.2,89.3,2.2247,-28.2,0.77,1.51),
 ('亨通光电','600487','光通信','2026-03-23','2026-06-23',125.16,202.6,38.059601804524,92.0,3.2098398439758,92.0,2.6,8.0112,-23.8,2.43,2.51),
 ('世纪华通','002602','游戏','2025-01-13','2025-09-26',22.49,381.6,28.687350660223,6.0,1.3537414522537,67.6,np.nan,7.2573,-13.4,1.03,0.73),
 ('华盛锂电','688353','锂电材料','2025-08-28','2025-11-17',155.0,374.7,-31.455283471992,14.8,1.5626771401461,84.4,np.nan,-1.2552,-20.0,2.2,3.33),
 ('北京君正','300223','存储芯片','2026-03-31','2026-07-01',279.44,167.9,81.005905671548,0.0,3.9580864847684,75.6,-16.0,2.0917,-31.4,0.92,0.72),
 ('工业富联','601138','算力硬件','2025-05-27','2025-10-30',83.88,354.1,15.117787224544,7.2,2.3338688268934,7.2,25.0,3.3763,-37.3,0.46,0.73),
 ('香农芯创','300475','存储芯片','2025-07-22','2025-11-11',204.57,519.3,57.682778944036,77.2,4.7947479727471,54.8,18.7,0.5666,-26.9,0.49,0.54),
 ('香农芯创','300475','存储芯片','2026-04-03','2026-07-01',325.82,167.1,30.565050978227,0.0,11.669298938559,50.4,-1.4,11.3695,-40.4,0.76,1.2),
 ('兆易创新','603986','存储芯片','2026-03-31','2026-06-29',846.66,255.6,58.070728964787,0.0,6.6050911288833,48.0,30.2,6.2446,-28.1,1.01,1.3),
 ('大族激光','002008','激光设备','2026-03-23','2026-06-26',168.08,180.6,51.840006631003,92.8,3.56842735034,92.8,-39.5,5.2154,-21.1,2.11,1.62),
 ('多利科技','001311','汽车零部件','2025-10-30','2026-02-05',46.97,104.2,25.991179814947,93.6,1.5562082192422,70.4,-28.1,3.4535,-31.7,1.39,1.2),
 ('恩捷股份','002812','锂电材料','2026-01-29','2026-05-06',87.71,80.3,335.21968544299,92.4,1.8763686203217,80.4,-119.5,-0.3506,-26.7,0.96,1.08),
]
COLS = ['name','code','dir','start','peak','peak_price','gain','pe','pe_pct','pb','pb_pct',
        'profit_yoy','roe','dist250','amt_pre','amt_post']

SYM = {'300390':'sz300390','002558':'sz002558','002517':'sz002517','300438':'sz300438',
       '600487':'sh600487','002602':'sz002602','688353':'sh688353','300223':'sz300223',
       '601138':'sh601138','300475':'sz300475','603986':'sh603986','002008':'sz002008',
       '001311':'sz001311','002812':'sz002812'}


def fetch_kline_raw_qfq(sym):
    """按需抓取日线（东财分片）：qfq -> kline/{sym}.pkl，不复权 -> raw_kline/{sym}.pkl"""
    _f02.fetch_one(sym, 'qfq')
    _f02.fetch_one(sym, 'raw')


def fetch_val(code):
    out = CACHE / 'val' / f'{code}.pkl'
    if out.exists() and out.stat().st_size > 200:
        return
    s = session()
    rows, page = [], 1
    while True:
        js = get_json(s, URL_V, params={
            'reportName': 'RPT_VALUEANALYSIS_DET',
            'columns': 'SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ,CLOSE_PRICE,TOTAL_SHARES,BOARD_NAME',
            'filter': f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'2021-06-01\')',
            'pageSize': 500, 'pageNumber': page,
            'sortColumns': 'TRADE_DATE', 'sortTypes': 1}, referer='https://data.eastmoney.com/')
        res = (js or {}).get('result') or {}
        rows += res.get('data') or []
        if page >= (res.get('pages') or 1) or not res.get('data'):
            break
        page += 1
        time.sleep(0.1)
    if rows:
        df = pd.DataFrame(rows)
        df['TRADE_DATE'] = df['TRADE_DATE'].str[:10]
        df.drop_duplicates('TRADE_DATE').sort_values('TRADE_DATE').reset_index(drop=True).to_pickle(out)


def feats_at(df, date, val, fin_df, code):
    """在指定日期上按 05 的口径重算特征（含 3 种量能口径）。"""
    A = det.feat_arrays(df, val)
    idx = np.nonzero(A['dates'] == date)[0]
    if not len(idx):
        return None
    i = int(idx[0])
    fl = det.FinLookup(fin_df, code)
    fin = fl.asof(date)
    amt = A['amt']
    amt20 = float(np.nanmean(amt[i - 20:i]))
    amt120a = float(np.nanmean(amt[i - 140:i - 20]))   # 相邻：前120日
    amt120b = float(np.nanmean(amt[i - 120:i]))        # 重叠：近120日
    amt20b = float(np.nanmean(amt[i - 19:i + 1]))      # 含启动日
    vol = A.get('vol')
    out = dict(
        date=date,
        dist250=(A['close'][i] / A['maxh250'][i] - 1) * 100,
        dist250_ex=(A['close'][i] / A['high'][max(0, i - 250):i].max() - 1) * 100,
        amt_a=amt20 / amt120a, amt_b=amt20b / amt120b, amt_c=amt20 / amt120b,
        amt_d=amt20b / amt120a,
        pe_ttm=A['pe'][i], pb=A['pb'][i],
        pe_pct=det.pct_rank_window(A['pe'], i) * 100,
        pb_pct=det.pct_rank_window(A['pb'], i) * 100,
        profit_yoy=(fin.get('sjltz') if fin else np.nan),
        roe=(fin.get('roe') if fin else np.nan),
        fin_period=(fin.get('reportdate') if fin else None),
        fin_notice=(fin.get('notice_date') if fin else None),
    )
    if vol is not None:
        v20 = float(np.nanmean(vol[i - 20:i])); v120a = float(np.nanmean(vol[i - 140:i - 20]))
        v120b = float(np.nanmean(vol[i - 120:i]))
        out['vol_a'] = v20 / v120a
        out['vol_c'] = v20 / v120b
    return out


def detect_in_window(df, val, start, peak):
    A = det.feat_arrays(df, val)
    waves = det.detect_waves(A, '2023-06-01', '2026-08-25')
    return [(A['dates'][w[0]], A['dates'][w[1]], w) for w in waves]


def main():
    fin_path = CACHE / 'fin' / 'fin_all.csv'
    fin_df = pd.read_csv(fin_path, dtype={'code': str}) if fin_path.exists() else pd.DataFrame(columns=['code', 'notice_date'])
    rep = pd.DataFrame(CASES, columns=COLS)
    out_rows = []
    for code, g in rep.groupby('code', sort=False):
        sym = SYM[code]
        fetch_kline_raw_qfq(sym)
        fetch_val(code)
        val = det.load_val(code)
        dfq = pd.read_pickle(CACHE / 'kline' / f'{sym}.pkl')
        raw_p = CACHE / 'raw_kline' / f'{sym}.pkl'
        dfr = pd.read_pickle(raw_p) if raw_p.exists() else dfq
        for _, r in g.iterrows():
            fq = feats_at(dfq, r['start'], val, fin_df, code)
            fr = feats_at(dfr, r['start'], val, fin_df, code)
            row = dict(name=r['name'], code=code, rep_start=r['start'], rep_peak=r['peak'],
                       rep_peak_price=r['peak_price'], rep_gain=r['gain'],
                       rep_dist250=r['dist250'], rep_amt=r['amt_pre'],
                       rep_pe_pct=r['pe_pct'], rep_pb_pct=r['pb_pct'],
                       rep_profit_yoy=r['profit_yoy'], rep_roe=r['roe'])
            for tag, f in (('qfq', fq), ('raw', fr)):
                if f is None:
                    continue
                row[f'{tag}_dist250'] = round(f['dist250'], 2)
                row[f'{tag}_dist250_ex'] = round(f['dist250_ex'], 2)
                row[f'{tag}_amt_a'] = round(f['amt_a'], 3)
                row[f'{tag}_amt_b'] = round(f['amt_b'], 3)
                row[f'{tag}_amt_c'] = round(f['amt_c'], 3)
                row[f'{tag}_amt_d'] = round(f['amt_d'], 3)
                if 'vol_a' in f:
                    row[f'{tag}_vol_a'] = round(f['vol_a'], 3)
                    row[f'{tag}_vol_c'] = round(f['vol_c'], 3)
                row[f'{tag}_pe'] = None if f['pe_ttm'] is None or np.isnan(f['pe_ttm']) else round(f['pe_ttm'], 1)
                row[f'{tag}_pe_pct'] = None if f['pe_pct'] is None or np.isnan(f['pe_pct']) else round(f['pe_pct'], 1)
                row[f'{tag}_pb_pct'] = None if f['pb_pct'] is None or np.isnan(f['pb_pct']) else round(f['pb_pct'], 1)
                row[f'{tag}_profit_yoy'] = f['profit_yoy']
                row[f'{tag}_fin'] = f['fin_period']
            # 检测器在该案例上能否复现
            det_w = detect_in_window(dfq, val, r['start'], r['peak'])
            near = [w for w in det_w if abs((pd.Timestamp(w[0]) - pd.Timestamp(r['start'])).days) <= 25]
            row['det_waves_near'] = '; '.join(f'{a}->{b}' for a, b, _ in near) if near else '未检出'
            out_rows.append(row)
            print(row['name'], r['start'], 'raw_dist250', row.get('raw_dist250'),
                  'vs rep', r['dist250'], '| amt a/b/c/d', row.get('raw_amt_a'), row.get('raw_amt_b'),
                  row.get('raw_amt_c'), row.get('raw_amt_d'), '| vol_a', row.get('raw_vol_a'), 'vs rep', r['amt_pre'])
    out = pd.DataFrame(out_rows)
    out.to_csv(CACHE / 'cases_recheck.csv', index=False)
    print(out.to_string())


if __name__ == '__main__':
    main()
