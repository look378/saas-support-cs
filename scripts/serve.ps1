<#
.SYNOPSIS
    启动全部服务并保持运行（供直接试用）。

.DESCRIPTION
    一次性起 PostgreSQL 主从、Redis、Mock 后端、API 网关与坐席工作台，
    启动完成后**保持前台运行**并定期打印心跳与数据库健康状态。

    为什么要"保持前台运行"：
    本环境会在父进程结束时回收子进程。若用一次性命令启动服务，
    命令一结束服务就随之消失。因此需要一个长驻承载进程，
    本脚本即承担该角色。

    停止：按下 Ctrl+C，或调用 scripts/dev-down.ps1。

.NOTES
    - API 以 `python -m app.main` 启动（自己掌控事件循环，见 docs/environment-constraints.md E-7）
    - 端口：API 8011 / Mock 8010 / 工作台 8502（刻意避开常被占用的 8000/8501）
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8011,
    [int]$MockPort = 8010,
    [int]$DashboardPort = 8502,
    [int]$HeartbeatSeconds = 30,
    [switch]$WithDashboard
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = Split-Path -Parent $PSScriptRoot

# ---- PostgreSQL 的 ASCII 路径绕行（自动化，无需人工设环境变量）----
# 背景：PostgreSQL 有个已知缺陷——initdb / 部分工具在"二进制路径含非 ASCII 字符"时
# 会报 `invalid byte sequence for encoding "UTF8"`（pgsql-bugs #16926）。
# 本工程路径含中文，因此约定：
#   D:\pgtool  --(目录联接)--> <工程>\..\.toolchain\pg16\pgsql     （二进制）
#   D:\pgdata  --(目录联接)--> <工程>\data\pg                      （数据）
# 真实文件都在工程内，联接只是给 PG 一个纯 ASCII 的视角。
# 下面自动探测：联接存在就用联接，不存在则回退真实路径（在纯 ASCII 路径的机器上
# 不需要联接也能正常工作）。联接的创建需要管理员权限，见 scripts/setup-ascii-links.ps1。
$AsciiPgBin  = "D:\pgtool\bin"
$AsciiPgData = "D:\pgdata"
if (Test-Path (Join-Path $AsciiPgBin "pg_ctl.exe")) {
    $env:SAASKEFU_PG_BIN = $AsciiPgBin
    Write-Host "  [pg] 使用 ASCII 二进制路径：$AsciiPgBin" -ForegroundColor DarkGray
}
if (Test-Path $AsciiPgData) {
    $env:SAASKEFU_PG_DATA_ROOT = $AsciiPgData
    Write-Host "  [pg] 使用 ASCII 数据路径：$AsciiPgData" -ForegroundColor DarkGray
}

$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectRoot "logs"
$RunDir = Join-Path $ProjectRoot "data\run"
New-Item -ItemType Directory -Force -Path $LogDir, $RunDir | Out-Null

function Test-Port {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Show-Owner {
    param([int]$Port)
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $c) { return "空闲" }
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    $cmd = ""
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue).CommandLine
    } catch { }
    if ($cmd -and $cmd.Length -gt 90) { $cmd = $cmd.Substring(0, 90) + "…" }
    return "PID=$($c.OwningProcess) $($proc.ProcessName) :: $cmd"
}

function Start-Svc {
    param([string]$Name, [string]$CmdLine, [int]$Port, [string]$ProbeUrl)
    if ($ProbeUrl -and (Test-Port -Port $Port)) {
        Write-Host "  [$Name] 端口 $Port 已在监听，跳过"
        return $null
    }
    $out = Join-Path $LogDir "$Name.log"
    $err = Join-Path $LogDir "$Name.log.err"
    # 用 -ArgumentList 传字符串数组时，PowerShell 会把参数拼成命令行并可能丢失引号，
    # 例如 `-m app.main` 会被拆坏、退化成交互式解释器（实测踩到）。
    # 因此改走 cmd 的 /c，由 cmd 负责引号与拼接，参数传递更可靠。
    $proc = Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/c", "`"$Py`" $CmdLine") `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $proc.Id -Encoding ascii
    Write-Host "  [$Name] 已启动 PID=$($proc.Id) 端口=$Port 日志=$out"
    return $proc
}

Write-Host "================================================================"
Write-Host "  启动服务拓扑（保持运行）"
Write-Host "  工程：$ProjectRoot"
Write-Host "================================================================"

Write-Host "`n[1/5] PostgreSQL 主从"
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action start -Role all

Write-Host "`n[2/5] Redis"
& $Py (Join-Path $PSScriptRoot "redisctl.py") start

Write-Host "`n[3/5] 数据库结构同步"
& $Py -m app.cli init-db 2>&1 | Select-String -Pattern "表结构已就绪" | ForEach-Object { "    $_" }

Write-Host "`n[4/5] 种子数据与知识库索引"
& $Py -m app.cli seed 2>&1 | Select-String -Pattern "indexed|tenants|agents|users|cases" | Select-Object -First 6 | ForEach-Object { "    $_" }

Write-Host "`n[5/5] Mock 后端 / API 网关 / 坐席工作台"
$mock = Start-Svc -Name "mock" -CmdLine "-m uvicorn app.mocks.server:app --host 127.0.0.1 --port $MockPort" -Port $MockPort -ProbeUrl "http://127.0.0.1:$MockPort/health"
$api = Start-Svc -Name "api" -CmdLine "-m app.main" -Port $ApiPort -ProbeUrl "http://127.0.0.1:$ApiPort"

