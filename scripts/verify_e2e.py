"""端到端验证：诊断 → 转人工 → 队列 → 派单 → 接管 → 对话 → 办结 → 回流。

两种运行模式：
  - **默认（进程内）**：Mock 后端用进程内 ASGI 直连。自包含、可重复，无需起服务。
  - **HTTP 模式**：设置 ``E2E_USE_HTTP=1`` 后走真实 HTTP，要求 Mock 服务已在
    127.0.0.1:8010 运行。用于证明"真调用"这条链路确实可用。

覆盖任务书验收标准中的：
  - 技能 2 完整演示（诊断 → 远程修复 → 验证 → 闭环）
  - 技能 5 完整演示（异常登录 → 风险评分 → 锁定 + 通知）
  - 转人工全流程（AI 触发 → 队列 → 派单 → 坐席收到 → 接管 → 对话 → 办结，快照完整）
  - 座席掉线 → 心跳过期 → 会话自动重排队
  - 已被坐席接管后 AI 不再介入

用法：
    .venv\\Scripts\\python.exe scripts\\verify_e2e.py
    $env:E2E_USE_HTTP=1; .venv\\Scripts\\python.exe scripts\\verify_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import platform_compat

platform_compat.apply()

USE_HTTP = os.environ.get("E2E_USE_HTTP", "") not in ("", "0", "false")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 进程内 ASGI 直连（HTTP 模式跳过）
# ---------------------------------------------------------------------------
from fastapi.testclient import (
    TestClient,
)

if not USE_HTTP:
    from app.mocks.server import app as mock_app

    _mock_client = TestClient(mock_app)

    class _InProcClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, params=None, json=None, headers=None, content=None):
            path = url.split("8010", 1)[-1]
            return _mock_client.request(
                method, path, params=params, json=json, content=content, headers=headers or {}
            )

        def get(self, url):
            return _mock_client.get(url.split("8010", 1)[-1])

    def _patch_httpx() -> None:
        import httpx

        httpx.Client = _InProcClient  # type: ignore[misc]

    _patch_httpx()

from app.main import app as api_app

api = TestClient(api_app, raise_server_exceptions=False)
TENANT = "t-001"
# 每次运行的会话键都不同：固定键会命中上一轮遗留的 Redis/DB 状态
# （例如已 CLOSED 的会话无法再接管），使验收结果依赖执行顺序。
import time as _time

RUN = _time.strftime("%H%M%S")


def SK(name: str) -> str:
    """生成本次运行专属的会话键。"""
    return f"e2e-{name}-{RUN}"


# 转人工/接管流程整段复用同一个会话键，这里取一次并全程引用，
# 避免"取键函数每次调用都产生新键"导致后面查不到前面建的会话。
BIL_KEY = SK("bil")
HEADERS = {"X-Tenant-ID": TENANT}

print("=" * 74)
print(f"  端到端验证：诊断 → 转人工 → 接管 → 办结   [{'HTTP 真实链路' if USE_HTTP else '进程内直连'}]")
print("=" * 74)

# ---------------------------------------------------------------- 0 准备
print("\n[0] 准备：装载种子数据")
from app.core.trace_context import bind
from app.services.seed_service import SeedService

with bind(tenant_id=TENANT, trace_id="e2e-seed"):
    # 重置 Mock 运行期状态，保证每次运行结论一致（修复动作会改变后端状态）
    from app.mocks import data as mock_data

    mock_data.reset_runtime_state()
    seed = SeedService().run()
print(f"      新增：租户 {seed['tenants']} / 坐席 {seed['agents']} / 用户 {seed['users']} / 订阅 {seed['subscriptions']} / 案例 {seed['cases']}")
idx = seed.get("index", {})
check("知识库已建索引（44 条）", idx.get("indexed", 0) >= 44, f"indexed={idx.get('indexed')} cases={idx.get('cases')} faq={idx.get('faq')}")

# ---------------------------------------------------------------- 1 技能2
print("\n[1] 技能 2：集成故障诊断与修复（核心演示）")
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={
        "session_key": SK("int"),
        "message": "工作区 ws-1001 的 slack 机器人从昨天开始就不回复消息了",
        "user_ext_id": "u-42",
    },
)
check("对话接口返回 200", r.status_code == 200, f"status={r.status_code}")
body = r.json() if r.status_code == 200 else {}
if r.status_code != 200:
    print("      响应:", str(r.text)[:400])
check("路由到集成技能", body.get("skill") == "integration", f"skill={body.get('skill')}")
check(
    "诊断结论为令牌过期",
    body.get("cause_code") == "TOKEN_EXPIRED",
    f"cause={body.get('cause_code')} 置信度={body.get('confidence')}",
)
check("执行了远程修复", any(a.get("action") == "refresh_token" for a in body.get("actions", [])),
      "；".join(a.get("detail", "") for a in body.get("actions", [])))
check("验证可达通过（真投递回执）", body.get("verify_ok") is True, str(body.get("verify_detail"))[:90])
check("闭环未转人工", body.get("escalate") is False, f"outcome={body.get('outcome')}")
check("链路轨迹完整", body.get("node_path") == ["route", "observe", "diagnose", "act", "verify", "finalize"],
      str(body.get("node_path")))
print(f"      回复：{(body.get('reply') or '')[:110]}...")

# ---------------------------------------------------------------- 2 技能5
print("\n[2] 技能 5：账号安全异常排查")
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={"session_key": SK("acc"), "message": "账号 u-42 凌晨在境外登录了，怀疑被盗", "user_ext_id": "u-42"},
)
body5 = r.json() if r.status_code == 200 else {}
check("路由到账号安全技能", body5.get("skill") == "account", f"skill={body5.get('skill')}")
check("识别为疑似盗用", body5.get("cause_code") == "ACCOUNT_COMPROMISE_SUSPECTED", f"cause={body5.get('cause_code')}")
check("风险分达到高风险档", (body5.get("risk_score") or 0) >= 60, f"风险分={body5.get('risk_score')}")
actions5 = {a.get("action") for a in body5.get("actions", [])}
check("执行了锁定+通知", {"lock_account", "notify"} <= actions5, f"动作={sorted(actions5)}")
check("安全复核通过", body5.get("verify_ok") is True, str(body5.get("verify_detail"))[:80])
print(f"      回复：{(body5.get('reply') or '')[:110]}...")

# ---------------------------------------------------------------- 3 技能1
print("\n[3] 技能 1：FAQ 咨询")
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={"session_key": SK("faq"), "message": "怎么设置消息免打扰？", "user_ext_id": "u-43"},
)
body1 = r.json() if r.status_code == 200 else {}
check("路由到 FAQ 技能", body1.get("skill") == "faq", f"skill={body1.get('skill')}")
check("生成了回答", bool((body1.get("reply") or "").strip()), (body1.get("reply") or "")[:70])

# ---------------------------------------------------------------- 4 技能3
print("\n[4] 技能 3：权限访问诊断（多方案）")
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={"session_key": SK("per"), "message": "文档 doc-771 打不开，提示没有访问权限", "user_ext_id": "u-42"},
)
body3 = r.json() if r.status_code == 200 else {}
check("路由到权限技能", body3.get("skill") == "permission", f"skill={body3.get('skill')}")
check("诊断分享未开启", body3.get("cause_code") == "SHARE_NOT_ENABLED", f"cause={body3.get('cause_code')}")
check("给出多个可选方案", len(body3.get("options") or []) >= 2, f"方案数={len(body3.get('options') or [])}")
check("执行推荐方案后复核通过", body3.get("verify_ok") is True, str(body3.get("verify_detail"))[:70])

# ---------------------------------------------------------------- 5 技能4
print("\n[5] 技能 4：计费争议（风控 + 幂等 + 转人工审批）")
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={"session_key": BIL_KEY, "message": "这个月账单重复计费了，账号 acct-501，我要退款", "user_ext_id": "u-42"},
)
body4 = r.json() if r.status_code == 200 else {}
# 会话令牌：本会话后续还要读状态/续聊，用户端接口要求凭令牌访问
BIL_TOKEN = body4.get("user_token", "")
check("首轮对话签发了会话令牌", bool(BIL_TOKEN), f"token 长度={len(BIL_TOKEN)}")
check("路由到计费技能", body4.get("skill") == "billing", f"skill={body4.get('skill')}")
check("识别为计费争议", body4.get("cause_code") == "BILLING_DISPUTE_CONFIRMED", f"cause={body4.get('cause_code')}")
check("风险分已计算", (body4.get("risk_score") or 0) > 0, f"风险分={body4.get('risk_score')}")
check("大额争议转人工审批", body4.get("decision") == "manual_approval", f"decision={body4.get('decision')}")
check("因需审批而转人工", body4.get("escalate") is True)

# ---------------------------------------------------------------- 6 转人工流转
print("\n[6] 转人工真实流转：队列 → 派单 → 接管 → 对话 → 办结")
esc_created = body4.get("escalation") or {}
check("已创建转人工记录", bool(esc_created.get("request_no")), f"单号={esc_created.get('request_no')}")
check("已创建工单", bool(esc_created.get("ticket_no")), f"工单号={esc_created.get('ticket_no')}")
check("交接快照非空", bool((esc_created.get("snapshot") or "").strip()),
      f"快照 {len(esc_created.get('snapshot') or '')} 字符")

# 坐席登录
r = api.post("/api/v1/agent/login", headers=HEADERS, json={"username": "agent_zhao", "password": "Agent@12345"})
check("坐席登录成功", r.status_code == 200, f"status={r.status_code}")
agent_token = r.json().get("token", "") if r.status_code == 200 else ""
agent_headers = {**HEADERS, "Authorization": f"Bearer {agent_token}"}

# 上报心跳
r = api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "online"})
check("坐席心跳上报成功", r.status_code == 200 and r.json()["data"]["presence"] == "online")

# 查看队列
r = api.get("/api/v1/agent/queue", headers=agent_headers)
queue_items = r.json().get("data", []) if r.status_code == 200 else []
check("队列中能看到待接入会话", any(i.get("session_key") == BIL_KEY for i in queue_items),
      f"队列长度={len(queue_items)}")

# 派单（模拟后台派单任务：无请求上下文，由服务自身确保租户绑定）
from app.core.trace_context import ensure_tenant
from app.services.escalation_service import get_escalation_service

with ensure_tenant(TENANT):
    dispatch = get_escalation_service().dispatch_one(tenant_id=TENANT)
check("派单成功且分配给在线坐席", bool(dispatch and dispatch.get("assigned")), json.dumps(dispatch, ensure_ascii=False)[:150])

# 接管
r = api.post("/api/v1/agent/claim", headers=agent_headers, json={"session_key": BIL_KEY})
check("坐席接管成功（拿到分布式锁）", r.status_code == 200, f"status={r.status_code} {str(r.text)[:120]}")
claimed = r.json().get("data", {}) if r.status_code == 200 else {}
check("会话状态切为坐席处理中", claimed.get("state") == "AGENT_HANDLING", f"state={claimed.get('state')}")

# 第二个坐席抢同一会话应失败
r2 = api.post("/api/v1/agent/login", headers=HEADERS, json={"username": "agent_lin", "password": "Agent@12345"})
token2 = r2.json().get("token", "")
r = api.post(
    "/api/v1/agent/claim",
    headers={**HEADERS, "Authorization": f"Bearer {token2}"},
    json={"session_key": BIL_KEY},
)
check("第二个坐席抢同一会话被拒绝（锁生效）", r.status_code == 409, f"status={r.status_code}")

# AI 停止介入：用户再发消息应转给坐席
r = api.post(
    "/api/v1/chat",
    headers=HEADERS,
    json={"session_key": BIL_KEY, "message": "请问处理得怎么样了？", "user_ext_id": "u-42", "user_token": BIL_TOKEN},
)
body_after = r.json() if r.status_code == 200 else {}
check("坐席接管后 AI 不再介入", body_after.get("handler") == "agent_zhao" and body_after.get("skill") == "",
      f"handler={body_after.get('handler')} skill={body_after.get('skill') or '（无）'}")

# 坐席对话
r = api.post(
    "/api/v1/agent/message",
    headers=agent_headers,
    json={"session_key": BIL_KEY, "content": "您好，我已核对账单，重复计费部分将为您退款。"},
)
check("坐席发送消息成功", r.status_code == 200, f"status={r.status_code}")

# 办结 + 回流
r = api.post(
    "/api/v1/agent/close",
    headers=agent_headers,
    json={
        "session_key": BIL_KEY,
        "resolution": "确认席位费重复计费，已提交退款 380 元",
        "skill": "billing",
        "diagnosis_code": "BILLING_DISPUTE_CONFIRMED",
        "fix_action": "refund",
        "automatable": True,
        "promote_to_kb": True,
    },
)
closed = r.json().get("data", {}) if r.status_code == 200 else {}
check("办结成功", r.status_code == 200 and closed.get("closed") is True, str(closed)[:150])
check("办结时回流知识库（闭环）", closed.get("promoted") is True, f"feedback_id={closed.get('feedback_id')}")

# 办结后会话状态
r = api.get(
    f"/api/v1/session/{BIL_KEY}",
    headers=HEADERS,
    params={"user_token": BIL_TOKEN, "user_ext_id": "u-42"},
)
sess = r.json().get("data", {}) if r.status_code == 200 else {}
check("会话状态为已关闭", sess.get("redis", {}).get("state") == "CLOSED", f"state={sess.get('redis', {}).get('state')}")

# ---------------------------------------------------------------- 7 掉线重排队
print("\n[7] 坐席掉线 → 心跳过期 → 会话自动重排队")
from app.services.session_service import (
    get_presence_service,
    get_session_service,
)

presence = get_presence_service()
sessions = get_session_service()

# 本段直接操作会话状态机与数据库，必须绑定租户上下文：
# 状态跃迁会同步落库（Redis 是实时权威、数据库是审计来源），
# 而租户闸门会拒绝任何未绑定租户的租户表访问（本项目实测踩到）。
with bind(tenant_id=TENANT, trace_id="e2e-offline"):

    # 造一个"坐席处理中"的会话
    sessions.get_or_create(tenant_id=TENANT, session_key="e2e-drop-001", user_ext_id="u-43")
    sessions.transition(
        tenant_id=TENANT, session_key="e2e-drop-001", to_state="QUEUED", handler="QUEUE", reason="测试"
    )
    sessions.transition(
        tenant_id=TENANT, session_key="e2e-drop-001", to_state="AGENT_HANDLING", handler="agent_zhao", reason="接管"
    )

    # 模拟掉线：清除心跳
    presence.set_offline(tenant_id=TENANT, agent="agent_zhao")
    ttl_map = presence.all_with_ttl(tenant_id=TENANT, agents=["agent_zhao"])
    check("掉线后心跳已失效", ttl_map.get("agent_zhao", 0) < 0, f"ttl={ttl_map.get('agent_zhao')}")

    requeued = get_escalation_service().requeue_agent_sessions(tenant_id=TENANT, agent="agent_zhao")
    check("掉线坐席名下会话被重新排队", "e2e-drop-001" in requeued, f"重排队={requeued}")
    info = sessions.get_or_create(tenant_id=TENANT, session_key="e2e-drop-001")
    check("重排队后状态为 QUEUED", info.state == "QUEUED", f"state={info.state} handler={info.handler}")

# ---------------------------------------------------------------- 8 降级
print("\n[8] 降级路径：模型不可用 → 友好提示 + 转人工")
import app.core.llm_client as llm_mod


class _BoomLLM:
    def invoke(self, *a, **k):
        raise llm_mod.LLMUnavailable("模拟模型故障", kind="timeout")

    def structured(self, *a, **k):
        raise llm_mod.LLMUnavailable("模拟模型故障", kind="timeout")


_orig = llm_mod.get_llm_client
llm_mod.get_llm_client = lambda refresh=False: _BoomLLM()  # type: ignore[assignment]
try:
    r = api.post(
        "/api/v1/chat",
        headers=HEADERS,
        json={"session_key": SK("degrade"), "message": "产品功能怎么设置？", "user_ext_id": "u-44"},
    )
    bd = r.json() if r.status_code == 200 else {}
    check("模型故障时接口仍可用（不 500）", r.status_code == 200, f"status={r.status_code}")
    check("降级为兜底回答并转人工", bd.get("degraded") is True or bd.get("escalate") is True,
          f"degraded={bd.get('degraded')} escalate={bd.get('escalate')} reason={bd.get('degrade_reason')}")
    print(f"      降级回复：{(bd.get('reply') or '')[:90]}...")
finally:
    llm_mod.get_llm_client = _orig  # type: ignore[assignment]

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  端到端验证全部通过。")
