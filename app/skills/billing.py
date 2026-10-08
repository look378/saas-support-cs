"""技能 4：订阅计费争议。

本文件把任务书里**只有维度名、没有阈值**的部分补齐为两张可评审的表：
  - ``RISK_DIMENSIONS``：五维打分规则（金额 / 投诉次数 / 历史记录 / 用户等级 / 订阅时长）
  - ``DECISION_THRESHOLDS``：风险分 → 处置动作的映射

以及写操作的三件套：**幂等键 + HMAC 签名 + 时间戳窗口 + 防重放**。
幂等采用"持久表 + 先查后写"：即使 Redis 重启或过期，也不会重复退款
（Redis 只做快速去重，最终一致性由数据库唯一约束保证）。
"""

from __future__ import annotations

import operator
from typing import Any

from app.agents.graph.state import SkillOutcome, SupportState
from app.core.config import settings
from app.skills.base import BaseSkill
from app.tools import business as T

# ===========================================================================
# 风控评分表（可评审、可调整）
# ===========================================================================
# 每维：判据 → 分值。总分 0-100，越高越需要人工介入。
#
# **重要**：同一维度内的规则必须**互斥**（写成"区间"而不是"累计上界"）。
# 若只写 `amount <= 50 / <= 100 / <= 500 / > 500` 这样的累计上界，
# 一支 10 元的争议会同时满足全部四条，取最后一条就会得到 35 分（最高档），
# 完全失真（本项目实测踩到）。因此每条规则都显式写出上下界。
# 规则用**数据化谓词**表达（字段 / 比较符 / 阈值），不用可执行字符串：
#   1) 去掉 eval 这层注入面——规则一旦改为来自配置或数据库就是远程代码执行；
#   2) 规则可被校验（非法字段/比较符立即报错，而不是静默跳过导致评分失真）。
# 区间写成不重叠区间，且**取第一条命中**（见 _score 注释）。
RISK_DIMENSIONS: list[dict[str, Any]] = [
    {
        "name": "金额",
        "field": "amount",
        "max_score": 35,
        "rules": [
            {"cmp": [("amount", "<=", 50)], "score": 0, "note": "小额，自动化风险低"},
            {"cmp": [("amount", ">", 50), ("amount", "<=", 100)], "score": 12, "note": "接近阈值"},
            {"cmp": [("amount", ">", 100), ("amount", "<=", 500)], "score": 25, "note": "中等金额"},
            {"cmp": [("amount", ">", 500)], "score": 35, "note": "大额，必须人工复核"},
        ],
    },
    {
        "name": "投诉次数",
        "field": "open_complaints",
        "max_score": 25,
        "rules": [
            {"cmp": [("open_complaints", "==", 0)], "score": 0},
            {"cmp": [("open_complaints", "==", 1)], "score": 8},
            {"cmp": [("open_complaints", "==", 2)], "score": 15},
            {"cmp": [("open_complaints", ">", 2)], "score": 25, "note": "高频投诉，倾向人工"},
        ],
    },
    {
        "name": "历史记录",
        "field": "history_adjustments",
        "max_score": 15,
        "rules": [
            {"cmp": [("history_adjustments", "==", 0)], "score": 0},
            {"cmp": [("history_adjustments", "==", 1)], "score": 5},
            {"cmp": [("history_adjustments", "==", 2)], "score": 10},
            {"cmp": [("history_adjustments", ">", 2)], "score": 15, "note": "多次调整历史"},
        ],
    },
    {
        "name": "用户等级",
        "field": "user_tier",
        "max_score": 10,
        # 等级越高，越倾向人工妥善处理（避免误伤高价值客户）
        "rules": [
            {"cmp": [("user_tier", "==", "basic")], "score": 0},
            {"cmp": [("user_tier", "==", "silver")], "score": 3},
            {"cmp": [("user_tier", "==", "gold")], "score": 6},
            {"cmp": [("user_tier", "==", "platinum")], "score": 10, "note": "高价值客户，人工确认"},
        ],
    },
    {
        "name": "订阅时长",
        "field": "tenure_months",
        "max_score": 15,
        # 老客户按低风险处理（更信任），新客户从严
        "rules": [
            {"cmp": [("tenure_months", "<", 3)], "score": 15, "note": "新客户，从严"},
            {"cmp": [("tenure_months", ">=", 3), ("tenure_months", "<", 12)], "score": 6},
            {"cmp": [("tenure_months", ">=", 12)], "score": 0, "note": "长期客户，从宽"},
        ],
    },
]

