"""诊断状态图构建与执行。

图的形状（架构文档 §4）：

    route ─┬─ clarify ──► observe ──► diagnose ──► act ──► verify ─┬─► close
           │     ▲（interrupt 挂起）                ▲               │
           └─────┘                                  └──── 重试 ─────┘
                                                          │
                                                          └─► escalate

关键机制都在这里落地：
  - ``clarify`` 用 ``interrupt()`` 真正挂起，跨轮/跨进程用 ``Command(resume=...)`` 恢复（C-7）
  - ``step_count`` 超阈值 → 熔断转人工
  - 每个节点写 ``ai_task_logs``，构成 trace_id 链路回放数据
  - 诊断结论写 ``diagnoses``，供工单快照/指标/回放/测试断言复用
"""

from __future__ import annotations

import time
from typing import Any

# 必须**运行时**导入：图引擎按注解的**字符串形式**与白名单比对
# （见 langgraph/_internal/_runnable.py 的 KWARGS_CONFIG_KEYS）。
# 白名单里只有 "RunnableConfig" 与 "Optional[RunnableConfig]" 两种写法，
# 而本文件启用了 ``from __future__ import annotations``（注解一律变成字符串），
# 因此写成 ``RunnableConfig | None`` 会**匹配不上** → 引擎不会把 config
# 注入节点 → 节点拿不到图配置里的 trace_id/tenant_id，只能退回上下文变量兜底
# （跨线程时上下文变量不保证传播，工具调用记录会丢 trace_id，回放查不到工具调用）。
# 结论：节点注解统一用裸 ``RunnableConfig``（其字符串形式恰好在白名单内）。
# ⚠️ 不要"顺手"改成 ``RunnableConfig | None``，也不要用 ``Optional[...]``：
#     前者匹配不上白名单，后者会被风格工具（UP045/UP007）自动改回前者，
#     两条路都会静默破坏注入。
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.graph.state import (
    SkillOutcome,
    SupportState,
    merge,
    note_step,
    record_diagnosis_step,
    record_tool_calls,
)
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.trace_context import get_tenant_id, get_trace_id
from app.skills.registry import get_registry

log = get_logger(__name__)


