"""Mock 业务后端的确定性数据。

**确定性**是这里最重要的一条设计：同一个人 workspace_id / doc_id / account_id
永远得到同一份故障画像。这样：
  - 案例库的"期望诊断"才有意义（测试可断言）
  - 演示可复现（不会这次是 Token 过期、下次变成 Webhook 失效）
  - 并发测试不需要担心数据竞争

故障画像与 docs/mock-boundary.md 中列的表严格对应。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")




# ===========================================================================
# 集成域：工作区故障画像
# ===========================================================================
# 每个工作区预设一种故障，诊断链应能识别出对应的 cause_code
WORKSPACE_PROFILES: dict[str, dict[str, Any]] = {
    # --- 单一故障画像：每个工作区只带一种故障，便于"诊断→修复→验证"形成闭环 ---
    # ws-1001：仅令牌过期（回调地址正常）。刷新令牌后应恢复可达。
    "ws-1001": {
        "provider": "slack",
        "fault": "TOKEN_EXPIRED",
        "token_expired": True,
        "has_valid_refresh_token": True,
        "webhook_reachable": True,
        "webhook_path": "current-path",
        "scopes": ["chat:write", "channels:read"],
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 19,
    },
    # ws-1002：仅回调地址失效（令牌正常）。修复回调后应恢复可达。
    "ws-1002": {
        "provider": "slack",
        "fault": "WEBHOOK_INVALID",
        "token_expired": False,
        "has_valid_refresh_token": True,
        "webhook_reachable": False,
        "webhook_path": "stale-path",
        "scopes": ["chat:write", "channels:read"],
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 3,
    },
    # ws-1003：仅权限被移除（无法自动修复，必须重授权）
    "ws-1003": {
        "provider": "feishu",
        "fault": "SCOPE_REVOKED",
        "token_expired": False,
        "has_valid_refresh_token": True,
        "webhook_reachable": True,
        "webhook_path": "current-path",
        "scopes": ["channels:read"],  # 缺少 chat:write
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 2,
    },
    # ws-1004：健康
    "ws-1004": {
        "provider": "dingtalk",
        "fault": "HEALTHY",
        "token_expired": False,
        "has_valid_refresh_token": True,
        "webhook_reachable": True,
        "webhook_path": "current-path",
        "scopes": ["chat:write", "channels:read"],
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 0,
    },
    # ws-1005：令牌过期且刷新令牌也失效 → 只能引导用户重新授权
    "ws-1005": {
        "provider": "dingtalk",
        "fault": "TOKEN_EXPIRED",
        "token_expired": True,
        "has_valid_refresh_token": False,
        "webhook_reachable": True,
        "webhook_path": "current-path",
        "scopes": ["chat:write", "channels:read"],
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 30,
    },
    # ws-1006：复合故障（令牌过期 + 回调失效）。
    # 刻意保留一个复合场景：用于验证"先修主因、验证失败后回退重试并继续修次因"的路径，
    # 以及"多步仍未解决则转人工"的熔断逻辑。
    "ws-1006": {
        "provider": "slack",
        "fault": "TOKEN_EXPIRED",
        "token_expired": True,
        "has_valid_refresh_token": True,
        "webhook_reachable": False,
        "webhook_path": "old-path",
        "scopes": ["chat:write", "channels:read"],
        "bot_user": "@yunshu-bot",
        "last_event_hours_ago": 20,
    },
}
DEFAULT_WORKSPACE = "ws-1001"


def workspace_profile(workspace_id: str) -> dict[str, Any]:
    """返回工作区画像（未登记的工作区按健康处理）。"""
    return WORKSPACE_PROFILES.get(
        workspace_id,
        {
            "provider": "slack",
            "fault": "HEALTHY",
            "token_expired": False,
            "has_valid_refresh_token": True,
            "webhook_reachable": True,
            "scopes": ["chat:write", "channels:read"],
            "bot_user": "@yunshu-bot",
            "webhook_path": "current-path",
            "last_event_hours_ago": 0,
        },
    )


def correct_webhook(workspace_id: str) -> str:
    return f"https://hooks.example.com/{workspace_id}/current-path"


def stored_webhook(workspace_id: str) -> str:
    prof = effective_profile(workspace_id)
    return f"https://hooks.example.com/{workspace_id}/{prof['webhook_path']}"


# ===========================================================================
# 运行期状态覆盖
# ===========================================================================
# 修复动作必须**真的改变后端状态**，否则"验证可达"永远失败：
# 静态画像会让 refresh/repair 变成无副作用的空操作。
# 这里记录一次运行内的状态变更（令牌已刷新 / 回调已修复），
# 使"诊断 → 修复 → 验证"形成真实因果链。
_RUNTIME_STATE: dict[str, dict[str, Any]] = {}


def _state_for(workspace_id: str) -> dict[str, Any]:
    return _RUNTIME_STATE.setdefault(workspace_id, {})


def effective_profile(workspace_id: str) -> dict[str, Any]:
    """叠加运行期变更后的工作区画像。"""
    prof = dict(workspace_profile(workspace_id))
    st = _state_for(workspace_id)
    if st.get("token_refreshed"):
        prof["token_expired"] = False
    if st.get("webhook_repaired"):
        prof["webhook_reachable"] = True
        prof["webhook_path"] = "current-path"
    # 令牌健康且回调可达且权限齐备 → 视为健康
    if (
        not prof["token_expired"]
        and prof["webhook_reachable"]
        and "chat:write" in prof["scopes"]
    ):
        prof["fault"] = "HEALTHY"
    return prof


def mark_token_refreshed(workspace_id: str) -> None:
    _state_for(workspace_id)["token_refreshed"] = True


def mark_webhook_repaired(workspace_id: str) -> None:
    _state_for(workspace_id)["webhook_repaired"] = True


# 账号维度的处置状态（与工作区状态分开存放，键是 user_id）。
# 同上：处置动作必须真的改变后端状态，否则"复核"会永远通过（或永远不通过）。
_ACCOUNT_STATE: dict[str, dict[str, Any]] = {}


def mark_account_remediated(user_id: str, action: str) -> None:
    """记录账号已被处置（锁定 / 强制重置口令 / 开启二次验证）。"""
    entry = _ACCOUNT_STATE.setdefault(user_id, {})
    entry["remediated"] = True
    entry.setdefault("actions", []).append(action)


def reset_account_state(user_id: str | None = None) -> None:
    """重置账号处置状态（测试用）。"""
    if user_id is None:
        _ACCOUNT_STATE.clear()
    else:
        _ACCOUNT_STATE.pop(user_id, None)


def reset_runtime_state(workspace_id: str | None = None) -> None:
    """重置运行期状态（测试用，保证用例互不影响）。"""
    if workspace_id is None:
        _RUNTIME_STATE.clear()
        _ACCOUNT_STATE.clear()
    else:
        _RUNTIME_STATE.pop(workspace_id, None)


# ===========================================================================
# 权限域：文档故障画像
# ===========================================================================
DOCUMENT_PROFILES: dict[str, dict[str, Any]] = {
    "doc-771": {"fault": "SHARE_NOT_ENABLED", "title": "Q4 产品路线图"},
    "doc-772": {"fault": "ORG_POLICY_BLOCKED", "title": "客户续约方案（含外链）"},
    "doc-773": {"fault": "VERSION_CONFLICT", "title": "API 接入说明"},
    "doc-774": {"fault": "ACCESSIBLE", "title": "团队周报"},
}
DEFAULT_DOC = "doc-771"


def document_profile(doc_id: str) -> dict[str, Any]:
    return DOCUMENT_PROFILES.get(doc_id, {"fault": "ACCESSIBLE", "title": "未命名文档"})


# ===========================================================================
# 计费域：账号故障画像
# ===========================================================================
ACCOUNT_PROFILES: dict[str, dict[str, Any]] = {
    # 重复计费：金额 380 超阈值 → 应触发转人工审批（金额维度）
    "acct-501": {
        "plan": "team",
        "seats": 25,
        "tenure_months": 9,
        "user_tier": "gold",
        "open_complaints": 1,
        "amount": 2380.00,
        "disputed": True,
        "dispute_reason": "重复计费：11 月席位费被计两次",
        "overage": 38000,
        "included_quota": 100000,
        "api_calls": 138000,
    },
    # 小额争议：金额 42 → 可自动调整
    "acct-502": {
        "plan": "starter",
        "seats": 3,
        "tenure_months": 2,
        "user_tier": "basic",
        "open_complaints": 0,
        "amount": 42.00,
        "disputed": True,
        "dispute_reason": "用量计数异常：少量超限费用",
        "overage": 4200,
        "included_quota": 10000,
        "api_calls": 14200,
    },
    # 高频投诉用户：应触发转人工（投诉次数维度）
    "acct-503": {
        "plan": "team",
        "seats": 12,
        "tenure_months": 4,
        "user_tier": "silver",
        "open_complaints": 4,
        "amount": 60.00,
        "disputed": True,
        "dispute_reason": "对计费规则反复投诉",
        "overage": 6000,
        "included_quota": 50000,
        "api_calls": 56000,
    },
    # 无争议
    "acct-504": {
        "plan": "enterprise",
        "seats": 120,
        "tenure_months": 26,
        "user_tier": "platinum",
        "open_complaints": 0,
        "amount": 9600.00,
        "disputed": False,
        "dispute_reason": "",
        "overage": 0,
        "included_quota": 500000,
        "api_calls": 120000,
    },
}
DEFAULT_ACCOUNT = "acct-501"


def account_profile(account_id: str) -> dict[str, Any]:
    return ACCOUNT_PROFILES.get(
        account_id,
        {
            "plan": "team",
            "seats": 5,
            "tenure_months": 1,
            "user_tier": "basic",
            "open_complaints": 0,
            "amount": 0.0,
            "disputed": False,
            "dispute_reason": "",
            "overage": 0,
            "included_quota": 10000,
            "api_calls": 0,
        },
    )


def period() -> str:
    """当前账期。"""
    d = datetime.now(TZ)
    return f"{d.year:04d}-{d.month:02d}"


# ===========================================================================
# 账号安全域：用户登录画像
# ===========================================================================
# 高风险：命中 新地域 + 异常时段 + 新设备 三个信号
USER_PROFILES: dict[str, dict[str, Any]] = {
    "u-42": {
        "risk": "HIGH",
        "usual_geo": ["CN-杭州", "CN-上海"],
        "usual_hours": [9, 10, 11, 14, 15, 16, 17, 18],
        "usual_devices": ["Edge/Windows", "iOS"],
        "anomalous_event": {
            "geo": "境外-未知",
            "ip": "203.0.113.77",
            "device": "Chrome/Windows",
            "hour": 3,
            "mfa": False,
            "risk_flags": ["new_geo", "odd_hour", "new_device"],
        },
    },
    # 中风险：仅异常时段
    "u-43": {
        "risk": "MEDIUM",
        "usual_geo": ["CN-杭州"],
        "usual_hours": [9, 10, 11, 14, 15, 16, 17, 18],
        "usual_devices": ["Edge/Windows"],
        "anomalous_event": {
            "geo": "CN-杭州",
            "ip": "10.20.30.41",
            "device": "Edge/Windows",
            "hour": 2,
            "mfa": True,
            "risk_flags": ["odd_hour"],
        },
    },
    # 低风险：正常登录
    "u-44": {
        "risk": "LOW",
        "usual_geo": ["CN-杭州"],
        "usual_hours": [9, 10, 11, 14, 15, 16, 17, 18],
        "usual_devices": ["Edge/Windows"],
        "anomalous_event": {
            "geo": "CN-杭州",
            "ip": "10.20.30.42",
            "device": "Edge/Windows",
            "hour": 10,
            "mfa": True,
            "risk_flags": [],
        },
    },
}
DEFAULT_USER = "u-42"


def user_profile(user_id: str) -> dict[str, Any]:
    return USER_PROFILES.get(user_id, USER_PROFILES[DEFAULT_USER])


def login_events(user_id: str, days: int = 7) -> dict[str, Any]:
    """构造登录事件列表：最近一条为"异常事件"，其余为基线内的正常登录。"""
    prof = user_profile(user_id)
    anom = prof["anomalous_event"]
    now = datetime.now(TZ)

    events: list[dict[str, Any]] = []
    # 异常事件（放在最近，但按画像设定的时辰）
    anom_time = (now - timedelta(hours=2)).replace(hour=anom["hour"], minute=14, second=0, microsecond=0)
    if anom_time > now:
        anom_time -= timedelta(days=1)
    events.append(
        {
            "at": anom_time.isoformat(timespec="seconds"),
            "ip": anom["ip"],
            "geo": anom["geo"],
            "device": anom["device"],
            "result": "success",
            "mfa": anom["mfa"],
            "risk_flags": list(anom["risk_flags"]),
        }
    )
    # 基线内正常登录若干条
    for i in range(1, 5):
        t = now - timedelta(days=i, hours=1)
        t = t.replace(hour=10, minute=2, second=0, microsecond=0)
        events.append(
            {
                "at": t.isoformat(timespec="seconds"),
                "ip": "10.20.30.40",
                "geo": prof["usual_geo"][0],
                "device": prof["usual_devices"][0],
                "result": "success",
                "mfa": True,
                "risk_flags": [],
            }
        )
    return {
        "user_id": user_id,
        "events": events,
        "baseline": {
            "usual_geo": prof["usual_geo"],
            "usual_hours": prof["usual_hours"],
            "usual_devices": prof["usual_devices"],
        },
        "anomaly_score_hint": {"HIGH": 0.87, "MEDIUM": 0.45, "LOW": 0.05}[prof["risk"]],
    }


def verify_account_state(user_id: str) -> dict[str, Any]:
    """处置后复核：**只有真的处置过**才算安全态。

    早期实现写的是 ``safe = prof["risk"] in ("HIGH", "MEDIUM")``，
    即"风险高就算已安全"——把静态画像当成了处置结果。
    后果很严重：账号处于高风险且从未被锁定/重置时，复核也会返回 safe=True，
    于是"处置 → 复核"这条闭环可以在**什么都没做**的情况下被判为通过，
    验证环节失去意义（审查发现）。

    正确判据：运行期是否记录过本账号的处置动作；未处置的高/中风险账号必须报告**不安全**。
    """
    prof = user_profile(user_id)
    remediated = bool(_ACCOUNT_STATE.get(user_id, {}).get("remediated"))
    safe = remediated or prof["risk"] == "LOW"
    return {
        "sessions_active": 0 if safe else 1,
        "mfa_enabled": bool(safe),
        "risk_score": 0.12 if safe else {"HIGH": 0.87, "MEDIUM": 0.45, "LOW": 0.05}[prof["risk"]],
        "safe": safe,
    }
