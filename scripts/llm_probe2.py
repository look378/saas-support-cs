"""大模型调用参数实测（第二轮）。

第一轮实测暴露两个必须解决的问题，本脚本用于定位正确参数：

  P1. ``with_structured_output`` 默认走 json_schema（response_format），
      该端点返回 400 "This response_format type is unavailable now"。
      → 需要确认可用替代路径。

  P2. 部分模型返回空 content 却消耗了 tokens
      （实测 deepseek-flash 用 57 tokens 返回空串，deepseek-v4-pro 用 110 tokens 返回空串）
      → 需确认是否为默认开启的思考模式所致，以及关闭方式。

结论将直接写入 ``app/core/llm_client.py`` 与 ``docs/architecture.md``。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.core.config import settings  # noqa: E402

MODEL = settings.llm_model
URL = f"{settings.llm_base_url}/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {settings.llm_api_key}",
    "Content-Type": "application/json",
}

print("=" * 74)
print(f"  调用参数实测  model={MODEL}")
print("=" * 74)


def post(payload: dict) -> tuple[int, dict | str]:
    with httpx.Client(timeout=60.0) as c:
        r = c.post(URL, json=payload, headers=HEADERS)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, r.text[:300]


def short(msgs, **extra) -> str:
    payload = {
        "model": MODEL,
        "messages": msgs,
        "max_tokens": 64,
        "stream": False,
        **extra,
    }
    code, body = post(payload)
    if code != 200 or not isinstance(body, dict):
        return f"HTTP {code}: {str(body)[:200]}"
    ch = (body.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return json.dumps(
        {
            "content": (msg.get("content") or "")[:60],
            "reasoning_len": len(msg.get("reasoning_content") or ""),
            "finish": ch.get("finish_reason"),
            "tokens": (body.get("usage") or {}).get("total_tokens"),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- P2 思考模式
print("\n[P2] 思考模式对返回内容的影响")
user = [{"role": "user", "content": "回复两个字：收到"}]
print("  默认(不传 thinking)          :", short(user))
print("  thinking=disabled            :", short(user, thinking={"type": "disabled"}))
print("  reasoning_effort=none        :", short(user, reasoning_effort="none"))
print("  两者同时                     :", short(user, thinking={"type": "disabled"}, reasoning_effort="none"))
print("  未传thinking + max_tokens=512:", short(user, max_tokens=None) if False else short(user))

print("\n  思考模式下 max_tokens 是否被思考耗尽：")
payload_think = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "一个客服系统为什么要记录 trace_id？请说明。"}],
    "max_tokens": 64,
    "stream": False,
    "thinking": {"type": "enabled"},
}
code, body = post(payload_think)
if isinstance(body, dict):
    ch = (body.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    print(f"    max_tokens=64  thinking=enabled -> finish={ch.get('finish_reason')} "
          f"content_len={len(msg.get('content') or '')} reasoning_len={len(msg.get('reasoning_content') or '')} "
          f"tokens={(body.get('usage') or {}).get('total_tokens')}")

# ---------------------------------------------------------------- P1 结构化输出
print("\n[P1] 结构化输出的可用路径")


class Diagnosis(BaseModel):
    """诊断结论。"""

    cause: str = Field(description="根因分类，取 TOKEN_EXPIRED / WEBHOOK_INVALID / SCOPE_REVOKED")
    confidence: float = Field(description="置信度 0-1")
    evidence: list[str] = Field(default_factory=list, description="证据列表")


from langchain_openai import ChatOpenAI  # noqa: E402

BASE = dict(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    model=MODEL,
    temperature=0,
    timeout=60.0,
)
SCENARIO = (
    "用户反馈：钉钉机器人从昨天开始不再回复消息。查询发现访问令牌已于昨日到期。"
    "请给出根因分类、置信度和证据列表。"
)

# 路径 A: 原生 json_object（文档明确支持的 response_format）
print("\n  路径A: response_format=json_object（原生 HTTP）")
code, body = post(
    {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "只输出 JSON，包含 cause、confidence、evidence 三个字段。"},
            {"role": "user", "content": SCENARIO},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
)
if code == 200 and isinstance(body, dict):
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    try:
        parsed = json.loads(content)
        print(f"    [可用] 解析成功: {json.dumps(parsed, ensure_ascii=False)[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [部分] HTTP200 但 JSON 解析失败({exc}): {content[:160]}")
else:
    print(f"    [失败] HTTP {code}: {str(body)[:200]}")

# 路径 B: langchain with_structured_output(method="function_calling")
print("\n  路径B: langchain with_structured_output(method='function_calling')")
for method in ("function_calling", "json_mode"):
    try:
        llm = ChatOpenAI(**BASE)
        chain = llm.with_structured_output(Diagnosis, method=method)
        t0 = time.perf_counter()
        out = chain.invoke(SCENARIO)
        ms = (time.perf_counter() - t0) * 1000
        print(f"    [可用] method={method:<16} {ms:.0f}ms -> {out.model_dump_json()[:170]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    [失败] method={method:<16} {type(exc).__name__}: {str(exc)[:150]}")

# 路径 C: json_schema（第一轮失败的那条，确认失败原因）
print("\n  路径C: langchain with_structured_output(method='json_schema')")
try:
    llm = ChatOpenAI(**BASE)
    out = llm.with_structured_output(Diagnosis, method="json_schema").invoke(SCENARIO)
    print(f"    [可用] {out.model_dump_json()[:150]}")
except Exception as exc:  # noqa: BLE001
    print(f"    [失败] {type(exc).__name__}: {str(exc)[:180]}")

# 路径 D: 手工提示词 + JSON 解析（最终兜底）
print("\n  路径D: 提示词约束 + 自行解析（兜底方案）")
try:
    llm = ChatOpenAI(**BASE)
    resp = llm.invoke(
        SCENARIO + "\n\n严格只输出一个 JSON 对象，键为 cause、confidence、evidence，不要任何其他文字。"
    )
    text = str(resp.content).strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    parsed = json.loads(text)
    obj = Diagnosis(**parsed)
    print(f"    [可用] {obj.model_dump_json()[:170]}")
except Exception as exc:  # noqa: BLE001
    print(f"    [失败] {type(exc).__name__}: {str(exc)[:170]}")

# ---------------------------------------------------------------- 稳定复现
print("\n[稳定性] deepseek-chat 连续 5 次是否稳定返回非空内容")
ok = 0
for i in range(5):
    code, body = post(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "回复两个字：收到"}],
            "max_tokens": 32,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
    )
    c = ""
    if code == 200 and isinstance(body, dict):
        c = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if c.strip():
        ok += 1
print(f"  非空回复 {ok}/5")

print("\n" + "=" * 74)
