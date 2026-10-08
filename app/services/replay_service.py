"""链路回放：按 trace_id 还原一次诊断的完整执行过程。

数据来源（四张表联合）：
  - ``ai_task_logs``      —— 图节点级执行记录（节点跳转序列、耗时）
  - ``tool_invocations``  —— 工具调用（出站目标、HTTP 状态、重试、耗时）
  - ``diagnosis_steps``   —— 诊断步骤明细
  - ``diagnoses``         —— 最终结论（含证据与决策依据）

输出为"时间线 + 耗时瀑布"两段结构，直接供工作台渲染。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.core.database import read_session
from app.core.logging_config import get_logger
from app.core.trace_context import ensure_tenant

log = get_logger(__name__)


class TraceReplayService:
    """链路回放（只读）。"""

    # ------------------------------------------------------------------
    def by_trace(self, *, tenant_id: str, trace_id: str) -> dict[str, Any]:
        """按 trace_id 还原完整链路。"""
        from app.models import AiTaskLog, Diagnosis, DiagnosisStep, ToolInvocation

        with ensure_tenant(tenant_id), read_session() as s:
            nodes = s.execute(
                select(AiTaskLog)
                .where(AiTaskLog.tenant_id == tenant_id, AiTaskLog.trace_id == trace_id)
                .order_by(AiTaskLog.id.asc())
            ).scalars().all()

            tools = s.execute(
                select(ToolInvocation)
                .where(
                    ToolInvocation.tenant_id == tenant_id,
                    ToolInvocation.trace_id == trace_id,
                )
                .order_by(ToolInvocation.id.asc())
            ).scalars().all()

            diagnosis = s.execute(
                select(Diagnosis)
                .where(Diagnosis.tenant_id == tenant_id, Diagnosis.trace_id == trace_id)
                .order_by(Diagnosis.id.desc())
            ).scalars().first()

            steps = []
            if diagnosis is not None:
                steps = s.execute(
                    select(DiagnosisStep)
                    .where(
                        DiagnosisStep.tenant_id == tenant_id,
                        DiagnosisStep.diagnosis_id == diagnosis.id,
                    )
                    .order_by(DiagnosisStep.seq.asc(), DiagnosisStep.id.asc())
                ).scalars().all()

        if not nodes and not tools and diagnosis is None:
            return {"found": False, "trace_id": trace_id, "note": "未找到该 trace_id 的记录"}

        timeline: list[dict[str, Any]] = []

        # 节点轨迹
        for n in nodes:
            timeline.append(
                {
                    "kind": "node",
                    "name": n.node,
                    "phase": n.phase,
                    "status": n.status,
                    "skill": n.skill,
                    "duration_ms": round(n.duration_ms or 0.0, 2),
                    "degraded": bool(n.degraded),
                    "detail": (n.detail or "")[:500],
                    "at": n.created_at.isoformat() if n.created_at else "",
                }
            )

        # 工具调用
        for t in tools:
            timeline.append(
                {
                    "kind": "tool",
                    "name": t.tool_name,
                    "status": "ok" if t.ok else "error",
                    "target": t.target,
                    "http_status": t.http_status,
                    "retry_count": t.retry_count,
                    "error_code": t.error_code,
                    "duration_ms": round(t.duration_ms or 0.0, 2),
                    "request": _safe_json(t.request_json),
                    "response": _safe_json(t.response_json),
                    "at": t.created_at.isoformat() if t.created_at else "",
                }
            )

        timeline.sort(key=lambda x: (x.get("at") or "", x["kind"] != "node"))

        total_ms = sum(x["duration_ms"] for x in timeline if x["kind"] == "node")
        waterfall = [
            {
                "name": f"{x['kind']}:{x['name']}",
                "duration_ms": x["duration_ms"],
                "share": round(x["duration_ms"] / total_ms, 4) if total_ms else 0.0,
                "status": x["status"],
            }
            for x in sorted(timeline, key=lambda x: -x["duration_ms"])
            if x["duration_ms"] > 0
        ]

        conclusion: dict[str, Any] = {}
        if diagnosis is not None:
            conclusion = {
                "diagnosis_id": diagnosis.id,
                "skill": diagnosis.skill,
                "cause_code": diagnosis.cause_code,
                "cause_text": diagnosis.cause_text,
                "confidence": diagnosis.confidence,
                "risk_score": diagnosis.risk_score,
                "decision": diagnosis.decision,
                "decision_reason": diagnosis.decision_reason,
                "outcome": diagnosis.outcome,
                "evidence": _safe_json(diagnosis.evidence_json) or [],
                "matched_cases": (diagnosis.matched_cases or "").split(",") if diagnosis.matched_cases else [],
                "latency_ms": round(diagnosis.latency_ms or 0.0, 2),
            }

        return {
            "found": True,
            "trace_id": trace_id,
            "tenant_id": tenant_id,
            "node_count": len(nodes),
            "tool_count": len(tools),
            "step_count": len(steps),
            "total_node_ms": round(total_ms, 2),
            "slowest": waterfall[0]["name"] if waterfall else "",
            "timeline": timeline,
            "waterfall": waterfall[:15],
            "steps": [
                {
                    "seq": st.seq,
                    "node": st.node,
                    "status": st.status,
                    "detail": (st.detail or "")[:300],
                    "duration_ms": round(st.duration_ms or 0.0, 2),
                }
                for st in steps
            ],
            "conclusion": conclusion,
        }

    # ------------------------------------------------------------------
    def recent_traces(self, *, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """最近产生诊断的 trace_id 列表（供工作台选择回放目标）。"""
        from app.models import Diagnosis

        with ensure_tenant(tenant_id), read_session() as s:
            rows = s.execute(
                select(Diagnosis)
                .where(Diagnosis.tenant_id == tenant_id)
                .order_by(Diagnosis.id.desc())
                .limit(limit)
            ).scalars().all()

        return [
            {
                "trace_id": d.trace_id,
                "skill": d.skill,
                "cause_code": d.cause_code,
                "outcome": d.outcome,
                "decision": d.decision,
                "confidence": d.confidence,
                "risk_score": d.risk_score,
                "session_id": d.session_id,
                "at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in rows
        ]

    # ------------------------------------------------------------------
    def session_timeline(self, *, tenant_id: str, session_key: str) -> dict[str, Any]:
        """按会话还原：消息流 + 状态跃迁 + 关联诊断。"""
        from app.models import Diagnosis, Message, SessionEvent, SupportSession

        with ensure_tenant(tenant_id):
            with read_session() as s:
                session = s.execute(
                    select(SupportSession).where(
                        SupportSession.tenant_id == tenant_id,
                        SupportSession.session_key == session_key,
                    )
                ).scalar_one_or_none()
                if session is None:
                    return {"found": False, "session_key": session_key}

                messages = s.execute(
                    select(Message)
                    .where(Message.tenant_id == tenant_id, Message.session_id == session.id)
                    .order_by(Message.id.asc())
                ).scalars().all()

                events = s.execute(
                    select(SessionEvent)
                    .where(SessionEvent.tenant_id == tenant_id, SessionEvent.session_id == session.id)
                    .order_by(SessionEvent.id.asc())
                ).scalars().all()

                diagnoses = s.execute(
                    select(Diagnosis)
                    .where(Diagnosis.tenant_id == tenant_id, Diagnosis.session_id == session.id)
                    .order_by(Diagnosis.id.asc())
                ).scalars().all()

        return {
            "found": True,
            "session": {
                "session_key": session.session_key,
                "state": session.state,
                "handler": session.handler,
                "skill": session.skill,
                "turn_count": session.turn_count,
                "escalated": session.escalated,
                "resolved_by_ai": session.resolved_by_ai,
                "duration_seconds": session.duration_seconds,
                "created_at": session.created_at.isoformat() if session.created_at else "",
            },
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "latency_ms": m.latency_ms,
                    "diagnosis_id": m.diagnosis_id,
                    "at": m.created_at.isoformat() if m.created_at else "",
                }
                for m in messages
            ],
            "events": [
                {
                    "type": e.event_type,
                    "from": e.from_state,
                    "to": e.to_state,
                    "detail": _safe_json(e.detail_json),
                    "at": e.created_at.isoformat() if e.created_at else "",
                }
                for e in events
            ],
            "diagnoses": [
                {
                    "trace_id": d.trace_id,
                    "skill": d.skill,
                    "cause_code": d.cause_code,
                    "outcome": d.outcome,
                    "decision": d.decision,
                }
                for d in diagnoses
            ],
        }


def _safe_json(raw: str) -> Any:
    """宽松解析 JSON，失败时回退为原字符串。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw[:500]


_service: TraceReplayService | None = None


def get_replay_service() -> TraceReplayService:
    """获取链路回放服务单例。"""
    global _service
    if _service is None:
        _service = TraceReplayService()
    return _service
