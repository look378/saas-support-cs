"""数据库访问层（主从读写分离 + 租户隔离 + 从库故障自动回退）。

设计要点（对应 docs/architecture.md §5.1 / §5.3）：

1. **读写分离**：写操作走主库（5432），只读查询走从库（5433）。
   ``read_session()`` 显式声明意图，避免"默认走主库"导致读写分离形同虚设。

2. **从库故障回退**：从库不可用时自动回退主库并写降级告警日志，
   业务不中断。回退状态带冷却期，避免每个请求都去探测故障从库。

3. **租户隔离**：``tenant_id`` 由 ``contextvars`` 承载，Session 工厂在建立
   会话时把当前租户写入会话信息（``session.info['tenant_id']``），
   由 ORM 层的事件钩子强制校验：**访问带 tenant_id 的表时若上下文缺失租户，
   直接抛错而不是静默返回全量数据**。这条"宁失败不越权"的规则是防止跨租户
   串数据的最后一道闸门。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging_config import get_logger, warn_degraded
from app.core.trace_context import get_tenant_id

log = get_logger(__name__)


class TenantContextMissing(RuntimeError):
    """访问租户数据时缺少租户上下文。

    这是**故意的硬失败**：宁可直接报错，也不允许在无租户上下文时执行查询，
    因为那会退化成"跨租户读取全部数据"。
    """



# ---------------------------------------------------------------------------
# 引擎构建
# ---------------------------------------------------------------------------
def _engine_kwargs() -> dict[str, Any]:
    return {
        "pool_pre_ping": True,          # 取连接前探活，避免使用已断开的连接
        "pool_size": settings.pg_pool_size,
        "max_overflow": settings.pg_max_overflow,
        "pool_recycle": 1800,
        "future": True,
        "echo": False,
    }


_primary_engine: Engine | None = None
_replica_engine: Engine | None = None
_replica_failed_until: float = 0.0
_REPLICA_COOLDOWN_SECONDS = 15.0


def get_primary_engine() -> Engine:
    global _primary_engine
    if _primary_engine is None:
        _primary_engine = create_engine(settings.primary_dsn, **_engine_kwargs())
        _attach_tenant_guard(_primary_engine)
        log.info("db.engine.primary.ready", dsn=_mask(settings.primary_dsn))
    return _primary_engine


def get_replica_engine() -> Engine:
    global _replica_engine
    if _replica_engine is None:
        _replica_engine = create_engine(settings.replica_dsn, **_engine_kwargs())
        _attach_tenant_guard(_replica_engine)
        log.info("db.engine.replica.ready", dsn=_mask(settings.replica_dsn))
    return _replica_engine


def _mask(dsn: str) -> str:
    import re

    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)


def _attach_tenant_guard(engine: Engine) -> None:
    """ORM 事件钩子：执行语句前校验租户上下文。

    只对**带 tenant_id 列的业务表**生效；状态持久化表等系统表不受影响。
    命中租户表且当前无租户上下文时，直接拒绝执行——这是防止跨租户读取的
    最后一道闸门，宁可报错也不放行。
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn, cursor, statement, parameters, context, executemany):
        if get_tenant_id() != _UNSET_VALUE:
            return
        table = _statement_touches_tenant_table(statement)
        if table:
            log.error(
                "db.tenant.guard_triggered",
                table=table,
                statement=statement[:160],
                hint="未绑定租户上下文即访问租户表，已阻止执行",
            )
            raise TenantContextMissing(
                f"访问租户表 {table} 前必须绑定租户上下文"
                "（请求头 X-Tenant-ID 或 with bind(tenant_id=...)）"
            )


# 由 models 注册后填充；在此处硬编码表名可避免 import 循环
_TENANT_TABLES: set[str] = set()
_UNSET_VALUE = "-"
_TENANT_TABLE_RE: re.Pattern[str] | None = None


def register_tenant_tables(names: set[str]) -> None:
    """注册需要租户隔离的表名（由 ORM 模块在导入时调用）。"""
    global _TENANT_TABLE_RE
    _TENANT_TABLES.update(n.lower() for n in names)
    # 用词边界匹配，避免 "sessions" 误命中 "user_sessions_backup" 之类的近似名
    alternation = "|".join(sorted((re.escape(t) for t in _TENANT_TABLES), key=len, reverse=True))
    _TENANT_TABLE_RE = re.compile(
        rf"(?:\bfrom\b|\bjoin\b|\binto\b|\bupdate\b|\bdelete\s+from\b)\s+\"?({alternation})\"?\b",
        re.IGNORECASE,
    )


def ensure_models_loaded() -> None:
    """确保 ORM 模型已导入，从而使租户表名单完成注册。

    这是一个**必须显式调用**的步骤：若模型未导入，租户表名单为空，
    执行钩子就无从判断，闸门会静默失效（Spike 阶段实际踩到）。
    所有入口（CLI、应用启动、测试夹具）都应先调用它。
    """
    import app.models  # noqa: F401  （导入即注册）


