"""集成测试：转人工全流程与存储层能力。

覆盖任务书验收标准：
  - 转人工全流程（AI 触发 → 队列 → 派单 → 坐席实时收到 → 接管 → 对话 → 办结）
  - 坐席掉线 → 心跳过期 → 会话自动重排队
  - 已被接管后 AI 不再介入
  - 同一会话只能一个处理方（接管互斥）
  - 办结回流知识库
  - 读写分离与租户隔离
  - 计费幂等（同 key 不重复退款）
"""

from __future__ import annotations

import pytest


@pytest.fixture
def lead_headers(api, headers):
    r = api.post(
        "/api/v1/agent/login",
        headers=headers,
        json={"username": "lead_chen", "password": "Agent@12345"},
    )
    assert r.status_code == 200
    return {**headers, "Authorization": f"Bearer {r.json()['token']}"}


def _escalate_a_session(api, headers, session_key: str) -> tuple[dict, str]:
    """制造一个必然转人工的会话（大额计费争议）。

    返回 ``(响应体, 会话令牌)``：本类用例后续还要读会话状态，
    而用户端接口要求**凭令牌访问**（首轮响应里签发），因此这里把令牌一并带出。
    """
    r = api.post(
        "/api/v1/chat",
        headers=headers,
        json={
            "session_key": session_key,
            "message": "账号 acct-501 账单重复计费，涉及 2380 元，要求退款",
            "user_ext_id": "u-42",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["escalate"] is True, f"未触发转人工：{body}"
    assert body["user_token"], "首轮对话应签发会话令牌"
    return body, body["user_token"]


class TestEscalationFlow:
    """转人工全流程。"""

    @pytest.mark.integration
    def test_full_chain_from_ai_to_agent_close(self, api, headers, agent_headers, session_key):
        """AI 触发 → 派单 → 接管 → 对话 → 办结，全链路贯通。"""
        body, user_token = _escalate_a_session(api, headers, session_key)
        assert body["escalation"]["request_no"]

        # 坐席上报心跳后才有派单资格
        r = api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "online"})
        assert r.status_code == 200

        # 队列中能看到
        r = api.get("/api/v1/agent/queue", headers=agent_headers)
        assert r.status_code == 200
        assert any(i["session_key"] == session_key for i in r.json()["data"])

        # 派单
        from app.core.trace_context import ensure_tenant
        from app.services.escalation_service import get_escalation_service

        with ensure_tenant("t-001"):
            dispatch = get_escalation_service().dispatch_one(tenant_id="t-001")
        assert dispatch and dispatch["assigned"] is True

        # 接管
        r = api.post("/api/v1/agent/claim", headers=agent_headers, json={"session_key": session_key})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["state"] == "AGENT_HANDLING"

        # AI 停止介入
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "session_key": session_key,
                "message": "请问进度如何？",
                "user_token": user_token,
            },
        )
        after = r.json()
        assert after["handler"] == "agent_zhao"
        assert after["skill"] == ""
        assert "转交" in after["reply"] or "人工" in after["reply"]

        # 坐席对话
        r = api.post(
            "/api/v1/agent/message",
            headers=agent_headers,
            json={"session_key": session_key, "content": "已核对，重复部分将退款。"},
        )
        assert r.status_code == 200

        # 办结 + 回流
        r = api.post(
            "/api/v1/agent/close",
            headers=agent_headers,
            json={
                "session_key": session_key,
                "resolution": "确认重复计费，已退款 380 元",
                "skill": "billing",
                "diagnosis_code": "BILLING_DISPUTE_CONFIRMED",
                "fix_action": "refund",
                "automatable": True,
                "promote_to_kb": True,
            },
        )
        assert r.status_code == 200
        closed = r.json()["data"]
        assert closed["closed"] is True
        assert closed["promoted"] is True
        assert closed["feedback_id"] is not None

        # 状态终态（用户端读取需令牌）
        r = api.get(
            f"/api/v1/session/{session_key}",
            headers=headers,
            params={"user_token": user_token, "user_ext_id": "u-42"},
        )
        assert r.json()["data"]["redis"]["state"] == "CLOSED"

    @pytest.mark.integration
    def test_second_agent_cannot_claim(self, api, headers, agent_headers, lead_headers, session_key):
        """同一会话只能被一个坐席持有（接管互斥）。"""
        _escalate_a_session(api, headers, session_key)
        api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "online"})
        api.post("/api/v1/agent/heartbeat", headers=lead_headers, json={"presence": "online"})

        r = api.post("/api/v1/agent/claim", headers=agent_headers, json={"session_key": session_key})
        assert r.status_code == 200

        # 第二名坐席接管应被拒绝（409）
        r = api.post("/api/v1/agent/claim", headers=lead_headers, json={"session_key": session_key})
        assert r.status_code == 409, "已被接管的会话不应被抢占"

    @pytest.mark.integration
    def test_agent_not_owner_cannot_speak(self, api, headers, agent_headers, lead_headers, session_key):
        """非持有者不能在会话中发言。"""
        _escalate_a_session(api, headers, session_key)
        api.post("/api/v1/agent/heartbeat", headers=lead_headers, json={"presence": "online"})
        api.post("/api/v1/agent/claim", headers=lead_headers, json={"session_key": session_key})

        r = api.post(
        "/api/v1/agent/message",
            headers=agent_headers,
            json={"session_key": session_key, "content": "我不是持有者"},
        )
        assert r.status_code == 409

    @pytest.mark.integration
    def test_agent_offline_requeues_sessions(self, api, headers, agent_headers, session_key):
        """坐席掉线 → 其名下会话重新排队。"""
        _body, user_token = _escalate_a_session(api, headers, session_key)
        api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "online"})
        r = api.post("/api/v1/agent/claim", headers=agent_headers, json={"session_key": session_key})
        assert r.status_code == 200

        # 主动下线（等价于心跳过期）
        r = api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "offline"})
        assert r.status_code == 200
        assert session_key in r.json()["data"]["requeued"]

        # 状态回到排队
        r = api.get(
            f"/api/v1/session/{session_key}",
            headers=headers,
            params={"user_token": user_token, "user_ext_id": "u-42"},
        )
        assert r.json()["data"]["redis"]["state"] == "QUEUED"

    @pytest.mark.integration
    def test_offline_sweep_recovers_passive_disconnect(self, api, headers, agent_headers, session_key, tenant):
        """**被动**掉线（进程崩溃/断网）由巡检兜住。

        回归：主动下线会走心跳接口立即回收，但被动掉线没有任何请求会触发回收，
        会话会永远停在 AGENT_HANDLING——AI 已停止介入、坐席又不在。
        这里模拟"心跳凭空消失"，再调用巡检，验证会话被回收。
        """
        from app.services.escalation_service import sweep_offline_agents
        from app.services.session_service import get_presence_service

        _body, user_token = _escalate_a_session(api, headers, session_key)
        api.post("/api/v1/agent/heartbeat", headers=agent_headers, json={"presence": "online"})
        r = api.post("/api/v1/agent/claim", headers=agent_headers, json={"session_key": session_key})
        assert r.status_code == 200

        # 模拟被动掉线：**直接清掉心跳**（等价于进程崩溃，不发任何请求）
        get_presence_service().set_offline(tenant_id="t-001", agent="agent_zhao")

        result = sweep_offline_agents(tenant_id="t-001")
        assert "agent_zhao" in result["offline"], f"应检出生席已掉线：{result}"
        assert session_key in result["requeued"], f"应回收其名下会话：{result}"

        # 幂等：重复巡检不应重复回收（会话已不在 AGENT_HANDLING）
        again = sweep_offline_agents(tenant_id="t-001")
        assert session_key not in again["requeued"], "巡检必须幂等"

        # 用户侧看到的会话状态应回到排队
        r = api.get(
            f"/api/v1/session/{session_key}",
            headers=headers,
            params={"user_token": user_token, "user_ext_id": "u-42"},
        )
        assert r.json()["data"]["redis"]["state"] == "QUEUED"

    @pytest.mark.integration
    def test_offline_recovery_endpoint(self, api, agent_headers):
        """手动巡检入口可用（定时器被关闭时的兜底）。"""
        r = api.post("/api/v1/agent/offline-recovery", headers=agent_headers)
        assert r.status_code == 200
        data = r.json()["data"]
        assert {"checked", "offline", "requeued"} <= set(data)

    @pytest.mark.integration
    def test_offline_sweep_task_is_started(self):
        """后台巡检任务必须被真正创建（否则"自动回收"只是空话）。

        回归点：光有巡检函数而没人调用，等于没有这个能力。
        这里直接调用启动函数，确认在配置周期 > 0 时返回任务句柄。
        """
        import asyncio

        from app.core.config import settings
        from app.main import _start_offline_sweep

        assert settings.agent_offline_sweep_seconds > 0, "默认应开启自动巡检"

        async def _case() -> None:
            task = _start_offline_sweep()
            try:
                assert task is not None, "周期 > 0 时必须创建巡检任务"
                assert not task.done(), "任务不应立即结束"
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        import contextlib

        asyncio.run(_case())

    @pytest.mark.integration
    def test_high_priority_goes_first(self, api, headers, session_key):
        """高优先级会话先出队（VIP / 高金额）。"""
        from app.core.trace_context import ensure_tenant
        from app.services.session_service import get_escalation_queue

        queue = get_escalation_queue()
        with ensure_tenant("t-001"):
            queue.enqueue(tenant_id="t-001", session_key=f"low-{session_key}", priority=0)
            queue.enqueue(tenant_id="t-001", session_key=f"high-{session_key}", priority=1)
            first = queue.dequeue()
        assert first is not None
        assert first["session_key"] == f"high-{session_key}", "高优先级应先出队"


