# HANDOFF.md —— 接手提示词（直接复制发给下一个 AI）

```
你接手一个「主升浪启动前信号」扩样本测试项目。严格按以下顺序执行：

一、先读文档（顺序不能乱，都在 GitHub 仓库 https://github.com/yueyoue/daily-stock-ai 的 主升浪信号验证/ 目录）：
1. DATA.md —— 数据字典。重点看「第六节：抓取进度+接手AI操作卡」，那是唯一权威进度表，
   当前每批抓到多少、还缺什么、下一步跑什么命令，全部以它为准
2. CONCLUSIONS.md —— 已得出的全部结论，不要重复验证已判定的内容
3. BATCH_LOG.md —— 各批次测试记录与口径注意事项
4. TEST_PLAN.md / METHOD.md —— 分批计划与测试口径（主升浪定义、特征口径），口径不许改动

二、再干活（照 DATA.md 第六节「接手AI操作卡」执行，命令都是幂等的，可重复、可中断）：
1. 先续抓日线：python3 02_fetch_kline.py both（已抓的自动跳过，不会重复抓取）
2. 每当某批次 1500 只前复权日线齐了，立即跑该批：05_detect.py --slice <切片> → 06_stats.py → 08_export_batch.py
3. 用 sync_github.sh 推送该批结果+数据归档到 GitHub

三、铁律（必须遵守）：
- 每次抓取有进展并推送后，立刻更新 DATA.md 第六节进度表（数量+时间戳），再推送一次
- 测试口径、批次切片、列定义一律沿用现有文档，不许自行更改
- 新发现的特征/结论按 CONCLUSIONS.md 现有格式追加
- 4 批全部完成后：写 验证报告.md、更新 CONCLUSIONS.md、推送

工作目录：~/.openclaw/workspace/主升浪信号验证/（数据缓存在 ../.openclaw/tmp/stock-analysis/cache/）
```
