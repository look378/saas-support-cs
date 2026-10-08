"""单元测试：技能路由与实体抽取。

覆盖点：
  - 五类技能的关键词路由准确性
  - 实体抽取（工作区/文档/账号/用户）
  - 必填项缺失时触发澄清
  - 实体加成对路由的影响（抽到 workspace_id 应显著偏向集成技能）
"""

from __future__ import annotations

import pytest

from app.skills.registry import get_registry


@pytest.fixture(scope="module")
def registry():
    return get_registry()


class TestSkillRegistry:
    """技能注册表。"""

    def test_five_skills_registered(self, registry):
        codes = {s["code"] for s in registry.describe()}
        assert codes == {"faq", "integration", "permission", "billing", "account"}

    def test_every_skill_has_metadata(self, registry):
        for skill in registry.all().values():
            assert skill.code, "技能缺少 code"
            assert skill.name, "技能缺少 name"
            assert skill.description, "技能缺少 description"


class TestRouting:
    """关键词路由。"""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("工作区 ws-1001 的 slack 机器人从昨天开始不回复消息了", "integration"),
            ("文档 doc-771 打不开，提示没有访问权限", "permission"),
            ("这个月账单重复计费了，账号 acct-501，我要退款", "billing"),
            ("账号 u-42 凌晨在境外登录了，怀疑被盗", "account"),
            ("怎么设置消息免打扰？", "faq"),
        ],
    )
    def test_route_matches_expected_skill(self, registry, message, expected):
        result = registry.route(message)
        assert result.skill == expected, f"{message!r} → {result.skill}（期望 {expected}）"

    def test_route_confidence_high_when_keywords_decisive(self, registry):
        result = registry.route("工作区 ws-1001 的 slack 机器人不回复消息")
        assert result.confidence >= 0.7

    def test_entity_bonus_biases_routing(self, registry):
        """仅凭工作区标识也应路由到集成技能（实体是强信号）。"""
        result = registry.route("ws-1002 有问题", entities={"workspace_id": "ws-1002"})
        assert result.skill == "integration"

    def test_unknown_message_does_not_crash(self, registry):
        result = registry.route("今天天气怎么样")
        assert result.skill in {"faq", "integration", "permission", "billing", "account"}


class TestEntityExtraction:
    """实体抽取。"""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("skill_code", "message", "key", "expected"),
        [
            ("integration", "工作区 ws-1002 的 slack 机器人不回复", "workspace_id", "ws-1002"),
            ("integration", "ws-1001 出问题了", "workspace_id", "ws-1001"),
            ("permission", "doc-772 打不开，账号 u-43", "doc_id", "doc-772"),
            ("billing", "账号 acct-502 账单有问题", "account_id", "acct-502"),
            ("account", "u-44 疑似异地登录", "user_id", "u-44"),
        ],
    )
    def test_extract_entity(self, registry, skill_code, message, key, expected):
        skill = registry.get(skill_code)
        found = skill.extract(message)
        assert found.get(key) == expected

    def test_missing_required_entity_detected(self, registry):
        skill = registry.get("integration")
        entities = skill.extract("机器人不回消息了")
        assert "workspace_id" in skill.missing_entities(entities)

    def test_required_entity_satisfied(self, registry):
        skill = registry.get("integration")
        entities = skill.extract("工作区 ws-1001 机器人不回消息")
        assert skill.missing_entities(entities) == []

    def test_clarify_question_mentions_missing_field(self, registry):
        skill = registry.get("integration")
        question = skill.clarify_question(["workspace_id"])
        assert "工作区" in question


class TestSkillOutcomeMerge:
    """技能结果合并（空值不覆盖）。"""

    def test_empty_values_do_not_override(self):
        from app.agents.graph.state import SkillOutcome, merge

        delta = merge(SkillOutcome(outcome="", cause_code="", reply=""))
        assert "outcome" not in delta
        assert "cause_code" not in delta

    def test_non_empty_values_are_carried(self):
        from app.agents.graph.state import SkillOutcome, merge

        delta = merge(SkillOutcome(outcome="resolved", cause_code="TOKEN_EXPIRED", confidence=0.9))
        assert delta["outcome"] == "resolved"
        assert delta["cause_code"] == "TOKEN_EXPIRED"
        assert delta["confidence"] == 0.9

    def test_clear_escalation_flag(self):
        from app.agents.graph.state import SkillOutcome, merge

        delta = merge(SkillOutcome(verify_ok=True, clear_escalation=True))
        assert delta["escalate"] is False
        assert delta["escalate_reasons"] == []

    def test_escalate_reason_collected(self):
        from app.agents.graph.state import SkillOutcome, merge

        delta = merge(SkillOutcome(escalate=True, escalate_reason="需人工审批"))
        assert delta["escalate"] is True
        assert "需人工审批" in delta["escalate_reasons"]
