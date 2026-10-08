# Mock 边界说明 · 外部系统接口契约

> 本文件是**唯一**被允许"非真实实现"的边界说明。四类业务后端在本机由统一 Mock 服务承载，
> 但**调用是真的**（真发 HTTP、真超时、真报错、真解析），只是后端是本地进程。
>
> 生产对接时：替换 `MOCK_BASE_URL` 为真实服务地址即可，**业务代码零改动**。
> 每个 Mock 端点在代码中对应一处 `# TODO[生产对接]: <目标系统> · schema 见 docs/mock-boundary.md`。

---

## 1. 统一 Mock 服务

| 项 | 值 |
|---|---|
| 进程 | `app/mocks/server.py`（单进程 FastAPI 多路由） |
| 默认端口 | `8010` |
| 路由前缀 | `/mock/integration/*`、`/mock/permission/*`、`/mock/billing/*`、`/mock/account/*` |
| 数据源 | `app/mocks/data.py`（确定性内存数据，按租户隔离） |
| 链路追踪 | 所有请求带 `X-Trace-ID` 头，Mock 侧回写该头并在日志中打印 |

### 1.1 异常模拟统一开关

**请求头 `X-Mock-Mode`**，由统一下载中间件处理，**不为每个端点单独写异常分支**：

| 取值 | 行为 | HTTP 状态 |
|---|---|---|
| `normal` | 正常返回（默认，头缺失时等同） | 200 |
| `timeout` | 挂起 `MOCK_TIMEOUT_SECONDS`（默认 30s）后返回 | 504 |
| `error` | 返回 500 + 结构化错误体 | 500 |
| `bad_data` | 返回 200 但字段缺失/类型错误（用于校验 Pydantic 兜底） | 200 |
| `unauthorized` | 返回 401 + `WWW-Authenticate` | 401 |

附加可选头：

| 头 | 作用 |
|---|---|
| `X-Mock-Delay-Ms` | 在正常返回前注入延迟，用于测超时边界 |
| `X-Mock-Fail-N` | 前 N 次调用失败，之后成功（用于测重试后成功路径） |
| `X-Mock-Idempotency` | `on` 时对重复 `Idempotency-Key` 返回首次结果（用于测幂等） |

### 1.2 通用响应包装

```json
{
  "ok": true,
  "data": { },
  "meta": { "mock": true, "trace_id": "...", "server_time": "2026-01-01T00:00:00+08:00" }
}
```

错误体：

```json
{
  "ok": false,
  "error": { "code": "TOKEN_EXPIRED", "message": "访问令牌已过期", "retryable": true },
  "meta": { "mock": true, "trace_id": "..." }
}
```

---

## 2. 集成域 `/mock/integration/*`（技能 2）

被模拟对象：SaaS 产品的三方 IM 集成服务（Slack / 钉钉 / 飞书）。

### 2.1 `GET /mock/integration/config`

查询某工作区的集成配置。

**Query**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `tenant_id` | string | 是 | 租户 |
| `workspace_id` | string | 是 | 工作区 |
| `provider` | string | 是 | `slack` / `dingtalk` / `feishu` |

**200 响应 `data`**

```json
{
  "workspace_id": "ws-1001",
  "provider": "slack",
  "connected": false,
  "bot_user": "@yunshu-bot",
  "webhook_url": "https://hooks.example.com/ws-1001/old-path",
  "webhook_reachable": false,
  "token": {
    "kind": "bot",
    "prefix": "xoxb-****",
    "issued_at": "2025-11-02T09:00:00+08:00",
    "expires_at": "2025-12-02T09:00:00+08:00",
    "scopes": ["chat:write", "channels:read"]
  },
  "last_event_at": "2025-12-01T22:14:00+08:00",
  "health": "degraded"
}
```

**已知故障画像**（由 `app/mocks/data.py` 按 `workspace_id` 确定性给出）

