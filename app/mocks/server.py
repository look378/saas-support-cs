"""统一 Mock 业务后端（单进程多路由）。

设计要点（对应 docs/mock-boundary.md）：

1. **一个进程四类域**：``/mock/integration/*`` ``/mock/permission/*``
   ``/mock/billing/*`` ``/mock/account/*``。不为每个域单独起服务。

2. **异常模拟统一开关**：请求头 ``X-Mock-Mode`` 由**一个中间件**统一处理，
   不在每个端点里写 if。取值：normal / timeout / error / bad_data / unauthorized。
   这样"四类域 × 五种异常"是 4 个端点集 × 1 处实现，而不是 20 处分支。

3. **调用是真的**：真发 HTTP、真超时、真报错、真解析。生产对接时只需把
   基址换成真实服务，业务代码零改动。

4. **确定性**：同 ID 永远同一故障画像（见 data.py），保证案例断言与演示可复现。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging_config import get_logger
from app.mocks import data as D

log = get_logger(__name__)

app = FastAPI(
    title="统一 Mock 业务后端",
    description="集成 / 权限 / 计费 / 账号安全 四类外部系统的本机替身",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# 运行期状态
# ---------------------------------------------------------------------------
# 幂等登记：idempotency_key -> 首次响应
_IDEM: dict[str, dict[str, Any]] = {}
# 防重放登记：nonce -> 时间戳
_NONCES: dict[str, float] = {}
# 失败计数器：用于 X-Mock-Fail-N（前 N 次失败）
_FAIL_COUNTER: dict[str, int] = {}


def _prune() -> None:
    """清理过期登记，避免长跑内存增长。"""
    now = time.time()
    for k, ts in list(_NONCES.items()):
        if now - ts > 600:
            _NONCES.pop(k, None)
    if len(_IDEM) > 2000:
        for k in list(_IDEM.keys())[:1000]:
            _IDEM.pop(k, None)


# ---------------------------------------------------------------------------
# 统一异常模拟中间件
# ---------------------------------------------------------------------------
@app.middleware("http")
async def mock_mode_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/mock/"):
        return await call_next(request)

    _prune()
    mode = (request.headers.get("X-Mock-Mode") or "normal").strip().lower()
    trace_id = request.headers.get("X-Trace-ID", "")
    delay_ms = int(request.headers.get("X-Mock-Delay-Ms") or 0)
    fail_n = int(request.headers.get("X-Mock-Fail-N") or 0)
    idem_on = (request.headers.get("X-Mock-Idempotency") or "").strip().lower() == "on"

    async def respond(status: int, payload: dict[str, Any]) -> JSONResponse:
        payload.setdefault("meta", {}).update(
            {"mock": True, "trace_id": trace_id, "server_time": D.now_iso(), "mode": mode}
        )
        return JSONResponse(status_code=status, content=payload)

    # 前 N 次失败（用于验证"重试后成功"路径）
    if fail_n > 0:
        key = f"{path}:{fail_n}"
        used = _FAIL_COUNTER.get(key, 0)
        if used < fail_n:
            _FAIL_COUNTER[key] = used + 1
            return await respond(
                500,
                {
                    "ok": False,
                    "error": {
                        "code": "TRANSIENT_MOCK_FAILURE",
                        "message": f"模拟瞬时故障（第 {used + 1}/{fail_n} 次）",
                        "retryable": True,
                    },
                },
            )
        _FAIL_COUNTER.pop(key, None)

    # 注入延迟
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)

    if mode == "timeout":
        # 挂起到客户端超时，再返回 504（用于验证超时降级与重试）。
        # 睡眠时长只需略大于客户端超时即可——设得过长（如 30s）会让
        # "重试 N 次"的用例把测试拖到几分钟（本项目实测踩到）。
        sleep_s = min(8.0, max(2.0, settings.mock_timeout_seconds + 2.0))
        await asyncio.sleep(sleep_s)
        return await respond(
            504,
            {"ok": False, "error": {"code": "MOCK_TIMEOUT", "message": "模拟后端超时", "retryable": True}},
        )

    if mode == "error":
        return await respond(
            500,
            {
                "ok": False,
                "error": {
                    "code": "BILLING_UPSTREAM_ERROR" if "billing" in path else "MOCK_UPSTREAM_ERROR",
                    "message": "模拟后端 5xx 错误",
                    "retryable": True,
                },
            },
        )

    if mode == "unauthorized":
        return await respond(
            401,
            {
                "ok": False,
                "error": {"code": "TOKEN_INVALID", "message": "模拟鉴权失败", "retryable": False},
            },
        )

    if mode == "bad_data":
        # 返回 200 但结构不符合契约（字段缺失/类型错误）→ 触发调用方 Pydantic 兜底
        return await respond(200, {"ok": True, "data": {"unexpected_field": "oops", "amount": "not-a-number"}})

    # ---- normal：处理幂等 ----
    if idem_on:
        idem_key = request.headers.get("Idempotency-Key", "")
        if idem_key and idem_key in _IDEM:
            cached = dict(_IDEM[idem_key])
            cached.setdefault("data", {})
            cached["data"] = {**cached.get("data", {}), "replayed": True}
            cached["meta"] = {"mock": True, "trace_id": trace_id, "server_time": D.now_iso(), "mode": mode}
            return JSONResponse(status_code=200, content=cached)

        response = await call_next(request)
        body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += chunk
        if idem_key and response.status_code == 200:
            try:
                _IDEM[idem_key] = json.loads(body)
            except Exception as exc:  # noqa: BLE001
                # 响应体不是 JSON 时无法缓存幂等结果：会让后续同 key 请求重放，
                # 因此必须留痕（此前是静默 pass）。
                log.warning("mock.idem_cache_failed", error=str(exc)[:150])
        return JSONResponse(
            status_code=response.status_code,
            content=json.loads(body) if body else {},
            headers=dict(response.headers),
        )

    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Mock 服务健康检查。"""
    return {"status": "ok", "service": "mock-backend", "time": D.now_iso()}


