"""单元测试：风控评分表与阈值表（技能 4）、风险信号表（技能 5）。

这两张表是"可评审的规则"，因此测试必须能断言：
  - 每个维度的打分符合表定义
  - 阈值区间连续且覆盖 0-100
  - 硬性规则能压过总分
  - 风险信号累加符合预期
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.mocks.data import account_profile
from app.skills.account import RISK_LEVELS, RISK_SIGNALS
from app.skills.billing import (
    DECISION_THRESHOLDS,
    RISK_DIMENSIONS,
    BillingSkill,
    _match_cmp,
)


@pytest.fixture(scope="module")
def skill() -> BillingSkill:
    return BillingSkill()


def _decide(score: float, facts: dict) -> str:
    """按阈值表 + 硬性规则得出动作（与技能实现保持一致）。"""
    action = "manual_approval"
    for band in DECISION_THRESHOLDS:
        if score <= band["max_score"]:
            action = band["action"]
            break
    if facts["amount"] > settings.escalate_amount_threshold:
        action = "manual_approval"
    if facts["open_complaints"] > settings.escalate_complaint_threshold:
        action = "manual_approval"
    return action


class TestRiskTableStructure:
    """评分表与阈值表的结构完整性。"""

    @pytest.mark.unit
    def test_five_dimensions_defined(self):
        assert len(RISK_DIMENSIONS) == 5
        names = {d["name"] for d in RISK_DIMENSIONS}
        assert names == {"金额", "投诉次数", "历史记录", "用户等级", "订阅时长"}

    @pytest.mark.unit
    def test_each_dimension_has_bounded_score(self):
        for dim in RISK_DIMENSIONS:
            assert dim["rules"], f"{dim['name']} 没有规则"
            for rule in dim["rules"]:
                assert 0 <= rule["score"] <= dim["max_score"], (
                    f"{dim['name']} 的规则分值 {rule['score']} 超出上限 {dim['max_score']}"
                )

    @pytest.mark.unit
    def test_total_max_score_is_100(self):
        assert sum(d["max_score"] for d in RISK_DIMENSIONS) == 100

    @pytest.mark.unit
    def test_rules_within_dimension_are_mutually_exclusive(self):
        """同一维度的规则必须互斥。

        回归测试：早期规则写成"累计上界"（`amount<=50 / <=100 / <=500 / >500`），
        一支 10 元争议会同时命中四条、取最后一条得到 35 分（最高档），严重失真。
        这里用代表性取值验证每个取值最多命中一条规则。
        """
        samples = {
            "amount": [0.0, 10.0, 50.0, 50.01, 100.0, 100.01, 500.0, 500.01, 5000.0],
            "open_complaints": [0, 1, 2, 3, 10],
            "history_adjustments": [0, 1, 2, 3, 9],
            "user_tier": ["basic", "silver", "gold", "platinum"],
            "tenure_months": [0, 1, 2, 3, 11, 12, 26, 120],
        }
        for dim in RISK_DIMENSIONS:
            values = {k: 0 for k in samples}
            values["user_tier"] = "basic"
            for sample in samples[dim["field"]]:
                values[dim["field"]] = sample
                # 规则以数据化谓词表达（字段/比较符/阈值），不再用可执行字符串，
                # 因此这里直接用被测代码的判定函数，避免测试自己再实现一套比较逻辑。
                hits = [rule for rule in dim["rules"] if _match_cmp(rule["cmp"], values)]
                assert len(hits) == 1, (
                    f"{dim['name']} 在取值 {sample!r} 命中 {len(hits)} 条规则，规则必须互斥"
                )

    @pytest.mark.unit
    def test_amount_dimension_matches_table(self):
        """金额档位取值应与表定义一致（防止取最后一条式的失真）。"""
        skill = BillingSkill()
        base = {
            "open_complaints": 0,
            "history_adjustments": 0,
            "user_tier": "basic",
            "tenure_months": 24,
        }
        expectations = [(10.0, 0), (80.0, 12), (300.0, 25), (5000.0, 35)]
        for amount, expected in expectations:
            score, detail = skill._score({**base, "amount": amount})
            amount_dim = next(d for d in detail if d["dimension"] == "金额")
            assert amount_dim["score"] == expected, f"金额 {amount} 应得 {expected} 分"
            assert score == expected, "其余维度均为 0 分，总分应等于金额维度得分"

    @pytest.mark.unit
    def test_thresholds_cover_full_range(self):
        upper_bounds = [b["max_score"] for b in DECISION_THRESHOLDS]
        assert upper_bounds == sorted(upper_bounds), "阈值区间必须递增"
        assert upper_bounds[-1] == 100, "阈值表必须覆盖到 100"

    @pytest.mark.unit
    def test_thresholds_have_actions(self):
        for band in DECISION_THRESHOLDS:
            assert band["action"] in {"auto_adjust", "auto_adjust_audit", "manual_approval"}


class TestRiskScoring:
    """打分正确性。"""

    @pytest.mark.unit
    def test_low_amount_low_complaint_scores_low(self, skill):
        facts = {
            "amount": 10.0,
            "open_complaints": 0,
            "history_adjustments": 0,
            "user_tier": "basic",
            "tenure_months": 24,
        }
        score, detail = skill._score(facts)
        assert score == 0
        assert len(detail) == 5

    @pytest.mark.unit
    def test_high_amount_scores_max_amount_dimension(self, skill):
        facts = {
            "amount": 5000.0,
            "open_complaints": 0,
            "history_adjustments": 0,
            "user_tier": "basic",
            "tenure_months": 24,
        }
        score, detail = skill._score(facts)
        amount_dim = next(d for d in detail if d["dimension"] == "金额")
        assert amount_dim["score"] == 35

    @pytest.mark.unit
    def test_new_customer_scores_higher_than_veteran(self, skill):
        base = {
            "amount": 80.0,
            "open_complaints": 0,
            "history_adjustments": 0,
            "user_tier": "basic",
        }
        new_score, _ = skill._score({**base, "tenure_months": 1})
        old_score, _ = skill._score({**base, "tenure_months": 30})
        assert new_score > old_score, "新客户应从严格（得分更高）"

    @pytest.mark.unit
    def test_platinum_tier_scores_higher_than_basic(self, skill):
        base = {
            "amount": 80.0,
            "open_complaints": 0,
            "history_adjustments": 0,
            "tenure_months": 24,
        }
        platinum, _ = skill._score({**base, "user_tier": "platinum"})
        basic, _ = skill._score({**base, "user_tier": "basic"})
        assert platinum > basic, "高价值客户应倾向人工确认（得分更高）"

    @pytest.mark.unit
    def test_score_never_exceeds_100(self, skill):
        facts = {
            "amount": 99999.0,
            "open_complaints": 99,
            "history_adjustments": 99,
            "user_tier": "platinum",
            "tenure_months": 0,
        }
        score, _ = skill._score(facts)
        assert score == 100.0


class TestHardRules:
    """硬性规则压过总分。"""

    @pytest.mark.unit
    def test_amount_above_threshold_forces_manual(self, skill):
        p = account_profile("acct-501")
        facts = {
            "amount": p["amount"],
            "open_complaints": p["open_complaints"],
            "history_adjustments": 0,
            "user_tier": p["user_tier"],
            "tenure_months": p["tenure_months"],
        }
        score, _ = skill._score(facts)
        assert _decide(score, facts) == "manual_approval"

    @pytest.mark.unit
    def test_complaints_above_threshold_forces_manual(self, skill):
        p = account_profile("acct-503")
        facts = {
            "amount": p["amount"],
            "open_complaints": p["open_complaints"],
            "history_adjustments": 0,
            "user_tier": p["user_tier"],
            "tenure_months": p["tenure_months"],
        }
        score, _ = skill._score(facts)
        assert _decide(score, facts) == "manual_approval"

    @pytest.mark.unit
    def test_small_dispute_can_be_automated(self, skill):
        """小额且老客户 → 可自动处理（不必人工审批）。"""
        facts = {
            "amount": 30.0,
            "open_complaints": 0,
            "history_adjustments": 0,
            "user_tier": "basic",
            "tenure_months": 30,
        }
        score, _ = skill._score(facts)
        assert _decide(score, facts) in {"auto_adjust", "auto_adjust_audit"}


class TestAccountRiskSignals:
    """账号安全风险信号表。"""

    @pytest.mark.unit
    def test_signal_table_structure(self):
        assert RISK_SIGNALS
        for sig in RISK_SIGNALS:
            assert sig["code"] and sig["label"]
            assert 0 < sig["score"] <= 100

    @pytest.mark.unit
    def test_risk_levels_cover_full_range(self):
        upper = [b["max_score"] for b in RISK_LEVELS]
        assert upper == sorted(upper)
        assert upper[-1] == 100

    @pytest.mark.unit
    def test_high_risk_signals_reach_high_level(self):
        """异地 + 异常时段 + 新设备 应落入高风险档。"""
        codes = {"new_geo", "odd_hour", "new_device"}
        score = sum(s["score"] for s in RISK_SIGNALS if s["code"] in codes)
        high_threshold = next(b for b in RISK_LEVELS if b["level"] == "high")
        medium_upper = max(b["max_score"] for b in RISK_LEVELS if b["level"] != "high")
        assert score > medium_upper, f"三信号合计 {score} 应超过中风险上限 {medium_upper}"
        assert high_threshold["action"] == "lock_and_notify"
