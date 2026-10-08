# 架构设计说明 · SaaS 智能客服系统

> 本文档是本项目的**技术事实来源**（single source of truth）。所有实现必须与本文一致；
> 若实现需要偏离，先改本文并记录在 §11 决策记录，再改代码。
>
> 产品命名：**智汇云客服**（SaaS 产品支持场景）
> 被服务的虚构 SaaS 产品：**云枢协作平台**（含 IM 集成、文档协作、团队权限、订阅计费、账号安全五大模块）

---

## 1. 定位与边界

### 1.1 系统做什么

面向 SaaS 产品终端用户的智能客服系统，覆盖五类支持场景，其中三类需要**诊断—修复—验证**闭环：

| # | 技能 | 层次 | 是否闭环 |
|---|---|---|---|
| 1 | FAQ 功能咨询 | L1 问答 | 否（跑量场景） |
| 2 | 集成故障诊断与修复 | L3 诊断修复 | ✅ 诊断→远程修复→验证 |
| 3 | 权限访问问题诊断 | L3 诊断建议 | ✅ 诊断→多方案→用户选择→执行→验证 |
| 4 | 订阅计费争议 | L2 办结 | ✅ 查询→风控→自动调整/转人工 |
| 5 | 账号安全异常排查 | L3 诊断 | ✅ 检测→评分→处置 |

### 1.2 系统不做什么（明确边界）

- **不实现真实的第三方 SaaS 控制台**。Slack/钉钉/飞书的 Token 刷新、Webhook 修复、重授权等动作，在本地通过**受控 Mock 服务**实现，接口 schema 见 `docs/mock-boundary.md`。这是全项目**唯一**允许的"非真实"部分，且必须是"真调用 + 假后端"，绝不是"假调用"。
- **不接入真实支付通道**。计费退款只写本地账务表 + Mock 计费后端。
- **不做多机分布式部署**。本机单进程拓扑，生产演进路径见 §10。

### 1.3 功能真实性纪律

> 任何声明的功能必须真实实现且可演示。禁止 `TODO` 占位、禁止"预留接口"、禁止静默返回空实现。
> 唯一的例外是 §1.2 的 Mock 边界，且必须：
> 1. 代码中标注 `# TODO[生产对接]: <对接目标> · schema 见 docs/mock-boundary.md`
> 2. 在 `docs/mock-boundary.md` 中有完整请求/响应 schema
> 3. 在 README 的"已 Mock 清单"中列出

---

## 2. 环境约束（硬约束，不可绕过）

| 约束 | 说明 |
|---|---|
| 操作系统 | Windows 10/11 + PowerShell 7 |
| 无 Docker Desktop | 中间件一律**免安装 zip 版**前台/后台进程启动 |
| 无管理员权限 | 禁止 `Start-Service`、禁止注册 Windows 服务、禁止写 `Program Files` |
| Python | 3.11（项目本地 `.venv`） |
| 网络 | 运行时仅需访问 LLM 端点；Embedding 与向量检索全部本机完成 |

---

## 3. 分层架构

```
                        ┌──────────────────────────────────────────┐
   用户端 Web Widget ──► │  API 网关层 (FastAPI)                     │
   坐席工作台       ──► │  ① 租户识别中间件   X-Tenant-ID           │
   (WebSocket)          │  ② 鉴权            JWT（用户/坐席 双套）    │
                        │  ③ 限流            Redis 令牌桶 租户+端点   │
                        │  ④ 链路追踪        trace_id 注入           │
                        │  ⑤ 会话路由        Redis 会话归属           │
                        └───────┬──────────────────────┬────────────┘
                                │                      │
              ┌─────────────────▼────────┐   ┌─────────▼──────────────┐
              │  Agent 服务               │   │  坐席服务                │
              │  LangGraph 状态图          │   │  心跳/在线状态           │
              │  共享骨架 + 5 技能节点组   │   │  派单(轮询+技能组+负载)   │
              │  checkpointer = Postgres  │   │  转人工队列 / 接管锁      │
              └───┬──────────┬───────────┘   └─────────┬──────────────┘
                  │          │                         │
        ┌─────────▼──┐  ┌────▼─────────┐  ┌────────────▼──────────┐
        │ Redis      │  │ PostgreSQL   │  │ Chroma                │
        │ 会话状态机  │  │ 主 5432 (写) │  │ 案例库 / FAQ / 历史工单 │
        │ 坐席心跳    │  │ 从 5433 (读) │  │ (本机持久化)            │
        │ 转人工队列  │  │ 流复制       │  │                       │
        │ 分布式锁    │  │ 全表tenant_id│  │                       │
        │ 令牌桶      │  │ checkpointer │  │                       │
        └────────────┘  └──────────────┘  └───────────────────────┘
```

