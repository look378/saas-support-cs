"""坐席工作台（Streamlit）。

形态（对应任务书第 6 节的三列布局）：

    +------------+----------------------+---------------+
    | 会话队列    | 对话区                | 客户信息       |
    | 待接入/处理中| [消息流+工具调用标记]  | 画像/历史工单   |
    | 已办结      | [诊断步骤可视化]      | 快捷操作       |
    +------------+----------------------+---------------+
    | Tab: 指标看板 | 诊断链路回放 | 坐席管理 | 转人工队列  |
    +--------------------------------------------------+

**关于实时性**：Streamlit 的执行模型是"交互触发脚本重跑"，无法作为 WebSocket
客户端接收服务端主动推送。因此本页面通过**自动刷新轮询**获取状态，
真正的实时推送由 `app/static/agent_console.html`（独立 WebSocket 控制台）承载。
这一分工是刻意的，见 docs/architecture.md 决策 ADR-004。

启动：
    streamlit run app/dashboard.py --server.port 8501
"""

from __future__ import annotations

import time
from typing import Any

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

API = st.session_state.get("api_base", "http://127.0.0.1:8011")
DEFAULT_TENANT = "t-001"
REFRESH_SECONDS = 5

st.set_page_config(
    page_title="坐席工作台 · SaaS 智能客服",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ===========================================================================
# HTTP 辅助
# ===========================================================================
def _headers(tenant: str, token: str = "") -> dict[str, str]:
    h = {"X-Tenant-ID": tenant}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def api_get(path: str, tenant: str, token: str = "", **params: Any) -> Any:
    try:
        r = requests.get(
            f"{API}{path}", headers=_headers(tenant, token), params=params or None, timeout=15
        )
        if r.status_code == 200:
            return r.json()
        return {"ok": False, "error": r.text[:300], "status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def api_post(path: str, tenant: str, token: str = "", payload: dict | None = None) -> Any:
    try:
        r = requests.post(
            f"{API}{path}",
            headers=_headers(tenant, token),
            json=payload or {},
            timeout=120,
        )
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": r.text[:300], "status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ===========================================================================
# 登录 / 会话状态
# ===========================================================================
def ensure_state() -> None:
    for key, default in (
        ("token", ""),
        ("agent", ""),
        ("display_name", ""),
        ("skills", []),
        ("tenant", DEFAULT_TENANT),
        ("active_session", ""),
        ("api_base", "http://127.0.0.1:8011"),
    ):
        st.session_state.setdefault(key, default)


def render_sidebar() -> None:
    with st.sidebar:
        st.title("🎧 坐席工作台")
        st.caption("智汇云客服 · SaaS 产品支持")

        st.session_state["api_base"] = st.text_input(
            "API 地址", value=st.session_state.get("api_base", "http://127.0.0.1:8011")
        )
        global API
        API = st.session_state["api_base"]

        st.session_state["tenant"] = st.selectbox(
            "租户", ["t-001", "t-002"], index=0 if st.session_state["tenant"] == "t-001" else 1
        )
        tenant = st.session_state["tenant"]

        st.divider()
        if not st.session_state["token"]:
            st.subheader("坐席登录")
            username = st.selectbox("账号", ["agent_zhao", "agent_lin", "lead_chen"])
            password = st.text_input("口令", value="Agent@12345", type="password")
            if st.button("登录", use_container_width=True, type="primary"):
                res = api_post(
                    "/api/v1/agent/login", tenant, payload={"username": username, "password": password}
                )
                if res.get("ok", False) and res.get("token"):
                    st.session_state.update(
                        token=res["token"],
                        agent=res["agent"],
                        display_name=res.get("display_name", username),
                        skills=res.get("skills", []),
                    )
                    st.rerun()
                else:
                    st.error(f"登录失败：{res.get('detail') or res.get('error')}")
        else:
            st.success(f"已登录：{st.session_state['display_name']}")
            st.caption(f"技能组：{'、'.join(st.session_state['skills']) or '未配置'}")
            if st.button("退出登录", use_container_width=True):
                st.session_state.update(token="", agent="", display_name="", skills=[])
                st.rerun()

        if st.session_state["token"]:
            st.divider()
            st.subheader("在线状态")
            presence = st.radio(
                "状态", ["online", "break", "offline"], horizontal=True, label_visibility="collapsed"
            )
            if st.button("上报心跳", use_container_width=True):
                res = api_post(
                    "/api/v1/agent/heartbeat",
                    tenant,
                    st.session_state["token"],
                    {"presence": presence},
                )
                if res.get("ok"):
                    st.toast(f"心跳已上报：{res['data'].get('presence')}")
                    st.rerun()
                else:
                    st.error(f"上报失败：{res.get('error') or res.get('detail')}")
            st.caption("心跳每 10 秒一次，TTL 30 秒；过期后名下会话自动重新排队。")

        st.divider()
        healthy = api_get("/health", tenant)
        st.caption("服务状态：" + ("✅ 正常" if healthy.get("status") == "ok" else "❌ 未连接"))


# ===========================================================================
# 三列主视图
# ===========================================================================
FA_EMOJI = {"resolved": "✅", "escalated": "🚨", "pending_approval": "⏳",
            "pending_user_action": "🔗", "verify_failed": "⚠️", "unresolved": "❔"}


def render_queue_column(tenant: str, token: str) -> None:
    st.markdown("#### 会话队列")
    tab_wait, tab_handling, tab_closed = st.tabs(["待接入", "处理中", "已办结"])

    with tab_wait:
        res = api_get("/api/v1/agent/queue", tenant, token)
        items = res.get("data", []) if res.get("ok") else []
        if not items:
            st.info("暂无待接入会话")
        for item in items:
            with st.container(border=True):
                st.markdown(
                    f"**{item.get('skill') or '通用'}** · "
                    f"{'🔥高优' if item.get('priority') else '普通'}"
                )
                st.caption(f"{item.get('session_key', '')[:18]}…")
                st.caption(f"{(item.get('reason') or '')[:40]}")
                if st.button("接管", key=f"claim_{item['session_key']}", use_container_width=True):
                    r = api_post(
                        "/api/v1/agent/claim",
                        tenant,
                        token,
                        {"session_key": item["session_key"]},
                    )
                    if r.get("ok"):
                        st.session_state["active_session"] = item["session_key"]
                        st.toast("已接管")
                        st.rerun()
                    else:
                        st.error(f"接管失败：{r.get('detail') or r.get('error')}")

    with tab_handling:
        agents = api_get("/api/v1/agent/metrics/agents", tenant, token)
        rows = agents.get("data", []) if agents.get("ok") else []
        mine = next((r for r in rows if r["agent"] == st.session_state.get("agent")), None)
        if mine:
            st.metric("我的当前负载", mine["current_load"])
            st.metric("累计接管", mine["handled_sessions"])
        if st.session_state.get("active_session"):
            st.success(f"当前会话：{st.session_state['active_session'][:18]}…")
        else:
            st.caption("未选中会话")

    with tab_closed:
        res = api_get("/api/v1/agent/escalations", tenant, token, status="closed", limit=20)
        rows = res.get("data", []) if res.get("ok") else []
        if not rows:
            st.caption("暂无已办结记录")
        for row in rows[:10]:
            st.caption(
                f"{FA_EMOJI.get('resolved', '')} {row.get('request_no', '')} · "
                f"{(row.get('reason') or '')[:28]} · 坐席 {row.get('assigned_agent') or '-'}"
            )


def render_conversation_column(tenant: str, token: str) -> None:
    st.markdown("#### 对话区")
    session_key = st.session_state.get("active_session", "")
    if not session_key:
        st.info("请先在左侧队列中接管一个会话")
        return

    res = api_get(f"/api/v1/agent/replay/session/{session_key}", tenant, token)
    if not res.get("ok"):
        st.warning("读取会话失败")
        return
    data = res["data"]
    sess = data["session"]

    cols = st.columns(4)
    cols[0].metric("状态", sess["state"])
    cols[1].metric("轮次", sess["turn_count"])
    cols[2].metric("技能", sess["skill"] or "-")
    cols[3].metric("处理方", sess["handler"] or "-")

    st.divider()
    for msg in data["messages"]:
        role = msg["role"]
        with st.chat_message("assistant" if role in ("ai", "agent") else "user"):
            label = {"ai": "AI 小汇", "agent": f"坐席 {sess['handler']}", "system": "系统"}.get(
                role, "用户"
            )
            st.markdown(f"**{label}**：{msg['content']}")
            meta = []
            if msg.get("latency_ms"):
                meta.append(f"{msg['latency_ms']:.0f}ms")
            if msg.get("diagnosis_id"):
                meta.append(f"诊断#{msg['diagnosis_id']}")
            if meta:
                st.caption(" · ".join(meta))

    # AI 诊断步骤可视化
    if data["diagnoses"]:
        with st.expander("AI 诊断快照", expanded=False):
            for d in data["diagnoses"]:
                st.markdown(
                    f"- **{d['skill']}** → `{d['cause_code']}` → 处置 `{d['decision']}` → "
                    f"结论 `{d['outcome']}`"
                )
                if d.get("trace_id"):
                    st.caption(f"trace_id: {d['trace_id']}")

    # 人工回复
    text = st.chat_input("输入回复内容…")
    if text:
        r = api_post(
            "/api/v1/agent/message", tenant, token, {"session_key": session_key, "content": text}
        )
        if r.get("ok"):
            st.rerun()
        else:
            st.error(f"发送失败：{r.get('detail') or r.get('error')}")

    # 办结
    with st.expander("办结会话", expanded=False):
        resolution = st.text_area("办结结论", placeholder="例如：确认重复计费，已提交退款 380 元")
        c1, c2 = st.columns(2)
        diagnosis_code = c1.text_input("诊断编码", value="BILLING_DISPUTE_CONFIRMED")
        fix_action = c2.text_input("修复动作", value="refund")
        promote = st.checkbox("沉淀进知识库（人工办结回流）", value=True)
        if st.button("提交办结", type="primary"):
            r = api_post(
                "/api/v1/agent/close",
                tenant,
                token,
                {
                    "session_key": session_key,
                    "resolution": resolution or "已处理",
                    "skill": sess["skill"],
                    "diagnosis_code": diagnosis_code,
                    "fix_action": fix_action,
                    "automatable": True,
                    "promote_to_kb": promote,
                },
            )
            if r.get("ok"):
                st.success(
                    f"已办结，处理时长 {r['data'].get('handle_seconds')}s"
                    + ("，已回流知识库" if r["data"].get("promoted") else "")
                )
                st.session_state["active_session"] = ""
                time.sleep(1)
                st.rerun()
            else:
                st.error(f"办结失败：{r.get('detail') or r.get('error')}")


def render_customer_column(tenant: str, token: str) -> None:
    st.markdown("#### 客户信息")
    session_key = st.session_state.get("active_session", "")
    if not session_key:
        st.caption("未选中会话")
        return

    res = api_get(f"/api/v1/agent/replay/session/{session_key}", tenant, token)
    if not res.get("ok"):
        return
    sess = res["data"]["session"]

    st.markdown(f"**会话标识**：`{sess['session_key'][:16]}…`")
    st.markdown(f"**接入时间**：{sess['created_at'][:19]}")
    st.markdown(f"**是否转人工**：{'是' if sess['escalated'] else '否'}")

    st.divider()
    st.markdown("**历史工单**")
    tickets = api_get("/api/v1/tickets", tenant, token, limit=10)
    rows = tickets.get("data", []) if tickets.get("ok") else []
    if rows:
        for t in rows[:5]:
            st.caption(f"`{t['ticket_no']}` {t['status']} · {(t['title'] or '')[:24]}")
    else:
        st.caption("暂无历史工单")

    st.divider()
    st.markdown("**快捷操作**")
    if st.button("查看完整交接快照", use_container_width=True):
        for t in rows:
            if t.get("handover_snapshot"):
                st.markdown(t["handover_snapshot"])
                break
        else:
            st.caption("未找到快照")

    if st.button("刷新会话状态", use_container_width=True):
        st.rerun()


# ===========================================================================
# 指标看板
# ===========================================================================
def render_metrics_tab(tenant: str, token: str) -> None:
    days = st.slider("统计窗口（天）", 1, 30, 7, key="metrics_days")

    ov = api_get("/api/v1/agent/metrics/overview", tenant, token, days=days)
    if not ov.get("ok"):
        st.warning("指标读取失败：" + str(ov.get("error"))[:120])
        return
    d = ov["data"]
    if not d.get("total_sessions"):
        st.info(d.get("note", "暂无数据"))
        return

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("会话总数", d["total_sessions"])
    c2.metric("AI 一次解决率", f"{d['ai_first_contact_resolution_rate'] * 100:.1f}%")
    c3.metric("转人工率", f"{d['escalation_rate'] * 100:.1f}%")
    c4.metric("平均轮次", d["avg_turns_per_session"])
    c5.metric("平均办结时长", f"{d['avg_handle_minutes']:.2f} 分钟")

    st.divider()
    left, right = st.columns(2)

    with left:
        st.markdown("**7 日趋势**")
        trend = api_get("/api/v1/agent/metrics/trend", tenant, token, days=days)
        rows = trend.get("data", []) if trend.get("ok") else []
        if rows:
            df = pd.DataFrame(rows).set_index("day")
            st.line_chart(df[["sessions", "escalated", "resolved_by_ai"]])
        else:
            st.caption("暂无趋势数据")

    with right:
        st.markdown("**分技能对比**")
        skills = api_get("/api/v1/agent/metrics/skills", tenant, token, days=days)
        rows = skills.get("data", []) if skills.get("ok") else []
        if rows:
            st.dataframe(
                pd.DataFrame(rows)[["skill", "count", "resolution_rate", "escalation_rate", "avg_latency_ms"]],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("暂无数据")

    st.divider()
    left2, right2 = st.columns(2)
    with left2:
        st.markdown("**转人工原因分布**")
        reasons = api_get("/api/v1/agent/metrics/escalation-reasons", tenant, token, days=days)
        rows = reasons.get("data", []) if reasons.get("ok") else []
        if rows:
            st.bar_chart(pd.DataFrame(rows).set_index("trigger"))
        else:
            st.caption("暂无转人工记录")

    with right2:
        st.markdown("**AI 实测 vs 行业基准**")
        cmp_res = api_get("/api/v1/agent/metrics/compare", tenant, token, days=days)
        if cmp_res.get("ok"):
            c = cmp_res["data"]
            st.metric(
                "AI 平均处理时长",
                f"{c['ai_avg_handle_minutes']:.2f} 分钟" if c.get("ai_avg_handle_minutes") else "无数据",
                help=f"行业参考区间 {c['industry_aht_minutes'][0]}–{c['industry_aht_minutes'][1]} 分钟",
            )
            st.caption(f"对比结论：{c['verdict']}")
            st.caption(c["note"])

    st.divider()
    st.markdown("**坐席维度指标**")
    agents = api_get("/api/v1/agent/metrics/agents", tenant, token)
    rows = agents.get("data", []) if agents.get("ok") else []
    if rows:
        st.dataframe(
            pd.DataFrame(rows)[
                ["agent", "display_name", "presence", "current_load", "handled_sessions", "avg_handle_minutes"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("模型调用计量（成本观察）"):
        llm = api_get("/api/v1/agent/metrics/llm", tenant, token, days=days)
        if llm.get("ok"):
            L = llm["data"]
            st.caption(
                f"总调用 {L['total_calls']} 次 · 总 token {L['total_tokens']} · 失败 {L['failed_calls']} 次"
            )
            if L["by_kind"]:
                st.dataframe(pd.DataFrame(L["by_kind"]), use_container_width=True, hide_index=True)


# ===========================================================================
# 链路回放
# ===========================================================================
def render_replay_tab(tenant: str, token: str) -> None:
    st.markdown("按 `trace_id` 还原一次诊断的节点跳转、工具调用与耗时瀑布。")

    recent = api_get("/api/v1/agent/replay/traces", tenant, token, limit=30)
    rows = recent.get("data", []) if recent.get("ok") else []
    options = [""] + [r["trace_id"] for r in rows]
    picked = st.selectbox(
        "从最近诊断中选择",
        options,
        format_func=lambda x: x if not x else next(
            (f"{r['skill']} · {r['cause_code']} · {r['outcome']} · {r['trace_id'][:10]}…" for r in rows if r["trace_id"] == x),
            x,
        ),
    )
    manual = st.text_input("或直接输入 trace_id")
    trace_id = manual.strip() or picked

    if not trace_id:
        st.caption("请选择或输入一个 trace_id")
        return

    res = api_get(f"/api/v1/agent/replay/trace/{trace_id}", tenant, token)
    if not res.get("ok"):
        st.warning("未找到该 trace_id 的记录")
        return
    data = res["data"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("节点数", data["node_count"])
    c2.metric("工具调用", data["tool_count"])
    c3.metric("节点总耗时", f"{data['total_node_ms']:.0f} ms")
    c4.metric("最慢环节", data["slowest"].split(":")[-1] if data["slowest"] else "-")

    if data["conclusion"]:
        st.markdown("**最终结论**")
        cc = data["conclusion"]
        st.markdown(
            f"- 技能：`{cc['skill']}`\n"
            f"- 诊断：`{cc['cause_code']}` — {cc['cause_text']}\n"
            f"- 置信度：{cc['confidence']:.2f} · 风险分：{cc['risk_score']:.0f}\n"
            f"- 决策：`{cc['decision']}`（{cc['decision_reason']}）\n"
            f"- 结论：`{cc['outcome']}`"
        )
        if cc.get("evidence"):
            with st.expander("诊断证据"):
                for e in cc["evidence"]:
                    st.markdown(f"- {e}")
        if cc.get("matched_cases"):
            st.caption(f"命中案例：{', '.join(cc['matched_cases'])}")

    st.divider()
    st.markdown("**耗时瀑布**")
    if data["waterfall"]:
        st.dataframe(
            pd.DataFrame(data["waterfall"]).rename(
                columns={"name": "环节", "duration_ms": "耗时(ms)", "share": "占比", "status": "状态"}
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("**完整时间线**")
    for item in data["timeline"]:
        icon = "🔧" if item["kind"] == "tool" else "🔷"
        status = item.get("status", "")
        badge = "✅" if status == "ok" else ("⚠️" if status == "degraded" else "❌" if status else "")
        line = f"{icon} **{item['name']}** {badge} — {item['duration_ms']:.0f} ms"
        if item["kind"] == "tool":
            line += f" · HTTP {item.get('http_status')} · 重试 {item.get('retry_count')}"
        st.markdown(line)
        if item.get("detail"):
            st.caption(item["detail"][:200])
        if item["kind"] == "tool" and item.get("response"):
            with st.expander(f"响应 · {item['name']}", expanded=False):
                st.json(item["response"])


# ===========================================================================
# 主入口
# ===========================================================================
def main() -> None:
    ensure_state()
    render_sidebar()

    tenant = st.session_state["tenant"]
    token = st.session_state["token"]

    st.title("🎧 坐席工作台")
    if not token:
        st.warning("请先在左侧登录坐席账号。")
        st.info(
            "预置账号：`agent_zhao`（计费/账号安全组）、`agent_lin`（集成/权限组）、"
            "`lead_chen`（组长，全能） · 口令 `Agent@12345`"
        )
        return

    # 自动刷新（轮询）：Streamlit 无法接收服务端推送，实时推送见 WebSocket 控制台
    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="workbench_refresh")
    st.caption(
        f"每 {REFRESH_SECONDS} 秒自动刷新 · "
        f"需要真正的实时推送请打开 `/static/agent_console.html`（WebSocket 控制台）"
    )

    col_queue, col_chat, col_customer = st.columns([1.1, 2.4, 1.3], gap="medium")
    with col_queue:
        render_queue_column(tenant, token)
    with col_chat:
        render_conversation_column(tenant, token)
    with col_customer:
        render_customer_column(tenant, token)

    st.divider()
    tab_metrics, tab_replay = st.tabs(["📊 指标看板", "🔍 诊断链路回放"])
    with tab_metrics:
        render_metrics_tab(tenant, token)
    with tab_replay:
        render_replay_tab(tenant, token)


if __name__ == "__main__":
    main()
