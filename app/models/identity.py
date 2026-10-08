"""租户与身份域：租户、终端用户、坐席、坐席技能。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class Tenant(Base):
    __tablename__ = "tenants"

    """租户（SaaS 客户企业）。

    注意：租户表自身以 ``id`` 字符串作为租户标识，不继承 TenantMixin
    （它本身就是租户维度的定义者）。
    """

    tenant_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)  # standard/vip
    plan: Mapped[str] = mapped_column(String(32), default="team", nullable=False)
    seats: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 风控相关：租户级投诉累计、是否允许自动退款
    complaint_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    auto_refund_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(String(512), default="", nullable=False)


class EndUser(Base, TenantMixin):
    __tablename__ = "end_users"

    """终端用户（使用 SaaS 产品的人）。"""

    __table_args__ = (UniqueConstraint("tenant_id", "external_id", name="uq_user_tenant_ext"),)

    external_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    user_tier: Mapped[str] = mapped_column(String(32), default="basic", nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    # 订阅与风控画像
    tenure_months: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    complaint_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    account_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Agent(Base, TenantMixin):
    __tablename__ = "agents"

    """坐席账号。"""

    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_agent_tenant_user"),)

    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="agent", nullable=False)  # agent/lead
    # 在线状态由心跳驱动，这里存最后落库值便于审计（实时状态在 Redis）
    presence: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    # 负载与绩效
    current_load: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    handled_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_handle_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AgentSkill(Base, TenantMixin):
    __tablename__ = "agent_skills"

    """坐席技能组标签（派单匹配用）。

    技能标签取值：faq / integration / permission / billing / account
    """

    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", "skill", name="uq_agentskill"),)

    agent_id: Mapped[int] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    proficiency: Mapped[int] = mapped_column(Integer, default=3, nullable=False)  # 1-5
