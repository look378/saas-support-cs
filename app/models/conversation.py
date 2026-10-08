"""会话域：会话、消息、会话事件、工单、工单消息、转人工记录。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TraceMixin

# 会话状态机取值（与 Redis 中的状态保持一致）
SESSION_STATE_AI = "AI_HANDLING"
SESSION_STATE_QUEUED = "QUEUED"
SESSION_STATE_AGENT = "AGENT_HANDLING"
SESSION_STATE_CLOSED = "CLOSED"


class SupportSession(Base, TenantMixin, TraceMixin):
    __tablename__ = "support_sessions"

    """会话。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "session_key", name="uq_session_tenant_key"),
        Index("ix_sessions_tenant_state", "tenant_id", "state"),
    )

    session_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_ext_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    channel: Mapped[str] = mapped_column(String(32), default="web_widget", nullable=False)

    state: Mapped[str] = mapped_column(String(32), default=SESSION_STATE_AI, nullable=False)
    # 当前处理方：AI 或坐席用户名
    handler: Mapped[str] = mapped_column(String(64), default="AI", nullable=False)
    skill: Mapped[str] = mapped_column(String(32), default="", nullable=False, index=True)

    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 情绪与结果标记（指标看板口径）
    last_emotion: Mapped[str] = mapped_column(String(32), default="neutral", nullable=False)
    max_emotion_intensity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_by_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Message(Base, TenantMixin, TraceMixin):
    __tablename__ = "messages"

    """会话消息。"""

    __table_args__ = (Index("ix_messages_tenant_session", "tenant_id", "session_id", "id"),)

    session_id: Mapped[int] = mapped_column(
        ForeignKey("support_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user/ai/agent/system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 该条消息触发/携带的工具调用与诊断（JSON 字符串，便于回放）
    tool_calls_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    diagnosis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class SessionEvent(Base, TenantMixin, TraceMixin):
    __tablename__ = "session_events"

    """会话状态跃迁与关键事件（审计与回放）。

    ``session_id`` 已在列上 index=True；此处只定义复合索引，
    不重复覆盖同一列，避免同名索引冲突。
    """

    __table_args__ = (Index("ix_events_tenant_type", "tenant_id", "event_type"),)

    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_state: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    to_state: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Ticket(Base, TenantMixin, TraceMixin):
    __tablename__ = "tickets"

    """工单。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "ticket_no", name="uq_ticket_tenant_no"),
        Index("ix_tickets_tenant_status", "tenant_id", "status"),
    )

    ticket_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_ext_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)   # integration/permission/billing/account/faq
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 转人工交接快照（Markdown）
    handover_snapshot: Mapped[str] = mapped_column(Text, default="", nullable=False)
    assigned_agent: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    resolution: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handle_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class TicketMessage(Base, TenantMixin):
    __tablename__ = "ticket_messages"

    """工单内消息/备注。"""

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user/agent/system/ai
    content: Mapped[str] = mapped_column(Text, nullable=False)


class Escalation(Base, TenantMixin, TraceMixin):
    __tablename__ = "escalations"

    """转人工记录。"""

    __table_args__ = (
        UniqueConstraint("tenant_id", "request_no", name="uq_escalation_tenant_no"),
        Index("ix_escalations_tenant_status", "tenant_id", "status"),
    )

    request_no: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_ext_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 触发转人工时命中的技能。必须落库：
    # 派单需要按技能组匹配坐席，工作台也需要按技能统计转人工分布。
    # 早期只把它放在 Redis 队列消息里，导致落库记录缺该字段、
    # 工作台与推送事件里的技能为空（本项目实测踩到）。
    skill: Mapped[str] = mapped_column(String(32), default="", nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_code: Mapped[str] = mapped_column(String(48), default="", nullable=False, index=True)
    emotion: Mapped[str] = mapped_column(String(32), default="neutral", nullable=False)
    emotion_intensity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    priority: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    # 交接快照：已排查项 / 已执行操作 / 当前状态 / 建议话术
    snapshot_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    assigned_agent: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wait_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
