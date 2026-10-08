"""示例数据集：44 条（案例 24 + FAQ 20）。

设计要点（决策 ADR-005）：案例库同时承担三个角色
  1. RAG 语料——为诊断提供历史参考
  2. 集成测试的断言依据——``expected_diagnosis`` / ``expected_action`` /
     ``expected_verify_result`` 三列让测试能断言"诊断是否正确、动作是否符合预期"，
     而不是只能断言"有回复"
  3. 演示可复现——与 Mock 后端的确定性故障画像一一对应

数据以代码形式维护并导出为 CSV（``scripts/export_samples.py``），
避免手工维护 CSV 时出现列错位、编码问题。
"""

from __future__ import annotations

# ===========================================================================
# 案例库 24 条：集成 8 + 权限 6 + 计费 6 + 账号安全 4
# ===========================================================================
CASES: list[dict[str, str]] = [
    # ---------------- 集成故障 8 条 ----------------
    {
        "case_id": "C-INT-001",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "Slack 机器人突然不回复消息",
        "symptom": "工作区 ws-1001 的 Slack 机器人从昨天开始不再回复任何消息，重新拉群也没用",
        "expected_diagnosis": "TOKEN_EXPIRED",
        "expected_action": "refresh_token",
        "expected_verify_result": "delivered",
        "risk_level": "low",
        "source_ref": "mock:ws-1001",
    },
    {
        "case_id": "C-INT-002",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "钉钉回调地址被改动导致事件收不到",
        "symptom": "工作区 ws-1002 的钉钉机器人仍能发消息，但收不到用户消息，怀疑回调地址被改过",
        "expected_diagnosis": "WEBHOOK_INVALID",
        "expected_action": "repair_webhook",
        "expected_verify_result": "delivered",
        "risk_level": "low",
        "source_ref": "mock:ws-1002",
    },
    {
        "case_id": "C-INT-003",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "飞书应用权限被移除无法发送消息",
        "symptom": "工作区 ws-1003 的飞书机器人授权还在，但发送消息一直失败，提示权限不足",
        "expected_diagnosis": "SCOPE_REVOKED",
        "expected_action": "reauth_link",
        "expected_verify_result": "pending_user_action",
        "risk_level": "medium",
        "source_ref": "mock:ws-1003",
    },
    {
        "case_id": "C-INT-004",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "集成配置一切正常但用户误以为故障",
        "symptom": "用户反馈机器人不回复，但检查发现令牌、回调、权限全部正常",
        "expected_diagnosis": "HEALTHY",
        "expected_action": "none",
        "expected_verify_result": "no_action_needed",
        "risk_level": "low",
        "source_ref": "mock:ws-1004",
    },
    {
        "case_id": "C-INT-005",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "刷新令牌时发现刷新令牌也失效",
        "symptom": "访问令牌过期，尝试自动刷新时提示刷新令牌无效，需要用户重新授权",
        "expected_diagnosis": "TOKEN_EXPIRED",
        "expected_action": "reauth_link",
        "expected_verify_result": "pending_user_action",
        "risk_level": "medium",
        "source_ref": "mock:ws-1001",
    },
    {
        "case_id": "C-INT-006",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "令牌过期同时掩盖了回调地址问题",
        "symptom": "机器人不回复，回调地址也显示不可达，但令牌同样已过期，需要区分主因",
        "expected_diagnosis": "TOKEN_EXPIRED",
        "expected_action": "refresh_token",
        "expected_verify_result": "delivered",
        "risk_level": "low",
        "source_ref": "mock:ws-1001",
    },
    {
        "case_id": "C-INT-007",
        "tenant_profile": "vip",
        "skill": "integration",
        "scenario": "VIP 租户集成故障要求优先处理",
        "symptom": "VIP 客户的工作区机器人失效，影响客服响应，要求尽快恢复",
        "expected_diagnosis": "TOKEN_EXPIRED",
        "expected_action": "refresh_token",
        "expected_verify_result": "delivered",
        "risk_level": "high",
        "source_ref": "mock:ws-1001",
    },
    {
        "case_id": "C-INT-008",
        "tenant_profile": "standard",
        "skill": "integration",
        "scenario": "集成查询接口本身报错导致无法诊断",
        "symptom": "尝试查询集成配置时上游接口返回 5xx，无法获取令牌与回调状态",
        "expected_diagnosis": "QUERY_FAILED",
        "expected_action": "escalate",
        "expected_verify_result": "escalated",
        "risk_level": "medium",
        "source_ref": "mock:X-Mock-Mode=error",
    },
    # ---------------- 权限访问 6 条 ----------------
    {
        "case_id": "C-PER-001",
        "tenant_profile": "standard",
        "skill": "permission",
        "scenario": "同事打不开我分享的文档",
        "symptom": "文档 doc-771 分享给同事后对方仍打不开，提示没有访问权限",
        "expected_diagnosis": "SHARE_NOT_ENABLED",
        "expected_action": "grant_viewer",
        "expected_verify_result": "accessible",
        "risk_level": "low",
        "source_ref": "mock:doc-771",
    },
    {
        "case_id": "C-PER-002",
        "tenant_profile": "standard",
        "skill": "permission",
        "scenario": "组织策略禁止外链分享",
        "symptom": "文档 doc-772 生成了分享链接但外部同事无法访问，提示组织策略限制",
        "expected_diagnosis": "ORG_POLICY_BLOCKED",
        "expected_action": "policy_exception",
        "expected_verify_result": "pending_approval",
        "risk_level": "medium",
        "source_ref": "mock:doc-772",
    },
    {
        "case_id": "C-PER-003",
        "tenant_profile": "standard",
        "skill": "permission",
        "scenario": "文档版本冲突导致无法打开",
        "symptom": "文档 doc-773 打开时提示版本冲突，两个人在同时编辑",
        "expected_diagnosis": "VERSION_CONFLICT",
        "expected_action": "unlock_keep_latest",
        "expected_verify_result": "accessible",
        "risk_level": "low",
        "source_ref": "mock:doc-773",
    },
    {
        "case_id": "C-PER-004",
        "tenant_profile": "standard",
        "skill": "permission",
        "scenario": "权限实际正常属于客户端缓存问题",
        "symptom": "用户说文档打不开，检查发现分享已开启、用户有权限",
        "expected_diagnosis": "ACCESSIBLE",
        "expected_action": "none",
        "expected_verify_result": "no_action_needed",
        "risk_level": "low",
        "source_ref": "mock:doc-774",
    },
    {
        "case_id": "C-PER-005",
        "tenant_profile": "standard",
        "skill": "permission",
        "scenario": "需要编辑权限而非只读",
        "symptom": "同事需要共同编辑文档，当前只有只读权限无法修改内容",
        "expected_diagnosis": "SHARE_NOT_ENABLED",
        "expected_action": "grant_editor",
        "expected_verify_result": "accessible",
        "risk_level": "medium",
        "source_ref": "mock:doc-771",
    },
    {
        "case_id": "C-PER-006",
        "tenant_profile": "vip",
        "skill": "permission",
        "scenario": "策略例外审批超时需人工跟进",
        "symptom": "组织策略例外申请已提交但超过 SLA 小时数仍未审批",
        "expected_diagnosis": "ORG_POLICY_BLOCKED",
        "expected_action": "escalate",
        "expected_verify_result": "escalated",
        "risk_level": "high",
        "source_ref": "mock:doc-772",
    },
    # ---------------- 订阅计费 6 条 ----------------
    {
        "case_id": "C-BIL-001",
        "tenant_profile": "standard",
        "skill": "billing",
        "scenario": "席位费被重复计费",
        "symptom": "11 月账单里席位费被计了两次，账号 acct-501 要求退款",
        "expected_diagnosis": "BILLING_DISPUTE_CONFIRMED",
        "expected_action": "manual_approval",
        "expected_verify_result": "pending_approval",
        "risk_level": "high",
        "source_ref": "mock:acct-501",
    },
    {
        "case_id": "C-BIL-002",
        "tenant_profile": "standard",
        "skill": "billing",
        "scenario": "小额用量超限费用争议",
        "symptom": "账号 acct-502 的用量超限费用 42 元认为计数异常，申请退款",
        "expected_diagnosis": "BILLING_DISPUTE_CONFIRMED",
        "expected_action": "auto_adjust",
        "expected_verify_result": "resolved",
        "risk_level": "low",
        "source_ref": "mock:acct-502",
    },
    {
        "case_id": "C-BIL-003",
        "tenant_profile": "standard",
        "skill": "billing",
        "scenario": "高频投诉用户反复申请退款",
        "symptom": "账号 acct-503 已有多次投诉记录，再次对计费规则提出异议",
        "expected_diagnosis": "BILLING_DISPUTE_CONFIRMED",
        "expected_action": "manual_approval",
        "expected_verify_result": "pending_approval",
        "risk_level": "high",
        "source_ref": "mock:acct-503",
    },
    {
        "case_id": "C-BIL-004",
        "tenant_profile": "vip",
        "skill": "billing",
        "scenario": "大客户账单核对无异常",
        "symptom": "企业客户 acct-504 要求核对本月账单金额是否正确",
        "expected_diagnosis": "NO_DISPUTE",
        "expected_action": "none",
        "expected_verify_result": "no_action_needed",
        "risk_level": "low",
        "source_ref": "mock:acct-504",
    },
    {
        "case_id": "C-BIL-005",
        "tenant_profile": "standard",
        "skill": "billing",
        "scenario": "重复提交退款申请需要幂等保护",
        "symptom": "同一笔争议金额用户连续提交了两次退款申请，需要避免重复退款",
        "expected_diagnosis": "BILLING_DISPUTE_CONFIRMED",
        "expected_action": "idempotent_hit",
        "expected_verify_result": "resolved",
        "risk_level": "medium",
        "source_ref": "mock:acct-502",
    },
    {
        "case_id": "C-BIL-006",
        "tenant_profile": "standard",
        "skill": "billing",
        "scenario": "账务接口报错需确认是否已处理",
        "symptom": "发起退款时计费接口返回 5xx 错误，需要确认这笔退款是否实际成功",
        "expected_diagnosis": "BILLING_DISPUTE_CONFIRMED",
        "expected_action": "adjustment_confirm",
        "expected_verify_result": "resolved",
        "risk_level": "medium",
        "source_ref": "mock:X-Mock-Mode=error",
    },
    # ---------------- 账号安全 4 条 ----------------
    {
        "case_id": "C-ACC-001",
        "tenant_profile": "standard",
        "skill": "account",
        "scenario": "境外异常登录疑似账号被盗",
        "symptom": "账号 u-42 凌晨 3 点在境外 IP 登录成功，且使用了从未见过的设备",
        "expected_diagnosis": "ACCOUNT_COMPROMISE_SUSPECTED",
        "expected_action": "lock_and_notify",
        "expected_verify_result": "safe",
        "risk_level": "high",
        "source_ref": "mock:u-42",
    },
    {
        "case_id": "C-ACC-002",
        "tenant_profile": "standard",
        "skill": "account",
        "scenario": "异常时段登录但设备与地区正常",
        "symptom": "账号 u-43 在凌晨 2 点登录，但 IP 与设备都在常用范围内",
        "expected_diagnosis": "SUSPICIOUS_MEDIUM",
        "expected_action": "notify_require_mfa",
        "expected_verify_result": "safe",
        "risk_level": "medium",
        "source_ref": "mock:u-43",
    },
    {
        "case_id": "C-ACC-003",
        "tenant_profile": "standard",
        "skill": "account",
        "scenario": "正常登录被误报为异常",
        "symptom": "账号 u-44 收到异常登录提醒，但登录时间与设备均与平时一致",
        "expected_diagnosis": "NO_ANOMALY",
        "expected_action": "none",
        "expected_verify_result": "no_action_needed",
        "risk_level": "low",
        "source_ref": "mock:u-44",
    },
    {
        "case_id": "C-ACC-004",
        "tenant_profile": "vip",
        "skill": "account",
        "scenario": "高风险锁定后需要人工核实身份",
        "symptom": "账号已临时锁定，用户要求尽快恢复使用，需要人工核实身份",
        "expected_diagnosis": "ACCOUNT_COMPROMISE_SUSPECTED",
        "expected_action": "lock_and_notify",
        "expected_verify_result": "escalated",
        "risk_level": "high",
        "source_ref": "mock:u-42",
    },
]

