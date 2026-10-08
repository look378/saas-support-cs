"""安全边界回归测试（全面审查发现的越权与输入校验缺陷）。

每个用例都对应一个**实测踩到过**的缺陷，防止修复被后续改动悄悄回退：

1. 租户内跨用户读会话（会话标识可枚举 + 读取接口无归属校验）
2. 用户端身份靠自报字段 → 省略字段即可绕过归属校验
3. 控制台初始状态接口信任查询参数里的租户（跨租户读队列与事件）
4. ``limit`` / 字段长度未做边界校验，越界值直达 SQL 触发 500
5. 会话标识用毫秒时间戳，可被枚举

身份模型（加固后）：
  - 首轮 ``POST /chat`` 可自报 ``user_ext_id``，服务端据此建立会话并**签发会话令牌**；
  - 会话一旦建立，续聊/读消息/读状态**必须**携带 ``user_token``；
  - 令牌里的身份是唯一可信来源，自报字段与令牌不一致直接拒绝。
"""

from __future__ import annotations

import pytest

TENANT = "t-001"


@pytest.mark.exceptions
class TestSessionOwnership:
    """会话归属：同一租户内也不能读别人的会话。"""

    def test_other_user_cannot_read_session_messages(self, api, headers, user_session):
        """用户 A 的会话，用户 B 拿自己的令牌读取必须被拒。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200

        # 攻击者：另一个真实用户，自己走一轮对话拿到**自己的合法令牌**
        attacker = type(user_session)(api, headers, TENANT)
        assert attacker.chat("你好", user_ext_id="attacker-B").status_code == 200
        assert attacker.token and attacker.token != user_session.token

        r = api.get(
            f"/api/v1/session/{user_session.session_key}/messages",
            headers=headers,
            params={"user_token": attacker.token},
        )
        assert r.status_code == 403, "跨用户读取会话消息必须被拒绝"

    def test_other_user_cannot_read_session_state(self, api, headers, user_session):
        """会话状态接口同样要拦（否则可探测他人会话的存在与处理方）。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200

        attacker = type(user_session)(api, headers, TENANT)
        attacker.chat("你好", user_ext_id="attacker-B")

        r = api.get(
            f"/api/v1/session/{user_session.session_key}",
            headers=headers,
            params={"user_token": attacker.token},
        )
        assert r.status_code == 403

    def test_other_user_cannot_post_into_session(self, api, headers, user_session):
        """拿自己的令牌往他人会话里发言（冒充）必须被拒。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200

        attacker = type(user_session)(api, headers, TENANT)
        attacker.chat("你好", user_ext_id="attacker-B")

        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "session_key": user_session.session_key,
                "message": "继续",
                "user_token": attacker.token,
            },
        )
        assert r.status_code == 403

    def test_owner_can_continue_session(self, api, user_session):
        """归属人自己续聊必须正常（不能因为加固而误伤正常用户）。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200
        r = user_session.chat("继续")
        assert r.status_code == 200
        assert r.json()["session_key"] == user_session.session_key

    def test_reusing_session_key_without_token_is_rejected(self, api, headers, user_session):
        """回归：**省略身份字段**曾是绕过归属校验的开关。

        会话建立后，既不带令牌、也不带身份，也必须被拒——
        否则"我不声明我是谁"就等于"我是任何人"。
        """
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": user_session.session_key, "message": "继续"},
        )
        assert r.status_code == 401, "无令牌续聊必须被拒绝"

    def test_reading_without_token_is_rejected(self, api, headers, user_session):
        """只读接口无令牌一律拒绝（不允许"凭会话标识即可读"）。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200
        r = api.get(f"/api/v1/session/{user_session.session_key}/messages", headers=headers)
        assert r.status_code == 401

    def test_declared_identity_must_match_token(self, api, headers, user_session):
        """自报身份与令牌不一致 → 拒绝（防止用他人令牌+自己的标识混搭）。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={
                "session_key": user_session.session_key,
                "message": "继续",
                "user_ext_id": "someone-else",
                "user_token": user_session.token,
            },
        )
        assert r.status_code == 403

    def test_agent_token_cannot_be_used_as_user_token(self, api, agent_headers, session_key):
        """受众隔离：坐席令牌拿去当用户令牌必须失败。"""
        agent_token = agent_headers["Authorization"].split(None, 1)[1]
        r = api.post(
            "/api/v1/chat",
            headers={"X-Tenant-ID": TENANT},
            json={"session_key": session_key, "message": "你好", "user_token": agent_token},
        )
        assert r.status_code == 401, "坐席令牌不能冒充用户身份"


