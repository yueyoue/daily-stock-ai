"""增量抓取快照 inc_snapshot.py（2026-11-24 接手环境重建；对应 BATCH_LOG 运行注的 batch_{n}_inc{k}_kline*.pkl.gz 机制）。
把尚未完整归档批次的已抓日线（前复权+不复权）导出为 write-once 增量快照，防环境销毁丢抓取进度；
09_restore_cache 的 batch_*_kline* 模式可识别恢复。批次完整归档产出后 inc 文件冗余，可删可留。
用法: python3 inc_snapshot.py    # 幂等：无新增则不产出文件
"""
from pathlib import Path
import pandas as pd

REPO = Path('/home/work/.openclaw/workspace/daily-stock-ai')
RES = REPO / '主升浪信号验证' / 'results'
CACHE = REPO / '.openclaw' / 'tmp' / 'stock-analysis' / 'cache'
BATCHES = [(0, 1500, 1), (1500, 3000, 2), (3000, 4500, 3), (4500, 5223, 4)]
FLOAT_COLS = ['open', 'close', 'high', 'low', 'volume', 'amount']


def covered(n, tag):
    """已归档/已快照覆盖的 code 集合（完整归档 + 增量快照）。"""
    done = set()
    for p in list(RES.glob(f'batch_{n}_{tag}.pkl.gz')) + list(RES.glob(f'batch_{n}_inc*_{tag}.pkl.gz')):
        try:
            done |= set(pd.read_pickle(p)['code'].astype(str))
        except Exception:
            pass
    return done


def next_k(n, tag):
    ks = []
    for p in RES.glob(f'batch_{n}_inc*_{tag}.pkl.gz'):
        mid = p.name.split('_inc')[1].split('_')[0]
        if mid.isdigit():
            ks.append(int(mid))
    return max(ks, default=0) + 1


def main():
    uni = pd.read_csv(CACHE / 'universe.csv', dtype={'code': str, 'symbol': str}).sort_values('symbol').reset_index(drop=True)
    sym2code = dict(zip(uni.symbol, uni.code))
    made = 0
    for a, b, n in BATCHES:
        sl = uni.iloc[a:b]
        for sub, tag in (('kline', 'kline'), ('raw_kline', 'kline_raw')):
            done = covered(n, tag)
            parts = []
            for s in sl.symbol:
                c = sym2code[s]
                if c in done:
                    continue
                p = CACHE / sub / f'{s}.pkl'
                if not p.exists():
                    continue
                k = pd.read_pickle(p)
                if 'amount' not in k.columns or len(k) == 0:
                    continue
                k = k.copy()
                k['code'] = c
                parts.append(k)
            if not parts:
                continue
            df = pd.concat(parts, ignore_index=True)
            df['date'] = df['date'].str.replace('-', '').astype('int32')
            for c in FLOAT_COLS:
                df[c] = df[c].astype('float32')
            k_idx = next_k(n, tag)
            out = RES / f'batch_{n}_inc{k_idx}_{tag}.pkl.gz'
            df.to_pickle(out, compression='gzip')
            made += 1
            print(out.name, 'codes', df.code.nunique(), 'rows', len(df))
    print('DONE inc snapshot, files made:', made)


if __name__ == '__main__':
    main()