| workspace_id | 故障 | 期望诊断结论 |
|---|---|---|
| `ws-1001` | Token 过期 + Webhook 失效 | `TOKEN_EXPIRED`（主因） |
| `ws-1002` | Webhook 路径被改 | `WEBHOOK_INVALID` |
| `ws-1003` | 应用权限被移除 | `SCOPE_REVOKED` |
| `ws-1004` | 全部正常 | `HEALTHY` |

### 2.2 `POST /mock/integration/token/refresh`

刷新访问令牌。

**Body**

```json
{ "tenant_id": "t-001", "workspace_id": "ws-1001", "provider": "slack", "refresh_token": "rt-****" }
```

**200 响应 `data`**

```json
{ "workspace_id": "ws-1001", "token": { "kind": "bot", "prefix": "xoxb-****", "issued_at": "...", "expires_at": "...", "scopes": ["chat:write","channels:read"] }, "refreshed": true }
```

**失败画像**

| 条件 | 错误码 | 说明 |
|---|---|---|
| `refresh_token` 为空或已失效 | `REFRESH_TOKEN_INVALID`（401） | 触发降级：引导重新授权 |
| 应用权限已被移除（`ws-1003`） | `SCOPE_REVOKED`（403） | 刷新成功但权限不足，仍需重授权 |

### 2.3 `POST /mock/integration/webhook/repair`

修复 Webhook 配置。

**Body**

```json
{ "tenant_id": "t-001", "workspace_id": "ws-1002", "provider": "slack", "expected_url": "https://hooks.example.com/ws-1002/current-path" }
```

**200 响应 `data`**

```json
{ "workspace_id": "ws-1002", "webhook_url": "https://hooks.example.com/ws-1002/current-path", "webhook_reachable": true, "repaired": true }
```

### 2.4 `POST /mock/integration/reauth-link`

生成重新授权链接（Token 刷新不可用时的降级路径）。

**Body** `{ "tenant_id", "workspace_id", "provider" }`

**200 响应 `data`**

```json
{ "authorize_url": "https://auth.example.com/oauth/authorize?workspace=ws-1003&state=...", "expires_in_seconds": 900 }
```

### 2.5 `POST /mock/integration/verify`

**验证可达性**：真实向该工作区投递一条验证消息。

**Body**

```json
{ "tenant_id": "t-001", "workspace_id": "ws-1001", "provider": "slack", "channel": "#support-verify", "text": "【智汇云客服】集成连通性验证消息" }
```

**200 响应 `data`**

```json
{ "delivered": true, "channel": "#support-verify", "provider_message_id": "msg-8f21", "latency_ms": 142 }
```

**说明**：这是"真调用假后端"的核心证据点——验证不是查本地标记，而是真发一次消息并拿到回执。

---

## 3. 权限域 `/mock/permission/*`（技能 3）

被模拟对象：SaaS 产品的文档协作与组织权限服务。

### 3.1 `GET /mock/permission/document`

**Query**：`tenant_id`、`doc_id`、`user_id`

**200 响应 `data`**

```json
{
  "doc_id": "doc-771",
  "title": "Q4 产品路线图",
  "owner": "u-owner-3",
  "share": { "mode": "private", "link_enabled": false, "allow_external": false },
  "org_policy": { "external_sharing_allowed": false, "min_role_to_share": "admin" },
  "user": { "user_id": "u-42", "role": "member", "in_org": true },
  "version_conflict": false,
  "user_can_open": false,
  "deny_reason": "SHARE_NOT_ENABLED"
}
```

**故障画像**

| doc_id | 成因 | 期望诊断结论 |
|---|---|---|
| `doc-771` | 分享未开启 | `SHARE_NOT_ENABLED` |
| `doc-772` | 组织策略禁止外链 | `ORG_POLICY_BLOCKED` |
| `doc-773` | 版本冲突锁 | `VERSION_CONFLICT` |
| `doc-774` | 正常可访问 | `ACCESSIBLE` |

