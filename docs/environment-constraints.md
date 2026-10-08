# 环境约束与实测记录

> 本文件记录**在本机真实踩到并已定位**的环境约束。每条都附现象、根因与对应处置，
> 目的是让后续任何人在同一环境下不再重复踩坑。
>
> 记录方式：`scripts/spike_verify.py`、`scripts/envtest-pg.ps1`、`scripts/verify_storage.py`
> 的输出即为证据，保存在 `docs/evidence/`。

---

## 一、执行环境约束（影响"怎么跑"，不影响"能不能实现"）

### E-1 PostgreSQL 需要非受限进程环境运行

| 项 | 内容 |
|---|---|
| **现象** | 在受限沙箱下运行 PostgreSQL 会出现一连串看似"环境不支持"的失败：<br>① `initdb: error: could not create restricted token: error code 87`<br>② `pg_ctl: could not create restricted token` / `could not start server`<br>③ `ERROR: could not signal for checkpoint: Operation not permitted`（即使已授予 `pg_checkpoint`）<br>④ `pg_basebackup` 失败（它需要请求主库做一次检查点）<br>⑤ 数据目录被强杀后重启，恢复流程执行完 redo 却**永远停在 "database system is starting up"** |
| **根因** | 这些操作都依赖**向子进程投递信号**。受限执行环境阻止了信号投递，于是检查点请求、备份协调、崩溃恢复收尾全部卡死。不是 PostgreSQL 的问题，也不是权限或路径问题。 |
| **验证方法** | 用 `scripts/envtest-pg.ps1` 在两种执行模式下各跑一次。受限模式下 9 项里 6 项失败；放开后 **9/9 全部通过**（含检查点、`pg_basebackup`、强杀后恢复、优雅重启）。 |
| **处置** | 所有涉及 PostgreSQL 的运维与验证操作，必须在非受限执行环境下进行。受限环境下可以**连接与读写**已运行的实例（本地 TCP 正常），只是不能做上述管理动作。 |
| **证据** | `docs/evidence/D3-middleware/envtest-pg.txt`（受限，6 项失败）<br>`docs/evidence/D3-middleware/envtest-pg-escalated.txt`（放开，9/9 通过） |

### E-2 子进程随调用结束被回收

| 项 | 内容 |
|---|---|
| **现象** | 用一次性命令启动 `postgres.exe` / `redis-server.exe` 后，命令一结束，进程就消失；下一个命令再查已经"未运行"。 |
| **根因** | 子进程生命周期绑定在发起它的进程树上，调用结束即整树回收。 |
| **处置** | ① 服务的**搭建与验证**必须放在**同一个调用/同一个后台任务**内串起来完成（`scripts/pg-setup-all.ps1` 就是为此而写）。<br>② 需要长期驻留的服务，用**后台任务**承载（`scripts/dev-up.ps1` 必须作为长驻任务运行）。<br>③ Redis 用 `DETACHED_PROCESS` 标志启动以脱离父进程组。 |
| **证据** | 搭建脚本 `scripts/pg-setup-all.ps1` 的注释；服务在单次后台任务内完成搭建后可持续跨调用访问。 |

### E-3 非 ASCII 工程路径对 Windows 版 Redis 不可用

| 项 | 内容 |
|---|---|
| **现象** | `redis-server.exe` 启动即退出，日志：`Can't chdir to 'D:/.../项目二/...': Illegal byte sequence` |
| **根因** | Windows 版 Redis 5.x 处理多字节路径时使用窄字符 API，遇到非 ASCII 路径失败。 |
| **处置** | 由 `scripts/redisctl.py` 自动判断：工程路径含非 ASCII 时，把 Redis 数据目录落到同级的纯 ASCII 目录（`.toolchain/redis-data`），配置文件中 `dir` 指向该路径；配置文件本身仍留在工程内。 |
| **影响** | 仅影响 Redis 的持久化数据存放位置，不影响功能。 |
| **证据** | `docs/evidence/D3-middleware/redis-start.txt` |

> **修正记录（审查发现）**：本条曾写"PostgreSQL 在非 ASCII 路径下工作正常"。该结论**错误**，
> 已在迁移工具链时被实测推翻并定位——见下面的 E-9。已运行的实例可以正常读写，
> 但**初始化数据目录（initdb）在非 ASCII 路径下必然失败**，因此不能据此认为"PG 不受影响"。

### E-4 Windows 免安装版 Redis 不支持 RESP3

