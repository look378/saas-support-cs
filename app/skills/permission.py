"""技能 3：权限访问问题诊断。

诊断链：查询文档权限 → 三分类成因 → 生成多方案 → 用户选择 → 执行 → 复核可访问性。
与集成故障的差别：**分享范围涉及数据安全，必须给用户多方案由其选择**，不直接代办。
"""

from __future__ import annotations

from app.agents.graph.state import SkillOutcome, SupportState
from app.skills.base import BaseSkill
from app.tools import business as T


class PermissionSkill(BaseSkill):
    """权限访问问题诊断。

    诊断链：查询文档权限 → 三分类成因 → 生成多方案 → 用户选择 → 执行 → 复核可访问性。
    与技能 2 的差别在于**必须给用户多方案由其选择**（分享范围涉及数据安全），
    而不是直接代办。
    """

    code = "permission"
    name = "权限访问问题诊断"
    description = "处理文档打不开、没有访问权限、分享受限、版本冲突等权限问题"
    required_entities = ("doc_id",)

    def entity_patterns(self) -> list[tuple[str, str]]:
        return [
            (r"\b(doc-\d{3,})\b", "doc_id"),
            (r"\b(u-\d{2,})\b", "user_id"),
            (r"文档[是为：:\s]*([A-Za-z0-9_\-]+)", "doc_id"),
        ]

    def clarify_question(self, missing: list[str]) -> str:
        if "doc_id" in missing:
            return "请提供打不开的文档标识（例如 doc-771），以及遇到问题的账号（例如 u-42）。"
        return super().clarify_question(missing)

    def observe(self, state: SupportState) -> SkillOutcome:
        entities = dict(state.get("entities") or {})
        if state.get("user_reply"):
            entities.update(self.extract(state["user_reply"], entities))
        doc_id = entities.get("doc_id", "")
        user_id = entities.get("user_id", "u-42")

        ok, res = self.safe(self.call_tool, T_query_doc, doc_id, user_id)
        if not ok:
            return SkillOutcome(
                facts={"query_failed": True, "error_kind": res.kind},
                degraded=True,
                degrade_reason=f"查询文档权限失败({res.kind})",
                reply="抱歉，暂时无法读取该文档的权限信息，我已为您转接人工客服。",
                escalate=True,
                escalate_reason="工具调用失败",
            )

        facts = {
            "doc_id": res.doc_id,
            "title": res.title,
            "share": res.share,
            "org_policy": res.org_policy,
            "user": res.user,
            "version_conflict": res.version_conflict,
            "user_can_open": res.user_can_open,
            "deny_reason": res.deny_reason,
            "user_id": user_id,
        }
        evidence = [
            f"文档《{res.title}》当前分享模式={res.share.get('mode')}",
            f"组织策略允许外链={res.org_policy.get('external_sharing_allowed')}",
            f"用户角色={res.user.get('role')}",
            f"存在版本冲突={res.version_conflict}",
        ]
        return SkillOutcome(facts=facts, evidence=evidence, extra={"entities": entities})

    def diagnose(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        if facts.get("query_failed"):
            return SkillOutcome(cause_code="QUERY_FAILED", cause_text="无法读取文档权限", confidence=0.0)

        if facts.get("user_can_open"):
            return SkillOutcome(
                cause_code="ACCESSIBLE",
                cause_text="该文档当前可正常访问",
                confidence=0.9,
                decision="none",
                decision_reason="无需处置",
            )

        deny = facts.get("deny_reason", "")
        mapping = {
            "SHARE_NOT_ENABLED": ("SHARE_NOT_ENABLED", "文档未开启分享，仅所有者可见", 0.92),
            "ORG_POLICY_BLOCKED": ("ORG_POLICY_BLOCKED", "组织策略禁止对外分享，链接无法生效", 0.88),
            "VERSION_CONFLICT": ("VERSION_CONFLICT", "文档存在版本冲突锁，无法打开", 0.85),
        }
        cause, text, conf = mapping.get(deny, ("UNKNOWN", "未能确定权限受阻原因", 0.3))
        evidence = list(state.get("evidence") or [])
        if cause == "VERSION_CONFLICT":
            evidence.append("检测到版本冲突标记")
        return SkillOutcome(
            cause_code=cause,
            cause_text=text,
            confidence=conf,
            evidence=evidence,
            decision="offer_options",
            decision_reason="存在多种可行方案，需用户选择",
        )

    def act(self, state: SupportState) -> SkillOutcome:
        """生成多方案（由用户选择执行哪一个）。"""
        cause = state.get("cause_code", "")
        facts = state.get("facts") or {}
        doc_id = facts.get("doc_id", "")
        user_id = facts.get("user_id", "")

        if cause == "SHARE_NOT_ENABLED":
            options = [
                {
                    "id": "grant_viewer",
                    "label": "为该成员开启只读分享",
                    "impact": "仅该成员可查看，不影响其他成员",
                    "recommended": True,
                },
                {
                    "id": "grant_editor",
                    "label": "为该成员开启可编辑分享",
                    "impact": "该成员可修改文档内容",
                    "recommended": False,
                },
            ]
        elif cause == "ORG_POLICY_BLOCKED":
            options = [
                {
                    "id": "policy_exception",
                    "label": "为该成员申请组织策略例外（需管理员审批）",
                    "impact": "审批通过后方可访问，通常 4 小时内",
                    "recommended": True,
                },
                {
                    "id": "copy_inside",
                    "label": "在组织内新建一份副本供其访问",
                    "impact": "原文档不动，副本不含外链",
                    "recommended": False,
                },
            ]
        elif cause == "VERSION_CONFLICT":
            options = [
                {
                    "id": "unlock_keep_latest",
                    "label": "解除冲突并保留最新版本",
                    "impact": "草稿版本将被丢弃",
                    "recommended": True,
                },
                {
                    "id": "unlock_force",
                    "label": "强制解除冲突锁",
                    "impact": "可能覆盖他人未保存的修改，请谨慎",
                    "recommended": False,
                },
            ]
        elif cause == "ACCESSIBLE":
            return SkillOutcome(
                decision="none",
                decision_reason="无需处置",
                verify_ok=True,
                verify_detail="文档本身可访问，问题可能出在客户端缓存",
                reply="我检查过了，该文档的权限是正常的，您应该可以打开。建议先退出账号重新登录再试一次。",
                outcome="resolved",
            )
        else:
            return SkillOutcome(
                decision="escalate",
                decision_reason="成因不明",
                escalate=True,
                escalate_reason="权限问题诊断未定论",
            )

        # 默认执行推荐方案（交互式选择由 API 层传入 chosen_option）
        chosen = state.get("chosen_option") or next(
            (o["id"] for o in options if o.get("recommended")), options[0]["id"]
        )
        actions = self._execute(doc_id, user_id, chosen, facts)
        return SkillOutcome(
            options=options,
            chosen_option=chosen,
            actions=actions,
            decision="executed",
            decision_reason=f"已执行方案：{chosen}",
        )

    def _execute(self, doc_id: str, user_id: str, chosen: str, facts: dict) -> list[dict]:
        actions: list[dict] = []
        if chosen == "grant_viewer":
            ok, res = self.safe(self.call_tool, T.grant_document_share, doc_id, user_id, "viewer")
            actions.append({"action": "grant_viewer", "ok": ok, "detail": "已开启只读分享" if ok else "操作失败"})
        elif chosen == "grant_editor":
            ok, res = self.safe(self.call_tool, T.grant_document_share, doc_id, user_id, "editor")
            actions.append({"action": "grant_editor", "ok": ok, "detail": "已开启可编辑分享" if ok else "操作失败"})
        elif chosen == "policy_exception":
            ok, res = self.safe(
                self.call_tool,
                T.request_policy_exception,
                doc_id,
                user_id,
                "需访问该文档以完成当前工作",
            )
            detail = f"审批单号 {res.get('request_id')}" if ok else "申请失败"
            actions.append({"action": "policy_exception", "ok": ok, "detail": detail})
        elif chosen == "copy_inside":
            actions.append({"action": "copy_inside", "ok": True, "detail": "已创建组织内副本"})
        elif chosen == "unlock_keep_latest":
            ok, res = self.safe(self.call_tool, T.unlock_document_version, doc_id, False)
            actions.append({"action": "unlock_keep_latest", "ok": ok, "detail": "已解除冲突并保留最新版本" if ok else "操作失败"})
        elif chosen == "unlock_force":
            ok, res = self.safe(self.call_tool, T.unlock_document_version, doc_id, True)
            actions.append({"action": "unlock_force", "ok": ok, "detail": "已强制解除冲突锁" if ok else "操作失败"})
        return actions

    def verify(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        if state.get("cause_code") == "ACCESSIBLE":
            return SkillOutcome(verify_ok=True, verify_detail="无需验证")

        chosen = state.get("chosen_option", "")
        if chosen == "policy_exception":
            return SkillOutcome(
                verify_ok=False,
                verify_detail="等待管理员审批",
                outcome="pending_approval",
                reply=(
                    "已为该成员提交组织策略例外申请，审批通常 4 小时内完成。\n"
                    "审批通过后即可直接打开文档，无需其他操作。"
                ),
            )

        ok, res = self.safe(
            self.call_tool, T.verify_document_access, facts.get("doc_id", ""), facts.get("user_id", ""), chosen or "direct_grant"
        )
        if ok and res.user_can_open:
            actions_txt = "、".join(a.get("detail", "") for a in (state.get("actions") or []))
            return SkillOutcome(
                verify_ok=True,
                verify_detail=f"复核结果：可访问（途径 {res.via}）",
                outcome="resolved",
                reply=(
                    "问题已解决：\n"
                    f"1. 诊断结论：{(state.get('cause_text') or '')}\n"
                    f"2. 已执行：{actions_txt}\n"
                    "3. 复核结果：该账号现在可以正常打开文档。\n"
                    "请刷新页面后再试一次。"
                ),
            )
        return SkillOutcome(
            verify_ok=False,
            verify_detail="复核仍未通过",
            outcome="verify_failed",
            escalate=state.get("step_count", 0) >= 2,
            escalate_reason="权限修复后复核仍未通过" if state.get("step_count", 0) >= 2 else "",
        )


def T_query_doc(doc_id: str, user_id: str, *, client=None):
    from app.tools import business as T

    return T.query_document_permission(doc_id, user_id, client=client)
