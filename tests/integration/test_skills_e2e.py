"""集成测试：五技能端到端。

每个技能都断言到"结论层"，而不只是"有回复"：
诊断编码、置信度/风险分、执行动作、验证结果。
"""

from __future__ import annotations

import pytest


class TestSkillIntegration:
    """技能 2：集成故障诊断与修复。"""

    @pytest.mark.integration
    def test_token_expired_auto_repaired_and_verified(self, api, headers, session_key):
        """令牌过期 → 刷新 → 真投递验证 → 闭环（不转人工）。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "session_key": session_key,
                "message": "工作区 ws-1001 的 slack 机器人从昨天开始不回复消息了",
                "user_ext_id": "u-42",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["skill"] == "integration"
        assert body["cause_code"] == "TOKEN_EXPIRED"
        assert body["confidence"] >= 0.9
        assert body["verify_ok"] is True
        assert body["escalate"] is False
        assert body["outcome"] == "resolved"
        assert any(a["action"] == "refresh_token" for a in body["actions"])
        assert body["node_path"] == [
            "route", "observe", "diagnose", "act", "verify", "finalize",
        ]

    @pytest.mark.integration
    def test_webhook_invalid_repaired(self, api, headers, session_key):
        """回调地址失效 → 修复 → 验证。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1002 的机器人收不到消息，回调好像有问题"},
        )
        body = r.json()
        assert body["skill"] == "integration"
        assert body["cause_code"] == "WEBHOOK_INVALID"
        assert any(a["action"] == "repair_webhook" for a in body["actions"])
        assert body["verify_ok"] is True

    @pytest.mark.integration
    def test_scope_revoked_requires_reauth(self, api, headers, session_key):
        """权限被移除 → 无法自动修复 → 给出重授权链接并转人工。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1003 的机器人发消息提示权限不足"},
        )
        body = r.json()
        assert body["skill"] == "integration"
        assert body["cause_code"] == "SCOPE_REVOKED"
        assert body["decision"] == "need_reauth"
        assert body["escalate"] is True
        assert "重新授权" in body["reply"] or "authorize" in body["reply"]
        assert any(a["action"] == "reauth_link" for a in body["actions"])

    @pytest.mark.integration
    def test_healthy_workspace_no_action(self, api, headers, session_key):
        """健康工作区 → 判定无需处置。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1004 的机器人好像没反应"},
        )
        body = r.json()
        assert body["cause_code"] == "HEALTHY"
        assert body["decision"] == "none"
        assert body["verify_ok"] is True

    @pytest.mark.integration
    def test_clarify_when_workspace_missing(self, api, headers, session_key):
        """缺少工作区标识 → 挂起并追问（不瞎猜）。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "我的机器人不回复消息了"},
        )
        body = r.json()
        assert body["skill"] == "integration"
        assert body["pending_clarify"] is not None, "应挂起并追问"
        assert "workspace_id" in body["pending_clarify"].get("missing", [])
        assert body["pending_clarify"].get("question")


class TestSkillAccount:
    """技能 5：账号安全异常排查。"""

    @pytest.mark.integration
    def test_high_risk_locked_and_verified(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "账号 u-42 凌晨在境外登录了，怀疑被盗"},
        )
        body = r.json()
        assert body["skill"] == "account"
        assert body["cause_code"] == "ACCOUNT_COMPROMISE_SUSPECTED"
        assert body["risk_score"] >= 60
        actions = {a["action"] for a in body["actions"]}
        assert {"lock_account", "notify"} <= actions
        assert body["verify_ok"] is True

    @pytest.mark.integration
    def test_medium_risk_requires_mfa_not_lock(self, api, headers, session_key):
        """中风险：通知 + 要求二次验证，但不锁定。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "账号 u-43 凌晨登录提醒，帮我看下是否安全"},
        )
        body = r.json()
        assert body["skill"] == "account"
        actions = {a["action"] for a in body["actions"]}
        assert "notify" in actions
        assert "lock_account" not in actions, "中风险不应直接锁定账号"

    @pytest.mark.integration
    def test_low_risk_no_action(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "账号 u-44 说收到异常登录提醒，帮我确认"},
        )
        body = r.json()
        assert body["skill"] == "account"
        assert body["cause_code"] == "NO_ANOMALY"
        assert body["decision"] == "none"


