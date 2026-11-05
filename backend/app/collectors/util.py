"""采集器共享工具：列名兼容取值、DataFrame 转 records、日期归一。

各数据源 DataFrame 列名不统一（中文/英文/别名），统一走 pick 多别名兜底；
不依赖 pandas 类型（测试可注入假 DataFrame）。
"""
import math
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


# ---------- 数值安全（NaN/Inf 防护，缺陷 D-01 修复）----------
_NON_NUMBER = {"nan", "none", "null", "inf", "-inf", "+inf", "infinity", "-infinity"}


def is_bad_number(value) -> bool:
    """是否为不可用于计算/JSON 的数值：真实 NaN / ±Inf，或其字符串形式（"nan"/"inf" 等）。"""
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, str):
        return value.strip().lower() in _NON_NUMBER
    return False


def safe_float(value, default=None) -> float | None:
    """任意值 → float；空/非数值/NaN/±Inf 一律返回 default（默认 None）。

    与 float(pick(...) or 0) 的关键差异：DataFrame 里 pandas 的 NaN 不会在
    float() 时抛错，只会把 NaN 一路带进结果（写入 MySQL JSON 列即报 3140）。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return default if is_bad_number(value) else float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _NON_NUMBER:
        return default
    try:
        result = float(text)
    except ValueError:
        return default
    return default if (math.isnan(result) or math.isinf(result)) else result


def safe_int(value, default=None) -> int | None:
    """任意值 → int；不可转换返回 default。"""
    result = safe_float(value, default=None)
    if result is None:
        return default
    try:
        return int(result)
    except (ValueError, OverflowError):
        return default


def json_safe(value):
    """递归清洗为 JSON 可序列化值：dict/list 深拷贝，NaN/±Inf → None，其余转 str。"""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)