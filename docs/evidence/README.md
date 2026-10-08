# 测试与验收证据总览

> 本文档是"声明 → 证据"的对照表。每条能力声明都指向一份**真实执行产生**的输出，
> 而非口头承诺。所有证据文件位于 `docs/evidence/`。

---

## 一、测试套件

执行命令：

```powershell
$P = "<工程目录>"
& "$P\.venv\Scripts\python.exe" -m pytest -q
```

**结果：139 passed in 167.26s**（证据：`docs/evidence/D18-tests/pytest-full.txt`）

| 层次 | 数量 | 覆盖内容 |
|---|---|---|
| 单元 | 76 | 技能路由与实体抽取、风控评分表与阈值表、风险信号表、向量化与相似度、向量库持久化、口令/JWT/签名、转人工触发矩阵、状态合并语义 |
| 集成 | 38 | 五技能端到端（断言到诊断编码/动作/验证结果）、转人工全流程、接管互斥、掉线与重排队、计费幂等、租户隔离、读写分离、坐席鉴权、链路追踪落库 |
| 异常专项 | 19 | Mock 五种异常模式、工具重试与归类、结构校验拦截、模型不可用、空检索兜底、从库宕机回退、Redis 不可用降级、限流、防重放、分布式锁、状态跨进程持久化、非法状态跃迁 |
| 并发/负载 | 6 | 20 并发跨租户隔离、多坐席并发心跳、并发派单不重复、复制延迟测量、从库恢复模式校验 |

对照任务书要求（单元 ≥15 / 集成 ≥12 / 异常 ≥10 / 并发 ≥3 / 合计 ≥40）：**全部超额满足**。

---

## 二、分阶段验收证据

| 阶段 | 声明 | 结果 | 证据文件 |
|---|---|---|---|
| D0 | 扩展点全部可用（配置/日志/链路/向量化/向量库/状态图/状态持久化/驱动） | 18/18 | `D0-spike/spike-output.txt` |
| D0 | 大模型端点与能力实测（模型名、结构化输出、工具调用、流式、时延） | 全部结论落盘 | `D0-spike/llm-probe.txt`、`llm-probe2.txt` |
| D3 | PostgreSQL 主从流复制搭建（5432/5433） | `state=streaming` | `D3-middleware/pg-init.txt`、`pg-setup-all.txt` |
| D3 | 环境能力边界实测（确认非环境缺陷而是沙箱限制） | 受限 6 项失败 / 放开 9/9 通过 | `D3-middleware/envtest-pg.txt`、`envtest-pg-escalated.txt` |
| D3 | Redis 起服与四件套原语 | 锁竞争/令牌桶/防重放/心跳 TTL 全通 | `D3-middleware/redis-primitives.txt` |
| D4 | 状态持久化与**跨进程**断点续接 | 进程A 挂起 → 全新进程B 续接并跑完 | `D4-checkpointer/verify-checkpointer.txt` |
| D7 | 存储层：租户闸门 + 读写分离 + 从库回退 | 12/12 | `D7-storage/verify-storage.txt` |
| D8 | Mock 后端契约（四域 + 五模式 + 计费安全） | 18/18 | `D8-mock/verify-mock.txt` |
| D8 | 工具层（四域调用 + 全部降级路径） | 18/18 | `D8-mock/verify-tools.txt` |
| D9 | 框架装配（技能/路由/实体/建图/规则表） | 18/18 | `D9-skills/check-framework.txt` |
| D12 | 端到端全链路（真实 HTTP + 真实 PG/Redis + 真实模型） | 33/33 | `D12-e2e/verify-e2e-http.txt` |
| D12 | 端到端全链路（进程内自包含模式） | 全部通过 | `D12-e2e/verify-e2e.txt` |
| D18 | 分层测试套件 | 139 passed | `D18-tests/pytest-full.txt` |

---

## 三、可复跑脚本清单

所有脚本都在 `scripts/`，可独立重复执行：

| 脚本 | 用途 |
|---|---|
| `fetch-toolchain.ps1` | 下载并解压 PostgreSQL / Redis 免安装 zip |
| `pg-init.ps1` | 初始化主库（含角色、业务库、权限授予） |
| `pg-replica.ps1` | `pg_basebackup` 搭建从库并启动 |
| `pg-setup-all.ps1` | 主从一键搭建（初始化 + 从库 + 校验） |
| `pgctl.ps1` | PostgreSQL 进程管理（start/stop/status） |
| `redisctl.ps1`… `redisctl.py` | Redis 进程管理（含非 ASCII 路径自动规避） |
| `dev-up.ps1` | 一键起全拓扑（PG 主从 + Redis + Mock + API + 工作台） |
| `spike_verify.py` | 扩展点验证（18 项） |
| `llm_probe.py` / `llm_probe2.py` | 大模型端点与调用参数实测 |
| `verify_storage.py` | 存储层验证（租户闸门/读写分离/回退） |
| `verify_mock.py` | Mock 契约验证 |
| `verify_tools.py` | 工具层与降级路径验证 |
| `verify_checkpointer.py` | 跨进程断点续接验证 |
| `verify_e2e.py` | 端到端全链路验证（支持 HTTP / 进程内两种模式） |
| `check_framework.py` | 框架装配自检 |
| `export_samples.py` | 示例数据集导出与分布校验 |

---

## 四、表述纪律说明

- 交付物内不出现外部参考项目的名称、类名、文档链接。
- 不使用派生性表述描述能力来源；
  本项目分层设计。"本项目分层设计"。
- `.env` 不提交、不入交付物；`.env.example` 仅含占位符。
- 引用外部事实（如行业基准区间）时标注来源。
