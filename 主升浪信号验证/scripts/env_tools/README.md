# env_tools/ —— 环境工具（非口径代码，不参与测试流水线定义）

供接手 AI 使用。默认适配 OpenClaw 沙箱布局（工作区 `~/.openclaw/workspace/`、克隆在
`workspace/daily-stock-ai/`）；其他目录布局改各文件头的路径常量即可。
机制说明见 BATCH_LOG.md「运行与归档注」与 HANDOFF.md「接手备忘」；已知坑见 BATCH_LOG「接手会话日志」。

| 工具 | 用途 | 用法 |
|---|---|---|
| `restore_extra.py` | results/ 归档 → 缓存补恢复（val/features/waves/fin；09_restore_cache 只管日线） | `python3 restore_extra.py` |
| `inc_snapshot.py` | 增量抓取快照 `batch_{n}_inc{k}_kline*.pkl.gz`（write-once，09 可识别恢复） | `python3 inc_snapshot.py` |
| `paced_fetch.py` | 日线节流续抓（探活+指数冷却+自适应降速，复用 02 抓取函数同口径；限速不空磨） | `python3 paced_fetch.py [qfq\|raw\|both]` |
| `git-cred-helper.sh` | `sync_github.sh` 的 git 凭据 helper 模板（token 读本地文件，零暴露） | 路径传给 `credential.helper` |

注意：
- 全部幂等可中断：已抓/已恢复的自动跳过；限速 empty 不落缓存，可重抓。
- `paced_fetch.py` 依赖 `02_fetch_kline.py`（同目录上级 scripts/）与 `utils.py`，别单独拷走。
- 令牌文件放 `~/.openclaw/workspace/.openclaw/tmp/token.txt`，**不入仓**；任务结束建议轮换令牌。
