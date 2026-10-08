"""事件总线：把坐席实时事件推送给 WebSocket 连接。

用途：坐席工作台需要"实时"收到派单、接管、消息、办结事件。
Streamlit 承载不了服务端主动推送，因此由本模块配合真实 WebSocket 控制台实现
（决策 ADR-004）。

设计：
  - 按租户分频道，避免跨租户推送（否则 A 租户坐席会看到 B 租户的会话）
  - 发送失败不抛异常（连接断了不应影响业务流转）
  - 保留最近事件环形缓冲，便于新连接补看最近状态
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from app.core.logging_config import get_logger

log = get_logger(__name__)

# 每个租户保留最近 N 条事件，供新连接回看
_BUFFER_SIZE = 100


@dataclass
class Event:
    """一条坐席事件。"""

    type: str
    tenant_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(
            {"type": self.type, "tenant_id": self.tenant_id, "at": self.at, **self.payload},
            ensure_ascii=False,
        )


class EventBus:
    """进程内事件总线（按租户隔离频道）。"""

    def __init__(self) -> None:
        self._connections: dict[str, set[Any]] = {}
        self._buffers: dict[str, deque[Event]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, tenant_id: str, websocket: Any) -> None:
        """注册一个 WebSocket 连接。"""
        async with self._lock:
            self._connections.setdefault(tenant_id, set()).add(websocket)
        log.info("eventbus.subscribe", tenant_id=tenant_id, total=len(self._connections[tenant_id]))

    async def unsubscribe(self, tenant_id: str, websocket: Any) -> None:
        """注销连接。"""
        async with self._lock:
            conns = self._connections.get(tenant_id)
            if conns:
                conns.discard(websocket)
                if not conns:
                    self._connections.pop(tenant_id, None)
        log.info("eventbus.unsubscribe", tenant_id=tenant_id)

    def recent(self, tenant_id: str, limit: int = 20) -> list[Event]:
        """最近事件（新连接回看用）。"""
        buf = self._buffers.get(tenant_id)
        if not buf:
            return []
        return list(buf)[-limit:]

    def connection_count(self, tenant_id: str) -> int:
        return len(self._connections.get(tenant_id, ()))

    async def publish(self, event: Event) -> int:
        """广播事件。返回成功送达的连接数。"""
        buf = self._buffers.setdefault(event.tenant_id, deque(maxlen=_BUFFER_SIZE))
        buf.append(event)

        conns = list(self._connections.get(event.tenant_id, ()))
        if not conns:
            log.info("eventbus.no_subscriber", type=event.type, tenant_id=event.tenant_id)
            return 0

        text = event.to_json()
        delivered = 0
        dead: list[Any] = []
        for ws in conns:
            try:
                await ws.send_text(text)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 连接已断属正常情况
                dead.append(ws)
                log.warning("eventbus.send_failed", type=event.type, error=str(exc)[:120])

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.get(event.tenant_id, set()).discard(ws)
        log.info("eventbus.publish", type=event.type, tenant_id=event.tenant_id, delivered=delivered)
        return delivered

    def publish_nowait(self, event: Event) -> None:
        """在同步代码中发布事件（无事件循环时降级为只写缓冲）。

        业务代码（同步的领域服务）通过它通知异步的推送层，
        用 ``asyncio.run_coroutine_threadsafe`` 交给主循环执行。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            loop.create_task(self.publish(event))
            return

        # 同步上下文（如命令行脚本）：只写缓冲，不推送
        self._buffers.setdefault(event.tenant_id, deque(maxlen=_BUFFER_SIZE)).append(event)
        log.info("eventbus.buffered_only", type=event.type, tenant_id=event.tenant_id)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """获取事件总线单例。"""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


# 事件类型常量（与前端约定）
EVT_ESCALATION_CREATED = "escalation.created"
EVT_ESCALATION_ASSIGNED = "escalation.assigned"
EVT_SESSION_CLAIMED = "session.claimed"
EVT_SESSION_CLOSED = "session.closed"
EVT_MESSAGE = "session.message"
EVT_AGENT_PRESENCE = "agent.presence"
EVT_AGENT_DISCONNECTED = "agent.disconnected"
