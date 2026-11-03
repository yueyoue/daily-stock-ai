"""采集器共享工具：列名兼容取值、DataFrame 转 records、日期归一。

各数据源 DataFrame 列名不统一（中文/英文/别名），统一走 pick 多别名兜底；
不依赖 pandas 类型（测试可注入假 DataFrame）。
"""
from datetime import date, datetime


def pick(row: dict, keys: tuple) -> str:
    """按别名顺序取第一个非空值（字符串化去空白）。"""
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def to_records(df) -> list[dict]:
    """DataFrame → list[dict]（只用 to_dict("records")，不依赖 pandas 类型）。"""
    return [dict(r) for r in df.to_dict("records")]


def to_records_indexed(df) -> list[dict]:
    """同 to_records，但日期等信息在**索引**上时先 reset_index（新浪/乐咕常见布局）。

    pandas to_dict("records") 会静默丢索引（无列时甚至返回空列表）；
    首行缺日期列或为空时重取一次。
    """
    rows = to_records(df)
    if hasattr(df, "reset_index") and (
        not rows or not any(k in rows[0] for k in ("date", "日期", "datetime", "REPORT_DATE"))
    ):
        reset_rows = to_records(df.reset_index())
        if reset_rows:
            rows = reset_rows
    return rows


def to_date(value) -> date | None:
    """日期归一：date/datetime/字符串（YYYY-MM-DD、YYYYMMDD、含时间）→ date。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    head = s[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    try:  # 8 位紧凑日期
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None
