# 智汇云客服 · SaaS 产品支持智能客服系统

面向 SaaS 产品终端用户的智能客服系统。覆盖五类支持场景，其中三类需要
**诊断 → 处置 → 验证**闭环；转人工为真实流转（队列 → 派单 → 坐席接管 → 办结 → 回流）。

被服务的虚构 SaaS 产品：**云枢协作平台**（含 IM 集成、文档协作、团队权限、订阅计费、账号安全五大模块）。

- 环境：Windows 10/11 + PowerShell 7 + Python 3.11，**无 Docker、无管理员权限**
- 中间件：PostgreSQL 16 免安装 zip（主从流复制）+ Redis zip + Chroma（本机持久化）
- 大模型：DeepSeek（`deepseek-chat`）
- 向量化：项目内确定性本地实现（**无需 Embedding API**，见 §7）

---

## 1. 能力概览

### 五个技能

| # | 技能 | 层次 | 闭环 |
|---|---|---|---|
| 1 | FAQ 功能咨询 | L1 问答 | 向量检索 + 生成回答，空检索走兜底话术 |
| 2 | 集成故障诊断与修复 | L3 诊断修复 | 诊断三分类 → 远程修复 → **真投递验证可达** |
| 3 | 权限访问问题诊断 | L3 诊断建议 | 诊断 → **多方案由用户选择** → 执行 → 复核 |
| 4 | 订阅计费争议 | L2 办结 | 五维风控评分 → 自动调整 / 转人工审批（幂等+签名+防重放） |
| 5 | 账号安全异常排查 | L3 诊断 | 异常登录检测 → 风险评分 → 锁定/通知/转人工 |

### 工程能力

- **多租户**：`X-Tenant-ID` 识别；所有业务表带 `tenant_id`；数据库层设**租户闸门**——
  未绑定租户上下文访问租户表会**直接拒绝**（宁可报错也不越权）
- **读写分离**：写走主库 5432，只读查询走从库 5433；从库不可用自动回退主库并告警
- **状态持久化**：诊断流程状态落在 PostgreSQL，**应用重启后可从断点续接，用户无需重述**
- **全链路追踪**：structlog + `trace_id` 贯穿 网关→会话→意图→工具→RAG→模型→响应→工单
- **坐席体系**：心跳在线状态、技能组派单、分布式锁接管（同一会话唯一处理方）、办结回流知识库
- **容错降级**：9 类异常各有明确降级路径，逐条有测试

---

## 2. 快速开始（Windows，5 步）

> 全程无需管理员权限，不注册 Windows 服务，不使用 Docker。

### 前置：获取本机工具链（一次性）

```powershell
cd <工程目录>
pwsh -File scripts/fetch-toolchain.ps1
```
下载并解压到 `项目二\.toolchain\`（PostgreSQL 16、Redis，约 300MB 下载 / 1.2GB 解压）。
工具链与工程同级、位于 `项目二` 内，因此 `项目二` 是自包含单元，可整体拷贝。

> **含中文路径时还需一步（管理员权限）**：PostgreSQL 存在已知缺陷——`initdb` 在
> **二进制路径含非 ASCII 字符**时会失败（`invalid byte sequence for encoding "UTF8"`）。
> 工程路径含中文时，用两个纯 ASCII 目录联接给它一个 ASCII 视角（真实文件仍在工程内）：
>
> ```powershell
> # 在**管理员** PowerShell 中执行一次
> pwsh -File scripts/setup-ascii-links.ps1
> # → D:\pgtool → <工程>\..\.toolchain\pg16\pgsql   （二进制）
> # → D:\pgdata → <工程>\data\pg                    （数据）
> ```
>
> `scripts/serve.ps1` 会自动探测并使用这两个联接；纯 ASCII 路径的机器无需执行本步。
> 详见 `docs/environment-constraints.md` 的 E-9。

### 第 1 步：创建虚拟环境并安装依赖

```powershell
# 若本机没有 Python 3.11，可用 uv 安装（脚本会自动落到 .toolchain）
uv python install 3.11
uv venv --python 3.11 .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

