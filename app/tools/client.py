"""外部系统调用客户端（工具层基础）。

所有对 Mock 后端（生产环境即真实业务系统）的调用都经这里，统一具备：

  1. **超时**：``MOCK_TIMEOUT_SECONDS``（默认 5s），超时按矩阵降级
  2. **重试**：``MOCK_MAX_RETRIES``（默认 2 次），仅对可重试错误重试
  3. **出参校验**：用 Pydantic 校验响应结构；``bad_data`` 模式会被这里拦下
  4. **链路透传**：自动带 ``X-Trace-ID`` / ``X-Tenant-ID``，Mock 侧可续接同一链路
  5. **异常归一化**：把 HTTP/超时/校验失败统一成 ``ToolError``，业务层只处理一种异常
  6. **落库**：每次调用写 ``tool_invocations``，供链路回放与指标统计

生产对接时只需改 ``MOCK_BASE_URL``，本模块与业务代码均无需改动
（接口契约见 docs/mock-boundary.md）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.logging_config import get_logger, warn_degraded
from app.core.trace_context import TraceContext

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class ToolError(RuntimeError):
    """工具调用失败（已归一化）。

    ``kind`` 用于降级矩阵判定：
      timeout / upstream / unauthorized / bad_data / connection / business
    """

    def __init__(self, message: str, *, kind: str = "unknown", code: str = "", retryable: bool = False,
                 status: int = 0, raw: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.retryable = retryable
        self.status = status
        self.raw = raw


@dataclass
class ToolCall:
    """一次工具调用的记录（用于落库与回放）。"""

    tool_name: str
    target: str
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    http_status: int = 0
    ok: bool = True
    error_code: str = ""
    retry_count: int = 0
    duration_ms: float = 0.0
    raw_response: str = ""


class ToolClient:
    """带超时、重试与校验的 HTTP 客户端。

    ``default_headers`` 用于给该客户端的所有调用附加固定头，
    典型用途是异常专项测试注入 ``X-Mock-Mode``，或给某类调用附加租户级标记。
    """

    def __init__(self, base_url: str | None = None, default_headers: dict[str, str] | None = None) -> None:
        self.base_url = (base_url or settings.mock_url).rstrip("/")
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.calls: list[ToolCall] = []

    # ------------------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        ctx = TraceContext.current()
        headers = {"Content-Type": "application/json"}
        headers.update(self.default_headers)
        # 链路头：调用方显式传入可覆盖
        headers.update(ctx.as_headers())
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        tool_name: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        """发起调用，返回 ``data`` 字段（已取出 data 字段）。

        ``content`` 用于**签名场景**：需要与服务端对同一段字节做 HMAC 时，
        必须由调用方把 body 预先序列化，不能交给 HTTP 库二次序列化
        （否则客户端签名用的字节与服务端收到的不一致，签名必然失败）。

        失败抛 ``ToolError``。
        """
        if content is not None and json_body is not None:
            raise ValueError("content 与 json_body 不能同时提供")

        url = f"{self.base_url}{path}"
        timeout_s = timeout if timeout is not None else settings.mock_timeout_seconds
        retries = max_retries if max_retries is not None else settings.mock_max_retries
        attempts = max(1, retries + 1)

        record = ToolCall(tool_name=tool_name, target=url, request=params or json_body or {})
        started = time.perf_counter()
        last_exc: Exception | None = None

        for attempt in range(attempts):
            record.retry_count = attempt
            try:
                with httpx.Client(timeout=timeout_s) as client:
                    resp = client.request(
                        method.upper(),
                        url,
                        params=params,
                        json=json_body,
                        content=content,
                        headers=self._headers(headers),
                    )
                record.http_status = resp.status_code
                record.raw_response = resp.text[:2000]

                if resp.status_code >= 400:
                    code, message, retryable = self._parse_error(resp)
                    if resp.status_code == 401:
                        raise ToolError(message, kind="unauthorized", code=code, status=resp.status_code,
                                        retryable=False, raw=record.raw_response)
                    raise ToolError(message, kind="upstream", code=code, status=resp.status_code,
                                    retryable=retryable, raw=record.raw_response)

                payload = resp.json()
                if not payload.get("ok", True):
                    err = payload.get("error") or {}
                    raise ToolError(
                        err.get("message", "业务错误"),
                        kind="business",
                        code=err.get("code", ""),
                        retryable=bool(err.get("retryable")),
                        status=resp.status_code,
                        raw=record.raw_response,
                    )
                data = payload.get("data", payload)
                record.response = data if isinstance(data, dict) else {"value": data}
                record.ok = True
                record.duration_ms = (time.perf_counter() - started) * 1000
                self.calls.append(record)
                log.info(
                    "tool.call.ok",
                    tool=tool_name,
                    target=path,
                    status=record.http_status,
                    duration_ms=round(record.duration_ms, 2),
                    attempt=attempt + 1,
                )
                return data

            except httpx.TimeoutException as exc:
                last_exc = exc
                log.warning("tool.call.timeout", tool=tool_name, target=path, attempt=attempt + 1,
                            timeout_s=timeout_s)
                if attempt >= attempts - 1:
                    record.ok = False
                    record.error_code = "TOOL_TIMEOUT"
                    record.duration_ms = (time.perf_counter() - started) * 1000
                    self.calls.append(record)
                    raise ToolError(f"调用超时（{timeout_s}s）: {path}", kind="timeout",
                                    code="TOOL_TIMEOUT", retryable=True,
                                    raw=record.raw_response) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("tool.call.connection_error", tool=tool_name, target=path,
                            attempt=attempt + 1, error=str(exc)[:200])
                if attempt >= attempts - 1:
                    record.ok = False
                    record.error_code = "TOOL_CONNECTION"
                    record.duration_ms = (time.perf_counter() - started) * 1000
                    self.calls.append(record)
                    raise ToolError(f"连接失败: {exc}", kind="connection", code="TOOL_CONNECTION",
                                    retryable=True, raw=record.raw_response) from exc
            except ToolError as exc:
                last_exc = exc
                if not exc.retryable or attempt >= attempts - 1:
                    record.ok = False
                    record.error_code = exc.code or exc.kind
                    record.duration_ms = (time.perf_counter() - started) * 1000
                    self.calls.append(record)
                    if exc.kind == "upstream":
                        warn_degraded("tool.upstream_error", tool=tool_name, target=path,
                                      code=exc.code, status=exc.status)
                    raise
                log.warning("tool.call.retry", tool=tool_name, target=path, code=exc.code,
                            attempt=attempt + 1)

        raise ToolError(f"调用失败: {last_exc}", kind="unknown", retryable=False)

    @staticmethod
    def _parse_error(resp: httpx.Response) -> tuple[str, str, bool]:
        try:
            body = resp.json()
            err = body.get("error") or {}
            return err.get("code", ""), err.get("message", resp.text[:200]), bool(err.get("retryable"))
        except Exception:  # noqa: BLE001
            return "", resp.text[:200], resp.status_code >= 500

    # ------------------------------------------------------------------
    def validated(
        self,
        schema: type[T],
        tool_name: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> T:
        """发起调用并用 Pydantic 校验结构。

        ``X-Mock-Mode: bad_data`` 会在这里被拦下并转成 ``bad_data`` 类错误，
        业务层据此走"工具返回格式异常"的降级路径。
        """
        data = self.request(tool_name, method, path, **kwargs)
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            warn_degraded("tool.schema_invalid", tool=tool_name, target=path,
                          errors=exc.error_count(), detail=str(exc)[:200])
            raise ToolError(
                f"工具返回结构不符合契约: {exc.error_count()} 处错误",
                kind="bad_data",
                code="TOOL_SCHEMA_INVALID",
                retryable=False,
                raw=json.dumps(data, ensure_ascii=False)[:500],
            ) from exc

    # ------------------------------------------------------------------
    def health(self) -> tuple[bool, str]:
        """Mock 后端连通性。"""
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/health")
            return resp.status_code == 200, resp.text[:120]
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def stats(self) -> dict[str, Any]:
        """本次进程内调用统计。"""
        ok = [c for c in self.calls if c.ok]
        return {
            "calls": len(self.calls),
            "ok": len(ok),
            "failed": len(self.calls) - len(ok),
            "retries": sum(c.retry_count for c in self.calls),
            "avg_duration_ms": round(sum(c.duration_ms for c in ok) / len(ok), 2) if ok else 0.0,
        }


_client: ToolClient | None = None


def get_tool_client(refresh: bool = False) -> ToolClient:
    """获取工具客户端单例。"""
    global _client
    if _client is None or refresh:
        _client = ToolClient()
        log.info("tool.client.ready", base_url=_client.base_url)
    return _client
