"""业务工具集（五类技能共用）。

每个工具：
  - 有明确的 Pydantic 出参 schema（结构不符即降级，不把脏数据带进诊断）
  - 通过 ``ToolClient`` 调用外部系统（本机为 Mock 后端，生产为真实系统）
  - 自动带链路上下文；每次调用都会被记录以供回放

切换为生产对接时，只需替换 ``MOCK_BASE_URL``，本文件无需改动。
每处调用点均标注 ``# TODO[生产对接]``，schema 见 docs/mock-boundary.md。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import settings
from app.tools.client import ToolClient, ToolError, get_tool_client


# ===========================================================================
# 出参 Schema
# ===========================================================================
class TokenInfo(BaseModel):
    kind: str = "bot"
    prefix: str = ""
    issued_at: str = ""
    expires_at: str = ""
    expired: bool = False
    scopes: list[str] = Field(default_factory=list)


class IntegrationConfig(BaseModel):
    workspace_id: str
    provider: str = ""
    connected: bool = False
    bot_user: str = ""
    webhook_url: str = ""
    webhook_expected_url: str = ""
    webhook_reachable: bool = False
    token: TokenInfo
    last_event_hours_ago: int = 0
    health: str = "unknown"


class TokenRefreshResult(BaseModel):
    workspace_id: str
    refreshed: bool = False
    token: TokenInfo


class WebhookRepairResult(BaseModel):
    workspace_id: str
    webhook_url: str
    webhook_reachable: bool
    repaired: bool


class ReauthLink(BaseModel):
    authorize_url: str
    expires_in_seconds: int = 900


class VerifyDelivery(BaseModel):
    delivered: bool
    channel: str = ""
    provider_message_id: str = ""
    latency_ms: float = 0.0


class DocumentPermission(BaseModel):
    doc_id: str
    title: str = ""
    owner: str = ""
    share: dict[str, Any] = Field(default_factory=dict)
    org_policy: dict[str, Any] = Field(default_factory=dict)
    user: dict[str, Any] = Field(default_factory=dict)
    version_conflict: bool = False
    user_can_open: bool = False
    deny_reason: str = ""


class AccessVerify(BaseModel):
    user_can_open: bool
    via: str = ""
    doc_id: str = ""
    checked_at: str = ""


class SubscriptionInfo(BaseModel):
    account_id: str
    plan: str = ""
    seats: int = 0
    status: str = ""
    tenure_months: int = 0
    user_tier: str = "basic"
    open_complaints: int = 0


class InvoiceInfo(BaseModel):
    invoice_id: str
    account_id: str = ""
    period: str = ""
    amount: float = 0.0
    currency: str = "CNY"
    status: str = ""
    lines: list[dict[str, Any]] = Field(default_factory=list)
    disputed: bool = False
    dispute_reason: str = ""


class UsageInfo(BaseModel):
    period: str = ""
    api_calls: int = 0
    included_quota: int = 0
    overage: int = 0
    storage_gb: float = 0.0


class AdjustmentResult(BaseModel):
    adjustment_id: str
    status: str = ""
    amount: float = 0.0
    account_id: str = ""
    replayed: bool = False


class ApprovalResult(BaseModel):
    approval_id: str
    status: str = "pending"
    approver_role: str = ""
    account_id: str = ""
    amount: float = 0.0
    risk_score: float = 0.0


class LoginEvent(BaseModel):
    at: str
    ip: str = ""
    geo: str = ""
    device: str = ""
    result: str = ""
    mfa: bool = False
    risk_flags: list[str] = Field(default_factory=list)


class LoginEvents(BaseModel):
    user_id: str
    events: list[LoginEvent] = Field(default_factory=list)
    baseline: dict[str, Any] = Field(default_factory=dict)
    anomaly_score_hint: float = 0.0


class LockResult(BaseModel):
    locked: bool
    user_id: str = ""
    until: str = ""
    sessions_revoked: int = 0
    lock_type: str = "temporary"


class NotifyResult(BaseModel):
    sent: bool
    channel: str = ""
    template: str = ""
    message_id: str = ""


class AccountVerify(BaseModel):
    sessions_active: int = 0
    mfa_enabled: bool = False
    risk_score: float = 0.0
    safe: bool = False


# ===========================================================================
# 集成域工具（技能 2）
# ===========================================================================
def query_integration_config(
    workspace_id: str, provider: str = "", *, client: ToolClient | None = None
) -> IntegrationConfig:
    """查询集成配置：令牌有效期、Webhook 可达性、授权范围。

    # TODO[生产对接]: 云枢协作平台 开放平台 API · schema 见 docs/mock-boundary.md §2.1
    """
    c = client or get_tool_client()
    return c.validated(
        IntegrationConfig,
        "query_integration_config",
        "GET",
        "/mock/integration/config",
        params={"tenant_id": _tenant(), "workspace_id": workspace_id, "provider": provider},
    )


def refresh_integration_token(
    workspace_id: str, refresh_token: str = "", *, client: ToolClient | None = None
) -> TokenRefreshResult:
    """刷新集成访问令牌。

    # TODO[生产对接]: 云枢协作平台 OAuth 令牌端点 · schema 见 docs/mock-boundary.md §2.2
    """
    c = client or get_tool_client()
    return c.validated(
        TokenRefreshResult,
        "refresh_integration_token",
        "POST",
        "/mock/integration/token/refresh",
        json_body={
            "tenant_id": _tenant(),
            "workspace_id": workspace_id,
            "refresh_token": refresh_token,
        },
    )


def repair_webhook(workspace_id: str, expected_url: str = "", *, client: ToolClient | None = None) -> WebhookRepairResult:
    """修复 Webhook 回调地址。

    # TODO[生产对接]: 云枢协作平台 Webhook 配置接口 · schema 见 docs/mock-boundary.md §2.3
    """
    c = client or get_tool_client()
    return c.validated(
        WebhookRepairResult,
        "repair_webhook",
        "POST",
        "/mock/integration/webhook/repair",
        json_body={"tenant_id": _tenant(), "workspace_id": workspace_id, "expected_url": expected_url},
    )


def request_reauth_link(workspace_id: str, provider: str = "", *, client: ToolClient | None = None) -> ReauthLink:
    """生成重新授权链接（令牌刷新不可用时的降级路径）。

    # TODO[生产对接]: 云枢协作平台 OAuth 授权端点 · schema 见 docs/mock-boundary.md §2.4
    """
    c = client or get_tool_client()
    return c.validated(
        ReauthLink,
        "request_reauth_link",
        "POST",
        "/mock/integration/reauth-link",
        json_body={"tenant_id": _tenant(), "workspace_id": workspace_id, "provider": provider},
    )


def verify_integration(
    workspace_id: str, channel: str = "#support-verify", *, client: ToolClient | None = None
) -> VerifyDelivery:
    """验证集成可达性：**真实投递一条验证消息**并取回执。

    # TODO[生产对接]: 云枢协作平台 消息投递接口 · schema 见 docs/mock-boundary.md §2.5
    """
    c = client or get_tool_client()
    return c.validated(
        VerifyDelivery,
        "verify_integration",
        "POST",
        "/mock/integration/verify",
        json_body={
            "tenant_id": _tenant(),
            "workspace_id": workspace_id,
            "provider": "",
            "channel": channel,
            "text": "【智汇云客服】集成连通性验证消息",
        },
        max_retries=1,
    )


# ===========================================================================
# 权限域工具（技能 3）
# ===========================================================================
def query_document_permission(doc_id: str, user_id: str, *, client: ToolClient | None = None) -> DocumentPermission:
    """查询文档权限与组织策略。

    # TODO[生产对接]: 云枢文档服务 权限 API · schema 见 docs/mock-boundary.md §3.1
    """
    c = client or get_tool_client()
    return c.validated(
        DocumentPermission,
        "query_document_permission",
        "GET",
        "/mock/permission/document",
        params={"tenant_id": _tenant(), "doc_id": doc_id, "user_id": user_id},
    )


def grant_document_share(
    doc_id: str, grantee_user_id: str, role: str = "viewer", *, client: ToolClient | None = None
) -> dict[str, Any]:
    """开启分享并授予访问权限。

    # TODO[生产对接]: 云枢文档服务 分享接口 · schema 见 docs/mock-boundary.md §3.2
    """
    c = client or get_tool_client()
    return c.request(
        "grant_document_share",
        "POST",
        "/mock/permission/share/grant",
        json_body={
            "tenant_id": _tenant(),
            "doc_id": doc_id,
            "grantee_user_id": grantee_user_id,
            "role": role,
        },
    )


def request_policy_exception(
    doc_id: str, user_id: str, justification: str, *, client: ToolClient | None = None
) -> dict[str, Any]:
    """为个人申请组织策略例外（需审批）。

    # TODO[生产对接]: 云枢审批流 · schema 见 docs/mock-boundary.md §3.3
    """
    c = client or get_tool_client()
    return c.request(
        "request_policy_exception",
        "POST",
        "/mock/permission/policy/exception",
        json_body={
            "tenant_id": _tenant(),
            "doc_id": doc_id,
            "user_id": user_id,
            "justification": justification,
        },
    )


def unlock_document_version(doc_id: str, force: bool = False, *, client: ToolClient | None = None) -> dict[str, Any]:
    """解除版本冲突锁。

    # TODO[生产对接]: 云枢文档服务 版本接口 · schema 见 docs/mock-boundary.md §3.4
    """
    c = client or get_tool_client()
    return c.request(
        "unlock_document_version",
        "POST",
        "/mock/permission/version/unlock",
        json_body={"tenant_id": _tenant(), "doc_id": doc_id, "force": force},
    )


def verify_document_access(
    doc_id: str, user_id: str, via: str = "direct_grant", *, client: ToolClient | None = None
) -> AccessVerify:
    """复核文档可访问性。

    # TODO[生产对接]: 云枢文档服务 权限校验接口 · schema 见 docs/mock-boundary.md §3.5
    """
    c = client or get_tool_client()
    return c.validated(
        AccessVerify,
        "verify_document_access",
        "POST",
        "/mock/permission/verify-access",
        json_body={"tenant_id": _tenant(), "doc_id": doc_id, "user_id": user_id, "via": via},
    )


# ===========================================================================
# 计费域工具（技能 4）
# ===========================================================================
def query_subscription(account_id: str, *, client: ToolClient | None = None) -> SubscriptionInfo:
    """查询订阅信息。

    # TODO[生产对接]: 云枢计费系统 订阅接口 · schema 见 docs/mock-boundary.md §4.2
    """
    c = client or get_tool_client()
    return c.validated(
        SubscriptionInfo,
        "query_subscription",
        "GET",
        "/mock/billing/subscription",
        params={"tenant_id": _tenant(), "account_id": account_id},
    )


def query_invoice(account_id: str, period: str = "", *, client: ToolClient | None = None) -> InvoiceInfo:
    """查询账单。

    # TODO[生产对接]: 云枢计费系统 账单接口 · schema 见 docs/mock-boundary.md §4.3
    """
    c = client or get_tool_client()
    return c.validated(
        InvoiceInfo,
        "query_invoice",
        "GET",
        "/mock/billing/invoice",
        params={"tenant_id": _tenant(), "account_id": account_id, "period": period},
    )


def query_usage(account_id: str, period: str = "", *, client: ToolClient | None = None) -> UsageInfo:
    """查询用量。

    # TODO[生产对接]: 云枢计费系统 用量接口 · schema 见 docs/mock-boundary.md §4.4
    """
    c = client or get_tool_client()
    return c.validated(
        UsageInfo,
        "query_usage",
        "GET",
        "/mock/billing/usage",
        params={"tenant_id": _tenant(), "account_id": account_id, "period": period},
    )


def _billing_headers(raw_body: bytes, idempotency_key: str) -> dict[str, str]:
    """构造计费写操作的安全头：签名 + 时间戳 + 防重放随机数 + 幂等键。"""
    ts = str(int(time.time()))
    signature = hmac.new(
        settings.billing_hmac_secret.encode("utf-8"), f"{ts}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Timestamp": ts,
        "X-Signature": signature,
        "X-Nonce": uuid.uuid4().hex,
        "Idempotency-Key": idempotency_key,
    }


def create_billing_adjustment(
    account_id: str,
    amount: float,
    kind: str = "refund",
    reason: str = "",
    invoice_id: str = "",
    idempotency_key: str = "",
    operator: str = "ai",
    *,
    client: ToolClient | None = None,
) -> AdjustmentResult:
    """发起账务调整（退款 / 抵扣）。

    写操作必须带幂等键；签名与时间戳由本函数按契约生成。

    # TODO[生产对接]: 云枢计费系统 账务调整接口 · schema 见 docs/mock-boundary.md §4.5
    """
    c = client or get_tool_client()
    body = {
        "tenant_id": _tenant(),
        "account_id": account_id,
        "invoice_id": invoice_id,
        "amount": amount,
        "currency": "CNY",
        "kind": kind,
        "reason": reason,
        "operator": operator,
    }
    # 关键：先序列化为字节，签名与发送使用**同一段字节**。
    # 若交给 HTTP 库自行序列化，客户端签的字节与服务端收到的不一致，签名必然失败。
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    idem = idempotency_key or f"auto-{uuid.uuid4().hex[:16]}"
    headers = _billing_headers(raw, idem)
    headers["Content-Type"] = "application/json"
    return c.validated(
        AdjustmentResult,
        "create_billing_adjustment",
        "POST",
        "/mock/billing/adjustment",
        content=raw,
        headers=headers,
    )


def request_billing_approval(
    account_id: str,
    amount: float,
    reason: str = "",
    risk_score: float = 0.0,
    invoice_id: str = "",
    *,
    client: ToolClient | None = None,
) -> ApprovalResult:
    """转人工审批（金额或风险分超阈值时）。

    # TODO[生产对接]: 云枢计费审批流 · schema 见 docs/mock-boundary.md §4.6
    """
    c = client or get_tool_client()
    return c.validated(
        ApprovalResult,
        "request_billing_approval",
        "POST",
        "/mock/billing/adjustment/approval",
        json_body={
            "tenant_id": _tenant(),
            "account_id": account_id,
            "invoice_id": invoice_id,
            "amount": amount,
            "reason": reason,
            "risk_score": risk_score,
        },
    )


# ===========================================================================
# 账号安全域工具（技能 5）
# ===========================================================================
def query_login_events(user_id: str, days: int = 7, *, client: ToolClient | None = None) -> LoginEvents:
    """查询登录事件与行为基线。

    # TODO[生产对接]: 云枢账号风控 API · schema 见 docs/mock-boundary.md §5.1
    """
    c = client or get_tool_client()
    return c.validated(
        LoginEvents,
        "query_login_events",
        "GET",
        "/mock/account/login-events",
        params={"tenant_id": _tenant(), "user_id": user_id, "days": days},
    )


def lock_account(
    user_id: str, reason: str, lock_type: str = "temporary", duration_minutes: int = 120,
    *, client: ToolClient | None = None,
) -> LockResult:
    """锁定账号并吊销会话。

    # TODO[生产对接]: 云枢账号风控 锁定接口 · schema 见 docs/mock-boundary.md §5.2
    """
    c = client or get_tool_client()
    return c.validated(
        LockResult,
        "lock_account",
        "POST",
        "/mock/account/lock",
        json_body={
            "tenant_id": _tenant(),
            "user_id": user_id,
            "reason": reason,
            "lock_type": lock_type,
            "duration_minutes": duration_minutes,
        },
    )


def notify_user(
    user_id: str, channel: str = "email", template: str = "security_alert",
    payload: dict[str, Any] | None = None, *, client: ToolClient | None = None,
) -> NotifyResult:
    """发送通知。

    # TODO[生产对接]: 云枢通知服务 · schema 见 docs/mock-boundary.md §5.3
    """
    c = client or get_tool_client()
    return c.validated(
        NotifyResult,
        "notify_user",
        "POST",
        "/mock/account/notify",
        json_body={
            "tenant_id": _tenant(),
            "user_id": user_id,
            "channel": channel,
            "template": template,
            "payload": payload or {},
        },
    )


def require_password_reset(user_id: str, *, client: ToolClient | None = None) -> dict[str, Any]:
    """要求下次登录重置口令。

    # TODO[生产对接]: 云枢账号服务 · schema 见 docs/mock-boundary.md §5.4
    """
    c = client or get_tool_client()
    return c.request(
        "require_password_reset",
        "POST",
        "/mock/account/password/reset-required",
        json_body={"tenant_id": _tenant(), "user_id": user_id},
    )


def verify_account_security(user_id: str, *, client: ToolClient | None = None) -> AccountVerify:
    """复核账号处置结果。

    # TODO[生产对接]: 云枢账号风控 校验接口 · schema 见 docs/mock-boundary.md §5.5
    """
    c = client or get_tool_client()
    return c.validated(
        AccountVerify,
        "verify_account_security",
        "POST",
        "/mock/account/verify",
        json_body={"tenant_id": _tenant(), "user_id": user_id},
    )


# ===========================================================================
# 内部工具
# ===========================================================================
def _tenant() -> str:
    """当前租户（用于外部系统调用参数）。"""
    from app.core.trace_context import get_tenant_id

    tid = get_tenant_id()
    return tid if tid != "-" else "t-001"


__all__ = [
    "DocumentPermission",
    "IntegrationConfig",
    "InvoiceInfo",
    "LoginEvents",
    "SubscriptionInfo",
    "ToolError",
    "UsageInfo",
    "create_billing_adjustment",
    "grant_document_share",
    "lock_account",
    "notify_user",
    "query_document_permission",
    "query_integration_config",
    "query_invoice",
    "query_login_events",
    "query_subscription",
    "query_usage",
    "refresh_integration_token",
    "repair_webhook",
    "request_billing_approval",
    "request_policy_exception",
    "request_reauth_link",
    "require_password_reset",
    "unlock_document_version",
    "verify_account_security",
    "verify_document_access",
    "verify_integration",
]
