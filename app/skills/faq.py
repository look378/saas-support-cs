"""技能 1：FAQ 功能咨询（L1 问答，跑量场景）。

与其它四个技能的区别：它不调用外部系统做处置，只做**向量检索 + 生成回答**。
但同样具备降级能力——检索为空或分数过低时走规则兜底话术并标记低置信度
（容错矩阵中的"RAG 空检索"一行）。
"""

from __future__ import annotations

from app.agents.graph.state import SkillOutcome, SupportState
from app.skills.base import BaseSkill


class FaqSkill(BaseSkill):
    """FAQ 功能咨询。"""

    code = "faq"
    name = "FAQ 功能咨询"
    description = "解答产品功能、使用方法、计费规则、账号设置等常见问题"
    required_entities = ()

    def observe(self, state: SupportState) -> SkillOutcome:
        """向量检索 FAQ 与案例。"""
        from app.services.knowledge_service import KnowledgeService

        query = state.get("user_message", "")
        svc = KnowledgeService()
        hits = svc.search(query, top_k=4)
        facts = {
            "hit_count": len(hits),
            "top_score": hits[0].score if hits else 0.0,
            "snippets": [h.document for h in hits],
            "matched": [h.metadata.get("doc_id", "") for h in hits],
            "matched_cases": [h.metadata.get("case_id", "") for h in hits if h.metadata.get("case_id")],
        }
        return SkillOutcome(
            facts=facts,
            evidence=[f"[{h.score:.2f}] {h.document[:60]}" for h in hits],
            matched_cases=facts["matched_cases"],
        )

    def diagnose(self, state: SupportState) -> SkillOutcome:
        """FAQ 场景不做故障诊断，只判断检索是否可用。"""
        facts = state.get("facts") or {}
        if facts.get("hit_count", 0) == 0:
            return SkillOutcome(
                cause_code="NO_KNOWLEDGE",
                cause_text="知识库中未检索到相关内容",
                confidence=0.2,
                decision="fallback",
                decision_reason="检索为空，改用兜底话术并标记低置信度",
            )
        return SkillOutcome(
            cause_code="KNOWLEDGE_FOUND",
            cause_text="知识库命中相关内容",
            confidence=min(0.95, float(facts.get("top_score", 0.0)) + 0.3),
            decision="answer",
            decision_reason="基于知识库回答",
        )

    def act(self, state: SupportState) -> SkillOutcome:
        """生成回答（有知识库内容则据此作答，否则兜底）。"""
        facts = state.get("facts") or {}
        snippets = facts.get("snippets") or []
        question = state.get("user_message", "")

        if not snippets:
            # 检索空检索降级：规则兜底话术 + 低置信度标记
            return SkillOutcome(
                degraded=True,
                degrade_reason="知识库检索为空",
                decision="fallback",
                reply=(
                    "抱歉，我暂时没有查到与您问题完全匹配的说明。\n"
                    "建议您：\n"
                    "1. 换一种说法描述（例如带上具体功能名称）\n"
                    "2. 或回复「转人工」，我为您接通人工客服\n"
                    "3. 也可拨打支持热线 400-888-9999"
                ),
            )

        context = "\n\n".join(snippets[:3])
        try:
            from app.core.llm_client import get_llm_client

            llm = get_llm_client()
            prompt = (
                "你是 SaaS 产品「云枢协作平台」的客服助手小汇。\n"
                "请只依据下方资料回答用户问题；资料中没有的信息不要编造。\n"
                "回答控制在 3 句话以内，涉及步骤时用编号列出。\n\n"
                f"## 资料\n{context}\n\n## 用户问题\n{question}\n\n## 回答\n"
            )
            reply = llm.invoke(prompt, kind="faq_answer").strip()
            return SkillOutcome(decision="answer", reply=reply, outcome="resolved")
        except Exception as exc:  # noqa: BLE001
            # 模型不可用 → 直接给出检索到的资料原文（仍然有用）
            from app.core.logging_config import warn_degraded

            warn_degraded("faq.llm_unavailable", error=str(exc)[:200])
            return SkillOutcome(
                degraded=True,
                degrade_reason="模型不可用，改用知识库原文作答",
                decision="fallback",
                reply=(
                    "我为您找到以下相关说明：\n\n"
                    + "\n\n".join(f"- {s[:200]}" for s in snippets[:2])
                    + "\n\n如需更详细的帮助，请回复「转人工」。"
                ),
                outcome="resolved",
            )

    def verify(self, state: SupportState) -> SkillOutcome:
        """问答类无需验证。"""
        return SkillOutcome(verify_ok=bool(state.get("reply")), verify_detail="问答无需验证")
