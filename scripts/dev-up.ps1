<#
.SYNOPSIS
    一键启动本机全部服务（PostgreSQL 主从 + Redis + Mock 服务 + API 服务）。

.DESCRIPTION
    全部以普通进程方式启动，不注册 Windows 服务、不调用 Start-Service、不使用 Docker，
    因此无需管理员权限。

    启动顺序：
      1. PostgreSQL 主库 127.0.0.1:5432
      2. PostgreSQL 从库 127.0.0.1:5433（流复制）
      3. Redis          127.0.0.1:6379
      4. Mock 业务后端  127.0.0.1:8010
      5. API 网关       127.0.0.1:8011
      6. Streamlit 坐席工作台 127.0.0.1:8502（可选，-WithDashboard）

.NOTES
    幂等：已在运行的服务会被跳过。
    停止：scripts/dev-down.ps1
#>
[CmdletBinding()]
param(
    [switch]$WithDashboard,
    [switch]$SkipApp,
    [switch]$SkipRedis,
    [switch]$SkipMock
)

$ErrorActionPreference = "Stop"

function Find-Toolchain {
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir ".toolchain"
        if (Test-Path (Join-Path $candidate "pg16\pgsql\bin\postgres.exe")) { return $candidate }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "未找到工具链目录 .toolchain，请先执行 scripts/fetch-toolchain.ps1"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Toolchain   = Find-Toolchain
$VenvPy      = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$LogRoot     = Join-Path $ProjectRoot "logs"
$RunRoot     = Join-Path $ProjectRoot "data\run"
New-Item -ItemType Directory -Force -Path $LogRoot, $RunRoot | Out-Null

if (-not (Test-Path $VenvPy)) { throw "未找到项目虚拟环境: $VenvPy" }

function Test-PortOpen {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSec = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -ge 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-PythonService {
    param(
        [string]$Name,
        [string[]]$Args,
        [int]$Port,
        [string]$HealthUrl
    )
    if (Test-PortOpen -Port $Port) {
        Write-Host "  [$Name] 端口 $Port 已在监听，跳过"
        return
    }
    $out = Join-Path $LogRoot "$Name.log"
    $err = Join-Path $LogRoot "$Name.log.err"
    $proc = Start-Process -FilePath $VenvPy -ArgumentList $Args `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $out -RedirectStandardError $err `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunRoot "$Name.pid") -Value $proc.Id -Encoding ascii

    if ($HealthUrl) {
        if (-not (Wait-Http -Url $HealthUrl -TimeoutSec 45)) {
            $tail = if (Test-Path $err) { (Get-Content $err -Tail 15) -join "`n" } else { "" }
            throw "[$Name] 启动后未通过健康检查: $HealthUrl`n$tail"
        }
    }
    Write-Host "  [$Name] 已启动 PID=$($proc.Id) 端口=$Port 日志=$out"
}

Write-Host "================================================================"
Write-Host "  启动本机服务拓扑"
Write-Host "  工程: $ProjectRoot"
Write-Host "================================================================"

# ---------------------------------------------------------------- 1) PostgreSQL
Write-Host "`n[1/6] PostgreSQL 主从"
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action start -Role primary
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action start -Role replica

# ---------------------------------------------------------------- 2) Redis
Write-Host "`n[2/6] Redis"
if ($SkipRedis) {
    Write-Host "  已跳过"
} elseif (Test-PortOpen -Port 6379) {
    Write-Host "  端口 6379 已在监听，跳过"
} else {
    $redisExe = Join-Path $Toolchain "redis\redis-server.exe"
    $redisConf = Join-Path $ProjectRoot "data\redis\redis.conf"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $redisConf) | Out-Null
    # 生成最小配置：监听本机、开启 AOF 便于重启后状态可恢复性验证
    @"
bind 127.0.0.1
port 6379
dir $(Join-Path $ProjectRoot 'data\redis')
appendonly yes
appendfsync everysec
save 900 1
logfile ""
"@ | Set-Content -Path $redisConf -Encoding ascii

    $redisOut = Join-Path $LogRoot "redis.log"
    $redisErr = Join-Path $LogRoot "redis.log.err"
    $proc = Start-Process -FilePath $redisExe -ArgumentList @($redisConf) `
        -WorkingDirectory (Split-Path -Parent $redisExe) `
        -RedirectStandardOutput $redisOut -RedirectStandardError $redisErr `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunRoot "redis.pid") -Value $proc.Id -Encoding ascii

    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 400
        if (Test-PortOpen -Port 6379) { $ok = $true; break }
    }
    if (-not $ok) {
        $tail = if (Test-Path $redisErr) { (Get-Content $redisErr -Tail 10) -join "`n" } else { "" }
        throw "[redis] 启动失败`n$tail"
    }
    Write-Host "  [redis] 已启动 PID=$($proc.Id) 端口=6379"
}