# ===========================================================================
# FAQ 20 条
# ===========================================================================
FAQ: list[dict[str, str]] = [
    {
        "faq_id": "F-001",
        "category": "账号",
        "question": "如何修改账号绑定的手机号？",
        "answer": "进入「个人设置」→「账号安全」→「手机号」，验证原手机号后即可更换新号码。若原手机号已不可用，请提交人工核验工单。",
        "keywords": "手机号 更换 绑定 账号安全",
    },
    {
        "faq_id": "F-002",
        "category": "账号",
        "question": "怎么开启二次验证（MFA）？",
        "answer": "进入「个人设置」→「账号安全」→「二次验证」，支持验证器 App 与短信两种方式。建议优先使用验证器 App，安全性更高。",
        "keywords": "二次验证 MFA 动态口令 安全",
    },
    {
        "faq_id": "F-003",
        "category": "账号",
        "question": "忘记密码怎么办？",
        "answer": "在登录页点击「忘记密码」，通过绑定手机号或邮箱验证后重置。若账号已被锁定，需先联系管理员解锁。",
        "keywords": "忘记密码 重置 找回",
    },
    {
        "faq_id": "F-004",
        "category": "消息",
        "question": "如何设置消息免打扰？",
        "answer": "在会话设置中打开「消息免打扰」，可选择「始终」或「定时」（例如仅夜间）。免打扰期间消息仍会保留，不会丢失。",
        "keywords": "免打扰 通知 消息 静音",
    },
    {
        "faq_id": "F-005",
        "category": "消息",
        "question": "消息可以撤回多长时间内的内容？",
        "answer": "发送后 2 分钟内可撤回任意消息，超过 2 分钟仅管理员可在审计日志中处理。撤回后对方将看到「消息已撤回」提示。",
        "keywords": "撤回 消息 时限",
    },
    {
        "faq_id": "F-006",
        "category": "消息",
        "question": "如何搜索历史消息？",
        "answer": "在顶部搜索框输入关键词即可全局搜索，也支持「from:某人」「in:某群」等筛选语法。免费版保留 90 天记录，付费版不限。",
        "keywords": "搜索 历史消息 记录 检索",
    },
    {
        "faq_id": "F-007",
        "category": "文档",
        "question": "如何把文档分享给外部人员？",
        "answer": "打开文档 →「分享」→ 选择「开启链接分享」。若组织策略禁止外链，需要管理员开启「允许外部共享」或为该文档申请策略例外。",
        "keywords": "文档 分享 外链 外部 共享",
    },
    {
        "faq_id": "F-008",
        "category": "文档",
        "question": "文档的版本历史可以保留多久？",
        "answer": "标准版保留最近 30 天版本记录，专业版保留 180 天，旗舰版不限。可在「文档 → 更多 → 版本历史」中查看与还原。",
        "keywords": "文档 版本 历史 还原",
    },
    {
        "faq_id": "F-009",
        "category": "文档",
        "question": "多人同时编辑会冲突吗？",
        "answer": "不会。多人协作采用实时同步，可同时编辑同一文档。若出现「版本冲突」提示，通常是因为离线编辑后重新联网，按提示选择保留的版本即可。",
        "keywords": "协作 同时编辑 冲突 版本",
    },
    {
        "faq_id": "F-010",
        "category": "权限",
        "question": "文档权限有哪几种角色？",
        "answer": "共四种：仅查看、可评论、可编辑、可管理。可管理角色可以继续给他人授权。所有角色变更都会记录在审计日志中。",
        "keywords": "权限 角色 查看 编辑 评论 管理",
    },
    {
        "faq_id": "F-011",
        "category": "权限",
        "question": "为什么我看不到某个文档？",
        "answer": "可能原因有三种：文档未对你开启分享、组织策略限制了访问范围、或存在版本冲突。可以在文档链接页面查看具体提示，或让文档所有者为你开启权限。",
        "keywords": "看不到 打不开 权限 访问",
    },
    {
        "faq_id": "F-012",
        "category": "集成",
        "question": "支持哪些第三方集成？",
        "answer": "目前支持 Slack、钉钉、飞书、企业微信，以及通过 Webhook 自建的集成。集成入口在「工作区设置」→「集成」。",
        "keywords": "集成 Slack 钉钉 飞书 webhook",
    },
    {
        "faq_id": "F-013",
        "category": "集成",
        "question": "集成授权过期了怎么办？",
        "answer": "授权过期系统会自动尝试刷新；若刷新失败，会生成重新授权链接发给你。点击链接完成授权后集成立即恢复，无需重新配置。",
        "keywords": "集成 授权 过期 令牌 token 刷新",
    },
    {
        "faq_id": "F-014",
        "category": "集成",
        "question": "机器人不回复消息怎么排查？",
        "answer": "按三步检查：1) 工作区集成页确认「已连接」状态；2) 检查访问令牌是否过期；3) 检查回调地址是否与平台配置一致。若三项均正常，可能是平台侧限流，稍后重试。",
        "keywords": "机器人 不回复 排查 集成 回调",
    },
    {
        "faq_id": "F-015",
        "category": "计费",
        "question": "订阅套餐有哪些区别？",
        "answer": "入门版适合 10 人以内团队；团队版含 100GB 存储与 10 万次 API 调用；旗舰版不限存储并含专属支持与 SSO。",
        "keywords": "订阅 套餐 价格 版本 计费",
    },
    {
        "faq_id": "F-016",
        "category": "计费",
        "question": "用量超出套餐额度会怎样？",
        "answer": "超出部分按量计费，API 调用 0.01 元/次，存储在下一计费周期按 0.5 元/GB 结算。可在「账单 → 用量」中设置额度告警。",
        "keywords": "用量 超额 超限 计费 额度",
    },
    {
        "faq_id": "F-017",
        "category": "计费",
        "question": "如何申请退款？",
        "answer": "在「账单」中对应账期点击「申请退款」并填写原因。小额争议（100 元以内）通常即时处理；超过阈值需财务审批，3-5 个工作日原路退回。",
        "keywords": "退款 申请 账单 争议",
    },
    {
        "faq_id": "F-018",
        "category": "计费",
        "question": "发票怎么开具？",
        "answer": "进入「账单 → 发票管理」，填写开票信息后提交，电子发票通常 1 个工作日内发送到指定邮箱。已开票的账期如需修改，请提交工单。",
        "keywords": "发票 开票 报销 账单",
    },
    {
        "faq_id": "F-019",
        "category": "安全",
        "question": "收到异常登录提醒该怎么办？",
        "answer": "若确认不是本人操作：立即修改密码、开启二次验证，并在「账号安全 → 登录设备」中移除陌生设备。必要时回复「转人工」由安全团队协助处置。",
        "keywords": "异常登录 提醒 安全 被盗 陌生设备",
    },
    {
        "faq_id": "F-020",
        "category": "安全",
        "question": "如何查看账号的登录记录？",
        "answer": "进入「个人设置」→「账号安全」→「登录记录」，可查看最近 30 天的登录时间、IP、地区与设备。发现异常可一键锁定账号。",
        "keywords": "登录记录 审计 设备 IP 安全",
    },
]

