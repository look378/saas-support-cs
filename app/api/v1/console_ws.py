"""坐席实时控制台（真实 WebSocket 推送）。

**为什么单独做 WebSocket 而不是只用 Streamlit**（决策 ADR-004）：
Streamlit 的执行模型是"交互触发脚本重跑"，它无法作为 WebSocket 客户端接收
服务端主动推送。因此"实时收到派单"这个能力必须由真正的 WebSocket 承载，
Streamlit 只负责看板与回放。

连接方式：
    ws://127.0.0.1:8000/ws/agent?tenant_id=t-001&token=<坐席JWT>

消息协议（服务端 → 客户端）：
    {"type": "...", "at": 1712345678.9, ...}
事件类型见 ``app/services/event_bus.py`` 的常量。

客户端 → 服务端命令：
    {"cmd": "dispatch"}   —— 手动触发一次派单
    {"cmd": "ping"}       —— 保活
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from app.core.logging_config import get_logger
from app.core.trace_context import get_tenant_id
from app.security import TokenError, decode_token
from app.services.escalation_service import get_escalation_service
from app.services.event_bus import get_event_bus
from app.services.session_service import get_escalation_queue

log = get_logger(__name__)
router = APIRouter(tags=["实时控制台"])


@router.websocket("/ws/agent")
async def agent_console(
    websocket: WebSocket,
    tenant_id: str = Query(..., description="租户标识"),
    token: str = Query("", description="坐席 JWT"),
) -> None:
    """坐席实时控制台连接。"""
    # 鉴权：WebSocket 无法用标准 Authorization 头（浏览器限制），走查询参数
    if not token:
        await websocket.close(code=4401, reason="缺少坐席令牌")
        return
    try:
        claims = decode_token(token, audience="agent")
    except TokenError as exc:
        await websocket.close(code=4401, reason=f"令牌无效: {exc.kind}")
        return
    if claims.tenant_id != tenant_id:
        await websocket.close(code=4403, reason="令牌租户与请求租户不一致")
        return

    await websocket.accept()
    bus = get_event_bus()
    await bus.subscribe(tenant_id, websocket)

    # 连接建立后先回放最近事件，避免新连接"看不到当前状态"
    try:
        for ev in bus.recent(tenant_id, limit=20):
            await websocket.send_text(ev.to_json())
        await websocket.send_text(
            json.dumps(
                {
                    "type": "console.ready",
                    "tenant_id": tenant_id,
                    "agent": claims.subject,
                    "queue_size": get_escalation_queue().size(),
                    "replayed": len(bus.recent(tenant_id, limit=20)),
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("console.handshake_failed", error=str(exc)[:200])

    log.info("console.connected", tenant_id=tenant_id, agent=claims.subject)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            cmd = msg.get("cmd", "")
            if cmd == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "at": __import__("time").time()}))
            elif cmd == "dispatch":
                result = get_escalation_service().dispatch_one(tenant_id=tenant_id)
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "dispatch.result",
                            "tenant_id": tenant_id,
                            "result": result,
                            "queue_size": get_escalation_queue().size(),
                        },
                        ensure_ascii=False,
                    )
                )
            elif cmd == "queue":
                items = get_escalation_service().queue_view(tenant_id=tenant_id)
                await websocket.send_text(
                    json.dumps({"type": "queue.snapshot", "items": items}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        log.info("console.disconnected", tenant_id=tenant_id, agent=claims.subject)
    except Exception as exc:  # noqa: BLE001 - 连接级兜底，断连不应影响其它坐席
        log.warning("console.error", tenant_id=tenant_id, error=str(exc)[:200])
    finally:
        await bus.unsubscribe(tenant_id, websocket)


@router.get("/api/v1/console/state", summary="控制台初始状态（供页面加载）")
async def console_state(request: Request, tenant_id: str = Query(...)) -> dict[str, Any]:
    """返回队列、在线坐席与最近事件，供控制台页面首屏渲染。

    **租户必须与令牌一致**：本接口只要求租户头即可通过中间件，
    若再信任查询参数里的 ``tenant_id``，任一租户的坐席即可读取其它租户的
    队列与事件（``/ws/agent`` 已做同样校验，这里必须对齐）。
    """
    ctx_tenant = get_tenant_id()
    if not ctx_tenant or ctx_tenant == "-" or ctx_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="租户标识与请求上下文不一致")

    esc = get_escalation_service()
    bus = get_event_bus()
    queue_items = esc.queue_view(tenant_id=tenant_id)
    return {
        "ok": True,
        "data": {
            "tenant_id": tenant_id,
            "agent": getattr(request.state, "agent", ""),
            "queue": queue_items,
            "queue_size": esc.queue.size(tenant_id=tenant_id),
            "recent_events": [json.loads(e.to_json()) for e in bus.recent(tenant_id, limit=20)],
            "subscribers": bus.connection_count(tenant_id),
        },
    }
