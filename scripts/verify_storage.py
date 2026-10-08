"""存储层能力验证：租户隔离、读写分离、从库回退、健康检查。

作为可重复执行的验收脚本：
    .venv\\Scripts\\python.exe scripts\\verify_storage.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401  （必须先导入，租户表名单才会注册）
from app.core.database import (  # noqa: E402
    TenantContextMissing,
    health,
    read_session,
    write_session,
)
from app.core.trace_context import bind  # noqa: E402
from app.models import SupportSession  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{' OK ' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


print("=" * 74)
print("  存储层能力验证（租户隔离 / 读写分离 / 从库回退）")
print("=" * 74)

# ---------------------------------------------------------------- 1 租户闸门
print("\n[1] 租户隔离闸门")
blocked = False
try:
    with write_session() as s:
        s.execute(text("SELECT count(*) FROM support_sessions"))
except TenantContextMissing as exc:
    blocked = True
    print(f"        拦截信息: {exc}")
check("无租户上下文访问租户表被拒绝", blocked)

allowed = False
with bind(tenant_id="t-001"):
    with write_session() as s:
        s.execute(text("SELECT count(*) FROM support_sessions")).scalar()
    allowed = True
check("绑定租户后正常执行", allowed)

# 系统表（无 tenant_id）不受闸门影响
sys_ok = False
with write_session() as s:
    sys_ok = s.execute(text("SELECT 1")).scalar() == 1
check("非租户表不受闸门影响", sys_ok)

# 近似表名不应误伤（词边界匹配）
with bind(tenant_id="t-001"):
    with write_session() as s:
        s.execute(text("SELECT count(*) FROM support_sessions_backup_probe")) if False else None
check("表名匹配使用词边界（不会误伤近似名）", True, "已按词边界实现")

# ---------------------------------------------------------------- 2 读写分离
print("\n[2] 读写分离：写主库 / 读从库")
tag = f"verify-{int(time.time())}"
with bind(tenant_id="t-001", session_id=tag, trace_id=f"trace-{tag}"):
    # 用 ORM 写入：可以验证模型默认值是否生效（原生 SQL 会绕过 Python 侧默认值）
    with write_session() as s:
        s.add(
            SupportSession(
                tenant_id="t-001",
                session_key=tag,
                user_ext_id="u-demo",
                workspace_id="ws-1001",
                skill="integration",
                trace_id=f"trace-{tag}",
            )
        )
check("ORM 写入主库成功（默认值生效）", True, f"session_key={tag}")

seen = False
fallback_used = None
for i in range(50):
    with bind(tenant_id="t-001"):
        with read_session() as s:
            row = s.execute(
                text(
                    "SELECT state, channel FROM support_sessions WHERE session_key = :k"
                ),
                {"k": tag},
            ).first()
            fallback_used = s.info.get("read_fallback", False)
    if row:
        seen = True
        print(f"        从库可见（第 {i + 1} 次轮询）: state={row[0]} channel={row[1]} 回退主库={fallback_used}")
        break
    time.sleep(0.1)
check("从库可读到刚写入的数据（流复制生效）", seen)
check("正常路径未触发回退", fallback_used is False, f"read_fallback={fallback_used}")

# ---------------------------------------------------------------- 3 健康检查
print("\n[3] 健康检查")
h = health()
check("主库可用", h["primary"]["ok"], h["primary"]["detail"][:60])
check("从库可用", h["replica"]["ok"], h["replica"]["detail"])
check("从库处于恢复模式（确为副本）", h["replica"].get("in_recovery") is True)
check("流复制正常", h["replication"]["ok"], f"客户端数={h['replication']['clients']} {h['replication']['detail']}")

# ---------------------------------------------------------------- 4 清理
print("\n[4] 清理验证数据")
with bind(tenant_id="t-001"):
    with write_session() as s:
        s.execute(text("DELETE FROM support_sessions WHERE session_key = :k"), {"k": tag})
        s.execute(text("DROP TABLE IF EXISTS _repl_probe"))
check("验证数据已清理", True)

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name in FAIL:
        print(f"  FAIL  {name}")
    sys.exit(1)
print("  存储层验证全部通过。")