def _statement_touches_tenant_table(statement: str) -> str | None:
    """判断语句是否访问了租户表，返回命中的表名。"""
    if _TENANT_TABLE_RE is None:
        return None
    m = _TENANT_TABLE_RE.search(statement)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# 会话工厂
# ---------------------------------------------------------------------------
_primary_sessionmaker: sessionmaker | None = None
_replica_sessionmaker: sessionmaker | None = None


def _sm(engine: Engine, readonly: bool) -> sessionmaker:
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False,
        info={"readonly": readonly},
    )


def _get_primary_sm() -> sessionmaker:
    global _primary_sessionmaker
    if _primary_sessionmaker is None:
        _primary_sessionmaker = _sm(get_primary_engine(), readonly=False)
    return _primary_sessionmaker


def _get_replica_sm() -> sessionmaker:
    global _replica_sessionmaker
    if _replica_sessionmaker is None:
        _replica_sessionmaker = _sm(get_replica_engine(), readonly=True)
    return _replica_sessionmaker


@contextmanager
def write_session() -> Iterator[Session]:
    """写会话（主库）。"""
    session = _get_primary_sm()()
    _bind_info(session)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_primary_session() -> Iterator[Session]:
    """一致性敏感的只读会话（**强制读主库**）。

    用途：鉴权/授权类查询。``read_session()`` 优先走从库，而复制延迟会让
    "刚被停用的坐席仍能登录""刚改的口令旧值仍可用"这类问题间歇性出现——
    安全判定不能建立在可能过期的快照上（本项目的租户校验出于同样原因走主库）。
    """
    session = _get_primary_sm()()
    _bind_info(session)
    try:
        yield session
    finally:
        session.close()


@contextmanager
def read_session() -> Iterator[Session]:
    """只读会话（优先从库，从库不可用则回退主库）。

    实现上刻意写成**显式生成器**而不是复合 try/except/finally：
    ``@contextmanager`` 要求生成器"只 yield 一次、且异常不被吞掉"。
    若把 yield 放在多层 try 里，异常路径可能让生成器再次挂起，
    从而抛出 ``RuntimeError: generator didn't stop after throw()``
    （本项目实测踩到）。这里每个分支各自 try/finally，结构清晰、yield 唯一。
    """
    global _replica_failed_until

    use_replica = settings.pg_replica_enabled and time.time() >= _replica_failed_until

    if use_replica:
        session = _get_replica_sm()()
        _bind_info(session)
        try:
            # 轻量探活：确认从库真的可用，而非仅连接池可建立
            session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - 从库不可用 → 回退主库
            session.close()
            _replica_failed_until = time.time() + _REPLICA_COOLDOWN_SECONDS
            warn_degraded(
                "db.replica.fallback_to_primary",
                error=f"{type(exc).__name__}: {exc}"[:200],
                cooldown_seconds=_REPLICA_COOLDOWN_SECONDS,
                hint="只读查询已回退主库，业务不中断",
            )
        else:
            try:
                yield session
            finally:
                session.close()
            return

    # 回退路径：读主库
    session = _get_primary_sm()()
    _bind_info(session)
    session.info["read_fallback"] = True
    try:
        yield session
    finally:
        session.close()


def _bind_info(session: Session) -> None:
    """把当前链路与租户信息绑定到会话，便于日志与调试。"""
    from app.core.trace_context import get_session_id, get_trace_id

    session.info["tenant_id"] = get_tenant_id()
    session.info["trace_id"] = get_trace_id()
    session.info["app_session_id"] = get_session_id()


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
def health() -> dict[str, Any]:
    """返回主库/从库可用性与复制状态。"""
    result: dict[str, Any] = {
        "primary": {"ok": False, "detail": ""},
        "replica": {"ok": False, "detail": "", "in_recovery": None},
        "replication": {"ok": False, "detail": "", "clients": 0},
    }
    try:
        with get_primary_engine().connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            result["primary"] = {"ok": True, "detail": str(version)[:60]}
            # 统计视图在旧快照下会返回陈旧（甚至全 NULL）的行，先刷新快照再读，
            # 否则会把"复制正常"误报成"无复制连接"。
            conn.execute(text("SELECT pg_stat_clear_snapshot()"))
            rows = conn.execute(
                text(
                    "SELECT client_addr::text, state, sync_state, "
                    "sent_lsn::text, replay_lsn::text FROM pg_stat_replication"
                )
            ).all()
            live = [r for r in rows if r[1] == "streaming"]
            result["replication"] = {
                "ok": len(live) > 0,
                "detail": "; ".join(f"{r[0] or 'local'}/{r[1]}/{r[2]}" for r in live)
                or "无活动复制连接",
                "clients": len(live),
                "stale_rows": len(rows) - len(live),
            }
    except Exception as exc:  # noqa: BLE001
        result["primary"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}

    try:
        with get_replica_engine().connect() as conn:
            in_recovery = conn.execute(text("SELECT pg_is_in_recovery()")).scalar_one()
            result["replica"] = {
                "ok": True,
                "detail": f"in_recovery={in_recovery}",
                "in_recovery": bool(in_recovery),
            }
    except Exception as exc:  # noqa: BLE001
        result["replica"] = {
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "in_recovery": None,
        }
    return result