### 第 2 步：配置密钥

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少填写 LLM_API_KEY（DeepSeek 平台申请）
```

`.env` 不提交、不入交付物；`.env.example` 只含占位符。

### 第 3 步：搭建 PostgreSQL 主从并初始化数据库

```powershell
pwsh -File scripts/pg-setup-all.ps1      # 主库 + 从库 + 流复制
.\.venv\Scripts\python.exe -m app.cli init-db    # 建表（25 张）
.\.venv\Scripts\python.exe -m app.cli seed       # 种子数据 + 知识库索引（44 条）
```

### 第 4 步：一键启动全部服务

```powershell
pwsh -File scripts/serve.ps1 -WithDashboard
```

该脚本会起 PostgreSQL 主从、Redis、Mock 后端、API 网关、坐席工作台，
然后**保持前台运行**并每 30 秒打印一次心跳。
（本环境会在父进程结束时回收子进程，因此需要一个长驻承载进程。）

### 第 5 步：打开页面

| 用途 | 地址 |
|---|---|
| 👤 **用户提问** | http://127.0.0.1:8011/static/user_widget.html |
| 🎧 **坐席实时控制台**（WebSocket 推送） | http://127.0.0.1:8011/static/agent_console.html |
| 📊 **坐席工作台**（看板 + 链路回放） | http://127.0.0.1:8502 |
| 📖 接口文档 | http://127.0.0.1:8011/docs |

**坐席账号**：`agent_zhao`（计费/账号安全）· `agent_lin`（集成/权限）· `lead_chen`（组长）
**口令**：`Agent@12345` ｜ **租户**：`t-001`（标准）· `t-002`（旗舰）

> 端口刻意用 **8011 / 8010 / 8502**，避开最常被占用的 8000/8501。
> 端口被其它程序占用时新进程会绑定失败而旧服务仍在响应，
> 表现为"接口全 404 但 /health 正常"，极难定位（本项目实测踩到过）。

---

## 3. 三分钟演示剧本

> 建议**两个浏览器窗口并排**：左「用户提问」，右「坐席实时控制台」。

### 0:00–0:40 技能 FAQ 与集成故障自愈

**窗口 A（用户）** 依次点击快捷问题：

1. `怎么设置消息免打扰？` → 走知识库作答，10 秒内给出答案
2. `工作区 ws-1001 的 slack 机器人不回复了`

预期：AI 贴出诊断结论 **`TOKEN_EXPIRED`**（置信度 0.95），
并显示「已执行：访问令牌已刷新」「验证结果：成功投递验证消息到 #support-verify，回执 msg-xxxxx」。
展开「查看诊断依据」可看到令牌过期、回调可达性、授权范围等证据。

**讲解要点**：结论不是模型编的——`验证可达` 是真的向该工作区投递了一条消息并拿到回执，
这就是"诊断→修复→验证"闭环的证据。

### 0:40–1:20 权限诊断：多方案由用户选择

**窗口 A** 再发：`文档 doc-771 打不开，提示没有访问权限`

预期：诊断 **`SHARE_NOT_ENABLED`**，并给出两个按钮（只读分享 / 可编辑分享），
推荐项高亮。点其中一个 → 复核"该账号现在可以正常打开文档"。

**讲解要点**：分享范围涉及数据安全，所以**不由 AI 代办**，而是给方案让用户选。

### 1:20–2:20 转人工真实流转（核心）

**窗口 A** 发：`这个月账单重复计费了，账号 acct-501，我要退款`

预期：AI 给出风控结论——风险分 **55**、命中「金额超阈值」硬性规则 → **转人工审批**，
回复里带审批单号，界面提示已转接人工。

**窗口 B（坐席）** 打开实时控制台，选 `agent_zhao`，点「登录并连接」。

预期：中间「实时事件流」**立刻弹出**（服务端主动推送）：
```
🆕 新转人工：billing · 计费争议需人工审批
📌 已派单给 赵工（billing）
```

点左栏「接管」→ 再回**窗口 A 发一句话** → 界面显示「您的消息已转交给人工客服」（**AI 已停止介入**）。

回**窗口 B 发一条消息** → **窗口 A 在数秒内看到坐席回复**。

最后窗口 B 点「办结」，勾选「沉淀进知识库」。

**讲解要点**：转人工不是"弹个提示"，而是真的走 Redis 队列 → 派单（技能组∩在线∩负载最小）
→ WebSocket 推送 → 分布式锁接管 → 办结 → 回流。控制台是**真 WebSocket**，不是轮询。

### 2:20–3:00 可观测与多租户

**窗口 B** 点「指标看板」→ 展示会话数、AI 一次解决率、转人工率、平均轮次、
平均办结时长、7 日趋势、分技能对比、转人工原因分布、坐席维度指标。

点「诊断链路回放」→ 选一条 `trace_id` → 展示**节点跳转序列**（route→observe→diagnose→act→verify→finalize）、
**工具调用**（含出站地址与耗时）、**耗时瀑布**与最终结论。

最后把**窗口 A 的租户切到 `t-002`**，重复发一次消息 → 数据与 `t-001` 完全隔离。

---

## 4. 目录结构

```
saas-support-cs/
├─ app/
│  ├─ core/        配置 · 结构化日志 · 链路上下文 · 平台兼容 · 本地向量化
│  │               向量库 · 模型客户端 · Redis 客户端 · 数据库(读写分离+租户闸门)
│  ├─ gateway/     租户中间件 · JWT 双套鉴权 · 令牌桶限流 · 链路中间件
│  ├─ agents/      LangGraph 状态图骨架与节点
│  ├─ skills/      五个技能 + 注册表（关键词优先 + 模型兜底路由）
│  ├─ tools/       工具客户端（超时/重试/契约校验）+ 四域业务工具
│  ├─ models/      SQLAlchemy ORM（25 张表）
│  ├─ services/    会话状态机 · 转人工派单 · 坐席 · 知识库 · 指标 · 链路回放 · 事件总线
│  ├─ mocks/       统一 Mock 业务后端（四域 + 异常模式开关）
│  ├─ api/v1/      用户端 / 坐席端 / WebSocket 控制台路由
│  ├─ main.py      应用入口（自建选择器事件循环，见 §7）
│  ├─ dashboard.py Streamlit 坐席工作台
│  └─ static/      用户提问页 · 坐席实时控制台
├─ scripts/        建库/起服/验证/自检 等可复跑脚本
├─ tests/          unit(76) · integration(38) · exceptions(19) · load(6)
├─ data/           样本数据 · 向量库 · PG/Redis 数据（均不入库）
├─ docs/           architecture.md · mock-boundary.md · environment-constraints.md
│                  evidence/（声明→证据对照表 + 实测输出）
├─ deploy/         docker-compose.yml / Dockerfile（交付物，本机验证不依赖）
└─ requirements.lock.txt
```

---

## 5. 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # 139 passed
```