# 校验：数量与分布必须与任务书要求一致（集成 8 + 权限 6 + 计费 6 + 账号 4 + FAQ 20）
EXPECTED_DISTRIBUTION = {"integration": 8, "permission": 6, "billing": 6, "account": 4}
EXPECTED_FAQ = 20


def distribution() -> dict[str, int]:
    """案例的技能分布。"""
    out: dict[str, int] = {}
    for c in CASES:
        out[c["skill"]] = out.get(c["skill"], 0) + 1
    return out


def validate() -> list[str]:
    """校验数据集自身是否符合预期。返回问题列表（空表示通过）。"""
    problems: list[str] = []
    dist = distribution()
    if dist != EXPECTED_DISTRIBUTION:
        problems.append(f"案例分布不符：得到 {dist}，期望 {EXPECTED_DISTRIBUTION}")
    if len(FAQ) != EXPECTED_FAQ:
        problems.append(f"FAQ 数量不符：得到 {len(FAQ)}，期望 {EXPECTED_FAQ}")

    ids = [c["case_id"] for c in CASES]
    if len(ids) != len(set(ids)):
        problems.append("案例 ID 存在重复")

    faq_ids = [f["faq_id"] for f in FAQ]
    if len(faq_ids) != len(set(faq_ids)):
        problems.append("FAQ ID 存在重复")

    required = {
        "case_id", "tenant_profile", "skill", "scenario", "symptom",
        "expected_diagnosis", "expected_action", "expected_verify_result",
        "risk_level", "source_ref",
    }
    for c in CASES:
        missing = required - set(c.keys())
        if missing:
            problems.append(f"{c.get('case_id', '?')} 缺少字段：{sorted(missing)}")
        if not c.get("expected_diagnosis"):
            problems.append(f"{c.get('case_id', '?')} 的 expected_diagnosis 为空（测试将无法断言）")

    for f in FAQ:
        if not f.get("question") or not f.get("answer"):
            problems.append(f"{f.get('faq_id', '?')} 问题或答案为空")

    return problems


CASES_HEADER = [
    "case_id", "tenant_profile", "skill", "scenario", "symptom",
    "expected_diagnosis", "expected_action", "expected_verify_result",
    "risk_level", "source_ref",
]
FAQ_HEADER = ["faq_id", "category", "question", "answer", "keywords"]
