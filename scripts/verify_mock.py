"""Mock 服务契约验证。

在**进程内**启动 Mock 应用（无需外部端口），逐项验证：
  1. 四类域的正常返回符合 docs/mock-boundary.md 契约
  2. X-Mock-Mode 五种模式的行为一致
  3. 计费域签名 / 时间戳窗口 / 防重放 / 幂等
  4. 确定性：同一 ID 多次请求得到同一故障画像

用法：
    .venv\\Scripts\\python.exe scripts\\verify_mock.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.mocks import data as mock_data  # noqa: E402
from app.mocks.server import app  # noqa: E402

# Mock 后端是**有状态**的（修复动作会真实改变画像，否则"验证可达"永远失败）。
# 若不清零，先前在同一进程里跑过的验证脚本（例如端到端验收修好了 ws-1001）
# 会让本次断言读到"已修复"的画像——故障案例断言随执行顺序漂移。
# 本脚本用进程内 ASGI 直连，因此重置的是本进程状态，不影响正在运行的 Mock 服务。
mock_data.reset_runtime_state()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


client = TestClient(app)

print("=" * 74)
print("  Mock 服务契约验证")
print("=" * 74)

# ---------------------------------------------------------------- 1 正常契约
print("\n[1] 四类域正常返回（契约符合性）")

r = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1001", "provider": "slack"},
)
d = r.json()["data"]
check(
    # 画像口径：ws-1001 只带**单一**故障（令牌过期），回调地址是正常的。
    # 这里不能再断言 webhook_reachable is False——那属于旧的"复合故障"口径，
    # 已被刻意拆分为单一故障画像，以便"诊断 → 修复 → 验证"逐项形成闭环。
    "集成域 config 返回令牌过期画像",
    r.status_code == 200 and d["token"]["expired"] is True and d["health"] == "degraded",
    f"health={d['health']} expired={d['token']['expired']} "
    f"webhook_reachable={d['webhook_reachable']} scopes={d['token']['scopes']}",
)

r = client.get(
    "/mock/permission/document",
    params={"tenant_id": "t-001", "doc_id": "doc-771", "user_id": "u-42"},
)
d = r.json()["data"]
check(
    "权限域 document 返回分享未开启画像",
    r.status_code == 200 and d["deny_reason"] == "SHARE_NOT_ENABLED" and d["user_can_open"] is False,
    f"share.mode={d['share']['mode']}",
)

r = client.get("/mock/billing/subscription", params={"tenant_id": "t-001", "account_id": "acct-501"})
d = r.json()["data"]
check(
    "计费域 subscription 返回高金额争议画像",
    r.status_code == 200 and d["tenure_months"] == 9 and d["user_tier"] == "gold",
    f"plan={d['plan']} seats={d['seats']}",
)

r = client.get("/mock/billing/invoice", params={"tenant_id": "t-001", "account_id": "acct-501"})
d = r.json()["data"]
check(
    "计费域 invoice 金额超阈值且标记争议",
    r.status_code == 200 and d["amount"] == 2380.0 and d["disputed"] is True,
    f"dispute={d['dispute_reason']}",
)

r = client.get("/mock/account/login-events", params={"tenant_id": "t-001", "user_id": "u-42"})
d = r.json()["data"]
check(
    "账号域 login-events 返回高风险信号",
    r.status_code == 200
    and set(d["events"][0]["risk_flags"]) == {"new_geo", "odd_hour", "new_device"},
    f"score_hint={d['anomaly_score_hint']}",
)

# ---------------------------------------------------------------- 2 确定性
print("\n[2] 确定性：同 ID 多次请求画像一致")
a = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1002", "provider": "slack"},
).json()["data"]
b = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1002", "provider": "slack"},
).json()["data"]
check("重复请求返回同一画像（可复现演示与断言）", a["webhook_url"] == b["webhook_url"] and a["token"]["expired"] == b["token"]["expired"])

# ---------------------------------------------------------------- 3 异常开关
print("\n[3] X-Mock-Mode 五种模式")

r = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1001"},
    headers={"X-Mock-Mode": "error"},
)
check("error 模式返回 5xx + retryable", r.status_code == 500 and r.json()["error"]["retryable"] is True, f"code={r.json()['error']['code']}")

r = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1001"},
    headers={"X-Mock-Mode": "unauthorized"},
)
check("unauthorized 模式返回 401 + 不可重试", r.status_code == 401 and r.json()["error"]["retryable"] is False)

r = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1001"},
    headers={"X-Mock-Mode": "bad_data"},
)
body = r.json()
check(
    "bad_data 模式返回 200 但结构非法（可触发校验兜底）",
    r.status_code == 200 and "workspace_id" not in body.get("data", {}),
    f"data={body.get('data')}",
)

# X-Mock-Fail-N：前 2 次失败，第 3 次成功（验证重试后成功路径）
hdr = {"X-Mock-Fail-N": "2"}
codes = [
    client.get("/mock/integration/config", params={"tenant_id": "t-001", "workspace_id": "ws-1004"}, headers=hdr).status_code
    for _ in range(3)
]
check("X-Mock-Fail-N 实现瞬时故障（前2次500，第3次200）", codes == [500, 500, 200], f"codes={codes}")

# timeout：用一个很短的客户端超时验证会挂起
r = client.get(
    "/mock/integration/config",
    params={"tenant_id": "t-001", "workspace_id": "ws-1001"},
    headers={"X-Mock-Mode": "timeout", "X-Mock-Delay-Ms": "0"},
)
check("timeout 模式最终返回 504", r.status_code == 504, f"code={r.json()['error']['code']}")

# ---------------------------------------------------------------- 4 计费安全
print("\n[4] 计费写操作安全（签名 / 时间戳 / 防重放 / 幂等）")


def sign(body: bytes, ts: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


payload = {"tenant_id": "t-001", "account_id": "acct-501", "amount": 380.0, "kind": "refund"}
raw = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = sign(raw, ts, settings.billing_hmac_secret)

r = client.post(
    "/mock/billing/adjustment",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": sig,
        "X-Nonce": "nonce-001",
        "Idempotency-Key": "idem-001",
        "X-Mock-Idempotency": "on",
    },
)
d = r.json().get("data", {})
check("合法签名请求成功", r.status_code == 200 and d.get("status") == "succeeded", f"adjustment_id={d.get('adjustment_id')}")

# 同 nonce 重放 → 拒绝
r2 = client.post(
    "/mock/billing/adjustment",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": sig,
        "X-Nonce": "nonce-001",
    },
)
check("同 nonce 重放被拒（防重放）", r2.status_code == 409 and r2.json()["error"]["code"] == "NONCE_REPLAYED")

# 错误签名 → 拒绝
r3 = client.post(
    "/mock/billing/adjustment",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": "deadbeef",
        "X-Nonce": "nonce-002",
    },
)
check("签名错误被拒", r3.status_code == 401 and r3.json()["error"]["code"] == "SIGNATURE_INVALID")

# 时间戳超窗 → 拒绝
old_ts = str(int(time.time()) - settings.billing_timestamp_window_seconds - 60)
old_sig = sign(raw, old_ts, settings.billing_hmac_secret)
r4 = client.post(
    "/mock/billing/adjustment",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": old_ts,
        "X-Signature": old_sig,
        "X-Nonce": "nonce-003",
    },
)
check(
    "时间戳超窗被拒",
    r4.status_code == 401 and r4.json()["error"]["code"] == "TIMESTAMP_OUT_OF_WINDOW",
    f"window=±{settings.billing_timestamp_window_seconds}s",
)

# 缺签名要素 → 拒绝
r5 = client.post("/mock/billing/adjustment", content=raw, headers={"Content-Type": "application/json"})
check("缺签名要素被拒", r5.status_code == 401 and r5.json()["error"]["code"] == "SIGNATURE_MISSING")

# 幂等：同 Idempotency-Key 第二次应标记 replayed
ts2 = str(int(time.time()))
sig2 = sign(raw, ts2, settings.billing_hmac_secret)
r6 = client.post(
    "/mock/billing/adjustment",
    content=raw,
    headers={
        "Content-Type": "application/json",
        "X-Timestamp": ts2,
        "X-Signature": sig2,
        "X-Nonce": "nonce-004",
        "Idempotency-Key": "idem-001",
        "X-Mock-Idempotency": "on",
    },
)
check("同幂等键第二次返回首次结果并标记 replayed", r6.json().get("data", {}).get("replayed") is True)

# ---------------------------------------------------------------- 5 故障画像区分度
print("\n[5] 故障画像区分度（技能 2 三分类可诊断性）")
faults = {}
for ws in ("ws-1001", "ws-1002", "ws-1003", "ws-1004"):
    d = client.get(
        "/mock/integration/config", params={"tenant_id": "t-001", "workspace_id": ws, "provider": "slack"}
    ).json()["data"]
    if not d["token"]["expired"] and not d["webhook_reachable"]:
        faults[ws] = "WEBHOOK_INVALID"
    elif d["token"]["expired"]:
        faults[ws] = "TOKEN_EXPIRED"
    elif "chat:write" not in d["token"]["scopes"]:
        faults[ws] = "SCOPE_REVOKED"
    else:
        faults[ws] = "HEALTHY"
check(
    "四类集成故障画像可被区分",
    faults
    == {
        "ws-1001": "TOKEN_EXPIRED",
        "ws-1002": "WEBHOOK_INVALID",
        "ws-1003": "SCOPE_REVOKED",
        "ws-1004": "HEALTHY",
    },
    json.dumps(faults, ensure_ascii=False),
)

print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  Mock 服务契约验证全部通过。")
