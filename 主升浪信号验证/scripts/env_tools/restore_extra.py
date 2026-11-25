"""接手环境补恢复工具（仅恢复缓存，不改任何口径）：
09_restore_cache.py 只恢复日线；本工具从 results/ 归档补齐其余缓存，免重抓：
  - batch_1_val.pkl.gz  -> cache/val/{code}.pkl        （日期 int32->ISO，同 09 的修复方式）
  - batch_1_days.pkl.gz -> cache/features/{code}.pkl   （日级特征，归档注明"命中率可完全复算"）
  - batch_1_waves.csv   -> cache/waves.csv             （Batch1 波段，05 增量合并需要）
  - fin_all.pkl.gz      -> cache/fin/fin_all.csv       （05 的 FinLookup 输入）
已有缓存文件一律跳过（幂等）。缺失的 val/fin 如需补抓用 03/04（TEST_PLAN 文档路径）。
"""
from pathlib import Path
import pandas as pd

REPO = Path('/home/work/.openclaw/workspace/daily-stock-ai')
RES = REPO / '主升浪信号验证' / 'results'
CACHE = REPO / '.openclaw' / 'tmp' / 'stock-analysis' / 'cache'
for d in ['val', 'features', 'fin']:
    (CACHE / d).mkdir(parents=True, exist_ok=True)


def iso(s):
    # 归档日期 int32(20220401) -> 流水线内部 ISO(2022-04-01)，与 09_restore_cache 修复一致
    return pd.to_datetime(s.astype(str)).dt.strftime('%Y-%m-%d')


def main():
    n = 0
    vp = RES / 'batch_1_val.pkl.gz'
    if vp.exists():
        df = pd.read_pickle(vp)
        for code, g in df.groupby('code'):
            out = CACHE / 'val' / f'{code}.pkl'
            if out.exists():
                continue
            g = g.drop(columns=['code']).copy()
            g['TRADE_DATE'] = iso(g['TRADE_DATE'])
            g = g.drop_duplicates('TRADE_DATE').sort_values('TRADE_DATE').reset_index(drop=True)
            g.to_pickle(out)
            n += 1
        print('val restored', n)

    n = 0
    dp = RES / 'batch_1_days.pkl.gz'
    if dp.exists():
        df = pd.read_pickle(dp)
        for code, g in df.groupby('code'):
            out = CACHE / 'features' / f'{code}.pkl'
            if out.exists():
                continue
            g = g.drop(columns=['code']).copy()
            g['date'] = iso(g['date'])
            g.to_pickle(out)
            n += 1
        print('features restored', n)

    src, dst = RES / 'batch_1_waves.csv', CACHE / 'waves.csv'
    if src.exists() and not dst.exists():
        dst.write_bytes(src.read_bytes())
        print('waves seeded rows', sum(1 for _ in open(dst)) - 1)

    fp, fout = RES / 'fin_all.pkl.gz', CACHE / 'fin' / 'fin_all.csv'
    if fp.exists() and not fout.exists():
        fin = pd.read_pickle(fp)
        fin.to_csv(fout, index=False)
        print('fin restored rows', len(fin), 'codes', fin.code.nunique())


if __name__ == '__main__':
    main()
