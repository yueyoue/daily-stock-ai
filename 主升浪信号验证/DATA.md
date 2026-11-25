# 数据字典与存放位置（DATA.md）

> 任何人/AI 拿到本文件即可定位、理解、加载本项目全部数据。最后更新：2026-11-24（Batch 1 归档后）。

## 一、存放位置

| 位置 | 路径 | 说明 |
|---|---|---|
| **GitHub 仓库** | `https://github.com/yueyoue/daily-stock-ai` | 唯一持久化存储 |
| 项目根目录 | `主升浪信号验证/`（仓库 main 分支） | 文档+代码+数据全在此 |
| **数据归档** | `主升浪信号验证/results/` | ⬅ 测新指标直接用这里的文件 |
| 浏览入口 | `https://github.com/yueyoue/daily-stock-ai/tree/main/主升浪信号验证/results` | |
| 本机工作缓存 | `~/.openclaw/workspace/.openclaw/tmp/stock-analysis/cache/` | 逐票 pkl，仅供断点续跑，可能被销毁 |

**读取方式（测新指标）**：clone 仓库后直接用 pandas 读 `results/` 下文件；或运行
`python3 主升浪信号验证/scripts/09_restore_cache.py` 把日线归档还原为逐票缓存跑完整流水线。

## 二、文件清单（results/ 下）

| 文件模式 | 格式 | 单批大小 | 覆盖 | 状态 |
|---|---|---|---|---|
| `batch_{1..4}_kline.pkl.gz` | pandas pickle(gzip) | ~23MB | 前复权日线 | Batch1✅ 后续批次补 |
| `batch_{1..4}_kline_raw.pkl.gz` | 同上 | ~23MB | 不复权日线（对照口径） | 待全部抓完补归档 |
| `batch_{1..4}_val.pkl.gz` | 同上 | ~16MB | 每日估值 PE/PB/股本/行业 | Batch1✅ |
| `batch_{1..4}_days.pkl.gz` | 同上 | ~14MB | 日级特征（命中率测算表） | Batch1✅ |
| `batch_{1..4}_waves.csv` | CSV | ~1.2MB | 主升浪波段+启动前特征 | Batch1✅ |
| `fin_all.pkl.gz` | pandas pickle(gzip) | 6.1MB | 定期业绩 2022年报~2026三季报 | ✅ 一次性 |
| `universe.csv` | CSV | ~0.2MB | 全A 5223 只清单 | ✅ |
| `index_sh.pkl` | pandas pickle | ~0.2MB | 上证指数日线 2021-01 起 | ✅ |
| `cases_recheck.csv` | CSV | 4KB | 原报告 15 案例校准对照 | ✅ |
| `daily/YYYYMMDD_kline*.pkl.gz` | pandas pickle(gzip) | ~0.3MB/日 | **每日增量日线**（自动采集，交易日一文件，write-once） | 2026-11-25 起，与基线归档按 (code,date) 去重合并读取 |

批次划分（按 universe.csv 的 symbol 升序切片，固定不变）：
Batch1=0:1500、Batch2=1500:3000、Batch3=3000:4500、Batch4=4500:5223。
注：Batch1 以沪市为主（sh6/sh688），深市创业板在 Batch2~4。

## 三、列定义（数据字典）

### batch_*_kline*.pkl.gz —— 日线（核心行情）
| 列 | 类型 | 含义 |
|---|---|---|
| code | str | 6 位股票代码（如 300390） |
| date | int32 | 交易日 YYYYMMDD |
| open/close/high/low | float32 | 价格（元）。kline=前复权；kline_raw=不复权 |
| volume | float32 | 成交量（**手**，1手=100股） |
| amount | float32 | 成交额（**元**，真实值，来自东财） |

区间 2022-04-01 ~ 2026-11-24（约 1126 根/股）。前复权锚定 2026-11-24。
2026-11-25 起每日自动采集：全量归档保持 2026-11-24 基线不变，此后增量在 `results/daily/`
（前复权锚定=抓取当日滚动，raw 无锚定问题；合并读取时按 (code,date) 去重）。

