"""全链路上下文（trace_id / tenant_id / session_id 传播）。

**为什么用 ContextVar 而不是模块级全局变量**
-------------------------------------------------
FastAPI 在异步请求间共享同一线程，模块级全局变量会让并发请求互相覆盖上下文，
直接导致「跨租户串数据」。`contextvars.ContextVar` 在 asyncio 任务派生时自动
隔离，是并发安全的最小成本方案。架构文档 §5.1 将此列为硬约束。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

# 未设置时的占位值 —— 出现在日志里即说明有代码绕过了中间件
_UNSET = "-"

_trace_id: ContextVar[str] = ContextVar("trace_id", default=_UNSET)
_tenant_id: ContextVar[str] = ContextVar("tenant_id", default=_UNSET)
_session_id: ContextVar[str] = ContextVar("session_id", default=_UNSET)
_user_id: ContextVar[str] = ContextVar("user_id", default=_UNSET)
_node: ContextVar[str] = ContextVar("node", default=_UNSET)


def new_trace_id() -> str:
    """生成新的链路 ID。"""
    return uuid.uuid4().hex


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(value: str) -> Token:
    return _trace_id.set(value or _UNSET)


def get_tenant_id() -> str:
    return _tenant_id.get()


def set_tenant_id(value: str) -> Token:
    return _tenant_id.set(value or _UNSET)


def get_session_id() -> str:
    return _session_id.get()


def set_session_id(value: str) -> Token:
    return _session_id.set(value or _UNSET)


def get_user_id() -> str:
    return _user_id.get()


def set_user_id(value: str) -> Token:
    return _user_id.set(value or _UNSET)


def get_node() -> str:
    return _node.get()


def set_node(value: str) -> Token:
    return _node.set(value or _UNSET)


def reset(token: Token) -> None:
    """按 token 还原上一次取值。"""
    var = token.var
    var.reset(token)


@dataclass(frozen=True)
class TraceContext:
    """上下文快照，便于跨进程/跨调用传递。"""

    trace_id: str
    tenant_id: str
    session_id: str
    user_id: str
    node: str

    @classmethod
    def current(cls) -> TraceContext:
        return cls(
            trace_id=get_trace_id(),
            tenant_id=get_tenant_id(),
            session_id=get_session_id(),
            user_id=get_user_id(),
            node=get_node(),
        )

    def as_headers(self) -> dict[str, str]:
        """转换为出站 HTTP 头，保证 Mock 服务侧能续接同一条链路。"""
        return {
            "X-Trace-ID": self.trace_id,
            "X-Tenant-ID": self.tenant_id,
            "X-Session-ID": self.session_id,
        }

    def as_log_fields(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "node": self.node,
        }


@contextmanager
def bind(
    *,
    trace_id: str | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    node: str | None = None,
) -> Iterator[TraceContext]:
    """绑定一组上下文，退出时自动还原。

    用法::

        with bind(tenant_id="t-001", session_id="s-1") as ctx:
            ...  # 期间日志自动带上下文
    """
    tokens: list[Token] = []
    if trace_id is not None:
        tokens.append(set_trace_id(trace_id))
    if tenant_id is not None:
        tokens.append(set_tenant_id(tenant_id))
    if session_id is not None:
        tokens.append(set_session_id(session_id))
    if user_id is not None:
        tokens.append(set_user_id(user_id))
    if node is not None:
        tokens.append(set_node(node))
    try:
        yield TraceContext.current()
    finally:
        for token in reversed(tokens):
            token.var.reset(token)


@contextmanager
def bind_node(node: str) -> Iterator[None]:
    """只切换当前节点名（图节点内使用）。"""
    token = set_node(node)
    try:
        yield
    finally:
        _node.reset(token)


@contextmanager
def ensure_tenant(tenant_id: str) -> Iterator[str]:
    """确保当前上下文有租户。

    在**非请求上下文**（后台任务、派单、命令行脚本）调用领域服务时，
    ``tenant_id`` 往往只作为参数传入而没有绑定到上下文，
    此时数据库的租户闸门会拒绝执行（这是设计使然，不是缺陷）。

    本上下文管理器解决该问题：上下文已有租户则原样透传，缺失则临时绑定，
    退出时自动还原。因此领域服务无论被谁调用都能安全工作，
    且**不会覆盖**调用方已有的租户——若两者不一致则明确报错，
    而不是静默择一（静默择一会造成难以定位的跨租户写入）。
    """
    existing = get_tenant_id()
    if existing != _UNSET:
        if existing != tenant_id:
            raise ValueError(f"租户上下文冲突：上下文中为 {existing}，调用参数为 {tenant_id}")
        yield existing
        return

    token = set_tenant_id(tenant_id)
    try:
        yield tenant_id
    finally:
        _tenant_id.reset(token)