# ---------------------------------------------------------------- 3) Mock 服务
Write-Host "`n[3/6] Mock 业务后端"
if ($SkipMock) {
    Write-Host "  已跳过"
} else {
    Start-PythonService -Name "mock" `
        -Args @("-m", "uvicorn", "app.mocks.server:app", "--host", "127.0.0.1", "--port", "8010") `
        -Port 8010 -HealthUrl "http://127.0.0.1:8010/health"
}

# ---------------------------------------------------------------- 4) API 网关
Write-Host "`n[4/6] API 网关"
if ($SkipApp) {
    Write-Host "  已跳过"
} else {
    Start-PythonService -Name "api" `
        -Args @("-m", "app.main") `
        -Port 8011 -HealthUrl "http://127.0.0.1:8011/health"
}

# ---------------------------------------------------------------- 5) 数据库迁移
Write-Host "`n[5/6] 数据库结构同步"
if ($SkipApp) {
    Write-Host "  已跳过"
} else {
    & $VenvPy -m app.cli init-db 2>&1 | ForEach-Object { "    $_" }
}

# ---------------------------------------------------------------- 6) 工作台
Write-Host "`n[6/6] 坐席工作台"
if ($WithDashboard) {
    if (Test-PortOpen -Port 8502) {
        Write-Host "  端口 8502 已在监听，跳过"
    } else {
        $out = Join-Path $LogRoot "dashboard.log"
        $err = Join-Path $LogRoot "dashboard.log.err"
        $proc = Start-Process -FilePath $VenvPy -ArgumentList @(
            "-m", "streamlit", "run", "app/dashboard.py",
            "--server.port", "8502", "--server.address", "127.0.0.1",
            "--server.headless", "true"
        ) -WorkingDirectory $ProjectRoot `
          -RedirectStandardOutput $out -RedirectStandardError $err `
          -WindowStyle Hidden -PassThru
        Set-Content -Path (Join-Path $RunRoot "dashboard.pid") -Value $proc.Id -Encoding ascii
        if (-not (Wait-Http -Url "http://127.0.0.1:8502" -TimeoutSec 60)) {
            throw "[dashboard] 启动失败，见 $err"
        }
        Write-Host "  [dashboard] 已启动 PID=$($proc.Id) 地址=http://127.0.0.1:8502"
    }
} else {
    Write-Host "  未启用（加 -WithDashboard 启用）"
}

# ---------------------------------------------------------------- 汇总
Write-Host "`n================================================================"
Write-Host "  服务拓扑状态"
Write-Host "================================================================"
$ports = @(
    @{ Name = "PostgreSQL 主库"; Port = 5432 },
    @{ Name = "PostgreSQL 从库"; Port = 5433 },
    @{ Name = "Redis"; Port = 6379 },
    @{ Name = "Mock 后端"; Port = 8010 },
    @{ Name = "API 网关"; Port = 8011 },
    @{ Name = "坐席工作台"; Port = 8502 }
)
foreach ($p in $ports) {
    $state = if (Test-PortOpen -Port $p.Port) { "监听中" } else { "未启动" }
    Write-Host ("  {0,-16} :{1}  {2}" -f $p.Name, $p.Port, $state)
}
Write-Host ""
