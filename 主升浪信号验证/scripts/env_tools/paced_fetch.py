"""节流续抓驱动 paced_fetch.py（2026-11-24 接手环境重建；对应 BATCH_LOG 运行注同名工具）。
探活 + 冷却 + 自适应降速，复用 02_fetch_kline 的 fetch_one（同口径；限速 empty 不落缓存，幂等可重抓）。
限速态先探活确认恢复再抓，失败票顺延下一轮，不空磨。
用法: python3 paced_fetch.py [qfq|raw|both]     # 默认 qfq
"""
import sys, time, random, importlib.util
from pathlib import Path

WS = Path('/home/work/.openclaw/workspace')
SCRIPTS = WS / 'daily-stock-ai' / '主升浪信号验证' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('fetch_kline', SCRIPTS / '02_fetch_kline.py')
fk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fk)          # 02 有 __main__ 守卫，导入安全
from utils import session, get_json, load_universe

PROBE_URL = 'https://1.push2his.eastmoney.com/api/qt/stock/kline/get'
PROBE_PARAMS = {'secid': '1.600519', 'fields1': 'f1,f2,f3,f4,f5,f6',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57',
                'klt': 101, 'fqt': 1, 'beg': '20220401', 'end': '20261124'}


def probe_ok():
    """探活：茅台日线能否取到（判定限速是否解除）。"""
    js = get_json(session(), PROBE_URL, params=PROBE_PARAMS, tries=1)
    node = (js or {}).get('data') or {}
    return bool(node.get('klines'))


def wait_resume(consec):
    """限速冷却（指数退避，上限 240s）；仅在中点与结束时各探活一次，防探测过频续期限流。"""
    cd = min(240, 20 * (2 ** (consec - 1)))
    print(f'  rate-limited, cooldown {cd}s (consec={consec})', flush=True)
    time.sleep(cd / 2)
    if probe_ok():
        print('  probe ok, resume early', flush=True)
        return
    time.sleep(cd / 2)


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else 'qfq'
    uni = load_universe()
    syms = uni.symbol.tolist()
    jobs = []
    if only in (None, 'both', 'qfq'):
        jobs += [(s, 'qfq') for s in syms]
    if only in (None, 'both', 'raw'):
        jobs += [(s, 'raw') for s in syms]
    print('jobs:', len(jobs), flush=True)

    delay, consec = 0.35, 0
    ok = skip = nodata = limited = 0
    queue, p = jobs, 0
    while queue:
        nxt = []
        for idx, (s, f) in enumerate(queue):
            if consec > 0:                      # 限速态：先探活，不空磨
                if not probe_ok():
                    consec += 1
                    wait_resume(consec)
                    if not probe_ok():
                        nxt += queue[idx:]
                        break                   # 未恢复，整批顺延下一轮
                consec = 0
            sym, st = fk.fetch_one(s, f)        # 复用 02 抓取函数（同口径）
            if st == 'skip':
                skip += 1
                continue
            if st == 'empty' or st.startswith('err'):
                if probe_ok():
                    nodata += 1                 # 探活正常 => 该票真无行情
                    continue
                limited += 1
                consec += 1
                nxt.append((s, f))              # 失败票顺延下一轮
                wait_resume(consec)
                continue
            ok += 1
            delay = max(0.25, delay * 0.98)
            time.sleep(delay + random.random() * 0.3)
            p += 1
            if p % 50 == 0:
                print(f'progress ok={ok}/{p} skip={skip} nodata={nodata} limited={limited} queue={len(nxt)} delay={delay:.2f}', flush=True)
        queue = nxt
        if queue:
            print(f'pass end, retry queue={len(queue)}', flush=True)
            time.sleep(10)
    print('DONE paced', only, f'ok={ok} skip={skip} nodata={nodata} limited={limited}', flush=True)


if __name__ == '__main__':
    main()
