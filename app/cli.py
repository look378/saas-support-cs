"""命令行工具（数据库初始化、数据装载、自检、验收证据采集）。

用法：
    python -m app.cli init-db            创建/同步表结构
    python -m app.cli drop-db            删除全部表（危险，仅测试用）
    python -m app.cli seed               装载租户/坐席/用户/案例种子数据
    python -m app.cli build-index        构建向量索引（案例库 + FAQ）
    python -m app.cli health             服务与依赖健康检查
    python -m app.cli demo               运行一次端到端演示并打印结果
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bootstrap() -> None:
    from app.core.config import settings
    from app.core.logging_config import configure_logging

    configure_logging()
    _ = settings


def cmd_init_db(args: argparse.Namespace) -> int:
    from sqlalchemy import text

    from app.core.database import write_session
    from app.models import Base

    with write_session() as s:
        Base.metadata.create_all(bind=s.get_bind())
        tables = s.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        ).scalars().all()
    print(f"  表结构已就绪，共 {len(tables)} 张表:")
    for t in tables:
        print(f"    - {t}")
    return 0


def cmd_drop_db(args: argparse.Namespace) -> int:
    from app.core.database import write_session
    from app.models import Base

    with write_session() as s:
        Base.metadata.drop_all(bind=s.get_bind())
    print("  全部表已删除")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from app.services.seed_service import SeedService

    result = SeedService().run()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_build_index(args: argparse.Namespace) -> int:
    from app.services.knowledge_service import KnowledgeService

    result = KnowledgeService().rebuild(force=getattr(args, "force", False))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from app.services.health_service import full_health

    report = full_health()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("overall") == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description="SaaS 智能客服系统命令行工具")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="创建/同步表结构").set_defaults(func=cmd_init_db)
    sub.add_parser("drop-db", help="删除全部表").set_defaults(func=cmd_drop_db)
    sub.add_parser("seed", help="装载种子数据").set_defaults(func=cmd_seed)

    bi = sub.add_parser("build-index", help="构建向量索引")
    bi.add_argument("--force", action="store_true", help="强制重建集合")
    bi.set_defaults(func=cmd_build_index)

    sub.add_parser("health", help="依赖健康检查").set_defaults(func=cmd_health)
    return p


def main(argv: list[str] | None = None) -> int:
    _bootstrap()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
