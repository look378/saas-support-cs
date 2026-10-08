"""指标服务：从**真实运行日志**聚合成效指标。

指标口径（架构文档 §7 与任务书第 7 节）：

  - **一次解决率**：会话未转人工、且最终结论为 resolved 的占比
  - **平均轮次**：每会话平均对话轮数
  - **转人工率**：产生过转人工记录的会话占比
  - **平均办结时长**：已办结会话的平均时长（秒）
  - **AI 自助解决率**：由 AI 独立闭环（非人工办结）的占比

人工基线的处理原则：**不虚构人工实测数据**。对比页引用公开行业基准区间
并在文档中标注来源；本项目只负责给出 AI 侧的真实统计值。

全部查询走**只读从库**（属只读分析场景），并显式绑定租户上下文。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.database import read_session
from app.core.logging_config import get_logger, timed_step
from app.core.trace_context import ensure_tenant

log = get_logger(__name__)

# AI 侧指标的时间窗口默认值（天）
DEFAULT_WINDOW_DAYS = 7

# 人工处理时长的公开行业参考区间（分钟）。
# 仅作演示对比用，来源标注见 docs/architecture.md；本项目未做人工实测。
INDUSTRY_AHT_MINUTES = (5.0, 10.0)


class MetricsService:
    """指标聚合（只读）。"""

    # ------------------------------------------------------------------
    def overview(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
        """核心指标总览。"""
        with ensure_tenant(tenant_id):
            with timed_step("metrics.overview", tenant_id=tenant_id, days=days):
                return self._overview(tenant_id, days)

    def _overview(self, tenant_id: str, days: int) -> dict[str, Any]:
        from app.models import Diagnosis, Escalation, SupportSession

        since = datetime.now(timezone.utc) - timedelta(days=days)

        with read_session() as s:
            sessions = s.execute(
                select(SupportSession).where(
                    SupportSession.tenant_id == tenant_id,
                    SupportSession.created_at >= since,
                )
            ).scalars().all()

            escalation_rows = s.execute(
                select(Escalation.session_id).where(
                    Escalation.tenant_id == tenant_id,
                    Escalation.created_at >= since,
                )
            ).scalars().all()

            diagnoses = s.execute(
                select(Diagnosis).where(
                    Diagnosis.tenant_id == tenant_id,
                    Diagnosis.created_at >= since,
                )
            ).scalars().all()

        total = len(sessions)
        if total == 0:
            return {
                "window_days": days,
                "total_sessions": 0,
                "note": "该时间窗内暂无会话数据",
            }

        escalated_session_ids = set(escalation_rows)
        escalated = sum(1 for x in sessions if x.escalated or x.id in escalated_session_ids)
        resolved_by_ai = sum(1 for x in sessions if x.resolved_by_ai and not x.escalated)
        closed = [x for x in sessions if x.state == "CLOSED" and x.duration_seconds > 0]

        # 一次解决率：未转人工且已闭环
        fcr = resolved_by_ai / total if total else 0.0
        avg_turns = sum(x.turn_count for x in sessions) / total if total else 0.0
        avg_duration = (
            sum(x.duration_seconds for x in closed) / len(closed) if closed else 0.0
        )

        # 诊断结论分布
        cause_counts: dict[str, int] = {}
        skill_counts: dict[str, int] = {}
        for d in diagnoses:
            cause_counts[d.cause_code or "UNKNOWN"] = cause_counts.get(d.cause_code or "UNKNOWN", 0) + 1
            skill_counts[d.skill or "unknown"] = skill_counts.get(d.skill or "unknown", 0) + 1

        return {
            "window_days": days,
            "total_sessions": total,
            "closed_sessions": len(closed),
            "escalated_sessions": escalated,
            "resolved_by_ai": resolved_by_ai,
            "ai_first_contact_resolution_rate": round(fcr, 4),
            "escalation_rate": round(escalated / total, 4) if total else 0.0,
            "avg_turns_per_session": round(avg_turns, 2),
            "avg_handle_seconds": round(avg_duration, 1),
            "avg_handle_minutes": round(avg_duration / 60.0, 2),
            "diagnosis_count": len(diagnoses),
            "cause_distribution": dict(sorted(cause_counts.items(), key=lambda kv: -kv[1])),
            "skill_distribution": dict(sorted(skill_counts.items(), key=lambda kv: -kv[1])),
            "industry_aht_minutes": list(INDUSTRY_AHT_MINUTES),
            "industry_baseline_note": "人工基线引用公开行业参考区间，本项目未做人工实测",
        }

    # ------------------------------------------------------------------
    def trend(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
        """按天趋势（用于看板折线）。"""
        from app.models import SupportSession

        since = datetime.now(timezone.utc) - timedelta(days=days)
        with ensure_tenant(tenant_id):
            with read_session() as s:
                rows = s.execute(
                    select(
                        func.date_trunc("day", SupportSession.created_at).label("day"),
                        func.count(SupportSession.id),
                        func.sum(func.cast(SupportSession.escalated, __import__("sqlalchemy").Integer)),
                        func.sum(func.cast(SupportSession.resolved_by_ai, __import__("sqlalchemy").Integer)),
                    )
                    .where(
                        SupportSession.tenant_id == tenant_id,
                        SupportSession.created_at >= since,
                    )
                    .group_by("day")
                    .order_by("day")
                ).all()

        return [
            {
                "day": r[0].date().isoformat() if r[0] else "",
                "sessions": int(r[1] or 0),
                "escalated": int(r[2] or 0),
                "resolved_by_ai": int(r[3] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    def skill_comparison(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
        """分技能对比：会话数、转人工率、平均耗时、验证通过率。"""
        from app.models import Diagnosis

        since = datetime.now(timezone.utc) - timedelta(days=days)
        with ensure_tenant(tenant_id), read_session() as s:
            rows = s.execute(
                select(Diagnosis).where(
                    Diagnosis.tenant_id == tenant_id, Diagnosis.created_at >= since
                )
            ).scalars().all()

        by_skill: dict[str, dict[str, Any]] = {}
        for d in rows:
            item = by_skill.setdefault(
                d.skill or "unknown",
                {"skill": d.skill or "unknown", "count": 0, "resolved": 0, "escalated": 0, "latency_sum": 0.0},
            )
            item["count"] += 1
            if d.outcome == "resolved":
                item["resolved"] += 1
            if d.outcome == "escalated":
                item["escalated"] += 1
            item["latency_sum"] += d.latency_ms or 0.0

        out: list[dict[str, Any]] = []
        for item in by_skill.values():
            count = item["count"] or 1
            out.append(
                {
                    "skill": item["skill"],
                    "count": item["count"],
                    "resolved": item["resolved"],
                    "escalated": item["escalated"],
                    "resolution_rate": round(item["resolved"] / count, 4),
                    "escalation_rate": round(item["escalated"] / count, 4),
                    "avg_latency_ms": round(item["latency_sum"] / count, 1),
                }
            )
        return sorted(out, key=lambda x: -x["count"])

    # ------------------------------------------------------------------
    def escalation_reasons(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> list[dict[str, Any]]:
        """转人工原因分布。"""
        from app.models import Escalation

        since = datetime.now(timezone.utc) - timedelta(days=days)
        with ensure_tenant(tenant_id), read_session() as s:
            rows = s.execute(
                select(Escalation.trigger_code).where(
                    Escalation.tenant_id == tenant_id, Escalation.created_at >= since
                )
            ).scalars().all()

        counts: dict[str, int] = {}
        for code in rows:
            # 一条转人工记录可能带多个触发码（逗号分隔），逐个计数
            for part in (code or "UNKNOWN").split(","):
                trigger = part.strip() or "UNKNOWN"
                counts[trigger] = counts.get(trigger, 0) + 1
        return [{"trigger": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

    # ------------------------------------------------------------------
    def agent_metrics(self, *, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """坐席维度指标：接管量、平均处理时长、当前负载、在线状态。"""
        from app.models import Agent
        from app.services.session_service import get_presence_service

        with ensure_tenant(tenant_id):
            with read_session() as s:
                agents = s.execute(
                    select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.id)
                ).scalars().all()

            presence = get_presence_service()
            ttl_map = presence.all_with_ttl(tenant_id=tenant_id, agents=[a.username for a in agents])

        out: list[dict[str, Any]] = []
        for a in agents[:limit]:
            pr = presence.get(tenant_id=tenant_id, agent=a.username)
            out.append(
                {
                    "agent": a.username,
                    "display_name": a.display_name,
                    "role": a.role,
                    "online": bool(pr and pr.presence in ("online", "break")),
                    "presence": pr.presence if pr else "offline",
                    "heartbeat_ttl": ttl_map.get(a.username, -2),
                    "current_load": pr.load if pr else 0,
                    "handled_sessions": a.handled_sessions,
                    "avg_handle_seconds": round(a.avg_handle_seconds, 1),
                    "avg_handle_minutes": round(a.avg_handle_seconds / 60.0, 2),
                }
            )
        return out

    # ------------------------------------------------------------------
    def llm_usage(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
        """模型调用计量（成本观察口径）。"""
        from app.models import LlmCall

        since = datetime.now(timezone.utc) - timedelta(days=days)
        with ensure_tenant(tenant_id), read_session() as s:
            rows = s.execute(
                select(LlmCall).where(
                    LlmCall.tenant_id == tenant_id, LlmCall.created_at >= since
                )
            ).scalars().all()

        by_kind: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = by_kind.setdefault(r.kind or "chat", {"kind": r.kind or "chat", "calls": 0, "tokens": 0, "failed": 0, "ms": 0.0})
            item["calls"] += 1
            item["tokens"] += r.total_tokens or 0
            item["failed"] += 0 if r.ok else 1
            item["ms"] += r.duration_ms or 0.0

        return {
            "total_calls": len(rows),
            "total_tokens": sum(r.total_tokens or 0 for r in rows),
            "failed_calls": sum(0 if r.ok else 1 for r in rows),
            "by_kind": [
                {
                    "kind": v["kind"],
                    "calls": v["calls"],
                    "tokens": v["tokens"],
                    "failed": v["failed"],
                    "avg_ms": round(v["ms"] / v["calls"], 1) if v["calls"] else 0.0,
                }
                for v in sorted(by_kind.values(), key=lambda x: -x["calls"])
            ],
        }

    # ------------------------------------------------------------------
    def compare_with_industry(self, *, tenant_id: str, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
        """AI 实测 vs 行业基准区间（不虚构人工实测数据）。"""
        ov = self.overview(tenant_id=tenant_id, days=days)
        ai_minutes = ov.get("avg_handle_minutes")
        lo, hi = INDUSTRY_AHT_MINUTES
        verdict = "无数据"
        if isinstance(ai_minutes, (int, float)) and ai_minutes > 0:
            if ai_minutes < lo:
                verdict = "快于行业区间下界"
            elif ai_minutes <= hi:
                verdict = "落在行业区间内"
            else:
                verdict = "慢于行业区间上界"
        return {
            "ai_avg_handle_minutes": ai_minutes,
            "industry_aht_minutes": [lo, hi],
            "verdict": verdict,
            "note": (
                "人工基线为公开行业参考区间，仅用于量级对比；"
                "本项目未采集人工实测数据，故不做统计显著性宣称。"
            ),
        }


_service: MetricsService | None = None


def get_metrics_service() -> MetricsService:
    """获取指标服务单例。"""
    global _service
    if _service is None:
        _service = MetricsService()
    return _service