class TestSkillFaq:
    """技能 1：FAQ 咨询。"""

    @pytest.mark.integration
    def test_faq_answers_from_knowledge_base(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "怎么设置消息免打扰？"},
        )
        body = r.json()
        assert body["skill"] == "faq"
        assert body["reply"].strip()
        assert body["outcome"] in {"resolved", "escalated"}

    @pytest.mark.integration
    def test_billing_rule_question_routed_to_faq(self, api, headers, session_key):
        """规则类提问（非争议）应走 FAQ 而不是发起退款流程。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "用量超出套餐额度会怎么计费？"},
        )
        body = r.json()
        assert body["skill"] == "faq"
        assert body["escalate"] is False


class TestSkillPermission:
    """技能 3：权限访问诊断。"""

    @pytest.mark.integration
    def test_share_not_enabled_gives_options_and_verifies(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "文档 doc-771 打不开，提示没有访问权限"},
        )
        body = r.json()
        assert body["skill"] == "permission"
        assert body["cause_code"] == "SHARE_NOT_ENABLED"
        assert len(body["options"]) >= 2, "涉及数据安全必须给多方案"
        assert any(o.get("recommended") for o in body["options"])
        assert body["verify_ok"] is True

    @pytest.mark.integration
    def test_org_policy_blocked_needs_approval(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "文档 doc-772 外链分享给客户了，对方打不开"},
        )
        body = r.json()
        assert body["skill"] == "permission"
        assert body["cause_code"] == "ORG_POLICY_BLOCKED"
        assert any(a["action"] == "policy_exception" for a in body["actions"])
        assert body["verify_ok"] is False, "审批未完成不应算通过"
        assert body["outcome"] == "pending_approval"

    @pytest.mark.integration
    def test_version_conflict_unlocked(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "文档 doc-773 打开提示版本冲突"},
        )
        body = r.json()
        assert body["skill"] == "permission"
        assert body["cause_code"] == "VERSION_CONFLICT"
        assert any(a["action"].startswith("unlock") for a in body["actions"])


class TestSkillBilling:
    """技能 4：订阅计费争议。"""

    @pytest.mark.integration
    def test_large_dispute_requires_manual_approval(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "这个月账单重复计费了，账号 acct-501，我要退款"},
        )
        body = r.json()
        assert body["skill"] == "billing"
        assert body["cause_code"] == "BILLING_DISPUTE_CONFIRMED"
        assert body["risk_score"] > 0
        assert body["decision"] == "manual_approval"
        assert body["escalate"] is True
        assert any(a["action"] == "manual_approval" for a in body["actions"])

    @pytest.mark.integration
    def test_small_dispute_auto_adjusted(self, api, headers, session_key):
        """小额 + 老客户口径：自动调整闭环。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "账号 acct-502 的用量超限费用 42 元不对，申请退款"},
        )
        body = r.json()
        assert body["skill"] == "billing"
        assert body["decision"] in {"auto_adjust", "auto_adjust_audit"}
        assert body["verify_ok"] is True

    @pytest.mark.integration
    def test_no_dispute_recognized(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "帮我核对一下账号 acct-504 本月账单金额"},
        )
        body = r.json()
        assert body["skill"] == "billing"
        assert body["cause_code"] == "NO_DISPUTE"
        assert body["escalate"] is False


class TestEscalationHandover:
    """转人工交接内容完整性。"""

    @pytest.mark.integration
    def test_handover_snapshot_present_when_escalated(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "账号 acct-503 账单有问题，已经投诉很多次了"},
        )
        body = r.json()
        assert body["escalate"] is True
        esc = body["escalation"]
        assert esc is not None
        assert esc["request_no"].startswith("ESC-")
        assert esc["ticket_no"].startswith("TK-")
        snapshot = esc["snapshot"]
        assert len(snapshot) > 50, "交接快照不能为空"
        for section in ("交接快照", "已排查项", "已执行操作", "建议话术"):
            assert section in snapshot, f"快照缺少「{section}」小节"
