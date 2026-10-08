"""异常与降级专项测试。

对应容错降级矩阵（架构文档 §8）的每一行：
  - Mock 超时 / 上游 5xx → 重试后转人工 + 建工单
  - 工具返回格式异常 → 校验拦下并走兜底
  - 鉴权失败 → 不重试，走重新授权降级
  - RAG 空检索 → 兜底话术 + 低置信度
  - LLM 不可用 → 友好提示 + 转人工
  - 从库不可用 → 读回退主库 + 告警
  - Redis 不可用 → 限流放行并告警（不阻断业务）
  - 服务重启 → 状态存储续接
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.tools.client import ToolClient, ToolError


def _mode_client(mode: str) -> ToolClient:
    return ToolClient(default_headers={"X-Mock-Mode": mode})


class TestToolDegradation:
    """工具层降级（直接调用工具，验证异常归类）。"""

    @pytest.mark.exceptions
    def test_upstream_error_is_retryable(self):
        from app.tools import business as T

        with pytest.raises(ToolError) as exc:
            T.query_subscription("acct-501", client=_mode_client("error"))
        assert exc.value.kind == "upstream"
        assert exc.value.retryable is True

    @pytest.mark.exceptions
    def test_unauthorized_is_not_retryable(self):
        from app.tools import business as T

        with pytest.raises(ToolError) as exc:
            T.query_subscription("acct-501", client=_mode_client("unauthorized"))
        assert exc.value.kind == "unauthorized"
        assert exc.value.retryable is False, "鉴权失败不应重试"

    @pytest.mark.exceptions
    def test_bad_data_caught_by_schema_validation(self):
        """返回结构不符合契约时必须被 Pydantic 拦下，不能带脏数据进诊断。"""
        from app.tools import business as T

        with pytest.raises(ToolError) as exc:
            T.query_subscription("acct-501", client=_mode_client("bad_data"))
        assert exc.value.kind == "bad_data"
        assert exc.value.code == "TOOL_SCHEMA_INVALID"

    @pytest.mark.exceptions
    def test_transient_failure_succeeds_after_retry(self):
        """前 N 次失败后应重试成功（重试策略生效）。"""
        from app.tools import business as T

        client = ToolClient(default_headers={"X-Mock-Fail-N": "2"})
        result = T.query_subscription("acct-504", client=client)
        assert result.account_id == "acct-504"
        assert any(c.retry_count > 0 for c in client.calls), "应记录重试次数"

    @pytest.mark.exceptions
    def test_timeout_classified(self):
        """超时应归类为超时/上游错误，且可重试。"""
        from app.tools import business as T

        with pytest.raises(ToolError) as exc:
            T.query_subscription("acct-501", client=_mode_client("timeout"))
        assert exc.value.kind in {"timeout", "upstream"}

    @pytest.mark.exceptions
    def test_failures_are_recorded(self):
        """失败调用也要留痕（否则线上无法排查）。"""
        from app.tools import business as T

        client = _mode_client("error")
        with pytest.raises(ToolError):
            T.query_subscription("acct-501", client=client)
        assert client.calls, "失败调用应被记录"
        assert client.calls[-1].ok is False

    @pytest.mark.exceptions
    def test_stats_reports_failures(self):
        from app.tools import business as T

        client = _mode_client("error")
        with pytest.raises(ToolError):
            T.query_subscription("acct-501", client=client)
        stats = client.stats()
        assert stats["failed"] >= 1


class TestSkillDegradation:
    """技能层降级：工具失败时不能崩，应给出兜底话术并转人工。"""

    @pytest.mark.exceptions
    def test_integration_query_failure_escalates(self, api, headers, session_key, monkeypatch):
        from app.mocks import data as mock_data
        from app.skills.integration import IntegrationSkill

        # 让集成技能使用带异常模式的客户端
        original_init = IntegrationSkill.__init__

        def patched_init(self, client=None):
            original_init(self, client=ToolClient(default_headers={"X-Mock-Mode": "error"}))

        monkeypatch.setattr(IntegrationSkill, "__init__", patched_init)
        try:
            r = api.post(
                "/api/v1/chat",
                headers=headers,
                json={"session_key": session_key, "message": "工作区 ws-1001 的机器人不回复消息"},
            )
            body = r.json()
            assert r.status_code == 200
            assert body["escalate"] is True, "工具连续失败后应转人工"
            assert body["degraded"] is True or body["cause_code"] == "QUERY_FAILED"
            assert body["reply"], "必须给用户兜底话术"
        finally:
            monkeypatch.undo()
            mock_data.reset_runtime_state()

    @pytest.mark.exceptions
    def test_llm_unavailable_falls_back(self, api, headers, session_key, monkeypatch):
        """模型不可用时：FAQ 走检索原文兜底，接口不 500。"""
        import app.core.llm_client as llm_mod

        class _Boom:
            def invoke(self, *a, **k):
                raise llm_mod.LLMUnavailable("模拟模型故障", kind="timeout")

            def structured(self, *a, **k):
                raise llm_mod.LLMUnavailable("模拟模型故障", kind="timeout")

        monkeypatch.setattr(llm_mod, "get_llm_client", lambda refresh=False: _Boom())
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "怎么设置消息免打扰？"},
        )
        assert r.status_code == 200, "模型故障不应导致接口 500"
        body = r.json()
        assert body["reply"].strip(), "应有兜底回答"
        assert body["degraded"] is True or body["escalate"] is True

    @pytest.mark.exceptions
    def test_empty_retrieval_falls_back_with_low_confidence(self, api, headers, session_key):
        """RAG 空检索 → 兜底话术 + 标记低置信度。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "请解释一下量子纠缠与客服系统的关系"},
        )
        body = r.json()
        assert r.status_code == 200
        assert body["reply"].strip()
        # 检索不到时应带降级标记或给出转人工/兜底引导
        assert body["degraded"] is True or body["escalate"] is True or body["confidence"] < 0.5

    @pytest.mark.exceptions
    def test_agent_unavailable_returns_friendly_message(self, api, headers, session_key, monkeypatch):
        """诊断服务整体不可用 → 友好提示 + 转人工，不暴露堆栈。"""
        from app.services.agent_service import AgentUnavailable, get_agent_service

        async def boom(*a, **k):
            raise AgentUnavailable("模拟图执行失败")

        monkeypatch.setattr(get_agent_service(), "resume_after_restart", boom)
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1001 机器人不回复"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["escalate"] is True
        assert body["degraded"] is True
        assert "人工" in body["reply"]
        assert "Traceback" not in body["reply"]
        assert "模拟图执行失败" not in body["reply"], "不应把内部异常细节暴露给用户"


