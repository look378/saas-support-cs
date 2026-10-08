"""Redis 客户端封装（会话状态 / 坐席心跳 / 转人工队列 / 分布式锁 / 令牌桶）。

实测约束（见 docs/architecture.md §11.2）：
Windows 免安装版 Redis 5.0.x **不支持 RESP3 协议**，而当前 redis-py 默认使用
RESP3 并在连接时发送 ``HELLO 3``，服务端会返回：

    unknown command `HELLO`

因此本项目统一在连接参数中显式指定 ``protocol=2``。所有 Redis 访问必须经本模块，
不得各自创建客户端，以免某个模块漏配协议导致"偶发连接失败"。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import redis

from app.core.config import settings
from app.core.logging_config import get_logger, warn_degraded

log = get_logger(__name__)

# 连接参数集中一处，协议版本是硬约束
_BASE_KWARGS: dict[str, Any] = {
    "host": settings.redis_host,
    "port": settings.redis_port,
    "db": settings.redis_db,
    "socket_timeout": settings.redis_socket_timeout,
    "socket_connect_timeout": settings.redis_socket_timeout,
    "decode_responses": True,
    "protocol": 2,
}
if settings.redis_password:
    _BASE_KWARGS["password"] = settings.redis_password


class RedisUnavailable(RuntimeError):
    """Redis 不可用。调用方按容错矩阵降级为进程内状态。"""


_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """获取全局 Redis 客户端。"""
    global _client
    if _client is None:
        _client = redis.Redis(**_BASE_KWARGS)
    return _client


def ping() -> tuple[bool, str]:
    """连通性自检。返回 (是否可用, 描述)。

    注意：不使用 ``INFO`` 解析版本号——Windows 版 Redis 的 INFO 响应中含非 UTF-8
    字节（构建信息），在 ``decode_responses=True`` 下会抛 UnicodeDecodeError，
    会把"其实可用"的实例误判为不可用。这里改用 PING 与 CONFIG GET 探测。
    """
    try:
        r = get_redis()
        if not r.ping():
            return False, "PING 无响应"
        version = "unknown"
        try:
            version = str(r.config_get("redis_version").get("redis_version", "unknown"))
        except Exception:  # noqa: BLE001 - 取版本失败不影响可用性判定
            version = "unknown"
        return True, f"redis {version} @ {settings.redis_host}:{settings.redis_port}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# 键命名规范（统一前缀，便于排查与清理）
# ---------------------------------------------------------------------------
def key_session(session_id: str) -> str:
    return f"cs:session:{session_id}"


def key_agent_heartbeat(agent_id: str) -> str:
    return f"cs:agent:hb:{agent_id}"


def key_agent_load(agent_id: str) -> str:
    return f"cs:agent:load:{agent_id}"


def key_session_lock(session_id: str) -> str:
    return f"cs:lock:session:{session_id}"


def key_rate_limit(tenant_id: str, endpoint: str, window: int) -> str:
    return f"cs:rate:{tenant_id}:{endpoint}:{window}"


def key_nonce(nonce: str) -> str:
    return f"cs:nonce:{nonce}"



# ---------------------------------------------------------------------------
# 分布式锁
# ---------------------------------------------------------------------------
@contextmanager
def distributed_lock(
    key: str, ttl_seconds: int, *, wait_seconds: float = 0.0, token: str | None = None
) -> Iterator[str | None]:
    """基于 SET NX EX 的分布式锁。

    产出锁令牌；若未抢到锁则产出 ``None``，由调用方决定是等待、拒绝还是提示"已被接管"。
    释放时校验令牌，避免误删他人持有的锁。
    """
    holder = token or uuid.uuid4().hex
    r = get_redis()
    acquired = False
    deadline = time.time() + max(0.0, wait_seconds)

    while True:
        try:
            acquired = bool(r.set(key, holder, nx=True, ex=ttl_seconds))
        except Exception as exc:
            warn_degraded("redis.lock.unavailable", key=key, error=str(exc)[:200])
            raise RedisUnavailable(f"获取分布式锁失败: {exc}") from exc

        if acquired or time.time() >= deadline:
            break
        time.sleep(0.05)

    try:
        yield holder if acquired else None
    finally:
        if acquired:
            # Lua 保证"校验并删除"的原子性
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end"
            )
            try:
                r.eval(script, 1, key, holder)
            except Exception as exc:  # noqa: BLE001
                log.warning("redis.lock.release_failed", key=key, error=str(exc)[:200])


# ---------------------------------------------------------------------------
# 令牌桶限流
# ---------------------------------------------------------------------------
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now_ms end

local delta = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + delta * refill_per_sec)

local allowed = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_sec * 1000) + 1000)
return {allowed, math.floor(tokens)}
"""


def token_bucket_allow(
    key: str, capacity: int, window_seconds: int, cost: int = 1
) -> tuple[bool, int]:
    """令牌桶限流。返回 (是否放行, 剩余令牌数)。

    使用 Lua 脚本保证"读取-补充-扣减-写回"的原子性，避免并发下超发。
    """
    r = get_redis()
    refill = capacity / float(max(1, window_seconds))
    now_ms = int(time.time() * 1000)
    try:
        allowed, remaining = r.eval(_LUA_TOKEN_BUCKET, 1, key, capacity, refill, now_ms, cost)
        return bool(allowed), int(remaining)
    except Exception as exc:  # noqa: BLE001
        # 限流依赖不可用时按"放行并告警"处理，避免因缓存故障导致业务全面不可用
        warn_degraded("redis.rate_limit.unavailable", key=key, error=str(exc)[:200])
        return True, -1


# ---------------------------------------------------------------------------
# 防重放
# ---------------------------------------------------------------------------
def nonce_consume(nonce: str, ttl_seconds: int) -> bool:
    """消费一次性随机数。返回 True 表示首次出现（合法），False 表示重放。"""
    r = get_redis()
    return bool(r.set(key_nonce(nonce), "1", nx=True, ex=ttl_seconds))
