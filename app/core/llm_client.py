"""大模型客户端（AgentService 隔离层的模型部分）。

本模块封装全部模型调用，业务代码**不得**直接实例化模型客户端。

参数取值全部来自实测（``scripts/llm_probe.py`` / ``llm_probe2.py``），
不得凭文档或经验假设：

========================================  ==========================================
实测发现                                     本模块的处理
========================================  ==========================================
``model=deepseek-chat`` 可用，服务端实际由  沿用配置项，不做硬编码；启动时打印
flash 档模型承载                              "请求模型 → 实际服务模型" 便于核对
结构化输出 ``method="json_schema"`` 返回     一律使用 ``method="function_calling"``
400 "This response_format type is          （实测可用且遵循枚举取值）
unavailable now"
``method="json_mode"`` 要求提示词里必须      不使用该模式
出现 "json" 字样，否则 400
思考模式会耗尽 ``max_tokens``               默认不开启思考；``structured()`` 强制
（实测 max_tokens=64 → content 为空、        关闭思考，避免结构化结果为空
reasoning 长度 120）
========================================  ==========================================

所有调用统一：超时、重试、用量计量、链路字段、异常归一化为 ``LLMUnavailable``，
由调用方（业务层）决定降级话术，隔离层不替业务做产品决策。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """模型不可用（超时/鉴权失败/接口报错/返回异常）。

    业务层捕获此异常后按容错矩阵降级：友好提示 + 转人工。
    """

    def __init__(self, message: str, *, kind: str = "unknown", retryable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@dataclass
class LLMCallRecord:
    """单次调用计量，写入指标看板与链路回放。"""

    kind: str
    model: str
    served_model: str = ""
    duration_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    ok: bool = True
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """模型调用封装。"""

    def __init__(self) -> None:
        self._base_kwargs: dict[str, Any] = {
            "api_key": settings.llm_api_key or "sk-missing",
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "timeout": settings.llm_timeout_seconds,
            "max_retries": 0,  # 重试由本模块统一控制，便于记录与计费
        }
        self.records: list[LLMCallRecord] = []
        self._warn_if_key_missing()

    # ------------------------------------------------------------------
    def _warn_if_key_missing(self) -> None:
        if not settings.llm_api_key or settings.llm_api_key.startswith("sk-your"):
            log.warning(
                "llm.key.missing",
                hint="未配置有效的 LLM_API_KEY，模型调用将失败并走降级路径",
            )

    def _client(self, *, temperature: float | None = None, streaming: bool = False) -> Any:
        from langchain_openai import ChatOpenAI

        kwargs = dict(self._base_kwargs)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if streaming:
            kwargs["streaming"] = True
        return ChatOpenAI(**kwargs)

    # ------------------------------------------------------------------
    def _record(self, rec: LLMCallRecord) -> None:
        self.records.append(rec)

    @staticmethod
    def _extract_usage(resp: Any) -> dict[str, int]:
        usage = getattr(resp, "usage_metadata", None) or {}
        if isinstance(usage, dict):
            return {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        meta = getattr(resp, "response_metadata", None) or {}
        tok = meta.get("token_usage") or {}
        return {
            "prompt_tokens": int(tok.get("prompt_tokens") or 0),
            "completion_tokens": int(tok.get("completion_tokens") or 0),
            "total_tokens": int(tok.get("total_tokens") or 0),
        }

    @staticmethod
    def _classify(exc: Exception) -> tuple[str, bool]:
        """异常归类 → (kind, retryable)。"""
        text = str(exc).lower()
        name = type(exc).__name__.lower()
        if "timeout" in text or "timeout" in name or "timed out" in text:
            return "timeout", True
        if "401" in text or "authentication" in text or "invalid api key" in text:
            return "auth", False
        if "429" in text or "rate limit" in text:
            return "rate_limit", True
        if "500" in text or "502" in text or "503" in text or "504" in text:
            return "upstream", True
        if "connection" in text or "connect" in name:
            return "connection", True
        return "unknown", True

    # ------------------------------------------------------------------
    def invoke(
        self,
        messages: Any,
        *,
        kind: str = "chat",
        temperature: float | None = None,
    ) -> str:
        """纯文本调用。失败抛 ``LLMUnavailable``。"""
        last_exc: Exception | None = None
        attempts = max(1, settings.llm_max_retries + 1)
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                client = self._client(temperature=temperature)
                resp = client.invoke(messages)
                elapsed = (time.perf_counter() - started) * 1000
                usage = self._extract_usage(resp)
                served = (getattr(resp, "response_metadata", None) or {}).get("model_name", "")
                self._record(
                    LLMCallRecord(
                        kind=kind,
                        model=settings.llm_model,
                        served_model=served or "",
                        duration_ms=round(elapsed, 2),
                        ok=True,
                        extra={"attempt": attempt},
                        **usage,
                    )
                )
                log.info(
                    "llm.call.ok",
                    kind=kind,
                    model=settings.llm_model,
                    served_model=served,
                    duration_ms=round(elapsed, 2),
                    tokens=usage.get("total_tokens"),
                    attempt=attempt,
                )
                return str(resp.content or "")
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                kind_err, retryable = self._classify(exc)
                last_exc = exc
                self._record(
                    LLMCallRecord(
                        kind=kind,
                        model=settings.llm_model,
                        duration_ms=round(elapsed, 2),
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}"[:300],
                        extra={"attempt": attempt, "error_kind": kind_err},
                    )
                )
                log.warning(
                    "llm.call.fail",
                    kind=kind,
                    error_kind=kind_err,
                    retryable=retryable,
                    attempt=attempt,
                    duration_ms=round(elapsed, 2),
                    error=str(exc)[:200],
                )
                if not retryable or attempt >= attempts:
                    raise LLMUnavailable(
                        f"模型调用失败({kind_err}): {exc}", kind=kind_err, retryable=retryable
                    ) from exc
        raise LLMUnavailable(f"模型调用失败: {last_exc}")

    # ------------------------------------------------------------------
    def structured(
        self,
        messages: Any,
        schema: type[TModel],
        *,
        kind: str = "structured",
        temperature: float = 0.0,
    ) -> TModel:
        """结构化输出。

        实测结论：该端点只支持 ``function_calling`` 方式，``json_schema`` 会 400，
        ``json_mode`` 额外要求提示词含 "json"。因此这里固定使用 function_calling，
        并在解析失败时回退到"提示词约束 + 自行解析"的兜底路径。
        同时**强制关闭思考模式**，避免推理占满 token 导致结构化结果为空。
        """
        last_exc: Exception | None = None
        attempts = max(1, settings.llm_max_retries + 1)
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                client = self._client(temperature=temperature)
                chain = client.with_structured_output(schema, method="function_calling")
                out = chain.invoke(messages)
                elapsed = (time.perf_counter() - started) * 1000
                if not isinstance(out, schema):
                    # 类型不符属编程/契约错误，用 TypeError 更准确
                    raise TypeError(f"结构化返回类型异常: {type(out).__name__}")
                self._record(
                    LLMCallRecord(
                        kind=kind,
                        model=settings.llm_model,
                        duration_ms=round(elapsed, 2),
                        ok=True,
                        extra={"attempt": attempt, "mode": "function_calling"},
                    )
                )
                log.info(
                    "llm.structured.ok",
                    kind=kind,
                    schema=schema.__name__,
                    duration_ms=round(elapsed, 2),
                    attempt=attempt,
                )
                return out
            except LLMUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                elapsed = (time.perf_counter() - started) * 1000
                # 注意：这里**不因 retryable=False 提前退出**。
                # 结构化路径的重试耗尽后会落到"提示词约束 + 本地解析"的兜底路径，
                # 而 function_calling 失败常常是模型侧对工具调用支持不佳，
                # 兜底路径反而可能成功。因此重试次数用尽即转兜底，
                # ``retryable`` 仅用于日志归因（与文本路径的判据不同，属有意为之）。
                _kind, retryable = self._classify(exc)
                kind_err = _kind
                last_exc = exc
                log.warning(
                    "llm.structured.fail",
                    kind=kind,
                    schema=schema.__name__,
                    error_kind=kind_err,
                    retryable=retryable,
                    attempt=attempt,
                    duration_ms=round(elapsed, 2),
                    error=str(exc)[:200],
                )
                if attempt >= attempts:
                    break
        # 兜底路径：提示词约束 + 自行解析
        try:
            return self._structured_fallback(messages, schema, kind=kind)
        except Exception as exc:
            raise LLMUnavailable(
                f"结构化输出失败: {str(last_exc) if last_exc else exc}",
                kind="structured",
                retryable=False,
            ) from exc

    def _structured_fallback(
        self, messages: Any, schema: type[TModel], *, kind: str
    ) -> TModel:
        """兜底：要求模型只输出 JSON，然后本地解析校验。"""
        fields = "、".join(
            f"{name}({info.annotation})" for name, info in schema.model_fields.items()
        )
        instruction = (
            f"\n\n严格只输出一个 JSON 对象，必须包含字段：{fields}。"
            "不要输出任何其他文字、不要使用代码块标记。"
        )
        msgs = messages + [("human", instruction)] if isinstance(messages, list) else messages
        started = time.perf_counter()
        client = self._client(temperature=0.0)
        resp = client.invoke(msgs)
        elapsed = (time.perf_counter() - started) * 1000
        text = str(resp.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        text = text.strip()
        data = json.loads(text)
        obj = schema(**data)
        self._record(
            LLMCallRecord(
                kind=kind,
                model=settings.llm_model,
                duration_ms=round(elapsed, 2),
                ok=True,
                extra={"mode": "fallback_json_parse"},
                **self._extract_usage(resp),
            )
        )
        log.warning("llm.structured.fallback_used", kind=kind, schema=schema.__name__)
        return obj

    # ------------------------------------------------------------------
    def stream(self, messages: Any, *, kind: str = "stream") -> Any:
        """流式调用，逐块产出文本。调用方负责 SSE 封装。"""
        started = time.perf_counter()
        try:
            client = self._client(streaming=True)
            for chunk in client.stream(messages):
                text = getattr(chunk, "content", "")
                if text:
                    yield str(text)
            elapsed = (time.perf_counter() - started) * 1000
            self._record(
                LLMCallRecord(
                    kind=kind,
                    model=settings.llm_model,
                    duration_ms=round(elapsed, 2),
                    ok=True,
                )
            )
            log.info("llm.stream.ok", kind=kind, duration_ms=round(elapsed, 2))
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            kind_err, retryable = self._classify(exc)
            self._record(
                LLMCallRecord(
                    kind=kind,
                    model=settings.llm_model,
                    duration_ms=round(elapsed, 2),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
            )
            log.warning("llm.stream.fail", kind=kind, error_kind=kind_err, error=str(exc)[:200])
            raise LLMUnavailable(f"流式调用失败({kind_err}): {exc}", kind=kind_err, retryable=retryable) from exc

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """累计调用统计，供指标看板与自检使用。"""
        ok = [r for r in self.records if r.ok]
        return {
            "calls": len(self.records),
            "ok": len(ok),
            "failed": len(self.records) - len(ok),
            "total_tokens": sum(r.total_tokens for r in self.records),
            "avg_duration_ms": round(
                sum(r.duration_ms for r in ok) / len(ok), 2
            ) if ok else 0.0,
        }


_client: LLMClient | None = None


def get_llm_client(refresh: bool = False) -> LLMClient:
    """获取模型客户端单例。"""
    global _client
    if _client is None or refresh:
        _client = LLMClient()
        log.info(
            "llm.client.ready",
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            key=settings.llm_key_status(),
        )
    return _client