class TestInfraDegradation:
    """基础设施降级。"""

    @pytest.mark.exceptions
    def test_replica_failure_falls_back_to_primary(self, tenant, monkeypatch):
        """从库不可用 → 只读查询回退主库，业务不中断。"""
        from sqlalchemy import text

        from app.core import database
        from app.core.config import settings as cfg

        # 把从库端口指向一个不存在的端口，模拟从库宕机
        broken = cfg.replica_dsn.replace(f":{cfg.pg_replica_port}/", ":5999/")
        monkeypatch.setattr(type(cfg), "replica_dsn", property(lambda self: broken))
        monkeypatch.setattr(database, "_replica_engine", None)
        monkeypatch.setattr(database, "_replica_sm", None, raising=False)
        monkeypatch.setattr(database, "_replica_sessionmaker", None)
        monkeypatch.setattr(database, "_replica_failed_until", 0.0)

        with database.read_session() as s:
            fallback = s.info.get("read_fallback", False)
            assert s.execute(text("SELECT 1")).scalar() == 1
        assert fallback is True, "从库不可用时应回退主库并标记"

        # 复原，避免影响后续测试
        monkeypatch.setattr(database, "_replica_engine", None)
        monkeypatch.setattr(database, "_replica_sessionmaker", None)
        monkeypatch.setattr(database, "_replica_failed_until", 0.0)

    @pytest.mark.exceptions
    def test_rate_limit_allows_when_redis_unavailable(self, monkeypatch):
        """Redis 不可用时限流应放行并告警，而不是让业务全面不可用。"""
        from app.core import redis_client

        def boom(*a, **k):
            raise RuntimeError("redis down")

        monkeypatch.setattr(redis_client.get_redis(), "eval", boom)
        allowed, remaining = redis_client.token_bucket_allow("cs:rate:probe", 1, 60)
        assert allowed is True, "限流依赖不可用时应放行"
        assert remaining == -1, "应返回哨兵值表示未实际计数"

    @pytest.mark.exceptions
    def test_rate_limit_blocks_over_capacity(self, tenant):
        """令牌桶：超出容量应被拒绝。"""
        from app.core.redis_client import get_redis, token_bucket_allow

        r = get_redis()
        r.delete("cs:rate:probe2")
        results = [token_bucket_allow("cs:rate:probe2", 3, 60)[0] for _ in range(4)]
        assert results[:3] == [True, True, True]
        assert results[3] is False, "第 4 次应被限流"

    @pytest.mark.exceptions
    def test_nonce_replay_blocked(self, tenant):
        """防重放：同一 nonce 第二次必须被拒。"""
        from app.core.redis_client import get_redis, nonce_consume

        get_redis().delete("cs:nonce:probe")
        assert nonce_consume("probe", 600) is True
        assert nonce_consume("probe", 600) is False

    @pytest.mark.exceptions
    def test_distributed_lock_mutual_exclusion(self, tenant):
        """分布式锁：同一时刻只有一个持有者。"""
        from app.core.redis_client import distributed_lock

        with distributed_lock("cs:lock:probe", ttl_seconds=10) as first:
            assert first is not None
            with distributed_lock("cs:lock:probe", ttl_seconds=10) as second:
                assert second is None, "锁被持有时不应再次获取成功"
        with distributed_lock("cs:lock:probe", ttl_seconds=10) as third:
            assert third is not None, "释放后应可再次获取"