### 3.2 `POST /mock/permission/share/grant`

**Body**：`{ "tenant_id", "doc_id", "grantee_user_id", "role": "viewer|commenter|editor" }`

**200 响应 `data`**：`{ "granted": true, "doc_id": "doc-771", "grantee_user_id": "u-42", "role": "viewer" }`

### 3.3 `POST /mock/permission/policy/exception`

为个人申请组织策略例外（需审批）。

**Body**：`{ "tenant_id", "doc_id", "user_id", "justification": "..." }`

**200 响应 `data`**：`{ "request_id": "pol-2201", "status": "pending_approval", "approver_role": "admin", "sla_hours": 4 }`

### 3.4 `POST /mock/permission/version/unlock`

解除版本冲突锁。

**Body**：`{ "tenant_id", "doc_id", "force": false }`

**200 响应 `data`**：`{ "unlocked": true, "doc_id": "doc-773", "kept_version": "v12", "discarded_version": "v11-draft" }`

### 3.5 `POST /mock/permission/verify-access`

复核可访问性。**Body**：`{ "tenant_id", "doc_id", "user_id" }`

**200 响应 `data`**：`{ "user_can_open": true, "via": "direct_grant|org_policy_exception|version_unlock|none", "checked_at": "..." }`

---

## 4. 计费域 `/mock/billing/*`（技能 4）

被模拟对象：SaaS 产品的订阅计费系统。

### 4.1 安全要求（本域独有）

所有**写操作**必须同时满足：

| 要求 | 头/字段 | 规则 |
|---|---|---|
| 幂等 | `Idempotency-Key` | 同 key 重复提交**不得重复退款**；返回首次结果并带 `replayed: true` |
| 签名 | `X-Signature` | `HMAC-SHA256(secret, f"{timestamp}.{raw_body}")` 的十六进制小写 |
| 时间戳 | `X-Timestamp` | Unix 秒；与服务端时间偏差 **±300s** 之外直接拒绝 |
| 防重放 | `X-Nonce` | 同 nonce 在窗口内重复出现直接拒绝（本机用 Redis `SET NX EX`） |

拒绝响应：`401 SIGNATURE_INVALID` / `401 TIMESTAMP_OUT_OF_WINDOW` / `409 NONCE_REPLAYED` / `409 IDEMPOTENT_REPLAY`（后者为幂等命中，非错误）。

### 4.2 `GET /mock/billing/subscription`

**Query**：`tenant_id`、`account_id`

```json
{
  "account_id": "acct-501",
  "plan": "team",
  "seats": 25,
  "status": "active",
  "started_at": "2025-03-01T00:00:00+08:00",
  "renews_at": "2026-03-01T00:00:00+08:00",
  "tenure_months": 9,
  "user_tier": "gold",
  "open_complaints": 1
}
```

### 4.3 `GET /mock/billing/invoice`

**Query**：`tenant_id`、`account_id`、`period`（`YYYY-MM`，可选；缺省最近一期）

```json
{
  "invoice_id": "inv-2025-11-0501",
  "account_id": "acct-501",
  "period": "2025-11",
  "amount": 2380.00,
  "currency": "CNY",
  "status": "unpaid",
  "lines": [
    { "sku": "seat-monthly", "qty": 25, "unit_price": 80.0, "amount": 2000.00 },
    { "sku": "api-overage", "qty": 38000, "unit_price": 0.01, "amount": 380.00 }
  ],
  "disputed": true,
  "dispute_reason": "重复计费：11 月席位费被计两次"
}
```

### 4.4 `GET /mock/billing/usage`

**Query**：`tenant_id`、`account_id`、`period`

```json
{ "period": "2025-11", "api_calls": 138000, "included_quota": 100000, "overage": 38000, "storage_gb": 42.5 }
```

### 4.5 `POST /mock/billing/adjustment`

发起账务调整（退款 / 抵扣）。

**Body**