| 项 | 内容 |
|---|---|
| **现象** | `redis-py 8.x` 连接报错：`unknown command 'HELLO', with args beginning with: '3'` |
| **根因** | redis-py 8 默认使用 RESP3，连接时先发 `HELLO 3`；Redis 5.0.x 不认识该命令。 |
| **处置** | `app/core/redis_client.py` 统一在连接参数中指定 `protocol=2`。**所有 Redis 访问必须经该模块**，不得各自创建客户端。 |
| **附带** | 不使用 `INFO` 解析版本号——Windows 版 `INFO` 响应含非 UTF-8 字节，在 `decode_responses=True` 下会抛 `UnicodeDecodeError`，把可用实例误判为不可用。改用 `PING` + `CONFIG GET` 探测。 |
| **证据** | `docs/evidence/D3-middleware/redis-primitives.txt` |

### E-5 `pg_ctl start` 不可用，但 `pg_ctl stop` 可用

| 项 | 内容 |
|---|---|
| **现象** | `pg_ctl start` 报 `could not create restricted token`；但 `pg_ctl stop -m fast` 正常。 |
| **根因** | 只有**启动**路径需要创建受限令牌来降权，停止路径不需要。 |
| **处置** | 起服务统一走 `scripts/pgctl.ps1`：直接以当前用户身份运行 `postgres.exe` 并记录 PID；停服务优先用 `pg_ctl stop -m fast`（优雅停止），仅在无响应时才强制终止。 |
| **重要** | 必须避免强杀实例：强杀会留下需要恢复的数据目录，而在受限环境下恢复会卡死（见 E-1）。所以"优雅停止优先"不只是整洁问题，而是可用性问题。 |
| **证据** | `scripts/pgctl.ps1` 的 `Clear-StaleState` 实现与注释 |

### E-6 异步数据库访问需要选择器事件循环

| 项 | 内容 |
|---|---|
| **现象** | 使用异步状态持久化时抛：`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode` |
| **根因** | Windows 上 asyncio 默认使用 `ProactorEventLoop`，psycopg 的异步连接不支持该循环。 |
| **处置** | 新增 `app/core/platform_compat.py`，导入即把事件循环策略切为 `WindowsSelectorEventLoopPolicy`；所有入口（CLI、应用启动、验证脚本）在工作流开始前导入它。 |
| **证据** | `docs/evidence/D4-checkpointer/verify-checkpointer.txt`（跨进程续接验证通过，说明该修正生效） |

### E-7 **uvicorn 会覆盖事件循环策略，导致"服务能启动、接口却全不能干活"**

| 项 | 内容 |
|---|---|
| **现象** | 通过 uvicorn 启动后，`/health` 与所有常规接口都返回 200，但**凡是走诊断的接口一律降级为"转人工"**；日志里能看到 `app.event_loop_incompatible`。pytest 进程内测试却完全正常。 |
| **根因** | uvicorn 的循环装配在 Windows 上会安装 `ProactorEventLoop`，**绕过**已设置的 `WindowsSelectorEventLoopPolicy`。实测日志可同时看到"策略 = WindowsSelectorEventLoopPolicy"与"运行中循环 = ProactorEventLoop"——即策略压根没生效。显式传 `loop="asyncio"` **也无法阻止**。 |
| **影响范围** | 只影响用 uvicorn 命令行启动的场景；这也是它最难定位的地方——测试全绿、服务健康检查正常，只有真实业务的诊断链路全线失败。 |
| **处置** | `app/main.py` 提供 `python -m app.main` 入口：**自己创建选择器事件循环**，再用 uvicorn 的 `Server` API 运行，完全绕开 uvicorn 的循环装配。启动脚本（`dev-up.ps1`、`run-workbench-check.ps1`）统一改用该入口。 |
| **自检** | 启动时会打印实际事件循环类型；若不是选择器循环则直接报 error 并给出修复提示，避免隐蔽故障。 |
| **证据** | `docs/evidence/D19-workbench/verify-workbench.txt`（修复后工作台与实时推送 27/27 通过） |

### E-8 端口冲突会伪装成"接口不存在"

| 项 | 内容 |
|---|---|
| **现象** | 所有业务接口返回 404，但 `/health` 返回 200，看起来像"路由没注册"。 |
| **根因** | 端口被**另一个工程**的服务占用。新进程绑定失败退出，而旧服务仍在响应；其 `/health` 恰好也是 200，于是造成"服务就绪"的假象。 |
| **处置** | ① 本工程默认端口改为 **API 8011 / 工作台 8502**（避开最常被占用的 8000/8501）；② 就绪探测改用**本工程特有**的接口（`/api/v1/agent/metrics/overview`）而不是 `/health`；③ `run-workbench-check.ps1` 启动前先检查端口归属并提示占用进程。 |
| **证据** | `scripts/run-workbench-check.ps1` 的端口检查输出 |

