<#
.SYNOPSIS
    重建本地 PostgreSQL 主从并启动全栈（一次性恢复脚本）。

.DESCRIPTION
    用途：数据目录需要重建时（例如迁移工具链后、或演示数据想从零开始），
    在**同一个长驻任务**里完成"清场 → 初始化主库 → 搭建从库 → 起全栈"。

    为什么必须放在同一个任务里（三个真实踩到的坑）：
      1. 子进程随调用进程树回收：分两次调用时，上一次调用结束会带走 postgres.exe，
         于是下一次调用连不上主库。
      2. `pg_basebackup` 需要主库配合做一次检查点，而受限执行环境会阻止**信号投递**，
         表现为 pg_basebackup **无限挂起**（不是报错）。因此本脚本必须在不受限环境下运行。
      3. 从库搭建失败不应中断启动；主库可用即可提供完整功能（读写分离退化为读主库）。

    前置条件：
      - scripts/setup-ascii-links.ps1 已执行（建立 D:\pgtool 与 D:\pgdata 联接），
        否则 PostgreSQL 的 initdb 会因非 ASCII 路径失败（见 E-9）。
      - 在不受限（可投递信号）的执行环境下运行。

.EXAMPLE
    pwsh -File scripts/rebuild-and-serve.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipReplica,
    [switch]$WithDashboard = $true
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

$env:SAASKEFU_PG_BIN = if (Test-Path "D:\pgtool\bin\pg_ctl.exe") { "D:\pgtool\bin" } else { "" }
$env:SAASKEFU_PG_DATA_ROOT = if (Test-Path "D:\pgdata") { "D:\pgdata" } else { "" }
$env:PGPASSWORD = "saaskefu_local"

Write-Host "================================================================"
Write-Host "  重建本地 PostgreSQL 主从并启动全栈"
Write-Host "  二进制路径: $(if ($env:SAASKEFU_PG_BIN) { $env:SAASKEFU_PG_BIN } else { '（工具链默认）' })"
Write-Host "  数据路径  : $(if ($env:SAASKEFU_PG_DATA_ROOT) { $env:SAASKEFU_PG_DATA_ROOT } else { '（工程 data\pg）' })"
Write-Host "================================================================"

Write-Host "`n[1/5] 清场（终止残留 postgres / pg_basebackup）"
Get-Process postgres, pg_basebackup -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Write-Host "  残留进程数: $((Get-Process postgres -ErrorAction SilentlyContinue | Measure-Object).Count)"

Write-Host "`n[2/5] 初始化主库并启动"
& (Join-Path $PSScriptRoot "pg-init.ps1") -Force 2>&1 |
    Select-String -Pattern "就绪|业务库已创建|初始化完成|失败|FATAL" | ForEach-Object { "  $_" }

if (-not $SkipReplica) {
    Write-Host "`n[3/5] 搭建从库"
    & (Join-Path $PSScriptRoot "pg-replica.ps1") -Force 2>&1 |
        Select-String -Pattern "完成|失败|streaming|standby|basebackup|错误" | ForEach-Object { "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  从库搭建未成功；主库仍可用，读写分离将退化为读主库。" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[3/5] 按要求跳过从库"
}

Write-Host "`n[4/5] 复制与就绪状态"
$psql = if ($env:SAASKEFU_PG_BIN) { Join-Path $env:SAASKEFU_PG_BIN "psql.exe" } else { Join-Path (Split-Path -Parent $ProjectRoot) ".toolchain\pg16\pgsql\bin\psql.exe" }
& $psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -tAc "SELECT count(*) FROM pg_stat_replication;" 2>&1 | ForEach-Object { "  复制连接数: $_" }
& $psql -h 127.0.0.1 -p 5433 -U postgres -d postgres -tAc "SELECT pg_is_in_recovery();" 2>&1 | ForEach-Object { "  从库 in_recovery: $_" }
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

Write-Host "`n[5/5] 启动全栈（长驻，承载 PG / Redis / Mock / API / 看板）"
& (Join-Path $PSScriptRoot "serve.ps1") -WithDashboard:$WithDashboard
