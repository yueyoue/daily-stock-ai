"""资讯降噪分级服务（T6.2，开发文档 §4 模块6）：LLM 过滤无关消息 + 高/中/低影响等级。

口径（模块内固化，与开发文档 §4 模块6 一致）：
- 分级对象：news 表中 impact_level 为空（未分级）的资讯（T6.1 入库时留空，分级回填不走入库服务）
- 等级取值：高 / 中 / 低；与盘面基本无关的消息判为「无关」
- 过滤语义：无关消息**不删除**（不丢数据），标 impact_level="无关"；
  /api/news/list 默认不返回「无关」（only_relevant=true），显式 impact_level=无关 可查
- 失败兜底：LLM 不可用 / 输出无法解析 → 该批资讯保持「未分级」原样，不写入任何变更，
  仅记 task_log 与统计（failed_batches / unclassified / last_error），**绝不因 LLM 故障丢数据**
"""
import json
import logging
import re

from sqlalchemy.orm import Session

from app.models.content import News
from app.services.llm import complete
from app.services.pipeline import log_stage

logger = logging.getLogger("app.news_classify")

LEVELS = ("高", "中", "低")
IRRELEVANT = "无关"
UNCLASSIFIED = ""  # 未分级（T6.1 入库默认值）
ALLOWED = set(LEVELS) | {IRRELEVANT}
DEFAULT_BATCH = 20   # 单批条数（控制 prompt 长度，兼顾分级稳定性）
CONTENT_SNIPPET = 200  # 单条正文截断字符数

SYSTEM_PROMPT = (
    "你是 A 股资讯降噪助手。请逐条判断资讯对当日 A 股盘面的影响程度，给出等级：\n"
    "高 = 可能显著影响大盘/板块或个股股价（重大政策、突发事件、大额订单、业绩暴雷等）；\n"
    "中 = 对特定行业/板块有参考价值（行业数据、机构调研、产业进展、券商观点等）；\n"
    "低 = 影响有限（个股日常公告、例行披露、小额事项等）；\n"
    "无关 = 与 A 股盘面基本无关（生活服务、娱乐体育、社会民生、纯广告等）。\n"
    "只输出 JSON 数组，不要任何解释文字，不要 Markdown 代码块。"
)


def build_prompt(items: list[dict]) -> str:
    """构造分级提示词；items 为 [{index,title,content,source}]。"""
    lines = [
        "请为下列资讯逐条判定影响等级，严格输出如下 JSON 数组（index 与输入一致）：",
        '[{"index":0,"level":"高|中|低|无关","reason":"不超过20字"}]',
        "",
    ]
    for it in items:
        snippet = re.sub(r"\s+", " ", str(it.get("content") or "")).strip()[:CONTENT_SNIPPET]
        lines.append(f'[{it["index"]}] 来源：{it.get("source") or "未知"}；标题:{it.get("title")}'
                     + (f"；正文:{snippet}" if snippet else ""))
    return "\n".join(lines)


def _extract_json_array(raw) -> object:
    """从 LLM 文本中提取 JSON 数组；剥离 Markdown 代码块与多余文字，失败返回 None。"""
    text = str(raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None


def parse_levels(raw, count: int) -> list:
    """LLM 输出 → 长度 count 的等级列表；无法解析/越界/非法值 → None（该条保持未分级）。"""
    out: list = [None] * count
    data = _extract_json_array(raw)
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < count:
            continue
        level = str(entry.get("level") or "").strip()
        if level in ALLOWED:
            out[idx] = level
    return out


def classify_batch(db: Session, items: list[dict], *, llm=None) -> list:
    """单批分级：返回等级列表（None=未分级）。llm 可注入（测试用），默认走 LLM 网关。"""
    llm = llm or complete
    raw = llm(db, build_prompt(items), system=SYSTEM_PROMPT,
              max_tokens=1024, temperature=0.2)
    return parse_levels(raw, len(items))


def classify_pending(db: Session, *, limit: int = 50, batch_size: int = DEFAULT_BATCH,
                     llm=None) -> dict:
    """给未分级资讯打等级并回填 impact_level；LLM 故障时保持原样（不丢数据）。

    返回统计：{pending, batches, failed_batches, classified:{高,中,低}, irrelevant,
              unclassified, last_error, ok}
    """
    limit = max(1, int(limit))
    batch_size = max(1, int(batch_size))
    rows = (db.query(News)
            .filter(News.impact_level == UNCLASSIFIED)
            # 有发布时间者优先、近期优先；无发布于时间者排最后（is_(None) 升序在两库均成立）
            .order_by(News.published_at.is_(None), News.published_at.desc(), News.id.desc())
            .limit(limit).all())

    stats = {
        "pending": len(rows), "batches": 0, "failed_batches": 0,
        "classified": {k: 0 for k in LEVELS}, "irrelevant": 0, "unclassified": 0,
        "last_error": "",
    }
    if not rows:
        stats["ok"] = True
        return stats

    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        items = [{"index": i, "title": r.title,
                  "content": r.content or "", "source": r.source}
                 for i, r in enumerate(chunk)]
        try:
            levels = classify_batch(db, items, llm=llm)
        except Exception as e:  # noqa: BLE001 LLM 供应商异常类型不可枚举 → 兜底不丢数据
            stats["failed_batches"] += 1
            stats["unclassified"] += len(chunk)
            stats["last_error"] = str(e)[:200]
            logger.warning("资讯分级批次失败（保持未分级，不丢数据）：%s", str(e)[:200])
            continue
        stats["batches"] += 1
        for i, row in enumerate(chunk):
            level = levels[i] if i < len(levels) else None
            if level in LEVELS:
                stats["classified"][level] += 1
            elif level == IRRELEVANT:
                stats["irrelevant"] += 1
            else:
                stats["unclassified"] += 1  # 未识别/缺项 → 保持未分级
                continue
            row.impact_level = level
        db.commit()

    stats["ok"] = stats["failed_batches"] == 0
    log_stage(
        db, "资讯分级", "success" if stats["ok"] else "failed",
        message=(f"pending={stats['pending']} 高={stats['classified']['高']} "
                 f"中={stats['classified']['中']} 低={stats['classified']['低']} "
                 f"无关={stats['irrelevant']} 未分级={stats['unclassified']} "
                 f"失败批次={stats['failed_batches']} {stats['last_error']}"),
    )
    return stats