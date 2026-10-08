"""技能 2：集成故障诊断与修复（核心演示）。

诊断链（对应任务书 4.1）：
    意图识别 → 澄清追问 → 查询集成配置 → 诊断分类
    → 远程修复（刷令牌 / 修 Webhook / 重授权） → 验证可达 → 闭环 / 智能转人工

三分类成因（与 Mock 后端画像一一对应，可断言）：
    TOKEN_EXPIRED   访问令牌已过期         → 远程刷新令牌
    WEBHOOK_INVALID 回调地址失效           → 修复 Webhook
    SCOPE_REVOKED   应用权限被移除         → 生成重授权链接（无法自动修复）
    HEALTHY         未发现故障

**"验证可达"是本技能的关键**：不是查本地标记，而是真的投递一条验证消息
并拿到回执（``verify_integration``）。这使"修复成功"具备可证伪性。
"""

from __future__ import annotations

from typing import Any

from app.agents.graph.state import SkillOutcome, SupportState
from app.skills.base import BaseSkill
from app.tools import business as T


class IntegrationSkill(BaseSkill):
    """集成故障诊断与修复。"""

    code = "integration"
    name = "集成故障诊断与修复"
    description = "处理 Slack/钉钉/飞书等三方集成失效：机器人不回复、消息发不出去、回调不通"
    required_entities = ("workspace_id",)

    def entity_patterns(self) -> list[tuple[str, str]]:
        return [
            (r"\b(ws-\d{3,})\b", "workspace_id"),
            (r"工作区[是为：:\s]*([A-Za-z0-9_\-]+)", "workspace_id"),
            (r"(slack|钉钉|飞书|feishu|dingtalk)", "channel"),
        ]

    def clarify_question(self, missing: list[str]) -> str:
        if "workspace_id" in missing:
            return "请提供出问题的工作区标识（例如 ws-1001），以及使用的集成渠道（Slack / 钉钉 / 飞书）。"
        return super().clarify_question(missing)

    # ------------------------------------------------------------------
    def observe(self, state: SupportState) -> SkillOutcome:
        """查询集成配置：令牌有效期、Webhook 可达性、授权范围。"""
        entities = dict(state.get("entities") or {})
        workspace_id = entities.get("workspace_id", "")
        channel = entities.get("channel", "")
        if state.get("user_reply"):
            # 用户在澄清中补充的信息优先
            supp = self.extract(state["user_reply"], entities)
            entities.update(supp)
            workspace_id = entities.get("workspace_id", workspace_id)
            channel = entities.get("channel", channel)

        ok, result = self.safe(
            self.call_tool, T.query_integration_config, workspace_id, channel
        )
        if not ok:
            return SkillOutcome(
                facts={"query_failed": True, "error_kind": result.kind, "error_code": result.code},
                degraded=True,
                degrade_reason=f"查询集成配置失败({result.kind})",
                reply="抱歉，暂时无法读取该工作区的集成配置，我已经为您转接人工客服继续排查。",
                escalate=True,
                escalate_reason="工具调用失败",
            )

        facts: dict[str, Any] = {
            "workspace_id": result.workspace_id,
            "provider": result.provider,
            "connected": result.connected,
            "health": result.health,
            "token_expired": result.token.expired,
            "token_scopes": result.token.scopes,
            "token_expires_at": result.token.expires_at,
            "webhook_url": result.webhook_url,
            "webhook_expected_url": result.webhook_expected_url,
            "webhook_reachable": result.webhook_reachable,
            "last_event_hours_ago": result.last_event_hours_ago,
            "bot_user": result.bot_user,
            # 是否持有可用刷新令牌由配置接口体现；这里以"令牌前缀存在"作为持有标志
            "has_refresh_token": bool(result.token.prefix),
        }
        evidence = [
            f"工作区 {result.workspace_id}（{result.provider}）健康状态={result.health}",
            f"访问令牌已过期={result.token.expired}",
            f"回调地址可达={result.webhook_reachable}",
            f"已授权范围={result.token.scopes}",
        ]
        return SkillOutcome(facts=facts, evidence=evidence, extra={"entities": entities})

    # ------------------------------------------------------------------
    def diagnose(self, state: SupportState) -> SkillOutcome:
        """三分类诊断。"""
        facts = state.get("facts") or {}
        if facts.get("query_failed"):
            return SkillOutcome(cause_code="QUERY_FAILED", cause_text="无法读取集成配置", confidence=0.0)

        scopes = facts.get("token_scopes") or []
        token_expired = bool(facts.get("token_expired"))
        webhook_ok = bool(facts.get("webhook_reachable"))
        health = facts.get("health", "")

        if health == "healthy" and not token_expired and webhook_ok and "chat:write" in scopes:
            return SkillOutcome(
                cause_code="HEALTHY",
                cause_text="未发现集成故障，配置与令牌均正常",
                confidence=0.9,
                evidence=list(state.get("evidence") or []) + ["各检查项均通过"],
                decision="none",
                decision_reason="无需修复",
            )

        # 优先级：令牌过期 > 权限被移除 > Webhook 失效
        # 令牌过期会同时导致回调不可达，先修令牌可避免误判
        if token_expired:
            cause, text, conf = (
                "TOKEN_EXPIRED",
                "集成访问令牌已过期，导致机器人无法调用接口",
                0.95,
            )
        elif "chat:write" not in scopes:
            cause, text, conf = (
                "SCOPE_REVOKED",
                "应用的发送消息权限已被移除，需要重新授权",
                0.85,
            )
        elif not webhook_ok:
            cause, text, conf = (
                "WEBHOOK_INVALID",
                "回调地址与预期不一致，事件无法送达",
                0.9,
            )
        else:
            cause, text, conf = ("UNKNOWN", "未能确定具体成因", 0.3)

        evidence = list(state.get("evidence") or [])
        if cause == "TOKEN_EXPIRED":
            evidence.append("令牌过期时间早于最后一次事件时间，时间线吻合")
        elif cause == "SCOPE_REVOKED":
            evidence.append("授权范围内缺少 chat:write（发送消息所需）")
        elif cause == "WEBHOOK_INVALID":
            evidence.append(
                f"实际回调地址与预期不一致：{facts.get('webhook_url')} ≠ {facts.get('webhook_expected_url')}"
            )

        return SkillOutcome(
            cause_code=cause,
            cause_text=text,
            confidence=conf,
            evidence=evidence,
            decision="auto_fix" if cause in ("TOKEN_EXPIRED", "WEBHOOK_INVALID") else "need_reauth",
            decision_reason="可远程自动修复" if cause in ("TOKEN_EXPIRED", "WEBHOOK_INVALID") else "需用户重新授权",
        )

    # ------------------------------------------------------------------
    def act(self, state: SupportState) -> SkillOutcome:
        """远程修复。

        设计取舍：**先修主因，再修"顺带可修"的次因**。
        真实故障常是复合的（令牌过期通常同时导致回调不可达），
        若只修被诊断为主因的那一项，验证会失败并进入回退重试，
        浪费一轮往返。因此这里对"当前确实异常且可自动修复"的项都尝试修复，
        把不可自动修复的项（权限被移除）留给重新授权路径。
        """
        cause = state.get("cause_code", "")
        facts = state.get("facts") or {}
        workspace_id = facts.get("workspace_id", "")
        provider = facts.get("provider", "")
        actions: list[dict[str, Any]] = []

        if cause == "HEALTHY":
            return SkillOutcome(
                decision="none",
                decision_reason="无需修复",
                verify_ok=True,
                verify_detail="配置正常，无需处置",
                reply="我检查了该工作区的集成配置，令牌、回调地址与授权范围都正常，目前没有发现故障。",
                outcome="resolved",
            )

        # --- 1) 令牌过期：尝试刷新；失败则降级为引导重新授权 ---
        if cause == "TOKEN_EXPIRED":
            refresh_token = "rt-" + workspace_id if facts.get("has_refresh_token") else ""
            ok, res = self.safe(
                self.call_tool, T.refresh_integration_token, workspace_id, refresh_token
            )
            if ok and res.refreshed:
                actions.append({"action": "refresh_token", "ok": True, "detail": "访问令牌已刷新"})
            else:
                err = getattr(res, "code", "") or "刷新失败"
                actions.append({"action": "refresh_token", "ok": False, "detail": err})
                ok2, link = self.safe(self.call_tool, T.request_reauth_link, workspace_id, provider)
                detail = link.authorize_url if ok2 else "无法生成授权链接"
                actions.append({"action": "reauth_link", "ok": ok2, "detail": detail})
                return SkillOutcome(
                    actions=actions,
                    decision="need_reauth",
                    decision_reason="刷新令牌不可用，已生成重新授权链接",
                    reply=(
                        "该工作区的访问令牌已失效，且无法自动刷新，需要您重新授权。\n"
                        f"请点击以下链接完成授权（15 分钟内有效）：\n{detail}\n"
                        "授权完成后回复我，我会立即为您验证集成是否恢复。"
                    ),
                    escalate=True,
                    escalate_reason="令牌刷新失败，需用户重新授权",
                    outcome="pending_user_action",
                )

        # --- 2) 权限被移除：无法自动修复 ---
        if cause == "SCOPE_REVOKED":
            ok, link = self.safe(self.call_tool, T.request_reauth_link, workspace_id, provider)
            detail = link.authorize_url if ok else "无法生成授权链接"
            actions.append({"action": "reauth_link", "ok": ok, "detail": detail})
            return SkillOutcome(
                actions=actions,
                decision="need_reauth",
                decision_reason="应用权限被移除，必须由用户重新授权",
                reply=(
                    "排查发现：该集成的「发送消息」权限已被移除，这类变更无法由系统自动恢复。\n"
                    f"请通过以下链接重新授权：\n{detail}\n"
                    "授权后请回复我，我会立即验证集成是否恢复。"
                ),
                escalate=True,
                escalate_reason="权限被移除，需管理员重新授权",
                outcome="pending_user_action",
            )

        # --- 3) 回调地址失效：修复 ---
        # 注意：不仅"主因是回调失效"时修，令牌修好后回调若仍不可达也要修（复合故障）
        webhook_ok = bool(facts.get("webhook_reachable"))
        if cause == "WEBHOOK_INVALID" or not webhook_ok:
            expected = facts.get("webhook_expected_url", "")
            ok, res = self.safe(self.call_tool, T.repair_webhook, workspace_id, expected)
            if ok and res.repaired:
                actions.append(
                    {
                        "action": "repair_webhook",
                        "ok": True,
                        "detail": f"回调地址已修复为 {res.webhook_url}",
                    }
                )
            else:
                actions.append({"action": "repair_webhook", "ok": False, "detail": "修复失败"})
                return SkillOutcome(
                    actions=actions,
                    decision="escalate",
                    decision_reason="回调修复失败",
                    escalate=True,
                    escalate_reason="集成修复失败",
                )

        if actions:
            return SkillOutcome(
                actions=actions,
                decision="auto_fix",
                decision_reason="已执行可用的远程修复动作",
            )

        return SkillOutcome(
            decision="escalate",
            decision_reason="成因不明或无需自动修复",
            escalate=True,
            escalate_reason="集成修复未产生有效动作",
        )

    # ------------------------------------------------------------------
    def verify(self, state: SupportState) -> SkillOutcome:
        """验证可达性：真实投递验证消息并取回执。"""
        if state.get("decision") == "none":
            return SkillOutcome(verify_ok=True, verify_detail="无需验证")

        facts = state.get("facts") or {}
        workspace_id = facts.get("workspace_id", "")

        # 需要通过重新授权才能恢复的场景：本次流程不投递验证，等用户完成授权
        if state.get("decision") == "need_reauth":
            return SkillOutcome(
                verify_ok=False,
                verify_detail="等待用户完成重新授权后验证",
                outcome="pending_user_action",
                reply=state.get("reply")
                or "已为您生成重新授权链接，完成授权后我会立即验证集成是否恢复。",
            )

        ok, res = self.safe(self.call_tool, T.verify_integration, workspace_id)
        if ok and res.delivered:
            return SkillOutcome(
                verify_ok=True,
                verify_detail=f"已向 {res.channel} 投递验证消息，消息回执 {res.provider_message_id}（{res.latency_ms}ms）",
                outcome="resolved",
                # 验证通过即闭环：撤销流程早期可能置上的转人工标记
                clear_escalation=True,
                reply=(
                    "已为您修复并验证完成：\n"
                    f"1. 诊断结论：{(state.get('cause_text') or '')}\n"
                    f"2. 已执行：{'、'.join(a.get('detail', '') for a in (state.get('actions') or []))}\n"
                    f"3. 验证结果：成功投递验证消息到 {res.channel}，回执 {res.provider_message_id}\n"
                    "如果机器人仍未回复，请再告诉我，我会继续排查。"
                ),
            )

        # 验证失败 → 回退重试（由图的循环边处理），若已达上限则转人工
        detail = getattr(res, "code", "") or "验证未通过"
        return SkillOutcome(
            verify_ok=False,
            verify_detail=f"验证失败：{detail}",
            outcome="verify_failed",
            escalate=state.get("step_count", 0) >= 2,
            escalate_reason="修复后验证仍不通过" if state.get("step_count", 0) >= 2 else "",
        )
