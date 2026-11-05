# daily-stock-ai

个人投研辅助工具：自动归集全市场数据（多源互备、接口可配置）+ AI 生成盘前早报 / 盘后复盘 + 选股回测。

> **不荐股、不接实盘、不构成投资建议**，仅供个人研究使用。

- [这个项目能做什么](#这个项目能做什么)
- [安装教程](#安装教程)
- [第一次使用](#第一次使用)
- [使用教程](#使用教程)
- [选股条件怎么写](#选股条件怎么写)
- [常见问题](#常见问题)
- [当前进度与更新记录](#当前进度与更新记录)
- [文档索引](#文档索引)
- [技术栈](#技术栈)

---

## 这个项目能做什么

| 模块 | 你能拿到什么 |
|---|---|
| 大盘仪表盘 | 成交额、涨跌家数、涨停梯队、资金流向等按日汇总，快速判断市场情绪 |
| 盘前早报 / 盘后复盘 | 由大模型按固定模板生成的 7 维度报告（未配 LLM Key 时降级为结构化骨架，并如实提示原因） |
| 选股器 | 用 28 项技术指标 + 30 项财务指标自由组合条件，全市场扫描 |
| 策略回测 | 用历史 K 线回放策略，输出净值曲线、胜率、最大回撤、盈亏比与逐笔明细 |
| 热点题材库 / 掘金观察池 / 资讯中心 | 题材热度与关联个股、自选股观察池、财经资讯流（含 AI 降噪分级） |
| 任务监控 / 系统配置 | 采集与报告任务全流程日志、数据源接口增删改与连通测试、定时任务时间配置 |

数据层的设计要点：所有外部数据接口都在「系统配置 → 数据源」里登记，支持 **多源互备**（主源失败自动降级备源）、优先级、启停与连通测试；**业务代码里没有任何硬编码接口**，外部接口改版时只需新增一条线路，不用改代码。

---

## 安装教程

**前置条件**：一台能跑 Docker（含 Compose）的机器——飞牛 fnOS / 宝塔面板 / 任意 Linux / NAS 均可。
镜像已发布在 `ghcr.io/yueyoue/daily-stock-ai:latest`，国内直连超时属常态，可改用下方「离线导入」。

启动后会有 3 个容器：

- `dsa-app`：后端 + 前端静态页，**唯一对外端口 8000**；
- `dsa-mysql`：MySQL 8，只在容器网络内使用，**不映射宿主端口**；
- `dsa-init-data`：一次性权限修复容器，跑完立即 `Exited (0)`，**这是正常的，别删也别重启**。

### 方式 A：飞牛 fnOS 一键安装（新手首选，不用另建 `.env`）

飞牛面板 →「Docker」→ 左侧「Compose」→「新增项目」：

1. 项目名称填 `daily-stock-ai`，路径填 `/vol1/docker/daily-stock-ai`（不存在会自动创建）；
2. 来源选「创建 docker-compose.yaml」，把下面**整段**粘贴进去；
3. 把 3 处 `CHANGE_ME_*` 换成你自己的字符串（`DATABASE_URL` 里的数据库密码与 `MYSQL_PASSWORD` 必须一致）；
4. 勾选「创建项目后立即启动」→ 确定。

```yaml
name: daily-stock-ai

services:
  # 一次性：把数据目录权限改成容器内 appuser(uid 1000)，做完即退出（exited 是正常的）
  init-data:
    image: ghcr.io/yueyoue/daily-stock-ai:latest
    container_name: dsa-init-data
    user: "0"
    entrypoint: ["/bin/sh", "-c", "mkdir -p /app/data && chown -R 1000:1000 /app/data"]
    restart: "no"
    healthcheck:
      disable: true            # 只用一次，关掉镜像自带健康检查，免得显示成 unhealthy
    environment:
      TZ: "Asia/Shanghai"
    volumes:
      - /vol1/docker/daily-stock-ai/data:/app/data

  # 应用：后端 + 前端静态页，唯一对外端口 8000
  app:
    image: ghcr.io/yueyoue/daily-stock-ai:latest
    container_name: dsa-app
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      TZ: "Asia/Shanghai"
      SECRET_KEY: "CHANGE_ME_secret_key"            # 建议替换为随机串：openssl rand -hex 32
      ADMIN_USERNAME: "admin"
      ADMIN_INITIAL_PASSWORD: "CHANGE_ME_admin_password"   # 首次登录用它，登录后可在页面改
      API_DOCS_ENABLED: "false"
      DATABASE_URL: "mysql+pymysql://dsa:CHANGE_ME_db_password@mysql:3306/daily_stock_ai?charset=utf8mb4"
      LLM_API_KEY: ""                               # 可留空；留空时报告降级为结构化骨架
      LLM_MODEL: "qwen-plus"
      SCHEDULER_ENABLED: "true"
      REPORT_AM_CRON: "0 8 * * mon-fri"
      REPORT_PM_CRON: "0 17 * * mon-fri"
      REPORT_AM_ENABLED: "true"
      REPORT_PM_ENABLED: "true"
      COLLECT_AM_CRON: "30 7 * * *"
      COLLECT_PM_CRON: "30 15 * * mon-fri"
      COLLECT_AM_ENABLED: "true"
      COLLECT_PM_ENABLED: "true"
      ALERT_WEBHOOK: ""
    volumes:
      - /vol1/docker/daily-stock-ai/data:/app/data
    depends_on:
      mysql:
        condition: service_healthy
      init-data:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  # 数据库：仅 compose 内部网络使用，不对宿主机暴露端口
  mysql:
    image: mysql:8.0
    container_name: dsa-mysql
    restart: unless-stopped
    environment:
      TZ: "Asia/Shanghai"
      MYSQL_ROOT_PASSWORD: "CHANGE_ME_db_password"
      MYSQL_DATABASE: "daily_stock_ai"
      MYSQL_USER: "dsa"
      MYSQL_PASSWORD: "CHANGE_ME_db_password"       # 必须与上面 DATABASE_URL 里的密码一致
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_unicode_ci
      - --innodb-buffer-pool-size=256M
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      # 用"真能登录并执行语句"作为健康判据（mysqladmin ping 在密码错时也会返回成功，不可靠）。
      # MySQL 首次初始化要 2~3 分钟，所以容忍时间给得很宽，避免被误判导致启动中断。
      test: ["CMD-SHELL", "mysql -uroot -p\"$$MYSQL_ROOT_PASSWORD\" -e 'SELECT 1' >/dev/null 2>&1"]
      interval: 5s
      timeout: 5s
      retries: 60
      start_period: 180s

volumes:
  mysql_data:
```

首次启动需要 **2~4 分钟**（MySQL 第一次初始化较慢），期间页面打不开是正常的，耐心等。
启动完成后浏览器打开 `http://<飞牛IP>:8000`，账号 `admin`，密码填你替换后的 `ADMIN_INITIAL_PASSWORD`。

> ⚠️ **别改已经生效的数据库密码**：MySQL 密码只在数据卷**首次初始化**时写入，之后改它不会生效，反而会导致应用连不上。确实要改密码或彻底重装，先执行：
> ```bash
> cd /vol1/docker/daily-stock-ai && docker compose down -v   # -v 会清空数据库，不可逆
> ```

### 方式 B：任意 Linux / 宝塔，Docker Compose 标准部署

用仓库根目录的 `docker-compose.yml` + `.env.example` 模板版部署，适合想统一管理配置、或需要 `nginx` 反代/SSL 的场景。

**第 1 步 · 取镜像**（二选一）

- **离线导入（推荐）**：GitHub 仓库 → **Actions** → `build-image` → 最新一次运行 → **Artifacts** → 下载 `daily-stock-ai-image` → 解压得到 `daily-stock-ai-image.tar.gz` → `docker load -i daily-stock-ai-image.tar.gz`
- 直连拉取：`docker pull ghcr.io/yueyoue/daily-stock-ai:latest`（ghcr.io 国内常超时，超时就走离线包，不要硬磕）

**第 2 步 · 准备部署目录**

```bash
mkdir -p /opt/daily-stock-ai && cd /opt/daily-stock-ai
# 把仓库根目录的 docker-compose.yml 与 .env.example 放进来
cp .env.example .env && vi .env     # 按下表填写
mkdir -p data                       # app 数据目录（指标缓存、导出文件）
```

**第 3 步 · 启动并自检**

```bash
docker compose up -d
docker compose ps                 # 各服务状态应为 healthy
curl http://localhost:8000/api/health
docker exec dsa-app date          # 应显示 CST(+08)，不是 UTC
```

**第 4 步 · 登录**：浏览器打开 `http://<机器IP>:8000`，用 `.env` 中 `ADMIN_USERNAME`（默认 `admin`）与 `ADMIN_INITIAL_PASSWORD` 登录，首登后请在页面改密码。

宝塔用户：Docker 管理器 → Compose 项目 → 创建并启动；再用宝塔「网站 → 反向代理」指向 `http://127.0.0.1:8000` 并配 SSL。**注意宝塔自带 MySQL 占 3306，所以容器 mysql 不映射宿主端口**。

#### `.env` 关键项

| 变量 | 是否必填 | 说明 |
|---|---|---|
| `SECRET_KEY` | 必填 | 会话签名密钥，生产替换为长随机串（`openssl rand -hex 32`） |
| `DB_ROOT_PASSWORD` | 必填 | MySQL root 密码（仅容器内部使用） |
| `DB_PASSWORD` | 必填 | 应用库用户（`dsa`）密码 |
| `LLM_API_KEY` | 建议填 | 大模型 API Key，**只经环境变量注入，不入库、不回显前端**（页面仅显示尾 4 位）；留空也能跑，报告降级为结构化骨架 |
| `ADMIN_USERNAME` | 可选 | 默认 `admin` |
| `ADMIN_INITIAL_PASSWORD` | 可选 | 首次启动创建管理员口令；留空则随机生成并打印在容器日志 |
| `LLM_MODEL` | 可选 | 默认 `qwen-plus` |
| `ALERT_WEBHOOK` | 可选 | 任务失败告警地址；留空则失败只落日志与任务日志 |
| `API_DOCS_ENABLED` | 生产建议 `false` | 关闭 `/api/docs`、`/api/openapi.json` 对外暴露（生产模板默认已关） |

> `.env` 已列入 `.gitignore` 与 `.dockerignore`，既不会进 git，也不会进镜像层。
> compose 已内置 `TZ=Asia/Shanghai`，**勿删**——早报 08:00 / 复盘 17:00 依赖它。

### 方式 C：本地开发（改代码用）

```bash
# 后端（Python 3.11+；在 backend/ 目录执行，.env 放该目录）
cd backend
python -m venv .venv
. .venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp ../.env.example .env             # 本地无 MySQL 时，额外设 DATABASE_URL=sqlite:///./dev.db
alembic upgrade head                # 建表（SQLite / MySQL 均可）
uvicorn app.main:app --reload --port 8000

# 前端（另开一个终端）
cd frontend
npm install
npm run dev                         # http://localhost:5173，/api 已代理到 :8000
```

- 本地开发默认可设 `DATABASE_URL=sqlite:///./dev.db`，无需 MySQL；生产改用 `mysql+pymysql://...`。
- 开发环境可保留 `API_DOCS_ENABLED=true`，用 `http://localhost:8000/api/docs` 浏览 OpenAPI。
- 前端构建：`npm run build`（产物 `frontend/dist`，容器内由后端托管）；类型检查 `npm run typecheck`。

---

## 第一次使用

1. **登录**：`http://<机器IP>:8000`，账号 `admin`，密码为部署时填的 `ADMIN_INITIAL_PASSWORD`。登录后到「系统配置 → 账号」改密码。
2. **（建议）配大模型 Key**：「系统配置 → 模型」填入大模型 API Key 与模型名（默认 `qwen-plus`），保存后点「测试连通」。不配也能用，只是早报/复盘会降级为结构化骨架，页面会提示原因。
3. **首次取数**：右上角「手动触发采集」——想一次性拉全 20 年 K 线 + 财务选 `full`（耗时长）；只想先看到效果选 `incremental`（增量，快）。到「任务监控」页看进度。
4. **之后不用管**：内置定时任务已按工作日排好（07:30 采隔夜资讯 → 08:00 早报 → 15:30 收盘采集 → 16:00 指标缓存 → 17:00 复盘），非交易日自动跳过。时间可在「系统配置 → 定时任务」调整。
5. **时区自检**：`docker exec dsa-app date` 应显示 CST(+08)；报告时间是按时区触发的。

---

## 使用教程

对着下面这张表就能把 10 个页面用起来（左侧导航顺序一致）：

| 页面 | 地址 | 怎么用 |
|---|---|---|
| 大盘仪表盘 | `/` | 一眼看当日成交额、涨跌家数、涨停梯队、资金流向，用于判断市场情绪 |
| 盘前早报 | `/morning` | 看 08:00 自动生成的早报（7 维度）；可翻历史版本 |
| 盘后复盘 | `/afternoon` | 看 17:00 自动生成的复盘，含情绪评分、风险预警、策略成功率 |
| 选股器 | `/selector` | 左侧编辑选股条件 → 保存为策略 → 点扫描 → 结果可一键加入观察池 |
| 策略回测 | `/backtest` | 选已保存策略，**或**直接填自定义条件 JSON，跑回测看净值曲线/统计卡/逐笔明细 |
| 热点题材库 | `/hot` | 题材热度卡片、关联个股、热度分级 |
| 掘金观察池 | `/watch` | 自选股快照（行情 / ROE / 概念），可分组管理 |
| 资讯中心 | `/news` | 财经资讯流（AI 降噪分级：高/中/低/无关），可切换「显示全部含无关」 |
| 任务监控 | `/task` | 采集/报告任务全链路日志；可按状态筛选（运行中/成功/失败/告警/已跳过），失败原因可展开看摘要 |
| 系统配置 | `/config` | 数据源接口增删改/启停/优先级/连通测试；存储与性能（指标模式 A/B、缓存天数、引擎）；模型/账号/定时任务 |

---

## 选股条件怎么写

选股条件是一段 JSON，结构如下：

```json
{"logic": "and", "conditions": [{"factor": "roe", "op": ">=", "value": 15}]}
```

- `logic`：`and` 或 `or`；
- `conditions[]`：每项是 `factor`（因子名）+ `op`（比较符）+ `value`（阈值）；区间用 `op: "between"` 并配 `value` / `value2`；
- `op` 全集：`>` `>=` `<` `<=` `==` `!=` `between`。

**可用因子**

- 技术面（28 项）：`ma5` `ma10` `ma20` `ma60` `ema12` `ema26` `macd_dif` `macd_dea` `macd_hist` `kdj_k` `kdj_d` `kdj_j` `rsi6` `rsi14` `boll_upper` `boll_mid` `boll_lower` `wr14` `cci14` `bias6` `atr14` `obv` `roc12` `mtm12` `dma_dif` `dma_ama` `vr26` `psy12`
- 财务面（30 项）：`total_revenue` `net_profit` `deduct_net_profit` `operating_cashflow` `invest_cashflow` `finance_cashflow` `total_assets` `total_liabilities` `equity` `eps` `bvps` `ocf_ps` `gross_margin` `net_margin` `operating_margin` `roe` `roa` `debt_to_asset` `equity_ratio` `current_ratio` `quick_ratio` `ocf_to_profit` `ocf_to_revenue` `deduct_ratio` `rd_expense_ratio` `revenue_yoy` `profit_yoy` `ocf_yoy` `revenue_qoq` `profit_qoq`
- 周期类技术指标可带数字后缀（如 `ma30`、`rsi24`），规则为 `^(ma|ema|rsi|atr|cci|wr|bias|roc|mtm|psy|vr)\d+$`

**示例**（ROE ≥ 15 且 MACD 柱在 -0.2 ~ 0.2）：

```json
{
  "logic": "and",
  "conditions": [
    {"factor": "roe", "op": ">=", "value": 15},
    {"factor": "macd_hist", "op": "between", "value": -0.2, "value2": 0.2}
  ]
}
```

> 填了清单外的因子（例如 `pct_change`）会被后端校验拒绝、回测直接失败；请只用上面列出的名字。

---

## 常见问题

**页面打不开，或者飞牛面板提示「部分运行」？**
首次启动 2~4 分钟属正常（MySQL 首次初始化慢）。`dsa-init-data` 显示 `Exited (0)` 也是正常的——它是一次性容器。查状态 `docker compose ps`，看应用日志 `docker logs dsa-app --tail 100`。

**拉镜像卡住 / 超时？**
走离线包：Actions → `build-image` → Artifacts → `daily-stock-ai-image.tar.gz` → `docker load -i`。或在飞牛「Docker → 设置 → 镜像加速器」配置加速后重试。

**早报 / 复盘没有正文，只有骨架？**
说明没配大模型 API Key，页面会提示「降级原因」。到「系统配置 → 模型」填 Key 后重新生成即可。

**改了数据库密码后应用连不上？**
密码只在数据卷首次初始化时生效。要用新密码必须 `docker compose down -v` 先清卷（会清空数据库，不可逆），再重新部署。

**某个采集没数据 / 数据源报错？**
到「系统配置 → 数据源」点「测试连通」定位；主源失败会自动降级到备源。外部免费接口改版是常态，新增一条线路登记即可，**不用改代码**。

**怎么升级？**
`docker compose pull && docker compose up -d`；有表结构变更时先备份。

**数据怎么备份？**
每日 `mysqldump`，具体命令与恢复演练见 [`docs/部署指南-飞牛与宝塔.md`](docs/部署指南-飞牛与宝塔.md) 第 4 节。

**这个工具会自动下单 / 荐股吗？**
不会。不接实盘、不自动下单、不荐股，只做数据归集与研究辅助。

---

## 当前进度与更新记录

67 项任务已完成 **61 项（91%）**：阶段 0~7（脚手架与交付链路、数据层与采集、指标引擎、后端 API、前端工作台、选股器与回测、热点资讯观察池、LLM 报告与定时调度）的任务已全部完成并通过测试；**阶段 8（联调与交付）为部署验收阶段**，仓库侧安全自查与文档收尾已完成，其余压测 / 飞牛验收 / 宝塔验收 / 备份恢复演练需在实机执行。

进度明细与任务定义见 [`docs/开发进度文档.md`](docs/开发进度文档.md)。

**2026-11-05 · 缺陷修复轮次**：根据首轮实机测试结论，修复了 14 个缺陷（D-01 ~ D-14）并顺带修正 2 处新发现的问题（采集 NaN 写入致入库失败并卡死任务行、akshare 版本漂移、超时参数未生效、回测页缺自定义条件入口、cron 校验过弱、方法不匹配返回码、任务日志泄漏原始 SQL 等）。修复已在本地与飞牛实机双向验证：后端 `pytest` 257 passed（3 项为既有 GBK 环境问题）、前端 `tsc --noEmit` 0 错误、飞牛端到端 11 项全过。逐条说明见 [`docs/缺陷修复记录-2026-11-05.md`](docs/缺陷修复记录-2026-11-05.md)。

---

## 文档索引

| 文件 | 说明 |
|---|---|
| [docs/AI投研分析平台开发文档.md](docs/AI投研分析平台开发文档.md) | 开发文档：架构 / 功能 / 数据 / API / 部署 |
| [docs/开发进度文档.md](docs/开发进度文档.md) | **开发执行任务清单（接手必读）**：67 项任务分解 + 进度跟踪 |
| [docs/部署指南-飞牛与宝塔.md](docs/部署指南-飞牛与宝塔.md) | 飞牛 fnOS / 宝塔面板部署分步指南（含备份与恢复演练） |
| [docs/缺陷修复记录-2026-11-05.md](docs/缺陷修复记录-2026-11-05.md) | 首轮实机测试的 14 个缺陷（D-01~D-14）修复说明与验证结果 |
| [docs/开发进度表.md](docs/开发进度表.md) | 周级计划概览（存档参考） |
| [docs/AI投研工作台.html](docs/AI投研工作台.html) | 前端演示原型（浏览器直接打开） |
| [docs/原型-实现一致性核对.md](docs/原型-实现一致性核对.md) | 预览原型 ↔ 实现逐项核对结论 |

---

## 技术栈

- **后端**：FastAPI + SQLAlchemy 2 + Alembic + APScheduler（Asia/Shanghai）+ LiteLLM；生产 MySQL 8，测试 SQLite。
- **前端**：React 18 + Vite 5 + TypeScript + antd 5 + ECharts。
- **交付**：单镜像——后端托管前端构建产物，唯一对外端口 8000；mysql 不映射宿主端口。