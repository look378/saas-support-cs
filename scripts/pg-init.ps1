<#
.SYNOPSIS
    初始化 PostgreSQL 主从双实例（免安装 zip 版，无需管理员权限）。

.DESCRIPTION
    1. 主库 initdb → data/pg/primary，监听 5432
    2. 启动主库，创建复制账号、业务库、checkpointer 所需扩展
    3. pg_basebackup 从主库复制 → data/pg/replica，监听 5433
    4. 从库以 standby 模式启动，建立流复制

    全程不注册 Windows 服务、不调用 Start-Service、不写系统目录。

.NOTES
    幂等：若主库已初始化则跳过重建；-Force 可强制重建（会清空数据）。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [int]$PrimaryPort = 5432,
    [int]$ReplicaPort = 5433,
    [string]$SuperUser = "postgres",
    [string]$AppUser = "saaskefu",
    [string]$AppPassword = "saaskefu_local",
    [string]$Database = "saaskefu"
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- 路径与变量
# 工具链定位不依赖固定的目录层级：自脚本向上逐级查找 .toolchain 标记目录，
# 这样整个工程移动位置后脚本依然可用（本项目工程位于多级目录下，曾因此踩坑）。
function Find-Toolchain {
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir ".toolchain"
        if (Test-Path (Join-Path $candidate "pg16\pgsql\bin\initdb.exe")) { return $candidate }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "未找到工具链目录 .toolchain（应包含 pg16\pgsql\bin）。请先执行 scripts/fetch-toolchain.ps1"
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
# 数据目录可用环境变量 SAASKEFU_PG_DATA_ROOT 覆盖。
# 为什么需要：Windows 版 PostgreSQL 的 initdb/bootstrap 无法处理**非 ASCII 路径**
# （会在 bootstrap 阶段报 invalid byte sequence for encoding "UTF8"）。
# 本工程的路径含中文，因此约定用一个纯 ASCII 的目录联接指向真实目录，
# 再把这里指到联接路径上；真实数据仍留在工程内（见 docs/environment-constraints.md E-9）。
$DataRoot    = if ($env:SAASKEFU_PG_DATA_ROOT) { $env:SAASKEFU_PG_DATA_ROOT } else { Join-Path $ProjectRoot "data\pg" }
$PrimaryDir  = Join-Path $DataRoot "primary"
$ReplicaDir  = Join-Path $DataRoot "replica"
$LogRoot     = Join-Path $ProjectRoot "logs"
$PwFile      = Join-Path $env:TEMP "saaskefu_pgpw.txt"

function Assert-PgBin {
    $required = @("initdb.exe", "pg_ctl.exe", "psql.exe", "pg_basebackup.exe")
    foreach ($exe in $required) {
        $p = Join-Path $PgBin $exe
        if (-not (Test-Path $p)) { throw "缺少 PostgreSQL 可执行文件: $p" }
    }
}

function New-PwFile {
    param([string]$Password)
    # initdb / pg_basebackup 通过该文件读取口令
    Set-Content -Path $PwFile -Value $Password -Encoding ascii -NoNewline
    return $PwFile
}

function Wait-PgReady {
    param([int]$Port, [string]$Db = "postgres", [int]$TimeoutSec = 60)
    $exe = Join-Path $PgBin "pg_isready.exe"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        & $exe -h 127.0.0.1 -p $Port -d $Db -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-PgIfRunning {
    param([string]$Role)
    # 统一走 pgctl.ps1：pg_ctl 在无管理员权限下无法启动实例（受限令牌问题）
    & (Join-Path $PSScriptRoot "pgctl.ps1") -Action stop -Role $Role 2>&1 | Out-Null
}


Assert-PgBin
New-Item -ItemType Directory -Force -Path $DataRoot, $LogRoot | Out-Null

Write-Host "================================================================"
Write-Host "  PostgreSQL 主从双实例初始化"
Write-Host "  PostgreSQL : $((& (Join-Path $PgBin 'postgres.exe') --version))"
Write-Host "  主库目录   : $PrimaryDir  (端口 $PrimaryPort)"
Write-Host "  从库目录   : $ReplicaDir  (端口 $ReplicaPort)"
Write-Host "================================================================"

if ($Force) {
    Write-Host "[Force] 清理既有数据目录"
    Stop-PgIfRunning -Role primary
    Stop-PgIfRunning -Role replica
    Remove-Item -Recurse -Force $PrimaryDir, $ReplicaDir -ErrorAction SilentlyContinue
}

# ================================================================ 1) 主库
Write-Host "`n[1/5] 初始化主库"
if (Test-Path (Join-Path $PrimaryDir "PG_VERSION")) {
    Write-Host "  已初始化，跳过 initdb"
} else {
    New-Item -ItemType Directory -Force -Path $PrimaryDir | Out-Null
    $pw = New-PwFile -Password $AppPassword
    # --auth-local=trust : 本机连接免密，避免 pg_hba 反复调整
    # --auth-host=scram-sha-256 : 走 TCP 的连接必须带口令
    # --encoding=UTF8 : 中文数据必须
    & (Join-Path $PgBin "initdb.exe") `
        -D $PrimaryDir `
        -U $SuperUser `
        --pwfile=$pw `
        --auth-local=trust `
        --auth-host=scram-sha-256 `
        --encoding=UTF8 `
        --locale=C 2>&1 | ForEach-Object { "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "initdb 失败（主库）" }
}

# 主库配置：端口、只监听本机、复制参数、checkpointer 需要的 WAL 级别
$primaryConf = @"

# ===== 本项目追加（主库）=====
port = $PrimaryPort
listen_addresses = '127.0.0.1'
max_connections = 100
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
hot_standby = on
wal_keep_size = 256MB
synchronous_commit = local
logging_collector = off
timezone = 'Asia/Shanghai'
statement_timeout = 15000
"@
Add-Content -Path (Join-Path $PrimaryDir "postgresql.conf") -Value $primaryConf -Encoding utf8

# 允许本机复制连接与业务连接
$hbaPath = Join-Path $PrimaryDir "pg_hba.conf"
$hbaAppend = @"

# ===== 本项目追加 =====
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
host    replication     repl            127.0.0.1/32            scram-sha-256
host    replication     $SuperUser      127.0.0.1/32            scram-sha-256
"@
Add-Content -Path $hbaPath -Value $hbaAppend -Encoding utf8

Write-Host "[2/5] 启动主库"
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action start -Role primary -PrimaryPort $PrimaryPort
if (-not (Wait-PgReady -Port $PrimaryPort)) { throw "主库启动后未就绪，见 logs/pg-primary.log" }
Write-Host "  主库就绪: 127.0.0.1:$PrimaryPort"

# ================================================================ 2) 账号与库
Write-Host "`n[3/5] 创建复制账号与应用库"
$env:PGPASSWORD = $AppPassword
$psql = Join-Path $PgBin "psql.exe"
$sql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'repl') THEN
        CREATE ROLE repl WITH REPLICATION LOGIN PASSWORD '$AppPassword';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$AppUser') THEN
        CREATE ROLE $AppUser WITH LOGIN PASSWORD '$AppPassword' CREATEDB;
    END IF;
END
`$`$;
"@
$sql | & $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -v ON_ERROR_STOP=1 2>&1 | ForEach-Object { "    $_" }
if ($LASTEXITCODE -ne 0) { throw "创建角色失败" }

# 复制账号需要能触发检查点：pg_basebackup 会通过复制协议发出 CHECKPOINT 指令，
# 仅具备 REPLICATION 权限时该指令会被拒绝：
#     ERROR: could not signal for checkpoint: Operation not permitted
# 这里授予 PostgreSQL 15+ 提供的专用角色 pg_checkpoint，
# 而不是把复制账号提升为超级用户（最小权限原则）。
& $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres `
    -c "GRANT pg_checkpoint TO repl;" -v ON_ERROR_STOP=1 2>&1 | ForEach-Object { "    $_" }
if ($LASTEXITCODE -ne 0) { throw "授予 pg_checkpoint 失败" }

# 应用账号需要能读复制监控视图（pg_stat_replication）。
# 该视图要求超级用户或 pg_monitor 角色，否则会**静默返回 NULL 行**，
# 让人误以为"流复制断了"（本项目实测踩到）。授予 pg_monitor 而非超级用户。
& $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres `
    -c "GRANT pg_monitor TO $AppUser;" -v ON_ERROR_STOP=1 2>&1 | ForEach-Object { "    $_" }
if ($LASTEXITCODE -ne 0) { throw "授予 pg_monitor 失败" }

# 业务库（存在则跳过）
$dbExists = & $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$Database'"
if ($dbExists -ne "1") {
    & $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -c "CREATE DATABASE $Database OWNER $AppUser ENCODING 'UTF8'" 2>&1 | ForEach-Object { "    $_" }
    if ($LASTEXITCODE -ne 0) { throw "创建业务库失败" }
    Write-Host "  业务库已创建: $Database"
} else {
    Write-Host "  业务库已存在: $Database"
}

# checkpointer 需要以复制角色之外的普通权限建表；授予 schema 权限
$grantSql = @"
GRANT ALL ON SCHEMA public TO $AppUser;
ALTER DATABASE $Database OWNER TO $AppUser;
"@
$grantSql | & $psql -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d $Database -v ON_ERROR_STOP=1 2>&1 | ForEach-Object { "    $_" }

Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

# ================================================================ 主库完成
# 从库搭建已拆分到 scripts/pg-replica.ps1。
# 原因：pg_basebackup 失败会抛出异常并中断整个脚本，导致前面的主库初始化
# 结果无法被后续步骤使用；两者职责分离后，从库失败不会影响主库可用性。
Write-Host "`n[4/4] 主库初始化完成（从库请执行 scripts/pg-replica.ps1）"

Write-Host "`n================================================================"
Write-Host "  主库状态"
Write-Host "================================================================"
$env:PGPASSWORD = $AppPassword
& (Join-Path $PgBin "psql.exe") -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -c "SELECT rolname, rolsuper, rolreplication FROM pg_roles WHERE rolname IN ('repl','$AppUser');" 2>&1 | ForEach-Object { "  $_" }
Write-Host "  复制账号的检查点能力:"
& (Join-Path $PgBin "psql.exe") -h 127.0.0.1 -p $PrimaryPort -U $SuperUser -d postgres -tAc "SELECT member::regrole || ' -> ' || roleid::regrole FROM pg_auth_members WHERE member::regrole::text = 'repl';" 2>&1 | ForEach-Object { "    $_" }
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

Remove-Item $PwFile -ErrorAction SilentlyContinue
Write-Host "`n主库初始化完成：127.0.0.1:$PrimaryPort（库 $Database，应用账号 $AppUser）"
Write-Host "下一步：执行 scripts/pg-replica.ps1 搭建从库（5433）"
