"""用户端 API：对话、会话、工单、健康检查。

对话入口会按顺序做四件事：
  1. 载入/建立会话（Redis 状态机 + 数据库记录）
  2. 判断当前处理方——**若已转人工，AI 不再介入**（把消息转给坐席而不是抢答）
  3. 调用隔离层执行诊断
  4. 需要转人工则走真实流转（入队 + 派单 + 推送）

第 2 步是"AI 停止介入"这条约束的落地位置：会话一旦被坐席接管，
后续消息必须走人工通道，否则会出现 AI 与坐席同时回复用户。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.database import read_session, write_session
from app.core.logging_config import get_logger
from app.core.trace_context import get_tenant_id, get_trace_id
from app.models import Message, SupportSession, Ticket, TicketMessage
from app.models.conversation import SESSION_STATE_AGENT
from app.security import TokenError, decode_user_token, issue_user_token
from app.services.agent_service import AgentUnavailable, get_agent_service
from app.services.escalation_service import evaluate_escalation, get_escalation_service
from app.services.event_bus import EVT_MESSAGE, Event, get_event_bus
from app.services.session_service import get_session_service

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["用户端"])


# ===========================================================================
# 请求 / 响应模型
# ===========================================================================
class ChatRequest(BaseModel):
    # 长度上限与数据库列宽对齐（session_key 为 String(64)）：
    # 不设限会让超长输入在插入时抛 DataError，表现为 500 而不是 422。
    session_key: str = Field(default="", max_length=64, description="会话标识；留空则新建")
    message: str = Field(min_length=1, max_length=8000, description="用户消息")
    user_ext_id: str = Field(default="", max_length=64, description="终端用户标识")
    workspace_id: str = Field(default="", max_length=64, description="工作区（集成类问题用）")
    # 会话级访客令牌：首轮由服务端签发并在响应中返回，之后必须回传。
    # 不放在请求头是为了让"首轮"与"续聊"共用同一个调用形状（前端更简单），
    # 同时也便于 SSE 这种用 Query 参数传参的场景复用同一套身份解析。
    user_token: str = Field(default="", max_length=2048, description="会话级访客令牌")


class ClarifyOption(BaseModel):
    id: str
    label: str
    impact: str = ""
    recommended: bool = False


class ChatResponse(BaseModel):
    ok: bool = True
    session_key: str
    # 首轮（或即将过期时）签发，前端保存后每次请求回传。
    # 有了它，服务端不再依赖请求体自报的 user_ext_id 来判定"你是谁"。
    user_token: str = ""
    reply: str
    skill: str = ""
    outcome: str = ""
    cause_code: str = ""
    cause_text: str = ""
    confidence: float = 0.0
    risk_score: float = 0.0
    decision: str = ""
    escalate: bool = False
    escalate_reasons: list[str] = []
    evidence: list[str] = []
    actions: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    verify_ok: bool = False
    verify_detail: str = ""
    node_path: list[str] = []
    degraded: bool = False
    degrade_reason: str = ""
    pending_clarify: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None
    handler: str = "AI"
    state: str = ""
    trace_id: str = ""
    duration_ms: float = 0.0


# ===========================================================================
# 会话持久化辅助
# ===========================================================================
def _user_token_identity(
    *, tenant_id: str, session_key: str, user_token: str
) -> tuple[str, str]:
    """校验会话令牌并返回 ``(身份, 令牌绑定的会话标识)``。

    **会话绑定必须在这里核对**：令牌不绑会话时，"自己的合法令牌 + 他人的会话标识"
    可以操作他人会话（安全审查实测到该路径）。
    """
    try:
        claims = decode_user_token(user_token, tenant_id=tenant_id)
    except TokenError as exc:
        log.warning("user.token_rejected", tenant_id=tenant_id, kind=exc.kind)
        raise HTTPException(status_code=401, detail=f"用户令牌无效：{exc}") from exc

    bound = str(claims.session_key or "")
    if bound and bound != session_key:
        log.warning(
            "user.token_session_mismatch",
            tenant_id=tenant_id,
            bound_session=bound,
            requested_session=session_key,
        )
        raise HTTPException(status_code=403, detail="令牌与目标会话不匹配")
    return str(claims.subject), bound


def _resolve_user_identity(
    *,
    tenant_id: str,
    session_key: str,
    user_ext_id: str,
    user_token: str,
) -> tuple[str, bool]:
    """解析调用方身份。返回 ``(user_ext_id, 是否已认证)``。

    判定顺序（**先令牌、后自报**，因为令牌才是可信来源）：

    1. 带令牌且令牌有效、且令牌绑定的就是本会话 → 以令牌身份为准；
       若同时自报了**不同**身份，说明调用方在试图冒充，直接拒绝。
    2. 带令牌但无效/过期/不属于本会话 → 拒绝（不静默降级为"未认证"，
       否则令牌形同虚设）。
    3. 不带令牌且**会话已存在归属** → 拒绝。这是本机制的关键一环：
       会话建立后必须凭令牌访问，否则"省略身份字段"就是一个绕过开关
       （安全审查实测确认过该绕过路径）。
    4. 不带令牌且会话无归属（首次调用/历史会话）→ 放行并按自报身份建立绑定，
       由 ``/chat`` 在响应里签发令牌，此后走第 1 条。

    第 4 条是"首次绑定"：本系统没有用户登录页，首轮身份无从核验，
    因此**首轮自报 + 立刻签发令牌**是当前可做到的最强约束；
    真正的身份核验需要引入用户登录（已列入未完成清单）。
    """
    if user_token:
        token_identity, _bound = _user_token_identity(
            tenant_id=tenant_id, session_key=session_key, user_token=user_token
        )
        declared = (user_ext_id or "").strip()
        if declared and declared != token_identity:
            log.warning(
                "user.identity_mismatch",
                tenant_id=tenant_id,
                session_key=session_key,
                token_identity=token_identity,
                declared=declared,
            )
            raise HTTPException(status_code=403, detail="身份与令牌不一致")
        return token_identity, True

    owner = _current_owner(tenant_id=tenant_id, session_key=session_key)
    if owner:
        log.warning(
            "user.token_required",
            tenant_id=tenant_id,
            session_key=session_key,
            owner=owner,
        )
        raise HTTPException(
            status_code=401,
            detail="该会话已建立，请携带会话令牌访问（首轮响应中的 user_token）",
        )
    return (user_ext_id or "").strip(), False


def _current_owner(*, tenant_id: str, session_key: str) -> str:
    """会话当前记录的归属身份（无会话或无归属则为空）。"""
    info = get_session_service().peek(tenant_id=tenant_id, session_key=session_key)
    return str(getattr(info, "user_ext_id", "") or "")


def _issue_user_token(
    *, tenant_id: str, session_key: str, user_ext_id: str, authenticated: bool
) -> str:
    """按需签发访客令牌（已认证则不再重复签发）。"""
    if authenticated or not user_ext_id:
        return ""
    return issue_user_token(tenant_id=tenant_id, user_ext_id=user_ext_id, session_key=session_key)


def _assert_session_owner(info: Any, *, tenant_id: str, session_key: str, user_ext_id: str) -> None:
    """校验会话归属：同一租户内，会话只能被其所属终端用户访问。

    这是**第二道**校验，用于只读接口与流式接口：这些接口不建立会话，
    因此不能套用 ``_resolve_user_identity`` 的"首轮放行"逻辑，
    而是要求"声明的身份 == 会话归属身份"。
    """
    owner = str(getattr(info, "user_ext_id", "") or "")
    requester = (user_ext_id or "").strip()
    if owner and requester and owner != requester:
        log.warning(
            "session.owner_mismatch",
            tenant_id=tenant_id,
            session_key=session_key,
            owner=owner,
            requester=requester,
        )
        raise HTTPException(status_code=403, detail="无权访问该会话")


def _new_session_key() -> str:
    """生成会话标识。

    必须**不可预测/不可枚举**：早期用毫秒时间戳（``s-<ms>``），
    同租户内可被逐个猜出，配合会话读取接口即可遍历他人会话。
    """
    return f"s-{uuid.uuid4().hex[:20]}"


def _ensure_db_session(*, tenant_id: str, session_key: str, user_ext_id: str, workspace_id: str = "") -> int:
    """确保数据库里有对应会话记录，返回主键。"""
    with write_session() as s:
        row = s.execute(
            select(SupportSession).where(
                SupportSession.tenant_id == tenant_id, SupportSession.session_key == session_key
            )
        ).scalar_one_or_none()
        if row is not None:
            return int(row.id)
        row = SupportSession(
            tenant_id=tenant_id,
            trace_id=get_trace_id() if get_trace_id() != "-" else "",
            session_key=session_key,
            user_ext_id=user_ext_id,
            workspace_id=workspace_id,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def _append_message(
    *,
    tenant_id: str,
    session_pk: int,
    role: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    diagnosis_id: int | None = None,
    latency_ms: float = 0.0,
) -> None:
    """写消息记录。"""
    with write_session() as s:
        s.add(
            Message(
                tenant_id=tenant_id,
                trace_id=get_trace_id() if get_trace_id() != "-" else "",
                session_id=session_pk,
                role=role,
                content=content,
                tool_calls_json=json.dumps(tool_calls or [], ensure_ascii=False)[:8000],
                diagnosis_id=diagnosis_id,
                latency_ms=round(latency_ms, 2),
            )
        )
        row = s.get(SupportSession, session_pk)
        if row is not None:
            row.turn_count += 1


# ===========================================================================
# 对话
# ===========================================================================
@router.post("/chat", response_model=ChatResponse, summary="发送消息（AI 诊断）")
async def chat(payload: ChatRequest) -> ChatResponse:
    tenant_id = get_tenant_id()
    if tenant_id == "-":
        raise HTTPException(status_code=400, detail="缺少租户上下文")

    session_key = payload.session_key or _new_session_key()
    sessions = get_session_service()
    # 身份解析必须在**建立会话之前**：既拦住越权，也不给越权探测留下垃圾记录。
    # 首轮允许自报身份（本系统无用户登录页）并立刻签发令牌；
    # 会话一旦建立，后续访问必须凭令牌（省略身份字段不再是绕过开关）。
    user_ext_id, authenticated = _resolve_user_identity(
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=payload.user_ext_id,
        user_token=payload.user_token,
    )
    issued_token = _issue_user_token(
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=user_ext_id,
        authenticated=authenticated,
    )
    info = sessions.get_or_create(
        tenant_id=tenant_id, session_key=session_key, user_ext_id=user_ext_id
    )
    session_pk = _ensure_db_session(
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=user_ext_id,
        workspace_id=payload.workspace_id,
    )
    _append_message(
        tenant_id=tenant_id, session_pk=session_pk, role="user", content=payload.message
    )

    # ---- 已被坐席接管：AI 不再介入，消息转给坐席 ----
    if info.state == SESSION_STATE_AGENT:
        _append_message(
            tenant_id=tenant_id,
            session_pk=session_pk,
            role="system",
            content=f"会话由坐席 {info.handler} 处理中，消息已转交",
        )
        get_event_bus().publish_nowait(
            Event(
                type=EVT_MESSAGE,
                tenant_id=tenant_id,
                payload={
                    "session_key": session_key,
                    "role": "user",
                    "content": payload.message,
                    "agent": info.handler,
                },
            )
        )
        return ChatResponse(
            session_key=session_key,
            # 坐席已接管的会话也要把令牌回传：前端凭它继续访问，避免"接管后读不到消息"
            user_token=issued_token,
            reply="您的消息已转交给人工客服，请稍候。",
            handler=info.handler,
            state=info.state,
            trace_id=get_trace_id(),
        )

    # ---- AI 处理 ----
    svc = get_agent_service()
    try:
        result = await svc.resume_after_restart(
            tenant_id=tenant_id,
            session_key=session_key,
            user_message=payload.message,
            user_ext_id=user_ext_id,
            session_pk=session_pk,
        )
    except AgentUnavailable as exc:
        # 容错矩阵：模型/图执行失败 → 友好提示 + 转人工
        log.error("chat.agent_unavailable", error=str(exc)[:300])
        escalation = _force_escalate(
            tenant_id=tenant_id,
            session_key=session_key,
            session_pk=session_pk,
            user_ext_id=user_ext_id,
            skill="",
            reason=f"诊断服务不可用（{type(exc).__name__}）",
            codes=["AGENT_UNAVAILABLE"],
            snapshot="## 交接快照\n\n**触发原因**：诊断服务不可用，无法自动排查。\n\n**建议话术**：请人工确认用户问题并跟进。",
        )
        return ChatResponse(
            session_key=session_key,
            user_token=issued_token,
            reply="抱歉，系统暂时无法完成自动排查，我已为您转接人工客服。",
            escalate=True,
            escalate_reasons=["诊断服务不可用"],
            degraded=True,
            degrade_reason="图执行失败",
            escalation=escalation,
            handler="QUEUE",
            state="QUEUED",
            trace_id=get_trace_id(),
        )

    _append_message(
        tenant_id=tenant_id,
        session_pk=session_pk,
        role="ai",
        content=result.reply,
        tool_calls=result.actions,
        diagnosis_id=result.diagnosis_id,
        latency_ms=result.duration_ms,
    )

    # ---- 转人工判定与真实流转 ----
    escalation_payload: dict[str, Any] | None = None
    if result.escalate:
        # 注意不要"编造"触发维度：早期这里用
        # ``emotion="angry" if "情绪" in reasons else "neutral"`` 硬凑情绪，
        # 而 emotion_intensity 从未传入（恒为 0），于是 EMOTION_ANGRY 这条
        # 需要 intensity≥7 的分支在 API 路径上永远不可能命中——
        # 既制造了"有情绪升级能力"的错觉，又让触发码统计失真。
        # 这里只上报**真实掌握**的维度：流程内部判定 + 排查步数。
        # 情绪维度由会话级情绪识别产出后再接入（当前未实现，如实留空）。
        trigger = evaluate_escalation(
            step_count=len([n for n in result.node_path if n in ("diagnose", "retry")]),
            emotion="neutral",
            emotion_intensity=0,
            amount=0.0,
            explicit=True,
        )
        escalation_payload = _force_escalate(
            tenant_id=tenant_id,
            session_key=session_key,
            session_pk=session_pk,
            user_ext_id=user_ext_id,
            skill=result.skill,
            reason=result.escalate_reasons[0] if result.escalate_reasons else "AI 判定需人工介入",
            codes=trigger.codes or ["FLOW_EXPLICIT"],
            snapshot=result.handover_snapshot,
        )
        info = sessions.get_or_create(tenant_id=tenant_id, session_key=session_key)

    if result.pending_interrupt:
        info = sessions.get_or_create(tenant_id=tenant_id, session_key=session_key)

    return ChatResponse(
        session_key=session_key,
        user_token=issued_token,
        reply=result.reply,
        skill=result.skill,
        outcome=result.outcome,
        cause_code=result.cause_code,
        cause_text=result.cause_text,
        confidence=result.confidence,
        risk_score=result.risk_score,
        decision=result.decision,
        escalate=result.escalate,
        escalate_reasons=result.escalate_reasons,
        evidence=result.evidence,
        actions=result.actions,
        options=result.options,
        verify_ok=result.verify_ok,
        verify_detail=result.verify_detail,
        node_path=result.node_path,
        degraded=result.degraded,
        degrade_reason=result.degrade_reason,
        pending_clarify=result.pending_interrupt,
        escalation=escalation_payload,
        handler=info.handler,
        state=info.state,
        trace_id=get_trace_id(),
        duration_ms=result.duration_ms,
    )


def _force_escalate(
    *,
    tenant_id: str,
    session_key: str,
    session_pk: int,
    user_ext_id: str,
    skill: str,
    reason: str,
    codes: list[str],
    snapshot: str,
) -> dict[str, Any]:
    """执行转人工（写记录 + 入队 + 派单 + 推送）。"""
    esc = get_escalation_service()
    created = esc.create(
        tenant_id=tenant_id,
        session_key=session_key,
        reason=reason,
        trigger_codes=codes,
        user_ext_id=user_ext_id,
        skill=skill,
        priority=1 if "VIP" in reason else 0,
        snapshot=snapshot,
        session_pk=session_pk,
    )
    # 立即尝试派单（有在线坐席则直接分配）
    assigned = esc.dispatch_one(tenant_id=tenant_id)
    if assigned:
        created["assignment"] = assigned
    return created


# ===========================================================================
# 会话
# ===========================================================================
class SessionView(BaseModel):
    session_key: str
    state: str
    handler: str
    skill: str = ""
    turn_count: int = 0
    created_at: str = ""
    updated_at: str = ""


def _require_identity(
    *, tenant_id: str, session_key: str, user_ext_id: str, user_token: str
) -> str:
    """只读接口的身份要求：必须凭**有效且绑定本会话的令牌**访问。

    与 ``/chat`` 不同，只读接口不建立会话，所以没有"首轮放行"的余地：
    若允许不带令牌读取，任何拿到会话标识的人都能翻别人的聊天记录。
    """
    if not user_token:
        raise HTTPException(
            status_code=401,
            detail="缺少会话令牌（首轮对话响应中的 user_token）",
        )
    token_identity, _bound = _user_token_identity(
        tenant_id=tenant_id, session_key=session_key, user_token=user_token
    )
    declared = (user_ext_id or "").strip()
    if declared and declared != token_identity:
        raise HTTPException(status_code=403, detail="身份与令牌不一致")
    return token_identity


@router.get("/session/{session_key}", summary="查看会话状态")
async def get_session(
    session_key: str,
    user_ext_id: str = Query("", max_length=64),
    user_token: str = Query("", max_length=2048),
) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    sessions = get_session_service()
    identity = _require_identity(
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=user_ext_id,
        user_token=user_token,
    )
    _assert_session_owner(
        sessions.peek(tenant_id=tenant_id, session_key=session_key),
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=identity,
    )
    info = sessions.get_or_create(tenant_id=tenant_id, session_key=session_key)
    with read_session() as s:
        row = s.execute(
            select(SupportSession).where(
                SupportSession.tenant_id == tenant_id, SupportSession.session_key == session_key
            )
        ).scalar_one_or_none()
        db_view = row.to_dict() if row else {}
    return {"ok": True, "data": {**db_view, "redis": info.to_dict()}}


@router.get("/session/{session_key}/messages", summary="会话消息记录")
async def get_messages(
    session_key: str,
    limit: int = Query(50, ge=1, le=500),
    user_ext_id: str = Query("", max_length=64),
    user_token: str = Query("", max_length=2048),
) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    sessions = get_session_service()
    identity = _require_identity(
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=user_ext_id,
        user_token=user_token,
    )
    _assert_session_owner(
        sessions.peek(tenant_id=tenant_id, session_key=session_key),
        tenant_id=tenant_id,
        session_key=session_key,
        user_ext_id=identity,
    )
    with read_session() as s:
        row = s.execute(
            select(SupportSession).where(
                SupportSession.tenant_id == tenant_id, SupportSession.session_key == session_key
            )
        ).scalar_one_or_none()
        if row is None:
            return {"ok": True, "data": []}
        msgs = s.execute(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == int(row.id))
            .order_by(Message.id.asc())
            .limit(limit)
        ).scalars().all()
        return {"ok": True, "data": [m.to_dict() for m in msgs]}


@router.get("/sessions", summary="会话列表（走从库）")
async def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    user_token: str = Query("", max_length=2048),
) -> dict[str, Any]:
    """列出**本人**的会话。

    安全审查发现：早期该接口返回本租户**全部**会话标识，
    配合按会话键读取的接口即可遍历他人聊天记录（会话键枚举的入口）。
    因此这里必须按令牌身份过滤；未认证直接拒绝，而不是"返回全部"。
    """
    tenant_id = get_tenant_id()
    if not user_token:
        raise HTTPException(status_code=401, detail="缺少会话令牌（首轮对话响应中的 user_token）")
    try:
        claims = decode_user_token(user_token, tenant_id=tenant_id)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=f"用户令牌无效：{exc}") from exc
    identity = str(claims.subject)

    with read_session() as s:
        rows = s.execute(
            select(SupportSession)
            .where(
                SupportSession.tenant_id == tenant_id,
                SupportSession.user_ext_id == identity,
            )
            .order_by(SupportSession.id.desc())
            .limit(limit)
        ).scalars().all()
        return {"ok": True, "data": [r.to_dict() for r in rows], "total": len(rows)}


# ===========================================================================
# 工单
# ===========================================================================
@router.get("/tickets", summary="工单列表（走从库）")
async def list_tickets(
    status: str = Query("", max_length=16), limit: int = Query(50, ge=1, le=500)
) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    with read_session() as s:
        stmt = select(Ticket).where(Ticket.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(Ticket.status == status)
        rows = s.execute(stmt.order_by(Ticket.id.desc()).limit(limit)).scalars().all()
        return {"ok": True, "data": [r.to_dict() for r in rows], "total": len(rows)}


@router.get("/tickets/{ticket_no}", summary="工单详情（含交接快照）")
async def get_ticket(ticket_no: str) -> dict[str, Any]:
    tenant_id = get_tenant_id()
    with read_session() as s:
        row = s.execute(
            select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.ticket_no == ticket_no)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        msgs = s.execute(
            select(TicketMessage).where(TicketMessage.tenant_id == tenant_id, TicketMessage.ticket_id == int(row.id))
        ).scalars().all()
        return {"ok": True, "data": {**row.to_dict(), "messages": [m.to_dict() for m in msgs]}}


# 健康检查
# ===========================================================================
@router.get("/health/deps", summary="依赖健康检查（主从/复制/缓存/向量库/模型）")
async def health_deps() -> dict[str, Any]:
    from app.services.health_service import full_health

    report = full_health()
    return {"ok": report.get("overall") == "ok", "data": report}


# ===========================================================================
# 流式回复（SSE）
# ===========================================================================
@router.get("/chat/stream", summary="流式回复（SSE）")
async def chat_stream(
    message: str = Query(..., min_length=1, max_length=8000),
    session_key: str = Query("", max_length=64),
    user_ext_id: str = Query("", max_length=64),
    user_token: str = Query("", max_length=2048),
) -> Any:
    """以 SSE 逐步推送回复。

    诊断流程本身是确定的（工具调用+结论），这里把**最终回复按段落分块推送**，
    让用户端能即时看到内容而不是等整轮结束。事件格式：

        event: meta     —— 会话标识与技能
        event: chunk    —— 回复片段
        event: done     —— 结束，附带结构化结论
        event: error    —— 降级提示

    身份规则与 ``/chat`` 一致（首轮可自报身份并签发令牌，之后必须凭令牌），
    校验放在返回响应之前——一旦进入生成器，200 响应头已发出，
    此时再拒绝，客户端只会看到一个空的 200。
    """
    from fastapi.responses import StreamingResponse

    tenant_id = get_tenant_id()
    key = session_key or _new_session_key()
    identity, _authenticated = _resolve_user_identity(
        tenant_id=tenant_id,
        session_key=key,
        user_ext_id=user_ext_id,
        user_token=user_token,
    )

    async def event_source():
        def sse(event: str, data: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield sse("meta", {"session_key": key, "trace_id": get_trace_id()})
        try:
            svc = get_agent_service()
            result = await svc.resume_after_restart(
                tenant_id=tenant_id, session_key=key, user_message=message, user_ext_id=identity
            )
            yield sse("meta", {"skill": result.skill, "cause_code": result.cause_code})

            # 分块推送（按标点切分，兼顾中文阅读节奏）
            text = result.reply or ""
            chunk = ""
            for ch in text:
                chunk += ch
                if ch in "。！？\n；" and len(chunk) >= 12:
                    yield sse("chunk", {"text": chunk})
                    chunk = ""
            if chunk:
                yield sse("chunk", {"text": chunk})

            yield sse("done", result.to_dict())
        except Exception as exc:  # noqa: BLE001
            log.error("chat.stream_failed", error=str(exc)[:300])
            yield sse(
                "error",
                {
                    "message": "抱歉，自动排查过程中出现异常，请稍后重试或回复「转人工」。",
                    "degrade_reason": type(exc).__name__,
                },
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
