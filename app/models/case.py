"""案例与知识域、可观测域。

案例库同时承担三重角色（决策 ADR-005）：
  1. 向量检索的语料（RAG 参考依据）
  2. 集成测试的断言依据（期望诊断 / 期望动作 / 期望验证结果）
  3. 人工办结回流的知识沉淀（ADR-006 闭环）
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TraceMixin


class Case(Base):
    """案例库条目。

    **本表不按租户隔离**：案例是与租户无关的**共享参考知识**
    （同一类故障对任何租户都成立）。若强行按租户各存一份，会同时产生两个问题：
    案例 ID 全局唯一约束冲突；以及新租户没有案例可用（冷启动无知识）。
    租户间的差异体现在 ``tenant_profile`` 字段上，而不是拆表。
    """

    __tablename__ = "cases"
    # 注意：列上写 index=True 与在 __table_args__ 里显式 Index 定义**不能重复**，
    # 否则会生成两个同名索引，建表时报 DuplicateTable（本项目实测踩到）。
    # 这里统一用显式 Index，列上不再写 index=True。
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_case_id"),
        Index("ix_cases_skill", "skill"),
    )

    case_id: Mapped[str] = mapped_column(String(48), nullable=False)
    tenant_profile: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    skill: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario: Mapped[str] = mapped_column(String(255), nullable=False)
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    # --- 断言三列：集成测试据此判定"诊断是否正确、动作是否符合预期" ---
    expected_diagnosis: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    expected_action: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    expected_verify_result: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # --- 回流标记：由人工办结沉淀而来 ---
    promoted_from_escalation: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CaseFeedback(Base, TenantMixin, TraceMixin):
    """人工办结回流记录（ADR-006）。

    坐席办结时填写诊断结论与修复动作，经提升后写入案例库，
    使"人工解决过的问题"成为 AI 的后续知识。

    索引说明：``promoted`` 已在列上 index=True，不再在 __table_args__ 里重复定义，
    否则会生成同名索引导致建表失败。
    """

    __tablename__ = "case_feedbacks"

    escalation_no: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    diagnosis_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    fix_action: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    automatable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)


# ===========================================================================
# 可观测域
# ===========================================================================
class LlmCall(Base, TenantMixin, TraceMixin):
    __tablename__ = "llm_calls"

    """大模型调用计量（成本与性能统计口径）。"""

    __table_args__ = (Index("ix_llm_tenant_kind", "tenant_id", "kind"),)

    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48), default="chat", nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    served_model: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_kind: Mapped[str] = mapped_column(String(48), default="", nullable=False)


class AiTaskLog(Base, TenantMixin, TraceMixin):
    __tablename__ = "ai_task_logs"

    """链路回放数据源：图节点级执行记录。"""

    __table_args__ = (
        Index("ix_aitask_tenant_trace", "tenant_id", "trace_id", "id"),
        Index("ix_aitask_tenant_session", "tenant_id", "session_id", "id"),
    )

    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    diagnosis_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    node: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    skill: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AgentMetric(Base, TenantMixin):
    __tablename__ = "agent_metrics"

    """坐席维度指标（接管量 / 平均处理时长）。"""

    __table_args__ = (Index("ix_agentmetric_tenant_agent", "tenant_id", "agent_name"),)

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    claimed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_handle_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avg_handle_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    online_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