| 层次 | 数量 | 内容 |
|---|---|---|
| 单元 | 76 | 路由与实体抽取、风控评分表与阈值表、风险信号表、向量化与相似度、向量库、口令/JWT/签名、转人工触发矩阵、状态合并语义 |
| 集成 | 38 | 五技能端到端（断言到诊断编码/动作/验证结果）、转人工全流程、接管互斥、掉线重排队、计费幂等、租户隔离、读写分离、坐席鉴权、链路落库 |
| 异常专项 | 19 | Mock 五种异常模式、工具重试与归类、结构校验拦截、模型不可用、空检索兜底、从库宕机回退、Redis 不可用降级、限流、防重放、分布式锁、跨进程状态持久化、非法状态跃迁 |
| 并发/负载 | 6 | 20 并发跨租户隔离、多坐席并发心跳、并发派单不重复、复制延迟、从库恢复模式校验 |

### 分阶段验证脚本

```powershell
.\.venv\Scripts\python.exe scripts\spike_verify.py        # 扩展点 18 项
.\.venv\Scripts\python.exe scripts\check_framework.py     # 框架装配 18 项
.\.venv\Scripts\python.exe scripts\verify_storage.py      # 存储层 12 项
.\.venv\Scripts\python.exe scripts\verify_mock.py         # Mock 契约 18 项
.\.venv\Scripts\python.exe scripts\verify_tools.py        # 工具层 18 项
.\.venv\Scripts\python.exe scripts\verify_checkpointer.py # 跨进程断点续接
.\.venv\Scripts\python.exe scripts\verify_e2e.py          # 端到端 33 项
pwsh -File scripts\run-workbench-check.ps1                # 工作台与实时推送 27 项
```

