"""pytest 公共夹具。

关键设计：
  1. **进程内 Mock 后端**：测试启动时把 ``httpx.Client`` 替换为直连 Mock 应用的
     适配器。这样测试自包含、不依赖 8010 端口，且请求仍真实经过 Mock 的
     中间件与路由（不是打桩）。
  2. **租户上下文夹具**：数据库有租户闸门，未绑定租户的查询会被拒绝。
     需要直接访问数据库的测试用 ``tenant`` 夹具显式绑定。
  3. **租户数据隔离**：所有测试用租户 t-001，并用带随机后缀的会话键，
     避免相互干扰。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 平台修正必须在任何异步代码之前
from app.core import platform_compat

platform_compat.apply()

TENANT = "t-001"


# ---------------------------------------------------------------------------
# 进程内 Mock 后端：替换 httpx 出站
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _inprocess_mock() -> Iterator[None]:
    """把工具层的 HTTP 出站指向进程内 Mock 应用。"""
    from fastapi.testclient import TestClient

    from app.mocks.server import app as mock_app

    client = TestClient(mock_app)

    class _InProcClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, params=None, json=None, headers=None, content=None):
            path = url.split("8010", 1)[-1]
            return client.request(
                method, path, params=params, json=json, content=content, headers=headers or {}
            )

        def get(self, url):
            return client.get(url.split("8010", 1)[-1])

    import httpx

    original = httpx.Client
    httpx.Client = _InProcClient  # type: ignore[misc]
    yield
    httpx.Client = original  # type: ignore[misc]


@pytest.fixture(scope="session", autouse=True)
def _reset_mock_state(_inprocess_mock: None) -> Iterator[None]:
    """每个测试会话重置 Mock 运行期状态，保证结论可复现。"""
    from app.mocks import data as mock_data

    mock_data.reset_runtime_state()
    yield
    mock_data.reset_runtime_state()


@pytest.fixture(scope="session")
def api():
    """被测应用的 TestClient（不起真实端口）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="session")
def headers() -> dict[str, str]:
    return {"X-Tenant-ID": TENANT}


@pytest.fixture(scope="session", autouse=True)
def _seed_once(_reset_mock_state: None) -> None:
    """会话级装载一次种子数据（幂等）。"""
    from app.core.trace_context import bind
    from app.services.seed_service import SeedService

    with bind(tenant_id=TENANT, trace_id="pytest-seed"):
        SeedService().run(with_index=False)


@pytest.fixture
def tenant() -> Iterator[str]:
    """绑定租户上下文，供直接访问数据库的测试使用。"""
    from app.core.trace_context import bind

    with bind(tenant_id=TENANT, trace_id=f"test-{uuid.uuid4().hex[:8]}"):
        yield TENANT


@pytest.fixture
def session_key() -> str:
    """唯一会话键，避免测试间相互干扰。"""
    return f"t-{uuid.uuid4().hex[:12]}"


