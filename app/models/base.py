"""ORM 基类与通用列约定。

硬约定（架构文档 §6）：
- **所有业务表必须含 `tenant_id`**，非空且带索引。
- 所有表含 `id / created_at / updated_at`；审计类额外含 `trace_id`。
- 表名统一小写复数；模型导入时向数据库层注册"租户表名单"，
  由执行钩子强制校验租户上下文。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


def utcnow() -> datetime:
    """当前时间（带时区）。"""
    return datetime.now(timezone.utc)


def new_id(prefix: str = "") -> str:
    """生成业务 ID。"""
    raw = uuid.uuid4().hex
    return f"{prefix}{raw[:8]}{raw[8:16]}" if prefix else raw


class Base(DeclarativeBase):
    """声明式基类。

    表名策略：**每个模型必须显式声明 `__tablename__`**。
    不使用自动复数化推导——它会把 ``Diagnosis`` 变成 ``diagnosiss`` 之类，
    进而让外键找不到目标表（Spike 阶段实际踩到）。
    """

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典（JSON 友好）。"""
        out: dict[str, Any] = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[column.name] = value
        return out


class TenantMixin:
    """租户表混入：强制 `tenant_id` 列 + 索引。

    必须用 ``@declared_attr`` 定义：普通 ``mapped_column`` 会让所有使用该混入的
    模型**共用同一个 Column 对象**，于是各自自动生成的索引会重名，
    建表时报 ``DuplicateTable: relation "ix_xxx" already exists``（本项目实测踩到）。
    用 ``declared_attr`` 后每个模型得到独立的列与索引。
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:
        return mapped_column(String(64), nullable=False, index=True)


class TraceMixin:
    """链路追踪混入：记录产生该行的 trace_id。

    同样必须用 ``declared_attr``，原因与 ``TenantMixin`` 相同。
    """

    @declared_attr
    def trace_id(cls) -> Mapped[str]:
        return mapped_column(String(64), nullable=False, default="", index=True)


