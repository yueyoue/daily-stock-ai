# 主升浪启动前信号验证（扩大样本复测）

> 用途：验证《主升浪启动前信号复盘.html》(15 案例) 的结论在扩大样本后是否成立，为选股模型提供依据。
> 创建：2026-11-24。**本文件夹自包含：新会话读完 README + PROGRESS + METHOD 即可无缝续跑。**

## 一键续跑

```bash
cd 主升浪信号验证
bash resume.sh          # 自动按顺序续跑，已抓数据自动跳过
```

**分批测试（推荐，见 TEST_PLAN.md）**：每 1500 只一批，抓完即测、测完即推送 GitHub；
每批产物 `results/batch_*_{waves.csv,days.parquet,kline.parquet}` 均已入仓，环境销毁后可免重抓恢复。

或分步手动执行（全部可中断、可重复执行，已抓取的个股数据不会重抓）：

```bash
cd scripts
python3 01_fetch_universe.py     # 全A清单（已完成，5223只）
python3 02_fetch_kline.py both   # 日线：qfq + 不复权，逐票缓存，断点续跑
python3 03_fetch_valuation.py    # 每日 PE/PB/行业（已完成）
python3 04_fetch_financials.py   # 定期业绩+公告日（已完成）
python3 05_detect.py             # 主升浪识别+特征（CPU，约5-15分钟）
python3 06_stats.py              # 统计验证，输出 tables/*.csv
python3 07_cases_check.py        # 15 案例校准对照
```

## 文件结构

| 文件 | 说明 |
|---|---|
| `README.md` | 本文件：续跑指南 |
| `DATA.md` | **数据字典与存放位置**（列定义/格式/加载方式） |
| `CONCLUSIONS.md` | **结论汇总**（所有判定，建模直接引用） |
| `PROGRESS.md` | 进度快照与中间结论（每次中断前更新） |
| `METHOD.md` | 测试方法、数据源、口径定义 |
| `TEST_PLAN.md` | 分批测试计划与环境销毁后的恢复流程 |
| `BATCH_LOG.md` | 各批次测试结果与结论对照（持续累积） |
| `resume.sh` | 一键续跑脚本 |
| `sync_github.sh` | 推送本文件夹到 GitHub 仓库（凭据内部读取） |
| `scripts/` | 01~09 全部代码（05_detect.py 为核心识别器） |
| `results/` | 每批产物：波段特征/日级特征/日线数据 + 案例校准 |
| `tables/` | 06_stats.py 输出的累计验证统计表 |

## 数据缓存（不在 git 内，位于本机工作区）

所有抓取数据按“每只股票一个 pkl 文件”缓存在：
`~/.openclaw/workspace/.openclaw/tmp/stock-analysis/cache/`
（kline/ raw_kline/ val/ fin/ features/ 子目录，当前约 550MB，全量完成后约 800MB~1GB）

- **中断续跑不会重复抓取**：每个脚本先检查缓存文件存在且格式正确才跳过。
- 只有当运行环境被销毁、缓存目录丢失时才需要全量重抓（K线约 1~2 小时，估值约 20 分钟）。
- 若需要跨机器迁移缓存：打包整个 `cache/` 目录放到新机器同路径即可。

## 数据源（东财/腾讯公开接口，无需 token）

- 日线（前复权+不复权，含真实成交额）：东财 `push2his` 分片接口（域名被限速时自动换分片重试）
- 每日 PE_TTM/PB_MRQ/行业：东财 `datacenter-web` RPT_VALUEANALYSIS_DET
- 定期业绩（净利同比/ROE/毛利率/公告日期）：东财 `datacenter-web` RPT_LICO_FN_CPD
- 股票清单：新浪 Market_Center
⚠️ 沙箱网络对部分行情域名（push2his 主域、腾讯 web.ifzq）会限速/屏蔽，脚本已做分片与重试；如大面积失败，先降速（02 里 workers 调 2）再续跑。
