"""种子数据装载：租户、坐席、终端用户、订阅、案例入库。

规模按任务书要求：2 租户 × 3 坐席 × 10 用户。
装载后自动构建向量索引，使系统开箱即可演示与测试。

幂等：重复执行不会产生重复数据（按业务键判断存在性）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.core.database import write_session
from app.core.logging_config import get_logger, timed_step
from app.models import Agent, AgentSkill, Case, EndUser, Subscription, Tenant
from app.security import hash_password

log = get_logger(__name__)

# ===========================================================================
# 租户定义（两个租户，用于验证跨租户隔离）
# ===========================================================================
TENANTS = [
    {
        "tenant_key": "t-001",
        "name": "星桥科技（标准版）",
        "tier": "standard",
        "plan": "team",
        "seats": 25,
        "complaint_count": 1,
        "auto_refund_enabled": True,
        "notes": "标准租户，用于主流程演示与测试",
    },
    {
        "tenant_key": "t-002",
        "name": "远洋数字（旗舰版）",
        "tier": "vip",
        "plan": "enterprise",
        "seats": 120,
        "complaint_count": 0,
        "auto_refund_enabled": True,
        "notes": "VIP 租户，用于优先级与隔离验证",
    },
]

# ===========================================================================
# 坐席：每租户 3 名（1 组长 + 2 坐席），带技能组
# ===========================================================================
AGENTS = [
    {"username": "lead_chen", "display_name": "陈组长", "role": "lead", "skills": ["integration", "permission", "billing", "account", "faq"]},
    {"username": "agent_lin", "display_name": "林工", "role": "agent", "skills": ["integration", "permission", "faq"]},
    {"username": "agent_zhao", "display_name": "赵工", "role": "agent", "skills": ["billing", "account", "faq"]},
]

# ===========================================================================
# 终端用户：每租户 10 名
# ===========================================================================
_USER_TEMPLATES = [
    ("u-42", "王敏", "wangmin@example.com", "gold", 9, 1, "ws-1001"),
    ("u-43", "李强", "liqiang@example.com", "silver", 4, 0, "ws-1002"),
    ("u-44", "张伟", "zhangwei@example.com", "basic", 2, 0, "ws-1004"),
    ("u-45", "刘洋", "liuyang@example.com", "basic", 26, 0, "ws-1001"),
    ("u-46", "陈静", "chenjing@example.com", "gold", 14, 2, "ws-1003"),
    ("u-47", "杨帆", "yangfan@example.com", "basic", 1, 0, "ws-1002"),
    ("u-48", "周雪", "zhouxue@example.com", "silver", 7, 0, "ws-1004"),
    ("u-49", "吴涛", "wutao@example.com", "basic", 3, 1, "ws-1001"),
    ("u-50", "郑琳", "zhenglin@example.com", "platinum", 31, 0, "ws-1003"),
    ("u-51", "孙浩", "sunhao@example.com", "basic", 5, 0, "ws-1002"),
]

_ACCOUNTS = [
    ("acct-501", "team", 25, 9, "gold", 1),
    ("acct-502", "starter", 3, 2, "basic", 0),
    ("acct-503", "team", 12, 4, "silver", 4),
    ("acct-504", "enterprise", 120, 26, "platinum", 0),
]

DEFAULT_AGENT_PASSWORD = "Agent@12345"


class SeedService:
    """种子数据装载。"""

    def run(self, *, with_index: bool = True) -> dict[str, Any]:
        """装载种子数据。

        **必须按租户绑定上下文**：坐席/终端用户/订阅这几张表都走租户闸门，
        未绑定租户上下文时会被直接拒绝（表现为 `seed.run` 报
        ``TenantContextMissing``，而种子数据其实一条都没装进去——
        本项目实测踩到：从 CLI 或启动脚本执行 seed 时全量失败）。
        租户表本身（tenants）与案例库无租户闸门，放在绑定之外执行。
        """
        from app.core.trace_context import bind

        with timed_step("seed.run"):
            result: dict[str, Any] = {}
            result["tenants"] = self._seed_tenants()
            agents = users = subs = 0
            for tenant in TENANTS:
                tid = tenant["tenant_key"]
                with bind(tenant_id=tid, trace_id="seed"):
                    agents += self._seed_agents(tid)
                    users += self._seed_users(tid)
                    subs += self._seed_subscriptions(tid)
            result["agents"] = agents
            result["users"] = users
            result["subscriptions"] = subs
            result["cases"] = self._seed_cases()
            if with_index:
                result["index"] = self._build_index()
            log.info("seed.done", **{k: v for k, v in result.items() if isinstance(v, int)})
            return result

    # ------------------------------------------------------------------
    def _seed_tenants(self) -> int:
        count = 0
        with write_session() as s:
            for spec in TENANTS:
                exists = s.execute(
                    select(Tenant).where(Tenant.tenant_key == spec["tenant_key"])
                ).scalar_one_or_none()
                if exists:
                    continue
                s.add(Tenant(**spec))
                count += 1
        return count

    def _seed_agents(self, tid: str) -> int:
        count = 0
        with write_session() as s:
            for spec in AGENTS:
                exists = s.execute(
                    select(Agent).where(Agent.tenant_id == tid, Agent.username == spec["username"])
                ).scalar_one_or_none()
                if exists:
                    agent = exists
                else:
                    agent = Agent(
                        tenant_id=tid,
                        username=spec["username"],
                        display_name=spec["display_name"],
                        password_hash=hash_password(DEFAULT_AGENT_PASSWORD),
                        role=spec["role"],
                        presence="offline",
                    )
                    s.add(agent)
                    s.flush()
                    count += 1
                # 技能组
                for skill in spec["skills"]:
                    has = s.execute(
                        select(AgentSkill).where(
                            AgentSkill.tenant_id == tid,
                            AgentSkill.agent_id == agent.id,
                            AgentSkill.skill == skill,
                        )
                    ).scalar_one_or_none()
                    if not has:
                        s.add(
                            AgentSkill(
                                tenant_id=tid,
                                agent_id=agent.id,
                                skill=skill,
                                proficiency=5 if spec["role"] == "lead" else 3,
                            )
                        )
        return count

    def _seed_users(self, tid: str) -> int:
        count = 0
        suffix = "" if tid == "t-001" else "-b"
        with write_session() as s:
            for ext, name, email, tier, tenure, complaints, ws in _USER_TEMPLATES:
                ext_id = f"{ext}{suffix}"
                exists = s.execute(
                    select(EndUser).where(EndUser.tenant_id == tid, EndUser.external_id == ext_id)
                ).scalar_one_or_none()
                if exists:
                    continue
                s.add(
                    EndUser(
                        tenant_id=tid,
                        external_id=ext_id,
                        name=name,
                        email=email,
                        user_tier=tier,
                        workspace_id=ws,
                        tenure_months=tenure,
                        complaint_count=complaints,
                    )
                )
                count += 1
        return count

    def _seed_subscriptions(self, tid: str) -> int:
        count = 0
        suffix = "" if tid == "t-001" else "-b"
        with write_session() as s:
            for acct, plan, seats, tenure, tier, complaints in _ACCOUNTS:
                acct_id = f"{acct}{suffix}"
                exists = s.execute(
                    select(Subscription).where(
                        Subscription.tenant_id == tid, Subscription.account_id == acct_id
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                s.add(
                    Subscription(
                        tenant_id=tid,
                        account_id=acct_id,
                        plan=plan,
                        seats=seats,
                        tenure_months=tenure,
                        user_tier=tier,
                        open_complaints=complaints,
                    )
                )
                count += 1
        return count

    def _seed_cases(self) -> int:
        """把案例导入数据库（共享参考知识，不按租户重复存储）。"""
        from app.data import samples as S

        count = 0
        with write_session() as s:
            for c in S.CASES:
                exists = s.execute(
                    select(Case).where(Case.case_id == c["case_id"])
                ).scalar_one_or_none()
                if exists:
                    continue
                s.add(Case(**c))
                count += 1
        return count

    def _build_index(self) -> dict[str, Any]:
        from app.services.knowledge_service import KnowledgeService

        return KnowledgeService().rebuild(force=True)
