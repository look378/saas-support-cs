"""安全原语：口令哈希、JWT 签发校验、计费签名。

集中一处，避免各处自行实现导致强度不一致。

口令哈希使用 Argon2id（当前推荐的密码哈希算法），不落明文、不用可逆加密。
JWT 采用双套密钥与受众（用户端 / 坐席端），互不通用——
用户端令牌拿去调坐席接口必须失败。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger(__name__)

_hasher = PasswordHasher()

Audience = Literal["user", "agent"]


# ===========================================================================
# 口令
# ===========================================================================
def hash_password(password: str) -> str:
    """生成口令哈希。"""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("security.password_verify_error", error=str(exc)[:150])
        return False


# ===========================================================================
# JWT
# ===========================================================================
class TokenError(RuntimeError):
    """令牌校验失败。"""

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class TokenClaims:
    """令牌载荷。"""

    subject: str            # 用户 external_id 或坐席用户名
    tenant_id: str
    audience: Audience
    role: str = ""
    session_key: str = ""
    issued_at: int = 0
    expires_at: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.subject,
            "tid": self.tenant_id,
            "aud": self.audience,
            "role": self.role,
            "sid": self.session_key,
            "iat": self.issued_at,
            "exp": self.expires_at,
            "jti": uuid.uuid4().hex,
            **self.extra,
        }


def _secret_for(audience: Audience) -> str:
    """按受众取密钥——两套令牌互不通用。"""
    return settings.jwt_agent_secret if audience == "agent" else settings.jwt_user_secret


def _ttl_for(audience: Audience) -> int:
    return settings.jwt_agent_ttl_minutes if audience == "agent" else settings.jwt_user_ttl_minutes


def issue_token(
    *,
    subject: str,
    tenant_id: str,
    audience: Audience,
    role: str = "",
    session_key: str = "",
    ttl_minutes: int | None = None,
    **extra: Any,
) -> str:
    """签发访问令牌。"""
    now = int(time.time())
    ttl = (ttl_minutes if ttl_minutes is not None else _ttl_for(audience)) * 60
    claims = TokenClaims(
        subject=subject,
        tenant_id=tenant_id,
        audience=audience,
        role=role,
        session_key=session_key,
        issued_at=now,
        expires_at=now + ttl,
        extra=extra,
    )
    return jwt.encode(claims.to_dict(), _secret_for(audience), algorithm=settings.jwt_algorithm)


def decode_token(token: str, *, audience: Audience) -> TokenClaims:
    """校验并解析令牌。

    受众隔离有**两层**保障，顺序上先撞到哪层取决于具体场景：
      1. 两套受众使用**不同密钥**——令牌签名根本不匹配（先触发 invalid）
      2. JWT 的 ``aud`` 声明校验——密钥相同但受众不符时（先触发 audience）

    因此错误归类上，``invalid`` 与 ``audience`` 都表示"令牌不可用于该受众"，
    调用方只需知道"不允许"即可，无须依赖具体是哪一层拦下的。
    """
    try:
        payload = jwt.decode(
            token,
            _secret_for(audience),
            algorithms=[settings.jwt_algorithm],
            audience=audience,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期", kind="expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenError(f"令牌受众不符（需要 {audience}）", kind="audience") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"令牌无效: {exc}", kind="invalid") from exc

    return TokenClaims(
        subject=str(payload.get("sub", "")),
        tenant_id=str(payload.get("tid", "")),
        audience=payload.get("aud", audience),  # type: ignore[arg-type]
        role=str(payload.get("role", "")),
        session_key=str(payload.get("sid", "")),
        issued_at=int(payload.get("iat", 0)),
        expires_at=int(payload.get("exp", 0)),
    )


def bearer_token(authorization: str | None) -> str | None:
    """从 Authorization 头提取令牌。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


# ===========================================================================
# 用户端身份（会话级访客令牌）
# ===========================================================================
def issue_user_token(*, tenant_id: str, user_ext_id: str, session_key: str = "") -> str:
    """为用户端签发访客令牌。

    ``subject`` 即终端用户标识：令牌是**唯一可信的身份来源**，
    调用方不应再采信请求体里自报的 ``user_ext_id``。
    这样同一租户内的两个用户即使拿到对方的会话标识，也无法读写对方会话。
    """
    return issue_token(
        subject=user_ext_id,
        tenant_id=tenant_id,
        audience="user",
        session_key=session_key,
    )


def decode_user_token(token: str, *, tenant_id: str) -> TokenClaims:
    """校验用户端令牌并返回其声明（含 ``session_key``）。

    校验四件事，任一不满足都按"未认证"处理：
      1. 签名与受众（``aud=user``）——用用户端专用密钥，坐席令牌无法冒充；
      2. 租户一致——令牌不能跨租户使用；
      3. ``sub`` 非空——空身份没有意义，不能当成"已认证"；
      4. （由调用方核对）``sid`` 会话绑定——令牌只对它被签发的那一个会话有效。

    为什么要绑定会话：令牌一旦不绑定会话，"我拿自己的合法令牌 + 别人的会话标识"
    就能操作他人会话（安全审查实测到该路径）。绑定后，令牌与会话必须成对出现。
    """
    claims = decode_token(token, audience="user")
    if not claims.subject:
        raise TokenError("用户令牌缺少身份标识", kind="subject")
    if claims.tenant_id != tenant_id:
        raise TokenError("用户令牌租户与请求不一致", kind="tenant")
    return claims


# ===========================================================================
# 计费签名
# ===========================================================================
def billing_signature(raw_body: bytes, timestamp: str, secret: str | None = None) -> str:
    """计费写操作签名。

    **必须对将要发送的同一段字节计算**：若先按 dict 交给 HTTP 库序列化再签名，
    客户端签的字节与服务端收到的不一致，签名必然失败（本项目实测踩到过）。
    """
    key = (secret or settings.billing_hmac_secret).encode("utf-8")
    return hmac.new(key, f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()


def verify_billing_signature(
    raw_body: bytes, timestamp: str, signature: str, *, secret: str | None = None
) -> tuple[bool, str]:
    """校验计费签名与时间戳窗口。返回 (是否通过, 原因)。"""
    if not timestamp or not signature:
        return False, "SIGNATURE_MISSING"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "TIMESTAMP_INVALID"
    if abs(int(time.time()) - ts) > settings.billing_timestamp_window_seconds:
        return False, "TIMESTAMP_OUT_OF_WINDOW"
    expected = billing_signature(raw_body, timestamp, secret)
    if not hmac.compare_digest(expected, signature):
        return False, "SIGNATURE_INVALID"
    return True, ""