### batch_*_val.pkl.gz —— 每日估值
| 列 | 类型 | 含义 |
|---|---|---|
| code | str | 股票代码 |
| TRADE_DATE | int32 | 交易日 YYYYMMDD |
| PE_TTM | float32 | 市盈率(TTM)。**为负表示亏损**；分位数计算包含负值（与原报告一致） |
| PB_MRQ | float32 | 市净率(MRQ) |
| CLOSE_PRICE | float32 | 不复权收盘价（元） |
| TOTAL_SHARES | float32 | 总股本（股） |
| BOARD_NAME | str | 东财二级行业（如 电池、游戏），用于行业分组 |

区间 2021-06-01 起（供 500 交易日 PE 分位计算）。

### batch_*_days.pkl.gz —— 日级特征（命中率/因子检验用）
| 列 | 类型 | 含义 |
|---|---|---|
| code | str | 股票代码 |
| date | int32 | 交易日 YYYYMMDD |
| fwd60 | float32 | **未来 60 交易日收盘最高涨幅**（close 最大值/当日-1，事后口径） |
| dist250 | float32 | 当日收盘/前 250 交易日最高价 - 1（≤0） |
| amt_ratio | float32 | 量比：近 20 日(含当日)成交额均值 / 其前 120 日均值 |
| pe_pct_2y | float32 | 当日 PE_TTM 在含当日往前 500 交易日内的分位（0~1） |

覆盖 2023-06-01 ~ 2026-08-25（留 60 日前向窗口），每交易日一行。

### batch_*_waves.csv —— 主升浪波段 + 启动前特征
识别口径：起点=局部最低收盘(±10日)，终点=段内最高价日，涨幅(高/起点收盘-1)≥50%，
段内收盘回撤<20%，持续 20~400 交易日；重叠波段取涨幅最大者。
| 列 | 含义 |
|---|---|
| code / symbol / name / industry | 股票标识与东财二级行业 |
| start_date / peak_date / end_scan_date | 启动低点日 / 高点日 / 扫描结束日 |
| close_start / price_peak | 起点收盘价 / 段内最高价（前复权元） |
| gain_high / gain_close | 涨幅：高/起点收盘-1（主口径）；收盘/起点收盘-1 |
| dur | 低点到高点的交易日数 |
| dd_seg | 段内收盘最大回撤（<0.2） |
| dist250 | 启动日距 250 日高点（=收盘/250日最高价-1） |
| rebound250 | 启动日距 250 日低点反弹幅度 |
| amt20_120 | 量比(不含启动日)：前20日均额/再前120日均额 |
| amt20_120_ovl | 量比(重叠)：前20日均额/近120日均额 |
| pe_ttm / pb | 启动日 PE(TTM) / PB |
| pe_pct / pb_pct | 启动日 PE/PB 两年分位（0~1，窗口500交易日含当日） |
| profit_yoy / rev_yoy / roe / gpr | **启动日前已公告**最新报告期：净利同比%/营收同比%/加权ROE%/毛利率% |
| fin_reportdate / fin_notice | 该报告期与公告日期（用于复核"当时可见"） |

### fin_all.pkl.gz —— 定期业绩（全市场）
| 列 | 含义 |
|---|---|
| code / name | 股票 |
| reportdate / notice_date | 报告期 / **公告日期**（做前视约束的关键字段） |
| sjltz / ystz | 净利润同比% / 营收同比%（累计口径） |
| parent_np / revenue | 归母净利(元) / 营业收入(元) |
| roe / bps / xsmll | 加权ROE% / 每股净资产 / 毛利率% |

覆盖 2022-12-31 ~ 2026-09-30 共 16 个报告期，13.8 万行（含新三板，按 code 过滤）。

### universe.csv —— 股票清单
| 列 | 含义 |
|---|---|
| symbol | sh600000 / sz000001 格式（腾讯行情接口用） |
| code / name | 6 位代码 / 名称 |
| is_st | 名称含 ST 标记 |
| mktcap | 清单抓取时总市值（万元，仅参考） |

### index_sh.pkl —— 上证指数日线（2021-01 起）：date/open/close/high/low/volume

## 四、数据质量与已知限制

