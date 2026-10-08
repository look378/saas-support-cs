"""智能转人工与坐席派单（真实流转）。

完整链路（架构文档 §9.3）：

    AI 触发 → 写 escalations + 进 Redis 队列
           → 派单（技能组 ∩ 在线 ∩ 负载最小）
           → WebSocket 推送坐席
           → 坐席接管（Redis 分布式锁抢会话）
           → AI 停止介入（会话状态机切 AGENT_HANDLING）
           → 人工对话 → 办结 → 可选回流案例库

触发条件是**矩阵**（架构文档 §9.1）：步数超限 / 情绪激烈 / 金额超阈值 /
需管理员介入 / 历史投诉超限 / 用户显式要求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import read_session, write_session
from app.core.logging_config import get_logger, warn_degraded
from app.core.redis_client import distributed_lock, key_session_lock
from app.core.trace_context import get_tenant_id, get_trace_id
from app.models import (
    Agent,
    AgentSkill,
    Escalation,
    SupportSession,
    Tenant,
    Ticket,
    TicketMessage,
)
from app.models.base import new_id
from app.models.conversation import (
    SESSION_STATE_AGENT,
    SESSION_STATE_CLOSED,
    SESSION_STATE_QUEUED,
)
from app.services.event_bus import (
    EVT_AGENT_DISCONNECTED,
    EVT_ESCALATION_ASSIGNED,
    EVT_ESCALATION_CREATED,
    EVT_SESSION_CLAIMED,
    EVT_SESSION_CLOSED,
    Event,
    get_event_bus,
)
from app.services.session_service import (
    get_escalation_queue,
    get_presence_service,
    get_session_service,
)

log = get_logger(__name__)


# ===========================================================================
# 触发条件矩阵
# ===========================================================================
@dataclass
class EscalationTrigger:
    """转人工触发判定结果。"""

    should_escalate: bool
    reasons: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    priority: int = 0        # >0 进高优先级队列
    amount: float = 0.0
    emotion_intensity: int = 0

    @property
    def reason_text(self) -> str:
        return "；".join(self.reasons)


def evaluate_escalation(
    *,
    step_count: int = 0,
    emotion: str = "neutral",
    emotion_intensity: int = 0,
    amount: float = 0.0,
    needs_admin: bool = False,
    complaint_count: int = 0,
    user_requested: bool = False,
    tenant_tier: str = "standard",
    explicit: bool = False,
) -> EscalationTrigger:
    """按矩阵判定是否转人工。

    矩阵取自架构文档 §9.1，阈值全部来自配置，便于调整与测试。
    """
    reasons: list[str] = []
    codes: list[str] = []
    priority = 0

    if user_requested:
        reasons.append("用户显式要求转人工")
        codes.append("USER_REQUESTED")
        priority = max(priority, 1)

    if step_count > settings.escalate_max_steps:
        reasons.append(f"已排查 {step_count} 步仍未解决（阈值 {settings.escalate_max_steps}）")
        codes.append("STEPS_EXCEEDED")

    if emotion == "angry" and emotion_intensity >= settings.escalate_emotion_intensity:
        reasons.append(f"情绪激烈（{emotion}，强度 {emotion_intensity}）")
        codes.append("EMOTION_ANGRY")
        priority = max(priority, 1)

    if amount > settings.escalate_amount_threshold:
        reasons.append(f"涉及金额 {amount:.2f} 元超过阈值 {settings.escalate_amount_threshold:.0f} 元")
        codes.append("AMOUNT_EXCEEDED")
        priority = max(priority, 1)

    if needs_admin:
        reasons.append("需要管理员介入（策略变更/重授权等）")
        codes.append("NEEDS_ADMIN")

    if complaint_count > settings.escalate_complaint_threshold:
        reasons.append(f"历史投诉 {complaint_count} 次超过阈值 {settings.escalate_complaint_threshold}")
        codes.append("COMPLAINTS_EXCEEDED")

    if explicit:
        reasons.append("流程内部显式要求转人工")
        codes.append("FLOW_EXPLICIT")

    if tenant_tier == "vip" and reasons:
        priority = max(priority, 1)
        reasons.append("VIP 租户优先派单")

    return EscalationTrigger(
        should_escalate=bool(reasons),
        reasons=reasons,
        codes=codes,
        priority=priority,
        amount=amount,
        emotion_intensity=emotion_intensity,
    )


# ===========================================================================
# 转人工服务
# ===========================================================================
class EscalationService:
    """转人工流转。"""

    def __init__(self) -> None:
        self.queue = get_escalation_queue()
        self.sessions = get_session_service()
        self.presence = get_presence_service()

    # ------------------------------------------------------------------
    def create(
        self,
        *,
        tenant_id: str,
        session_key: str,
        reason: str,
        trigger_codes: list[str],
        user_ext_id: str = "",
        skill: str = "",
        emotion: str = "neutral",
        emotion_intensity: int = 0,
        amount: float = 0.0,
        priority: int = 0,
        snapshot: str = "",
        session_pk: int | None = None,
    ) -> dict[str, Any]:
        """创建转人工记录并入队。"""
        request_no = f"ESC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{new_id()[:6].upper()}"

        with write_session() as s:
            row = Escalation(
                tenant_id=tenant_id,
                trace_id=get_trace_id() if get_trace_id() != "-" else "",
                request_no=request_no,
                session_id=session_pk or 0,
                user_ext_id=user_ext_id,
                skill=skill,
                reason=reason[:255],
                trigger_code=",".join(trigger_codes)[:48],
                emotion=emotion,
                emotion_intensity=emotion_intensity,
                amount=amount,
                priority="high" if priority > 0 else "normal",
                status="queued",
                snapshot_json=snapshot,
            )
            s.add(row)
            s.flush()
            escalation_id = int(row.id)

        # 会话状态机 → 排队中
        try:
            self.sessions.transition(
                tenant_id=tenant_id,
                session_key=session_key,
                to_state=SESSION_STATE_QUEUED,
                handler="QUEUE",
                reason=reason[:120],
            )
        except Exception as exc:  # noqa: BLE001
            warn_degraded("escalation.transition_failed", error=str(exc)[:200])

        # 入队
        self.queue.enqueue(
            tenant_id=tenant_id,
            session_key=session_key,
            priority=priority,
            payload={
                "request_no": request_no,
                "escalation_id": escalation_id,
                "skill": skill,
                "user_ext_id": user_ext_id,
                "reason": reason[:200],
                "priority_label": "high" if priority > 0 else "normal",
            },
        )

        # 建工单（附交接快照）
        ticket_no = self._create_ticket(
            tenant_id=tenant_id,
            session_pk=session_pk,
            user_ext_id=user_ext_id,
            skill=skill,
            reason=reason,
            snapshot=snapshot,
            priority="high" if priority > 0 else "normal",
            session_key=session_key,
        )

        # 推送坐席
        get_event_bus().publish_nowait(
            Event(
                type=EVT_ESCALATION_CREATED,
                tenant_id=tenant_id,
                payload={
                    "request_no": request_no,
                    "session_key": session_key,
                    "skill": skill,
                    "reason": reason[:200],
                    "priority": priority,
                    "ticket_no": ticket_no,
                    "queue_total": self.queue.total(tenant_id=tenant_id),
                },
            )
        )

        log.info(
            "escalation.created",
            request_no=request_no,
            session_key=session_key,
            skill=skill,
            priority=priority,
            ticket_no=ticket_no,
        )
        return {
            "request_no": request_no,
            "escalation_id": escalation_id,
            "ticket_no": ticket_no,
            "priority": priority,
            "reason": reason,
            "trigger_codes": trigger_codes,
            "snapshot": snapshot,
            "queue_total": self.queue.total(tenant_id=tenant_id),
        }

    # ------------------------------------------------------------------
    def _session_pk(self, *, tenant_id: str, session_key: str) -> int | None:
        """按业务会话标识取数据库主键。

        ``Escalation.session_id`` 存的是会话主键（不是 session_key），
        因此凡是要"找到本会话对应的转人工记录/工单"，都必须先把 key 换成主键；
        只按租户 + 状态取"最新一条"会关错别的会话（本项目实测踩到）。
        """
        with read_session() as s:
            return s.execute(
                select(SupportSession.id).where(
                    SupportSession.tenant_id == tenant_id,
                    SupportSession.session_key == session_key,
                )
            ).scalar_one_or_none()

    def _original_queue_item(
        self, *, tenant_id: str, session_key: str
    ) -> tuple[int, dict[str, Any]]:
        """取会话原本的队列优先级与关联信息（用于重新入队时保真）。"""
        pk = self._session_pk(tenant_id=tenant_id, session_key=session_key)
        if pk is None:
            return 0, {}
        with read_session() as s:
            row = s.execute(
                select(Escalation)
                .where(Escalation.tenant_id == tenant_id, Escalation.session_id == pk)
                .order_by(Escalation.id.desc())
            ).scalars().first()
        if row is None:
            return 0, {}
        return (
            1 if row.priority == "high" else 0,
            {
                "request_no": row.request_no,
                "escalation_id": int(row.id),
                "skill": row.skill,
                "user_ext_id": row.user_ext_id,
                "reason": row.reason[:200],
                "priority_label": row.priority,
            },
        )

    # ------------------------------------------------------------------
    def _create_ticket(
        self,
        *,
        tenant_id: str,
        session_pk: int | None,
        user_ext_id: str,
        skill: str,
        reason: str,
        snapshot: str,
        priority: str,
        session_key: str,
    ) -> str:
        """创建工单并写入快照。"""
        ticket_no = f"TK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{new_id()[:6].upper()}"
        with write_session() as s:
            t = Ticket(
                tenant_id=tenant_id,
                trace_id=get_trace_id() if get_trace_id() != "-" else "",
                ticket_no=ticket_no,
                session_id=session_pk,
                user_ext_id=user_ext_id,
                category=skill or "other",
                priority=priority,
                status="pending",
                title=f"[{skill or '通用'}] {reason[:80]}",
                description=reason,
                handover_snapshot=snapshot,
            )
            s.add(t)
            s.flush()
            s.add(
                TicketMessage(
                    tenant_id=tenant_id,
                    ticket_id=int(t.id),
                    role="system",
                    content=f"由 AI 转人工自动创建。触发原因：{reason}",
                )
            )
        return ticket_no

    # ------------------------------------------------------------------
    def dispatch_one(self, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        """从队列取一个会话并派单给最合适的在线坐席。

        派单策略：**技能组匹配 ∩ 在线 ∩ 负载最小**；同负载按用户名稳定排序（可复现）。

        队列按键共享，因此出队必须**按租户过滤**：否则一次派单可能取走
        另一个租户的会话，并用本租户的坐席去改动那条记录（跨租户写入）。
        """
        # 无显式租户时退化为"任意租户队首"：仅用于脚本/后台无上下文场景。
        # 常规调用（API/控制台）一律传显式租户。
        item = self.queue.dequeue(tenant_id=tenant_id)
        if not item:
            return None

        item_tid = str(item.get("tenant_id") or "")
        if tenant_id and item_tid and item_tid != tenant_id:
            # 防御：万一队列数据被外部写入成不一致的租户，宁可不派单也不跨租户写入
            warn_degraded(
                "escalation.queue_tenant_mismatch",
                expected=tenant_id,
                actual=item_tid,
                session_key=item.get("session_key", ""),
            )
            self.queue.enqueue(
                tenant_id=item_tid,
                session_key=item["session_key"],
                priority=item.get("priority", 0),
                payload={k: v for k, v in item.items() if k not in ("tenant_id", "session_key", "priority")},
            )
            return None
        tid = item_tid or tenant_id or get_tenant_id()
        skill = item.get("skill", "")

        # 派单可能由后台任务或脚本触发（无请求上下文），此处确保租户已绑定，
        # 否则数据库的租户闸门会拒绝查询坐席表。
        from app.core.trace_context import ensure_tenant

        with ensure_tenant(tid):
            candidates = self._rank_agents(tenant_id=tid, skill=skill)
            if not candidates:
                # 无可用坐席：放回队列（不能丢），并告警
                self.queue.enqueue(
                    tenant_id=tid,
                    session_key=item["session_key"],
                    priority=item.get("priority", 0),
                    payload={
                        k: v
                        for k, v in item.items()
                        if k not in ("tenant_id", "session_key", "priority")
                    },
                )
                warn_degraded("escalation.no_agent_available", tenant_id=tid, skill=skill)
                return {"assigned": False, "reason": "无可用坐席", "session_key": item["session_key"]}

            agent = candidates[0]
            request_no = item.get("request_no", "")
            session_key = item["session_key"]

            with write_session() as s:
                row = s.execute(
                    select(Escalation).where(
                        Escalation.tenant_id == tid, Escalation.request_no == request_no
                    )
                ).scalar_one_or_none()
                if row:
                    row.assigned_agent = agent["username"]
                    row.status = "assigned"
                    row.claimed_at = datetime.now(timezone.utc)

        get_event_bus().publish_nowait(
            Event(
                type=EVT_ESCALATION_ASSIGNED,
                tenant_id=tid,
                payload={
                    "request_no": request_no,
                    "session_key": session_key,
                    "agent": agent["username"],
                    "agent_name": agent["display_name"],
                    "skill": skill,
                    "queue_total": self.queue.total(tenant_id=tid),
                },
            )
        )
        log.info(
            "escalation.assigned",
            request_no=request_no,
            session_key=session_key,
            agent=agent["username"],
            skill=skill,
        )
        return {
            "assigned": True,
            "request_no": request_no,
            "session_key": session_key,
            "agent": agent["username"],
            "agent_name": agent["display_name"],
            "reason": item.get("reason", ""),
            "queue_total": self.queue.total(tenant_id=tid),
        }

    def _rank_agents(self, *, tenant_id: str, skill: str) -> list[dict[str, Any]]:
        """按技能组 + 在线 + 负载排序候选坐席。"""
        with read_session() as s:
            agents = s.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_active.is_(True))
            ).scalars().all()
            skill_rows = s.execute(
                select(AgentSkill).where(AgentSkill.tenant_id == tenant_id)
            ).scalars().all()

        skills_by_agent: dict[int, list[str]] = {}
        for r in skill_rows:
            skills_by_agent.setdefault(r.agent_id, []).append(r.skill)

        online = {
            p.agent: p
            for p in self.presence.online_agents(
                tenant_id=tenant_id, agents=[a.username for a in agents]
            )
        }

        candidates: list[dict[str, Any]] = []
        for a in agents:
            pr = online.get(a.username)
            if not pr:
                continue  # 不在线（心跳过期）不参与派单
            skills = skills_by_agent.get(a.id, [])
            # 技能匹配：优先匹配者，其次全能
            matches = (not skill) or (skill in skills)
            candidates.append(
                {
                    "username": a.username,
                    "display_name": a.display_name,
                    "skills": skills,
                    "load": pr.load,
                    "matched": matches,
                    "role": a.role,
                }
            )

        # 排序：技能匹配优先 → 负载小优先 → 用户名稳定排序
        candidates.sort(key=lambda c: (not c["matched"], c["load"], c["username"]))
        return candidates

    # ------------------------------------------------------------------
    def claim(
        self, *, tenant_id: str, session_key: str, agent: str, display_name: str = ""
    ) -> dict[str, Any]:
        """坐席接管会话：抢占会话归属，成功后 AI 停止介入。

        **关于锁的作用域**：分布式锁只保护"检查-切换"这一步的原子性，
        持锁时间极短（毫秒级），因为会话本身要跨越整段人工服务时长，
        不可能一直持锁。

        因此"同一会话只能有一个处理方"这条约束不能只靠锁，必须叠加
        **业务层归属校验**：先看会话当前处理方，已经被别人接管就直接拒绝。
        只靠锁会漏（锁释放后第二个坐席仍能接管，本项目实测踩到）。
        """
        lock_key = key_session_lock(f"{tenant_id}:{session_key}")
        with distributed_lock(lock_key, ttl_seconds=settings.agent_lock_ttl_seconds) as token:
            if token is None:
                current = self.sessions.owner(tenant_id=tenant_id, session_key=session_key)
                log.warning(
                    "session.claim_rejected",
                    session_key=session_key,
                    agent=agent,
                    current_owner=current,
                    cause="lock_busy",
                )
                return {
                    "claimed": False,
                    "reason": "会话正在被其他处理方操作，请稍后重试",
                    "current_owner": current,
                }

            # 业务层归属校验（锁内检查，避免并发下两个坐席同时通过）
            info = self.sessions.get_or_create(tenant_id=tenant_id, session_key=session_key)
            current_owner = info.handler
            if info.state == SESSION_STATE_CLOSED:
                # 已办结的会话不能再被接管。若直接调 transition，会撞上
                # "CLOSED → AGENT_HANDLING 不是合法的状态跃迁"并冒泡成 500——
                # 这是**可预期的业务拒绝**，应当返回可读的 4xx 语义而不是内部错误
                # （坐席重复点击"接管"、或看板数据滞后时就会遇到）。
                log.warning(
                    "session.claim_rejected",
                    session_key=session_key,
                    agent=agent,
                    cause="already_closed",
                )
                return {
                    "claimed": False,
                    "reason": "会话已办结，无法接管",
                    "current_owner": current_owner,
                }
            if info.state == SESSION_STATE_AGENT and current_owner and current_owner != agent:
                log.warning(
                    "session.claim_rejected",
                    session_key=session_key,
                    agent=agent,
                    current_owner=current_owner,
                    cause="already_claimed",
                )
                return {
                    "claimed": False,
                    "reason": f"会话已被坐席 {current_owner} 接管",
                    "current_owner": current_owner,
                }

            info = self.sessions.transition(
                tenant_id=tenant_id,
                session_key=session_key,
                to_state=SESSION_STATE_AGENT,
                handler=agent,
                reason=f"坐席 {agent} 接管",
            )
            # 队列里若有残留记录，移除
            self.queue.remove(tenant_id=tenant_id, session_key=session_key)
            # 坐席负载 +1
            self.presence.incr_load(tenant_id=tenant_id, agent=agent, delta=1)

            # 必须绑定到**本会话**：session_id 存的是会话主键，
            # 早期误写成 == 0（占位值），导致接管后转人工记录永远留在
            # queued/assigned，既被重复派单又让"待处理"计数不降。
            # 读主键放在 write_session 之外，避免读写会话嵌套。
            pk = self._session_pk(tenant_id=tenant_id, session_key=session_key)
            with write_session() as s:
                row = None
                if pk is not None:
                    row = s.execute(
                        select(Escalation)
                        .where(
                            Escalation.tenant_id == tenant_id,
                            Escalation.session_id == pk,
                            Escalation.status.in_(("queued", "assigned")),
                        )
                        .order_by(Escalation.id.desc())
                    ).scalars().first()
                if row is not None:
                    row.status = "handling"
                    row.assigned_agent = row.assigned_agent or agent
                    row.claimed_at = row.claimed_at or datetime.now(timezone.utc)

        get_event_bus().publish_nowait(
            Event(
                type=EVT_SESSION_CLAIMED,
                tenant_id=tenant_id,
                payload={
                    "session_key": session_key,
                    "agent": agent,
                    "agent_name": display_name or agent,
                    "handler": info.handler,
                    "state": info.state,
                },
            )
        )
        log.info("session.claimed", session_key=session_key, agent=agent, state=info.state)
        return {
            "claimed": True,
            "session_key": session_key,
            "agent": agent,
            "state": info.state,
            "lock_token": token,
        }

    # ------------------------------------------------------------------
    def close(
        self,
        *,
        tenant_id: str,
        session_key: str,
        agent: str,
        resolution: str,
        skill: str = "",
        diagnosis_code: str = "",
        fix_action: str = "",
        automatable: bool = False,
        promote_to_kb: bool = False,
        session_pk: int | None = None,
        escalation_no: str = "",
    ) -> dict[str, Any]:
        """坐席办结：写结论、算时长、可选回流案例库（ADR-006 闭环）。"""
        now = datetime.now(timezone.utc)
        info = self.sessions.get_or_create(tenant_id=tenant_id, session_key=session_key)
        handle_seconds = (now.timestamp() - info.claimed_at) if info.claimed_at else 0.0
        # 办结必须绑定到**本会话**的工单与转人工记录：
        # 只按"租户 + 未关闭"取最新一条，会在同租户存在多个进行中会话时
        # 把别人的工单/记录关掉并盖上错误的结论与坐席（本项目实测踩到）。
        pk = session_pk or self._session_pk(tenant_id=tenant_id, session_key=session_key)
        wait_seconds = 0.0
        if info.queued_at:
            wait_seconds = max(0.0, (info.claimed_at or now.timestamp()) - info.queued_at)

        with write_session() as s:
            # 办结会话记录
            row = s.execute(
                select(SupportSession).where(
                    SupportSession.tenant_id == tenant_id,
                    SupportSession.session_key == session_key,
                )
            ).scalar_one_or_none()
            if row is not None:
                row.closed_at = now
                row.duration_seconds = float(handle_seconds)
                row.escalated = True
                row.resolved_by_ai = False
                row.summary = resolution[:500]

            # 办结工单（按本会话主键定位）
            ticket = None
            if pk is not None:
                ticket = s.execute(
                    select(Ticket)
                    .where(Ticket.tenant_id == tenant_id, Ticket.session_id == pk)
                    .order_by(Ticket.id.desc())
                ).scalars().first()
            if ticket is not None and ticket.status != "closed":
                ticket.status = "closed"
                ticket.closed_at = now
                ticket.resolved_at = now
                ticket.resolution = resolution
                ticket.handle_seconds = float(handle_seconds)
                ticket.assigned_agent = agent
                s.add(
                    TicketMessage(
                        tenant_id=tenant_id,
                        ticket_id=int(ticket.id),
                        role="agent",
                        content=f"办结结论：{resolution}",
                    )
                )

            # 转人工记录办结（按本会话主键定位）
            esc = None
            if pk is not None:
                esc = s.execute(
                    select(Escalation)
                    .where(Escalation.tenant_id == tenant_id, Escalation.session_id == pk)
                    .order_by(Escalation.id.desc())
                ).scalars().first()
            if esc is not None:
                esc.status = "closed"
                esc.closed_at = now
                # 等待时长 = 认领时刻 - 入队时刻。
                # 早期写成 (claimed_at or now) 减自身，恒为 0，等待时长指标失去意义。
                esc.wait_seconds = float(wait_seconds)
                esc.assigned_agent = esc.assigned_agent or agent

            # 坐席绩效
            ag = s.execute(
                select(Agent).where(Agent.tenant_id == tenant_id, Agent.username == agent)
            ).scalar_one_or_none()
            if ag is not None:
                total = ag.avg_handle_seconds * ag.handled_sessions + float(handle_seconds)
                ag.handled_sessions += 1
                ag.avg_handle_seconds = total / ag.handled_sessions

        # 会话状态机 → 已关闭
        try:
            self.sessions.close(tenant_id=tenant_id, session_key=session_key, reason="坐席办结")
        except Exception as exc:  # noqa: BLE001
            warn_degraded("escalation.close_transition_failed", error=str(exc)[:200])
        self.presence.incr_load(tenant_id=tenant_id, agent=agent, delta=-1)

        # 回流案例库（人工解决过的问题沉淀为 AI 知识）
        feedback_id = None
        if promote_to_kb and diagnosis_code:
            feedback_id = self._record_feedback(
                tenant_id=tenant_id,
                session_pk=session_pk,
                escalation_no=escalation_no,
                skill=skill,
                diagnosis_code=diagnosis_code,
                fix_action=fix_action,
                automatable=automatable,
                agent_name=agent,
                promote=True,
            )

        get_event_bus().publish_nowait(
            Event(
                type=EVT_SESSION_CLOSED,
                tenant_id=tenant_id,
                payload={
                    "session_key": session_key,
                    "agent": agent,
                    "handle_seconds": round(handle_seconds, 1),
                    "resolution": resolution[:200],
                    "promoted": bool(feedback_id),
                },
            )
        )
        log.info(
            "escalation.closed",
            session_key=session_key,
            agent=agent,
            handle_seconds=round(handle_seconds, 1),
            promoted=bool(feedback_id),
        )
        return {
            "closed": True,
            "session_key": session_key,
            "handle_seconds": round(handle_seconds, 1),
            "feedback_id": feedback_id,
            "promoted": bool(feedback_id),
        }

    # ------------------------------------------------------------------
    def _record_feedback(
        self,
        *,
        tenant_id: str,
        session_pk: int | None,
        escalation_no: str,
        skill: str,
        diagnosis_code: str,
        fix_action: str,
        automatable: bool,
        agent_name: str,
        promote: bool,
    ) -> int:
        """记录人工办结回流（并可标记为已提升进知识库）。"""
        from app.models import CaseFeedback

        with write_session() as s:
            fb = CaseFeedback(
                tenant_id=tenant_id,
                trace_id=get_trace_id() if get_trace_id() != "-" else "",
                escalation_no=escalation_no,
                session_id=session_pk,
                skill=skill,
                diagnosis_code=diagnosis_code,
                fix_action=fix_action[:255],
                automatable=automatable,
                agent_name=agent_name,
                promoted=promote,
                notes=f"由坐席 {agent_name} 办结后沉淀",
            )
            s.add(fb)
            s.flush()
            return int(fb.id)

    # ------------------------------------------------------------------
    def detect_offline_agents(self, *, tenant_id: str, agents: list[str]) -> list[str]:
        """检测掉线坐席（心跳过期）。

        返回值是"曾被认为在线、现在心跳已过期"的坐席名。
        """
        ttl_map = self.presence.all_with_ttl(tenant_id=tenant_id, agents=agents)
        offline = [a for a, ttl in ttl_map.items() if ttl < 0]
        return offline

    def requeue_agent_sessions(self, *, tenant_id: str, agent: str) -> list[str]:
        """坐席掉线后，把其名下会话重新入队。

        实现要点：**通过状态机跃迁**（``transition``）而不是直接改数据库。
        状态机同时更新 Redis（实时权威）与数据库（审计/查询），
        保证两个存储不出现分叉；只改其中一边会导致"库里已重排队、Redis 仍显示
        坐席处理中"，路由判断随即出错（本项目实测踩到）。
        """
        requeued: list[str] = []
        with read_session() as s:
            rows = s.execute(
                select(SupportSession).where(
                    SupportSession.tenant_id == tenant_id,
                    SupportSession.handler == agent,
                    SupportSession.state == SESSION_STATE_AGENT,
                )
            ).scalars().all()
            session_keys = [row.session_key for row in rows]

        for key in session_keys:
            try:
                self.sessions.transition(
                    tenant_id=tenant_id,
                    session_key=key,
                    to_state=SESSION_STATE_QUEUED,
                    handler="QUEUE",
                    reason=f"坐席 {agent} 掉线，会话重新排队",
                )
                requeued.append(key)
            except Exception as exc:  # noqa: BLE001 - 单条失败不应影响其余会话回收
                warn_degraded("escalation.requeue_failed", session_key=key, error=str(exc)[:150])

        for key in requeued:
            # 保留原有优先级与关联信息：硬编码 priority=1 会让掉线回收的会话
            # 插到真正的高优先级（VIP/高金额）之前，且丢失 request_no，
            # 使后续按 request_no 的核对/回滚失效。
            priority, payload = self._original_queue_item(tenant_id=tenant_id, session_key=key)
            payload.setdefault("reason", f"坐席 {agent} 掉线，会话重新排队")
            self.queue.enqueue(
                tenant_id=tenant_id,
                session_key=key,
                priority=priority,
                payload=payload,
            )
        if requeued:
            get_event_bus().publish_nowait(
                Event(
                    type=EVT_AGENT_DISCONNECTED,
                    tenant_id=tenant_id,
                    payload={"agent": agent, "requeued": requeued, "count": len(requeued)},
                )
            )
            log.warning("agent.disconnected", agent=agent, requeued=len(requeued))
        return requeued

    # ------------------------------------------------------------------
    def queue_view(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """待接入队列（工作台展示）。**只展示本租户**，否则会泄露其它租户的会话标识。"""
        return self.queue.peek(limit=50, tenant_id=tenant_id)

    def list_escalations(self, *, tenant_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """转人工记录列表（走从库，只读查询）。"""
        with read_session() as s:
            stmt = select(Escalation).where(Escalation.tenant_id == tenant_id)
            if status:
                stmt = stmt.where(Escalation.status == status)
            rows = s.execute(stmt.order_by(Escalation.id.desc()).limit(limit)).scalars().all()
            return [r.to_dict() for r in rows]

    def pending_count(self, *, tenant_id: str) -> int:
        """待处理转人工数量。"""
        with read_session() as s:
            return int(
                s.execute(
                    select(func.count(Escalation.id)).where(
                        Escalation.tenant_id == tenant_id,
                        Escalation.status.in_(("queued", "assigned", "handling")),
                    )
                ).scalar()
                or 0
            )


_service: EscalationService | None = None


def get_escalation_service() -> EscalationService:
    """获取转人工服务单例。"""
    global _service
    if _service is None:
        _service = EscalationService()
    return _service


# ===========================================================================
# 掉线坐席巡检（供接口与后台定时任务共用）
# ===========================================================================
def sweep_offline_agents(*, tenant_id: str) -> dict[str, Any]:
    """巡检一个租户：把"名下有进行中会话但心跳已过期"的坐席的会话回收重排队。

    **幂等**：无可回收会话时返回空列表，可安全地按固定周期反复调用。

    为什么必须是独立的巡检而不是只靠心跳接口：坐席主动下线会调
    ``/api/v1/agent/heartbeat`` 立即回收，但进程崩溃/断网**不会发出任何请求**，
    那些会话会永远停在 ``AGENT_HANDLING``——AI 已停止介入、坐席又不在，
    用户消息无人处理。因此需要"没人调用也要有人检查"的机制。
    """
    from app.core.trace_context import bind

    svc = get_escalation_service()
    with bind(tenant_id=tenant_id, trace_id="offline-sweep"):
        with read_session() as s:
            holders = [
                h
                for h in s.execute(
                    select(SupportSession.handler)
                    .where(
                        SupportSession.tenant_id == tenant_id,
                        SupportSession.state == SESSION_STATE_AGENT,
                    )
                    .distinct()
                ).scalars().all()
                if h and h != "AI"
            ]
        if not holders:
            return {"tenant_id": tenant_id, "checked": [], "offline": [], "requeued": []}

        offline = svc.detect_offline_agents(tenant_id=tenant_id, agents=holders)
        requeued: list[str] = []
        for agent in offline:
            requeued.extend(svc.requeue_agent_sessions(tenant_id=tenant_id, agent=agent))
        if requeued:
            log.warning(
                "agent.offline_swept",
                tenant_id=tenant_id,
                offline=offline,
                requeued=len(requeued),
            )
        return {
            "tenant_id": tenant_id,
            "checked": holders,
            "offline": offline,
            "requeued": requeued,
        }


def sweep_all_tenants() -> dict[str, Any]:
    """对所有**启用中**的租户各做一次掉线巡检。

    租户清单读主库（``tenants`` 表不参与租户闸门），
    这样新租户无需改配置即可被纳入巡检。
    """
    with read_session() as s:
        tenant_keys = [
            str(k)
            for k in s.execute(
                select(Tenant.tenant_key).where(Tenant.is_active.is_(True))
            ).scalars().all()
            if k
        ]

    checked = offline = requeued = 0
    for tid in tenant_keys:
        result = sweep_offline_agents(tenant_id=tid)
        checked += len(result["checked"])
        offline += len(result["offline"])
        requeued += len(result["requeued"])
    return {
        "tenants": tenant_keys,
        "checked_agents": checked,
        "offline_agents": offline,
        "requeued": requeued,
    }
