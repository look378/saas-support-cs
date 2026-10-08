"""工具层验证：真实调用 Mock 后端，覆盖正常路径与全部降级路径。

为了不依赖外部端口，这里把工具的 HTTP 出站替换为进程内 ASGI 直连
（Mock 应用真实运行，请求真实经过中间件与路由，只是不经过 TCP）。
这样验证是自包含、可重复的。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.trace_context import bind  # noqa: E402
from app.mocks.server import app as mock_app  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 进程内 ASGI 直连：替换 httpx 出站
# ---------------------------------------------------------------------------
# Mock 后端有状态，先清零：否则同一进程里先前跑过的脚本（如端到端修好 ws-1001）
# 会让"应查到令牌过期"这类断言读到已修复的画像，结果随执行顺序漂移。
from app.mocks import data as mock_data  # noqa: E402

mock_data.reset_runtime_state()

mock_client = TestClient(mock_app)


class _InProcessResponse:
    def __init__(self, resp):  # noqa: ANN001
        self.status_code = resp.status_code
        self.text = resp.text

    def json(self):  # noqa: ANN201
        return mock_client_json(self)


def mock_client_json(resp):  # noqa: ANN001, ANN201
    import json

    return json.loads(resp.text)


def _patch_tool_client() -> None:
    """让 ToolClient 的 HTTP 调用走进程内 ASGI。"""
    import httpx

    from app.tools.client import ToolClient

    class _InProcessClient:
        def __init__(self, timeout=None):  # noqa: ANN001
            self.timeout = timeout

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN204
            return False

        def request(self, method, url, params=None, json=None, headers=None, content=None):  # noqa: A002, ANN001
            path = url.split("8010", 1)[-1]
            return mock_client.request(
                method, path, params=params, json=json, content=content, headers=headers or {}
            )

        def get(self, url):  # noqa: ANN001
            path = url.split("8010", 1)[-1]
            return mock_client.get(path)

    ToolClient._inproc = True  # type: ignore[attr-defined]
    httpx.Client = _InProcessClient  # type: ignore[misc]


_patch_tool_client()

# 必须在 patch 之后再导入业务工具
from app.tools import business as T  # noqa: E402
from app.tools.client import ToolClient, ToolError, get_tool_client  # noqa: E402


def _with_headers(base: ToolClient, headers: dict[str, str]) -> ToolClient:
    """派生一个带固定请求头的客户端（用于注入异常模式）。"""
    return ToolClient(base_url=base.base_url, default_headers=headers)


tc = get_tool_client(refresh=True)

print("=" * 74)
print("  工具层验证（真实调用 Mock 后端 + 全部降级路径）")
print("=" * 74)

# ---------------------------------------------------------------- 1 正常路径
print("\n[1] 四类域正常调用")
with bind(tenant_id="t-001", trace_id="tool-verify-1", session_id="s-tools"):
    cfg = T.query_integration_config("ws-1001", "slack")
    check(
        # ws-1001 只带单一故障（令牌过期），回调地址正常——旧的"复合故障"口径已拆分，
        # 断言不能再要求 webhook_reachable 为 False。
        "集成域：查到令牌过期",
        cfg.token.expired is True and cfg.health == "degraded",
        f"health={cfg.health} expired={cfg.token.expired} webhook_reachable={cfg.webhook_reachable}",
    )

    doc = T.query_document_permission("doc-771", "u-42")
    check("权限域：查到分享未开启", doc.deny_reason == "SHARE_NOT_ENABLED", f"title={doc.title}")

    sub = T.query_subscription("acct-501")
    inv = T.query_invoice("acct-501")
    usage = T.query_usage("acct-501")
    check(
        "计费域：订阅+账单+用量齐全",
        sub.tenure_months == 9 and inv.amount == 2380.0 and usage.overage == 38000,
        f"金额={inv.amount} 超限={usage.overage}",
    )

    ev = T.query_login_events("u-42")
    check(
        "账号域：查到异常登录信号",
        set(ev.events[0].risk_flags) == {"new_geo", "odd_hour", "new_device"},
        f"score_hint={ev.anomaly_score_hint}",
    )

# ---------------------------------------------------------------- 2 修复动作
print("\n[2] 技能 2 的远程修复与验证动作")
with bind(tenant_id="t-001", trace_id="tool-verify-2", session_id="s-tools"):
    webhook = T.repair_webhook("ws-1002")
    check("修复 Webhook 成功且可达", webhook.repaired and webhook.webhook_reachable, webhook.webhook_url)

    delivery = T.verify_integration("ws-1004")
    check(
        "验证可达：真实投递并拿到回执",
        delivery.delivered and bool(delivery.provider_message_id),
        f"message_id={delivery.provider_message_id} latency={delivery.latency_ms}ms",
    )

    # 未修复的工作区验证应失败 → 这正是"先修后验"要断言的
    try:
        T.verify_integration("ws-1001")
        check("未修复工作区验证应失败", False, "竟然成功了")
    except ToolError as exc:
        check("未修复工作区验证失败（可据此判定修复未生效）", exc.code == "DELIVERY_FAILED", f"code={exc.code}")

    # 权限修复
    grant = T.grant_document_share("doc-771", "u-42", "viewer")
    acc = T.verify_document_access("doc-771", "u-42")
    check("权限修复后复核可访问", grant.get("granted") is True and acc.user_can_open is True, f"via={acc.via}")

    # 账号处置
    lock = T.lock_account("u-42", "检测到异常登录", "temporary", 120)
    ntf = T.notify_user("u-42", "email", "security_alert", {"reason": "异常登录"})
    va = T.verify_account_security("u-42")
    check(
        "账号锁定+通知+复核通过",
        lock.locked and ntf.sent and va.safe,
        f"吊销会话={lock.sessions_revoked} 处置后风险分={va.risk_score}",
    )

# ---------------------------------------------------------------- 3 计费写操作
print("\n[3] 计费写操作（签名/时间戳/防重放由工具层自动处理）")
with bind(tenant_id="t-001", trace_id="tool-verify-3", session_id="s-tools"):
    adj = T.create_billing_adjustment(
        "acct-502", 42.0, "refund", "用量计数异常", "inv-2025-11-acct-502", idempotency_key="idem-tool-1"
    )
    check("账务调整成功（签名自动生成并通过校验）", adj.status == "succeeded", f"id={adj.adjustment_id}")

    apv = T.request_billing_approval("acct-501", 380.0, "金额超阈值", 0.72)
    check("转人工审批请求成功", apv.status == "pending", f"approver={apv.approver_role}")

# ---------------------------------------------------------------- 4 降级路径
print("\n[4] 降级路径（X-Mock-Mode 各模式）")


def with_mode(mode: str):
    return {"X-Mock-Mode": mode}


with bind(tenant_id="t-001", trace_id="tool-verify-4", session_id="s-tools"):
    # 4.1 upstream 5xx → 重试后仍失败
    tc_local = get_tool_client()
    before = len([c for c in tc_local.calls if c.tool_name == "verify_integration"])
    try:
        T.verify_integration("ws-1004", client=_with_headers(tc_local, with_mode("error")))
        check("error 模式应抛错", False)
    except ToolError as exc:
        check(
            "error 模式 → upstream 错误（可重试）",
            exc.kind == "upstream" and exc.status == 500,
            f"kind={exc.kind} code={exc.code}",
        )

    # 4.2 unauthorized 401 → 不可重试
    try:
        T.query_subscription("acct-501", client=_with_headers(tc_local, with_mode("unauthorized")))
        check("unauthorized 模式应抛错", False)
    except ToolError as exc:
        check(
            "unauthorized 模式 → 鉴权错误（不重试）",
            exc.kind == "unauthorized" and exc.retryable is False,
            f"code={exc.code}",
        )

    # 4.3 bad_data → Pydantic 校验拦下
    try:
        T.query_subscription("acct-501", client=_with_headers(tc_local, with_mode("bad_data")))
        check("bad_data 模式应被校验拦下", False)
    except ToolError as exc:
        check(
            "bad_data 模式 → 结构校验失败（触发兜底话术）",
            exc.kind == "bad_data" and exc.code == "TOOL_SCHEMA_INVALID",
            f"kind={exc.kind}",
        )

    # 4.4 timeout → 超时降级
    try:
        T.query_subscription(
            "acct-501",
            client=_with_headers(tc_local, with_mode("timeout")),
        )
        check("timeout 模式应抛超时", False)
    except ToolError as exc:
        check("timeout 模式 → 超时错误", exc.kind in ("timeout", "upstream"), f"kind={exc.kind} code={exc.code}")

    # 4.5 瞬时故障重试后成功
    ok_after_retry = False
    try:
        T.query_subscription("acct-504", client=_with_headers(tc_local, {"X-Mock-Fail-N": "2"}))
    except ToolError:
        pass
    else:
        ok_after_retry = True
    check("瞬时故障经重试后成功（重试策略生效）", ok_after_retry)

# ---------------------------------------------------------------- 5 记录与统计
print("\n[5] 调用记录与统计")
stats = tc.stats()
check("工具调用均被记录", stats["calls"] > 10, f"统计={stats}")
check("失败调用也被记录（便于排查）", stats["failed"] > 0, f"失败数={stats['failed']}")

print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  工具层验证全部通过。")