def _ok(data: dict[str, Any], trace_id: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {"mock": True, "trace_id": trace_id, "server_time": D.now_iso()},
    }


def _err(status: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable},
            "meta": {"mock": True, "server_time": D.now_iso()},
        },
    )


# ===========================================================================
# 集成域（技能 2）
# ===========================================================================
@app.get("/mock/integration/config")
async def integration_config(
    tenant_id: str, workspace_id: str, provider: str = "", x_trace_id: str = Header(default="")
) -> dict[str, Any]:
    """查询集成配置。"""
    prof = D.effective_profile(workspace_id)
    expired = prof["token_expired"]
    issued = D.now_iso()
    return _ok(
        {
            "workspace_id": workspace_id,
            "provider": provider or prof["provider"],
            "connected": prof["fault"] == "HEALTHY",
            "bot_user": prof["bot_user"],
            "webhook_url": D.stored_webhook(workspace_id),
            "webhook_expected_url": D.correct_webhook(workspace_id),
            "webhook_reachable": prof["webhook_reachable"],
            "token": {
                "kind": "bot",
                "prefix": "xoxb-****",
                "issued_at": issued,
                "expires_at": issued if expired else D.now_iso(),
                "expired": expired,
                "scopes": prof["scopes"],
            },
            "last_event_hours_ago": prof["last_event_hours_ago"],
            "health": "healthy" if prof["fault"] == "HEALTHY" else "degraded",
        },
        x_trace_id,
    )


