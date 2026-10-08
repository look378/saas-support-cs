"""风险规则表与 Mock 状态机回归测试（本次全面审查发现）。

覆盖两点：

1. **风险规则不得使用可执行字符串**。原实现把规则写成 ``"50 < amount <= 100"``
   并交给 ``eval`` 求值：既是注入面（规则一旦来自配置即变成远程代码执行），
   又让"规则写错"表现为静默跳过（风险分悄悄变小 → 该转人工的自动退款了）。
   现在规则是数据化谓词，写错必须**报错**。
2. **Mock 的复核接口必须以"是否真的处置过"为准**。原实现
   ``safe = prof["risk"] in ("HIGH","MEDIUM")`` 把静态画像当成处置结果，
   于是"什么都没做"也能通过复核，验证环节形同虚设。
"""

from __future__ import annotations

import pytest

from app.mocks import data as mock_data
from app.skills.billing import HARD_RULES, RISK_DIMENSIONS, BillingSkill, _match_cmp


@pytest.mark.unit
class TestRiskRulesAreDataNotCode:
    def test_no_executable_strings_in_risk_rules(self):
        """规则表里不得残留可执行表达式字段。"""
        for dim in RISK_DIMENSIONS:
            for rule in dim["rules"]:
                assert "when" not in rule, f"{dim['name']} 仍有可执行字符串规则"
                assert "cmp" in rule, f"{dim['name']} 缺少数据化判据"
                for _, op, _ in rule["cmp"]:
                    assert op in ("<", "<=", "==", "!=", ">=", ">"), f"非法比较符 {op}"

    def test_hard_rules_reference_config_not_literals(self):
        """硬性规则阈值必须指向配置项，而不是写死数字。"""
        for rule in HARD_RULES:
            assert rule["threshold_setting"], f"{rule['name']} 未指向配置项"

    def test_unknown_field_raises(self):
        """引用不存在的字段必须报错，不能静默判为不命中。"""
        with pytest.raises(KeyError):
            _match_cmp([("no_such_field", ">", 1)], {"amount": 5.0})

    def test_unsupported_operator_raises(self):
        with pytest.raises(ValueError):
            _match_cmp([("amount", "~=", 1)], {"amount": 5.0})

    def test_ten_yuan_dispute_scores_zero(self):
        """回归：小额争议曾因"累计上界"式规则被判为最高风险档（35 分）。"""
        skill = BillingSkill()
        score, detail = skill._score(
            {
                "amount": 10.0,
                "open_complaints": 0,
                "history_adjustments": 0,
                "user_tier": "basic",
                "tenure_months": 24,
            }
        )
        assert score == 0, f"10 元争议不应有风险加分，实际 {score}"
        assert all(d["score"] == 0 for d in detail)


@pytest.mark.unit
class TestAccountVerifyRequiresRealRemediation:
    """复核必须依赖真实处置动作，不得由静态画像推断。"""

    def test_unremediated_high_risk_profile_is_unsafe(self):
        mock_data.reset_account_state("u-42")
        prof = mock_data.user_profile("u-42")
        assert prof["risk"] == "HIGH", "夹具画像应为高风险，否则该用例失去意义"
        state = mock_data.verify_account_state("u-42")
        assert state["safe"] is False, "未做任何处置的高风险账号不得被判为安全"
        assert state["sessions_active"] > 0

    def test_remediated_profile_becomes_safe(self):
        mock_data.reset_account_state("u-42")
        mock_data.mark_account_remediated("u-42", "lock_account")
        state = mock_data.verify_account_state("u-42")
        assert state["safe"] is True
        assert state["sessions_active"] == 0

    def test_low_risk_profile_is_safe_without_action(self):
        """低风险账号本来就无需处置，应为安全态。"""
        low_user = next(
            uid for uid, p in mock_data.USER_PROFILES.items() if p["risk"] == "LOW"
        )
        mock_data.reset_account_state(low_user)
        assert mock_data.verify_account_state(low_user)["safe"] is True
