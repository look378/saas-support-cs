"""会话状态机 + 坐席心跳 + 转人工队列（Redis 承载）。

Redis 在本项目承担四件不可缺的事（架构文档 §5.2）：
  1. **会话状态**：会话 ↔ 处理方归属、状态机跃迁
  2. **坐席心跳**：TTL 键，掉线自动摘除并回收其会话
  3. **转人工队列**：带优先级的 FIFO
  4. **分布式锁**：同一会话唯一处理方（在 :mod:`app.core.redis_client` 中实现）

所有跃迁都写 ``session_events`` 审计表，"谁在什么时候把会话从 A 变成 B"可追溯。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.redis_client import (
    get_redis,
    key_agent_heartbeat,
    key_agent_load,
    key_session,
)
from app.models.conversation import (
    SESSION_STATE_AGENT,
    SESSION_STATE_AI,
    SESSION_STATE_CLOSED,
    SESSION_STATE_QUEUED,
)

log = get_logger(__name__)

VALID_STATES = {
    SESSION_STATE_AI,
    SESSION_STATE_QUEUED,
    SESSION_STATE_AGENT,
    SESSION_STATE_CLOSED,
}

# 状态机允许的跃迁（防止非法跳转，例如直接从 CLOSED 回到 AI）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    SESSION_STATE_AI: {SESSION_STATE_QUEUED, SESSION_STATE_AGENT, SESSION_STATE_CLOSED},
    SESSION_STATE_QUEUED: {SESSION_STATE_AGENT, SESSION_STATE_AI, SESSION_STATE_CLOSED},
    SESSION_STATE_AGENT: {SESSION_STATE_CLOSED, SESSION_STATE_QUEUED},
    SESSION_STATE_CLOSED: set(),
}


class InvalidTransition(RuntimeError):
    """非法状态跃迁。"""


@dataclass
class SessionInfo:
    """会话在 Redis 中的状态。"""

    session_key: str
    tenant_id: str
    state: str = SESSION_STATE_AI
    handler: str = "AI"
    skill: str = ""
    user_ext_id: str = ""
    priority: int = 0
    queued_at: float = 0.0
    claimed_at: float = 0.0
    updated_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "handler": self.handler,
            "skill": self.skill,
            "user_ext_id": self.user_ext_id,
            "priority": self.priority,
            "queued_at": self.queued_at,
            "claimed_at": self.claimed_at,
            "updated_at": self.updated_at,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionInfo:
        known = {
            "session_key", "tenant_id", "state", "handler", "skill",
            "user_ext_id", "priority", "queued_at", "claimed_at", "updated_at",
        }
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            session_key=str(data.get("session_key", "")),
            tenant_id=str(data.get("tenant_id", "")),
            state=str(data.get("state", SESSION_STATE_AI)),
            handler=str(data.get("handler", "AI")),
            skill=str(data.get("skill", "")),
            user_ext_id=str(data.get("user_ext_id", "")),
            priority=int(data.get("priority", 0) or 0),
            queued_at=float(data.get("queued_at", 0) or 0),
            claimed_at=float(data.get("claimed_at", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
            extra=extra,
        )


class SessionService:
    """会话状态机。"""

    def __init__(self, ttl_seconds: int | None = None) -> None:
        self.ttl = ttl_seconds or settings.session_ttl_seconds

    # ------------------------------------------------------------------
    def _read(self, tenant_id: str, session_key: str) -> SessionInfo | None:
        r = get_redis()
        raw = r.get(key_session(f"{tenant_id}:{session_key}"))
        if not raw:
            return None
        try:
            return SessionInfo.from_dict(json.loads(raw))
        except Exception:  # noqa: BLE001
            return None

    def _write(self, info: SessionInfo) -> None:
        r = get_redis()
        info.updated_at = time.time()
        r.set(
            key_session(f"{info.tenant_id}:{info.session_key}"),
            json.dumps(info.to_dict(), ensure_ascii=False),
            ex=self.ttl,
        )

    # ------------------------------------------------------------------
    def get_or_create(
        self, *, tenant_id: str, session_key: str, user_ext_id: str = "", skill: str = ""
    ) -> SessionInfo:
        """读取会话；不存在则建立为 AI 处理中。"""
        existing = self._read(tenant_id, session_key)
        if existing:
            # 续期，避免活跃会话因 TTL 到期丢失
            self._write(existing)
            return existing
        info = SessionInfo(
            session_key=session_key,
            tenant_id=tenant_id,
            state=SESSION_STATE_AI,
            handler="AI",
            user_ext_id=user_ext_id,
            skill=skill,
        )
        self._write(info)
        log.info("session.created", session_key=session_key, tenant_id=tenant_id)
        return info

    def peek(self, *, tenant_id: str, session_key: str) -> SessionInfo | None:
        """只读会话，不存在时返回 None（**不创建**）。

        用途：入站请求在"确认自己有权访问该会话"之前不能建立会话记录，
        否则越权探测会往库里写入垃圾会话（见 ``_assert_session_owner``）。
        """
        return self._read(tenant_id, session_key)

    # ------------------------------------------------------------------
    def transition(
        self,
        *,
        tenant_id: str,
        session_key: str,
        to_state: str,
        handler: str | None = None,
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> SessionInfo:
        """执行状态跃迁（带合法性校验 + 审计落库）。"""
        if to_state not in VALID_STATES:
            raise InvalidTransition(f"未知状态 {to_state}")

        info = self.get_or_create(tenant_id=tenant_id, session_key=session_key)
        frm = info.state
        if to_state != frm and to_state not in ALLOWED_TRANSITIONS.get(frm, set()):
            raise InvalidTransition(f"{frm} → {to_state} 不是合法的状态跃迁")

        info.state = to_state
        if handler is not None:
            info.handler = handler
        if to_state == SESSION_STATE_QUEUED:
            info.queued_at = time.time()
        elif to_state == SESSION_STATE_AGENT:
            info.claimed_at = time.time()
        elif to_state == SESSION_STATE_CLOSED and not info.queued_at:
            info.claimed_at = info.claimed_at or time.time()
        if extra:
            info.extra.update(extra)
        self._write(info)

        # 同步落库：Redis 是实时状态的权威来源，数据库是审计与查询来源。
        # 两者**必须同时更新**——否则依赖数据库的查询（如"某坐席名下处理中的会话"）
        # 会查不到任何记录，导致坐席掉线后无法回收会话（本项目实测踩到）。
        self._persist_state(
            tenant_id=tenant_id,
            session_key=session_key,
            state=to_state,
            handler=info.handler,
        )

        self._audit(
            tenant_id=tenant_id,
            session_key=session_key,
            event_type="state_transition",
            from_state=frm,
            to_state=to_state,
            detail={"reason": reason, "handler": info.handler, **(extra or {})},
        )
        log.info(
            "session.transition",
            session_key=session_key,
            tenant_id=tenant_id,
            from_state=frm,
            to_state=to_state,
            handler=info.handler,
            reason=reason,
        )
        return info

    def close(self, *, tenant_id: str, session_key: str, reason: str = "办结") -> SessionInfo:
        """办结会话。"""
        return self.transition(
            tenant_id=tenant_id,
            session_key=session_key,
            to_state=SESSION_STATE_CLOSED,
            reason=reason,
        )

    # ------------------------------------------------------------------
    def owner(self, *, tenant_id: str, session_key: str) -> str:
        """当前处理方（AI 或坐席名）。"""
        info = self._read(tenant_id, session_key)
        return info.handler if info else ""

    # ------------------------------------------------------------------
    def _persist_state(self, *, tenant_id: str, session_key: str, state: str, handler: str) -> None:
        """把会话状态同步到数据库。失败不影响主流程（Redis 仍是实时权威）。"""
        try:
            from sqlalchemy import select

            from app.core.database import write_session as db_write
            from app.core.trace_context import ensure_tenant
            from app.models import SupportSession

            with ensure_tenant(tenant_id), db_write() as s:
                row = s.execute(
                    select(SupportSession).where(
                        SupportSession.tenant_id == tenant_id,
                        SupportSession.session_key == session_key,
                    )
                ).scalar_one_or_none()
                if row is None:
                    # 会话记录可能尚未建立（例如图形流程早于 API 落库），此处补建
                    s.add(
                        SupportSession(
                            tenant_id=tenant_id,
                            session_key=session_key,
                            state=state,
                            handler=handler,
                        )
                    )
                else:
                    row.state = state
                    row.handler = handler
                    if state == SESSION_STATE_CLOSED:
                        from datetime import datetime, timezone

                        row.closed_at = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            log.warning("session.persist_state_failed", error=str(exc)[:200])

    def _audit(
        self,
        *,
        tenant_id: str,
        session_key: str,
        event_type: str,
        from_state: str = "",
        to_state: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """写会话事件审计表。失败不影响主流程。"""
        try:
            from sqlalchemy import select

            from app.core.database import write_session as db_write
            from app.core.trace_context import ensure_tenant, get_trace_id
            from app.models import SessionEvent, SupportSession

            # 状态跃迁可能由后台任务/脚本触发（无请求上下文），
            # 此处确保租户已绑定，否则审计写入会被租户闸门拒绝。
            with ensure_tenant(tenant_id), db_write() as s:
                row = s.execute(
                    select(SupportSession).where(
                        SupportSession.tenant_id == tenant_id,
                        SupportSession.session_key == session_key,
                    )
                ).scalar_one_or_none()
                s.add(
                    SessionEvent(
                        tenant_id=tenant_id,
                        trace_id=get_trace_id() if get_trace_id() != "-" else "",
                        session_id=int(row.id) if row else 0,
                        event_type=event_type,
                        from_state=from_state,
                        to_state=to_state,
                        detail_json=json.dumps(detail or {}, ensure_ascii=False)[:4000],
                    )
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("session.audit_failed", error=str(exc)[:200])


# ===========================================================================
# 坐席心跳与在线状态
# ===========================================================================
@dataclass
class AgentPresence:
    """坐席在线状态。"""

    agent: str
    tenant_id: str
    skills: list[str] = field(default_factory=list)
    presence: str = "online"      # online / break / offline
    load: int = 0
    last_beat: float = 0.0
    ttl: int = 0


class PresenceService:
    """坐席在线状态（心跳驱动）。"""

    def __init__(self) -> None:
        self.ttl = settings.agent_heartbeat_ttl_seconds

    def heartbeat(
        self,
        *,
        tenant_id: str,
        agent: str,
        skills: list[str] | None = None,
        presence: str = "online",
        load: int | None = None,
    ) -> AgentPresence:
        """上报心跳（每 ``AGENT_HEARTBEAT_INTERVAL_SECONDS`` 秒一次，TTL 30 秒）。"""
        r = get_redis()
        key = key_agent_heartbeat(f"{tenant_id}:{agent}")
        payload = {
            "agent": agent,
            "tenant_id": tenant_id,
            "skills": skills or [],
            "presence": presence,
            "load": load if load is not None else self._current_load(tenant_id, agent),
            "last_beat": time.time(),
        }
        r.set(key, json.dumps(payload, ensure_ascii=False), ex=self.ttl)
        if load is not None:
            r.set(key_agent_load(f"{tenant_id}:{agent}"), load, ex=self.ttl * 4)
        payload["ttl"] = self.ttl
        return AgentPresence(**{k: payload[k] for k in AgentPresence.__dataclass_fields__ if k in payload})

    def get(self, *, tenant_id: str, agent: str) -> AgentPresence | None:
        r = get_redis()
        raw = r.get(key_agent_heartbeat(f"{tenant_id}:{agent}"))
        if not raw:
            return None
        data = json.loads(raw)
        data["ttl"] = r.ttl(key_agent_heartbeat(f"{tenant_id}:{agent}"))
        return AgentPresence(**{k: data[k] for k in AgentPresence.__dataclass_fields__ if k in data})

    def is_online(self, *, tenant_id: str, agent: str) -> bool:
        pr = self.get(tenant_id=tenant_id, agent=agent)
        return bool(pr and pr.presence in ("online", "break"))

    def online_agents(self, *, tenant_id: str, agents: list[str]) -> list[AgentPresence]:
        """返回当前在线（心跳未过期）的坐席。"""
        out: list[AgentPresence] = []
        for a in agents:
            pr = self.get(tenant_id=tenant_id, agent=a)
            if pr and pr.presence in ("online", "break"):
                out.append(pr)
        return out

    def set_offline(self, *, tenant_id: str, agent: str) -> None:
        """主动下线（清除心跳）。"""
        r = get_redis()
        r.delete(key_agent_heartbeat(f"{tenant_id}:{agent}"))

    def _current_load(self, tenant_id: str, agent: str) -> int:
        r = get_redis()
        val = r.get(key_agent_load(f"{tenant_id}:{agent}"))
        return int(val) if val else 0

    def incr_load(self, *, tenant_id: str, agent: str, delta: int = 1) -> int:
        """调整坐席负载（接管 +1，办结 -1）。

        钳制到 0 之后**必须回写**：只在返回值上 clamp 而 Redis 里留着负数，
        会让"负载最小优先"的派单排序读到负值（越欠载越优先），
        掉线回收多发一次 -1 就会把该坐席永久顶到派单队列最前（本项目实测踩到）。
        """
        r = get_redis()
        key = key_agent_load(f"{tenant_id}:{agent}")
        raw = int(r.incrby(key, delta))
        new_val = max(0, raw)
        if new_val != raw:
            r.set(key, new_val)
        r.expire(key, self.ttl * 4)
        return new_val

    def all_with_ttl(self, *, tenant_id: str, agents: list[str]) -> dict[str, int]:
        """返回各坐席心跳剩余 TTL（用于掉线检测与工作台展示）。"""
        r = get_redis()
        return {
            a: r.ttl(key_agent_heartbeat(f"{tenant_id}:{a}"))
            for a in agents
        }


# ===========================================================================
# 转人工队列（带优先级的 FIFO）
# ===========================================================================
class EscalationQueue:
    """转人工队列。

    用 Redis List 实现 FIFO；优先级用**分段队列**实现：
    高优先级会话进 ``:high``，普通进 ``:normal``，出队时先取高优先级。
    这样既保留了同优先级内的 FIFO 公平性，又让 VIP/高金额会话优先被接管。
    """

    def __init__(self, base_key: str | None = None) -> None:
        self.base = base_key or settings.escalation_queue_key

    def _keys(self) -> list[str]:
        return [f"{self.base}:high", f"{self.base}:normal"]

    def enqueue(self, *, tenant_id: str, session_key: str, priority: int = 0, payload: dict[str, Any] | None = None) -> int:
        """入队。``priority > 0`` 进高优先级队列。"""
        r = get_redis()
        item = json.dumps(
            {
                "tenant_id": tenant_id,
                "session_key": session_key,
                "priority": priority,
                "enqueued_at": time.time(),
                **(payload or {}),
            },
            ensure_ascii=False,
        )
        key = f"{self.base}:high" if priority > 0 else f"{self.base}:normal"
        size = int(r.rpush(key, item))
        log.info("queue.enqueue", session_key=session_key, priority=priority, size=size)
        return size

    def dequeue(self, *, tenant_id: str | None = None) -> dict[str, Any] | None:
        """出队：先高优先级，再普通。

        ``tenant_id`` 给定时只取**该租户**的会话：队列键本身不含租户
        （单实例部署下共用一个 List），若不过滤，A 租户的派单动作会取到
        B 租户的队首元素并改动 B 的记录——这是租户隔离缺口，不是性能问题。
        用"扫描 + 精确 LREM"实现按租户出队；LREM 返回 0 说明该元素刚被
        其它派单者取走，继续往后找，不会丢件。
        """
        r = get_redis()
        for key in self._keys():
            if tenant_id is None:
                raw = r.lpop(key)
                if raw:
                    try:
                        return json.loads(raw)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("queue.bad_item", key=key, error=str(exc)[:150])
                        continue
                continue
            for raw in r.lrange(key, 0, -1):
                try:
                    item = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    # 队列元素损坏时跳过但不静默：损坏项会让后续派单一直取不到
                    log.warning("queue.bad_item", key=key, error=str(exc)[:150])
                    continue
                if item.get("tenant_id") != tenant_id:
                    continue
                if int(r.lrem(key, 1, raw)) > 0:
                    return item
        return None

    def peek(self, *, limit: int = 20, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """查看队列（不消费），用于工作台展示待接入队列。

        ``tenant_id`` 给定时只返回该租户的条目（控制台展示必须过滤，
        否则会看到其它租户的会话标识与技能）。
        """
        r = get_redis()
        out: list[dict[str, Any]] = []
        for key in self._keys():
            for raw in r.lrange(key, 0, limit - 1):
                try:
                    item = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    log.warning("queue.bad_item", key=key, error=str(exc)[:150])
                    continue
                if tenant_id is not None and item.get("tenant_id") != tenant_id:
                    continue
                out.append(item)
        return out

    def remove(self, *, tenant_id: str, session_key: str) -> int:
        """从队列中移除指定会话（例如坐席已手动接管）。"""
        r = get_redis()
        removed = 0
        for key in self._keys():
            for raw in r.lrange(key, 0, -1):
                try:
                    item = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    log.warning("queue.bad_item", key=key, error=str(exc)[:150])
                    continue
                if item.get("tenant_id") == tenant_id and item.get("session_key") == session_key:
                    removed += int(r.lrem(key, 0, raw))
        return removed

    def size(self, *, tenant_id: str | None = None) -> dict[str, int]:
        """各优先级队列长度。``tenant_id`` 给定时只统计该租户的条目。"""
        if tenant_id is None:
            r = get_redis()
            return {key.split(":")[-1]: int(r.llen(key)) for key in self._keys()}
        counted = {"high": 0, "normal": 0}
        for item in self.peek(limit=1000, tenant_id=tenant_id):
            label = "high" if int(item.get("priority", 0) or 0) > 0 else "normal"
            counted[label] += 1
        return counted

    def total(self, *, tenant_id: str | None = None) -> int:
        return sum(self.size(tenant_id=tenant_id).values())


# ===========================================================================
# 单例
# ===========================================================================
_session_service: SessionService | None = None
_presence_service: PresenceService | None = None
_queue: EscalationQueue | None = None


def get_session_service() -> SessionService:
    global _session_service
    if _session_service is None:
        _session_service = SessionService()
    return _session_service


def get_presence_service() -> PresenceService:
    global _presence_service
    if _presence_service is None:
        _presence_service = PresenceService()
    return _presence_service


def get_escalation_queue() -> EscalationQueue:
    global _queue
    if _queue is None:
        _queue = EscalationQueue()
    return _queue
