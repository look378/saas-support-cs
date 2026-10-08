"""单元测试：向量化、向量库、安全原语、降级策略判定。

这些是基础能力，必须独立可测（不依赖网络与服务）。
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.core.config import settings
from app.core.embedding import (
    LocalHashingEmbedding,
    cosine_similarity,
    get_embedding_provider,
)


class TestEmbedding:
    """本地向量化。"""

    @pytest.mark.unit
    def test_dimension_matches_config(self):
        provider = get_embedding_provider()
        vec = provider.embed_query("测试文本")
        assert len(vec) == provider.dim == settings.embedding_dim

    @pytest.mark.unit
    def test_deterministic(self):
        provider = LocalHashingEmbedding(dim=256)
        assert provider.embed_query("同一个句子") == provider.embed_query("同一个句子")

    @pytest.mark.unit
    def test_different_text_different_vector(self):
        provider = LocalHashingEmbedding(dim=256)
        assert provider.embed_query("令牌过期") != provider.embed_query("账单争议")

    @pytest.mark.unit
    def test_empty_text_gives_zero_vector(self):
        provider = LocalHashingEmbedding(dim=128)
        vec = provider.embed_query("")
        assert all(v == 0.0 for v in vec)

    @pytest.mark.unit
    def test_vectors_are_l2_normalized(self):
        provider = LocalHashingEmbedding(dim=256)
        vec = provider.embed_query("飞书机器人令牌过期导致消息发不出去")
        norm = sum(v * v for v in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-9

    @pytest.mark.unit
    def test_similar_text_scores_higher_than_unrelated(self):
        provider = LocalHashingEmbedding(dim=512)
        query = provider.embed_query("钉钉机器人 token 过期了怎么恢复")
        related = provider.embed_query("钉钉集成访问令牌已过期，需要刷新令牌")
        unrelated = provider.embed_query("订阅账单重复计费，申请退款")
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)

    @pytest.mark.unit
    def test_batch_embedding_length(self):
        provider = LocalHashingEmbedding(dim=64)
        out = provider.embed_documents(["甲", "乙", "丙"])
        assert len(out) == 3
        assert all(len(v) == 64 for v in out)


class TestCosineSimilarity:
    """余弦相似度边界。"""

    @pytest.mark.unit
    def test_identical_vectors(self):
        assert abs(cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9

    @pytest.mark.unit
    def test_orthogonal_vectors(self):
        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    @pytest.mark.unit
    def test_zero_vector_returns_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestVectorStore:
    """向量库（独立集合，避免污染主知识库）。"""

    @pytest.fixture
    def store(self, tmp_path):
        from app.core.vector_store import VectorStore

        return VectorStore(collection="test_probe", persist_dir=tmp_path / "vs", reset=True)

    @pytest.mark.unit
    def test_upsert_and_search(self, store):
        store.upsert(
            ids=["a", "b"],
            documents=["钉钉机器人令牌过期无法发送消息", "文档分享未开启导致同事打不开"],
            metadatas=[{"kind": "case", "case_id": "a"}, {"kind": "case", "case_id": "b"}],
        )
        hits = store.search("令牌过期发不出消息", top_k=1)
        assert hits and hits[0].metadata["case_id"] == "a"

    @pytest.mark.unit
    def test_count_reflects_upserts(self, store):
        store.upsert(ids=["x"], documents=["内容"], metadatas=[{"kind": "case"}])
        assert store.count == 1

    @pytest.mark.unit
    def test_high_threshold_yields_empty(self, store):
        store.upsert(ids=["y"], documents=["完全无关的内容"], metadatas=[{"kind": "case"}])
        assert store.search("今天天气如何", top_k=3, min_score=0.95) == []

    @pytest.mark.unit
    def test_distance_space_is_cosine(self, store):
        """关键约束：度量必须是余弦，否则分数换算会全错。"""
        assert store.distance_space == "cosine"

    @pytest.mark.unit
    def test_persistence_across_instances(self, tmp_path):
        from app.core.vector_store import VectorStore

        d = tmp_path / "vs2"
        s1 = VectorStore(collection="persist_probe", persist_dir=d, reset=True)
        s1.upsert(ids=["p1"], documents=["持久化测试内容"], metadatas=[{"kind": "case"}])
        s2 = VectorStore(collection="persist_probe", persist_dir=d)
        assert s2.count == 1


class TestSecurity:
    """安全原语。"""

    @pytest.mark.unit
    def test_password_hash_verifies(self):
        from app.security import hash_password, verify_password

        h = hash_password("Agent@12345")
        assert verify_password("Agent@12345", h) is True

    @pytest.mark.unit
    def test_wrong_password_rejected(self):
        from app.security import hash_password, verify_password

        h = hash_password("correct-horse")
        assert verify_password("wrong-password", h) is False

    @pytest.mark.unit
    def test_hash_is_salted(self):
        from app.security import hash_password

        assert hash_password("same") != hash_password("same"), "同一口令两次哈希应不同（含随机盐）"

    @pytest.mark.unit
    def test_token_roundtrip(self):
        from app.security import decode_token, issue_token

        token = issue_token(subject="agent_zhao", tenant_id="t-001", audience="agent", role="agent")
        claims = decode_token(token, audience="agent")
        assert claims.subject == "agent_zhao"
        assert claims.tenant_id == "t-001"
        assert claims.role == "agent"

    @pytest.mark.unit
    def test_audience_isolation(self):
        """用户端令牌不能用于坐席端接口。

        受众隔离有两层：两套密钥不同（签名先失败）与 ``aud`` 声明校验。
        无论哪一层拦下，结论都必须是"拒绝"；这里同时验证两层。
        """
        from app.security import TokenError, decode_token, issue_token

        user_token = issue_token(subject="u-42", tenant_id="t-001", audience="user")
        with pytest.raises(TokenError) as exc:
            decode_token(user_token, audience="agent")
        assert exc.value.kind in {"audience", "invalid"}

    @pytest.mark.unit
    def test_audience_claim_checked_when_secret_shared(self, monkeypatch):
        """即使两套受众共用密钥，``aud`` 声明仍必须拦住跨端使用。"""
        from app.core.config import settings
        from app.security import TokenError, decode_token, issue_token

        monkeypatch.setattr(settings, "jwt_agent_secret", settings.jwt_user_secret)
        user_token = issue_token(subject="u-42", tenant_id="t-001", audience="user")
        with pytest.raises(TokenError) as exc:
            decode_token(user_token, audience="agent")
        assert exc.value.kind == "audience", "共用密钥时 aud 校验必须生效"

    @pytest.mark.unit
    def test_tampered_token_rejected(self):
        from app.security import TokenError, decode_token, issue_token

        token = issue_token(subject="agent_zhao", tenant_id="t-001", audience="agent")
        tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
        with pytest.raises(TokenError):
            decode_token(tampered, audience="agent")

    @pytest.mark.unit
    def test_expired_token_rejected(self):
        from app.security import TokenError, decode_token, issue_token

        token = issue_token(
            subject="agent_zhao", tenant_id="t-001", audience="agent", ttl_minutes=-1
        )
        with pytest.raises(TokenError) as exc:
            decode_token(token, audience="agent")
        assert exc.value.kind == "expired"

    @pytest.mark.unit
    def test_signature_matches_manual_hmac(self):
        """签名必须对同一段字节计算——这是计费写操作能通过校验的前提。"""
        from app.security import billing_signature

        body = b'{"amount": 42.0}'
        ts = str(int(time.time()))
        expected = hmac.new(
            settings.billing_hmac_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        assert billing_signature(body, ts) == expected

    @pytest.mark.unit
    def test_signature_rejects_timestamp_out_of_window(self):
        from app.security import billing_signature, verify_billing_signature

        body = b'{"amount": 1}'
        old_ts = str(int(time.time()) - settings.billing_timestamp_window_seconds - 100)
        ok, reason = verify_billing_signature(body, old_ts, billing_signature(body, old_ts))
        assert ok is False and reason == "TIMESTAMP_OUT_OF_WINDOW"

    @pytest.mark.unit
    def test_signature_rejects_wrong_signature(self):
        from app.security import verify_billing_signature

        ts = str(int(time.time()))
        ok, reason = verify_billing_signature(b"{}", ts, "deadbeef")
        assert ok is False and reason == "SIGNATURE_INVALID"

    @pytest.mark.unit
    def test_signature_rejects_missing_fields(self):
        from app.security import verify_billing_signature

        ok, reason = verify_billing_signature(b"{}", "", "")
        assert ok is False and reason == "SIGNATURE_MISSING"


class TestEscalationTriggerMatrix:
    """转人工触发矩阵。"""

    @pytest.mark.unit
    def test_user_request_triggers(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(user_requested=True)
        assert t.should_escalate and "USER_REQUESTED" in t.codes

    @pytest.mark.unit
    def test_amount_above_threshold_triggers(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(amount=settings.escalate_amount_threshold + 1)
        assert t.should_escalate and "AMOUNT_EXCEEDED" in t.codes
        assert t.priority > 0, "超金额应进高优先级队列"

    @pytest.mark.unit
    def test_amount_below_threshold_does_not_trigger_alone(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(amount=settings.escalate_amount_threshold - 1)
        assert not t.should_escalate

    @pytest.mark.unit
    def test_angry_emotion_triggers(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(emotion="angry", emotion_intensity=settings.escalate_emotion_intensity)
        assert t.should_escalate and "EMOTION_ANGRY" in t.codes

    @pytest.mark.unit
    def test_mild_emotion_does_not_trigger(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(emotion="angry", emotion_intensity=1)
        assert not t.should_escalate

    @pytest.mark.unit
    def test_steps_exceeded_triggers(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(step_count=settings.escalate_max_steps + 1)
        assert t.should_escalate and "STEPS_EXCEEDED" in t.codes

    @pytest.mark.unit
    def test_complaints_exceeded_triggers(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(complaint_count=settings.escalate_complaint_threshold + 1)
        assert t.should_escalate and "COMPLAINTS_EXCEEDED" in t.codes

    @pytest.mark.unit
    def test_vip_raises_priority(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(user_requested=True, tenant_tier="vip")
        assert t.priority > 0

    @pytest.mark.unit
    def test_clean_case_does_not_escalate(self):
        from app.services.escalation_service import evaluate_escalation

        t = evaluate_escalation(step_count=1, emotion="neutral", amount=10.0, complaint_count=0)
        assert not t.should_escalate
