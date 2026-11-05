"""东财研报（T6.4 备源，开发文档 §2.3.3 研报不能单点）。

标准化记录：{title, org, rating, published_at, code}
真机联调点：ak.stock_research_report_em（T1.11/部署后演练验证）
"""
from app.collectors.base import BaseCollector

COLUMN_MAP = {
    "报告名称": "title", "标题": "title", "title": "title",
    "机构": "org", "研究机构": "org",
    "评级": "rating", "投资评级": "rating",
    "发布日期": "published_at", "日期": "published_at",
    "股票代码": "code", "代码": "code",
}


def normalize_report(raw: dict) -> dict | None:
    rec = {}
    for k, v in raw.items():
        rec[COLUMN_MAP.get(str(k).strip(), str(k).strip())] = v
    title = str(rec.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title[:256],
        "org": str(rec.get("org") or "")[:64],
        "rating": str(rec.get("rating") or "")[:32],
        "published_at": str(rec.get("published_at") or "")[:32],
        "code": str(rec.get("code") or "")[:12],
    }


class AkshareReportCollector(BaseCollector):
    """东财研报（备用线）。"""

    data_type = "research_report"

    def fetch(self, **params) -> list[dict]:
        rows = self._fetch_raw(**params)
        return [r for r in (normalize_report(row) for row in rows) if r is not None]

    def _fetch_raw(self, **params) -> list[dict]:
        import akshare as ak

        code = str(params.get("code") or params.get("symbol") or "").strip()
        if not code:
            raise RuntimeError("研报采集需要 code 参数（标的代码）")
        df = ak.stock_research_report_em(symbol=code)
        return df.to_dict("records")