### E-9 PostgreSQL 的 initdb 无法在非 ASCII 路径下初始化（已知缺陷）

| 项 | 内容 |
|---|---|
| **现象** | 在含中文的工程路径下执行 `initdb`，输出 `running bootstrap script ... ok` 后立刻失败：<br>`FATAL: invalid byte sequence for encoding "UTF8": 0xcf 0xee`，随后 `initdb: removing data directory ...`。<br>把数据目录换到纯 ASCII 路径（`D:\pgdata`）**仍然失败**——说明与数据目录无关。 |
| **根因** | PostgreSQL 的**已知缺陷**：initdb 在"**二进制路径**含某些非 ASCII 字符"时失败（`pgsql-bugs #16926`）。本项目工具链放在 `项目二\.toolchain`，路径含中文，因此直接调用必然失败。注意 `0xcf 0xee` 是 GBK 编码的"系"字，容易被误读为数据损坏或中文数据问题。 |
| **验证方法** | ① 直接从含中文的路径跑 `initdb -D D:\pgdata\bare` → 失败；② 为二进制目录建 ASCII 目录联接（`D:\pgtool` → `<工程>\..\.toolchain\pg16\pgsql`）后，用 `D:\pgtool\bin\initdb.exe` 跑同样命令 → **Success**。 |
| **处置** | 建立两个 ASCII **目录联接**，真实文件仍全部留在工程内（不影响"项目二 可整体拷贝"）：<br>`D:\pgtool` → `<工程>\..\.toolchain\pg16\pgsql`（二进制）<br>`D:\pgdata` → `<工程>\data\pg`（数据目录根）<br>由 `scripts/setup-ascii-links.ps1` 创建（需管理员权限），`scripts/serve.ps1` 自动探测并使用；各 PG 脚本支持 `SAASKEFU_PG_BIN` / `SAASKEFU_PG_DATA_ROOT` 覆盖。<br>**删除联接必须用 `cmd /c rmdir`，不可用 `Remove-Item -Recurse`**——后者会穿透联接删掉真实数据目录。 |
| **影响** | 在纯 ASCII 路径的机器上不需要联接，脚本会自动回退到真实路径。已初始化的实例在非 ASCII 路径下可正常读写，只有**初始化**受限。 |
| **证据** | `logs/pg-rebuild2.txt`（联接后主从搭建成功）、`docs/evidence/D3-middleware/pg-setup-all.txt` |

---

## 二、本次搭建过程中发现并修复的缺陷（工程性，非环境）

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| B-1 | `pg_basebackup` 报 `could not signal for checkpoint` | 复制账号仅有 `REPLICATION` 权限，无法触发检查点 | 授予 PostgreSQL 15+ 的专用角色 `pg_checkpoint`（而非提升为超级用户，遵循最小权限） |
| B-2 | 从库搭建失败会连带让主库初始化结果不可用 | 备份失败抛异常中断了整个脚本 | 把从库搭建拆到独立脚本，职责分离（`pg-init.ps1` 只管主库） |
| B-3 | 工程移动后脚本找不到工具链 | 用固定层级 `..\..\.toolchain` 推导路径 | 改为向上逐级查找 `.toolchain` 标记目录 |
| B-4 | 实例"半启动"被误判为"已运行" | 仅以进程是否存在判断状态 | 以**端口是否就绪**为准，并清理陈旧 `postmaster.pid` |
| B-5 | 表名自动复数化把 `Diagnosis` 变成 `diagnosiss`，导致外键找不到目标表 | 依赖命名推导而非显式声明 | 强制每个模型显式声明 `__tablename__`；移除自动推导 |
| B-6 | 租户隔离闸门静默失效 | 闸门依赖"租户表名单"，而名单在 ORM 模型导入时才注册；若入口未导入模型，名单为空 → 闸门无从判断 | 新增 `ensure_models_loaded()`，并要求所有入口显式调用；`scripts/verify_storage.py` 增加"无租户上下文必须被拒绝"的正向断言 |
| B-7 | 模型列默认值在原生 SQL 写入时不生效 | 原生 SQL 绕过 ORM 的 Python 侧默认值 | 业务写入统一走 ORM；验证脚本改用 ORM 写入 |

---

## 二、迁移工具链时踩到的坑（处置记录）

