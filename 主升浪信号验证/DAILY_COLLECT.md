# 每日收盘自动采集（DAILY_COLLECT.md）——数据仓库模式

> 目标：本仓库作为 A 股日线**数据仓库**，每交易日收盘数据自动入仓，供后续测试直接取用。
> 上线 2026-11-25。链路修复与设计记录见 BATCH_LOG.md「接手会话日志」；列定义以 DATA.md 第三节为准。

## 一、触发方式

| 方式 | 规则 | 说明 |
|---|---|---|
| **定时** | GitHub Actions `data-fetch`，cron `30 17 * * 1-5`（UTC） | = 北京时间**每交易日凌晨 01:30**（当日收盘数据已齐）；周末不跑，节假日跑空无害 |
| **手动** | 仓库 → Actions → data-fetch → Run workflow | 可选 `mode`=both/qfq/raw；`full_export`=true 时额外重导全量批次归档（大提交，按需） |

运行不依赖任何人的本地环境：定时任务跑在 GitHub Actions，电脑关机不影响。

## 二、执行链路（.github/workflows/data-fetch.yml）

1. `09_restore_cache.py` —— 从 results/ 归档恢复缓存（universe 自动补种；val/features/waves/fin 一并恢复）
2. `02_fetch_kline.py both` —— 补抓/刷新日线（幂等：过期缓存自动重抓，终点=运行当日；步限时 90 分钟，跑空不阻塞）
3. `14_fallback_sina.py` —— 东财断连时腾讯/新浪兜底（best-effort，只补前复权）
4. `15_export_daily.py` —— 导出**日增量**到 `results/daily/`（当日未收盘不导出，缺口自愈）
5. 更新 DATA.md 第六节时间戳 → `github-actions[bot]` 自动 commit + push

## 三、产物与读取

- **日增量**：`主升浪信号验证/results/daily/YYYYMMDD_kline.pkl.gz` / `YYYYMMDD_kline_raw.pkl.gz`
  每交易日一文件（约 0.3MB），write-once；列定义同 `batch_*_kline`（DATA.md 第三节）：
  `code / date(int32 YYYYMMDD) / open close high low float32 / volume(手) / amount(元)`
- **基线全量归档** `batch_{1..4}_kline*.pkl.gz` 截至 **2026-11-24**，不再每日重写（防仓库膨胀）；
  测试读取 = 基线 + `daily/` 增量，按 **(code, date) 去重**合并
- 不复权（kline_raw）无前复权锚定问题；前复权锚定=抓取当日（滚动），需精确复现用 raw

## 四、验证采集是否成功（次日核对，三选一）

1. **看提交**：Commits 里找 `github-actions[bot]` 的 `data: 自动采集归档 YYYY-MM-DD`
2. **看文件**：`results/daily/` 出现前一交易日的 `*_kline.pkl.gz` + `*_kline_raw.pkl.gz`
3. **看 Actions**：Actions → data-fetch 对应 run 绿勾（失败会红叉，可看失败步骤日志）

## 五、容错与幂等

- 全链路可重复执行：已抓不重抓、已导出不重导；某次限速跑空 → 下次自动补齐
- 抓取失败/超时不阻塞导出与提交（continue-on-error + 90 分钟上限）
- **前置一次性设置**：Settings → Actions → General → Workflow permissions → 勾选 **Read and write**（否则最后推送步失败）
- 手动 `full_export=true` 用于补全量归档（如不复权补齐后）；`daily/` 增量不受影响

## 六、会话/本地侧补充（可选，与 Actions 并行）

- 本地节流续抓：`python3 scripts/env_tools/paced_fetch.py both`（东财限速时探活+冷却+自适应降速）
- 每 30 分钟增量快照推送：`python3 scripts/env_tools/inc_snapshot.py` + `bash sync_github.sh`
  （防会话环境销毁丢抓取进度；由巡检任务自动执行）
- 推送后必须核对输出 `HEAD -> main`（sync 推送失败不返回非零 exit）
