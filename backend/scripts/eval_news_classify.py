"""T6.2 资讯降噪分级自检脚本（人工评估辅助 + 兜底验证）。

用途（对应 T6.2 验收「抽 20 条人工评估分级合理性；LLM 不可用时不丢数据」）：
    1. 构造 **20 条人工标注样本**（5 高 / 5 中 / 5 低 / 5 无关），在**独立临时 SQLite 库**
       中入库为「未分级」，调用 app.services.news_classify.classify_pending 分级；
    2. 打印「序号 / 预期 / 实际 / 是否一致」表格与命中率，供**人工**核对分级合理性；
    3. --offline：用必然失败的 LLM 桩验证兜底——全部保持「未分级」、资讯条数不变、退出码 0。

用法（backend/ 目录下）：
    python scripts/eval_news_classify.py --offline                 # 兜底验证（无 Key/无网络可跑，可进 CI）
    python scripts/eval_news_classify.py                            # 用系统设置里已保存的 LLM Key 真实分级
    python scripts/eval_news_classify.py --api-key sk-xxx --model qwen-plus   # 临时注入 Key 真实分级

退出码：数据无丢失且脚本流程正常 0；检测到数据丢失 1；缺少 LLM 配置且未 --offline 2。

注意：样本为**人工构造**的评估样本（非真实抓取新闻），仅用于核对分级口径。
      脚本只写临时库（DATABASE_URL 指向 tempdir），不触碰业务库。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 入口

# ---------- 人工标注样本（预期等级, 标题, 正文, 来源） ----------
SAMPLES: list[tuple[str, str, str, str]] = [
    # 高（重大政策 / 突发事件 / 大额订单 / 业绩暴雷）
    ("高", "央行宣布全面降准0.5个百分点", "释放长期资金约1万亿元，市场流动性预期显著改善。", "人工构造样本"),
    ("高", "监管发布程序化交易管理新规", "明确高频交易报告与异常交易监控要求，涉及全市场程序化交易。", "人工构造样本"),
    ("高", "某设备龙头公告中标120亿元大额订单", "订单金额占其上年营业收入约45%，预计对未来两年业绩产生重大影响。", "人工构造样本"),
    ("高", "某白马股三季报业绩暴雷", "净利润同比下滑90%，公司同时下调全年业绩指引。", "人工构造样本"),
    ("高", "交易所调整融资融券标的与结算规则", "新增标的范围扩大，涉及杠杆资金规模较大。", "人工构造样本"),
    # 中（行业数据 / 机构调研 / 产业进展 / 券商观点）
    ("中", "半导体设备10月销售额同比增长18%", "行业月度数据显示设备景气度延续，国产化率继续提升。", "人工构造样本"),
    ("中", "机构密集调研光伏组件企业", "多家机构关注行业库存拐点与排产变化。", "人工构造样本"),
    ("中", "多家券商上调非银金融板块评级", "看好流动性改善对券商自营与经纪业务的带动。", "人工构造样本"),
    ("中", "新能源汽车10月销量数据出炉", "环比增长8%，行业整体渗透率继续上行。", "人工构造样本"),
    ("中", "工信部发布工业机器人产业进展要点", "提及关键零部件国产化进展与下游需求结构变化。", "人工构造样本"),
    # 低（个股日常公告 / 例行披露）
    ("低", "某公司公告变更注册地址", "公司注册地址由A区迁至B区，生产经营不受影响。", "人工构造样本"),
    ("低", "某公司独立董事辞职", "因个人原因辞去独立董事职务，公司称不影响正常经营。", "人工构造样本"),
    ("低", "某公司披露例行月度经营数据", "月度销售数据小幅波动，未触及重大事项披露标准。", "人工构造样本"),
    ("低", "某公司解除部分股份质押", "控股股东解除质押股份占其持股比例较小。", "人工构造样本"),
    ("低", "某公司参加行业展会", "公司在展会披露日常经营交流信息，无重大事项。", "人工构造样本"),
    # 无关（生活服务 / 娱乐体育 / 社会民生）
    ("无关", "强降雨天气出行提示", "多地地铁高峰限流，提醒市民合理安排出行时间。", "人工构造样本"),
    ("无关", "国足公布世预赛集训名单", "教练组公布最新一期集训球员名单。", "人工构造样本"),
    ("无关", "某歌手巡回演唱会门票开售", "演唱会门票于今日上午开售。", "人工构造样本"),
    ("无关", "某综艺节目本周开播", "节目官宣定档，将在周末播出第一期。", "人工构造样本"),
    ("无关", "某城市新开放一处城市公园", "公园面积约20公顷，向市民免费开放。", "人工构造样本"),
]

BATCH = len(SAMPLES)


def _failing_llm(db, prompt, **kwargs):
    """必然失败的 LLM 桩：模拟未配置 Key（AppError 503）→ 走兜底分支。"""
    from app.core.errors import AppError

    raise AppError(503, "eval offline stub: LLM 不可用")


def _prepare(db):
    from app.models.content import News

    db.query(News).delete()
    db.commit()
    base = datetime(2026, 11, 5, 8, 0)
    for i, (_expect, title, content, source) in enumerate(SAMPLES):
        db.add(News(published_at=base - timedelta(minutes=i), title=title,
                    content=content, source=source, impact_level="",
                    url="", dedup_hash=f"eval-{i}"))
    db.commit()


def _actual_levels(db):
    from app.models.content import News

    rows = db.query(News).order_by(News.dedup_hash.asc()).all()
    return {r.dedup_hash: r.impact_level for r in rows}


def _print_table(actual: dict) -> int:
    lines = [
        "",
        "| 序号 | 预期 | 实际 | 一致 | 标题 |",
        "| --- | --- | --- | --- | --- |",
    ]
    matches = 0
    for i, (expect, title, _c, _s) in enumerate(SAMPLES):
        got = actual.get(f"eval-{i}", "")
        same = got == expect
        matches += 1 if same else 0
        lines.append(f"| {i + 1} | {expect} | {got or '未分级'} | {'✅' if same else '❌'} | {title} |")
    print("\n".join(lines))
    total = len(SAMPLES)
    print(f"\n命中率：{matches}/{total}（人工核对分级口径合理性；不一致项请结合正文判断口径）")
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description="T6.2 资讯降噪分级自检（20 条人工标注样本）")
    parser.add_argument("--offline", action="store_true", help="用必然失败的 LLM 桩验证兜底（无 Key 可跑）")
    parser.add_argument("--api-key", default="", help="临时注入 LLM API Key（仅本进程内存/临时库）")
    parser.add_argument("--model", default="", help="临时指定模型名（如 qwen-plus）")
    args = parser.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="eval_news_classify_")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(tmpdir, 'eval.db').as_posix()}"
    os.environ.setdefault("SECRET_KEY", "eval-secret")

    from app.models.base import SessionLocal, init_db
    from app.services.news_classify import classify_pending

    init_db()
    db = SessionLocal()
    try:
        _prepare(db)
        from app.models.content import News

        before = db.query(News).count()

        llm = _failing_llm if args.offline else None
        if not args.offline:
            from app.services.config_store import get_effective, save_config

            fields = {}
            if args.api_key:
                fields["llm_api_key"] = args.api_key
            if args.model:
                fields["llm_model"] = args.model
            if fields:
                save_config(db, **fields)
            if not get_effective(db, "llm_api_key"):
                print("未配置 LLM API Key（可用 --api-key 临时注入，或加 --offline 只做兜底验证）")
                return 2

        stats = classify_pending(db, limit=BATCH, batch_size=BATCH, llm=llm)
        after = db.query(News).count()
        actual = _actual_levels(db)
        unclassified = sum(1 for v in actual.values() if not v)
        lost = before - after

        if args.offline:
            print("== 兜底验证（--offline：LLM 必然失败）==")
            print(f"入库条数 before={before} after={after} 丢失={lost}")
            print(f"统计：{stats}")
            ok = lost == 0 and unclassified == before and stats["failed_batches"] == 1
            print("结果：数据零丢失、全部保持「未分级」原样 "
                  + ("✅ 通过" if ok else "❌ 未通过"))
            return 0 if ok else 1

        print("== 人工评估（20 条人工标注样本）==")
        print(f"入库条数 before={before} after={after} 丢失={lost}")
        print(f"统计：{stats}")
        _print_table(actual)
        if lost:
            print("❌ 检测到数据丢失：分级过程不得删除任何资讯")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())