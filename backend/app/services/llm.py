"""LLM 网关（T7.1，开发文档 §2.2/§5.4）+ 连通探测（T4.9）。

- complete：统一 LLM 调用——模型/Key 走配置覆盖层（config_store），3 次指数退避重试，
  用量记 task_log，错误信息脱敏（Key 永不出现在日志/响应）
- 多模型切换：系统设置页 llm_model 直接生效；模型名按 LiteLLM 约定传入
  （如 "openai/qwen-plus"、"kimi/..."，供应商前缀由用户配置）
- probe_llm：最小 completion 验证连通（配置页"连通测试"按钮）
"""
import logging
import time

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.services.config_store import get_effective
from app.services.pipeline import log_stage

logger = logging.getLogger("app.llm")

RETRIES = 3
BACKOFF_BASE_SEC = 1.0


def _scrub(text: str, secret: str) -> str:
    """脱敏：Key 明文一律替换为掩码。"""
    return text.replace(secret, "****") if secret else text


def _load_completion():
    try:
        from litellm import completion  # 延迟导入：数据栈依赖只在镜像内安装
    except ImportError:
        raise AppError(503, "LLM 依赖未安装（生产镜像内含 litellm）")
    return completion


def complete(
    db: Session,
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.4,
    retries: int = RETRIES,
    backoff_base: float = BACKOFF_BASE_SEC,
    sleep=time.sleep,
) -> str:
    """统一 LLM 调用，返回文本。失败自动重试，全败抛 502（信息脱敏）。"""
    key = get_effective(db, "llm_api_key")
    model = get_effective(db, "llm_model")
    if not key:
        raise AppError(503, "未配置 LLM API Key（请先在系统设置保存）")
    completion = _load_completion()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        t0 = time.monotonic()
        try:
            resp = completion(
                model=model,
                api_key=key,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=60,
            )
            content = ""
            usage = None
            if isinstance(resp, dict):
                content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                usage = resp.get("usage")
            else:
                content = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
            log_stage(
                db, "LLM调用", "success",
                message=(f"model={model} attempt={attempt} "
                         f"prompt_chars={len(prompt)} completion_chars={len(content)} "
                         f"usage={usage} duration_ms={int((time.monotonic()-t0)*1000)}"),
            )
            return content
        except Exception as e:  # noqa: BLE001 供应商异常类型不可枚举
            last_err = e
            logger.warning("LLM 调用失败 第%d/%d次: %s", attempt, retries,
                           _scrub(str(e)[:200], key))
            if attempt < retries:
                sleep(backoff_base * (2 ** (attempt - 1)))

    safe_err = _scrub(str(last_err)[:200], key)
    log_stage(db, "LLM调用", "failed", message=f"model={model} {retries}次均失败: {safe_err}")
    raise AppError(502, f"LLM 调用失败（已重试 {retries} 次）: {safe_err}")


def probe_llm(db: Session) -> dict:
    """连通性测试：最小 completion 调用。失败返回 {ok: False, error}（不抛，便于前端展示）。"""
    key = get_effective(db, "llm_api_key")
    model = get_effective(db, "llm_model")
    if not key:
        raise AppError(400, "未配置 LLM API Key（请先在系统设置保存）")
    completion = _load_completion()
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
