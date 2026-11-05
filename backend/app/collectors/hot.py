"""热点/概念采集（T6.3，开发文档 §3.2）：东财概念板块 → 热点题材归档记录。

标准化记录：{date, title, heat, industry_chain, related_codes}
- heat 分级：按板块当日涨跌幅（高：≥3%；中：≥0%；低：<0%）
- industry_chain：先以概念名作为产业链标签占位（细粒度产业链由后续 LLM 扩展）
- related_codes：成分股代码（截断上限防超长）；成分请求量大 → 只取当日热度 Top N
真机联调点：ak.stock_board_concept_name_em / ak.stock_board_concept_cons_em（T1.11 演练验证）
"""
from datetime import date as date_type

from app.collectors.base import BaseCollector

COLUMN_MAP = {
    "概念名": "title", "名称": "title", "板块名称": "title",
    "涨跌幅": "change_pct",
}

MAX_CODES = 30


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def grade_heat(change_pct: float | None) -> str:
    """热度分级（按当日板块涨跌幅）。"""
    if change_pct is None:
        return "中"
    if change_pct >= 3:
        return "高"
    if change_pct >= 0:
        return "中"
    return "低"


def normalize_concept(raw: dict, *, date_: date_type, members: list[str] | None = None) -> dict:
    rec = {}
    for k, v in raw.items():
        rec[COLUMN_MAP.get(str(k).strip(), str(k).strip())] = v
    title = str(rec.get("title") or "").strip()
    change_pct = _to_float(rec.get("change_pct"))
    codes = [str(c) for c in (members or []) if c]
    return {
        "date": date_,
        "title": title[:128],
        "heat": grade_heat(change_pct),
        "industry_chain": title,  # 概念名占位（LLM 细粒度产业链后续扩展）
        "related_codes": codes[:MAX_CODES],
    }


class AkshareConceptCollector(BaseCollector):
    """东财概念板块热点（主源）。"""

    data_type = "hot_topic"

    def fetch(self, **params) -> list[dict]:
        rows, members = self._fetch_raw(**params)
        date_ = params.get("date") or date_type.today()
        top_n = int(params.get("top_n", 50))
        # 热度优先：按涨跌幅绝对值取 Top N，控制成分请求量（§2.3.3 东财限流）
        rows = sorted(
            rows,
            key=lambda r: abs(_to_float(r.get("涨跌幅")) or 0),
            reverse=True,
        )[:top_n]
        out = []
        for r in rows:
            name = str(r.get("概念名") or r.get("名称") or r.get("板块名称") or "").strip()
            out.append(normalize_concept(r, date_=date_, members=members.get(name, [])))
        return [r for r in out if r["title"]]

    def _fetch_raw(self, **params) -> tuple[list[dict], dict[str, list[str]]]:
        import akshare as ak

        name_df = ak.stock_board_concept_name_em()
        rows = name_df.to_dict("records")
        members: dict[str, list[str]] = {}
        for name in name_df["板块名称"].tolist():
            try:
                cons = ak.stock_board_concept_cons_em(symbol=name)
                members[name] = [str(c) for c in cons["代码"].tolist()]
            except Exception:
                continue  # 单概念失败不阻塞（采集链兜底）
        return rows, members