class TestIdempotency:
    """计费幂等。"""

    @pytest.mark.integration
    def test_same_dispute_not_refunded_twice(self, api, headers, session_key):
        """同一诉求重复提交不得重复退款。"""
        message = "账号 acct-502 的用量超限费用 42 元不对，申请退款"
        first = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": message, "user_ext_id": "u-42"},
        ).json()
        assert first["decision"] in {"auto_adjust", "auto_adjust_audit"}
        assert first["verify_ok"] is True
        token = first["user_token"]
        assert token, "首轮对话应签发会话令牌"

        # 同一会话再次提交同一诉求 → 幂等命中（续聊需凭令牌）
        second = api.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "session_key": session_key,
                "message": message,
                "user_ext_id": "u-42",
                "user_token": token,
            },
        ).json()
        assert second["decision"] == "idempotent", f"应命中幂等，实际 {second['decision']}"
        assert any(a["action"] == "idempotent_hit" for a in second["actions"])
        assert "重复" in second["reply"]


class TestStorageIsolation:
    """存储层：租户隔离与读写分离。"""

    @pytest.mark.integration
    def test_tenant_guard_blocks_unbound_access(self):
        """无租户上下文访问租户表必须被拒绝（跨租户防护的硬闸门）。"""
        from sqlalchemy import text

        from app.core.database import (
            TenantContextMissing,
            ensure_models_loaded,
            write_session,
        )

        ensure_models_loaded()
        with pytest.raises(TenantContextMissing), write_session() as s:
            s.execute(text("SELECT count(*) FROM support_sessions"))

    @pytest.mark.integration
    def test_tenant_guard_allows_when_bound(self, tenant):
        from sqlalchemy import text

        from app.core.database import write_session

        with write_session() as s:
            assert s.execute(text("SELECT count(*) FROM support_sessions")).scalar() is not None

    @pytest.mark.integration
    def test_read_session_uses_replica(self, tenant):
        """只读会话应走从库且不触发回退。"""
        from sqlalchemy import text

        from app.core.database import read_session

        with read_session() as s:
            in_recovery = s.execute(text("SELECT pg_is_in_recovery()")).scalar()
            fallback = s.info.get("read_fallback", False)
        assert in_recovery is True, "只读查询应命中从库（处于恢复模式）"
        assert fallback is False, "从库正常时不应回退主库"

    @pytest.mark.integration
    def test_two_tenants_are_isolated(self, api, session_key):
        """跨租户隔离：t-002 即使拿着同一会话标识也读不到 t-001 的数据。"""
        r = api.post(
            "/api/v1/chat",
            headers={"X-Tenant-ID": "t-001"},
            json={"session_key": session_key, "message": "工作区 ws-1001 的机器人不回复了"},
        )
        assert r.status_code == 200
        token = r.json()["user_token"]

        # 令牌是**按租户签发**的：拿到 t-002 去用它必须失败（要么令牌租户不符 401，
        # 要么该租户下没有这个会话——两种都算隔离生效）
        r2 = api.get(
            f"/api/v1/session/{session_key}",
            headers={"X-Tenant-ID": "t-002"},
            params={"user_token": token},
        )
        assert r2.status_code in (200, 401, 403), r2.text
        if r2.status_code == 200:
            data = r2.json()["data"]
            assert data.get("session_key") in (None, "", session_key)
            assert data.get("id") is None or data.get("tenant_id") != "t-001"

    @pytest.mark.integration
    def test_missing_tenant_header_rejected(self, api):
        r = api.post("/api/v1/chat", json={"session_key": "x", "message": "你好"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "TENANT_REQUIRED"

    @pytest.mark.integration
    def test_invalid_tenant_rejected(self, api):
        r = api.post(
            "/api/v1/chat",
            headers={"X-Tenant-ID": "t-not-exist"},
            json={"session_key": "x", "message": "你好"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "TENANT_INVALID"


class TestAgentAuth:
    """坐席端鉴权。"""

    @pytest.mark.integration
    def test_login_ok(self, api, headers):
        r = api.post(
            "/api/v1/agent/login",
            headers=headers,
            json={"username": "agent_lin", "password": "Agent@12345"},
        )
        assert r.status_code == 200
        assert r.json()["skills"], "应返回坐席技能组"

    @pytest.mark.integration
    def test_wrong_password_rejected(self, api, headers):
        r = api.post(
            "/api/v1/agent/login",
            headers=headers,
            json={"username": "agent_lin", "password": "wrong"},
        )
        assert r.status_code == 401

    @pytest.mark.integration
    def test_agent_endpoint_requires_token(self, api, headers):
        r = api.get("/api/v1/agent/queue", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "AGENT_TOKEN_REQUIRED"

    @pytest.mark.integration
    def test_user_token_cannot_access_agent_endpoint(self, api, headers):
        """用户端令牌不能用于坐席接口。

        受众隔离有两层（不同密钥 / ``aud`` 声明），先撞到哪层取决于场景，
        因此这里断言"被拒绝"即可，不绑定具体错误码前缀。
        """
        from app.security import issue_token

        user_token = issue_token(subject="u-42", tenant_id="t-001", audience="user")
        r = api.get(
            "/api/v1/agent/queue",
            headers={**headers, "Authorization": f"Bearer {user_token}"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"].startswith("AGENT_TOKEN_")

    @pytest.mark.integration
    def test_token_tenant_mismatch_rejected(self, api):
        """令牌租户与请求头不一致 → 拒绝（防越权）。"""
        from app.security import issue_token

        token = issue_token(subject="agent_zhao", tenant_id="t-002", audience="agent")
        r = api.get(
            "/api/v1/agent/queue",
            headers={"X-Tenant-ID": "t-001", "Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "TENANT_MISMATCH"


class TestObservability:
    """链路追踪与记录落库。"""

    @pytest.mark.integration
    def test_trace_id_propagated_in_response(self, api, headers, session_key):
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1004 机器人没反应"},
        )
        assert r.headers.get("X-Trace-ID")
        assert r.json()["trace_id"] == r.headers["X-Trace-ID"]

    @pytest.mark.integration
    def test_incoming_trace_id_reused(self, api, headers, session_key):
        """上游传入 trace_id 时应沿用，保证跨系统链路连续。"""
        given = "trace-e2e-abc123"
        r = api.post(
            "/api/v1/chat",
            headers={**headers, "X-Trace-ID": given},
            json={"session_key": session_key, "message": "工作区 ws-1004 机器人没反应"},
        )
        assert r.headers["X-Trace-ID"] == given

    @pytest.mark.integration
    def test_diagnosis_and_steps_persisted(self, api, headers, session_key, tenant):
        """诊断结论与节点轨迹应落库（链路回放数据源）。"""
        from sqlalchemy import select

        from app.core.database import read_session
        from app.models import AiTaskLog, Diagnosis

        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1001 机器人不回复消息"},
        )
        assert r.status_code == 200

        with read_session() as s:
            diagnoses = s.execute(
                select(Diagnosis).where(Diagnosis.tenant_id == "t-001").order_by(Diagnosis.id.desc())
            ).scalars().first()
            assert diagnoses is not None
            logs = s.execute(
                select(AiTaskLog).where(AiTaskLog.tenant_id == "t-001").order_by(AiTaskLog.id.desc())
            ).scalars().all()
        nodes = {log.node for log in logs}
        assert {"route", "observe", "diagnose", "act", "verify", "finalize"} <= nodes
        assert diagnoses.cause_code

    @pytest.mark.integration
    def test_tool_invocations_recorded(self, api, headers, session_key, tenant):
        """工具调用应落库（含 HTTP 状态与耗时）。"""
        from sqlalchemy import select

        from app.core.database import read_session
        from app.models import ToolInvocation

        api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1001 机器人不回复消息"},
        )
        with read_session() as s:
            rows = s.execute(
                select(ToolInvocation)
                .where(ToolInvocation.tenant_id == "t-001")
                .order_by(ToolInvocation.id.desc())
                .limit(10)
            ).scalars().all()
        assert rows, "应有工具调用记录"
        assert any(r.tool_name == "query_integration_config" for r in rows)
        assert any(r.http_status == 200 for r in rows)
