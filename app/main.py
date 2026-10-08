"""应用入口：装配中间件、路由、异常处理与启动自检。

启动顺序（不可颠倒）：
  1. 平台兼容修正（异步事件循环）——必须最先
  2. 日志系统
  3. ORM 模型加载——**租户隔离闸门依赖它**，漏了会静默失效（约束 C-9）
  4. 中间件与路由
  5. 启动自检（打印依赖状态，让"环境是否可用"一眼可见）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ---- 1. 平台兼容：必须在任何异步代码之前 ----
from app.core import platform_compat

platform_compat.apply()

# ---- 2. 日志 ----
from app.core.config import STATIC_DIR, settings
from app.core.logging_config import configure_logging, get_logger

configure_logging()
log = get_logger(__name__)

# ---- 3. ORM 模型（注册租户表名单，否则隔离闸门失效）----
from app.core.database import ensure_models_loaded

ensure_models_loaded()

# ---- 4. 中间件与路由 ----
from app.api.v1 import agent_routes, console_ws, user_routes
from app.gateway.middleware import install_middlewares


def _start_offline_sweep() -> asyncio.Task | None:
    """启动掉线坐席巡检的后台循环。返回任务句柄（关闭时用于取消）。

    设计要点：
      - **进程内**而非外部定时器：容器化部署时不必额外维护 cron/计划任务，
        也不需要外部能访问到 API（演示机常没有对外端口）。
      - 周期来自配置（``AGENT_OFFLINE_SWEEP_SECONDS``，0 = 关闭）。
      - 巡检本身幂等，且异常一律吞掉后继续下一轮——巡检失败绝不能拖垮 API。
      - 先睡后跑：启动瞬间依赖（PG/Redis）可能尚未就绪，立刻跑会刷无谓告警。
    """
    interval = int(settings.agent_offline_sweep_seconds or 0)
    if interval <= 0:
        log.info("app.offline_sweep_disabled")
        return None

    async def loop() -> None:
        from app.services.escalation_service import sweep_all_tenants

        while True:
            await asyncio.sleep(interval)
            try:
                # 数据库/Redis 是同步驱动，必须放到线程里，否则阻塞事件循环
                stats = await asyncio.to_thread(sweep_all_tenants)
                if stats.get("requeued"):
                    log.warning(
                        "app.offline_sweep_requeued",
                        tenants=len(stats.get("tenants", [])),
                        requeued=stats["requeued"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "app.offline_sweep_failed",
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )

    task = asyncio.create_task(loop(), name="offline-sweep")
    log.info("app.offline_sweep_started", interval_seconds=interval)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动与关闭。"""
    log.info(
        "app.starting",
        app=settings.app_name,
        version=settings.app_version,
        env=settings.env,
        llm_model=settings.llm_model,
        llm_key=settings.llm_key_status(),
        embedding=settings.embedding_provider,
        loop_policy=platform_compat.describe(),
    )
    # 启动自检：依赖不可用要在日志里立刻可见，而不是等第一个请求失败
    try:
        import asyncio

        from app.services.health_service import full_health

        # 安全自检：占位签名密钥会让"服务正常但鉴权形同虚设"，
        # 必须在校验令牌之前就喊出来（详见 Settings.secret_findings）。
        for finding in settings.secret_findings:
            log.error("app.insecure_secret", detail=finding)

        report = await asyncio.to_thread(full_health)
        for check in report["checks"]:
            log.info(
                "app.dependency",
                name=check["name"],
                ok=check["ok"],
                detail=str(check["detail"])[:160],
            )
        if report["failed"]:
            log.warning("app.dependency_degraded", failed=report["failed"])

        # 关键自检：异步数据库驱动要求**选择器**事件循环。
        # 若当前循环不是选择器（例如用 uvicorn 命令行启动、策略被覆盖），
        # 诊断流程会全量失败并一律降级为转人工——这里明确报出来，
        # 避免出现"服务能启动、接口却全不能干活"的隐蔽故障。
        try:
            import asyncio as _aio

            loop_name = type(_aio.get_running_loop()).__name__
            log.info("app.event_loop", loop=loop_name)
            if not platform_compat.is_compatible_loop(loop_name):
                log.error(
                    "app.event_loop_incompatible",
                    loop=loop_name,
                    hint="异步数据库驱动需要选择器事件循环；请使用 `python -m app.main` 启动，"
                    "而非 uvicorn 命令行",
                )
        except Exception as exc:  # noqa: BLE001
            # 这条自检本身就出过错（曾因 exc_info 用法导致异常被吞），
            # 若它自己失败必须留痕，不能静默 pass。
            log.warning("app.event_loop_check_failed", error=f"{type(exc).__name__}: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("app.selfcheck_failed", error=str(exc)[:200])

    # 后台巡检：被动掉线的坐席不会发出任何请求，必须由进程内定时器兜住。
    sweep_task = _start_offline_sweep()

    yield

    if sweep_task is not None:
        sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            await sweep_task
    log.info("app.stopping")


app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "SaaS 产品支持的智能客服系统 API。\n\n"
        "- 用户端：对话、诊断链、工单\n"
        "- 坐席端：登录、心跳、队列、接管、办结\n"
        "- 实时：WebSocket 坐席控制台、SSE 流式回复\n\n"
        "多租户：所有请求需带 `X-Tenant-ID`；坐席接口需 `Authorization: Bearer <坐席令牌>`。"
    ),
    version=settings.app_version,
    lifespan=lifespan,
)

