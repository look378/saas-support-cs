"""大模型端点连通性与能力实测。

不做任何假设：**模型名以实际 HTTP 响应为准**，不采信二手信息。
逐项验证：

  1. 密钥是否有效、端点是否可达
  2. 候选模型名逐个实测（可用 / 不可用的真实结论）
  3. 结构化输出（诊断结论落库的前提）
  4. 工具调用（技能 2 远程修复动作的前提）
  5. 流式输出（用户端流式回复的前提）
  6. 时延与用量（写入指标看板的口径基线）

运行：
    .venv\\Scripts\\python.exe scripts\\llm_probe.py [模型名 ...]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

CANDIDATES = sys.argv[1:] or [
    settings.llm_model,
    "deepseek-chat",
    "deepseek-flash",
    "deepseek-v4-pro",
]
# 去重保序
_seen: set[str] = set()
CANDIDATES = [m for m in CANDIDATES if not (m in _seen or _seen.add(m))]

print("=" * 74)
print("  大模型端点实测")
print("=" * 74)
print(f"  base_url : {settings.llm_base_url}")
print(f"  key      : {settings.llm_key_status()}")
print(f"  候选模型 : {CANDIDATES}")
print("=" * 74)


def raw_http_probe(model: str) -> dict:
    """最底层 HTTP 探测，返回状态码与响应体（不抛异常）。"""
    import httpx

    url = f"{settings.llm_base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是客服助手，回答简短。"},
            {"role": "user", "content": "回复两个字：收到"},
        ],
        "max_tokens": 16,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, json=payload, headers=headers)
        elapsed = (time.perf_counter() - started) * 1000
        body: object
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text[:400]
        return {"status": resp.status_code, "ms": round(elapsed, 1), "body": body}
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        return {"status": None, "ms": round(elapsed, 1), "body": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 1) 模型可用性
print("\n[1] 候选模型名逐个实测")
usable: list[str] = []
for model in CANDIDATES:
    r = raw_http_probe(model)
    if r["status"] == 200 and isinstance(r["body"], dict):
        content = (
            (r["body"].get("choices") or [{}])[0].get("message", {}).get("content", "")
        )
        served = r["body"].get("model", "?")
        usage = r["body"].get("usage") or {}
        print(
            f"  [可用] {model:<20} HTTP 200  {r['ms']:>7.1f}ms  "
            f"实际服务模型={served}  回复={content!r}  tokens={usage.get('total_tokens')}"
        )
        usable.append(model)
    else:
        detail = r["body"]
        if isinstance(detail, dict):
            detail = (detail.get("error") or {}).get("message") or json.dumps(
                detail, ensure_ascii=False
            )[:200]
        print(f"  [不可用] {model:<18} HTTP {r['status']}  {str(detail)[:160]}")

if not usable:
    print("\n没有可用模型，后续能力测试跳过。请检查密钥与网络。")
    sys.exit(1)

MODEL = usable[0]
print(f"\n  → 后续能力测试使用首个可用模型: {MODEL}")


# ---------------------------------------------------------------- 2) 结构化输出
print("\n[2] 结构化输出（诊断结论落库前提）")
from pydantic import BaseModel, Field  # noqa: E402


class Diagnosis(BaseModel):
    """诊断结论。"""

    cause: str = Field(description="故障根因分类")
    confidence: float = Field(description="置信度 0-1")
    evidence: list[str] = Field(default_factory=list, description="支撑证据")


structured_ok = False
try:
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=MODEL,
        timeout=settings.llm_timeout_seconds,
        temperature=0,
    )
    chain = llm.with_structured_output(Diagnosis)
    t0 = time.perf_counter()
    out = chain.invoke(
        "用户反馈：钉钉机器人从昨天开始不再回复消息。查询发现访问令牌已于昨日到期。"
        "请给出根因分类（从 TOKEN_EXPIRED / WEBHOOK_INVALID / SCOPE_REVOKED 中选一个）、"
        "置信度和证据列表。"
    )
    ms = (time.perf_counter() - t0) * 1000
    print(f"  [通过] {ms:.0f}ms  cause={out.cause!r} confidence={out.confidence} evidence={out.evidence}")
    structured_ok = isinstance(out, Diagnosis) and out.cause.upper().startswith("TOKEN_EXPIRED")
    print(f"  → 结论与预期一致: {structured_ok}")
except Exception as exc:  # noqa: BLE001
    print(f"  [失败] {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 3) 工具调用
print("\n[3] 工具调用（技能 2 远程修复动作前提）")
tool_ok = False
try:
    from langchain_core.tools import tool

    @tool
    def query_integration_config(workspace_id: str) -> str:
        """查询某个工作区的集成配置状态，包含令牌有效期与回调地址可达性。"""
        return json.dumps(
            {
                "workspace_id": workspace_id,
                "token_expires_at": "2026-01-01T00:00:00+08:00",
                "webhook_reachable": False,
                "health": "degraded",
            },
            ensure_ascii=False,
        )

    bound = llm.bind_tools([query_integration_config])
    t0 = time.perf_counter()
    msg = bound.invoke("查一下 ws-1001 这个工作区的集成配置状态")
    ms = (time.perf_counter() - t0) * 1000
    calls = getattr(msg, "tool_calls", None) or []
    if calls:
        print(f"  [通过] {ms:.0f}ms  调用工具={calls[0]['name']}  参数={calls[0]['args']}")
        tool_ok = calls[0]["name"] == "query_integration_config"
    else:
        print(f"  [失败] 模型未发起工具调用，直接回复: {str(msg.content)[:120]}")
except Exception as exc:  # noqa: BLE001
    print(f"  [失败] {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 4) 流式输出
print("\n[4] 流式输出（用户端流式回复前提）")
stream_ok = False
try:
    t0 = time.perf_counter()
    chunks: list[str] = []
    for chunk in llm.stream("用一句话说明客服工单的作用。"):
        if chunk.content:
            chunks.append(str(chunk.content))
    ms = (time.perf_counter() - t0) * 1000
    text = "".join(chunks)
    stream_ok = len(chunks) > 1 and bool(text.strip())
    print(f"  [{'通过' if stream_ok else '失败'}] {ms:.0f}ms  分片数={len(chunks)}  首句={text.strip()[:60]!r}")
except Exception as exc:  # noqa: BLE001
    print(f"  [失败] {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 5) 时延基线
print("\n[5] 时延基线（指标看板口径参考）")
latencies: list[float] = []
try:
    for i in range(3):
        t0 = time.perf_counter()
        llm.invoke("回复一个词：好")
        latencies.append((time.perf_counter() - t0) * 1000)
    avg = sum(latencies) / len(latencies)
    print(f"  3 次短请求时延: {[round(x) for x in latencies]} ms  平均={avg:.0f} ms")
except Exception as exc:  # noqa: BLE001
    print(f"  [失败] {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
summary = {
    "usable_models": usable,
    "selected_model": MODEL,
    "structured_output": structured_ok,
    "tool_calling": tool_ok,
    "streaming": stream_ok,
    "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
}
print("  实测结论: " + json.dumps(summary, ensure_ascii=False))
print("=" * 74)
