"""扩展点验证（Spike）。

目的：在写任何业务代码之前，用**真实运行**验证本项目所依赖的每一个扩展点，
避免"带病开工"。验证项：

  A. 核心层：配置加载、结构化日志、链路上下文
  B. 向量化：维度正确、确定性、区分度、相似度排序合理
  C. 向量库：本机持久化集合的建立、写入、查询、重启复用
  D. 状态图：StateGraph 建图、条件边、节点级上下文、状态持久化接口可用
  E. 框架能力探测：确认旧世代 Agent 执行器 API 不存在（佐证 ADR-002）
  F. 存储/缓存驱动：PostgreSQL 驱动与 Redis 客户端可导入

运行：
    .venv\\Scripts\\python.exe scripts\\spike_verify.py

退出码 0 表示全部通过。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 状态定义放在模块级：图引擎在注册分支时会解析注解，
# 定义在函数局部作用域会导致名称无法解析（本次 Spike 实测发现）。
class BranchState(TypedDict, total=False):
    value: int
    path: list
    verdict: str


class TurnState(TypedDict, total=False):
    turns: list


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def check(name: str):
    """装饰器：记录每项验证的通过/失败。"""

    def wrapper(fn):
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001
            FAIL.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  [FAIL] {name}\n         {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
        else:
            PASS.append(name)
            print(f"  [ OK ] {name}" + (f"  -> {detail}" if detail else ""))
        return fn

    return wrapper


print("=" * 74)
print("  扩展点验证 · SaaS 智能客服系统")
print("=" * 74)

# ---------------------------------------------------------------- A 核心层
print("\n[A] 核心层")


@check("A1 配置加载")
def _a1():
    from app.core.config import settings

    assert settings.embedding_dim > 0
    assert settings.primary_dsn.startswith("postgresql+psycopg://")
    assert settings.replica_dsn.count("5433") == 1
    assert settings.mock_url.startswith("http://")
    return f"env={settings.env} dim={settings.embedding_dim} llm={settings.llm_model} key={settings.llm_key_status()}"


@check("A2 结构化日志 + 链路字段")
def _a2():
    from app.core.logging_config import get_logger, timed_step
    from app.core.trace_context import bind

    log = get_logger("spike")
    with bind(trace_id="spiketrace0001", tenant_id="t-001", session_id="s-1", node="n1"):
        with timed_step("spike.demo", foo="bar"):
            pass
        log.info("spike.log.ok")
    # 校验 JSON 落盘里确实带了 trace_id
    from app.core.config import settings

    text = settings.log_path.read_text(encoding="utf-8")
    assert "spiketrace0001" in text, "日志文件中未找到 trace_id"
    assert "duration_ms" in text, "日志文件中未找到 duration_ms"
    return f"log={settings.log_path.name} 含 trace_id 与 duration_ms"


@check("A3 链路上下文隔离（并发安全前提）")
def _a3():
    import asyncio

    from app.core.trace_context import bind, get_tenant_id

    async def worker(tid: str) -> str:
        with bind(tenant_id=tid):
            await asyncio.sleep(0.01)
            return get_tenant_id()

    async def main():
        return await asyncio.gather(*[worker(f"t-{i:03d}") for i in range(20)])

    got = asyncio.run(main())
    assert got == [f"t-{i:03d}" for i in range(20)], f"上下文串扰: {got}"
    return "20 个并发协程上下文互不串扰"


# ---------------------------------------------------------------- B 向量化
print("\n[B] 向量化")


@check("B1 维度与确定性")
def _b1():
    from app.core.embedding import get_embedding_provider

    p = get_embedding_provider()
    v1 = p.embed_query("Slack 集成失效了，机器人不回消息")
    v2 = p.embed_query("Slack 集成失效了，机器人不回消息")
    assert len(v1) == p.dim, f"维度不符 {len(v1)} != {p.dim}"
    assert v1 == v2, "同样文本未得到同样向量（非确定性）"
    return f"{p.name} 维度={len(v1)} 确定性=是"


@check("B2 区分度与语义排序")
def _b2():
    from app.core.embedding import cosine_similarity, get_embedding_provider

    p = get_embedding_provider()
    query = "钉钉机器人 token 过期了怎么恢复"
    docs = {
        "token_expired": "钉钉集成机器人访问令牌 Token 已过期，需要刷新令牌后重新连接",
        "webhook_bad": "钉钉机器人 Webhook 回调地址失效，需要重新配置回调地址",
        "billing": "订阅账单重复计费，需要核对席位费用与用量费用",
        "billing2": "发票金额有争议，申请退款与账务调整",
    }
    vecs = {k: p.embed_query(v) for k, v in docs.items()}
    qv = p.embed_query(query)
    scores = {k: cosine_similarity(qv, v) for k, v in vecs.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    assert ranked[0][0] == "token_expired", f"排序异常: {ranked}"
    assert scores["token_expired"] > scores["billing"], "故障类与计费类未区分开"
    return " → ".join(f"{k}:{v:.3f}" for k, v in ranked)


@check("B3 批量向量化与空文本健壮性")
def _b3():
    from app.core.embedding import get_embedding_provider

    p = get_embedding_provider()
    out = p.embed_documents(["第一条", "", "第三条内容较长一些用于测试"])
    assert len(out) == 3
    assert all(len(v) == p.dim for v in out)
    assert all(all(x == 0.0 for x in v) for v in out[1:2]), "空文本应得到零向量"
    return "3 条批量（含空串）均返回合法维度"


# ---------------------------------------------------------------- C 向量库
print("\n[C] 向量库（本机持久化）")


@check("C1 集合创建 / 写入 / 查询")
def _c1():
    import shutil

    from app.core.config import RAG_DIR
    from app.core.vector_store import VectorStore

    probe_dir = RAG_DIR / "_spike_probe"
    if probe_dir.exists():
        shutil.rmtree(probe_dir, ignore_errors=True)

    vs = VectorStore(collection="spike_probe", persist_dir=probe_dir)
    n = vs.upsert(
        ids=["c1", "c2", "c3"],
        documents=[
            "飞书机器人 token 过期导致消息发送失败",
            "文档分享链接未开启导致同事打不开文档",
            "订阅账单席位费重复计算产生争议金额",
        ],
        metadatas=[
            {"skill": 2, "case_id": "c1"},
            {"skill": 3, "case_id": "c2"},
            {"skill": 4, "case_id": "c3"},
        ],
    )
    assert n == 3, f"写入条数异常 {n}"
    hits = vs.search("飞书 token 过期发不出消息", top_k=2)
    assert hits, "查询无结果"
    assert hits[0].metadata["case_id"] == "c1", f"首条命中错误: {hits[0].metadata}"
    return f"写入3条 查询首条命中={hits[0].metadata['case_id']} 分数={hits[0].score:.3f}"


@check("C2 持久化复用（二次打开不重建）")
def _c2():
    from app.core.config import RAG_DIR
    from app.core.vector_store import VectorStore

    vs = VectorStore(collection="spike_probe", persist_dir=RAG_DIR / "_spike_probe")
    assert vs.count == 3, f"重启后条数异常: {vs.count}"
    hits = vs.search("同事打不开我的文档", top_k=1)
    assert hits and hits[0].metadata["case_id"] == "c2", "重启后检索结果异常"
    return f"复用已有集合 条数={vs.count}"


@check("C3 空检索可被识别（降级矩阵前提）")
def _c3():
    from app.core.config import RAG_DIR
    from app.core.vector_store import VectorStore

    vs = VectorStore(collection="spike_probe", persist_dir=RAG_DIR / "_spike_probe")
    hits = vs.search("完全无关的问题：今天天气如何呀", top_k=3, min_score=0.9)
    assert hits == [], f"高阈值下不应有结果: {hits}"
    return "高阈值下正确返回空结果（可触发兜底话术）"


# ---------------------------------------------------------------- D 状态图
print("\n[D] 状态图与状态持久化")


@check("D1 StateGraph 建图 / 条件边 / 环路执行")
def _d1():
    from langgraph.graph import END, START, StateGraph

    # 注意：条件边路由函数的注解会在**模块全局作用域**中解析，
    # 因此这里必须直接使用模块级类型名，不能用 `S = BranchState` 之类的局部别名。
    def n_start(state: BranchState) -> dict:
        return {"path": state.get("path", []) + ["start"], "value": state.get("value", 0) + 1}

    def n_check(state: BranchState) -> dict:
        return {
            "path": state["path"] + ["check"],
            "verdict": "fail" if state["value"] < 2 else "ok",
        }

    def n_retry(state: BranchState) -> dict:
        return {"path": state["path"] + ["retry"], "value": state["value"] + 1}

    def n_done(state: BranchState) -> dict:
        return {"path": state["path"] + ["done"]}

    def branch(state: BranchState) -> str:
        return "retry" if state["verdict"] == "fail" else "done"

    g = StateGraph(BranchState)
    g.add_node("start", n_start)
    g.add_node("check", n_check)
    g.add_node("retry", n_retry)
    g.add_node("done", n_done)
    g.add_edge(START, "start")
    g.add_edge("start", "check")
    g.add_conditional_edges("check", branch, {"retry": "retry", "done": "done"})
    g.add_edge("retry", "check")   # 环路：诊断失败后回退重试
    g.add_edge("done", END)
    app = g.compile()
    out = app.invoke({"value": 0, "path": []})
    assert out["path"] == ["start", "check", "retry", "check", "done"], f"路径异常: {out['path']}"
    return "路径=" + "→".join(out["path"])


@check("D2 状态持久化接口可用（checkpointer 存在性）")
def _d2():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    assert hasattr(AsyncPostgresSaver, "setup"), "AsyncPostgresSaver 缺少 setup"
    assert hasattr(AsyncPostgresSaver, "aput"), "AsyncPostgresSaver 缺少 aput"
    from langgraph.checkpoint.memory import InMemorySaver

    mem = InMemorySaver()
    assert mem is not None
    return "AsyncPostgresSaver(setup/aput) 与 InMemorySaver 均可用"


@check("D3 带内存 checkpointer 的续接（thread_id 隔离）")
def _d3():
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    def n(state: TurnState) -> dict:
        return {"turns": state.get("turns", []) + ["tick"]}

    g = StateGraph(TurnState)
    g.add_node("n", n)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    app = g.compile(checkpointer=InMemorySaver())

    app.invoke({"turns": []}, config={"configurable": {"thread_id": "sess-A"}})
    app.invoke({}, config={"configurable": {"thread_id": "sess-A"}})
    app.invoke({}, config={"configurable": {"thread_id": "sess-B"}})

    a = app.get_state(config={"configurable": {"thread_id": "sess-A"}}).values["turns"]
    b = app.get_state(config={"configurable": {"thread_id": "sess-B"}}).values["turns"]
    assert len(a) == 2 and len(b) == 1, f"会话隔离失败 a={a} b={b}"
    return f"sess-A 累计 {len(a)} 轮 / sess-B 累计 {len(b)} 轮（互不影响）"


# ---------------------------------------------------------------- E 框架探测
print("\n[E] 框架能力探测（佐证技术选型）")


@check("E1 旧世代 Agent 执行器 API 不存在（选型依据）")
def _e1():
    import langchain

    has_legacy = hasattr(langchain, "create_tool_calling_agent")
    from importlib.util import find_spec

    has_classic = find_spec("langchain_classic") is not None
    assert not has_legacy, "当前世代仍暴露旧式 create_tool_calling_agent，需重新评估选型"
    assert not has_classic
    return f"langchain={langchain.__version__} 无旧式 Agent 执行器（与 ADR-002 一致）"


@check("E2 结构化输出能力（诊断结论落库前提）")
def _e2():
    from pydantic import BaseModel

    from langchain_openai import ChatOpenAI

    class Diag(BaseModel):
        cause: str
        confidence: float

    llm = ChatOpenAI(api_key="sk-probe", base_url="https://api.deepseek.com", model="deepseek-chat")
    structured = llm.with_structured_output(Diag)
    assert structured is not None
    assert hasattr(llm, "bind_tools"), "缺少工具绑定能力"
    return "with_structured_output / bind_tools 可用"


@check("E3 LLM 客户端实例化（不发起真实请求）")
def _e3():
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key="sk-probe", base_url="https://api.deepseek.com", model="deepseek-chat", timeout=5
    )
    return f"model={llm.model_name} base={str(llm.openai_api_base)}"


# ---------------------------------------------------------------- F 驱动
print("\n[F] 存储与缓存驱动")


@check("F1 PostgreSQL 驱动可导入")
def _f1():
    import psycopg
    from sqlalchemy import create_engine  # noqa: F401

    return f"psycopg={psycopg.__version__} sqlalchemy 可用"


@check("F2 Redis 客户端可导入")
def _f2():
    import redis

    return f"redis={redis.__version__}"


@check("F3 Chroma 版本与持久化客户端")
def _f3():
    import chromadb

    c = chromadb.PersistentClient(path=str(ROOT / "data" / "rag" / "_spike_chroma"))
    assert c is not None
    return f"chromadb={chromadb.__version__} PersistentClient 可用"


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 74)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
print("=" * 74)
if FAIL:
    for name, err in FAIL:
        print(f"  FAIL  {name}: {err}")
    sys.exit(1)
print("  全部扩展点验证通过，可进入业务实现。")