### 3.1 代码分层与依赖方向

```
app/
  core/      配置 / 日志 / AgentService 隔离层 / PG·Redis 客户端封装 / 安全原语
  gateway/   租户中间件 / JWT / 限流 / 会话路由 / trace 中间件
  agents/    LangGraph 状态图骨架 + 各技能节点组
  skills/    技能定义（元数据 + 提示词 + 节点装配 + 知识装配）
  tools/     业务工具（查询集成配置 / 诊断 / 远程修复 / 验证 / 风控 / 账单 …）
  services/  会话·工单·坐席·派单·指标·案例 领域服务
  models/    SQLAlchemy ORM（全表 tenant_id）+ Pydantic schema
  mocks/     统一 Mock 服务（单进程多路由 + 异常开关）
  api/       用户端 API / 坐席端 API / WebSocket / SSE
```

**依赖方向严格单向**：`api → gateway → services → (agents|models|core)`，`agents → tools → core`。
`core` 不依赖任何上层；`skills` 只被 `agents` 装配。**禁止反向 import。**

### 3.2 AgentService 隔离层（关键设计）

所有业务代码访问 Agent 运行时 / LLM / 向量库 / 工具，**一律经过 `app/core/agent_service.py`**，业务层不得直接 `import langgraph` 或直接实例化 LLM 客户端。

隔离层职责：

| 职责 | 说明 |
|---|---|
| 模型调用 | 统一超时、重试、降级、Token 计量 |
| 状态图执行 | 统一 `thread_id` 构造、checkpointer 注入、trace 传播 |
| 工具调用 | 统一 Pydantic 出参校验、异常归一化 |
| 向量检索 | 统一 Embedding 供给与集合管理 |
| 降级决策 | 超时/报错时返回结构化降级信号，由业务层决定话术 |

**收益**：模型供应商、状态图引擎、向量库三者任一可替换；业务代码零耦合。

---

## 4. Agent 状态图骨架

五个技能**共用一套骨架**，只替换 `observe / diagnose / act / verify` 四段的工具集与提示词。这是本项目最重要的结构决策：避免五套图各写一遍，也让"断点续接""澄清追问""步数熔断"天然统一。

```
                    ┌──────────────┐
        ┌──────────►│  route_skill │  技能路由（分类器 + 置信度阈值）
        │           └──────┬───────┘
        │                  │ 置信度不足
        │                  ▼
        │           ┌──────────────┐
        │           │   clarify    │◄──────────── 需要用户补充信息
        │           └──────┬───────┘              (interrupt 挂起)
        │                  │                  ▲
        │                  ▼                  │
        │           ┌──────────────┐          │
        │           │   collect    │──────────┘ 缺参
        │           └──────┬───────┘
        │                  ▼
        │           ┌──────────────┐
        │           │   observe    │  查询：集成配置 / 权限 / 账单 / 登录记录
        │           └──────┬───────┘
        │                  ▼
        │           ┌──────────────┐
        │           │  diagnose    │  诊断推理 + 风险评分 + 结论落库
        │           └──────┬───────┘
        │                  ▼
        │           ┌──────────────┐
        │           │     act      │  自动修复 / 生成多方案 / 风控决策
        │           └──────┬───────┘
        │                  ▼
        │           ┌──────────────┐  失败且步数未熔断
        │           │   verify     │───────────┐
        │           └──────┬───────┘           │
        │                  │ 通过               │
        │                  ▼                   │
        │           ┌──────────────┐           │
        └───────────┤  close/escalate│◄─────────┘ 步数熔断 → 转人工
      重试/换方案   └──────────────┘
```