---

## 6. 关键设计取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| 状态图形态 | 五技能**共用一套骨架**，只替换 observe/diagnose/act/verify 四段 | 避免五套图各写一遍；让"断点续接""澄清追问""步数熔断"天然统一 |
| 澄清机制 | `interrupt()` 挂起 + `Command(resume=)` 续接 | 同一机制同时服务"追问"与"重启续接"两种场景 |
| 向量化 | 项目内确定性本地实现 | 所选模型供应商**无 Embedding 接口**；本地实现零密钥零下载、维度自控、可复现 |
| 结构化输出 | `method="function_calling"` | 该端点 `json_schema` 返回 400、`json_mode` 要求提示词含 "json" |
| 模糊路由 | 信息类技能优先 | FAQ 是只读的，误判代价低；误判到计费/账号/权限会触发**不可逆副作用** |
| 实时推送 | 独立 WebSocket 控制台承载；看板用轮询 | Streamlit 执行模型无法接收服务端主动推送 |
| 主从验收口径 | 稳定断言（写入后读到一致 + 从库宕机回退 + 告警日志） | "延迟 <2s" 受环境影响，是不可控断言（实测复制延迟约 32–72ms） |
| 案例库归属 | 不按租户拆表 | 案例是共享参考知识；按租户各存一份会撞唯一约束且新租户无知识可用 |

完整的决策记录（ADR）与实测硬约束见 `docs/architecture.md`。

---

## 7. 已知环境约束（重要）

以下均为**本机实测**得出，已在代码中规避；细节与证据见 `docs/environment-constraints.md`。

| 约束 | 现象 | 处置 |
|---|---|---|
| PostgreSQL 管理操作需要非受限进程环境 | 受限环境下检查点信号被拦、`pg_basebackup` 失败、强杀后恢复卡住 | 运维/验证脚本在非受限环境运行；受限环境仍可正常连接读写 |
| 子进程随承载进程结束被回收 | 一次性命令启动的服务随即消失 | 用长驻脚本（`serve.ps1`）承载；验证脚本内部串起来跑 |
| Windows 版 Redis 不支持非 ASCII 路径 | `Can't chdir ...: Illegal byte sequence` | 数据目录自动落到纯 ASCII 路径 |
| Windows 版 Redis 不支持 RESP3 | `unknown command HELLO` | 连接统一指定 `protocol=2` |
| `pg_ctl start` 需要受限令牌 | 无管理员权限时无法启动 | 直接以当前用户身份运行 `postgres.exe`，自行记录 PID |
| 异步数据库驱动需要选择器事件循环 | `Psycopg cannot use the 'ProactorEventLoop'` | 见下条 |
| **uvicorn 会覆盖事件循环策略** | 服务能启动、`/health` 正常，但**所有诊断接口一律降级为转人工** | `app/main.py` 提供 `python -m app.main` 入口，自建选择器循环并绕开 uvicorn 的循环装配；启动自检会打印实际循环类型 |
| `pg_stat_replication` 需 `pg_monitor` 角色 | 普通账号查询**静默返回 NULL**，易误判"复制断了" | 初始化脚本授予 `pg_monitor`（非超级用户） |
| 端口冲突伪装成"接口不存在" | 业务接口全 404 而 `/health` 200 | 默认端口避开 8000/8501；就绪探测改用本工程特有接口 |

