"""技能 5：账号安全异常排查（**做实**，不是预留）。

诊断链：异常登录检测 → 风险评分 → 处置（锁定/通知/强制改密/转人工）→ 复核。

风险评分同样用显式信号表（``RISK_SIGNALS``），而不是把阈值散落在代码里，
便于评审与调整，也让测试可以断言"哪些信号命中、得分多少"。
"""

from __future__ import annotations

from typing import Any

from app.agents.graph.state import SkillOutcome, SupportState
from app.skills.base import BaseSkill
from app.tools import business as T

# ===========================================================================
# 风险信号表
# ===========================================================================
RISK_SIGNALS: list[dict[str, Any]] = [
    {"code": "new_geo", "label": "异地登录（非常用地区）", "score": 35},
    {"code": "odd_hour", "label": "异常时段登录（凌晨 0-6 点）", "score": 25},
    {"code": "new_device", "label": "新设备登录", "score": 20},
    {"code": "no_mfa", "label": "未启用二次验证", "score": 15},
    {"code": "success_login", "label": "异常登录成功（非仅尝试）", "score": 10},
]

# 风险等级阈值
RISK_LEVELS: list[dict[str, Any]] = [
    {"max_score": 24, "level": "low", "action": "notify_only", "note": "仅通知，不打断使用"},
    {"max_score": 59, "level": "medium", "action": "notify_require_mfa", "note": "通知并要求启用二次验证"},
    {"max_score": 100, "level": "high", "action": "lock_and_notify", "note": "锁定账号并通知，转人工跟进"},
]


