# daily-stock-ai

个人投研辅助工具：自动归集全市场数据（多源互备、接口可配置）+ AI 生成盘前早报 / 盘后复盘 + 选股回测。

> **不荐股、不接实盘、不构成投资建议**，仅供个人研究使用。

## 当前进度

67 项任务已完成 **61 项（91%）**：阶段 0~7（脚手架与交付链路、数据层与采集、指标引擎、后端 API、前端工作台、选股器与回测、热点资讯观察池、LLM 报告与定时调度）的任务已全部完成并通过测试；**阶段 8（联调与交付）为部署验收阶段**，仓库侧安全自查与文档收尾已完成，其余压测 / 飞牛验收 / 宝塔验收 / 备份恢复演练需在实机执行（阶段 0 的首次实机冒烟 T0.8 随部署验收一并执行）。

进度明细与任务定义见 [`docs/开发进度文档.md`](docs/开发进度文档.md)。

## 30 分钟快速上手

目标：把服务跑起来、登录进去、看到大盘首页与报告页。
前置：一台装了 Docker（含 Compose）的机器（飞牛 fnOS / 宝塔面板 / 任意 Linux 均可）。

### 方式 A：Docker Compose（推荐）

镜像由 GitHub Actions 构建（GHCR 镜像 + 离线 tar 包），本地用 Docker Compose 拉起。

**第 1 步 · 取镜像**（二选一）

- **推荐（离线包）**：仓库 → Actions → `build-image` → 最新一次运行 → Artifacts → 下载 `daily-stock-ai-image` → 解压得到 `daily-stock-ai-image.tar.gz` → `docker load -i daily-stock-ai-image.tar.gz`
- 直连拉取：`docker pull ghcr.io/yueyoue/daily-stock-ai:latest`（ghcr.io 国内常超时，超时就走离线包，不要硬磕）

**第 2 步 · 准备部署目录**

```bash
mkdir -p /opt/daily-stock-ai && cd /opt/daily-stock-ai
# 把仓库根目录的 docker-compose.yml 与 .env.example 放进来
cp .env.example .env && vi .env     # 按下表填写必填项
mkdir -p data                       # app 数据目录（指标缓存、导出文件）
```

**第 3 步 · 启动并自检**

```bash
docker compose up -d
docker compose ps                 # 各服务状态应为 healthy
curl http://localhost:8000/api/health
docker exec dsa-app date          # 应显示 CST（+08），不是 UTC
```

**第 4 步 · 登录**：浏览器打开 `http://<机器IP>:8000`，用 `.env` 中 `ADMIN_USERNAME`（默认 `admin`）与 `ADMIN_INITIAL_PASSWORD` 登录，首登后请在页面改密码。

**第 5 步 · 首次采集**：网页右上角「手动触发采集」拉取 20 年 K 线 + 财务数据，首轮耗时较长，可在「任务监控」页看进度。

#### `.env` 必填项

| 变量 | 是否必填 | 说明 |
|---|---|---|
| `SECRET_KEY` | 必填 | 会话签名密钥，生产替换为长随机串 |
| `DB_ROOT_PASSWORD` | 必填 | MySQL root 密码（仅容器内部使用） |
| `DB_PASSWORD` | 必填 | 应用库用户（`dsa`）密码 |
| `LLM_API_KEY` | 必填 | 大模型 API Key，**只经环境变量注入，不入库、不回显前端**（页面仅显示尾 4 位） |
| `ADMIN_USERNAME` | 可选 | 默认 `admin` |
| `ADMIN_INITIAL_PASSWORD` | 可选 | 首次启动创建管理员口令；留空则随机生成并打印在容器日志 |
| `LLM_MODEL` | 可选 | 默认 `qwen-plus` |
| `ALERT_WEBHOOK` | 可选 | 任务失败告警地址；留空则失败只落日志与任务日志 |
| `API_DOCS_ENABLED` | 生产建议 `false` | 关闭 `/api/docs`、`/api/openapi.json` 对外暴露（生产模板默认已关） |

> `.env` 已列入 `.gitignore` 与 `.dockerignore`，既不会进 git，也不会进镜像层。
> compose 已内置 `TZ=Asia/Shanghai`，**勿删**——早报 08:00 / 复盘 17:00 依赖它。

### 方式 B：本地开发（改代码用）

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

## 文档

| 文件 | 说明 |
|---|---|
| [docs/AI投研分析平台开发文档.md](docs/AI投研分析平台开发文档.md) | 开发文档：架构 / 功能 / 数据 / API / 部署 |
| [docs/开发进度文档.md](docs/开发进度文档.md) | **开发执行任务清单（接手必读）**：67 项任务分解 + 进度跟踪，做完一项更新状态 |
| [docs/部署指南-飞牛与宝塔.md](docs/部署指南-飞牛与宝塔.md) | 飞牛 fnOS / 宝塔面板部署分步指南（含备份与恢复演练） |
| [docs/开发进度表.md](docs/开发进度表.md) | 周级计划概览（存档参考） |
| [docs/AI投研工作台.html](docs/AI投研工作台.html) | 前端演示原型（浏览器直接打开） |
| [docs/原型-实现一致性核对.md](docs/原型-实现一致性核对.md) | 预览原型 ↔ 实现逐项核对结论（T8.7 交付评审输入） |

## 技术栈

- **后端**：FastAPI + SQLAlchemy + Alembic + APScheduler（Asia/Shanghai）+ LiteLLM；生产 MySQL，测试 SQLite。
- **前端**：React 18 + Vite 5 + TypeScript + antd 5 + ECharts。
- **交付**：单镜像——后端托管前端构建产物，唯一对外端口 8000；mysql 不映射宿主端口。