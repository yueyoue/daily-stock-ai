# 估值模型模块补丁 · 应用说明（bw-value-v1.0）

> 面向 `yueyoue/daily-stock-ai`。本补丁只在本地副本上实现与验证，**未推送远程、未改动你的 Docker 部署**。

## 一、新增与修改的文件

**新增（后端）**
- `backend/app/services/valuation.py` — 估值快照（PE/PB/PS/PCF/市值/股息率，双 PE 口径）
- `backend/app/services/bw_model.py` — 风格分档、硬门槛、加权评分、区间位置、财报可信度 gate
- `backend/app/api/model.py` — 三个接口
- `backend/tests/test_bw_model.py` — 19 个用例

**新增（前端）**
- `frontend/src/pages/ModelPage.tsx` — 估值模型页（扫描表 / 规则配置 / 单票抽屉）

**修改**
- `backend/app/main.py` — 注册 `model_router`（挂鉴权）
- `frontend/src/api/client.ts` — 新增 `modelApi` 与类型
- `frontend/src/App.tsx` — 新增 `/model` 路由
- `frontend/src/components/AppLayout.tsx` — 侧栏新增「估值模型」

**未改动**：数据库表结构（不需要迁移）、既有选股器与回测的行为、采集链。

## 二、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/model/rules` | 规则元数据：权重、阈值、无风险利率、6 类风格门槛 |
| POST | `/api/model/scan` | 全市场扫描；body：`{trade_date?, style?, verdict?, limit?}` |
| GET | `/api/model/evaluate/{code}` | 单票评估：档位、分数、逐条命中、区间位置、红旗 |

均需登录会话（与既有业务接口一致）。

## 三、口径（重要）

- **PE 双口径并存**：`pe_annualized`（最新报告期年化，等同东财"市盈率-动态"）与 `pe_static`（最近年报）。实测两源差异 4%~11%（茅台 21.61 vs 22.61，中国移动 16.66 vs 18.44），**禁止混用**；`meta.pe_type` 标注当前口径。
- **价格用不复权 close**；市值 = 收盘价 × 总股本（总股本优先取 `extra` 总股本，缺失时用 `equity / bvps` 反推）。
- **股息率**：取 `extra` 每股股利（多键探测）；取不到返回 `None` → 对应门槛在 `checks` 中标注 `skipped`，既不当通过也不当失败。
- **财报可信度 gate**：第 0 层，不过关直接排除；银行/保险豁免负债率与速动比率（经营模式差异）。
- **关键估值门槛**（PE/PB/股息率）不满足 → 直接排除，不给"观察"档（"贵了就是贵了"）。

## 四、验证结果

- 新增用例：`python -m pytest tests/test_bw_model.py -q` → **19 passed**
- 全量回归：`python -m pytest -q` → **276 passed, 3 failed**；3 个失败均在 `tests/test_migrations.py`，原因是本机 Windows 下 `configparser` 用 GBK 解析含中文的 `alembic.ini`（`UnicodeDecodeError`），属环境编码问题，与本模块无关（Linux/CI 不受影响）。
- 前端：`npx tsc -p tsconfig.json --noEmit` 通过；`npm run build` 通过。

## 五、应用步骤

1. 解压本补丁到项目根目录（覆盖同名文件）。
2. 后端：无需迁移；重启后端进程或重建镜像即可。首次调用 `/api/model/scan` 会用库内 K 线 + 财务指标实时构建快照（全市场约 5000 只，注意首次耗时）。
3. 前端：`cd frontend && npm install && npm run build`，随后重建镜像（`frontend/dist` 由镜像静态托管）。
4. 登录后侧栏出现「估值模型」，进入即触发一次扫描。

## 六、已知限制（如实列出）

1. **股息率依赖分红数据**，当前未接采集 → 银行/红利风格的股息率门槛多为 `skipped`；需要新增 `collectors/dividend.py`（akshare 分红送配，接口名需容器内实测）才能启用。
2. **保险为降级口径**：无 EV/NBV，暂用 PB + 股息率 + ROE 近似。
3. **地产只列观察**：去化与减值数据不足，不参与评分。
4. **未映射行业归 other**：不参评，只出现在列表里。
5. **扫描为同步实时计算**：全市场规模下首次可能超过若干秒；若纳入定时任务，建议加缓存表（第二期）。
6. **gate 是红旗筛查**，不是造假识别；命中只代表"数据不自然，需人工复核"。

## 七、第二期建议（未做）

- 分红采集入库 → 启用股息率门槛、真实红利筛选
- 估值快照落表（每日一份）→ 回测直接用历史快照，避免"用今天算历史"
- 模型接入回测 `candidate_fn` → 跑 2019–2026 净值与稳健性测试
- 行业映射覆盖表（东财行业名 → 风格）改为可配置