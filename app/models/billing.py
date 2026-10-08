"""计费域与账号安全域：订阅、账单、用量、账务调整、幂等键、登录事件、安全事件。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TraceMixin


# ===========================================================================
# 计费域
# ===========================================================================
class Subscription(Base, TenantMixin):
    __tablename__ = "subscriptions"

    """订阅信息（技能 4 风控输入之一）。"""

    __table_args__ = (UniqueConstraint("tenant_id", "account_id", name="uq_sub_tenant_acct"),)

    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="team", nullable=False)
    seats: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tenure_months: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    user_tier: Mapped[str] = mapped_column(String(32), default="basic", nullable=False)
    open_complaints: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Invoice(Base, TenantMixin, TraceMixin):
    __tablename__ = "invoices"

    """账单。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_no", name="uq_invoice_tenant_no"),
        Index("ix_invoices_tenant_acct_period", "tenant_id", "account_id", "period"),
    )

    invoice_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # YYYY-MM
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="unpaid", nullable=False)
    lines_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    disputed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dispute_reason: Mapped[str] = mapped_column(String(512), default="", nullable=False)


class UsageRecord(Base, TenantMixin):
    __tablename__ = "usage_records"

    """用量记录。"""

    __table_args__ = (Index("ix_usage_tenant_acct_period", "tenant_id", "account_id", "period"),)

    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    api_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_quota: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    storage_gb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class BillingAdjustment(Base, TenantMixin, TraceMixin):
    __tablename__ = "billing_adjustments"

    """账务调整（退款 / 抵扣）。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "adjustment_no", name="uq_adj_tenant_no"),
        Index("ix_adj_tenant_idem", "tenant_id", "idempotency_key"),
    )

    adjustment_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invoice_no: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="refund", nullable=False)
    reason: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    operator: Mapped[str] = mapped_column(String(64), default="ai", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 幂等与防重放要素
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", nullable=False, index=True)
    request_signature: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    request_timestamp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    approval_no: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class IdempotencyKey(Base, TenantMixin):
    __tablename__ = "idempotency_keys"

    """幂等键登记表。

    与 Redis 的短期去重不同，这里做**持久化**记录：即使 Redis 重启或过期，
    也能通过该表判断某个幂等键是否已经成功执行过，从而保证"同 key 不重复退款"。
    """

    __table_args__ = (
        UniqueConstraint("tenant_id", "idem_key", "scope", name="uq_idem_tenant_key_scope"),
    )

    idem_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)  # 如 billing.adjustment
    status: Mapped[str] = mapped_column(String(24), default="in_progress", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resource_no: Mapped[str] = mapped_column(String(64), default="", nullable=False)


# ===========================================================================
# 账号安全域
# ===========================================================================
class LoginEvent(Base, TenantMixin, TraceMixin):
    __tablename__ = "login_events"

    """登录事件（技能 5 异常检测输入）。"""

    __table_args__ = (
        Index("ix_logins_tenant_user_at", "tenant_id", "user_ext_id", "occurred_at"),
    )

    user_ext_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    geo: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    device: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    result: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    mfa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    risk_flags_json: Mapped[str] = mapped_column(Text, default="", nullable=False)


class SecurityIncident(Base, TenantMixin, TraceMixin):
    __tablename__ = "security_incidents"

    """安全事件处置记录。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "incident_no", name="uq_incident_tenant_no"),
        Index("ix_incidents_tenant_user", "tenant_id", "user_ext_id"),
    )

    incident_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_ext_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    signals_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    action: Mapped[str] = mapped_column(String(48), default="", nullable=False)  # lock/notify/escalate/none
    action_detail: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
