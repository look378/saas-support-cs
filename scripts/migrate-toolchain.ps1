<#
.SYNOPSIS
    把本机中间件工具链迁移到「项目二」目录下，使项目二成为自包含单元。

.DESCRIPTION
    为什么要有这个脚本：
      工具链（PostgreSQL 16 / Redis / Python = 约 1.2GB 免安装 zip 版）原先放在
      工作区根目录（工程之外）。这会让"把 项目二 整体拷贝到另一台机器"失效——
      脚本虽然能向上找到 .toolchain，但那个目录已经不在拷贝范围内了。
      迁移到 项目二\.toolchain 后，项目二 内部两个文件夹互不依赖外部路径。

    迁移内容：
      <源>\.toolchain            →  <项目二>\.toolchain
      （其中 redis-data 是 Redis 的持久化目录，必须一并搬走，否则数据丢失）

    注意：
      - 必须先停掉 PostgreSQL / Redis / API / Mock / 看板，否则二进制文件被占用无法移动；
      - PostgreSQL 的数据目录在工程内的 data\pg（不在工具链里），因此不受影响；
      - 各脚本的 .toolchain 查找是"自脚本向上逐级查找"，迁移后无需改动即可命中；
        只有 fetch-toolchain.ps1 的**默认新建位置**需要改（否则新机器上会又建到工程外）。

.PARAMETER SkipRestart
    迁移后不自动重启服务（仅搬迁）。

.EXAMPLE
    pwsh -File scripts/migrate-toolchain.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipRestart
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot          # ...\项目二\saas-support-cs
$ProjectTwo  = Split-Path -Parent $ProjectRoot           # ...\项目二
$Workspace   = Split-Path -Parent $ProjectTwo            # 工作区根

$Source = Join-Path $Workspace ".toolchain"
$Target = Join-Path $ProjectTwo ".toolchain"

$PgBin = Join-Path $Source "pg16\pgsql\bin"
$RedisCli = Join-Path $Source "redis\redis-cli.exe"
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host "================================================================"
Write-Host "  工具链迁移"
Write-Host "================================================================"
Write-Host "  源目录  : $Source"
Write-Host "  目标目录: $Target"
Write-Host ""

if (-not (Test-Path $Source)) {
    if (Test-Path $Target) {
        Write-Host "  工具链已在目标位置，无需迁移。"
        exit 0
    }
    throw "既找不到源工具链，也没有目标工具链：$Source"
}
if (Test-Path $Target) {
    throw "目标已存在，拒绝覆盖：$Target（请先人工确认并清理）"
}

# ---------------------------------------------------------------------------
# 1) 停服
# ---------------------------------------------------------------------------
Write-Host "[1/6] 停止服务"
foreach ($p in 8011, 8010, 8502) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        Write-Host "  释放端口 $p（PID $($conn.OwningProcess)）"
    }
}
Start-Sleep -Seconds 1

# 复用既有的停服脚本，**不要在这里自己拼数据目录路径**：
# 早期版本把 PG 数据目录写成 data\pg\<role> 但漏了中间层，导致 pg_ctl 实际没停成，
# 服务器仍在写文件时被整目录搬走，WAL 尾部撕裂、集群需要 pg_resetwal 抢救。
& $Py (Join-Path $PSScriptRoot "redisctl.py") stop 2>&1 | Out-Null
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action stop -Role all 2>&1 | ForEach-Object { "  $_" }

# 必须**确认**端口真的空了：文件被占用时移动会失败或造成数据损坏
Write-Host "  等待端口释放..."
$deadline = (Get-Date).AddSeconds(30)
do {
    $busy = @(5432, 5433, 6379, 8011, 8010, 8502 | Where-Object {
        Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue
    })
    if ($busy.Count -eq 0) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)

if ($busy.Count -gt 0) {
    throw "端口仍被占用（$($busy -join ', ')），拒绝移动工具链以避免数据损坏。请手动停止占用进程后重试。"
}
Write-Host "  端口已全部释放，可以安全移动"