class UserSession:
    """用户端会话助手：自动保存并回传会话令牌。

    背景：用户端接口在首轮对话时签发**会话级访客令牌**，此后所有请求
    （续聊 / 读消息 / 读状态 / 流式）都必须携带它——身份以令牌为准，
    不再采信请求体自报的 ``user_ext_id``。
    测试若手工拼装每个请求很容易漏带某一处，因此统一封装：
    正常路径一行调用即可，同时把令牌显式暴露给需要断言的用例。
    """

    def __init__(self, api, headers: dict[str, str], tenant: str) -> None:
        self.api = api
        self.headers = headers
        self.tenant = tenant
        self.session_key = ""
        self.user_ext_id = ""
        self.token = ""

    # ---- 请求构造 ----
    def chat_payload(self, message: str) -> dict[str, object]:
        """对话请求体（带上已知令牌）。"""
        return {
            "session_key": self.session_key,
            "message": message,
            "user_ext_id": self.user_ext_id,
            "user_token": self.token,
        }

    def query(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        """查询参数形式的身份凭据（给 GET 接口用）。"""
        params: dict[str, object] = {"user_ext_id": self.user_ext_id, "user_token": self.token}
        if extra:
            params.update(extra)
        return params

    def adopt(self, body: dict) -> None:
        """从响应里吸收会话标识与令牌。"""
        if body.get("session_key"):
            self.session_key = str(body["session_key"])
        if body.get("user_token"):
            self.token = str(body["user_token"])

    # ---- 便捷调用 ----
    def chat(self, message: str, *, user_ext_id: str | None = None):
        if user_ext_id is not None:
            self.user_ext_id = user_ext_id
        resp = self.api.post("/api/v1/chat", headers=self.headers, json=self.chat_payload(message))
        if resp.status_code == 200:
            self.adopt(resp.json())
        return resp

    def messages(self, **extra: object):
        return self.api.get(
            f"/api/v1/session/{self.session_key}/messages",
            headers=self.headers,
            params=self.query(dict(extra)),
        )

    def state(self, **extra: object):
        return self.api.get(
            f"/api/v1/session/{self.session_key}",
            headers=self.headers,
            params=self.query(dict(extra)),
        )


@pytest.fixture
def user_session(api, headers) -> UserSession:
    """绑定到 t-001 的用户端会话助手（尚未发出任何请求）。"""
    return UserSession(api, headers, TENANT)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """覆盖 pytest 内置 ``tmp_path``，把临时目录固定到工作区内。

    原因：本机的系统临时目录（``%TEMP%``）在受限执行环境里不允许创建子目录，
    pytest 的 ``tmp_path_factory`` 会在创建 ``pytest-of-<user>`` 时直接抛
    ``PermissionError``，于是依赖 ``tmp_path`` 的用例在 setup 阶段就失败——
    失败原因是执行环境，不是被测代码（本项目实测踩到）。

    这里在仓库内建目录；若仍无法创建（受限环境会拒绝某些路径），
    就 **skip 并说明原因**，而不是让用例显示为"代码失败"。
    """
    base = ROOT / ".pytest_work"
    path: Path | None = None
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"case-{uuid.uuid4().hex[:10]}"
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 取决于执行环境
        pytest.skip(f"无法创建临时目录（执行环境限制）：{type(exc).__name__}: {exc}")

    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def agent_headers(api, headers) -> dict[str, str]:
    """登录一名坐席并返回带令牌的请求头。

    放在公共夹具里：多个测试模块都需要坐席身份，
    若各自定义一份，改登录方式时容易漏改某一份。
    """
    r = api.post(
        "/api/v1/agent/login",
        headers=headers,
        json={"username": "agent_zhao", "password": "Agent@12345"},
    )
    assert r.status_code == 200, r.text
    return {**headers, "Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="session", autouse=True)
def _reset_checkpointer(_seed_once: None) -> None:
    """会话开始时清空状态存储。

    状态存储是**按 thread_id 跨进程持久化**的（这是产品特性）。但对测试而言，
    上一轮运行遗留的检查点会让新用例在同一 thread_id 上"续接"旧状态，
    表现为澄清不触发、路由行为异常等难以定位的现象（本项目实测踩到）。
    因此测试开始前必须清空状态表，保证每个用例从干净状态出发。
    """
    import psycopg

    from app.core.config import settings

    try:
        with psycopg.connect(settings.checkpointer_dsn, autocommit=True) as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except Exception:  # noqa: BLE001 - 表可能尚未创建
                    pass
    except Exception:  # noqa: BLE001 - 状态存储不可用时由相关测试自行断言
        pass


@pytest.fixture(autouse=True)
def _reset_mock_runtime_state() -> Iterator[None]:
    """每个测试前重置 Mock 的运行期状态与技能注册表。

    Mock 后端是**有状态**的（修复动作会真实改变后端状态，否则"验证可达"永远失败）。
    若不逐测试重置，先跑的用例会把 ws-1001 修好，后面的用例就读不到故障画像，
    测试结果将依赖执行顺序（本项目实测踩到）。

    技能注册表同样要重建：技能实例持有工具客户端，
    对某个技能打桩（如注入异常模式客户端）后若不复原，会污染后续所有用例。
    """
    from app.mocks import data as mock_data
    from app.skills.registry import reset_registry

    mock_data.reset_runtime_state()
    reset_registry()
    yield
    mock_data.reset_runtime_state()
    reset_registry()


@pytest.fixture(autouse=True)
def _clear_redis_between_tests() -> Iterator[None]:
    """清理 Redis 中的会话/队列状态，保证测试独立。"""
    from app.core.redis_client import get_redis

    try:
        r = get_redis()
        for key in r.keys("cs:*"):
            r.delete(key)
    except Exception:  # noqa: BLE001 - Redis 不可用时由对应测试自行断言
        pass
    yield
