"""状态持久化验证：真实跨进程断点续接。

这是本项目最关键的一条验收声明：
    「应用进程重启 → 进行中的诊断流程从状态存储断点续接，用户无需重述」

验证方式必须**真的换一个进程**，否则只是"同一个内存对象还在"的假象。
因此本脚本分三种模式运行：

    prepare   进程A：建立状态图，执行到中途（等待用户澄清）并落库
    resume    进程B（全新进程）：凭同一 thread_id 恢复，继续执行到结束
    all       在单进程内依次跑 prepare → 调用子进程跑 resume → 校验

用法：
    .venv\\Scripts\\python.exe scripts\\verify_checkpointer.py all
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在任何异步代码之前应用平台修正（异步状态存储依赖选择器事件循环）
from app.core import platform_compat  # noqa: E402

platform_compat.apply()

from app.core.config import settings  # noqa: E402

THREAD_ID = "t-001:sess-ckpt-demo"
# 用一个独立的表名空间避免与业务 checkpointer 冲突（仍是真实的 Postgres 持久化）
SCHEMA_NOTE = "verify"


class CkptState(TypedDict, total=False):
    """诊断流程状态。

    注意：状态类型必须定义在模块级——条件边路由函数的注解会在模块全局作用域解析，
    局部别名会导致 NameError（见 docs/environment-constraints.md 与 architecture §11.1 C-1）。
    """

    session_id: str
    user_message: str
    need_clarify: bool
    clarify_question: str
    user_reply: str
    collected: dict
    steps: list
    result: str


def build_graph(checkpointer):
    """构建一个最小但真实的"澄清—挂起—续接"图。

    用 ``interrupt()`` 让图真正**挂起**（而不是跑完一轮就结束）：
    挂起后状态落库、``aget_state().next`` 指向待执行节点；
    新进程用 ``Command(resume=...)`` 凭同一 thread_id 继续。
    这正是五个技能里"澄清追问"与"重启续接"共用的机制。
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def n_observe(state: CkptState) -> dict:
        """查询集成配置（此处用确定性数据代替真实调用）。"""
        return {
            "steps": state.get("steps", []) + ["observe"],
            "collected": {"workspace_id": "ws-1001"},
        }

    def n_clarify(state: CkptState) -> dict:
        """信息不足则挂起，等用户补充后由本节点继续。"""
        question = "请提供该工作区使用的集成渠道（如 Slack / 钉钉）"
        answer = interrupt({"question": question, "node": "clarify"})
        collected = dict(state.get("collected") or {})
        collected["channel"] = answer
        return {
            "steps": state.get("steps", []) + ["clarify"],
            "clarify_question": question,
            "user_reply": answer,
            "collected": collected,
        }

    def n_diagnose(state: CkptState) -> dict:
        channel = (state.get("collected") or {}).get("channel", "unknown")
        return {
            "steps": state.get("steps", []) + ["diagnose"],
            "result": f"诊断完成：工作区 ws-1001 的 {channel} 集成访问令牌已过期",
        }

    def route_after_observe(state: CkptState) -> str:
        return "clarify" if not (state.get("collected") or {}).get("channel") else "diagnose"

    g = StateGraph(CkptState)
    g.add_node("observe", n_observe)
    g.add_node("clarify", n_clarify)
    g.add_node("diagnose", n_diagnose)
    g.add_edge(START, "observe")
    g.add_conditional_edges(
        "observe", route_after_observe, {"clarify": "clarify", "diagnose": "diagnose"}
    )
    g.add_edge("clarify", "diagnose")
    g.add_edge("diagnose", END)
    return g.compile(checkpointer=checkpointer)