@app.post("/mock/integration/token/refresh")
async def integration_token_refresh(request: Request) -> Any:
    """刷新访问令牌。"""
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    prof = D.effective_profile(workspace_id)
    refresh_token = body.get("refresh_token", "")

    if prof["fault"] == "SCOPE_REVOKED":
        # 刷新成功但权限不足：仍不可用，需要重授权
        return _err(403, "SCOPE_REVOKED", "应用权限已被移除，刷新令牌无法恢复，需要重新授权", retryable=False)
    if not prof["has_valid_refresh_token"]:
        # 该工作区的刷新令牌也已失效 → 只能引导用户重新授权
        return _err(401, "REFRESH_TOKEN_INVALID", "刷新令牌已失效，需要用户重新授权", retryable=False)
    if not refresh_token:
        return _err(401, "REFRESH_TOKEN_INVALID", "未提供刷新令牌，需要用户重新授权", retryable=False)

    D.mark_token_refreshed(workspace_id)
    return _ok(
        {
            "workspace_id": workspace_id,
            "refreshed": True,
            "token": {
                "kind": "bot",
                "prefix": "xoxb-****",
                "issued_at": D.now_iso(),
                "expires_at": D.now_iso(),
                "expired": False,
                "scopes": prof["scopes"],
            },
        }
    )


@app.post("/mock/integration/webhook/repair")
async def integration_webhook_repair(request: Request) -> dict[str, Any]:
    """修复 Webhook 回调地址。"""
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    D.mark_webhook_repaired(workspace_id)
    return _ok(
        {
            "workspace_id": workspace_id,
            "webhook_url": D.correct_webhook(workspace_id),
            "webhook_reachable": True,
            "repaired": True,
        }
    )


@app.post("/mock/integration/reauth-link")
async def integration_reauth_link(request: Request) -> dict[str, Any]:
    """生成重新授权链接（Token 刷新不可用时的降级路径）。"""
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    provider = body.get("provider", "slack")
    return _ok(
        {
            "authorize_url": (
                f"https://auth.example.com/oauth/authorize?workspace={workspace_id}"
                f"&provider={provider}&state=st-{int(time.time())}"
            ),
            "expires_in_seconds": 900,
        }
    )


@app.post("/mock/integration/verify")
async def integration_verify(request: Request) -> Any:
    """**真实验证**：向该工作区投递一条验证消息并返回回执。

    这是"真调用假后端"的关键证据点——验证不是查本地标记。
    """
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    channel = body.get("channel", "#support-verify")
    prof = D.effective_profile(workspace_id)

    if prof["fault"] != "HEALTHY":
        return _err(502, "DELIVERY_FAILED", "验证消息投递失败：集成仍不可用", retryable=True)

    return _ok(
        {
            "delivered": True,
            "channel": channel,
            "provider_message_id": f"msg-{hashlib.md5(workspace_id.encode()).hexdigest()[:6]}",
            "latency_ms": 142,
        }
    )


# ===========================================================================
# 权限域（技能 3）
# ===========================================================================
@app.get("/mock/permission/document")
async def permission_document(
    tenant_id: str, doc_id: str, user_id: str, x_trace_id: str = Header(default="")
) -> dict[str, Any]:
    """查询文档权限状态。"""
    prof = D.document_profile(doc_id)
    fault = prof["fault"]
    return _ok(
        {
            "doc_id": doc_id,
            "title": prof["title"],
            "owner": "u-owner-3",
            "share": {
                "mode": "private" if fault == "SHARE_NOT_ENABLED" else "org",
                "link_enabled": fault != "SHARE_NOT_ENABLED",
                "allow_external": fault != "ORG_POLICY_BLOCKED",
            },
            "org_policy": {
                "external_sharing_allowed": False,
                "min_role_to_share": "admin",
            },
            "user": {"user_id": user_id, "role": "member", "in_org": True},
            "version_conflict": fault == "VERSION_CONFLICT",
            "user_can_open": fault == "ACCESSIBLE",
            "deny_reason": "" if fault == "ACCESSIBLE" else fault,
        },
        x_trace_id,
    )


@app.post("/mock/permission/share/grant")
async def permission_share_grant(request: Request) -> dict[str, Any]:
    """开启分享 / 授予访问权限。"""
    body = await request.json()
    return _ok(
        {
            "granted": True,
            "doc_id": body.get("doc_id", ""),
            "grantee_user_id": body.get("grantee_user_id", ""),
            "role": body.get("role", "viewer"),
        }
    )