**关键机制**

| 机制 | 实现 | 验收对应 |
|---|---|---|
| 状态持久化 | `checkpointer = AsyncPostgresSaver`，`thread_id = tenant_id:session_id[:topic]` | 服务重启后从断点续接，用户无需重述 |
| 澄清挂起 | 状态图中 `clarify` 节点产出待问项后结束本轮；下一轮消息以同一 `thread_id` 恢复 | 技能 1-5 的"澄清追问" |
| 步数熔断 | 状态中 `step_count`，超过阈值（默认 3 步未解决）走 `escalate` | 智能转人工触发条件之一 |
| 诊断落库 | `diagnose` 节点写 `support_diagnoses` 表 | 工单快照 / 指标看板 / 链路回放 / 案例断言四处复用同一份数据 |

### 4.1 各技能节点组装配

| 技能 | observe | diagnose | act | verify |
|---|---|---|---|---|
| 1 FAQ | 向量检索 FAQ | 无（命中即答） | 生成回答 | 无 |
| 2 集成故障 | `query_integration_config` | Token 过期 / Webhook 失效 / 权限变更 三分类 | `attempt_remote_fix`（刷新 Token / 修 Webhook / 重授权） | `verify_fix` 真调集成端点发验证消息 |
| 3 权限访问 | 查分享设置 / 组织策略 / 版本 | 三类成因判定 | 生成多方案 → 用户选择 → 执行 | 复核可访问性 |
| 4 计费争议 | 查账单 / 用量 | 五维风控评分 | 自动调整 / 转人工审批（幂等+签名+时间戳） | 账务状态复核 |
| 5 账号安全 | 异常登录检测 | 风险评分 | 锁定 / 通知 / 转人工 | 登录状态复核 |

---

## 5. 生产级四件套

### 5.1 网关路由

| 能力 | 实现要点 |
|---|---|
| 租户识别 | `X-Tenant-ID` → 校验租户有效（**读主库**，避免复制延迟把新租户判为不存在）→ 存入 `contextvars.ContextVar`（**绝不用全局变量**，否则并发串租户） |
| 租户数据隔离 | Session 工厂按 ContextVar 注入 `tenant_id` 过滤；**读写两条路径都要过滤**（只读路径是最易漏的地方） |
| 租户内归属 | 会话除租户外还绑定终端用户（`user_ext_id`），会话标识用随机值而非时间戳，**不可枚举** |
| 用户端身份 | 会话级**访客令牌**（`aud=user`）：首轮对话签发并在响应回传，此后续聊/读消息/读状态**必须**携带；令牌内含 `sid` **绑定该会话**——"自己的令牌 + 他人的会话标识"会被拒。会话列表按令牌身份过滤，杜绝枚举他人会话 |
| 鉴权 | JWT 双套：用户端（`aud=user`）/ 坐席端（`aud=agent`），密钥分离，角色（坐席/组长）；坐席凭据校验**读主库** |
| 密钥纪律 | 启动时自检签名密钥：占位值/长度不足/两端密钥相同 → `app.insecure_secret` 告警（防止"能跑但等于没有鉴权"） |
| 限流 | Redis 令牌桶，维度 = 租户 + 端点（默认 60 req/min） |
| 会话路由 | `tenant_id + session_id` → Redis 查会话归属 → `AI_HANDLING / QUEUED / AGENT_HANDLING / CLOSED` |

> **用户端身份的能力边界（如实说明）**：本系统没有用户登录页，首轮身份只能"自报"，
> 因此首轮是**首次绑定**（自报 + 立刻签发令牌），此后一律凭令牌访问。
> 这消除了"猜到会话标识即可读他人会话"与"省略身份字段绕过校验"两条路径，
> 但**不等于**完整的用户身份核验——那需要引入用户登录/OIDC（已列入未完成清单）。

### 5.2 Redis 会话

