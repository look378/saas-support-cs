"""结构化日志与全链路追踪。

规范（架构文档 §7.1，D1 起冻结）：
- 统一使用本模块的 ``get_logger()``，**禁止 print**。
- 每条日志自动附带固定字段：
  ``trace_id / tenant_id / session_id / user_id / node``。
- 文件输出为 JSON（便于链路回放查询与程序化统计），控制台为彩色可读格式。
- 关键节点必须带 ``duration_ms`` 与 ``status``。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from app.core.config import settings
from app.core.trace_context import TraceContext

_CONFIGURED = False


def _inject_trace_context(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """把当前链路上下文注入每条日志。"""
    ctx = TraceContext.current()
    # 已显式传入的字段优先，不被上下文覆盖
    for key, value in ctx.as_log_fields().items():
        event_dict.setdefault(key, value)
    return event_dict


def _drop_unset(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """剔除未绑定的占位字段，避免日志被 "-" 噪声淹没。"""
    for key in ("tenant_id", "session_id", "user_id", "node"):
        if event_dict.get(key) == "-":
            event_dict.pop(key, None)
    return event_dict


def configure_logging(force: bool = False) -> None:
    """初始化日志系统（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # ---- 标准库 root logger：文件用 JSON ----
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(settings.log_path),
        maxBytes=32 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(level)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ---- structlog 处理器链 ----
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _inject_trace_context,
        _drop_unset,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 文件：JSON；控制台：彩色
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    file_handler.setFormatter(file_formatter)
    console_handler.setFormatter(console_formatter)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取结构化 logger。"""
    configure_logging()
    return structlog.get_logger(name or "saaskefu")


@contextmanager
def timed_step(name: str, **extra: Any) -> Iterator[dict[str, Any]]:
    """记录一次带耗时的关键节点。

    用法::

        with timed_step("tool.query_integration_config", workspace_id="ws-1001") as step:
            result = do_work()
            step["result_count"] = len(result)

    退出时自动输出 ``duration_ms`` 与 ``status``（ok / error）。
    """
    log = get_logger("step")
    started = time.perf_counter()
    payload: dict[str, Any] = dict(extra)
    log.info(f"{name}.start")
    try:
        yield payload
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log.error(
            f"{name}.end",
            duration_ms=round(elapsed, 2),
            status="error",
            error_type=type(exc).__name__,
            error=str(exc),
            **payload,
        )
        raise
    else:
        elapsed = (time.perf_counter() - started) * 1000
        slow = elapsed >= settings.slow_step_threshold_ms
        log.info(
            f"{name}.end",
            duration_ms=round(elapsed, 2),
            status="ok",
            slow=slow,
            **payload,
        )


def warn_degraded(reason: str, **extra: Any) -> None:
    """统一的降级告警入口（容错矩阵的每条降级路径都必须调用它）。"""
    get_logger("degraded").warning("degraded", reason=reason, **extra)
