<#
.SYNOPSIS
    搭建并启动 PostgreSQL 从库（流复制），全程无需管理员权限。

.DESCRIPTION
    前置条件：主库已按 scripts/pg-init.ps1 初始化并正在运行（5432）。

    步骤：
      1. 校验主库可达、复制账号可登录
      2. pg_basebackup 从主库拉取基础备份到 data/pg/replica（-R 自动生成 standby.signal 与主库连接信息）
      3. 追加从库端口与热备配置
      4. 以普通进程启动从库（5433）
      5. 校验流复制状态（pg_stat_replication / pg_is_in_recovery）

.NOTES
    幂等：从库数据目录已存在时默认跳过重建；-Force 强制重拉。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [int]$PrimaryPort = 5432,
    [int]$ReplicaPort = 5433,
    [string]$SuperUser = "postgres",
    [string]$ReplUser = "repl",
    [string]$AppPassword = "saaskefu_local",
    [int]$WaitSeconds = 90
)

$ErrorActionPreference = "Stop"

function Find-Toolchain {
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir ".toolchain"
        if (Test-Path (Join-Path $candidate "pg16\pgsql\bin\pg_basebackup.exe")) { return $candidate }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "未找到工具链目录 .toolchain"
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
# 数据目录可用环境变量 SAASKEFU_PG_DATA_ROOT 覆盖（原因见 pg-init.ps1 同处注释）
$PgDataRoot  = if ($env:SAASKEFU_PG_DATA_ROOT) { $env:SAASKEFU_PG_DATA_ROOT } else { Join-Path $ProjectRoot "data\pg" }
$ReplicaDir  = Join-Path $PgDataRoot "replica"
$LogRoot     = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

$psql = Join-Path $PgBin "psql.exe"

Write-Host "================================================================"
Write-Host "  搭建 PostgreSQL 从库（流复制）"
Write-Host "  主库 127.0.0.1:$PrimaryPort  →  从库 127.0.0.1:$ReplicaPort"
Write-Host "================================================================"

# ---------------------------------------------------------------- 1) 主库自检
Write-Host "`n[1/4] 校验主库与复制账号"
& (Join-Path $PgBin "pg_isready.exe") -h 127.0.0.1 -p $PrimaryPort -q 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { throw "主库未就绪（127.0.0.1:$PrimaryPort），请先启动主库" }

$env:PGPASSWORD = $AppPassword
$replOk = & $psql -h 127.0.0.1 -p $PrimaryPort -U $ReplUser -d postgres -tAc "SELECT 1" 2>&1
if ($replOk -notmatch "1") {
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    throw "复制账号 $ReplUser 无法登录主库，请先运行 scripts/pg-init.ps1。输出: $replOk"
}
Write-Host "  主库可达，复制账号可登录"

# ---------------------------------------------------------------- 2) basebackup
Write-Host "`n[2/4] pg_basebackup 拉取基础备份"
if ((Test-Path (Join-Path $ReplicaDir "PG_VERSION")) -and -not $Force) {
    Write-Host "  从库数据目录已存在，跳过（-Force 可强制重拉）"
} else {
    # 必须先优雅停掉既有从库，避免数据目录被占用
    & (Join-Path $PSScriptRoot "pgctl.ps1") -Action stop -Role replica 2>&1 | Out-Null
    Remove-Item -Recurse -Force $ReplicaDir -ErrorAction SilentlyContinue

    # -R: 写入 standby.signal 与 primary_conninfo
    # -Xs: 备份期间以流方式传输 WAL
    & (Join-Path $PgBin "pg_basebackup.exe") `
        -h 127.0.0.1 -p $PrimaryPort -U $ReplUser `
        -D $ReplicaDir -Fp -Xs -P -R 2>&1 | ForEach-Object { "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        throw "pg_basebackup 失败"
    }

    # 从库配置：端口与热备参数（ASCII 写入，避免配置编码问题）
    $conf = @"

# ===== 本项目追加（从库）=====
port = $ReplicaPort
listen_addresses = '127.0.0.1'
hot_standby = on
hot_standby_feedback = on
"@
    Add-Content -Path (Join-Path $ReplicaDir "postgresql.conf") -Value $conf -Encoding ascii
    Write-Host "  基础备份完成，从库配置已写入"
}
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- 3) 启动从库
Write-Host "`n[3/4] 启动从库"
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action start -Role replica -ReplicaPort $ReplicaPort -WaitSeconds $WaitSeconds
if ($LASTEXITCODE -ne 0) { throw "从库启动失败" }

# ---------------------------------------------------------------- 4) 校验复制
Write-Host "`n[4/4] 流复制状态校验"
$env:PGPASSWORD = $AppPassword
Write-Host "  主库 pg_stat_replication:"
& $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -c "SELECT client_addr, state, sync_state, sent_lsn, replay_lsn FROM pg_stat_replication;" 2>&1 | ForEach-Object { "    $_" }
Write-Host "  从库恢复状态:"
& $psql -h 127.0.0.1 -p $ReplicaPort -U $SuperUser -d postgres -c "SELECT pg_is_in_recovery() AS is_standby;" 2>&1 | ForEach-Object { "    $_" }
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

Write-Host "`n从库搭建完成：127.0.0.1:$ReplicaPort"