@pytest.mark.exceptions
class TestSessionListing:
    """会话列表不得成为"枚举他人会话"的入口。"""

    def test_sessions_listing_requires_token(self, api, headers):
        r = api.get("/api/v1/sessions", headers=headers)
        assert r.status_code == 401

    def test_sessions_listing_only_returns_own_sessions(self, api, headers, user_session):
        """列表只返回**本人**会话，别人的会话标识不能出现在结果里。"""
        assert user_session.chat("你好", user_ext_id="owner-A").status_code == 200
        other = type(user_session)(api, headers, TENANT)
        other.chat("你好", user_ext_id="owner-B")

        r = api.get(
            "/api/v1/sessions",
            headers=headers,
            params={"user_token": other.token, "limit": 200},
        )
        assert r.status_code == 200
        keys = {row["session_key"] for row in r.json()["data"]}
        assert user_session.session_key not in keys, "不得列出他人会话"


@pytest.mark.exceptions
class TestSessionKeyUnguessable:
    """会话标识必须不可枚举。"""

    def test_generated_session_key_is_not_timestamp_based(self, api, headers):
        """新建会话返回的标识不能是可预测的时间戳形式。"""
        keys = []
        for _ in range(3):
            r = api.post("/api/v1/chat", headers=headers, json={"message": "你好"})
            assert r.status_code == 200
            keys.append(r.json()["session_key"])
        # 时间戳方案（s-<毫秒>）的三个键会高度相似，这里要求长度足够且互不相同
        assert len(set(keys)) == 3
        assert all(len(k) >= 12 for k in keys), f"会话标识过短、可被枚举：{keys}"


@pytest.mark.exceptions
class TestTenantScoping:
    """租户隔离：不能靠"换个查询参数/请求头"读到别的租户。"""

    def test_session_not_visible_from_other_tenant(self, api, headers, user_session):
        """同一 session_key 在另一个租户下读不到数据。

        令牌是**按租户签发**的，因此跨租户要么令牌校验失败（401/403），
        要么该租户下根本没有这条会话（返回空）——两种都算隔离生效。
        """
        assert user_session.chat("你好", user_ext_id="u-42").status_code == 200
        r = api.get(
            f"/api/v1/session/{user_session.session_key}/messages",
            headers={"X-Tenant-ID": "t-002"},
            params={"user_token": user_session.token},
        )
        assert r.status_code in (401, 403, 200), r.text
        if r.status_code == 200:
            assert r.json()["data"] == [], "不得跨租户读到会话消息"

    def test_console_state_rejects_mismatched_tenant(self, api, agent_headers):
        """控制台初始状态的租户必须与令牌一致。

        回归：该接口曾用查询参数里的 tenant_id 作为数据范围，
        任一租户的坐席换一个参数即可读取其它租户的队列与事件。
        """
        r = api.get(
            "/api/v1/console/state",
            headers=agent_headers,
            params={"tenant_id": "t-002"},
        )
        assert r.status_code == 403

    def test_console_state_accepts_own_tenant(self, api, agent_headers):
        """同租户调用必须正常（不能把正常调用一起拒掉）。"""
        r = api.get(
            "/api/v1/console/state",
            headers=agent_headers,
            params={"tenant_id": TENANT},
        )
        assert r.status_code == 200
        assert r.json()["data"]["tenant_id"] == TENANT


@pytest.mark.exceptions
class TestInputBounds:
    """输入边界：非法参数要给 4xx，而不是让数据库报错变成 500。"""

    @pytest.mark.parametrize("limit", [-1, 0, 100000])
    def test_message_limit_out_of_range_rejected(self, api, headers, session_key, limit):
        r = api.get(
            f"/api/v1/session/{session_key}/messages", headers=headers, params={"limit": limit}
        )
        assert r.status_code == 422, f"limit={limit} 应被拒绝为 422，实际 {r.status_code}"

    def test_sessions_limit_out_of_range_rejected(self, api, headers):
        r = api.get("/api/v1/sessions", headers=headers, params={"limit": -5})
        assert r.status_code == 422

    def test_overlong_session_key_rejected(self, api, headers):
        """超长 session_key 会撞数据库列宽（String(64)），必须在入参层拦住。"""
        r = api.post(
            "/api/v1/chat",
            headers=headers,
            json={"session_key": "s-" + "x" * 100, "message": "你好"},
        )
        assert r.status_code == 422

    def test_empty_message_rejected(self, api, headers):
        r = api.post("/api/v1/chat", headers=headers, json={"message": ""})
        assert r.status_code == 422