```json
{
  "tenant_id": "t-001",
  "account_id": "acct-501",
  "invoice_id": "inv-2025-11-0501",
  "amount": 380.00,
  "currency": "CNY",
  "kind": "refund",
  "reason": "用量超限误计",
  "operator": "ai"
}
```

**200 响应 `data`**

```json
{ "adjustment_id": "adj-9911", "status": "succeeded", "amount": 380.00, "replayed": false }
```

**失败画像**：`error` 模式返回 500 `BILLING_UPSTREAM_ERROR`（用于触发"查幂等状态 → 重试/确认"降级）。

### 4.6 `POST /mock/billing/adjustment/approval`

转人工审批（金额或风险分超阈值时）。

**Body**：`{ "tenant_id", "account_id", "invoice_id", "amount", "reason", "risk_score" }`

**200 响应 `data`**：`{ "approval_id": "apv-3301", "status": "pending", "approver_role": "billing_admin" }`

---

## 5. 账号安全域 `/mock/account/*`（技能 5）

被模拟对象：SaaS 产品的账号与风控服务。

### 5.1 `GET /mock/account/login-events`

**Query**：`tenant_id`、`user_id`、`days`（默认 7）

```json
{
  "user_id": "u-42",
  "events": [
    { "at": "2025-12-02T03:14:00+08:00", "ip": "203.0.113.77", "geo": "境外-未知", "device": "Chrome/Windows", "result": "success", "mfa": false, "risk_flags": ["new_geo","odd_hour","new_device"] },
    { "at": "2025-12-01T10:02:00+08:00", "ip": "10.20.30.40", "geo": "CN-杭州", "device": "Edge/Windows", "result": "success", "mfa": true, "risk_flags": [] }
  ],
  "baseline": { "usual_geo": ["CN-杭州","CN-上海"], "usual_hours": [9,10,11,14,15,16,17,18], "usual_devices": ["Edge/Windows","iOS"] },
  "anomaly_score_hint": 0.87
}
```

### 5.2 `POST /mock/account/lock`

**Body**：`{ "tenant_id", "user_id", "reason": "...", "lock_type": "temporary|permanent", "duration_minutes": 120 }`

**200 响应 `data`**：`{ "locked": true, "user_id": "u-42", "until": "2025-12-02T05:14:00+08:00", "sessions_revoked": 3 }`

### 5.3 `POST /mock/account/notify`

**Body**：`{ "tenant_id", "user_id", "channel": "email|sms|inapp", "template": "security_alert", "payload": { } }`

**200 响应 `data`**：`{ "sent": true, "channel": "email", "message_id": "ntf-7781" }`

### 5.4 `POST /mock/account/password/reset-required`

**Body**：`{ "tenant_id", "user_id" }`

**200 响应 `data`**：`{ "required": true, "reset_token_issued": true, "expires_in_seconds": 1800 }`

### 5.5 `POST /mock/account/verify`

复核处置结果。**Body**：`{ "tenant_id", "user_id" }`

**200 响应 `data`**：`{ "sessions_active": 0, "mfa_enabled": true, "risk_score": 0.12, "safe": true }`

---

## 6. 生产对接清单

| 域 | 生产对接目标 | 需替换 | 备注 |
|---|---|---|---|
| 集成域 | 云枢协作平台 开放平台 API | `MOCK_BASE_URL` → 真实网关地址；`X-Mock-Mode` 头移除 | Token 刷新需接入真实 OAuth 客户端 |
| 权限域 | 云枢文档服务 权限 API | 同上 | 组织策略例外需接入审批流 |
| 计费域 | 云枢计费系统 API | 同上 + 真实 HMAC 密钥 | 幂等键语义需与计费方对齐 |
| 账号域 | 云枢账号风控 API | 同上 | 锁定动作需二次授权校验 |

**对接验收口径**：替换地址后，`tests/integration/` 下的四域用例应**无需改动代码**即可运行（这是本边界存在的意义）。
