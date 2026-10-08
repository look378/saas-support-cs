"""网关中间件：链路追踪、租户识别、鉴权、限流。

**中间件顺序很重要**，注册顺序即执行顺序（先注册先进入）：
  1. ``TraceMiddleware``   —— 最先，让后续所有日志都带 trace_id
  2. ``TenantMiddleware``  —— 校验租户并绑定上下文（后续所有 DB 访问都依赖它）
  3. ``AuthMiddleware``    —— 需要租户上下文才能正确签发/校验
  4. ``RateLimitMiddleware``—— 最后，前面都合法才消耗配额

顺序颠倒会导致"日志没有 trace_id"或"限流按错误维度计数"这类隐蔽问题。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.redis_client import key_rate_limit, token_bucket_allow
from app.core.trace_context import (
    new_trace_id,
    set_trace_id,
)
from app.security import TokenError, bearer_token, decode_token

log = get_logger(__name__)

# 不需要租户 / 鉴权的路径（健康检查、文档、静态资源）
# 注意：路径必须与**实际注册的路由**一致。此处曾写 "/health/deps"，
# 而真实路由是 "/api/v1/health/deps"，导致该条目永不匹配、
# 依赖健康检查被静默要求租户头（配置与行为不一致）。
PUBLIC_PATHS = {
    "/health",
    "/api/v1/health/deps",
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
    "/static",
}
# 坐席端路径（必须坐席令牌）
AGENT_PREFIXES = ("/api/v1/agent", "/api/v1/console")
# 坐席端中**不需要令牌**的路径：登录本身是获取令牌的入口，
# 若也要求令牌会形成"先有鸡还是先有蛋"（本项目实测踩到）。
AGENT_EXEMPT_PATHS = {"/api/v1/agent/login"}


def _is_public(path: str) -> bool:
    """仅表示"不需要鉴权"，**不表示"不需要租户"**。

    这两个概念必须分开：例如坐席登录不需要令牌（还没有令牌），
    但它必须能识别租户（令牌是签发给某租户的坐席的）。
    早期把二者混为一谈，导致登录接口在租户识别阶段被整体跳过，
    进而在查询坐席表时撞上租户闸门（本项目实测踩到）。
    """
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in ("/docs", "/static", "/openapi", "/redoc"))


def _tenant_free(path: str) -> bool:
    """真正不需要租户上下文的路径（健康检查与文档）。"""
    return _is_public(path)


class TraceMiddleware(BaseHTTPMiddleware):
    """生成/透传 trace_id，并记录请求级耗时。"""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("X-Trace-ID") or ""
        trace_id = incoming or new_trace_id()
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            log.error(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(elapsed, 2),
            )
            raise
        else:
            elapsed = (time.perf_counter() - started) * 1000
            response.headers["X-Trace-ID"] = trace_id
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(elapsed, 2),
            )
            return response
        finally:
            token.var.reset(token)


class TenantMiddleware(BaseHTTPMiddleware):
    """租户识别：``X-Tenant-ID`` → 校验有效性 → 绑定上下文。

    租户校验走**只读从库**，且这个过程本身不能依赖租户上下文
    （否则就成了先有鸡还是先有蛋），因此用不带闸门的直接查询。
    """

    def __init__(self, app: Any, *, strict: bool = True) -> None:
        super().__init__(app)
        self.strict = strict

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        # 只有真正的公共路径（健康检查/文档）才跳过租户识别；
        # 坐席登录虽然免鉴权，但**必须**识别租户。
        if _tenant_free(path):
            return await call_next(request)

        tenant_id = (request.headers.get("X-Tenant-ID") or "").strip()
        if not tenant_id:
            if self.strict:
                return JSONResponse(
                    status_code=400,
                    content={
                        "ok": False,
                        "error": {"code": "TENANT_REQUIRED", "message": "缺少请求头 X-Tenant-ID"},
                    },
                )
            return await call_next(request)

        valid, detail = _validate_tenant(tenant_id)
        if not valid:
            log.warning("tenant.rejected", tenant_id=tenant_id, detail=detail)
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": {"code": "TENANT_INVALID", "message": f"租户无效：{detail}"},
                },
            )

        from app.core.trace_context import set_tenant_id

        # 注意：这里**不重置**租户上下文。
        # 请求处理链路（路由函数 → 领域服务 → 数据库闸门）全程都需要租户；
        # 若在中间件返回前重置，路由函数执行时租户已丢失，
        # 数据库闸门会拒绝所有租户表访问（表现为接口 500，本项目实测踩到）。
        # 每个请求由独立的 asyncio 任务承载，其 ContextVar 上下文随任务结束回收，
        # 因此不需要也不应该在此处手动清理。
        set_tenant_id(tenant_id)
        return await call_next(request)


def _validate_tenant(tenant_id: str) -> tuple[bool, str]:
    """校验租户存在且启用。查询不经过租户闸门。

    这里**必须读主库**而不是从库：租户校验属于一致性敏感操作。
    若读从库，刚创建/刚更新的租户会因复制延迟被判为"不存在"，
    表现为"刚建的租户立刻报 403"（本项目实测踩到）。
    """
    try:
        from sqlalchemy import text

        from app.core.database import get_primary_engine

        with get_primary_engine().connect() as conn:
            row = conn.execute(
                text("SELECT is_active FROM tenants WHERE tenant_key = :k"), {"k": tenant_id}
            ).first()
        if row is None:
            return False, "租户不存在"
        if not row[0]:
            return False, "租户已停用"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        log.error("tenant.validate_failed", tenant_id=tenant_id, error=str(exc)[:200])
        # 校验依赖不可用时**拒绝**而不是放行——安全的默认选择
        return False, f"租户校验服务不可用: {type(exc).__name__}"


class AuthMiddleware(BaseHTTPMiddleware):
    """鉴权：坐席端路径必须坐席令牌；其它受保护路径可选用户令牌。"""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        # 免鉴权路径 = 真正的公共路径 + 坐席登录（登录是获取令牌的入口）。
        # 注意这里只跳过**鉴权**，不跳过租户识别——登录必须知道是哪个租户的坐席。
        if _is_public(path) or path in AGENT_EXEMPT_PATHS:
            return await call_next(request)

        needs_agent = any(path.startswith(p) for p in AGENT_PREFIXES)
        token = bearer_token(request.headers.get("Authorization"))

        if needs_agent:
            if not token:
                return _unauthorized("AGENT_TOKEN_REQUIRED", "坐席接口需要 Bearer 令牌")
            try:
                claims = decode_token(token, audience="agent")
            except TokenError as exc:
                return _unauthorized(f"AGENT_TOKEN_{exc.kind.upper()}", str(exc))
            # 令牌租户必须与请求头租户一致，否则属于越权
            header_tenant = (request.headers.get("X-Tenant-ID") or "").strip()
            if header_tenant and claims.tenant_id != header_tenant:
                log.warning(
                    "auth.tenant_mismatch",
                    token_tenant=claims.tenant_id,
                    header_tenant=header_tenant,
                    subject=claims.subject,
                )
                return _unauthorized("TENANT_MISMATCH", "令牌所属租户与请求头不一致")
            request.state.agent = claims.subject
            request.state.agent_role = claims.role
            return await call_next(request)

        # 非坐席路径：若带了用户令牌则解析并校验租户一致
        if token:
            try:
                claims = decode_token(token, audience="user")
                header_tenant = (request.headers.get("X-Tenant-ID") or "").strip()
                if header_tenant and claims.tenant_id != header_tenant:
                    return _unauthorized("TENANT_MISMATCH", "令牌所属租户与请求头不一致")
                request.state.user = claims.subject
            except TokenError as exc:
                return _unauthorized(f"USER_TOKEN_{exc.kind.upper()}", str(exc))
        return await call_next(request)


def _unauthorized(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"ok": False, "error": {"code": code, "message": message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流：Redis 令牌桶，维度 = 租户 + 端点。

    Redis 不可用时放行并告警（避免缓存故障导致业务全面不可用），
    这一取舍在架构文档 §8 中有说明。
    """

    def __init__(self, app: Any, *, capacity: int | None = None, window: int | None = None) -> None:
        super().__init__(app)
        self.capacity = capacity or settings.rate_limit_default
        self.window = window or settings.rate_limit_window_seconds

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not settings.rate_limit_enabled or _is_public(request.url.path):
            return await call_next(request)

        tenant_id = (request.headers.get("X-Tenant-ID") or "anon").strip()
        # 端点维度：用路由前缀而非完整路径，避免 /session/{id} 每个 id 一个桶
        parts = [p for p in request.url.path.split("/") if p]
        endpoint = "/".join(parts[:3]) or "root"
        key = key_rate_limit(tenant_id, endpoint, self.window)

        allowed, remaining = token_bucket_allow(key, self.capacity, self.window)
        if not allowed:
            log.warning("rate_limit.blocked", tenant_id=tenant_id, endpoint=endpoint)
            return JSONResponse(
                status_code=429,
                content={
                    "ok": False,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"请求过于频繁，限制 {self.capacity} 次/{self.window} 秒",
                    },
                },
                headers={"Retry-After": str(self.window)},
            )

        response = await call_next(request)
        if remaining >= 0:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Limit"] = str(self.capacity)
        return response


def install_middlewares(app: Any) -> None:
    """按正确顺序安装中间件。

    Starlette 的 ``add_middleware`` 是**后进先出**：最后添加的最先执行。
    因此这里按"期望执行顺序的逆序"添加。
    """
    app.add_middleware(RateLimitMiddleware)   # 期望第 4 个执行 → 最先添加
    app.add_middleware(AuthMiddleware)        # 第 3
    app.add_middleware(TenantMiddleware)      # 第 2
    app.add_middleware(TraceMiddleware)       # 第 1 → 最后添加，最先执行
