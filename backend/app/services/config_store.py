"""系统配置存取（T3.5，开发文档 §5.4 / §7）。

双层语义：Settings（.env）为默认值，system_config 表为运行时覆盖。
LLM Key 只进不出：保存写库，读取一律掩码（尾号 4 位），响应不含明文字段。
"""
import re

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.config import SystemConfig

_CRON_RE = re.compile(r"^[\d\*\,\-\ /]+$")

# 配置项规格（类型 / 默认值 / 约束）；默认值会被同名 Settings 环境变量覆盖
CONFIG_SPEC: dict[str, dict] = {
    "tech_indicator_store": {"type": bool, "default": False},
    "indicator_cache_days": {"type": int, "default": 3, "min": 1, "max": 30},
    "indicator_engine": {"type": str, "default": "talib", "choices": ["talib", "pandas"]},
    "report_am_cron": {"type": str, "default": "0 8 * * 1-5", "cron": True},
    "report_pm_cron": {"type": str, "default": "0 17 * * 1-5", "cron": True},
    "llm_model": {"type": str, "default": "qwen-plus"},
    "llm_api_key": {"type": str, "default": "", "secret": True},
}


def mask_secret(value: str | None) -> str:
    """掩码：只保留尾号 4 位。"""
    if not value:
        return ""
    return "****" + str(value)[-4:]


def _default(key: str):
    env_val = getattr(get_settings(), key, None)
    return env_val if env_val is not None else CONFIG_SPEC[key]["default"]


def _validate(key: str, value):
    spec = CONFIG_SPEC[key]
    if spec["type"] is bool and not isinstance(value, bool):
        raise AppError(400, f"{key} 必须是布尔值")
    if spec["type"] is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise AppError(400, f"{key} 必须是整数")
        if "min" in spec and not spec["min"] <= value <= spec["max"]:
            raise AppError(400, f"{key} 需在 {spec['min']}~{spec['max']} 之间")
    if spec["type"] is str:
        if not isinstance(value, str):
            raise AppError(400, f"{key} 必须是字符串")
        if "choices" in spec and value not in spec["choices"]:
            raise AppError(400, f"{key} 仅支持: {spec['choices']}")
        if spec.get("cron") and (not value or not _CRON_RE.match(value)):
            raise AppError(400, f"{key} 不是合法的 cron 表达式")
    return value


def get_override(db: Session, key: str):
    """取运行时覆盖原始值（无覆盖返回 None）。供业务代码做"配置生效"判断。"""
    row = db.get(SystemConfig, key)
    return None if row is None else row.value


def get_config(db: Session) -> dict:
    """配置视图：默认值 + 运行时覆盖；LLM Key 以掩码字段返回（无明文字段）。"""
    overrides = {r.key: r.value for r in db.query(SystemConfig).all()}
    view: dict = {}
    for key, spec in CONFIG_SPEC.items():
        value = overrides.get(key, _default(key))
        if spec.get("secret"):
            view[f"{key}_masked"] = mask_secret(value)  # 只出掩码，不出原字段名
        else:
            view[key] = value
    return view


def save_config(db: Session, **fields) -> dict:
    """保存配置（未知项报 400；llm_api_key 为空表示不修改）。"""
    unknown = set(fields) - set(CONFIG_SPEC)
    if unknown:
        raise AppError(400, f"未知配置项: {sorted(unknown)}")
    for key, value in fields.items():
        if CONFIG_SPEC[key].get("secret") and not value:
            continue  # 空值 = 不修改（防误清 Key）
        _validate(key, value)
        row = db.get(SystemConfig, key)
        if row is None:
            db.add(SystemConfig(key=key, value=value))
        else:
            row.value = value
    db.commit()
    return get_config(db)