@app.post("/mock/permission/policy/exception")
async def permission_policy_exception(request: Request) -> dict[str, Any]:
    """申请组织策略例外（需审批）。"""
    body = await request.json()
    return _ok(
        {
            "request_id": f"pol-{int(time.time()) % 100000}",
            "status": "pending_approval",
            "approver_role": "admin",
            "sla_hours": 4,
            "doc_id": body.get("doc_id", ""),
        }
    )


@app.post("/mock/permission/version/unlock")
async def permission_version_unlock(request: Request) -> dict[str, Any]:
    """解除版本冲突锁。"""
    body = await request.json()
    return _ok(
        {
            "unlocked": True,
            "doc_id": body.get("doc_id", ""),
            "kept_version": "v12",
            "discarded_version": "v11-draft",
        }
    )


@app.post("/mock/permission/verify-access")
async def permission_verify_access(request: Request) -> dict[str, Any]:
    """复核可访问性。"""
    body = await request.json()
    doc_id = body.get("doc_id", "")
    via = body.get("via", "direct_grant")
    return _ok(
        {
            "user_can_open": True,
            "via": via,
            "doc_id": doc_id,
            "checked_at": D.now_iso(),
        }
    )


# ===========================================================================
# 计费域（技能 4）
# ===========================================================================
def _verify_billing_security(
    raw_body: bytes,
    timestamp: str,
    signature: str,
    nonce: str,
    secret: str,
    window: int,
) -> JSONResponse | None:
    """校验签名、时间戳窗口与防重放。返回错误响应或 None（通过）。"""
    if not timestamp or not signature or not nonce:
        return _err(401, "SIGNATURE_MISSING", "缺少签名要素（X-Timestamp / X-Signature / X-Nonce）")

    try:
        ts = int(timestamp)
    except ValueError:
        return _err(401, "TIMESTAMP_INVALID", "时间戳格式非法")

    if abs(int(time.time()) - ts) > window:
        return _err(401, "TIMESTAMP_OUT_OF_WINDOW", f"时间戳超出允许窗口（±{window}s）")

    expected = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return _err(401, "SIGNATURE_INVALID", "签名校验失败")

    if nonce in _NONCES:
        return _err(409, "NONCE_REPLAYED", "该 nonce 已被使用（防重放拦截）")
    _NONCES[nonce] = time.time()
    return None


@app.get("/mock/billing/subscription")
async def billing_subscription(tenant_id: str, account_id: str, x_trace_id: str = Header(default="")) -> dict[str, Any]:
    """查询订阅信息。"""
    p = D.account_profile(account_id)
    return _ok(
        {
            "account_id": account_id,
            "plan": p["plan"],
            "seats": p["seats"],
            "status": "active",
            "started_at": D.now_iso(),
            "renews_at": D.now_iso(),
            "tenure_months": p["tenure_months"],
            "user_tier": p["user_tier"],
            "open_complaints": p["open_complaints"],
        },
        x_trace_id,
    )


@app.get("/mock/billing/invoice")
async def billing_invoice(
    tenant_id: str, account_id: str, period: str = "", x_trace_id: str = Header(default="")
) -> dict[str, Any]:
    """查询账单。"""
    p = D.account_profile(account_id)
    per = period or D.period()
    seat_amount = p["seats"] * 80.0
    overage_amount = round(p["overage"] * 0.01, 2)
    return _ok(
        {
            "invoice_id": f"inv-{per}-{account_id}",
            "account_id": account_id,
            "period": per,
            "amount": p["amount"],
            "currency": "CNY",
            "status": "unpaid" if p["disputed"] else "paid",
            "lines": [
                {"sku": "seat-monthly", "qty": p["seats"], "unit_price": 80.0, "amount": seat_amount},
                {"sku": "api-overage", "qty": p["overage"], "unit_price": 0.01, "amount": overage_amount},
            ],
            "disputed": p["disputed"],
            "dispute_reason": p["dispute_reason"],
        },
        x_trace_id,
    )