- **状态机**：`AI_HANDLING → QUEUED → AGENT_HANDLING → CLOSED`，所有跃迁写审计日志
- **坐席心跳**：每 10s 上报，TTL 30s；过期自动摘除，其名下会话重新入队
- **掉线巡检**：API 进程内后台任务按 `AGENT_OFFLINE_SWEEP_SECONDS`（默认 30s，0=关闭）周期巡检所有启用租户，回收"名下有进行中会话但心跳已过期"的坐席会话。**主动**下线由心跳接口即时回收，**被动**掉线（进程崩溃/断网）不发任何请求，只能靠此巡检兜住；另有 `POST /api/v1/agent/offline-recovery` 供运维手动触发。巡检幂等
- **分布式锁**：`SET NX EX`，保证同一会话同一时刻只有一个处理方
- **转人工队列**：List（FIFO）+ 优先级字段（VIP 租户 / 高金额优先）；**出队/查看按租户过滤**（队列键共享，不过滤会导致跨租户派单）
- **令牌桶**：限流计数
- **nonce 窗口**：计费接口防重放

### 5.3 读写分离

- PG 主从流复制：脚本 `scripts/pg-init.ps1`（双实例 initdb）+ `scripts/pg-replica.ps1`（`pg_basebackup` + standby）
- SQLAlchemy 路由：写 → 5432，只读查询 → 5433
- **只读走从库**：工单列表 / 历史会话 / 指标查询 / 案例检索
- **回退**：从库不可用时读自动回退主库 + 结构化告警日志（**不使用易抖动的性能断言作为必过项**，见 §11 ADR-003）

### 5.4 坐席管理

- 账号：注册/登录（JWT）/ 角色（坐席、组长）
- 在线状态：上线 / 小休 / 离线，心跳驱动
- 派单策略：轮询 + 技能组标签匹配（`integration/permission/billing/account/faq`）+ 负载（当前会话数最少优先）
- 工作台：待接入队列实时刷新（**WebSocket 真推送**）/ 接管 / 查看 AI 诊断快照 / 人工对话 / 办结
- 管理视图：在线坐席列表 / 人均会话数 / 平均接管时长

---

## 6. 数据模型约定

**所有业务表必须含 `tenant_id`（NOT NULL + 索引）**，无例外。表清单：

| 域 | 表 |
|---|---|
| 租户与身份 | `tenants`、`users`、`agents`(坐席)、`agent_skills` |
| 会话 | `sessions`、`messages`、`session_events` |
| 工单 | `tickets`、`ticket_messages`、`escalations` |
| 诊断 | `support_diagnoses`、`diagnosis_steps`、`tool_invocations` |
| 计费 | `subscriptions`、`invoices`、`usage_records`、`billing_adjustments`、`idempotency_keys` |
| 账号安全 | `login_events`、`security_incidents` |
| 案例与知识 | `cases`（案例库）、`case_feedback`（人工办结回流） |
| 可观测 | `llm_calls`、`ai_task_logs`、`agent_metrics` |

通用字段：`id`、`tenant_id`、`created_at`、`updated_at`；审计类额外含 `trace_id`。

**案例库字段**（决策 ADR-005，用于同时承担 RAG 语料 + 集成测试断言依据）：

```
case_id, tenant_profile, skill(1-5), scenario, symptom,
expected_diagnosis, expected_action, expected_verify_result,
risk_level, source_ref
```

---

## 7. 可观测性

### 7.1 日志规范（D1 起冻结，禁止事后补）

- 结构化：`structlog`，JSON 输出到 `logs/app.log`，控制台用彩色渲染
- **全链路 `trace_id`（UUID）**：贯穿 网关 → 会话 → 意图 → 工具 → RAG → LLM → 响应 → 工单
- 固定字段：`trace_id, tenant_id, session_id, node, tool, duration_ms, status, error_code`
- **禁止 `print`**：所有模块统一 logger；`print` 视为缺陷

### 7.2 trace 传播

```
HTTP 请求 → trace 中间件生成/透传 trace_id → contextvars
         → AgentService 注入图 config.metadata
         → 每个图节点/工具调用读 contextvars 打点
         → 出站 HTTP（Mock 服务）带头 X-Trace-ID
         → 落 ai_task_logs / tool_invocations，供链路回放页查询
```

### 7.3 链路回放

输入 `trace_id` → 查询 `ai_task_logs` + `tool_invocations` + `diagnosis_steps` → 展示节点跳转序列、工具调用、耗时瀑布。

---

## 8. 容错降级矩阵

