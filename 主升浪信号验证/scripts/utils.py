"""公共工具：缓存目录、HTTP 会话、重试。"""
import os, time, random, json
from pathlib import Path
import requests
import pandas as pd

WS = Path(__file__).resolve().parents[2]          # 工作区根目录
CACHE = WS / '.openclaw' / 'tmp' / 'stock-analysis' / 'cache'
for d in ['kline', 'val', 'fin', 'features', 'raw_kline']:
    (CACHE / d).mkdir(parents=True, exist_ok=True)

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

def session():
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Referer': 'https://finance.sina.com.cn/'})
    return s

def get_json(sess, url, params=None, tries=4, timeout=15, referer=None):
    """带重试的 GET，返回 json（失败返回 None）。"""
    last = None
    for k in range(tries):
        try:
            h = {'Referer': referer} if referer else None
            r = sess.get(url, params=params, timeout=timeout, headers=h)
            if r.status_code == 200:
                return r.json()
            last = f'HTTP {r.status_code}'
        except Exception as e:
            last = repr(e)[:120]
        time.sleep(0.6 * (k + 1) + random.random() * 0.5)
    print(f'  !! fail {url[:80]} {params} -> {last}')
    return None

def load_universe():
    return pd.read_csv(CACHE / 'universe.csv', dtype={'code': str, 'symbol': str})

if __name__ == '__main__':
    print('cache =', CACHE)