# ===========================================================================
# 节点
# ===========================================================================
def node_route(state: SupportState) -> dict[str, Any]:
    """技能路由 + 实体抽取。"""
    started = time.perf_counter()
    registry = get_registry()
    message = state.get("user_message", "")
    entities = dict(state.get("entities") or {})

    # 若用户这一轮是对澄清的补充，先并入实体
    if state.get("user_reply"):
        message = f"{message} {state['user_reply']}"

    route = registry.route(message, entities)
    skill = registry.get(route.skill)
    if skill is not None:
        entities = skill.extract(message, entities)

    missing = skill.missing_entities(entities) if skill else []
    need_clarify = bool(missing) or (route.need_clarify and not entities)
    # 澄清问题在**路由节点**就生成并写入状态：
    # 1) 条件边据"是否产出澄清问题"判断走向，比依赖一个可能被后续节点改写的布尔键更稳
    # 2) need_clarify 在上限后清回 False，避免污染后续轮次
    clarify_question = ""
    if need_clarify and skill is not None:
        clarify_question = skill.clarify_question(missing)

    # 路由决策留痕：把打分与依据写入日志，便于排查"为什么走了这个技能"
    log.info(
        "route.decision",
        skill=route.skill,
        confidence=round(route.confidence, 3),
        reason=route.reason,
        scores=route.scores,
        entities=entities,
        missing=missing,
        need_clarify=need_clarify,
    )

    delta: dict[str, Any] = {
        "skill": route.skill,
        "entities": entities,
        "decision_reason": route.reason,
        "need_clarify": need_clarify,
        "clarify_question": clarify_question,
        "missing_entities": missing,
        **note_step(state, "route"),
    }

    record_diagnosis_step(
        tenant_id=state.get("tenant_id") or get_tenant_id(),
        session_id=_session_pk(state),
        skill=route.skill,
        node="route",
        status="ok",
        detail=f"路由={route.skill} 置信度={route.confidence:.2f} 理由={route.reason}",
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return delta


def node_clarify(state: SupportState) -> dict[str, Any]:
    """澄清追问：用 interrupt 挂起，等待用户补充后继续。"""
    started = time.perf_counter()
    registry = get_registry()
    skill = registry.get(state.get("skill", "faq"))
    missing = list(state.get("missing_entities") or [])

    if missing and skill is not None:
        question = skill.clarify_question(missing)
    else:
        question = "为了更准确地帮您，请补充一下具体是哪个产品功能或哪个对象出了问题？"

    payload = {
        "type": "clarify",
        "skill": state.get("skill", ""),
        "question": question,
        "missing": missing,
    }
    # 挂起：下一次以 Command(resume=<用户回答>) 恢复，从本节点继续
    answer = interrupt(payload)

    merged_entities = dict(state.get("entities") or {})
    if skill is not None and isinstance(answer, str):
        merged_entities = skill.extract(answer, merged_entities)

    record_diagnosis_step(
        tenant_id=state.get("tenant_id") or get_tenant_id(),
        session_id=_session_pk(state),
        skill=state.get("skill", ""),
        node="clarify",
        status="ok",
        detail=f"已澄清：{str(answer)[:120]}",
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return {
        "user_reply": answer if isinstance(answer, str) else str(answer),
        "entities": merged_entities,
        "clarify_question": question,
        # **必须显式清除**：LangGraph 会保留上一轮写入的状态键。
        # 若这里不把 need_clarify 置回 False，第二轮的条件边仍会读到 True，
        # 于是再次走澄清分支、永远不进入取证与诊断（本项目实测踩到）。
        "need_clarify": False,
        "missing_entities": [],
        **note_step(state, "clarify"),
    }


def node_observe(state: SupportState, config: RunnableConfig = None) -> dict[str, Any]:
    """取证。"""
    return _run_skill_node(state, "observe", config)


def node_diagnose(state: SupportState, config: RunnableConfig = None) -> dict[str, Any]:
    """诊断。"""
    return _run_skill_node(state, "diagnose", config)


def node_act(state: SupportState, config: RunnableConfig = None) -> dict[str, Any]:
    """处置。"""
    return _run_skill_node(state, "act", config)


def node_verify(state: SupportState, config: RunnableConfig = None) -> dict[str, Any]:
    """验证。"""
    return _run_skill_node(state, "verify", config)


def _bind_graph_context(state: SupportState, config: RunnableConfig = None) -> Any:
    """在节点内部绑定链路上下文。

    图引擎可能在不同线程/任务里执行节点，``ContextVar`` 不保证跨线程传播，
    因此节点必须自己从图配置里取回 ``trace_id`` 等信息并绑定，
    否则该节点写出的日志、诊断步骤与工具调用都会丢失链路标识
    （表现为回放时查不到工具调用，本项目实测踩到）。

    返回一个上下文管理器；无配置时退化为空操作。
    """
    from contextlib import nullcontext

    from app.core.trace_context import bind as bind_ctx

    meta = {}
    if isinstance(config, dict):
        meta = config.get("metadata") or {}
    trace_id = meta.get("trace_id") or (get_trace_id() if get_trace_id() != "-" else "")
    tenant_id = meta.get("tenant_id") or (get_tenant_id() if get_tenant_id() != "-" else "")
    session_key = meta.get("session_key") or str(state.get("session_id") or "")
    if not trace_id and not tenant_id:
        return nullcontext()
    return bind_ctx(
        trace_id=trace_id or None,
        tenant_id=tenant_id or None,
        session_id=session_key or None,
        user_id=str(state.get("user_ext_id") or "") or None,
    )


def _run_skill_node(
    state: SupportState, phase: str, config: RunnableConfig = None
) -> dict[str, Any]:
    """统一的技能节点执行：计时、日志、错误隔离、结果合并。"""
    started = time.perf_counter()
    skill_code = state.get("skill", "faq")
    registry = get_registry()
    skill = registry.get(skill_code)

    if skill is None:
        return {
            "reply": "抱歉，系统暂时无法处理该问题，已为您转接人工客服。",
            "escalate": True,
            "escalate_reasons": list(state.get("escalate_reasons") or []) + [f"未知技能 {skill_code}"],
            "outcome": "error",
            **note_step(state, phase),
        }

    with _bind_graph_context(state, config):
        try:
            outcome: SkillOutcome = getattr(skill, phase)(state)
        except Exception as exc:
            log.error("skill.node_failed", skill=skill_code, phase=phase, error=str(exc)[:300], exc_info=True)
            record_diagnosis_step(
                tenant_id=state.get("tenant_id") or get_tenant_id(),
                session_id=_session_pk(state),
                skill=skill_code,
                node=phase,
                status="error",
                detail=f"{type(exc).__name__}: {exc}"[:500],
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return {
                "degraded": True,
                "degrade_reason": f"{phase} 阶段异常",
                "escalate": True,
                "escalate_reasons": list(state.get("escalate_reasons") or []) + [f"{phase} 阶段异常"],
                "outcome": "error",
                **note_step(state, phase),
            }

        delta = merge(outcome)
        delta.update(note_step(state, phase))
        # 技能内部的客户端调用记录落库（回放数据源）
        record_tool_calls(
            tenant_id=state.get("tenant_id") or get_tenant_id(),
            session_id=_session_pk(state),
            diagnosis_id=state.get("diagnosis_id"),
            client=getattr(skill, "client", None),
        )
        record_diagnosis_step(
            tenant_id=state.get("tenant_id") or get_tenant_id(),
            session_id=_session_pk(state),
            skill=skill_code,
            node=phase,
            status="degraded" if outcome.degraded else "ok",
            detail=(
                f"cause={outcome.cause_code or state.get('cause_code', '')} "
                f"decision={outcome.decision or state.get('decision', '')} "
                f"verify_ok={outcome.verify_ok if outcome.verify_ok is not None else ''}"
            )[:500],
            duration_ms=(time.perf_counter() - started) * 1000,
            degraded=outcome.degraded,
        )
        return delta


def node_finalize(state: SupportState) -> dict[str, Any]:
    """收尾：落库诊断结论并生成最终回复。"""
    started = time.perf_counter()
    escalate = bool(state.get("escalate"))
    verify_ok = bool(state.get("verify_ok"))

    if escalate:
        outcome = "escalated"
    elif verify_ok or state.get("decision") in ("none", "answer", "idempotent", "confirmed"):
        outcome = state.get("outcome") or "resolved"
    else:
        outcome = state.get("outcome") or "unresolved"

    reply = state.get("reply") or ""
    if not reply:
        if escalate:
            reply = "这个问题需要人工协助，我已为您转接人工客服，请稍候。"
        elif verify_ok:
            reply = "已为您处理完成。"
        else:
            reply = "抱歉，我暂时无法确认问题原因，建议回复「转人工」由人工客服跟进。"

    # 生成交接快照：**只要转人工就必须有快照**。
    # 若技能链路未产出（例如降级导致提前转人工），这里兜底生成，
    # 否则坐席拿到的是空交接，无法了解 AI 已经排查过什么（本项目实测踩到）。
    snapshot = ""
    if escalate:
        snapshot = _build_snapshot(state)

    diagnosis_id = _persist_diagnosis(state, outcome=outcome, snapshot=snapshot)

    record_diagnosis_step(
        tenant_id=state.get("tenant_id") or get_tenant_id(),
        session_id=_session_pk(state),
        skill=state.get("skill", ""),
        node="finalize",
        status="ok",
        detail=f"结论={outcome} 是否需要人工={escalate}",
        duration_ms=(time.perf_counter() - started) * 1000,
        diagnosis_id=diagnosis_id,
    )

    return {
        "outcome": outcome,
        "reply": reply,
        "handover_snapshot": snapshot,
        "diagnosis_id": diagnosis_id,
        **note_step(state, "finalize"),
    }


def _build_snapshot(state: SupportState) -> str:
    """转人工交接快照（Markdown）。"""
    checked = "；".join(state.get("evidence") or []) or "无"
    done = "；".join(a.get("detail", "") for a in (state.get("actions") or [])) or "无"
    reasons = "；".join(state.get("escalate_reasons") or []) or "未说明"
    suggestion = {
        "integration": "请人工核实该工作区的集成授权状态，必要时联系租户管理员重新授权。",
        "permission": "请人工确认组织策略与文档归属，必要时调整共享设置。",
        "billing": "请人工核对账单明细与用量记录，确认争议金额后决定是否退款。",
        "account": "请人工核实用户身份，确认是否为本人操作后再恢复账号。",
        "faq": "请人工补充说明该功能的使用方式。",
    }.get(state.get("skill", "faq"), "请人工跟进。")
    return "\n".join(
        [
            f"## 交接快照（技能：{state.get('skill', '')}）",
            "",
            f"**触发原因**：{reasons}",
            "",
            f"**已排查项**：{checked}",
            "",
            f"**已执行操作**：{done}",
            "",
            # 相邻 f-string 拼接为**同一个**列表元素（诊断结论 + 置信度/风险分）。
            # 显式加括号，避免后人误读成两个元素。
            (
                f"**诊断结论**：{state.get('cause_text') or state.get('cause_code') or '未定论'}"
                f"（置信度 {float(state.get('confidence', 0)):.2f}，"
                f"风险分 {float(state.get('risk_score', 0)):.0f}）"
            ),
            "",
            f"**当前状态**：{state.get('verify_detail') or state.get('outcome') or '待人工处理'}",
            "",
            f"**建议话术**：{suggestion}",
        ]
    )


def _persist_diagnosis(state: SupportState, *, outcome: str, snapshot: str) -> int | None:
    """落库诊断结论。返回主键（失败返回 None，不阻断主流程）。"""
    import json

    try:
        from app.core.database import write_session
        from app.core.trace_context import get_trace_id
        from app.models import Diagnosis

        with write_session() as s:
            row = Diagnosis(
                tenant_id=state.get("tenant_id") or get_tenant_id(),
                trace_id=get_trace_id(),
                session_id=_session_pk(state) or 0,
                skill=state.get("skill", ""),
                cause_code=state.get("cause_code", ""),
                cause_text=state.get("cause_text", ""),
                confidence=float(state.get("confidence", 0.0)),
                risk_score=float(state.get("risk_score", 0.0)),
                decision=state.get("decision", ""),
                decision_reason=state.get("decision_reason", "")[:500],
                evidence_json=json.dumps(state.get("evidence") or [], ensure_ascii=False)[:8000],
                outcome=outcome,
                matched_cases=",".join(state.get("matched_cases") or [])[:255],
            )
            s.add(row)
            s.flush()
            return int(row.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("diagnosis.persist_failed", error=str(exc)[:200])
        return None


def _session_pk(state: SupportState) -> int | None:
    """会话主键（图状态里存的是业务 session_key，这里映射为数据库主键）。"""
    value = state.get("session_pk")
    return int(value) if value else None


# ===========================================================================
# 条件边
# ===========================================================================
def after_route(state: SupportState) -> str:
    """路由后：产出了澄清问题就去澄清，否则进入取证。

    判据用"是否产出澄清问题"而不是 ``need_clarify`` 布尔键——
    前者只由 route 节点写入、语义明确；后者可能被其它节点或历史状态影响。
    """
    if state.get("clarify_question"):
        return "clarify"
    return "observe"


def after_observe(state: SupportState) -> str:
    """取证后：查询失败无facts时仍继续诊断（诊断会给出降级结论）。"""
    return "diagnose"


def after_diagnose(state: SupportState) -> str:
    """诊断后：无处置需求则直接收尾。"""
    if state.get("decision") == "none" and state.get("verify_ok"):
        return "finalize"
    return "act"


def after_act(state: SupportState) -> str:
    """处置后：需要用户操作（重新授权）时不再验证，直接收尾。

    为什么只保留这一条分支：``need_reauth`` 意味着"等用户点链接授权"，
    本轮不可能验证通过，走 verify 只会拿到 ``verify_ok=False`` 再回退重试，
    白白多跑一轮并可能把已闭环的会话误判为需要人工。
    其余情况（自动修复、转人工审批、幂等命中）都必须验证处置效果。
    """
    if state.get("decision") == "need_reauth" and not state.get("verify_ok"):
        return "finalize"
    return "verify"


def after_verify(state: SupportState) -> str:
    """验证后：通过或无处置 → 收尾；失败且步数未超限 → 回退重试；否则收尾转人工。"""
    if state.get("verify_ok"):
        return "finalize"
    if state.get("decision") == "none":
        return "finalize"
    if state.get("decision") == "need_reauth":
        return "finalize"

    steps = int(state.get("step_count", 0))
    if steps < settings.escalate_max_steps and not state.get("escalate"):
        return "retry"
    return "finalize"


def node_retry(state: SupportState) -> dict[str, Any]:
    """回退重试：清掉上一轮的结论，重新诊断（保留 facts）。"""
    record_diagnosis_step(
        tenant_id=state.get("tenant_id") or get_tenant_id(),
        session_id=_session_pk(state),
        skill=state.get("skill", ""),
        node="retry",
        status="ok",
        detail=f"验证未通过，回退重试（第 {state.get('step_count', 0)} 步）",
        duration_ms=0.0,
    )
    return {
        "cause_code": "",
        "decision": "",
        "verify_ok": False,
        "reply": "",
        "node_path": list(state.get("node_path", [])) + ["retry"],
    }


# ===========================================================================
# 建图
# ===========================================================================
def build_graph(checkpointer: Any = None) -> Any:
    """构建诊断图。

    ``checkpointer`` 传 ``AsyncPostgresSaver`` 时状态持久化到 PostgreSQL，
    支持跨进程断点续接；传 ``None`` 时用于纯函数测试。
    """
    g = StateGraph(SupportState)

    g.add_node("route", node_route)
    g.add_node("clarify", node_clarify)
    g.add_node("observe", node_observe)
    g.add_node("diagnose", node_diagnose)
    g.add_node("act", node_act)
    g.add_node("verify", node_verify)
    g.add_node("retry", node_retry)
    g.add_node("finalize", node_finalize)

    g.add_edge(START, "route")
    g.add_conditional_edges("route", after_route, {"clarify": "clarify", "observe": "observe"})
    g.add_edge("clarify", "observe")
    g.add_conditional_edges("observe", after_observe, {"diagnose": "diagnose"})
    g.add_conditional_edges("diagnose", after_diagnose, {"act": "act", "finalize": "finalize"})
    g.add_conditional_edges("act", after_act, {"verify": "verify", "finalize": "finalize"})
    g.add_conditional_edges(
        "verify", after_verify, {"finalize": "finalize", "retry": "retry"}
    )
    g.add_edge("retry", "diagnose")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