1. 价格为前复权（经济收益正确）；原报告用不复权，波段涨幅有差异（kline_raw 可对照）
2. 成交额为东财真实值；与原报告 iFind 口径有 3~6% 差异（见 cases_recheck.csv 校准）
3. float32 存储，精度约 7 位有效数字，对本项目所有统计无影响
4. PE 为负（亏损）时 PE 分位含义弱，跨亏损段比较需谨慎（用 PB 替代）
5. fwd60 是"60 日内最高涨幅"事后口径，实操需打折
6. 停牌股该日无行；退市股在 2022-04 后无数据
7. 财务为累计口径（如三季报=前三季度累计），公告日以 NOTICE_DATE 为准

## 五、更新规则

每批测试完成：`python3 scripts/08_export_batch.py a b n` 生成归档 → `bash sync_github.sh` 推送。
新数据列/新口径一律新增文件或新增列，**不覆盖既有定义**；变更记录在 BATCH_LOG.md。

## 六、抓取进度（每次抓取+推送后必须立即更新本节）

> 最后更新：2026-11-25 04:51 UTC（GitHub Actions 自动采集+归档）

### 数据集级进度

| 数据集 | 进度 | 说明 |
|---|---|---|
| 股票清单 universe | ✅ 5223/5223 | 完成 |
| 每日估值 val | ✅ 5223/5223 | 完成 |
| 定期业绩 fin | ✅ 16 期 13.8 万行 | 完成 |
| 前复权日线 kline | ✅ 5223/5223 | 东财 5095 + 腾讯兜底 128（14_fallback_sina.py） |
| 不复权日线 kline_raw | 🔄 3824/5223 | 唯一剩余项：`python3 02_fetch_kline.py raw` 续抓，齐后重跑各批 08 补 raw 归档 |
| 主升浪识别 detect | ✅ 全部完成：**15699 段/4940 只** | 396 万交易日特征表 |

### 批次级进度（接手 AI 看这张表就够）

| 批次 | 切片 | 前复权日线 | 不复权 | 识别/特征 | 测试统计 | 归档推送 | 状态 |
|---|---|---|---|---|---|---|---|
| Batch1 | 0:1500 | ✅ | ✅ | ✅ 3807 段 | ✅ | ✅ | **完成** |
| Batch2 | 1500:3000 | ✅ | ✅ | ✅ 4458 段 | ✅ | ✅ | **完成** |
| Batch3 | 3000:4500 | ✅ | 824/1500 | ✅ 4954 段 | ✅ | ✅ | **完成** |
| Batch4 | 4500:5223 | ✅ | 0/723 | ✅ 2480 段 | ✅ | ✅ | **完成** |

### 剩余事项（收尾清单）

1. 不复权日线抓齐（3824→5223）后：逐批重跑 `08_export_batch.py a b n` 补 raw 归档并推送
2. 验证报告与 CONCLUSIONS 已基于全量样本更新（见 验证报告.md）；如需新指标直接用 results/ 数据
3. 每日增量采集已自动化：GitHub Actions `data-fetch`（每交易日北京 01:30 自动；或手动 Run workflow，
   可选 mode=both/qfq/raw 与 full_export=true 重导全量批次归档）→ `results/daily/YYYYMMDD_*.pkl.gz`，
   **详见 `DAILY_COLLECT.md`（每日自动采集功能文档）**

### 接手 AI 操作卡（照做即可，全部幂等可重复）

```bash
cd 主升浪信号验证/scripts
# 日常采集已自动化（data-fetch：恢复→补抓→results/daily/ 增量→提交推送）；本地手动：
python3 02_fetch_kline.py both        # 补抓/刷新日线（过期缓存自动重抓，终点=当日）
python3 14_fallback_sina.py           # 东财断连时兜底（腾讯/新浪，只补前复权）
python3 15_export_daily.py            # 导出每日增量到 results/daily/（当日未收盘不导出）
# raw 抓齐后补全量归档：python3 08_export_batch.py a b n（4 批各跑一次，或 Actions full_export=true）
# 更新本文档「六」+ BATCH_LOG.md 后推送：
bash ../sync_github.sh "msg"
```

**铁律**：每次抓取有实质进展并推送后，**必须立即更新本节进度表**（数量+时间戳）；
只改数字不改结构。全部 4 批完成后写验证报告并更新 CONCLUSIONS.md。

### 完成标志

- 4 批次全部 ✅ 测试统计 + 归档推送
- kline_raw 补齐后重跑各批 `08_export_batch.py` 补 raw 归档
- `验证报告.md` 完成并推送

