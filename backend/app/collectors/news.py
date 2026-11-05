"""资讯采集（T6.1，开发文档 §2.4.3 / §4模块6）：财联社电报主源 + 同花顺快讯备用。

- 标准化记录：{title, content, published_at, source, url, impact_level, dedup_hash}
- 去重键：标题归一 + 分钟级时间 归一后哈希（同源重拉/跨批次重叠不重复入库）
- QPS≤0.5（接口配置登记约束）；真机联调点：akshare 接口名以实测为准（T6.x 演练验证）
"""
import hashlib
import re
from datetime import datetime

from collectors.base import BaseCollector

# 中文/异名列名 → 标准字段
COLUMN_MAP = {
    "标题": "title", "title": "title",
    "内容": "content", "正文": "content", "content": "content",
    "发布时间": "published_at", "publish_time": "published_at",
    "ctime": "published_at", "time": "published_at", "date": "published_at",
    "链接": "url", "url": "url",
}

_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d")


def normalize_title(title: str) -> str:
    """标题归一：去首尾空白、合并连续空白、全角空格转半角。"""
    return re.sub(r"\s+", " ", str(title or "").replace("\u3000", " ")).strip()


def parse_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:  # ISO 兜底
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def dedup_hash(title: str, published_at: datetime | None) -> str:
    """去重键：归一标题 + 分钟级时间。"""
    minute = published_at.strftime("%Y-%m-%d %H:%M") if published_at else ""
    raw = f"{normalize_title(title)}|{minute}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_news(raw: dict, source: str) -> dict | None:
    """列名归一 → 标准资讯记录；无有效标题返回 None（由采集侧跳过）。"""
    rec = {}
    for k, v in raw.items():
        rec[COLUMN_MAP.get(str(k).strip().lower(), COLUMN_MAP.get(str(k).strip(), str(k).strip()))] = v
    title = normalize_title(rec.get("title"))
    if not title:
        return None
    published_at = parse_datetime(rec.get("published_at"))
    return {
        "title": title[:256],
        "content": str(rec.get("content") or ""),
        "published_at": published_at,
        "source": source,
        "url": str(rec.get("url") or "")[:512],
        "impact_level": "",  # T6.2 LLM 分级
        "dedup_hash": dedup_hash(title, published_at),
    }


def normalize_batch(rows: list[dict], source: str) -> list[dict]:
    out = []
    for raw in rows:
        item = normalize_news(raw, source)
        if item is not None:
            out.append(item)
    return out


class ClsNewsCollector(BaseCollector):
    """财联社电报（主源）。"""

    data_type = "news"

    def fetch(self, **params) -> list[dict]:
        rows = self._fetch_raw(**params)
        return normalize_batch(rows, source="财联社电报")

    def _fetch_raw(self, **params) -> list[dict]:
        import akshare as ak

        df = ak.stock_telegraph_cls()
        return df.to_dict("records")


class ThsNewsCollector(BaseCollector):
    """同花顺快讯（备用）。"""

    data_type = "news"

    def fetch(self, **params) -> list[dict]:
        rows = self._fetch_raw(**params)
        return normalize_batch(rows, source="同花顺快讯")

    def _fetch_raw(self, **params) -> list[dict]:
        import akshare as ak

        df = ak.stock_info_global_ths()
        return df.to_dict("records")
