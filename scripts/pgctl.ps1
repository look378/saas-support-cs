<#
.SYNOPSIS
    免管理员的 PostgreSQL 进程管理工具。

.DESCRIPTION
    Windows 上 pg_ctl 启动实例时需要创建受限令牌（restricted token），
    在无管理员权限的环境下会失败：

        pg_ctl: could not create restricted token: error code 87
        pg_ctl: could not start server: error code 3

    因此本项目**不使用 pg_ctl 起服务**，改为直接以当前用户身份运行
    postgres.exe（它本身不需要受限令牌），并自行记录 PID 以便停服与查状态。
    这与"禁止 Start-Service、禁止注册 Windows 服务"的约束一致。

    用法：
        .\scripts\pgctl.ps1 -Action start   -Role primary
        .\scripts\pgctl.ps1 -Action start   -Role replica
        .\scripts\pgctl.ps1 -Action stop    -Role primary
        .\scripts\pgctl.ps1 -Action status  -Role primary
        .\scripts\pgctl.ps1 -Action start   -Role all
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [ValidateSet("primary", "replica", "all")]
    [string]$Role,

    [int]$PrimaryPort = 5432,
    [int]$ReplicaPort = 5433,
    [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Stop"

# 工具链定位：向上逐级查找 .toolchain 标记，工程可整体移动
function Find-Toolchain {
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir ".toolchain"
        if (Test-Path (Join-Path $candidate "pg16\pgsql\bin\postgres.exe")) { return $candidate }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "未找到工具链目录 .toolchain（应包含 pg16\pgsql\bin）"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Toolchain   = Find-Toolchain
# ---- PostgreSQL 二进制目录解析（含已知缺陷绕行）----
# PostgreSQL 有个**已知缺陷**：initdb 在"二进制路径含非 ASCII 字符"时，
# bootstrap 阶段会报 `invalid byte sequence for encoding "UTF8"`（pgsql-bugs #16926）。
# 本工程路径含中文，因此约定用一个纯 ASCII 目录联接指向工具链里的 pgsql 目录
# （见 docs/environment-constraints.md E-9），并支持用 SAASKEFU_PG_BIN 覆盖。
function Resolve-PgBin {
    param([string]$Toolchain)
    if ($env:SAASKEFU_PG_BIN) { return $env:SAASKEFU_PG_BIN }
    return (Join-Path $Toolchain "pg16\pgsql\bin")
}
$PgBin       = Resolve-PgBin -Toolchain $Toolchain
# 数据目录可用环境变量 SAASKEFU_PG_DATA_ROOT 覆盖（原因见 pg-init.ps1 同处注释：
# Windows 版 PG 无法处理非 ASCII 路径，工程路径含中文时用 ASCII 目录联接绕开）。
$DataRoot    = if ($env:SAASKEFU_PG_DATA_ROOT) { $env:SAASKEFU_PG_DATA_ROOT } else { Join-Path $ProjectRoot "data\pg" }
$LogRoot     = Join-Path $ProjectRoot "logs"
$RunRoot     = Join-Path $ProjectRoot "data\run"
New-Item -ItemType Directory -Force -Path $LogRoot, $RunRoot | Out-Null

function Get-RoleSpec {
    param([string]$Name)
    switch ($Name) {
        "primary" { return @{ Name = "primary"; Dir = (Join-Path $DataRoot "primary"); Port = $PrimaryPort } }
        "replica" { return @{ Name = "replica"; Dir = (Join-Path $DataRoot "replica"); Port = $ReplicaPort } }
    }
}

function Get-PidFile { param($Spec) return (Join-Path $RunRoot "pg-$($Spec.Name).pid") }
function Get-LogFile { param($Spec) return (Join-Path $LogRoot "pg-$($Spec.Name).log") }

function Get-RunningPid {
    param($Spec)
    $pidFile = Get-PidFile $Spec
    if (-not (Test-Path $pidFile)) { return $null }
    $raw = (Get-Content $pidFile -Raw).Trim()
    if (-not $raw) { return $null }
    $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like "postgres*") { return $proc.Id }
    return $null
}

function Test-PortReady {
    param([int]$Port, [int]$TimeoutSec)
    $isReady = Join-Path $PgBin "pg_isready.exe"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        & $isReady -h 127.0.0.1 -p $Port -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

<#
    清理"半启动"残留。

    实测教训：实例若在启动途中被强杀（例如上一次的 initdb/启动被中断），
    数据目录会留下 postmaster.pid。此后若仅以"进程是否存在"判断状态，
    会出现两种情况：
      1. 进程已死但 pid 文件仍在 → 误判为"已运行"，从而跳过启动；
      2. 进程仍在但从未完成启动（不监听端口）→ 误判为"正常"。
    两者都会让后续步骤拿到一个连不上的数据库。
    因此这里以"端口是否就绪"为准，并在未就绪时清理陈旧 pid 文件。
#>
function Clear-StaleState {
    param($Spec)
    $isReady = Join-Path $PgBin "pg_isready.exe"
    & $isReady -h 127.0.0.1 -p $Spec.Port -q 2>$null | Out-Null
    $listening = ($LASTEXITCODE -eq 0)
    $running = Get-RunningPid $Spec

    if ($listening) { return @{ action = "running"; pid = $running } }
    if ($running) {
        Write-Host "  [$($Spec.Name)] 进程 $running 存在但端口未就绪，判定为半启动，强制终止"
        Stop-Process -Id $running -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 800
    }
    $pidFile = Join-Path $Spec.Dir "postmaster.pid"
    if (Test-Path $pidFile) {
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        Write-Host "  [$($Spec.Name)] 已清理陈旧 postmaster.pid"
    }
    Remove-Item (Get-PidFile $Spec) -ErrorAction SilentlyContinue
    return @{ action = "clean"; pid = $null }
}

function Start-Instance {
    param($Spec)
    $state = Clear-StaleState $Spec
    if ($state.action -eq "running") {
        Write-Host "  [$($Spec.Name)] 已在运行 (PID $($state.pid), 端口 $($Spec.Port))"
        return
    }
    if (-not (Test-Path (Join-Path $Spec.Dir "PG_VERSION"))) {
        # 从库缺失时**不抛异常**：主库可用即可提供完整功能（读写分离会退化为全部读主库），
        # 从库应当由 pg-replica.ps1 单独搭建。若在此抛错，会中断整个启动流程，
        # 导致 Redis / API / 看板都起不来（本项目实测踩到：从库目录缺失时全栈无法启动）。
        if ($Spec.Name -eq "replica") {
            Write-Host "  [replica] 数据目录未初始化，跳过（如需读写分离请先运行 scripts/pg-replica.ps1）"
            return
        }
        throw "[$($Spec.Name)] 数据目录未初始化: $($Spec.Dir) —— 请先运行 scripts/pg-init.ps1"
    }

    # 每次启动使用独立日志，避免历史错误干扰本次判断
    $log = Get-LogFile $Spec
    $errLog = "$log.err"
    Remove-Item $log, $errLog -Force -ErrorAction SilentlyContinue

    # 直接运行 postgres.exe：不需要受限令牌，无需管理员权限
    $proc = Start-Process -FilePath (Join-Path $PgBin "postgres.exe") `
        -ArgumentList @("-D", $Spec.Dir, "-p", "$($Spec.Port)") `
        -RedirectStandardOutput $log `
        -RedirectStandardError $errLog `
        -WindowStyle Hidden `
        -PassThru

    Set-Content -Path (Get-PidFile $Spec) -Value $proc.Id -Encoding ascii

    if (-not (Test-PortReady -Port $Spec.Port -TimeoutSec $WaitSeconds)) {
        $tail = ""
        if (Test-Path $errLog) { $tail = (Get-Content $errLog -Tail 20) -join "`n" }
        throw "[$($Spec.Name)] 启动后 $WaitSeconds 秒内未就绪。日志尾部:`n$tail"
    }
    Write-Host "  [$($Spec.Name)] 已启动 PID=$($proc.Id) 端口=$($Spec.Port) 日志=$log"
}

function Stop-Instance {
    param($Spec)
    $runningPid = Get-RunningPid $Spec
    if (-not $runningPid) {
        Write-Host "  [$($Spec.Name)] 未在运行"
        Remove-Item (Get-PidFile $Spec) -ErrorAction SilentlyContinue
        return
    }
    # 优先用 pg_ctl stop（不需要受限令牌，只有 start 才需要）
    $ctl = Join-Path $PgBin "pg_ctl.exe"
    & $ctl -D $Spec.Dir -m fast stop 2>&1 | Out-Null
    Start-Sleep -Milliseconds 800
    if (Get-Process -Id $runningPid -ErrorAction SilentlyContinue) {
        Write-Host "  [$($Spec.Name)] pg_ctl stop 未生效，强制终止 PID=$runningPid"
        Stop-Process -Id $runningPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item (Get-PidFile $Spec) -ErrorAction SilentlyContinue
    Write-Host "  [$($Spec.Name)] 已停止"
}

function Get-Status {
    param($Spec)
    $runningPid = Get-RunningPid $Spec
    if ($runningPid) {
        $ready = Test-PortReady -Port $Spec.Port -TimeoutSec 3
        Write-Host "  [$($Spec.Name)] 运行中 PID=$runningPid 端口=$($Spec.Port) 就绪=$ready"
        return $true
    }
    Write-Host "  [$($Spec.Name)] 未运行"
    return $false
}

$targets = if ($Role -eq "all") { @("primary", "replica") } else { @($Role) }

foreach ($name in $targets) {
    $spec = Get-RoleSpec $name
    switch ($Action) {
        "start"   { Start-Instance $spec }
        "stop"    { Stop-Instance $spec }
        "restart" { Stop-Instance $spec; Start-Instance $spec }
        "status"  { Get-Status $spec | Out-Null }
    }
}