# 中间件（顺序在 install_middlewares 中保证）
install_middlewares(app)

# CORS：默认只允许本机前端，不使用通配符
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{settings.streamlit_port}",
        f"http://localhost:{settings.streamlit_port}",
        f"http://127.0.0.1:{settings.api_port}",
        f"http://localhost:{settings.api_port}",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Trace-ID", "X-RateLimit-Remaining", "X-RateLimit-Limit"],
)


# ===========================================================================
# 异常处理：对外不暴露内部细节，内部日志保留完整堆栈
# ===========================================================================
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求参数校验失败。"""
    log.info("http.validation_error", path=request.url.path, errors=exc.errors()[:3])
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法",
                "details": [
                    {"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()[:5]
                ],
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理。

    只回统一错误信息，不回堆栈——避免泄露文件路径、SQL 语句等内部信息
    （这一点是上线前审查中被点出的常见问题）。
    """
    log.error(
        "http.unhandled_error",
        path=request.url.path,
        method=request.method,
        error=f"{type(exc).__name__}: {exc}"[:400],
        # 必须传异常**对象**而不是 True：
        # 这里是 ASGI 异常处理器，异常来自函数参数而非当前 except 块，
        # 此时 sys.exc_info() 为空，传 True 会**静默丢掉真实堆栈**。
        # 传异常对象时 logging 会从对象上取 __traceback__（审查发现）。
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请稍后重试",
            },
        },
    )


# ===========================================================================
# 路由
# ===========================================================================
app.include_router(user_routes.router)
app.include_router(agent_routes.router)
app.include_router(console_ws.router)

# 静态资源：坐席实时控制台与用户端挂件
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", tags=["系统"], summary="服务信息")
async def root() -> dict[str, Any]:
    """根路径：返回服务基本信息与可用入口。"""
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.env,
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "health_deps": "/api/v1/health/deps",
            "user_widget": "/static/user_widget.html",
            "chat": "POST /api/v1/chat",
            "chat_stream": "GET /api/v1/chat/stream",
            "agent_login": "POST /api/v1/agent/login",
            "agent_console_ws": "/ws/agent?tenant_id=<tid>&token=<jwt>",
            "agent_console_page": "/static/agent_console.html",
        },
    }


@app.get("/health", tags=["系统"], summary="存活检查")
async def health() -> dict[str, Any]:
    """轻量存活检查（不访问依赖，用于负载均衡探活）。"""
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


# ===========================================================================
# 直接运行入口
# ===========================================================================
# **必须提供 `python -m app.main` 这个启动方式。**
# 原因：uvicorn 命令行在加载应用后会自行配置事件循环策略，
# 可能覆盖掉本项目设置的 Windows 选择器策略；而异步数据库驱动
# （psycopg 异步连接）**不能**在 Proactor 循环上运行，会抛：
#     Psycopg cannot use the 'ProactorEventLoop' to run in async mode
# 表现为"接口 500、诊断一律降级为转人工"，且只在通过 uvicorn CLI 启动时出现，
# pytest（进程内）完全正常——极难定位（本项目实测踩到）。
# 在本入口里先设策略、再调用 uvicorn.run，可保证顺序正确。
def _main() -> None:
    """启动 HTTP 服务（自己掌控事件循环）。

    **为什么不直接用 ``uvicorn.run()``**：
    uvicorn 的循环装配逻辑在 Windows 上会安装 ``ProactorEventLoop``，
    即使显式传 ``loop="asyncio"`` 也无法阻止——实测结果是
    "策略已是 WindowsSelectorEventLoopPolicy，运行中的循环却仍是 Proactor"。
    而 psycopg 的异步连接**不能**在 Proactor 循环上运行，导致
    诊断流程全量失败并一律降级为转人工。

    因此这里自己创建选择器事件循环，再用 uvicorn 的 Server API 运行，
    完全绕开 uvicorn 的循环装配。
    """
    import asyncio
    import sys

    import uvicorn

    # 1) 先设定策略，再显式创建循环（双保险）
    platform_compat.apply()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    log.info(
        "app.serve",
        host=settings.api_host,
        port=settings.api_port,
        loop=type(loop).__name__,
        compatible=platform_compat.is_compatible_loop(type(loop).__name__),
    )

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        log.info("app.interrupted")
    finally:
        try:
            loop.close()
        except Exception as exc:  # noqa: BLE001
            # 关闭事件循环失败不影响进程退出，但要留痕，避免静默掩盖资源泄漏
            log.warning(
                "app.loop_close_failed", error=f"{type(exc).__name__}: {exc}"[:200]
            )


if __name__ == "__main__":
    _main()