async def _with_saver(fn):
    """以 Postgres 状态存储建立图并执行回调。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(settings.checkpointer_dsn) as saver:
        await saver.setup()
        return await fn(build_graph(saver))


async def run_prepare() -> dict:
    """进程A：跑第一轮，遇 interrupt 挂起。"""

    async def job(app):
        cfg = {"configurable": {"thread_id": THREAD_ID}}
        out = await app.ainvoke(
            {
                "session_id": THREAD_ID,
                "user_message": "钉钉机器人不回消息了",
                "steps": [],
                "collected": {},
            },
            config=cfg,
        )
        snap = await app.aget_state(config=cfg)
        interrupts = []
        for task in snap.tasks or []:
            interrupts.extend(getattr(task, "interrupts", ()) or ())
        return {
            "after_turn1": out,
            "pending": bool(snap.next),
            "next": list(snap.next),
            "interrupt": (interrupts[0].value if interrupts else None),
        }

    return await _with_saver(job)


async def run_resume() -> dict:
    """进程B：凭 thread_id 恢复，用 Command(resume=...) 补充信息后跑完。"""

    async def job(app):
        from langgraph.types import Command

        cfg = {"configurable": {"thread_id": THREAD_ID}}
        before = await app.aget_state(config=cfg)
        out = await app.ainvoke(Command(resume="钉钉"), config=cfg)
        return {
            "resumed_from": before.values.get("steps", []),
            "pending_before": list(before.next),
            "final": out,
        }

    return await _with_saver(job)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "prepare":
        print(json.dumps(asyncio.run(run_prepare()), ensure_ascii=False))
        return 0

    if mode == "resume":
        print(json.dumps(asyncio.run(run_resume()), ensure_ascii=False))
        return 0

    # ---- all 模式：同进程 prepare + 子进程 resume + 校验 ----
    print("=" * 74)
    print("  状态持久化与跨进程断点续接验证")
    print("=" * 74)
    print(f"  状态存储 DSN : {settings.checkpointer_dsn.replace(settings.pg_password, '***')}")
    print(f"  thread_id    : {THREAD_ID}")

    print("\n[1] 进程A：执行到信息不足处应挂起（interrupt）")
    # 先清理可能的历史线程，保证从干净状态开始
    asyncio.run(_cleanup())
    stage1 = asyncio.run(run_prepare())
    print(f"    执行轨迹 : {stage1['after_turn1'].get('steps')}")
    print(f"    挂起问题 : {stage1['interrupt']}")
    print(f"    待执行节点 : {stage1['next']}")

    ok1 = stage1["pending"] and "clarify" in stage1["next"] and bool(stage1["interrupt"])
    print(f"    [{' OK ' if ok1 else 'FAIL'}] 第一轮正确挂起并产出澄清问题")

    print("\n[2] 进程B：**全新进程**凭同一 thread_id 恢复")
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "resume"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print("    子进程退出码:", proc.returncode)
        print("    stderr:", (proc.stderr or "")[-1500:])
        print("    [FAIL] 恢复进程执行失败")
        return 1
    # 取最后一行 JSON（前面可能有日志）
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    if payload is None:
        print("    子进程输出无法解析:", proc.stdout[-800:])
        return 1

    print(f"    恢复时读到的历史轨迹 : {payload['resumed_from']}")
    print(f"    恢复时待执行节点     : {payload['pending_before']}")
    print(f"    最终结果             : {payload['final'].get('result')}")
    print(f"    完整轨迹             : {payload['final'].get('steps')}")

    ok2 = payload["resumed_from"] == stage1["after_turn1"].get("steps")
    print(f"    [{' OK ' if ok2 else 'FAIL'}] 新进程成功读到上一进程的挂起状态（用户无需重述）")

    ok3 = "diagnose" in (payload["final"].get("steps") or []) and payload["final"].get("result")
    print(f"    [{' OK ' if ok3 else 'FAIL'}] 恢复后走完诊断并产出结论")

    print("\n[3] 校验状态确实落在 PostgreSQL 中（而非内存）")
    rows = asyncio.run(_count_checkpoints())
    print(f"    状态存储中的记录数 : {rows}")
    ok4 = rows > 0
    print(f"    [{' OK ' if ok4 else 'FAIL'}] 状态已持久化到数据库")

    print("\n" + "=" * 74)
    all_ok = all([ok1, ok2, ok3, ok4])
    print(f"  结论: {'全部通过' if all_ok else '存在失败项'}")
    print("=" * 74)
    return 0 if all_ok else 1


async def _cleanup() -> None:
    import psycopg

    def _do():
        with psycopg.connect(settings.checkpointer_dsn, autocommit=True) as conn:
            for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
                try:
                    conn.execute(f"DELETE FROM {tbl} WHERE thread_id = %s", (THREAD_ID,))
                except Exception:  # noqa: BLE001 - 表可能尚未创建
                    pass

    await asyncio.to_thread(_do)


async def _count_checkpoints() -> int:
    import psycopg

    def _do() -> int:
        with psycopg.connect(settings.checkpointer_dsn) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (THREAD_ID,)
                    )
                    return int(cur.fetchone()[0])
                except Exception:  # noqa: BLE001
                    return 0

    return await asyncio.to_thread(_do)


if __name__ == "__main__":
    raise SystemExit(main())
