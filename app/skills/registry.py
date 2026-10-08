"""技能注册表与路由。

路由策略（**关键词优先 + 模型兜底**）：
  1. 关键词/模式打分：确定性、零成本、可测试，覆盖绝大多数入口表达
  2. 若最高分与其他候选接近（或全部为 0），才调用模型做一次归类
  3. 仍无定论 → 触发澄清追问（而不是瞎猜一个技能）

这样做的好处：常用路径不消耗模型调用、行为可复现；模型只在真正模糊时介入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging_config import get_logger
from app.skills.account import AccountSkill
from app.skills.base import BaseSkill
from app.skills.billing import BillingSkill
from app.skills.faq import FaqSkill
from app.skills.integration import IntegrationSkill
from app.skills.permission import PermissionSkill

log = get_logger(__name__)


# 技能关键词表：命中即加权。权重体现判别强度。
SKILL_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "integration": [
        ("机器人", 6), ("不回", 5), ("不回复", 6), ("集成", 5), ("webhook", 6), ("回调", 5),
        ("令牌", 6), ("token", 5), ("slack", 4), ("钉钉", 4), ("飞书", 4), ("授权", 3),
        ("机器人失效", 8), ("发不出", 5), ("消息发不出", 7), ("工作区", 4),
    ],
    "permission": [
        ("打不开", 6), ("没权限", 7), ("没有权限", 7), ("权限", 5), ("文档", 5),
        ("分享", 6), ("共享", 5), ("访问不了", 6), ("看不了", 5), ("版本冲突", 7),
        ("协作者", 4), ("外链", 4),
    ],
    # 计费技能承接的是**争议/退款**诉求。
    # 泛化的计费词（用量/订阅/套餐/计费）权重刻意压低，避免把
    # "用量超出会怎么计费？"这类**规则咨询**误判成争议（应由 FAQ 承接）。
    "billing": [
        ("账单", 7), ("退款", 7), ("扣费", 7), ("重复计费", 10),
        ("争议", 6), ("发票", 6), ("多收", 7), ("退钱", 7), ("不对", 4),
        ("计费", 3), ("用量", 2), ("订阅", 3), ("套餐", 2), ("费用", 2), ("超限", 3),
    ],
    "account": [
        ("异常登录", 9), ("异地登录", 9), ("被盗", 9), ("盗用", 8), ("登录记录", 6),
        ("安全", 4), ("二次验证", 6), ("mfa", 4), ("账号安全", 8), ("陌生设备", 7),
        ("凌晨", 4), ("锁定", 5),
    ],
    "faq": [
        ("怎么", 3), ("如何", 3), ("是什么", 3), ("能不能", 2), ("支持", 2),
        ("功能介绍", 4), ("怎么用", 4), ("使用方法", 4), ("在哪里", 3), ("设置", 3),
        ("有什么区别", 5), ("规则", 3), ("可以吗", 3), ("多久", 3),
    ],
}

# "询问方式"标记：出现这些词通常是在**咨询规则/用法**，而不是在**主张权益**。
# 命中时压低"争议/处置"类技能（billing）的得分，把流量让给 FAQ。
#
# 只压低 billing 而不动 permission/account：后两者的关键词（打不开/文档/异地登录）
# 本身就很具体，误伤风险低；而 billing 的通用词（用量/套餐/费用）极易出现在
# 规则咨询里，是主要的误判来源。
INQUIRY_MARKERS: tuple[str, ...] = (
    "怎么", "如何", "是什么", "有什么区别", "可以吗", "能不能", "支持吗",
    "规则", "流程", "会不会", "多久", "在哪里",
)
DISPUTE_LIKE_SKILLS: tuple[str, ...] = ("billing",)
INQUIRY_FAQ_BONUS = 2


@dataclass
class RouteResult:
    """路由结果。"""

    skill: str
    confidence: float
    reason: str
    scores: dict[str, int] = field(default_factory=dict)
    need_clarify: bool = False


class SkillRegistry:
    """技能注册表。"""

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {
            "faq": FaqSkill(),
            "integration": IntegrationSkill(),
            "permission": PermissionSkill(),
            "billing": BillingSkill(),
            "account": AccountSkill(),
        }

    def get(self, code: str) -> BaseSkill | None:
        return self._skills.get(code)

    def all(self) -> dict[str, BaseSkill]:
        return dict(self._skills)

    # ------------------------------------------------------------------
    def score(self, message: str) -> dict[str, int]:
        """关键词打分。

        额外一步：识别"询问式表达"并抑制争议类技能的得分。
        例如"用量超出套餐额度会怎么计费？"含多个计费词，但它是在**咨询规则**，
        不是主张权益；若不抑制就会被路由到计费争议技能并走退款流程（本项目实测踩到）。
        """
        text = (message or "").lower()
        scores: dict[str, int] = {}
        for code, kws in SKILL_KEYWORDS.items():
            total = 0
            for kw, weight in kws:
                if kw.lower() in text:
                    total += weight
            scores[code] = total

        # 询问式表达 → 压低"争议/处置"类技能，并小幅提升 FAQ
        if any(marker in text for marker in INQUIRY_MARKERS):
            for code in DISPUTE_LIKE_SKILLS:
                if code in scores:
                    scores[code] = int(scores[code] * 0.4)
            scores["faq"] = scores.get("faq", 0) + INQUIRY_FAQ_BONUS

        return scores

    def route(self, message: str, entities: dict[str, Any] | None = None) -> RouteResult:
        """路由到技能。

        实体是强信号：能抽出 ``workspace_id`` 基本就是集成问题，``doc_id`` 是权限问题，
        因此实体命中会显著提升对应技能的置信度。
        """
        entities = entities or {}
        scores = self.score(message)

        # 实体加成
        if entities.get("workspace_id"):
            scores["integration"] = scores.get("integration", 0) + 10
        if entities.get("doc_id"):
            scores["permission"] = scores.get("permission", 0) + 10
        if entities.get("account_id"):
            scores["billing"] = scores.get("billing", 0) + 10

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top_code, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0

        if top_score == 0:
            # 全部为 0：交给模型归类，仍不确定则澄清
            llm_pick = self._llm_route(message)
            if llm_pick:
                return RouteResult(
                    skill=llm_pick,
                    confidence=0.55,
                    reason="关键词无命中，由模型归类",
                    scores=scores,
                )
            return RouteResult(
                skill="faq",
                confidence=0.3,
                reason="无法确定技能，按通用问答处理并准备澄清",
                scores=scores,
                need_clarify=True,
            )

        # 领先明显 → 直接采纳
        if top_score - second_score >= 4 or top_score >= 8:
            confidence = min(0.95, 0.55 + top_score / 40.0)
            return RouteResult(
                skill=top_code,
                confidence=confidence,
                reason=f"关键词命中（得分 {top_score}，次高 {second_score}）",
                scores=scores,
            )

        # 接近 → 先把"该不该让模型复判"这件事想清楚。
        # 若最高分是 FAQ 且与次高差距很小，直接采纳 FAQ：
        # FAQ 是**只读的信息类技能**，误判代价低；而误判到 billing/account/permission
        # 会触发退款、锁定账号、变更文档权限等**不可逆副作用**。
        # 因此模糊时默认走信息类，而不是把决定权交给一次随机性较强的模型归类。
        if top_code == "faq" and top_score - second_score <= 4:
            return RouteResult(
                skill="faq",
                confidence=0.6,
                reason=f"信息类优先：FAQ 得分 {top_score} 与次高 {second_score} 接近，按只读问答处理",
                scores=scores,
            )

        # 其余接近情形 → 让模型复判
        llm_pick = self._llm_route(message, candidates=[c for c, _ in ranked[:3]])
        if llm_pick:
            return RouteResult(
                skill=llm_pick,
                confidence=0.62,
                reason=f"关键词接近（{top_score} vs {second_score}），模型复判为 {llm_pick}",
                scores=scores,
            )
        return RouteResult(
            skill=top_code,
            confidence=0.45,
            reason=f"关键词略占优（{top_score} vs {second_score}），置信度偏低",
            scores=scores,
            need_clarify=True,
        )

    # ------------------------------------------------------------------
    def _llm_route(self, message: str, candidates: list[str] | None = None) -> str | None:
        """用模型做一次技能归类。失败返回 None（由调用方走澄清）。"""
        pool = candidates or list(self._skills.keys())
        try:
            from app.core.llm_client import get_llm_client

            catalog = "\n".join(f"- {c}: {self._skills[c].name} —— {self._skills[c].description}" for c in pool)
            prompt = (
                "你是客服工单分类器。请判断用户消息属于哪一类支持场景，"
                "只输出类别代号，不要输出其它内容。\n\n"
                f"可选类别：\n{catalog}\n\n"
                f"用户消息：{message}\n\n类别代号："
            )
            raw = get_llm_client().invoke(prompt, kind="skill_route", temperature=0.0)
            text = (raw or "").strip().lower()
            for code in pool:
                if code in text:
                    return code
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("skill.route.llm_failed", error=str(exc)[:200])
            return None

    # ------------------------------------------------------------------
    def describe(self) -> list[dict[str, str]]:
        """技能清单（用于工作台展示与文档）。"""
        return [
            {"code": s.code, "name": s.name, "description": s.description}
            for s in self._skills.values()
        ]


_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """获取技能注册表单例。"""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def reset_registry() -> None:
    """重建技能注册表。

    技能实例持有独立的工具客户端，因此注册表是有状态的。测试中若对某个技能
    打了桩（例如注入异常模式的客户端），必须重建注册表，否则打桩会泄漏到
    后续用例（本项目实测踩到：一次降级测试的异常客户端污染了后面所有集成用例）。
    """
    global _registry
    _registry = None
