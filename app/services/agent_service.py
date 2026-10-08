"""AgentService 隔离层（架构文档 §3.2 / ADR-007）。

**所有业务代码访问诊断能力都必须经过本模块**，不得直接 import 图引擎或模型客户端。
隔离层集中承担：

  - 状态图执行（含 Postgres 状态持久化与 ``thread_id`` 构造）
  - 澄清挂起与恢复（``interrupt`` / ``Command(resume=...)``）
  - 链路上下文注入（trace_id / tenant_id / session_id）
  - 超时与异常归一化（模型/图引擎故障 → 统一降级信号）
  - 状态快照读取（用于"进程重启后查看进度"与工作台展示）

收益：模型供应商、图引擎、向量库三者任一可替换，业务层零改动。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging_config import get_logger, timed_step
from app.core.platform_compat import apply as apply_platform_fix
from app.core.trace_context import bind, get_trace_id

log = get_logger(__name__)

# 必须在任何异步数据库访问之前应用平台修正
apply_platform_fix()


class AgentUnavailable(RuntimeError):
    """诊断服务不可用（图执行失败/超时）。业务层据此走降级话术。"""


@dataclass
class TurnResult:
    """一轮对话的执行结果。"""

    reply: str
    skill: str = ""
    outcome: str = ""
    cause_code: str = ""
    cause_text: str = ""
    confidence: float = 0.0
    risk_score: float = 0.0
    decision: str = ""
    escalate: bool = False
    escalate_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)
    verify_ok: bool = False
    verify_detail: str = ""
    node_path: list[str] = field(default_factory=list)
    handover_snapshot: str = ""
    diagnosis_id: int | None = None
    degraded: bool = False
    degrade_reason: str = ""
    pending_interrupt: dict[str, Any] | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "skill": self.skill,
            "outcome": self.outcome,
            "cause_code": self.cause_code,
            "cause_text": self.cause_text,
            "confidence": self.confidence,
            "risk_score": self.risk_score,
            "decision": self.decision,
            "escalate": self.escalate,
            "escalate_reasons": self.escalate_reasons,
            "evidence": self.evidence,
            "actions": self.actions,
            "options": self.options,
            "verify_ok": self.verify_ok,
            "verify_detail": self.verify_detail,
            "node_path": self.node_path,
            "handover_snapshot": self.handover_snapshot,
            "diagnosis_id": self.diagnosis_id,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "pending_interrupt": self.pending_interrupt,
            "duration_ms": round(self.duration_ms, 2),
        }


def make_thread_id(tenant_id: str, session_key: str) -> str:
    """构造状态存储的线程标识。

    带上租户可天然隔离不同租户的同名会话，避免状态串用。
    """
    return f"{tenant_id}:{session_key}"


class AgentService:
    """诊断服务隔离层。

    每次调用建立独立的异步状态持久化连接（Postgres），
    不用长连接是因为跨进程续接要求状态一定落在数据库而不是连接里。
    """

    def __init__(self) -> None:
        self._graph_cache: Any = None

    # ------------------------------------------------------------------
    async def _with_graph(self, fn):
        """在 Postgres 状态存储上下文内构建图并执行回调。"""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        from app.agents.graph.support_graph import build_graph

        try:
            async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn) as saver:
                await saver.setup()
                graph = build_graph(saver)
                return await fn(graph)
        except AgentUnavailable:
            raise
        except Exception as exc:
            # 结构化成套日志（structlog）支持 .exception() 直接带堆栈，
            # 不再手工传 exc_info=True
            log.exception("agent.execution_failed", error=f"{type(exc).__name__}: {exc}"[:400])
            raise AgentUnavailable(f"诊断流程执行失败: {exc}") from exc

    # ------------------------------------------------------------------
    async def run_turn(
        self,
        *,
        tenant_id: str,
        session_key: str,
        user_message: str,
        user_ext_id: str = "",
        session_pk: int | None = None,
        chosen_option: str | None = None,
        resume_value: str | None = None,
        trace_id: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> TurnResult:
        """执行一轮对话。

        - 首次进入：传 ``user_message``
        - 恢复挂起（用户回答澄清）：传 ``resume_value``
        """
        started = time.perf_counter()
        thread_id = make_thread_id(tenant_id, session_key)
        tid = trace_id or get_trace_id()
        if tid == "-":
            tid = uuid.uuid4().hex

        async def job(graph):
            cfg = {
                "configurable": {"thread_id": thread_id},
                # trace_id 放进图配置：图引擎可能在不同线程/任务中执行节点，
                # ContextVar 不保证跨线程传播，因此把链路标识显式随配置下发，
                # 由节点自行绑定（否则节点写入的日志与工具调用会丢失 trace_id）。
                "metadata": {"trace_id": tid, "tenant_id": tenant_id, "session_key": session_key},
            }
            payload: Any
            if resume_value is not None:
                from langgraph.types import Command

                payload = Command(resume=resume_value)
            else:
                payload = {
                    "tenant_id": tenant_id,
                    "session_id": session_key,
                    "session_pk": session_pk,
                    "user_ext_id": user_ext_id,
                    "user_message": user_message,
                    "entities": {},
                    "facts": {},
                    "actions": [],
                    "evidence": [],
                    "options": [],
                    "escalate_reasons": [],
                    "node_path": [],
                    "step_count": 0,
                }
                if chosen_option:
                    payload["chosen_option"] = chosen_option

            state = await graph.ainvoke(payload, config=cfg)
            snapshot = await graph.aget_state(config=cfg)
            interrupts: list[Any] = []
            for task in snapshot.tasks or []:
                interrupts.extend(getattr(task, "interrupts", ()) or ())
            return state, (interrupts[0].value if interrupts else None)

        with bind(trace_id=tid, tenant_id=tenant_id, session_id=session_key, user_id=user_ext_id):
            with timed_step("agent.turn", session_key=session_key, skill_hint=user_message[:40]):
                try:
                    state, interrupt_payload = await asyncio.wait_for(
                        self._with_graph(job), timeout=timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    log.error("agent.timeout", timeout_seconds=timeout_seconds)
                    raise AgentUnavailable(f"诊断超时（{timeout_seconds}s）") from exc

        result = TurnResult(
            reply=str(state.get("reply", "")),
            skill=str(state.get("skill", "")),
            outcome=str(state.get("outcome", "")),
            cause_code=str(state.get("cause_code", "")),
            cause_text=str(state.get("cause_text", "")),
            confidence=float(state.get("confidence", 0.0) or 0.0),
            risk_score=float(state.get("risk_score", 0.0) or 0.0),
            decision=str(state.get("decision", "")),
            escalate=bool(state.get("escalate")),
            escalate_reasons=list(state.get("escalate_reasons") or []),
            evidence=list(state.get("evidence") or []),
            actions=list(state.get("actions") or []),
            options=list(state.get("options") or []),
            verify_ok=bool(state.get("verify_ok")),
            verify_detail=str(state.get("verify_detail", "")),
            node_path=list(state.get("node_path") or []),
            handover_snapshot=str(state.get("handover_snapshot") or ""),
            diagnosis_id=state.get("diagnosis_id"),
            degraded=bool(state.get("degraded")),
            degrade_reason=str(state.get("degrade_reason", "")),
            pending_interrupt=interrupt_payload,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        log.info(
            "agent.turn_done",
            skill=result.skill,
            outcome=result.outcome,
            cause=result.cause_code,
            escalate=result.escalate,
            duration_ms=round(result.duration_ms, 2),
            thread_id=thread_id,
        )
        return result

    # ------------------------------------------------------------------
    async def get_state(self, *, tenant_id: str, session_key: str) -> dict[str, Any]:
        """读取会话的图状态（用于"重启后查看进度"与工作台展示）。"""
        thread_id = make_thread_id(tenant_id, session_key)

        async def job(graph):
            cfg = {"configurable": {"thread_id": thread_id}}
            snap = await graph.aget_state(config=cfg)
            interrupts: list[Any] = []
            for task in snap.tasks or []:
                interrupts.extend(getattr(task, "interrupts", ()) or ())
            return {
                "thread_id": thread_id,
                "next": list(snap.next or []),
                "values": dict(snap.values or {}),
                "pending": bool(snap.next),
                "interrupt": interrupts[0].value if interrupts else None,
                "created_at": str(getattr(snap, "created_at", "")),
            }

        return await self._with_graph(job)

    async def resume_after_restart(
        self, *, tenant_id: str, session_key: str, user_message: str, **kwargs: Any
    ) -> TurnResult:
        """进程重启后继续：自动判断是"回答澄清"还是"新话题"。

        若状态存储显示该会话有待执行节点（挂起中），说明上一轮是在等用户补充信息，
        则把本次消息作为澄清回答续接；否则作为新一轮处理。
        这样"重启续接"与"正常追问"走的是同一条路径，无需用户重述。
        """
        snap = await self.get_state(tenant_id=tenant_id, session_key=session_key)
        if snap.get("pending"):
            log.info("agent.resume_from_snapshot", thread_id=snap["thread_id"], next=snap["next"])
            return await self.run_turn(
                tenant_id=tenant_id,
                session_key=session_key,
                user_message=user_message,
                resume_value=user_message,
                **kwargs,
            )
        return await self.run_turn(
            tenant_id=tenant_id, session_key=session_key, user_message=user_message, **kwargs
        )


_service: AgentService | None = None


def get_agent_service() -> AgentService:
    """获取诊断服务单例。"""
    global _service
    if _service is None:
        _service = AgentService()
    return _service
