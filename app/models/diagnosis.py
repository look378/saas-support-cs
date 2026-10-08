"""诊断域：诊断结论、诊断步骤、工具调用记录。

这三个表是"一份数据四处复用"的载体：
  1. 转人工交接快照
  2. 指标看板统计
  3. trace_id 链路回放
  4. 集成测试的断言依据（期望诊断 / 期望动作 / 期望验证结果）
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TraceMixin

# 五类技能标识
SKILL_FAQ = "faq"
SKILL_INTEGRATION = "integration"
SKILL_PERMISSION = "permission"
SKILL_BILLING = "billing"
SKILL_ACCOUNT = "account"

ALL_SKILLS = (SKILL_FAQ, SKILL_INTEGRATION, SKILL_PERMISSION, SKILL_BILLING, SKILL_ACCOUNT)


class Diagnosis(Base, TenantMixin, TraceMixin):
    __tablename__ = "diagnoses"

    """一次诊断的结论。"""

    __table_args__ = (
        Index("ix_diagnoses_tenant_skill", "tenant_id", "skill"),
        Index("ix_diagnoses_tenant_cause", "tenant_id", "cause_code"),
    )

    session_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    skill: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 诊断结论编码，如 TOKEN_EXPIRED / WEBHOOK_INVALID / SHARE_NOT_ENABLED
    cause_code: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    cause_text: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 决策：自动修复 / 多方案待选 / 自动调整 / 转人工 / 锁定
    decision: Mapped[str] = mapped_column(String(48), default="", nullable=False, index=True)
    decision_reason: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # 诊断依据（证据列表，JSON 字符串）
    evidence_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 最终结果
    outcome: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # 命中的案例（RAG 参考依据，便于评估检索质量）
    matched_cases: Mapped[str] = mapped_column(String(255), default="", nullable=False)


class DiagnosisStep(Base, TenantMixin, TraceMixin):
    __tablename__ = "diagnosis_steps"

    """诊断链上的单个节点执行记录（用于链路回放与耗时瀑布）。"""

    __table_args__ = (
        Index("ix_dsteps_tenant_diag", "tenant_id", "diagnosis_id", "seq"),
    )

    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    detail: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entered_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ToolInvocation(Base, TenantMixin, TraceMixin):
    __tablename__ = "tool_invocations"

    """工具调用记录（含 Mock 后端交互的真实请求与响应摘要）。"""

    __table_args__ = (
        Index("ix_tools_tenant_name", "tenant_id", "tool_name"),
        Index("ix_tools_tenant_trace", "tenant_id", "trace_id"),
    )

    diagnosis_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 出站请求信息（不记录敏感字段）
    target: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    request_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok: Mapped[bool] = mapped_column(default=True, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