# ---------------------------------------------------------------------------
# 2) 移动
# ---------------------------------------------------------------------------
Write-Host "[2/6] 移动工具链（约 1.2GB，可能需要十几秒）"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
Move-Item -Path $Source -Destination $Target
$sw.Stop()
Write-Host ("  完成，用时 {0:N1}s" -f $sw.Elapsed.TotalSeconds)

# ---------------------------------------------------------------------------
# 3) 校验关键可执行文件
# ---------------------------------------------------------------------------
Write-Host "[3/6] 校验迁移结果"
$checks = @{
    "postgres.exe"     = Join-Path $Target "pg16\pgsql\bin\postgres.exe"
    "pg_isready.exe"   = Join-Path $Target "pg16\pgsql\bin\pg_isready.exe"
    "redis-server.exe" = Join-Path $Target "redis\redis-server.exe"
}
foreach ($name in $checks.Keys) {
    if (-not (Test-Path $checks[$name])) { throw "迁移后缺少 $name：$($checks[$name])" }
    Write-Host "  [OK ] $name"
}
# Python 在工具链里是 uv 的版本化目录（如 cpython-3.11.16-...\python.exe），
# 因此用"能找到就报 OK"的方式校验，不写死子目录名。
$pyFound = Get-ChildItem -Path (Join-Path $Target "python") -Filter "python.exe" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pyFound) { throw "迁移后未找到工具链内的 python.exe" }
Write-Host "  [OK ] python.exe（$($pyFound.Directory.Name)）"
if (Test-Path (Join-Path $Target "redis-data")) { Write-Host "  [OK ] redis-data（持久化数据已随迁）" }
if (Test-Path $Source) { throw "源目录仍存在，迁移不完整" }
Write-Host "  源目录已清空"

# ---------------------------------------------------------------------------
# 4) 修正 fetch-toolchain.ps1 的默认新建位置
# ---------------------------------------------------------------------------
Write-Host "[4/6] 修正工具链默认位置（供新机器使用）"
$fetch = Join-Path $PSScriptRoot "fetch-toolchain.ps1"
$content = Get-Content $fetch -Raw -Encoding UTF8
$oldDefault = 'return (Join-Path (Split-Path -Parent $ProjectRoot) ".toolchain")'
$newDefault = 'return (Join-Path (Split-Path -Parent $ProjectRoot) ".toolchain")  # 项目二\.toolchain：与工程同级，使项目二成为自包含单元'
if ($content -notlike "*$oldDefault*") {
    throw "未找到预期的默认路径语句，请人工确认 fetch-toolchain.ps1 的 Resolve-ToolchainDir"
}
$content = $content.Replace($oldDefault, $newDefault)
Set-Content -Path $fetch -Value $content -Encoding UTF8 -NoNewline
Write-Host "  已更新默认位置注释（默认值本身即为 项目二\.toolchain，无需改逻辑）"

# ---------------------------------------------------------------------------
# 5) 重启
# ---------------------------------------------------------------------------
if ($SkipRestart) {
    Write-Host "[5/6] 按参数要求跳过重启"
    Write-Host "[6/6] 完成"
    exit 0
}
Write-Host "[5/6] 重启全栈"
& (Join-Path $PSScriptRoot "serve.ps1") -WithDashboard

# ---------------------------------------------------------------------------
# 6) 迁移后自查
# ---------------------------------------------------------------------------
Write-Host "[6/6] 迁移后自查"
$ok = $true
foreach ($p in 5432, 5433, 6379) {
    $listening = [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
    Write-Host ("  端口 {0}: {1}" -f $p, $(if ($listening) { "监听中" } else { "未监听" }))
    if (-not $listening) { $ok = $false }
}
foreach ($url in @("http://127.0.0.1:8011/health", "http://127.0.0.1:8010/health")) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
        Write-Host ("  {0} -> {1}" -f $url, $r.StatusCode)
    } catch {
        Write-Host ("  {0} -> 失败: {1}" -f $url, $_.Exception.Message)
        $ok = $false
    }
}
if ($ok) {
    Write-Host ""
    Write-Host "  迁移完成，服务已在新工具链位置运行。"
} else {
    throw "迁移后自查未全部通过，请检查 logs/ 下的服务日志"
}
