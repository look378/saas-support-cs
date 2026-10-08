"""工作台与实时推送验证。

覆盖任务书验收标准中"工作台/实时推送"相关条目：
  1. 坐席工作台依赖的指标接口全部可用且有数据
  2. 链路回放接口能按 trace_id 还原节点轨迹、工具调用与耗时瀑布
  3. **真实 WebSocket 推送**：坐席连上控制台后，AI 触发转人工时
     服务端主动把事件推给坐席（不是轮询）

用法：
    # 先起服务：scripts/dev-up.ps1（或分别起 mock 与 api）
    .venv\\Scripts\\python.exe scripts\\verify_workbench.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import platform_compat  # noqa: E402

platform_compat.apply()

import httpx  # noqa: E402
import websockets  # noqa: E402

API = os.environ.get("VERIFY_API_BASE", "http://127.0.0.1:8011")
WS = API.replace("http://", "ws://").replace("https://", "wss://") + "/ws/agent"
TENANT = "t-001"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


H = {"X-Tenant-ID": TENANT}


def get(path: str, token: str = "", **params):  # noqa: ANN201
    headers = dict(H)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"{API}{path}", headers=headers, params=params or None, timeout=30)
    return r.status_code, r.json()


def post(path: str, payload: dict, token: str = ""):  # noqa: ANN201
    headers = dict(H)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.post(f"{API}{path}", headers=headers, json=payload, timeout=120)
    return r.status_code, r.json()


print("=" * 74)
print("  工作台与实时推送验证")
print("=" * 74)

# ---------------------------------------------------------------- 0 依赖
print("\n[0] 服务可用性")
try:
    code, body = get("/health")
    check("API 服务可用", code == 200, f"status={code}")
except Exception as exc:  # noqa: BLE001
    print(f"  无法连接 API（{API}）：{exc}")
    print("  请先执行 scripts/dev-up.ps1 启动服务拓扑。")
    sys.exit(2)

# ---------------------------------------------------------------- 1 坐席登录
print("\n[1] 坐席登录与心跳")
code, login = post("/api/v1/agent/login", {"username": "agent_zhao", "password": "Agent@12345"})
check("坐席登录成功", code == 200 and login.get("token"), f"status={code}")
token = login.get("token", "")
code, hb = post("/api/v1/agent/heartbeat", {"presence": "online"}, token=token)
check("心跳上报成功", code == 200 and hb.get("ok"), str(hb.get("data"))[:80])

# ---------------------------------------------------------------- 2 指标接口
print("\n[2] 指标接口（工作台看板依赖）")
for path, name in [
    ("/api/v1/agent/metrics/overview", "核心指标总览"),
    ("/api/v1/agent/metrics/trend", "按天趋势"),
    ("/api/v1/agent/metrics/skills", "分技能对比"),
    ("/api/v1/agent/metrics/escalation-reasons", "转人工原因分布"),
    ("/api/v1/agent/metrics/agents", "坐席维度指标"),
    ("/api/v1/agent/metrics/llm", "模型调用计量"),
    ("/api/v1/agent/metrics/compare", "AI vs 行业基准"),
]:
    code, body = get(path, token=token)
    ok = code == 200 and body.get("ok") is True
    check(f"{name} 可读", ok, f"status={code}")

code, ov = get("/api/v1/agent/metrics/overview", token=token, days=7)
d = ov.get("data", {})
check(
    "总览含关键指标字段",
    all(k in d for k in ("total_sessions", "ai_first_contact_resolution_rate", "escalation_rate",
                         "avg_turns_per_session", "avg_handle_minutes")),
    f"会话数={d.get('total_sessions')} 一次解决率={d.get('ai_first_contact_resolution_rate')}",
)
check(
    "人工基线标注为行业参考（未虚构实测）",
    "industry_aht_minutes" in d and "未做人工实测" in str(d.get("industry_baseline_note", "")),
)

# ---------------------------------------------------------------- 3 链路回放
print("\n[3] 链路回放")
# 先**主动产生一次诊断**，确保回放目标是本次运行的真实数据。
# 若直接取"最近一条"，可能拿到历史遗留记录（来自其它场景），断言就失去意义。
fresh_key = f"wb-replay-{uuid.uuid4().hex[:8]}"
code, fresh_chat = post(
    "/api/v1/chat",
    {
        "session_key": fresh_key,
        "message": "工作区 ws-1001 的 slack 机器人不回复消息了",
        "user_ext_id": "u-42",
    },
)
check("产生一次用于回放的诊断", code == 200 and bool(fresh_chat.get("trace_id")), f"status={code}")
fresh_trace = fresh_chat.get("trace_id", "")

code, traces = get("/api/v1/agent/replay/traces", token=token, limit=5)
rows = traces.get("data", []) if traces.get("ok") else []
check("可列出最近诊断链路", code == 200 and len(rows) > 0, f"条数={len(rows)}")

target_trace = fresh_trace or (rows[0]["trace_id"] if rows else "")
if target_trace:
    code, rp = get(f"/api/v1/agent/replay/trace/{target_trace}", token=token)
    rp = rp.get("data", {}) if code == 200 else {}
    check("可按 trace_id 回放", code == 200 and rp.get("found") is True, f"trace_id={target_trace[:12]}…")
    check(
        "回放含节点轨迹与工具调用",
        rp.get("node_count", 0) > 0 and rp.get("tool_count", 0) > 0,
        f"节点={rp.get('node_count')} 工具={rp.get('tool_count')}",
    )
    check("回放含耗时瀑布", bool(rp.get("waterfall")), f"最慢={rp.get('slowest', '')}")
    check(
        "回放含最终结论",
        bool(rp.get("conclusion", {}).get("cause_code")),
        str(rp.get("conclusion", {}).get("cause_code")),
    )
    nodes = [t["name"] for t in rp.get("timeline", []) if t["kind"] == "node"]
    check("节点顺序体现诊断链", "route" in nodes and "finalize" in nodes, str(nodes[:8]))
    check(
        "回放可定位到工具出站目标",
        any(t.get("target") for t in rp.get("timeline", []) if t["kind"] == "tool"),
        str([t.get("target") for t in rp.get("timeline", []) if t["kind"] == "tool"][:2]),
    )
else:
    print("  （暂无诊断记录，跳过回放明细断言）")

# ---------------------------------------------------------------- 4 WebSocket 实时推送
print("\n[4] WebSocket 实时推送（服务端主动推给坐席）")


async def ws_flow() -> dict:
    """连上控制台 → 触发一次转人工 → 等待推送。"""
    url = f"{WS}?tenant_id={TENANT}&token={token}"
    received: list[dict] = []
    async with websockets.connect(url, open_timeout=10) as ws:
        # 握手回放 + ready
        for _ in range(6):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:  # noqa: BLE001
                break
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            received.append(msg)
            if msg.get("type") == "console.ready":
                break

        # 触发转人工（大额计费争议 → 必然 escalate）
        key = f"ws-verify-{uuid.uuid4().hex[:8]}"
        r = httpx.post(
            f"{API}/api/v1/chat",
            headers=H,
            json={
                "session_key": key,
                "message": "账号 acct-501 账单重复计费 2380 元，要求退款",
                "user_ext_id": "u-42",
            },
            timeout=120,
        )
        chat = r.json()

        # 等待服务端推送
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
            except Exception:  # noqa: BLE001
                continue
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            received.append(msg)
            if msg.get("type") in ("escalation.created", "escalation.assigned"):
                break

        return {"chat": chat, "events": received, "session_key": key}


import asyncio  # noqa: E402

try:
    result = asyncio.run(ws_flow())
    events = result["events"]
    types = [e.get("type") for e in events]
    chat = result["chat"]

    check("WebSocket 连接建立并收到 ready", "console.ready" in types, f"收到 {len(events)} 条")
    check(
        "转人工由服务端主动推送（非轮询）",
        "escalation.created" in types or "escalation.assigned" in types,
        f"事件类型={types}",
    )
    created = next((e for e in events if e.get("type") == "escalation.created"), None)
    if created:
        print(f"      推送载荷：{json.dumps({k: v for k, v in created.items() if k != 'at'}, ensure_ascii=False)[:220]}")
        check(
            "推送内容含会话、技能与原因",
            bool(created.get("session_key")) and bool(created.get("skill")) and bool(created.get("reason")),
            f"skill={created.get('skill')!r} reason={str(created.get('reason'))[:24]!r}",
        )
        check(
            "推送的是本次触发的会话",
            created.get("session_key") == result["session_key"],
            f"推送={str(created.get('session_key'))[:16]}… 期望={result['session_key'][:16]}…",
        )
    check(
        "AI 侧确已触发转人工",
        chat.get("escalate") is True,
        f"escalate={chat.get('escalate')} decision={chat.get('decision')}",
    )
except Exception as exc:  # noqa: BLE001
    check("WebSocket 推送流程", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 5 静态控制台页面
print("\n[5] 静态控制台页面")
try:
    r = httpx.get(f"{API}/static/agent_console.html", timeout=10)
    check("控制台页面可访问", r.status_code == 200, f"status={r.status_code}")
    check(
        "页面确实使用 WebSocket（而非轮询）",
        "new WebSocket(" in r.text and "/ws/agent" in r.text,
    )
except Exception as exc:  # noqa: BLE001
    check("控制台页面可访问", False, f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  工作台与实时推送验证全部通过。")
