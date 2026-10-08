"""ORM 模型统一入口。

导入本模块即可获得全部模型，并向数据库层注册"租户表名单"
（用于执行钩子的租户上下文强校验）。
"""

from __future__ import annotations

from app.core.database import register_tenant_tables
from app.models.base import Base, TenantMixin, TraceMixin, new_id, utcnow
from app.models.billing import (
    BillingAdjustment,
    IdempotencyKey,
    Invoice,
    LoginEvent,
    SecurityIncident,
    Subscription,
    UsageRecord,
)
from app.models.case import AgentMetric, AiTaskLog, Case, CaseFeedback, LlmCall
from app.models.conversation import (
    SESSION_STATE_AGENT,
    SESSION_STATE_AI,
    SESSION_STATE_CLOSED,
    SESSION_STATE_QUEUED,
    Escalation,
    Message,
    SessionEvent,
    SupportSession,
    Ticket,
    TicketMessage,
)
from app.models.diagnosis import (
    ALL_SKILLS,
    SKILL_ACCOUNT,
    SKILL_BILLING,
    SKILL_FAQ,
    SKILL_INTEGRATION,
    SKILL_PERMISSION,
    Diagnosis,
    DiagnosisStep,
    ToolInvocation,
)
from app.models.identity import Agent, AgentSkill, EndUser, Tenant

__all__ = [
    "Base",
    "TenantMixin",
    "TraceMixin",
    "new_id",
    "utcnow",
    # identity
    "Tenant",
    "EndUser",
    "Agent",
    "AgentSkill",
    # conversation
    "SupportSession",
    "Message",
    "SessionEvent",
    "Ticket",
    "TicketMessage",
    "Escalation",
    "SESSION_STATE_AI",
    "SESSION_STATE_QUEUED",
    "SESSION_STATE_AGENT",
    "SESSION_STATE_CLOSED",
    # diagnosis
    "Diagnosis",
    "DiagnosisStep",
    "ToolInvocation",
    "SKILL_FAQ",
    "SKILL_INTEGRATION",
    "SKILL_PERMISSION",
    "SKILL_BILLING",
    "SKILL_ACCOUNT",
    "ALL_SKILLS",
    # billing + security
    "Subscription",
    "Invoice",
    "UsageRecord",
    "BillingAdjustment",
    "IdempotencyKey",
    "LoginEvent",
    "SecurityIncident",
    # case + observability
    "Case",
    "CaseFeedback",
    "LlmCall",
    "AiTaskLog",
    "AgentMetric",
]


def tenant_scoped_tables() -> set[str]:
    """所有带 tenant_id 的业务表名（供执行钩子强校验使用）。"""
    names: set[str] = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is not None and "tenant_id" in table.columns:
            names.add(table.name)
    return names


# 模块导入即完成注册，避免遗漏
register_tenant_tables(tenant_scoped_tables())