class AccountSkill(BaseSkill):
    """账号安全异常排查。"""

    code = "account"
    name = "账号安全异常排查"
    description = "处理异常登录、账号被盗、异地登录告警、二次验证等账号安全事件"
    required_entities = ("user_id",)

    def entity_patterns(self) -> list[tuple[str, str]]:
        return [
            (r"\b(u-\d{2,})\b", "user_id"),
            (r"账号[是为：:\s]*([A-Za-z0-9_\-]+)", "user_id"),
        ]

    def clarify_question(self, missing: list[str]) -> str:
        if "user_id" in missing:
            return "请提供需要排查的账号标识（例如 u-42）。"
        return super().clarify_question(missing)

    # ------------------------------------------------------------------
    def observe(self, state: SupportState) -> SkillOutcome:
        entities = dict(state.get("entities") or {})
        if state.get("user_reply"):
            entities.update(self.extract(state["user_reply"], entities))
        user_id = entities.get("user_id", "")

        ok, res = self.safe(self.call_tool, T.query_login_events, user_id, 7)
        if not ok:
            return SkillOutcome(
                facts={"query_failed": True, "error_kind": res.kind},
                degraded=True,
                degrade_reason=f"查询登录记录失败({res.kind})",
                reply="抱歉，暂时无法读取该账号的登录记录，我已为您转接人工客服核查。",
                escalate=True,
                escalate_reason="工具调用失败",
            )

        latest = res.events[0] if res.events else None
        facts = {
            "user_id": user_id,
            "event_count": len(res.events),
            "latest": latest.model_dump() if latest else {},
            "baseline": res.baseline,
            "anomaly_score_hint": res.anomaly_score_hint,
        }
        evidence = []
        if latest:
            evidence = [
                f"最近一次登录：{latest.at}",
                f"来源地区 {latest.geo} / IP {latest.ip} / 设备 {latest.device}",
                f"命中风险标记：{latest.risk_flags or '无'}",
                f"常用地区={res.baseline.get('usual_geo')}，常用设备={res.baseline.get('usual_devices')}",
            ]
        return SkillOutcome(facts=facts, evidence=evidence, extra={"entities": entities})

    # ------------------------------------------------------------------
    def diagnose(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        if facts.get("query_failed"):
            return SkillOutcome(cause_code="QUERY_FAILED", cause_text="无法读取登录记录", confidence=0.0)

        latest = facts.get("latest") or {}
        flags = list(latest.get("risk_flags") or [])
        signals: list[dict[str, Any]] = []

        score = 0.0
        for sig in RISK_SIGNALS:
            hit = False
            if sig["code"] in flags or sig["code"] == "no_mfa" and not latest.get("mfa", True):
                hit = True
            elif sig["code"] == "success_login" and latest.get("result") == "success":
                # 只有在已存在其它异常信号时，"登录成功"才作为加权项
                hit = any(f in flags for f in ("new_geo", "odd_hour", "new_device"))
            if hit:
                score += sig["score"]
                signals.append({"code": sig["code"], "label": sig["label"], "score": sig["score"]})

        score = min(100.0, score)

        level_cfg = RISK_LEVELS[-1]
        for band in RISK_LEVELS:
            if score <= band["max_score"]:
                level_cfg = band
                break

        if score == 0:
            return SkillOutcome(
                cause_code="NO_ANOMALY",
                cause_text="未发现异常登录迹象",
                confidence=0.9,
                risk_score=0.0,
                decision="none",
                decision_reason="无风险信号",
                evidence=list(state.get("evidence") or []) + ["各项行为与基线一致"],
            )

        cause_map = {
            "low": ("SUSPICIOUS_LOW", "存在轻微异常，暂不构成安全事件"),
            "medium": ("SUSPICIOUS_MEDIUM", "检测到可疑登录行为，建议加强验证"),
            "high": ("ACCOUNT_COMPROMISE_SUSPECTED", "高度疑似账号被盗用"),
        }
        cause, text = cause_map[level_cfg["level"]]

        evidence = list(state.get("evidence") or []) + [
            f"风险评分 {score:.0f}/100，命中信号：" + "；".join(f"{s['label']}(+{s['score']})" for s in signals)
        ]
        return SkillOutcome(
            cause_code=cause,
            cause_text=text,
            confidence=0.85 if level_cfg["level"] == "high" else 0.7,
            risk_score=score,
            evidence=evidence,
            decision=level_cfg["action"],
            decision_reason=f"风险分 {score:.0f} 落入 {level_cfg['level']} 区间",
            extra={"signals": signals, "risk_level": level_cfg["level"]},
        )

    # ------------------------------------------------------------------
    def act(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        user_id = facts.get("user_id", "")
        decision = state.get("decision", "")
        score = float(state.get("risk_score", 0.0))
        actions: list[dict[str, Any]] = []

        if decision == "none":
            return SkillOutcome(
                decision="none",
                decision_reason="无风险",
                verify_ok=True,
                verify_detail="无需处置",
                reply="我核对了该账号最近 7 天的登录记录，没有发现异常登录迹象，可以放心使用。",
                outcome="resolved",
            )

        if decision in ("lock_and_notify", "notify_require_mfa", "notify_only"):
            # 通知（所有等级都做）
            ok_n, res_n = self.safe(
                self.call_tool, T.notify_user, user_id, "email", "security_alert", {"risk_score": score}
            )
            actions.append(
                {
                    "action": "notify",
                    "ok": ok_n,
                    "detail": f"已发送安全提醒（{res_n.channel}）" if ok_n else "通知发送失败",
                }
            )

            if decision in ("lock_and_notify", "notify_require_mfa"):
                # 只需要成败与话术，第二个返回值（原始响应）在本分支用不到
                ok_r, _res_r = self.safe(self.call_tool, T.require_password_reset, user_id)
                actions.append(
                    {
                        "action": "require_password_reset",
                        "ok": ok_r,
                        "detail": "已要求下次登录重置口令" if ok_r else "设置失败",
                    }
                )

            if decision == "lock_and_notify":
                ok_l, res_l = self.safe(
                    self.call_tool, T.lock_account, user_id, f"检测到疑似盗用（风险分 {score:.0f}）", "temporary", 120
                )
                actions.append(
                    {
                        "action": "lock_account",
                        "ok": ok_l,
                        "detail": f"已临时锁定并吊销 {res_l.sessions_revoked} 个会话" if ok_l else "锁定失败",
                    }
                )
                return SkillOutcome(
                    actions=actions,
                    decision=decision,
                    decision_reason=state.get("decision_reason", ""),
                    escalate=True,
                    escalate_reason="疑似账号被盗用，需人工跟进确认身份",
                )

            return SkillOutcome(
                actions=actions,
                decision=decision,
                decision_reason=state.get("decision_reason", ""),
            )

        return SkillOutcome(decision="escalate", decision_reason="未知处置动作", escalate=True, escalate_reason="安全事件处置异常")

    # ------------------------------------------------------------------
    def verify(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        decision = state.get("decision", "")

        if decision == "none":
            return SkillOutcome(verify_ok=True, verify_detail="无需验证")

        ok, res = self.safe(self.call_tool, T.verify_account_security, facts.get("user_id", ""))
        if not ok:
            return SkillOutcome(
                verify_ok=False,
                verify_detail=f"复核失败({getattr(res, 'code', '')})",
                escalate=True,
                escalate_reason="安全处置复核失败",
            )

        actions_txt = "；".join(a.get("detail", "") for a in (state.get("actions") or []))
        if res.safe:
            reply = (
                "安全排查与处置已完成：\n"
                f"1. 诊断结论：{(state.get('cause_text') or '')}\n"
                f"2. 已执行：{actions_txt}\n"
                f"3. 复核结果：活跃会话 {res.sessions_active} 个，二次验证已启用，当前风险分 {res.risk_score:.2f}，账号已恢复安全。\n"
            )
            if decision == "lock_and_notify":
                reply += "\n由于涉及疑似盗用，我已同时创建工单由人工专员跟进，会主动联系您核实身份。"
            else:
                reply += "\n建议您尽快修改密码并检查已授权的第三方应用。"
            return SkillOutcome(
                verify_ok=True,
                verify_detail=f"复核通过：活跃会话={res.sessions_active} 风险分={res.risk_score:.2f}",
                outcome="resolved",
                reply=reply,
            )

        return SkillOutcome(
            verify_ok=False,
            verify_detail="复核显示仍存在风险",
            escalate=True,
            escalate_reason="安全处置后仍存在风险",
        )