# 就绪探测用**本工程特有**接口，而不是 /health：
# 端口被其它工程占用时 /health 也会 200，会造成"已就绪"的假象。
$probe = "http://127.0.0.1:$ApiPort/api/v1/agent/metrics/overview"
$apiReady = $false
for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri $probe -Headers @{ "X-Tenant-ID" = "t-001" } -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $apiReady = $true; Write-Host "    API 就绪（第 $i 次探测）"; break }
    } catch { }
}
if (-not $apiReady) {
    Write-Host "    API 未就绪，日志尾部："
    Get-Content (Join-Path $LogDir "api.log.err") -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { "      $_" }
}

$dash = $null
if ($WithDashboard) {
    if (Test-Port -Port $DashboardPort) {
        Write-Host "  [dashboard] 端口 $DashboardPort 已在监听，跳过"
    } else {
        $out = Join-Path $LogDir "dashboard.log"
        $err = Join-Path $LogDir "dashboard.log.err"
        $dash = Start-Process -FilePath "cmd.exe" `
            -ArgumentList @("/c", "`"$Py`" -m streamlit run app/dashboard.py --server.port $DashboardPort --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false") `
            -WorkingDirectory $ProjectRoot -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
        Set-Content -Path (Join-Path $RunDir "dashboard.pid") -Value $dash.Id -Encoding ascii
        for ($i = 1; $i -le 60; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-Port -Port $DashboardPort) { Write-Host "    [dashboard] 就绪（第 $i 次探测）"; break }
        }
        Write-Host "  [dashboard] PID=$($dash.Id) 端口=$DashboardPort"
    }
}

Write-Host "`n================================================================"
Write-Host "  试用入口"
Write-Host "================================================================"
Write-Host "  坐席实时控制台（真实 WebSocket 推送）"
Write-Host "      http://127.0.0.1:$ApiPort/static/agent_console.html"
Write-Host "  接口文档（Swagger）"
Write-Host "      http://127.0.0.1:$ApiPort/docs"
Write-Host "  用户端对话接口"
Write-Host "      POST http://127.0.0.1:$ApiPort/api/v1/chat   （需请求头 X-Tenant-ID: t-001）"
if ($WithDashboard) {
    Write-Host "  坐席工作台（Streamlit）"
    Write-Host "      http://127.0.0.1:$DashboardPort"
}
Write-Host ""
Write-Host "  坐席账号：agent_zhao / agent_lin / lead_chen    口令：Agent@12345"
Write-Host "  租户：t-001（标准版） / t-002（旗舰版）"
Write-Host ""
Write-Host "  端口归属检查："
foreach ($p in @($ApiPort, $MockPort, $DashboardPort)) {
    Write-Host "    $p -> $(Show-Owner -Port $p)"
}
Write-Host "================================================================"
Write-Host "  服务已在运行。保持本进程存活即可持续使用；按 Ctrl+C 结束。"
Write-Host "================================================================"

# 心跳：让长驻进程有可见输出，同时持续确认依赖存活
$n = 0
while ($true) {
    Start-Sleep -Seconds $HeartbeatSeconds
    $n++
    $apiOk = Test-Port -Port $ApiPort
    $mockOk = Test-Port -Port $MockPort
    $redisOk = Test-Port -Port 6379
    # PostgreSQL 就绪判定：直接探测端口 + 连接，而不是解析 pgctl 的文本输出。
    # 解析文本会因编码/格式差异误判（实测踩到：明明就绪却显示 down）。
    # 注意：pg_isready 的路径必须同样**向上查找工具链**，不能硬编码工程相对路径，
    # 否则工具链位置一变，启动脚本自家心跳就先失效（审查发现）。
    $pgOk = $false
    try {
        $toolchain = $null
        $probeDir = $ProjectRoot
        while ($probeDir) {
            $cand = Join-Path $probeDir ".toolchain"
            if (Test-Path (Join-Path $cand "pg16\pgsql\bin\pg_isready.exe")) { $toolchain = $cand; break }
            $parent = Split-Path -Parent $probeDir
            if ($parent -eq $probeDir) { break }
            $probeDir = $parent
        }
        if ($toolchain) {
            & (Join-Path $toolchain "pg16\pgsql\bin\pg_isready.exe") -h 127.0.0.1 -p 5432 -q 2>$null
            $pgOk = ($LASTEXITCODE -eq 0)
        } else {
            $pgOk = (Test-Port -Port 5432)
        }
    } catch {
        $pgOk = (Test-Port -Port 5432)
    }
    if ($WithDashboard) {
        $dashOk = Test-Port -Port $DashboardPort
    } else {
        $dashOk = $null
    }

    $parts = @(
        "api=$(if ($apiOk) { 'up' } else { 'DOWN' })",
        "mock=$(if ($mockOk) { 'up' } else { 'DOWN' })",
        "redis=$(if ($redisOk) { 'up' } else { 'DOWN' })",
        "pg=$(if ($pgOk) { 'up' } else { 'DOWN' })"
    )
    if ($dashOk -ne $null) { $parts += "dashboard=$(if ($dashOk) { 'up' } else { 'DOWN' })" }
    Write-Host ("[{0}] 心跳 #{1}  {2}" -f (Get-Date -Format "HH:mm:ss"), $n, ($parts -join "  "))
}
