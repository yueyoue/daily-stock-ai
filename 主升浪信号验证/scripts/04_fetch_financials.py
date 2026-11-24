"""抓取定期业绩报表（东财 datacenter RPT_LICO_FN_CPD）按报告期，含公告日期。
输出 cache/fin/fin_all.csv：code, reportdate, notice_date, sjltz(净利同比%), ystz(营收同比%),
parent_np(归母净利), roe(加权), bps, xsmll(毛利率%), revenue
"""
import sys, time, random
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from utils import session, get_json, CACHE
import pandas as pd

URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
COLS = ('SECURITY_CODE,SECURITY_NAME_ABBR,REPORTDATE,NOTICE_DATE,SJLTZ,YSTZ,'
        'PARENT_NETPROFIT,WEIGHTAVG_ROE,BPS,XSMLL,TOTAL_OPERATE_INCOME')
REF = 'https://data.eastmoney.com/'
PERIODS = ['2022-12-31', '2023-03-31', '2023-06-30', '2023-09-30', '2023-12-31',
                      '2024-03-31', '2024-06-30', '2024-09-30', '2024-12-31',
                      '2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31',
                      '2026-03-31', '2026-06-30', '2026-09-30']

def main():
    s = session()
    all_rows = []
    for p in PERIODS:
        page = 1
        while True:
            js = get_json(s, URL, params={
                'reportName': 'RPT_LICO_FN_CPD', 'columns': COLS,
                'filter': f"(REPORTDATE='{p}')",
                'pageSize': 500, 'pageNumber': page,
                'sortColumns': 'SECURITY_CODE', 'sortTypes': 1}, referer=REF)
            res = (js or {}).get('result') or {}
            data = res.get('data') or []
            all_rows += data
            pages = res.get('pages') or 1
            if page >= pages or not data:
                break
            page += 1
            time.sleep(0.05 + random.random() * 0.1)
        print(p, 'cum rows', len(all_rows), flush=True)
    df = pd.DataFrame(all_rows)
    df = df.rename(columns={
        'SECURITY_CODE': 'code', 'SECURITY_NAME_ABBR': 'name',
        'REPORTDATE': 'reportdate', 'NOTICE_DATE': 'notice_date',
        'SJLTZ': 'sjltz', 'YSTZ': 'ystz', 'PARENT_NETPROFIT': 'parent_np',
        'WEIGHTAVG_ROE': 'roe', 'BPS': 'bps', 'XSMLL': 'xsmll',
        'TOTAL_OPERATE_INCOME': 'revenue'})
    for c in ['reportdate', 'notice_date']:
        df[c] = df[c].str[:10]
    df = df.drop_duplicates(['code', 'reportdate'], keep='last')
    df.to_csv(CACHE / 'fin' / 'fin_all.csv', index=False)
    print('DONE fin rows:', len(df), 'codes:', df.code.nunique())

if __name__ == '__main__':
    main()