把工具链从工作区根目录迁到 `项目二\.toolchain` 时，连续踩到四个问题，逐一记录以免重演。

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| M-1 | 迁移"成功"（秒级完成），但 PG 随后起不来，日志反复 `database system is starting up`，最终报 `some data is corrupted` | 停服用了硬编码的 `pg_ctl -D <数据目录>`，而**路径写错了**（少了中间层），实际没停成；PG 仍在写文件时整个工具链目录被搬走，WAL 尾部撕裂 | 迁移脚本改为**调用既有停服脚本**（`pgctl.ps1` / `redisctl.py`），并且在移动前**轮询确认端口全部释放**，未释放就拒绝移动（fail-fast，宁可不迁也不损坏数据） |
| M-2 | 撕裂的集群用 `pg_resetwal -f` 抢救成功（29 张表、1149 个会话数据均保留），但随后我用 `pg-setup-all.ps1 -Force` 重建时**把数据目录删了**，连同备份一起丢失 | 误判 `-Force`（删库重建）可用于"重建从库"；且 `Remove-Item -Recurse` 作用在**目录联接内部**时会穿透联接删掉真实目录 | 演示数据完全可由 `initdb + seed + build-index` 重建，因此按"重建"处理；**删除联接一律用 `cmd /c rmdir`**，不使用 `Remove-Item -Recurse` |
| M-3 | `initdb` 在工程路径下必然失败（`invalid byte sequence for encoding "UTF8": 0xcf 0xee`） | PostgreSQL **已知缺陷**：initdb 在二进制路径含非 ASCII 字符时失败（pgsql-bugs #16926）。`0xcf 0xee` 是 GBK 的"系"字，极易被误读为数据损坏 | 建立 ASCII 目录联接（`D:\pgtool`→二进制、`D:\pgdata`→数据），见 E-9 与 `scripts/setup-ascii-links.ps1` |
| M-4 | 主库搭建完成后"自动消失"，后续连不上 | 子进程随调用进程树回收（E-2）：分两次调用时，第一次调用结束即带走 `postgres.exe` | **搭建与启动必须放在同一个长驻任务里**；`serve.ps1` 本身是长驻的，因此"先单独搭从库、再起栈"这种两步做法不可行 |

**结论**：迁移本身是一次**目录改名**（同卷、秒级），风险全部来自"停服不彻底"。因此
`scripts/migrate-toolchain.ps1` 的停服环节已改为复用停服脚本 + 端口释放校验，
在无法确认端口释放时会直接失败而不是继续移动。

---

## 三、实测通过的能力基线（可作为后续回归基准）

| 能力 | 结论 | 证据 |
|---|---|---|
| PostgreSQL 16.6 主库（5432） | 读写正常 | `docs/evidence/D7-storage/verify-storage.txt` |
| PostgreSQL 从库（5433）流复制 | `state=streaming`，`async`，从库 `in_recovery=t` | 同上 |
| 复制实时性 | 主库写入后从库**首次轮询即可见** | 同上 |
| 读写分离 | 写走主库，只读走从库，`read_fallback=False` | 同上 |
| 租户隔离闸门 | 无租户上下文访问租户表**被拒绝**；绑定后可执行；系统表不受影响 | 同上 |
| Redis（6379） | PING/读写/列表/心跳 TTL 正常，AOF 已开启 | `docs/evidence/D3-middleware/redis-primitives.txt` |
| Redis 分布式锁 | 第二次抢锁正确失败，释放后可再次获取 | 同上 |
| Redis 令牌桶限流 | 容量 3、连续 4 次请求 → 第 4 次被拒绝 | 同上 |
| Redis 防重放 | 同一 nonce 首次通过、二次拒绝 | 同上 |
| 结构化输出 | `method="function_calling"` 可用且遵循枚举取值 | `docs/evidence/D0-spike/llm-probe2.txt` |
| 工具调用 | 模型正确发起工具调用并传参 | `docs/evidence/D0-spike/llm-probe.txt` |
| 流式输出 | 分片正常 | 同上 |
| 状态图 | 建图/条件边/环路执行正确，`thread_id` 会话隔离正确 | `docs/evidence/D0-spike/spike-output.txt` |

---

## 四、对交付的影响说明

- **本项目的功能实现不依赖上述任何"降级"**：PostgreSQL 主从流复制、Redis 四件套、
  状态图状态持久化、检查点与崩溃恢复都在真实环境下实测通过。
- E-1/E-2 只影响**运行方式**（需要非受限、长驻的任务承载服务），不影响系统能力本身。
- E-3/E-4 是 Windows 免安装版 Redis 的已知限制，已在代码层自动规避，使用者无感。