class TestStatePersistence:
    """状态存储与跨进程续接。"""

    @pytest.mark.exceptions
    def test_graph_state_persisted_to_postgres(self, api, headers, session_key, tenant):
        """诊断后状态应落在 PostgreSQL（而非仅内存）。"""
        import psycopg


        api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1001 机器人不回复"},
        )
        thread_id = f"t-001:{session_key}"
        with psycopg.connect(settings.checkpointer_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,))
                count = int(cur.fetchone()[0])
        assert count > 0, "状态应已持久化到数据库"

        # 清理该线程的检查点：避免遗留状态影响后续用例在同一 thread_id 上的行为
        with psycopg.connect(settings.checkpointer_dsn, autocommit=True) as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                try:
                    conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
                except Exception:  # noqa: BLE001
                    pass

    @pytest.mark.exceptions
    def test_clarify_suspends_then_resumes_same_session(self, api, headers, session_key):
        """澄清挂起 → 同一会话补充信息后continue（用户无需重述）。"""
        # 第一轮：信息不足 → 挂起
        r1 = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "我的机器人不回复消息了"},
        )
        b1 = r1.json()
        assert b1["pending_clarify"] is not None

        # 第二轮：补充工作区 → 应接着排查并给出结论
        r2 = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": session_key, "message": "工作区 ws-1001"},
        )
        b2 = r2.json()
        assert r2.status_code == 200
        assert b2["cause_code"], "续接后应产出诊断结论"
        assert b2["pending_clarify"] is None, "信息补全后不应再次挂起"

    @pytest.mark.exceptions
    def test_invalid_state_transition_rejected(self, tenant):
        """非法状态跃迁必须被拒绝（防止 CLOSED 被改回 AI_HANDLING）。"""
        from app.services.session_service import InvalidTransition, get_session_service

        sessions = get_session_service()
        key = "probe-transition"
        sessions.get_or_create(tenant_id="t-001", session_key=key)
        sessions.close(tenant_id="t-001", session_key=key, reason="测试")
        with pytest.raises(InvalidTransition):
            sessions.transition(
                tenant_id="t-001", session_key=key, to_state="AI_HANDLING", reason="非法回退"
            )
