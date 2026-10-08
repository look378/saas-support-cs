"""坐席端 API：登录、在线状态、队列、接管、对话、办结。

坐席接口全部要求**坐席令牌**（``aud=agent``），且令牌租户必须与请求头一致
（在网关的 ``AuthMiddleware`` 中校验）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.database import read_primary_session, read_session, write_session
from app.core.logging_config import get_logger
from app.core.trace_context import get_tenant_id
from app.models import Agent, AgentSkill, Message, SupportSession
from app.models.conversation import SESSION_STATE_AGENT
from app.security import issue_token, verify_password
from app.services.escalation_service import get_escalation_service
from app.services.event_bus import EVT_AGENT_PRESENCE, Event, get_event_bus
from app.services.session_service import get_presence_service, get_session_service

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1/agent", tags=["坐席端"])


# ===========================================================================
# 登录
# ===========================================================================
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    ok: bool = True
    token: str
    agent: str
    display_name: str
    role: str
    tenant_id: str
    skills: list[str] = []
    expires_in_minutes: int = 0


@router.post("/login", response_model=LoginResponse, summary="坐席登录")
async def login(payload: LoginRequest) -> LoginResponse:
    tenant_id = get_tenant_id()
    # 凭据校验必须读**主库**：走从库时，刚被停用的坐席或刚改过的口令
    # 会因复制延迟在短时间内仍然通过验证（安全判定不能建立在可能过期的快照上）。
    with read_primary_session() as s:
        agent = s.execute(
            select(Agent).where(
                Agent.tenant_id == tenant_id,
                Agent.username == payload.username,
                Agent.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if agent is None or not verify_password(payload.password, agent.password_hash):
            # 统一错误信息，不区分"用户不存在"与"口令错误"，避免账号枚举
            log.warning("agent.login_failed", username=payload.username, tenant_id=tenant_id)
            raise HTTPException(status_code=401, detail="用户名或口令不正确")
        skills = [
            r.skill
            for r in s.execute(
                select(AgentSkill).where(
                    AgentSkill.tenant_id == tenant_id, AgentSkill.agent_id == agent.id
                )
            ).scalars().all()
        ]
        display_name, role = agent.display_name, agent.role

    token = issue_token(
        subject=payload.username, tenant_id=tenant_id, audience="agent", role=role
    )
    log.info("agent.login_ok", username=payload.username, tenant_id=tenant_id, role=role)
    return LoginResponse(
        token=token,
        agent=payload.username,
        display_name=display_name,
        role=role,
        tenant_id=tenant_id,
        skills=skills,
        expires_in_minutes=settings.jwt_agent_ttl_minutes,
    )


# ===========================================================================
# 在线状态与心跳
# ===========================================================================
class HeartbeatRequest(BaseModel):
    presence: str = Field(default="online", description="online / break / offline")


@router.post("/heartbeat", summary="上报心跳（建议每 10 秒一次，TTL 30 秒）")
async def heartbeat(request: Request, payload: HeartbeatRequest) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    agent = getattr(request.state, "agent", "")
    if not agent:
        raise HTTPException(status_code=401, detail="缺少坐席身份")

    presence = get_presence_service()
    if payload.presence == "offline":
        presence.set_offline(tenant_id=tenant_id, agent=agent)
        # 主动下线也回收其会话
        requeued = get_escalation_service().requeue_agent_sessions(tenant_id=tenant_id, agent=agent)
        return {"ok": True, "data": {"presence": "offline", "requeued": requeued}}

    skills = _agent_skills(tenant_id=tenant_id, agent=agent)
    pr = presence.heartbeat(
        tenant_id=tenant_id, agent=agent, skills=skills, presence=payload.presence
    )
    get_event_bus().publish_nowait(
        Event(
            type=EVT_AGENT_PRESENCE,
            tenant_id=tenant_id,
            payload={"agent": agent, "presence": pr.presence, "load": pr.load, "ttl": pr.ttl},
        )
    )
    return {"ok": True, "data": {"presence": pr.presence, "load": pr.load, "ttl": pr.ttl}}


def _agent_skills(*, tenant_id: str, agent: str) -> list[str]:
    with read_session() as s:
        row = s.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.username == agent)
        ).scalar_one_or_none()
        if row is None:
            return []
        return [
            r.skill
            for r in s.execute(
                select(AgentSkill).where(
                    AgentSkill.tenant_id == tenant_id, AgentSkill.agent_id == row.id
                )
            ).scalars().all()
        ]


@router.get("/presence", summary="坐席在线状态总览")
async def presence_overview() -> dict[str, Any]:
    tenant_id = get_tenant_id()
    with read_session() as s:
        agents = s.execute(
            select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_active.is_(True))
        ).scalars().all()
        names = [a.username for a in agents]
    presence = get_presence_service()
    ttl_map = presence.all_with_ttl(tenant_id=tenant_id, agents=names)
    data = []
    for a in agents:
        pr = presence.get(tenant_id=tenant_id, agent=a.username)
        data.append(
            {
                "agent": a.username,
                "display_name": a.display_name,
                "role": a.role,
                "online": bool(pr and pr.presence in ("online", "break")),
                "presence": pr.presence if pr else "offline",
                "load": pr.load if pr else 0,
                "heartbeat_ttl": ttl_map.get(a.username, -2),
                "handled_sessions": a.handled_sessions,
                "avg_handle_seconds": round(a.avg_handle_seconds, 1),
            }
        )
    return {"ok": True, "data": data, "total": len(data)}


@router.post("/offline-recovery", summary="掉线坐席会话回收（手动触发一次巡检）")
async def offline_recovery() -> dict[str, Any]:
    """巡检本租户内"名下有进行中会话但心跳已过期"的坐席，并回收其会话。

    正常情况下这件事由 API 进程内的**定时巡检**自动完成
    （见 ``AGENT_OFFLINE_SWEEP_SECONDS``）；本接口保留为手动入口，
    用于运维即时触发或定时器被关闭时的兜底。幂等，可重复调用。
    """
    from app.services.escalation_service import sweep_offline_agents

    result = sweep_offline_agents(tenant_id=get_tenant_id())
    return {
        "ok": True,
        "data": {
            "checked": result["checked"],
            "offline": result["offline"],
            "requeued": result["requeued"],
        },
    }


# ===========================================================================
# 队列与接管
# ===========================================================================
@router.get("/queue", summary="待接入队列（实时）")
async def queue_view() -> dict[str, Any]:
    tenant_id = get_tenant_id()
    esc = get_escalation_service()
    items = esc.queue_view(tenant_id=tenant_id)
    return {
        "ok": True,
        "data": items,
        "total": len(items),
        "queue_size": esc.queue.size(tenant_id=tenant_id),
        # 数据库口径的未结转入工单数：与 Redis 队列长度互为校验——
        # 两者长期不一致说明有队列/落库分叉（Redis 丢件或状态未回写）。
        "db_pending": esc.pending_count(tenant_id=tenant_id),
    }


class ClaimRequest(BaseModel):
    session_key: str


@router.post("/claim", summary="接管会话（分布式锁抢会话）")
async def claim(request: Request, payload: ClaimRequest) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    agent = getattr(request.state, "agent", "")
    if not agent:
        raise HTTPException(status_code=401, detail="缺少坐席身份")

    result = get_escalation_service().claim(
        tenant_id=tenant_id, session_key=payload.session_key, agent=agent
    )
    if not result.get("claimed"):
        # 抢不到锁是可预期的业务结果，返回 409 而不是 500
        raise HTTPException(status_code=409, detail=result.get("reason", "接管失败"))
    return {"ok": True, "data": result}


class AgentMessageRequest(BaseModel):
    session_key: str
    content: str


@router.post("/message", summary="坐席发送消息")
async def agent_message(request: Request, payload: AgentMessageRequest) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    agent = getattr(request.state, "agent", "")
    sessions = get_session_service()
    info = sessions.get_or_create(tenant_id=tenant_id, session_key=payload.session_key)
    if info.handler != agent:
        raise HTTPException(status_code=409, detail=f"会话当前由 {info.handler} 处理，您无法发言")

    with write_session() as s:
        row = s.execute(
            select(SupportSession).where(
                SupportSession.tenant_id == tenant_id,
                SupportSession.session_key == payload.session_key,
            )
        ).scalar_one_or_none()
        if row is not None:
            s.add(
                Message(
                    tenant_id=tenant_id,
                    session_id=int(row.id),
                    role="agent",
                    content=payload.content,
                )
            )

    get_event_bus().publish_nowait(
        Event(
            type="session.message",
            tenant_id=tenant_id,
            payload={
                "session_key": payload.session_key,
                "role": "agent",
                "content": payload.content,
                "agent": agent,
            },
        )
    )
    return {"ok": True, "data": {"delivered": True}}


class CloseRequest(BaseModel):
    session_key: str
    resolution: str = Field(description="办结结论")
    skill: str = ""
    diagnosis_code: str = ""
    fix_action: str = ""
    automatable: bool = False
    promote_to_kb: bool = Field(default=False, description="是否沉淀进知识库（回流闭环）")


@router.post("/close", summary="办结会话（可选回流知识库）")
async def close_session(request: Request, payload: CloseRequest) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    agent = getattr(request.state, "agent", "")
    if not agent:
        raise HTTPException(status_code=401, detail="缺少坐席身份")

    result = get_escalation_service().close(
        tenant_id=tenant_id,
        session_key=payload.session_key,
        agent=agent,
        resolution=payload.resolution,
        skill=payload.skill,
        diagnosis_code=payload.diagnosis_code,
        fix_action=payload.fix_action,
        automatable=payload.automatable,
        promote_to_kb=payload.promote_to_kb,
    )
    return {"ok": True, "data": result}


@router.get("/escalations", summary="转人工记录列表")
async def list_escalations(status: str = "", limit: int = 50) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    rows = get_escalation_service().list_escalations(
        tenant_id=tenant_id, status=status or None, limit=limit
    )
    return {"ok": True, "data": rows, "total": len(rows)}


# ===========================================================================
# 指标看板（坐席端）
# ===========================================================================
# 为什么挂在坐席路由下而不是用户路由下：
# 指标与链路回放属于**运营数据**，只有坐席/组长可看。
# 用户端与坐席端使用不同受众的 JWT，若把坐席令牌发往用户端路径，
# 受众校验会直接拒绝（这是刻意的安全设计）。
# 因此这类接口必须挂在坐席前缀下，用坐席令牌访问。
@router.get("/metrics/overview", summary="核心指标总览")
async def metrics_overview(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().overview(tenant_id=get_tenant_id(), days=days)}


@router.get("/metrics/trend", summary="按天趋势")
async def metrics_trend(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().trend(tenant_id=get_tenant_id(), days=days)}


@router.get("/metrics/skills", summary="分技能对比")
async def metrics_skills(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().skill_comparison(tenant_id=get_tenant_id(), days=days)}


@router.get("/metrics/escalation-reasons", summary="转人工原因分布")
async def metrics_escalation_reasons(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().escalation_reasons(tenant_id=get_tenant_id(), days=days)}


@router.get("/metrics/agents", summary="坐席维度指标")
async def metrics_agents() -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().agent_metrics(tenant_id=get_tenant_id())}


@router.get("/metrics/llm", summary="模型调用计量")
async def metrics_llm(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().llm_usage(tenant_id=get_tenant_id(), days=days)}


@router.get("/metrics/compare", summary="AI 实测 vs 行业基准区间")
async def metrics_compare(days: int = 7) -> dict[str, Any]:
    from app.services.metrics_service import get_metrics_service

    return {"ok": True, "data": get_metrics_service().compare_with_industry(tenant_id=get_tenant_id(), days=days)}


# ===========================================================================
# 链路回放（坐席端）
# ===========================================================================
@router.get("/replay/traces", summary="最近诊断链路列表")
async def replay_recent(limit: int = 20) -> dict[str, Any]:
    from app.services.replay_service import get_replay_service

    return {"ok": True, "data": get_replay_service().recent_traces(tenant_id=get_tenant_id(), limit=limit)}


@router.get("/replay/trace/{trace_id}", summary="按 trace_id 回放完整链路")
async def replay_trace(trace_id: str) -> dict[str, Any]:
    from app.services.replay_service import get_replay_service

    data = get_replay_service().by_trace(tenant_id=get_tenant_id(), trace_id=trace_id)
    if not data.get("found"):
        raise HTTPException(status_code=404, detail="未找到该 trace_id 的记录")
    return {"ok": True, "data": data}


@router.get("/replay/session/{session_key}", summary="按会话还原消息流与状态跃迁")
async def replay_session(session_key: str) -> dict[str, Any]:
    from app.services.replay_service import get_replay_service

    data = get_replay_service().session_timeline(
        tenant_id=get_tenant_id(), session_key=session_key
    )
    if not data.get("found"):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "data": data}