| 异常 | 检测方式 | 降级策略 |
|---|---|---|
| Mock 超时 | 超时阈值 5s | 重试 2 次 → 失败转人工 + 建工单 |
| RAG 空检索 | 结果数 = 0 或最高相似度 < 阈值 | 规则兜底话术 + 标记低置信度 |
| LLM 超时/报错 | 超时 + 异常捕获 | 友好提示 + 转人工 |
| 工具返回格式异常 | Pydantic 校验 | 兜底话术 + 转人工 + 记日志 |
| 计费接口 5xx | HTTP 状态码 | 查 `idempotency_keys` 状态 → 重试 / 查询确认 |
| Token 刷新失败 | 401 | 降级为引导重新授权（生成链接） |
| 服务重启/崩溃 | checkpointer | 从 Postgres 恢复图状态，断点续接，用户无需重述 |
| 从库不可用 | 连接异常 | 读回退主库 + 告警日志 |
| Redis 不可用 | 连接异常 | 降级为进程内状态（记录降级标记，恢复后回填） |

每条降级路径**必须有对应测试**（`tests/exceptions/`）。

---

## 9. 技能路由与智能转人工

### 9.1 转人工触发条件（矩阵）

| 条件 | 阈值 |
|---|---|
| 步数超限未解决 | > 3 步 |
| 情绪 | 愤怒且强度 ≥ 7 |
| 金额 | > 100 元 |
| 需管理员介入 | 组织策略变更 / 重授权失败 |
| 历史投诉 | > 2 次 |
| 用户显式要求 | 立即 |

### 9.2 交接快照（Markdown 附件随工单）

已排查项 + 已执行操作 + 当前状态 + 建议话术。

### 9.3 真实流转链路

```
AI 触发 → 写 escalations + Redis 转人工队列
       → 派单服务按 (技能组 ∩ 在线 ∩ 负载最小) 分配
       → WebSocket 推送给坐席（真推送）
       → 坐席接管：Redis 分布式锁抢会话（抢不到则提示已被接管）
       → AI 停止介入（会话状态机切 AGENT_HANDLING）
       → 人工对话（消息经同一会话通道）
       → 办结：写结论 → 可选回流案例库（ADR-006）
```

---

## 10. 生产演进路径

| 本机（现状） | 生产演进 |
|---|---|
| Redis 单实例前台进程 | 哨兵 → 集群 |
| PG 单主单从流复制 | 托管 PG / 多只读副本 / 连接池中间件 |
| 单进程 FastAPI + 后台任务 | 多副本 + 独立 Worker + 消息队列 |
| Chroma 本机持久化 | 独立向量服务 / 托管向量库 |
| 本机 zip 中间件 | 容器编排（K8s）+ 配置中心 + 密钥管理 |
| JWT 本地签发 | 统一身份中心（OIDC） |
| 单机限流 | 网关级全局限流 + 熔断 |

---

## 11. 决策记录（ADR）