> **推荐启动方式**：始终用 `python -m app.main` 或 `scripts/serve.ps1`，
> 不要用 `uvicorn app.main:app` 命令行——后者会触发上面第 7 条。

---

## 8. Mock 边界

外部业务系统（集成平台、文档权限、计费、账号风控）在本机由**统一 Mock 服务**承载。
**调用是真的**（真发 HTTP、真超时、真报错、真解析），只是后端是本地进程。

- 接口契约：`docs/mock-boundary.md`（含四域完整请求/响应 schema 与异常模式）
- 异常开关：请求头 `X-Mock-Mode: normal|timeout|error|bad_data|unauthorized`
- 生产对接：替换 `MOCK_BASE_URL` 即可，**业务代码零改动**；
  代码中每处调用标注 `# TODO[生产对接]: <目标系统> · schema 见 docs/mock-boundary.md`

---

## 9. 生产演进

| 当前（本机） | 生产演进 |
|---|---|
| Redis 单实例前台进程 | 哨兵 → 集群 |
| PG 单主单从流复制 | 托管 PG / 多只读副本 / 连接池中间件 |
| 单进程 FastAPI | 多副本 + 独立 Worker + 消息队列 |
| Chroma 本机持久化 | 独立向量服务 / 托管向量库 |
| 本机 zip 中间件 | 容器编排（K8s）+ 配置中心 + 密钥管理 |
| JWT 本地签发 | 统一身份中心（OIDC） |
| 单机限流 | 网关级全局限流 + 熔断 |

---

## 10. 效果指标口径

AI 侧指标**全部从真实运行日志聚合**（`ai_task_logs` / `diagnoses` / `sessions` 等表）：

- 一次解决率 = 未经转人工且已闭环的会话占比
- 转人工率 / 平均轮次 / 平均办结时长 / 分技能对比 / 转人工原因分布 / 坐席维度指标

**人工基线不虚构**：对比页引用公开行业参考区间（SaaS 客服平均处理时长 5–10 分钟）
并明确标注"本项目未采集人工实测数据，不做统计显著性宣称"。

---

## 11. 文档索引

| 文档 | 内容 |
|---|---|
| `docs/architecture.md` | 分层架构、状态图设计、数据模型、9 条 ADR、实测硬约束、交付纪律 |
| `docs/mock-boundary.md` | 四域 Mock 接口完整 schema 与生产对接清单 |
| `docs/environment-constraints.md` | 环境约束与已修缺陷的完整记录（含现象/根因/处置/证据） |
| `docs/evidence/README.md` | **声明 → 证据**对照表 |
| `docs/交付清单.md` | 已完成 / 受阻 / 未做 三清单 |

---

## 12. 许可与致谢

本项目采用 **MIT 许可**，详见 [`LICENSE`](LICENSE)。

第三方依赖均通过 `requirements.lock.txt` 锁定版本，各自遵循其原始许可。

> **安全提示（部署前必读）**：示例配置 `.env.example` 中的 JWT / HMAC 密钥均为占位值。
> 部署时**必须**替换为随机长密钥——服务启动自检会在检测到占位值时输出
> `app.insecure_secret` 告警。`app/services/seed_service.py` 中的演示账号口令
> （如坐席 `Agent@12345`）仅用于本机演示，公网部署前必须修改或删除。
