"""依赖健康检查：把"系统是否真的可用"聚合成一个报告。

覆盖：PostgreSQL 主从与流复制、Redis、向量库、大模型端点、诊断状态存储。
用于启动自检、`/health/deps` 接口与验收证据采集。
"""

from __future__ import annotations

import time
from typing import Any

from app.core.config import settings
from app.core.logging_config import get_logger

log = get_logger(__name__)


def check_database() -> dict[str, Any]:
    """主从 + 流复制。"""
    from app.core.database import health

    try:
        h = health()
        replication = h.get("replication", {})
        ok = bool(h["primary"]["ok"] and h["replica"]["ok"])
        return {
            "name": "PostgreSQL 主从",
            "ok": ok,
            "detail": {
                "primary": h["primary"]["detail"][:80],
                "replica": h["replica"]["detail"],
                "replication_streaming": replication.get("clients", 0) > 0,
                "replication_clients": replication.get("clients", 0),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": "PostgreSQL 主从", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


def check_redis() -> dict[str, Any]:
    from app.core.redis_client import ping

    ok, detail = ping()
    return {"name": "Redis", "ok": ok, "detail": detail}


def check_vector_store() -> dict[str, Any]:
    """向量库与知识条目数。"""
    try:
        from app.services.knowledge_service import KnowledgeService

        svc = KnowledgeService()
        count = svc.store.count
        return {
            "name": "向量库",
            "ok": count > 0,
            "detail": {
                "collection": svc.store.collection_name,
                "documents": count,
                "embedding": svc.store._provider.name,
                "distance": svc.store.distance_space,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": "向量库", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


def check_llm() -> dict[str, Any]:
    """大模型端点（不发起真实请求，只校验配置完整性）。"""
    key = settings.llm_api_key
    configured = bool(key) and not key.startswith("sk-your")
    return {
        "name": "大模型端点",
        "ok": configured,
        "detail": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "key": settings.llm_key_status(),
        },
    }


def check_mock_backend() -> dict[str, Any]:
    """Mock 业务后端（生产环境为真实业务系统）。"""
    from app.tools.client import get_tool_client

    ok, detail = get_tool_client().health()
    return {"name": "业务系统接口", "ok": ok, "detail": detail[:150]}


def check_checkpointer() -> dict[str, Any]:
    """状态存储表是否就绪。"""
    try:
        import psycopg

        with psycopg.connect(settings.checkpointer_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name IN ('checkpoints','checkpoint_writes','checkpoint_blobs')"
                )
                n = int(cur.fetchone()[0])
        return {
            "name": "诊断状态存储",
            "ok": n >= 3,
            "detail": f"状态表 {n}/3（首次执行诊断后自动建立）",
        }
    except Exception as exc:  # noqa: BLE001
        return {"name": "诊断状态存储", "ok": False, "detail": f"{type(exc).__name__}: {exc}"[:200]}


def full_health(*, include_slow: bool = True) -> dict[str, Any]:
    """汇总健康报告。"""
    started = time.perf_counter()
    checks = [
        check_database(),
        check_redis(),
        check_vector_store(),
        check_llm(),
        check_mock_backend(),
        check_checkpointer(),
    ]
    if not include_slow:  # pragma: no cover - 预留快速模式
        checks = checks[:2]

    failed = [c["name"] for c in checks if not c["ok"]]
    report = {
        "overall": "ok" if not failed else "degraded",
        "failed": failed,
        "checks": checks,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "env": settings.env,
        "app": f"{settings.app_name} {settings.app_version}",
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    """以可读形式打印（供 CLI 与验收脚本使用）。"""
    print("=" * 74)
    print(f"  依赖健康检查 · {report['app']}")
    print("=" * 74)
    for c in report["checks"]:
        mark = " OK " if c["ok"] else "FAIL"
        detail = c["detail"]
        if isinstance(detail, dict):
            detail = " | ".join(f"{k}={v}" for k, v in detail.items())
        print(f"  [{mark}] {c['name']:<18} {str(detail)[:110]}")
    print("-" * 74)
    print(f"  总体状态: {report['overall']}    耗时 {report['duration_ms']}ms")
    if report["failed"]:
        print(f"  失败项: {', '.join(report['failed'])}")
    print("=" * 74)