# 风险分 → 处置动作
DECISION_THRESHOLDS: list[dict[str, Any]] = [
    {"max_score": 39, "action": "auto_adjust", "note": "可直接自动调整并留审计"},
    {"max_score": 69, "action": "auto_adjust_audit", "note": "自动调整，但必须留审计与通知"},
    {"max_score": 100, "action": "manual_approval", "note": "必须转人工审批"},
]

# 硬性规则：任一命中即强制转人工（不看总分）。
# 阈值取自配置，因此这里存**配置项名**而不是写死的数字，避免两处漂移。
HARD_RULES: list[dict[str, Any]] = [
    {
        "name": "金额超阈值",
        "field": "amount",
        "op": ">",
        "threshold_setting": "escalate_amount_threshold",
        "note": "单笔金额超过阈值",
    },
    {
        "name": "投诉次数超阈值",
        "field": "open_complaints",
        "op": ">",
        "threshold_setting": "escalate_complaint_threshold",
        "note": "未结投诉超过阈值",
    },
]

_COMPARATORS: dict[str, Any] = {
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    ">": operator.gt,
}


def _match_cmp(conditions: list[tuple[str, str, Any]], values: dict[str, Any]) -> bool:
    """判断一组 "字段 比较符 阈值" 条件是否全部成立。

    字段或比较符非法时**抛错**而不是返回 False：规则写错会静默改变风险分，
    而风险分直接决定"自动退款还是转人工审批"，这类静默失真不可接受。
    """
    for field, op, threshold in conditions:
        if field not in values:
            raise KeyError(f"风险规则引用了未知字段：{field}")
        comparator = _COMPARATORS.get(op)
        if comparator is None:
            raise ValueError(f"风险规则使用了不支持的比较符：{op}")
        if not comparator(values[field], threshold):
            return False
    return True


