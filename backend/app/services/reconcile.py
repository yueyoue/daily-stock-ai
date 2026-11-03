"""交叉对账（开发文档 §2.3.4）：多源值比对，不一致时以对账结果为准。

规则：
1. 多数一致优先：任一取值占比 > 50% → 取该值
2. 无多数（如两源分歧）→ 以主源为准
3. 差异写入 data_reconcile（values 全量留痕）
"""
import logging
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.models.system import DataReconcile

logger = logging.getLogger("app.reconcile")


def _key(value: Any) -> str:
    return repr(value)


def reconcile(
    db: Session,
    data_type: str,
    data_key: str,
    values: dict[str, Any],
    primary: str,
) -> Any:
    """对账并留痕。values: {源名: 值}；primary: 主源名。返回对账结果值。"""
    if not values:
        raise ValueError("values 不能为空")

    counts = Counter(_key(v) for v in values.values())
    top_key, top_n = counts.most_common(1)[0]
    if top_n * 2 > len(values):
        result_key = top_key
    else:
        result_key = _key(values.get(primary)) if primary in values else top_key

    result_value = next(v for v in values.values() if _key(v) == result_key)
    is_consistent = len(counts) == 1

    db.add(
        DataReconcile(
            data_type=data_type,
            data_key=data_key,
            values={str(k): str(v) for k, v in values.items()},
            result_value=str(result_value)[:256],
            is_consistent=is_consistent,
        )
    )
    db.commit()

    if not is_consistent:
        logger.warning("对账不一致 %s %s: %s → 取 %s", data_type, data_key, values, result_value)
    return result_value