| # | 决策 | 备选 | 选定理由 | 影响 |
|---|---|---|---|---|
| ADR-001 | **Embedding 用项目内确定性本地实现**（字符 n-gram 哈希 + IDF 加权 + 显式维度），可选升级为本地句向量模型 | ① 第三方在线 embedding API ② 纯关键词检索 | 所选 LLM 供应商**不提供 embeddings 端点**（官方接口清单无该能力），走在线 embedding 会引入第二个供应商与密钥；纯关键词检索又无法满足向量库要求。本地确定性实现零密钥、零下载、可复现、维度自控 | 检索层定义 `EmbeddingProvider` 接口；默认 provider 为本地实现；配置项显式声明维度，**禁止硬编码维度** |
| ADR-002 | **采用当前世代 LangGraph 1.2 + LangChain 1.4，`StateGraph` 显式建图** | ① 旧世代 0.2/0.3 ② 高层封装 `create_agent` | 旧世代与 Python 3.11 新补丁存在依赖风险；高层封装隐藏节点级控制，无法做步数熔断、节点级 trace、诊断落库 | 不使用任何 `AgentExecutor` 式旧 API；图节点全部自写 |
| ADR-003 | **主从流复制保留，但验收口径改为稳定性断言** | ① 用"复制延迟 < 2s"作必过项 ② 放弃主从 | < 2s 受磁盘/杀软/负载影响，是不可控断言；"写入后轮询读到一致值 + 从库宕机回退主库 + 告警日志有记录"同样证明能力且稳定 | 验收脚本用轮询一致 + 回退日志双证据 |
| ADR-004 | **坐席"实时推送"用独立 WebSocket 控制台承载；Streamlit 只做看板与回放** | 仅用 Streamlit + 轮询 | Streamlit 的执行模型无法作为 WebSocket 客户端接收服务端主动推送，"实时"一词必须由真推送承载 | 交付物含 `app/static/agent_console.html`（真 WebSocket）+ Streamlit 看板（轮询） |
| ADR-005 | **案例库 CSV 含"期望诊断/期望动作/期望验证结果"三列** | 仅 `question/answer` | 案例库同时承担 RAG 语料与集成测试断言依据，缺断言列则测试只能断言"有回复" | 数据准备阶段必须填全三列，集成测试据此断言 |
| ADR-006 | **增加"人工办结回流案例库"闭环** | 办结即结束 | 人工解决的每条新故障应沉淀为 AI 知识，否则系统无学习闭环 | 新增 `case_feedback` 表 + `seed.py --promote` |
| ADR-007 | **业务代码一律经 `app/core/agent_service.py` 访问运行时能力** | 业务层直接调框架 | 保证模型/图引擎/向量库三者可替换 | 引入一层薄封装，禁止业务层 import 框架 |

### 11.1 实测得到的框架硬约束（Spike 发现，必须遵守）

以下约束由 `scripts/spike_verify.py` 实跑得出，写业务代码时不得违反：

| # | 约束 | 现象 | 规则 |
|---|---|---|---|
| C-1 | **状态类型必须是模块级名字** | 在函数内写 `S = MyState` 再用 `def router(state: S)`，注册条件边时抛 `NameError: name 'S' is not defined` | 状态用 `TypedDict` 定义在模块顶层，所有节点/路由函数注解直接写该类型名；**禁止局部别名** |
| C-2 | **向量化适配器的查询方法必须返回"列表的列表"** | 客户端协议要求 `embed_query` 返回嵌入列表；返回单条向量会抛 `TypeError: 'float' object cannot be converted to 'Sequence'` | 适配器 `embed_query` 统一返回 `list[list[float]]`，即使只查一条 |
| C-3 | **集合元数据只接受标量** | 传入 `int` 维度会导致集合打开失败 | 元数据值统一转字符串 |
| C-4 | **旧世代 Agent 执行器 API 已移除** | `langchain 1.4.3` 不含 `create_tool_calling_agent`，也没有 `langchain_classic` | 不得依赖任何 AgentExecutor 式 API（与 ADR-002 一致） |
| C-5 | **距离度量必须用 `hnsw:space` 键写入，且分数换算要匹配度量** | 写 `"distance": "cosine"` 会被客户端忽略、静默退回 L2 度量；此时若仍按 `1-距离` 算相似度，会把真实命中算成负分而被阈值全部过滤（表现为"检索永远为空"） | 集合元数据键名固定为 `hnsw:space`；相似度换算按实际度量分支；集合元数据记录"索引签名"，配置变化时自动重建 |
| C-6 | **异步状态存储需要选择器事件循环** | Windows 下 asyncio 默认 Proactor 循环，psycopg 异步连接直接抛 `Psycopg cannot use the 'ProactorEventLoop' to run in async mode` | `app/core/platform_compat.py` 在所有异步代码之前切换为 `WindowsSelectorEventLoopPolicy`；所有入口必须导入该模块 |
| C-7 | **"等待用户补充信息"必须用 `interrupt()`，不能靠"跑完一轮"** | 若澄清节点直接返回并连到 END，图会被判定为**已完成**（`aget_state().next` 为空），`Command(resume=...)` 也就无处续接 | 需要跨轮挂起的节点一律 `interrupt(payload)`；恢复时以 `Command(resume=值)` 凭同一 `thread_id` 继续。这是技能 1-5 共用"澄清追问"与"重启续接"两种场景的统一机制（已实测跨进程通过） |
| C-8 | **ORM 模型必须显式声明 `__tablename__`** | 自动复数化把 `Diagnosis` 变成 `diagnosiss`，外键解析失败：`NoReferencedTableError` | 每个模型显式声明表名，不依赖命名推导 |
| C-9 | **租户闸门依赖"租户表名单"，名单来自模型导入** | 若入口未导入 ORM 模型，名单为空 → 闸门**静默失效**（不报错，只是不拦截），跨租户风险被悄悄打开 | 提供 `app/core/database.py:ensure_models_loaded()`，所有入口必须显式调用；验证脚本必须包含"无租户上下文应被拒绝"的正向断言，而不是只测正常路径 |

