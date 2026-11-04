"""系统配置 KV 表（T3.5，开发文档 §5.4）。

运行时覆盖层：默认值在 .env/Settings，本表存用户在系统配置页的修改。
LLM Key 也存这里（只进不出，读取一律掩码）。
"""
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value = mapped_column(JSON, nullable=False)  # 标量/结构均可（JSON 存储）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
