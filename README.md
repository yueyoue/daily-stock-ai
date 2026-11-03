# daily-stock-ai

AI投研分析平台 - 项目开发文档与前端演示原型

个人投研辅助工具：自动归集全市场数据（多源互备、接口可配置）+ AI 生成盘前早报/盘后复盘 + 选股回测。不荐股、不接实盘、不构成投资建议。

## 文档

| 文件 | 说明 |
|---|---|
| [docs/AI投研分析平台开发文档.md](docs/AI投研分析平台开发文档.md) | 开发文档 V1.2（架构/功能/数据/API/部署） |
| [docs/开发进度表.md](docs/开发进度表.md) | 单人开发进度计划（约 6~7 周，含里程碑与验收） |
| [docs/部署指南-飞牛与宝塔.md](docs/部署指南-飞牛与宝塔.md) | 飞牛 fnOS / 宝塔面板部署分步指南 |
| [docs/AI投研工作台.html](docs/AI投研工作台.html) | 前端演示原型（浏览器直接打开） |

## 部署（概要）

项目在 GitHub Actions 构建 Docker 镜像（GHCR + 离线 tar 包），拉到飞牛/宝塔本地用 Docker Compose 启动：

```bash
cp .env.example .env   # 填 DB 密码、LLM API Key
docker compose up -d   # 详见 docs/部署指南-飞牛与宝塔.md
```

关键文件：`docker-compose.yml`（含 TZ=Asia/Shanghai、数据卷、健康检查）、`.github/workflows/build-image.yml`（CI 构建）。

## 状态

📐 开发文档与原型已完成，开发尚未开始。进度跟踪见 `docs/开发进度表.md`。