---

## 12. 交付纪律

1. **表述纪律**：交付物内**不得出现**外部参考项目的名称、内部类名、文档链接；不得使用"改造/二次开发/魔改/移植/剥离/底座/上游项目"等派生性表述。系统一律自述为"本项目分层设计"。
2. **密钥纪律**：`.env` 不提交、不入交付物；`.env.example` 只含占位符。任何真实密钥轮换后再用。
3. **证据纪律**：每个阶段产出 `docs/evidence/<阶段>/` 下的"命令原文 + 原始输出 + 截图"，并在 README 维护"声明 → 证据文件"对照表。**说到的必须能点到证据**。
4. **待办纪律**：不得把待办标完成；命令以实际运行结果为准；未完成项进"未做清单"并写明原因。

---

## 13. 实现现状对照

本节的目的是让读者能快速判断"文档描述的东西是否真的存在"。**逐项均可复跑取证**。

| 架构要素 | 实现位置 | 验证方式 | 状态 |
|---|---|---|---|
| 网关四件套（租户/鉴权/限流/会话路由） | `app/gateway/middleware.py` | `tests/integration/`、`tests/exceptions/` | ✅ |
| 租户闸门（无上下文拒绝访问租户表） | `app/core/database.py` | `scripts/verify_storage.py` | ✅ |
| 读写分离 + 从库回退 | `app/core/database.py` | `verify_storage.py`、异常专项 | ✅ |
| Redis 会话状态机 | `app/services/session_service.py` | 端到端、状态跃迁用例 | ✅ |
| 坐席心跳 / 队列 / 分布式锁 | `session_service.py`、`redis_client.py` | `redis-primitives`、并发用例 | ✅ |
| 状态图骨架 + 五技能 | `app/agents/graph/`、`app/skills/` | `check_framework.py`、端到端 | ✅ |
| Postgres 状态持久化 + 断点续接 | `app/services/agent_service.py` | `verify_checkpointer.py` | ✅ |
| 澄清挂起 / 恢复 | `support_graph.py`（`interrupt`） | 端到端澄清用例 | ✅ |
| 转人工全链路 + 交接快照 | `app/services/escalation_service.py` | 端到端、工作台验证 | ✅ |
| 办结回流知识库 | `escalation_service.close()` + `knowledge_service.py` | 端到端（`feedback_id`） | ✅ |
| 统一 Mock 后端 + 异常模式 | `app/mocks/` | `verify_mock.py` | ✅ |
| 工具层降级归类 | `app/tools/client.py` | `verify_tools.py`、异常专项 | ✅ |
| 指标看板 | `app/services/metrics_service.py` | `run-workbench-check.ps1` | ✅ |
| 链路回放 | `app/services/replay_service.py` | 同上（6 节点/3 工具） | ✅ |
| 用户提问页 | `app/static/user_widget.html` | 页面可达 + 手工流程 | ✅ |
| 坐席实时控制台（WebSocket） | `app/static/agent_console.html` + `console_ws.py` | 同上（真推送断言） | ✅ |
| Streamlit 工作台 | `app/dashboard.py` | 页面可达 | ✅ |
| SSE 流式回复 | `app/api/v1/user_routes.py` | 接口可用 | ✅ |
| 容器化交付物 | `deploy/` | 文件存在（本机无 Docker，未做容器内验证，见交付清单） | ⚠️ 仅交付物 |

**未实现/受限项**一律记录在 `docs/交付清单.md` 的"未做项"与"受阻项"两节，不在此重复。

