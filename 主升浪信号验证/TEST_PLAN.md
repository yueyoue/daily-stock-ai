# 分步测试计划（TEST_PLAN.md）

> 目的：环境可能被销毁，以 **GitHub 仓库 = 唯一持久化存储**。每完成一批立即把
> 测试结果 + 抓取到的日线数据 + 进度文档推送到 GitHub，任何时刻中断都不丢进度、不重复抓取。

## 批次划分（全A 5223 只，按 symbol 排序切片）

| 批次 | 切片 | 数量 | 状态 |
|---|---|---|---|
| Batch 1 | 0:1500 | 1500 | 见 BATCH_LOG.md |
| Batch 2 | 1500:3000 | 1500 | 待跑 |
| Batch 3 | 3000:4500 | 1500 | 待跑 |
| Batch 4 | 4500:5223 | 723 | 待跑 |

## 每批固定流程（全部幂等，可重复执行）

1. `python3 02_fetch_kline.py both` — 补齐该批 K 线（逐票缓存跳过，只抓缺的）
2. `python3 05_detect.py --slice a:b` — 该批主升浪识别 + 特征（合并进 waves.csv）
3. `python3 06_stats.py` — 全量累计统计（分布/命中率/行业同步）→ tables/
4. `python3 08_export_batch.py a b n` — 导出该批产物到 results/
   - `batch_n_waves.csv`（波段特征）
   - `batch_n_days.parquet`（日级特征，命中率可完全复算）
   - `batch_n_kline.parquet`（前复权日线，**避免重复抓取**，环境销毁后可直接恢复）
5. 更新 `BATCH_LOG.md`（本批结果摘要 + 与原报告结论的对照）与 `PROGRESS.md`
6. `bash sync_github.sh "batch n: ..."` — 推送 GitHub

## 环境销毁后的恢复流程（新会话照做即可）

1. 读 `README.md` → `PROGRESS.md` → `BATCH_LOG.md`（本文件记录全部口径与计划）
2. `git clone https://github.com/yueyoue/daily-stock-ai.git`（或用工作区已有克隆）
3. 把已推送的 `results/batch_*_kline.parquet` 解回缓存：
   `python3 scripts/09_restore_cache.py`（把 parquet 还原为逐票 pkl，已有的跳过）
4. 找到 PROGRESS.md 中最后一个完成批次，从下一批继续跑上面的固定流程
5. **已完成批次无需任何重抓**；只有未完成批次需要补抓缺的票

## 最终判断方式

全部批次跑完后，以 GitHub 上累计的 `tables/`（waves_overview / hit_table / sector_sync）
和 `BATCH_LOG.md` 各批一致性为依据，逐条判定原报告 8 章结论（见 PROGRESS.md 待验证清单），
撰写 `验证报告.md` 并推送。判断标准：

- 某结论在 ≥3 个批次方向一致且累计样本量足够 → 判定成立/不成立
- 批次间差异大（如分年度命中率）→ 如实报告失效条件，不做过度概括
- 与原报告口径差异（成交额来源、复权方式）在报告中显式标注

## 抓取数据的耐久性（全部归档 GitHub，后续测指标免重抓）

| 数据 | 位置 | 单批大小 | 全量预计 |
|---|---|---|---|
| 前复权日线（OHLCV+真实成交额） | `results/batch_*_kline.*` | ~23MB | ~85MB |
| 估值历史（PE_TTM/PB/收盘/股本/行业，2021-06起） | `results/batch_*_val.*` | ~16MB | ~60MB |
| 日级特征（fwd60/距高点/量比/PE分位） | `results/batch_*_days.*` | ~14MB | ~50MB |
| 波段及启动前特征 | `results/batch_*_waves.csv` | ~1.2MB | ~5MB |
| 定期业绩（净利/ROE/毛利率/公告日，2022年报~2026三季报） | `results/fin_all.pkl.gz` | 一次性 6.1MB | ✅已归档 |
| 不复权日线（对照口径） | `results/batch_*_kline_raw.*` | ~23MB | ~85MB（全量抓完后补归档） |

GitHub 仓库总量预计 ~300MB，单文件均 <100MB 限制。以后测新指标：
`git clone` → `python3 scripts/09_restore_cache.py`（或直接读 results/ 下的 parquet/pkl.gz）即可，无需任何重抓。

- 逐票 pkl 缓存（本机）：断点续跑用，销毁即丢
- 估值/财务重建成本：03 约 20 分钟、04 约 5 分钟（如需）
