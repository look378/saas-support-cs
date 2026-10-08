"""配置中心。

设计原则：
1. **单一拓扑**：一份 `.env` 贯穿全程，不区分开发/演示/生产的多套配置文件。
2. **不硬编码密钥**：所有敏感项只从环境变量读取，缺失时给出明确错误。
3. **禁止硬编码向量维度**：维度由 `EMBEDDING_DIM` 显式声明，代码任何位置不得写死。
4. **配置即契约**：新增配置项必须同步更新 `.env.example`。
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------
# 路径基线
# --------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_DIR / "app"
DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
DOCS_DIR = PROJECT_DIR / "docs"
SAMPLES_DIR = DATA_DIR / "samples"
RAG_DIR = DATA_DIR / "rag"
STATIC_DIR = APP_DIR / "static"

for _d in (DATA_DIR, LOG_DIR, SAMPLES_DIR, RAG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """全局配置。字段名与环境变量名大小写不敏感。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 运行环境 ----------------
    app_name: str = "智汇云客服"
    app_version: str = "1.0.0"
    env: Literal["local", "dev", "prod"] = "local"
    debug: bool = True
    timezone: str = "Asia/Shanghai"

    # ---------------- 服务端口 ----------------
    api_host: str = "127.0.0.1"
    # ---------------- 服务端口 ----------------
    # 默认端口刻意避开最常见的 8000/8501：同一台机器上常同时跑着其它工程，
    # 端口被占时新进程会绑定失败，而**旧服务仍在响应**，
    # 于是表现为"接口全 404 但 /health 正常"，极难定位
    # （本项目实测踩到：另一个工程的 API 占着 8000 并响应了 /health）。
    # 可用环境变量 API_PORT / STREAMLIT_PORT 覆盖。
    api_port: int = 8011
    mock_host: str = "127.0.0.1"
    mock_port: int = 8010
    streamlit_port: int = 8502

    # ---------------- 大模型 ----------------
    # 端点与模型名必须来自实测，不采信任何二手信息
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048

    # ---------------- 向量检索 ----------------
    # provider=local：项目内确定性本地实现（哈希 n-gram + IDF），零依赖零下载
    # provider=st：可选升级为本地句向量模型（需模型文件已在本机）
    embedding_provider: Literal["local", "st"] = "local"
    embedding_dim: int = 512
    embedding_model_path: str = ""
    chroma_collection: str = "support_knowledge"
    rag_top_k: int = 4
    rag_min_score: float = 0.15

    # ---------------- PostgreSQL ----------------
    pg_host: str = "127.0.0.1"
    pg_primary_port: int = 5432
    pg_replica_port: int = 5433
    pg_user: str = "saaskefu"
    pg_password: str = "saaskefu_local"
    pg_database: str = "saaskefu"
    pg_pool_size: int = 10
    pg_max_overflow: int = 20
    pg_replica_enabled: bool = True
    pg_statement_timeout_ms: int = 15000

    # ---------------- Redis ----------------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_socket_timeout: float = 3.0

    # ---------------- 鉴权 ----------------
    jwt_user_secret: str = "dev-only-user-secret-please-replace-in-production-32b"
    jwt_agent_secret: str = "dev-only-agent-secret-please-replace-in-production-32b"
    jwt_algorithm: str = "HS256"
    jwt_user_ttl_minutes: int = 720
    jwt_agent_ttl_minutes: int = 480

    # ---------------- 限流 ----------------
    rate_limit_enabled: bool = True
    rate_limit_default: int = 60          # 每窗口允许请求数
    rate_limit_window_seconds: int = 60

    # ---------------- 会话与坐席 ----------------
    session_ttl_seconds: int = 1800
    agent_heartbeat_interval_seconds: int = 10
    agent_heartbeat_ttl_seconds: int = 30
    agent_lock_ttl_seconds: int = 120
    # 掉线坐席巡检周期（秒）。0 = 关闭自动巡检。
    # 为什么需要它：坐席**主动**下线会在心跳接口里即时回收其会话，
    # 但进程崩溃/断网属**被动**掉线，没有任何请求会来触发回收——
    # 那些会话会永远停在 AGENT_HANDLING：AI 不再介入、坐席又不在。
    agent_offline_sweep_seconds: int = 30
    escalation_queue_key: str = "cs:queue:escalation"

    # ---------------- 转人工阈值 ----------------
    escalate_max_steps: int = 3
    escalate_emotion_intensity: int = 7
    escalate_amount_threshold: float = 100.0
    escalate_complaint_threshold: int = 2

    # ---------------- 计费安全 ----------------
    billing_hmac_secret: str = "dev-only-billing-hmac-please-replace-in-production-32b"
    billing_timestamp_window_seconds: int = 300
    billing_nonce_ttl_seconds: int = 600

    # ---------------- 容错 ----------------
    mock_timeout_seconds: float = 5.0
    mock_max_retries: int = 2
    mock_base_url: str = ""              # 留空则按 mock_host:mock_port 自动拼接

    # ---------------- 观测 ----------------
    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    log_json: bool = True
    slow_step_threshold_ms: int = 3000

    # ---------------- 派生属性 ----------------
    @property
    def mock_url(self) -> str:
        """Mock 服务基址。"""
        if self.mock_base_url:
            return self.mock_base_url.rstrip("/")
        return f"http://{self.mock_host}:{self.mock_port}"

    @property
    def primary_dsn(self) -> str:
        """主库 DSN（写）。"""
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_primary_port}/{self.pg_database}"
        )

    @property
    def replica_dsn(self) -> str:
        """从库 DSN（只读）。"""
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_replica_port}/{self.pg_database}"
        )

    @property
    def checkpointer_dsn(self) -> str:
        """状态图 checkpointer 使用的 DSN（固定主库，保证写入一致）。"""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_primary_port}/{self.pg_database}"
        )

    @property
    def log_path(self) -> Path:
        p = Path(self.log_file)
        return p if p.is_absolute() else PROJECT_DIR / p

    # ---------------- 校验 ----------------
    @field_validator("embedding_dim")
    @classmethod
    def _check_dim(cls, v: int) -> int:
        if v < 32 or v > 4096:
            raise ValueError("EMBEDDING_DIM 必须在 32..4096 之间")
        return v

    @property
    def secret_findings(self) -> list[str]:
        """检查签名密钥是否仍为示例占位值或强度不足。

        为什么要在启动时检查：``.env.example`` 里的占位密钥一旦被直接复制成
        ``.env``，服务**照常启动、所有功能正常**，但任何人凭仓库里可见的字符串
        就能伪造任意租户的坐席令牌，进而接管会话、读指标与回放。
        这种"能跑但等于没有鉴权"的状态必须显式告警，不能靠人记得改
        （本项目实测踩到：上线前一直是占位值）。
        """
        findings: list[str] = []
        placeholders = ("replace-with-random", "dev-only", "changeme", "your-secret")
        pairs = {
            "JWT_USER_SECRET": self.jwt_user_secret,
            "JWT_AGENT_SECRET": self.jwt_agent_secret,
            "BILLING_HMAC_SECRET": self.billing_hmac_secret,
        }
        for name, value in pairs.items():
            low = value.lower()
            if any(token in low for token in placeholders):
                findings.append(f"{name} 仍是示例占位值")
            elif len(value) < 32:
                findings.append(f"{name} 长度不足 32 字符")
        if self.jwt_user_secret == self.jwt_agent_secret:
            findings.append("JWT_USER_SECRET 与 JWT_AGENT_SECRET 相同（受众隔离失效）")
        return findings

    @field_validator("llm_base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL 必须是 http(s) 地址")
        return v.rstrip("/")

    def llm_key_status(self) -> str:
        """返回密钥状态描述，用于启动自检日志（不泄露密钥内容）。"""
        if not self.llm_api_key:
            return "缺失"
        k = self.llm_api_key
        return f"已配置({k[:4]}…{k[-4:]}, len={len(k)})" if len(k) > 12 else "已配置(过短，可能无效)"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。"""
    return Settings()


settings = get_settings()


def reload_settings() -> Settings:
    """清缓存并重新加载（测试用）。"""
    get_settings.cache_clear()
    return get_settings()
