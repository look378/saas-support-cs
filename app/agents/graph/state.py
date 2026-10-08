"""诊断状态图：共享骨架 + 五技能节点组。

设计（架构文档 §4）：五个技能**共用一套骨架**，只替换
``observe / diagnose / act / verify`` 四段的实现。骨架统一提供：

  - 技能路由（置信度不足或信息不全 → 澄清）
  - 澄清挂起与恢复（``interrupt()``，同一机制同时服务"追问"与"重启续接"）
  - 步数熔断（超过阈值转人工）
  - 风险/降级决策
  - 结论落库（供工单快照 / 指标看板 / 链路回放 / 测试断言 四处复用）

状态类型定义在**模块级**：条件边路由函数的注解会在模块全局作用域解析，
用局部别名会触发 NameError（约束 C-1）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from app.core.logging_config import get_logger
from app.core.trace_context import get_trace_id

log = get_logger(__name__)


# ===========================================================================
# 图状态
# ===========================================================================
class SupportState(TypedDict, total=False):
    """诊断流程状态（会持久化到 PostgreSQL，跨进程可续接）。"""

    # 标识
    tenant_id: str
    session_id: str
    user_ext_id: str
    skill: str

    # 输入
    user_message: str
    clarify_question: str
    user_reply: str

    # 抽取到的实体（工作区 / 文档 / 账号 / 用户）
    entities: dict[str, Any]

    # 取证结果
    facts: dict[str, Any]

    # 诊断结论
    cause_code: str
    cause_text: str
    confidence: float
    evidence: list[str]
    risk_score: float
    matched_cases: list[str]

    # 处置
    decision: str
    decision_reason: str
    actions: list[dict[str, Any]]
    options: list[dict[str, Any]]
    chosen_option: str

    # 验证
    verify_ok: bool
    verify_detail: str

    # 控制
    step_count: int
    escalate: bool
    escalate_reasons: list[str]
    degraded: bool
    degrade_reason: str
    reply: str
    outcome: str
    node_path: list[str]

    # 转人工交接快照（Markdown，随工单附给坐席）
    handover_snapshot: str
    # 诊断结论落库后的主键（供消息记录与链路回放关联）
    diagnosis_id: int
    # 会话数据库主键（图内节点写审计日志需要）
    session_pk: int
    # 待澄清的必填实体
    missing_entities: list[str]


# ===========================================================================
# 技能接口
# ===========================================================================
@dataclass
class SkillOutcome:
    """技能节点执行结果。

    字段必须**完整覆盖技能会返回的键**：若技能返回了未声明的键，
    ``SkillOutcome(...)`` 会直接抛 TypeError，并被图节点的异常兜底捕获，
    表现为"技能静默失败、结论丢失"（本项目实测踩到过）。
    因此这里把 ``outcome`` / ``chosen_option`` / ``matched_cases`` 都声明为显式字段。
    """

    facts: dict[str, Any] = field(default_factory=dict)
    cause_code: str = ""
    cause_text: str = ""
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    decision: str = ""
    decision_reason: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    chosen_option: str = ""
    verify_ok: bool = False
    verify_detail: str = ""
    reply: str = ""
    outcome: str = ""
    # 交接快照：转人工时随工单附给坐席。必须能写入状态，
    # 否则 finalize 生成的快照只存在于节点返回值里、不进状态，
    # 读取状态的调用方（API/工作台）就会拿到空快照（本项目实测踩到）。
    handover_snapshot: str = ""
    escalate: bool = False
    # 显式清除"需要转人工"标记。
    # 为什么需要它：流程早期可能因一次失败动作置上 escalate（例如首次刷新令牌
    # 返回"需重新授权"），随后回退重试成功。此时必须能**撤销**该标记，
    # 否则最终结论会错误地停留在 escalated，把一个已闭环的会话推给人工
    # （本项目实测踩到）。
    clear_escalation: bool = False
    escalate_reason: str = ""
    degraded: bool = False
    degrade_reason: str = ""
    matched_cases: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class Skill(Protocol):
    """技能契约。"""

    code: str          # faq / integration / permission / billing / account
    name: str          # 中文名，用于话术与派单技能组
    description: str   # 路由匹配用

    def extract(self, message: str, entities: dict[str, Any]) -> dict[str, Any]:
        """从消息中抽取实体，返回缺失的必填项列表（空列表表示信息足够）。"""

    def observe(self, state: SupportState) -> SkillOutcome:
        """取证：调用外部系统查询现状。"""

    def diagnose(self, state: SupportState) -> SkillOutcome:
        """诊断：给出结论、置信度、风险分。"""

    def act(self, state: SupportState) -> SkillOutcome:
        """处置：自动修复 / 生成方案 / 自动调整 / 锁定。"""

    def verify(self, state: SupportState) -> SkillOutcome:
        """验证：复核处置效果。"""


# ===========================================================================
# 通用工具
# ===========================================================================
def note_step(state: SupportState, node: str) -> dict[str, Any]:
    """记录节点轨迹与步数。"""
    return {
        "node_path": list(state.get("node_path", [])) + [node],
        "step_count": int(state.get("step_count", 0)) + 1,
    }


def merge(outcome: SkillOutcome) -> dict[str, Any]:
    """把技能结果合并为状态增量。

    规则：空值不覆盖已有值（避免后置节点把前置节点的结论清空）。
    这一点很关键：``verify`` 节点通常只关心验证结果，若它的
    ``outcome`` / ``handover_snapshot`` 为空并照常写入状态，
    就会把 ``finalize`` 已经生成好的结论与交接快照抹掉
    （本项目实测踩到：转人工快照变成空字符串）。
    因此这里对空值一律跳过，保留已有值。
    """
    delta: dict[str, Any] = {}
    mapping = {
        "facts": outcome.facts,
        "cause_code": outcome.cause_code,
        "cause_text": outcome.cause_text,
        "confidence": outcome.confidence,
        "evidence": outcome.evidence,
        "risk_score": outcome.risk_score,
        "decision": outcome.decision,
        "decision_reason": outcome.decision_reason,
        "actions": outcome.actions,
        "options": outcome.options,
        "chosen_option": outcome.chosen_option,
        "verify_ok": outcome.verify_ok,
        "verify_detail": outcome.verify_detail,
        "reply": outcome.reply,
        "outcome": outcome.outcome,
        "handover_snapshot": outcome.handover_snapshot,
        "matched_cases": outcome.matched_cases,
        "degraded": outcome.degraded,
        "degrade_reason": outcome.degrade_reason,
    }
    # 空值一律不写入：保留状态中已有的值
    for key, value in mapping.items():
        if value not in (None, "", 0.0, [], {}):
            delta[key] = value
    if outcome.escalate:
        delta["escalate"] = True
        reasons = list(delta.get("escalate_reasons", []))
        if outcome.escalate_reason:
            reasons.append(outcome.escalate_reason)
        delta["escalate_reasons"] = reasons
    elif outcome.clear_escalation:
        # 显式撤销转人工标记（例如回退重试后已成功闭环）
        delta["escalate"] = False
        delta["escalate_reasons"] = []
    if outcome.extra:
        delta.update(outcome.extra)
    return delta


def record_diagnosis_step(
    *,
    tenant_id: str,
    session_id: int | None,
    skill: str,
    node: str,
    status: str,
    detail: str,
    duration_ms: float,
    diagnosis_id: int | None = None,
    degraded: bool = False,
) -> None:
    """把节点执行写入 ``ai_task_logs``（链路回放数据源）。

    失败不影响主流程：可观测性不应让业务失败。
    """
    try:
        from app.core.database import write_session
        from app.models import AiTaskLog

        with write_session() as s:
            s.add(
                AiTaskLog(
                    tenant_id=tenant_id,
                    trace_id=get_trace_id(),
                    session_id=session_id,
                    diagnosis_id=diagnosis_id,
                    node=node,
                    skill=skill,
                    status=status,
                    duration_ms=round(duration_ms, 2),
                    detail=detail[:2000],
                    degraded=degraded,
                )
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("diagnosis.step_log_failed", node=node, error=str(exc)[:200])


def record_tool_calls(
    *, tenant_id: str, session_id: int | None, diagnosis_id: int | None, client: Any
) -> None:
    """把本次技能调用产生的工具调用记录落库（供回放与指标统计）。"""
    if client is None:
        return
    calls = getattr(client, "calls", [])
    if not calls:
        return
    try:
        from app.core.database import write_session
        from app.models import ToolInvocation

        with write_session() as s:
            for c in calls:
                s.add(
                    ToolInvocation(
                        tenant_id=tenant_id,
                        trace_id=get_trace_id(),
                        session_id=session_id,
                        diagnosis_id=diagnosis_id,
                        tool_name=c.tool_name,
                        target=c.target,
                        request_json=json.dumps(c.request, ensure_ascii=False)[:4000],
                        response_json=json.dumps(c.response, ensure_ascii=False)[:4000],
                        http_status=c.http_status,
                        ok=c.ok,
                        error_code=c.error_code,
                        retry_count=c.retry_count,
                        duration_ms=round(c.duration_ms, 2),
                    )
                )
        calls.clear()
    except Exception as exc:  # noqa: BLE001
        log.warning("diagnosis.tool_log_failed", error=str(exc)[:200])

