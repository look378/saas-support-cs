"""框架自检：技能注册、路由、实体抽取、建图、风控评分表。

不依赖数据库与外部服务，纯粹验证框架装配是否正确。
    .venv\\Scripts\\python.exe scripts\\check_framework.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import platform_compat  # noqa: E402

platform_compat.apply()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


print("=" * 74)
print("  框架自检")
print("=" * 74)

# ---------------------------------------------------------------- 技能注册
print("\n[1] 技能注册表")
from app.skills.registry import get_registry  # noqa: E402

reg = get_registry()
skills = reg.describe()
for s in skills:
    print(f"      {s['code']:<12} {s['name']}")
check("五个技能全部注册", len(skills) == 5, f"实际 {len(skills)} 个")
check(
    "技能代号齐全",
    {s["code"] for s in skills} == {"faq", "integration", "permission", "billing", "account"},
)

# ---------------------------------------------------------------- 路由
print("\n[2] 关键词路由（不调用模型）")
route_cases = [
    ("钉钉机器人从昨天开始就不回复消息了，工作区是 ws-1001", "integration"),
    ("我的文档 doc-771 打不开，提示没有访问权限", "permission"),
    ("这个月账单重复计费了，账号 acct-501，我要退钱", "billing"),
    ("账号 u-42 凌晨在境外登录了，怀疑被盗", "account"),
    ("怎么设置消息免打扰？支持定时免打扰吗", "faq"),
]
for msg, expect in route_cases:
    r = reg.route(msg)
    ok = r.skill == expect
    check(f"路由 → {expect}", ok, f"得到 {r.skill} ({r.confidence:.2f}) {r.reason[:38]}")

# ---------------------------------------------------------------- 实体抽取
print("\n[3] 实体抽取")
checks = [
    ("integration", "工作区 ws-1002 的 slack 机器人不回复", "workspace_id", "ws-1002"),
    ("permission", "doc-772 打不开，账号 u-43", "doc_id", "doc-772"),
    ("billing", "账号 acct-502 账单有问题", "account_id", "acct-502"),
    ("account", "u-44 疑似异地登录", "user_id", "u-44"),
]
for code, msg, key, expect in checks:
    sk = reg.get(code)
    got = (sk.extract(msg) or {}).get(key, "")
    check(f"{code} 抽取 {key}", got == expect, f"得到 {got!r}")

# ---------------------------------------------------------------- 澄清判定
print("\n[4] 信息不足应触发澄清")
sk_int = reg.get("integration")
check(
    "无工作区标识 → 必填项缺失",
    "workspace_id" in sk_int.missing_entities(sk_int.extract("机器人不回消息了")),
)
check(
    "给了工作区标识 → 必填项满足",
    not sk_int.missing_entities(sk_int.extract("工作区 ws-1001 机器人不回消息")),
)

# ---------------------------------------------------------------- 建图
print("\n[5] 状态图装配")
from app.agents.graph.support_graph import build_graph  # noqa: E402

g = build_graph(None)
nodes = sorted(n for n in g.get_graph().nodes if not n.startswith("__"))
expected_nodes = ["act", "clarify", "diagnose", "finalize", "observe", "retry", "route", "verify"]
check("节点齐全", nodes == expected_nodes, f"节点={nodes}")

# ---------------------------------------------------------------- 风控评分
print("\n[6] 计费风控评分表与阈值表")
from app.mocks.data import account_profile  # noqa: E402
from app.skills.billing import DECISION_THRESHOLDS, RISK_DIMENSIONS, BillingSkill  # noqa: E402

print(f"      评分维度：{len(RISK_DIMENSIONS)} 个，阈值区间：{len(DECISION_THRESHOLDS)} 档")
bs = BillingSkill()
expectations = [
    # 金额 2380 超硬阈值 → 必须人工审批
    ("acct-501", ("manual_approval",), "金额 2380 超硬阈值"),
    # 小额 42 但新客户（2 个月）→ 自动处理，且带审计（不直接进入"无审计自动"档）
    ("acct-502", ("auto_adjust", "auto_adjust_audit"), "小额可自动；新客户需留审计"),
    # 投诉 4 次超硬阈值 → 必须人工审批
    ("acct-503", ("manual_approval",), "投诉 4 次超硬阈值"),
]
for acct, expect_actions, why in expectations:
    p = account_profile(acct)
    facts = {
        "amount": p["amount"],
        "open_complaints": p["open_complaints"],
        "history_adjustments": 0,
        "user_tier": p["user_tier"],
        "tenure_months": p["tenure_months"],
    }
    score, detail = bs._score(facts)
    action = "manual_approval"
    for band in DECISION_THRESHOLDS:
        if score <= band["max_score"]:
            action = band["action"]
            break
    # 硬性规则覆盖
    from app.core.config import settings

    if p["amount"] > settings.escalate_amount_threshold or p["open_complaints"] > settings.escalate_complaint_threshold:
        action = "manual_approval"
    check(
        f"{acct} 决策 ∈ {expect_actions}",
        action in expect_actions,
        f"风险分={score:.0f} 动作={action}（{why}）",
    )
    print("         " + "；".join(f"{d['dimension']}={d['score']:.0f}" for d in detail))

# 阈值表区间必须连续且覆盖 0-100
covered = [b["max_score"] for b in DECISION_THRESHOLDS]
check("阈值表覆盖完整区间", covered[-1] == 100 and sorted(covered) == covered, f"上界={covered}")

# ---------------------------------------------------------------- 账号风险
print("\n[7] 账号安全风险信号表")
from app.skills.account import RISK_LEVELS, RISK_SIGNALS  # noqa: E402

print(f"      风险信号：{len(RISK_SIGNALS)} 个，等级阈值：{len(RISK_LEVELS)} 档")
print(f"      高风险信号组合总分 = {sum(s['score'] for s in RISK_SIGNALS[:3]) + RISK_SIGNALS[4]['score']}")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  框架自检全部通过。")