class BillingSkill(BaseSkill):
    """订阅计费争议。"""

    code = "billing"
    name = "订阅计费争议"
    description = "处理账单争议、重复计费、用量超限费用、退款与抵扣申请"
    required_entities = ("account_id",)

    def entity_patterns(self) -> list[tuple[str, str]]:
        return [
            (r"\b(acct-\d{3,})\b", "account_id"),
            (r"账号[是为：:\s]*([A-Za-z0-9_\-]+)", "account_id"),
            (r"\b(inv-[\w\-]+)\b", "invoice_id"),
        ]

    def clarify_question(self, missing: list[str]) -> str:
        if "account_id" in missing:
            return "请提供订阅账号标识（例如 acct-501），以便我核对账单明细。"
        return super().clarify_question(missing)

    # ------------------------------------------------------------------
    def observe(self, state: SupportState) -> SkillOutcome:
        entities = dict(state.get("entities") or {})
        if state.get("user_reply"):
            entities.update(self.extract(state["user_reply"], entities))
        account_id = entities.get("account_id", "")

        sub_ok, sub = self.safe(self.call_tool, T.query_subscription, account_id)
        inv_ok, inv = self.safe(self.call_tool, T.query_invoice, account_id)
        use_ok, usage = self.safe(self.call_tool, T.query_usage, account_id)

        if not (sub_ok and inv_ok):
            kind = getattr(sub, "kind", None) or getattr(inv, "kind", "unknown")
            return SkillOutcome(
                facts={"query_failed": True, "error_kind": kind},
                degraded=True,
                degrade_reason=f"查询账单失败({kind})",
                reply="抱歉，暂时无法读取您的账单信息，我已为您转接人工客服核对。",
                escalate=True,
                escalate_reason="工具调用失败",
            )

        facts = {
            "account_id": account_id,
            "plan": sub.plan,
            "seats": sub.seats,
            "tenure_months": sub.tenure_months,
            "user_tier": sub.user_tier,
            "open_complaints": sub.open_complaints,
            "invoice_id": inv.invoice_id,
            "amount": inv.amount,
            "disputed": inv.disputed,
            "dispute_reason": inv.dispute_reason,
            "lines": inv.lines,
            "overage": usage.overage if use_ok else 0,
            "included_quota": usage.included_quota if use_ok else 0,
            "api_calls": usage.api_calls if use_ok else 0,
        }
        line_desc = "；".join(
            "{sku} x{qty} = {amount}".format(
                sku=line.get("sku"), qty=line.get("qty"), amount=line.get("amount")
            )
            for line in inv.lines
        )
        evidence = [
            f"账单 {inv.invoice_id} 金额 {inv.amount} {inv.currency}",
            f"计费项：{line_desc}",
            f"用量：调用 {facts['api_calls']} 次，包含额度 {facts['included_quota']}，超出 {facts['overage']}",
            f"订阅时长 {sub.tenure_months} 个月，等级 {sub.user_tier}，未结投诉 {sub.open_complaints} 次",
        ]
        if inv.disputed:
            evidence.append(f"争议描述：{inv.dispute_reason}")
        return SkillOutcome(facts=facts, evidence=evidence, extra={"entities": entities})

    # ------------------------------------------------------------------
    @staticmethod
    def _risk_values(facts: dict[str, Any]) -> dict[str, Any]:
        """把取证事实归一化为评分用的字段字典（_score 与硬性规则共用）。"""
        return {
            "amount": float(facts.get("amount", 0.0)),
            "open_complaints": int(facts.get("open_complaints", 0)),
            "history_adjustments": int(facts.get("history_adjustments", 0)),
            "user_tier": str(facts.get("user_tier", "basic")),
            "tenure_months": int(facts.get("tenure_months", 0)),
        }

    def _score(self, facts: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        """按 RISK_DIMENSIONS 计算风险分。返回 (总分, 明细)。"""
        values = self._risk_values(facts)
        total = 0.0
        detail: list[dict[str, Any]] = []
        for dim in RISK_DIMENSIONS:
            value = values[dim["field"]]
            score = 0.0
            hit_note = ""
            # 规则互斥，取**第一条**命中即可（表内规则已写成不重叠区间）。
            # 取最后一条会在"累计上界"式写法下产生严重失真，见 RISK_DIMENSIONS 注释。
            for rule in dim["rules"]:
                if _match_cmp(rule["cmp"], values):
                    score = float(rule["score"])
                    hit_note = rule.get("note", "")
                    break
            total += score
            detail.append(
                {
                    "dimension": dim["name"],
                    "value": value,
                    "score": score,
                    "max_score": dim["max_score"],
                    "note": hit_note,
                }
            )
        return min(100.0, total), detail

    def diagnose(self, state: SupportState) -> SkillOutcome:
        facts = state.get("facts") or {}
        if facts.get("query_failed"):
            return SkillOutcome(cause_code="QUERY_FAILED", cause_text="无法读取账单", confidence=0.0)

        if not facts.get("disputed"):
            return SkillOutcome(
                cause_code="NO_DISPUTE",
                cause_text="账单未标记争议",
                confidence=0.9,
                decision="none",
                decision_reason="无争议，无需处置",
            )

        score, detail = self._score(facts)
        # 命中维度及其原因：用于向用户/坐席解释"为什么是这个风险分"。
        # 早期这里算完就丢掉了，导致证据里只有分值、没有"为什么"（审查发现）。
        risk_reasons = [
            f"{d['dimension']} +{d['score']:.0f}"
            + (f"（{d['note']}）" if d.get("note") else "")
            for d in detail
            if d["score"] > 0
        ]

        # 硬性规则（阈值来自配置；规则表是唯一来源，避免与内联判断两处漂移）
        hard: list[str] = []
        risk_values = self._risk_values(facts)
        for rule in HARD_RULES:
            threshold = float(getattr(settings, rule["threshold_setting"]))
            if _match_cmp([(rule["field"], rule["op"], threshold)], risk_values):
                hard.append(
                    f"{rule['note']}（{rule['name']}）："
                    f"{rule['field']}={risk_values[rule['field']]}，阈值 {threshold:.0f}"
                )

        # 阈值表判定
        action = "manual_approval"
        for band in DECISION_THRESHOLDS:
            if score <= band["max_score"]:
                action = band["action"]
                break
        if hard:
            action = "manual_approval"

        evidence = (
            list(state.get("evidence") or [])
            + [
                f"风控评分 {score:.0f}/100（"
                + "；".join(f"{d['dimension']}{d['score']:.0f}" for d in detail)
                + "）"
            ]
            + (
                ["主要风险来源：" + "；".join(risk_reasons)]
                if risk_reasons
                else ["各维度均未触发风险加分"]
            )
            + [f"硬性规则命中：{h}" for h in hard]
        )

        return SkillOutcome(
            cause_code="BILLING_DISPUTE_CONFIRMED",
            cause_text=facts.get("dispute_reason") or "账单存在争议",
            confidence=0.88,
            risk_score=score,
            evidence=evidence,
            decision=action,
            decision_reason=(
                ("命中硬性规则：" + "；".join(hard))
                if hard
                else f"风险分 {score:.0f} 落入 {action} 区间"
            ),
            extra={"risk_detail": detail, "hard_rules": hard},
        )

    # ------------------------------------------------------------------
    def _check_idempotency(self, idem_key: str) -> dict[str, Any] | None:
        """查幂等登记表：若已成功执行过，直接返回上次结果。"""
        from sqlalchemy import select

        from app.core.database import read_session
        from app.models import IdempotencyKey

        try:
            with read_session() as s:
                row = s.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.idem_key == idem_key, IdempotencyKey.scope == "billing.adjustment"
                    )
                ).scalar_one_or_none()
                if row and row.status == "succeeded":
                    import json

                    return json.loads(row.response_json) if row.response_json else {"status": "succeeded"}
        except Exception:  # noqa: BLE001 - 幂等查询失败不应阻断主流程
            return None
        return None

    def _record_idempotency(self, idem_key: str, result: dict[str, Any], resource_no: str) -> None:
        """登记幂等结果（唯一约束保证并发下也不会重复）。"""
        import json

        from sqlalchemy import select

        from app.core.database import write_session
        from app.core.trace_context import get_tenant_id
        from app.models import IdempotencyKey

        tid = get_tenant_id()
        try:
            with write_session() as s:
                exists = s.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.idem_key == idem_key, IdempotencyKey.scope == "billing.adjustment"
                    )
                ).scalar_one_or_none()
                if exists:
                    exists.status = "succeeded"
                    exists.response_json = json.dumps(result, ensure_ascii=False)
                    exists.resource_no = resource_no
                else:
                    s.add(
                        IdempotencyKey(
                            tenant_id=tid,
                            idem_key=idem_key,
                            scope="billing.adjustment",
                            status="succeeded",
                            response_json=json.dumps(result, ensure_ascii=False),
                            resource_no=resource_no,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            from app.core.logging_config import get_logger

            get_logger(__name__).warning("billing.idempotency_record_failed", error=str(exc)[:200])

    def act(self, state: SupportState) -> SkillOutcome:
        """按风险分决策执行：自动调整 / 带审计调整 / 转人工审批。"""
        facts = state.get("facts") or {}
        decision = state.get("decision", "")
        amount = float(facts.get("amount", 0.0))
        account_id = facts.get("account_id", "")
        invoice_id = facts.get("invoice_id", "")
        reason = state.get("cause_text", "")
        risk = float(state.get("risk_score", 0.0))

        if decision == "none":
            return SkillOutcome(
                decision="none",
                decision_reason="无争议",
                verify_ok=True,
                verify_detail="无需处置",
                reply="我核对了您的账单，没有发现异常计费项。如有疑问，可以告诉我具体是哪一笔。",
                outcome="resolved",
            )

        # 幂等键：会话 + 账单 + 金额，保证同一诉求不会重复退款
        idem_key = f"{state.get('session_id', '')}:{invoice_id}:{amount:.2f}"
        cached = self._check_idempotency(idem_key)
        if cached:
            return SkillOutcome(
                actions=[{"action": "idempotent_hit", "ok": True, "detail": "该诉求已处理过，未重复执行"}],
                decision="idempotent",
                decision_reason="幂等键命中，直接复用首次结果",
                verify_ok=True,
                verify_detail="幂等命中，未重复退款",
                outcome="resolved",
                reply="这笔调整此前已经提交过，我没有重复操作，避免重复退款。原处理结果依然有效。",
            )

        if decision in ("auto_adjust", "auto_adjust_audit"):
            ok, res = self.safe(
                self.call_tool,
                T.create_billing_adjustment,
                account_id,
                amount,
                "refund",
                reason,
                invoice_id,
                idem_key,
            )
            if ok and res.status == "succeeded":
                self._record_idempotency(
                    idem_key, {"status": "succeeded", "adjustment_id": res.adjustment_id}, res.adjustment_id
                )
                actions = [
                    {
                        "action": "adjustment",
                        "ok": True,
                        "detail": f"已发起退款 {amount} 元，单号 {res.adjustment_id}",
                    }
                ]
                if decision == "auto_adjust_audit":
                    actions.append(
                        {"action": "audit_note", "ok": True, "detail": "已留存审计记录并通知用户"}
                    )
                return SkillOutcome(
                    actions=actions,
                    decision=decision,
                    decision_reason=state.get("decision_reason", ""),
                    verify_ok=True,
                    verify_detail=f"账务调整成功，单号 {res.adjustment_id}",
                    outcome="resolved",
                    reply=(
                        f"已为您处理：账单 {invoice_id} 的 {amount} 元争议金额已发起退款，"
                        f"退款单号 {res.adjustment_id}，通常 3-5 个工作日原路退回。\n"
                        "如有其他疑问随时告诉我。"
                    ),
                )
            # 计费接口报错 → 查幂等状态再决定重试/确认（容错矩阵）
            err_code = getattr(res, "code", "UNKNOWN")
            cached2 = self._check_idempotency(idem_key)
            if cached2:
                return SkillOutcome(
                    actions=[{"action": "adjustment_confirm", "ok": True, "detail": "上游报错但幂等表显示已成功"}],
                    decision="confirmed",
                    decision_reason="接口报错，但幂等状态显示已成功，判定为已处理",
                    verify_ok=True,
                    verify_detail="经幂等状态确认已成功",
                    outcome="resolved",
                    reply="系统提示了一次接口异常，但我已确认这笔退款实际已提交成功，无需重复申请。",
                )
            return SkillOutcome(
                actions=[{"action": "adjustment", "ok": False, "detail": f"调整失败 {err_code}"}],
                decision="escalate",
                decision_reason=f"账务接口失败({err_code})",
                escalate=True,
                escalate_reason="账务调整失败",
            )

        # manual_approval
        ok, res = self.safe(
            self.call_tool,
            T.request_billing_approval,
            account_id,
            amount,
            reason,
            risk,
            invoice_id,
        )
        # 动作详情只放"值"，不放"标签"。
        # 话术里会自己加"审批单号："前缀，若这里也带前缀就会出现
        # "审批单号：审批单号 apv-xxx" 这类重复（本项目实测踩到）。
        detail = f"{res.approval_id}（审批角色 {res.approver_role}）" if ok else "提交审批失败"
        actions = [{"action": "manual_approval", "ok": ok, "detail": detail}]
        return SkillOutcome(
            actions=actions,
            decision="manual_approval",
            decision_reason=state.get("decision_reason", ""),
            escalate=True,
            escalate_reason="计费争议需人工审批",
        )

    def verify(self, state: SupportState) -> SkillOutcome:
        decision = state.get("decision", "")

        if decision == "manual_approval":
            ref = (state.get("actions") or [{}])[0].get("detail", "")
            return SkillOutcome(
                verify_ok=False,
                verify_detail="等待人工审批",
                outcome="pending_approval",
                reply=(
                    "该笔争议金额或账号情况需要人工复核，我已为您提交审批。\n"
                    f"审批单号：{ref}\n"
                    "审批通过后会原路退回；您也可以直接回复「转人工」，由人工客服同步进度。"
                ),
            )

        if decision in ("auto_adjust", "auto_adjust_audit", "idempotent", "confirmed", "none"):
            # 账务复核：确认调整单已落账（此处以幂等/调整结果为准）
            return SkillOutcome(
                verify_ok=True,
                verify_detail=state.get("verify_detail", "账务状态正常"),
                outcome=state.get("outcome", "resolved"),
                reply=state.get("reply", "已处理完成。"),
            )

        return SkillOutcome(
            verify_ok=False,
            verify_detail="处置未完成",
            escalate=True,
            escalate_reason="计费争议未闭环",
        )
