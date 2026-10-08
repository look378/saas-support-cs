<#
.SYNOPSIS
    PostgreSQL 主从一键搭建（初始化 + 从库 + 校验）。

.DESCRIPTION
    把 pg-init.ps1 与 pg-replica.ps1 串成一次完整流程。

    为什么必须串成"一次"：
    在无管理员权限的 Windows 环境下，postgres.exe 只能作为当前进程的子进程运行，
    而子进程的存活与发起它的进程树绑定。若分多次调用（每次一个新进程），
    中途失败时主库会随进程树一起消失，后续步骤就会连不上数据库。
    因此搭建过程应当作为**单个后台任务**执行，而不是多步交互式调用。

.NOTES
    幂等；-Force 会删库重建（数据丢失）。
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipReplica
)

$ErrorActionPreference = "Continue"
$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "############ 步骤 1/3：初始化主库并启动 ############"
& (Join-Path $PSScriptRoot "pg-init.ps1") -Force:$Force 2>&1 | ForEach-Object { $_ }
$initOk = $?
Write-Output "PG_INIT_OK=$initOk"

Write-Host "`n############ 步骤 1.5/3：主库连通性确认 ############"
$pgBin = ""
$dir = $PSScriptRoot
while ($dir) {
    $cand = Join-Path $dir ".toolchain"
    if (Test-Path (Join-Path $cand "pg16\pgsql\bin\pg_isready.exe")) { $pgBin = Join-Path $cand "pg16\pgsql\bin"; break }
    $parent = Split-Path -Parent $dir
    if ($parent -eq $dir) { break }
    $dir = $parent
}
& (Join-Path $pgBin "pg_isready.exe") -h 127.0.0.1 -p 5432
Write-Output "PRIMARY_READY_EXIT=$LASTEXITCODE"

if ($LASTEXITCODE -eq 0) {
    $env:PGPASSWORD = "saaskefu_local"
    Write-Host "`n--- repl 角色成员关系 ---"
    & (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "SELECT r.rolname AS member, g.rolname AS granted_role FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member JOIN pg_roles g ON g.oid=m.roleid WHERE r.rolname='repl';"
    Write-Host "--- 以 repl 执行 CHECKPOINT（验证 pg_basebackup 前置能力）---"
    & (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -p 5432 -U repl -d postgres -c "CHECKPOINT;"
    Write-Output "REPL_CHECKPOINT_EXIT=$LASTEXITCODE"
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
}

if (-not $SkipReplica) {
    Write-Host "`n############ 步骤 2/3：搭建从库 ############"
    & (Join-Path $PSScriptRoot "pg-replica.ps1") -Force:$Force 2>&1 | ForEach-Object { $_ }
    Write-Output "PG_REPLICA_OK=$?"
}

Write-Host "`n############ 步骤 3/3：最终状态 ############"
& (Join-Path $PSScriptRoot "pgctl.ps1") -Action status -Role all
Write-Host "`n=== 复制状态 ==="
$env:PGPASSWORD = "saaskefu_local"
& (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "SELECT client_addr, state, sync_state FROM pg_stat_replication;" 2>&1
& (Join-Path $pgBin "psql.exe") -h 127.0.0.1 -p 5433 -U postgres -d postgres -c "SELECT pg_is_in_recovery() AS is_standby;" 2>&1
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
Write-Host "`nSETUP_DONE"