@app.get("/mock/billing/usage")
async def billing_usage(tenant_id: str, account_id: str, period: str = "") -> dict[str, Any]:
    """查询用量。"""
    p = D.account_profile(account_id)
    return _ok(
        {
            "period": period or D.period(),
            "api_calls": p["api_calls"],
            "included_quota": p["included_quota"],
            "overage": p["overage"],
            "storage_gb": 42.5,
        }
    )


@app.post("/mock/billing/adjustment")
async def billing_adjustment(
    request: Request,
    x_timestamp: str = Header(default=""),
    x_signature: str = Header(default=""),
    x_nonce: str = Header(default=""),
) -> Any:
    """发起账务调整（写操作，受签名 + 时间戳 + 防重放保护）。"""
    raw = await request.body()
    guard = _verify_billing_security(
        raw,
        x_timestamp,
        x_signature,
        x_nonce,
        settings.billing_hmac_secret,
        settings.billing_timestamp_window_seconds,
    )
    if guard is not None:
        return guard

    body = json.loads(raw or b"{}")
    return _ok(
        {
            "adjustment_id": f"adj-{int(time.time()) % 1000000}",
            "status": "succeeded",
            "amount": body.get("amount", 0.0),
            "account_id": body.get("account_id", ""),
            "replayed": False,
        }
    )


@app.post("/mock/billing/adjustment/approval")
async def billing_adjustment_approval(request: Request) -> dict[str, Any]:
    """转人工审批。"""
    body = await request.json()
    return _ok(
        {
            "approval_id": f"apv-{int(time.time()) % 100000}",
            "status": "pending",
            "approver_role": "billing_admin",
            "account_id": body.get("account_id", ""),
            "amount": body.get("amount", 0.0),
            "risk_score": body.get("risk_score", 0.0),
        }
    )


# ===========================================================================
# 账号安全域（技能 5）
# ===========================================================================
@app.get("/mock/account/login-events")
async def account_login_events(
    tenant_id: str, user_id: str, days: int = 7, x_trace_id: str = Header(default="")
) -> dict[str, Any]:
    """查询登录事件与基线。"""
    return _ok(D.login_events(user_id, days), x_trace_id)


@app.post("/mock/account/lock")
async def account_lock(request: Request) -> dict[str, Any]:
    """锁定账号并吊销会话。"""
    body = await request.json()
    user_id = body.get("user_id", "")
    # 记录处置事实：复核接口据此判定安全态，保证"处置 → 复核"是真闭环
    D.mark_account_remediated(user_id, "lock_account")
    return _ok(
        {
            "locked": True,
            "user_id": user_id,
            "until": D.now_iso(),
            "sessions_revoked": 3,
            "lock_type": body.get("lock_type", "temporary"),
        }
    )


@app.post("/mock/account/notify")
async def account_notify(request: Request) -> dict[str, Any]:
    """发送安全通知。"""
    body = await request.json()
    return _ok(
        {
            "sent": True,
            "channel": body.get("channel", "email"),
            "template": body.get("template", "security_alert"),
            "message_id": f"ntf-{int(time.time()) % 100000}",
        }
    )


@app.post("/mock/account/password/reset-required")
async def account_reset_required(request: Request) -> dict[str, Any]:
    """强制下次登录重置口令。"""
    body = await request.json()
    user_id = body.get("user_id", "")
    # 强制重置口令同样属于真实处置动作
    D.mark_account_remediated(user_id, "require_password_reset")
    return _ok(
        {
            "required": True,
            "user_id": user_id,
            "reset_token_issued": True,
            "expires_in_seconds": 1800,
        }
    )


@app.post("/mock/account/verify")
async def account_verify(request: Request) -> dict[str, Any]:
    """复核处置结果。"""
    body = await request.json()
    return _ok(D.verify_account_state(body.get("user_id", "")))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
