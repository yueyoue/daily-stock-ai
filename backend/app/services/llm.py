"""LLM 探测（T4.9 系统配置页"连通测试"）。

最小实现：用配置的模型 + Key 发一次极小 completion 验证连通；
T7.1 将在此扩展为完整 LLM 网关（多模型切换/超时重试/用量记录）。
安全：Key 只进不出——错误信息做脱敏替换，任何响应不含明文。
"""
import logging

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.config_store import get_effective

logger = logging.getLogger("app.llm")


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "****") if secret else text


def probe_llm(db: Session) -> dict:
    """连通性测试：最小 completion 调用。失败返回 {ok: False, error}（不抛，便于前端展示）。"""
    key = get_effective(db, "llm_api_key")
    model = get_effective(db, "llm_model")
    if not key:
        raise AppError(400, "未配置 LLM API Key（请先在系统设置保存）")
    try:
        from litellm import completion  # 延迟导入：数据栈依赖只在镜像内安装
    except ImportError:
        raise AppError(503, "LLM 依赖未安装（生产镜像内含 litellm）")
    try:
        completion(
            model=model,
            api_key=key,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=15,
        )
        return {"ok": True, "model": model}
    except Exception as e:  # noqa: BLE001 供应商异常类型不可枚举
        logger.warning("LLM 连通测试失败: %s", _scrub(str(e)[:200], key))
        return {"ok": False, "model": model, "error": _scrub(str(e)[:300], key)}
