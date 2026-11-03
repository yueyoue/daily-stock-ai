"""内容类表（开发文档 §6）：报告、热点、资讯、观察池。"""
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Report(Base):
    """AI 报告：早报 / 复盘（§3.2）。"""

    __tablename__ = "report"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(16), index=True)  # morning / afternoon
    report_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    md_content: Mapped[str] = mapped_column(Text, default="")
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, default=1)  # 重新生成版本号


class HotTopic(Base):
    """热点题材库（§3.2）：产业链标签 + 关联个股。"""

    __tablename__ = "hot_topic"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(128))
    heat: Mapped[str] = mapped_column(String(16), default="中")  # 高/中/低
    industry_chain: Mapped[str] = mapped_column(String(256), default="")
    related_codes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class News(Base):
    """过滤后资讯（§4模块6）：影响等级 + 去重哈希。"""

    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="")
    impact_level: Mapped[str] = mapped_column(String(8), default="")  # 高/中/低/未分级
    url: Mapped[str] = mapped_column(String(512), default="")
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # 去重键


class WatchList(Base):
    """掘金观察池（§4模块7）：一期单用户，无用户字段。"""

    __tablename__ = "watch_list"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(12), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    dimension: Mapped[str] = mapped_column(String(64), default="")  # 关注维度/概念
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
