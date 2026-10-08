<#
.SYNOPSIS
    PostgreSQL 单实例生命周期能力测试（环境能力边界实测）。

.DESCRIPTION
    在**同一个进程**内完成"初始化 → 启动 → 写数据 → 检查点 → 非正常终止 → 恢复"
    全流程，用于确认本环境下 PostgreSQL 的可用边界。

    为什么必须在同一进程内完成：本环境会随调用结束回收子进程，
    跨调用无法保持实例存活。

    为什么需要这个脚本：本项目发现"受限执行环境会阻断进程间信号投递"，
    表现为检查点失败、`pg_basebackup` 失败、崩溃恢复卡住。
    该现象极容易被误判为"环境不支持 PostgreSQL"，
    因此保留本脚本作为**可复现的判定工具**：
    在两种执行模式下各跑一次，即可区分"环境缺陷"与"沙箱限制"。

    输出为结构化结论（PASS/FAIL），供 docs/environment-constraints.md 引用。

.NOTES
    本脚本会创建并清理临时数据目录，不影响工程正式数据。
#>
[CmdletBinding()]
param(
    [string]$WorkRoot = "",
    [int]$Port = 5442
)

$ErrorActionPreference = "Continue"

function Find-Toolchain {
    $dir = $PSScriptRoot
    while ($dir) {
        $cand = Join-Path $dir ".toolchain"
        if (Test-Path (Join-Path $cand "pg16\pgsql\bin\postgres.exe")) { return $cand }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "未找到工具链"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PgBin = Join-Path (Find-Toolchain) "pg16\pgsql\bin"
if (-not $WorkRoot) { $WorkRoot = Join-Path $ProjectRoot "data\_pgtest" }
$DataDir = Join-Path $WorkRoot "data"
$Result = [ordered]@{}

function Kill-All {
    Get-Process -Name postgres -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

function Wait-Ready {
    param([int]$P, [int]$TimeoutSec = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        & (Join-Path $PgBin "pg_isready.exe") -h 127.0.0.1 -p $P -q 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

Write-Host "======== PostgreSQL 生命周期能力测试 ========"
Write-Host "  数据目录: $WorkRoot"
Write-Host "  端口    : $Port"
Kill-All
Remove-Item -Recurse -Force $WorkRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

# ---------- 1) 初始化 ----------
$pw = Join-Path $env:TEMP "pgtest_pw.txt"
Set-Content -Path $pw -Value "t" -Encoding ascii -NoNewline
& (Join-Path $PgBin "initdb.exe") -D $DataDir -U postgres --pwfile=$pw `
    --auth-local=trust --auth-host=trust --encoding=UTF8 --locale=C 2>&1 | Out-Null
$Result["1_initdb"] = if (Test-Path (Join-Path $DataDir "PG_VERSION")) { "PASS" } else { "FAIL" }
Add-Content -Path (Join-Path $DataDir "postgresql.conf") `
    -Value "`nport = $Port`nlisten_addresses = '127.0.0.1'`nwal_level = replica`nmax_wal_senders = 10`nlogging_collector = on`nlog_directory = 'log'`n" -Encoding ascii

# ---------- 2) 启动 ----------
$proc = Start-Process -FilePath (Join-Path $PgBin "postgres.exe") `
    -ArgumentList @("-D", $DataDir, "-p", "$Port") -WindowStyle Hidden -PassThru
$Result["2_start"] = if (Wait-Ready -P $Port) { "PASS" } else { "FAIL" }
Write-Host "start -> $($Result['2_start'])  (PID $($proc.Id))"

$env:PGPASSWORD = "t"
$psql = Join-Path $PgBin "psql.exe"

# ---------- 3) 写数据 ----------
& $psql -h 127.0.0.1 -p $Port -U postgres -d postgres `
    -c "CREATE TABLE t(id int, v text); INSERT INTO t SELECT g, 'x'||g FROM generate_series(1,500) g;" 2>&1 | Out-Null
$cnt = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT count(*) FROM t;"
$Result["3_write"] = if ("$cnt".Trim() -eq "500") { "PASS" } else { "FAIL($cnt)" }
Write-Host "write -> $($Result['3_write'])"

# ---------- 4) 读取 ----------
$sum = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT sum(id) FROM t;"
$Result["4_read"] = if ("$sum".Trim() -eq "125250") { "PASS" } else { "FAIL($sum)" }
Write-Host "read -> $($Result['4_read'])"

# ---------- 5) 检查点（超级用户）----------
$cpOut = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -c "CHECKPOINT;" 2>&1
$cpExit = $LASTEXITCODE
$Result["5_checkpoint_superuser"] = if ($cpExit -eq 0) { "PASS" } else { "FAIL($cpOut)" }
Write-Host "checkpoint(superuser) -> $($Result['5_checkpoint_superuser'])"

# ---------- 6) 创建复制角色 + 检查点（复制角色）----------
& $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -c "CREATE ROLE repl WITH REPLICATION LOGIN PASSWORD 't'; GRANT pg_checkpoint TO repl;" 2>&1 | Out-Null
$cp2 = & $psql -h 127.0.0.1 -p $Port -U repl -d postgres -c "CHECKPOINT;" 2>&1
$Result["6_checkpoint_repl"] = if ($LASTEXITCODE -eq 0) { "PASS" } else { "FAIL($cp2)" }
Write-Host "checkpoint(repl) -> $($Result['6_checkpoint_repl'])"

# ---------- 7) pg_basebackup ----------
$baseDir = Join-Path $WorkRoot "base"
& (Join-Path $PgBin "pg_basebackup.exe") -h 127.0.0.1 -p $Port -U repl -D $baseDir -Fp -Xs -P -R 2>&1 | Out-Null
$Result["7_basebackup"] = if (Test-Path (Join-Path $baseDir "PG_VERSION")) { "PASS" } else { "FAIL" }
Write-Host "basebackup -> $($Result['7_basebackup'])"

# ---------- 8) 非正常终止后恢复 ----------
Write-Host "模拟非正常终止（强杀 postmaster）..."
Get-Process -Name postgres -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
Remove-Item (Join-Path $DataDir "postmaster.pid") -Force -ErrorAction SilentlyContinue
$proc2 = Start-Process -FilePath (Join-Path $PgBin "postgres.exe") `
    -ArgumentList @("-D", $DataDir, "-p", "$Port") -WindowStyle Hidden -PassThru
$recovered = Wait-Ready -P $Port -TimeoutSec 60
$Result["8_recovery_after_kill"] = if ($recovered) {
    $c2 = & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -tAc "SELECT count(*) FROM t;" 2>&1
    if ("$c2".Trim() -eq "500") { "PASS(数据完整)" } else { "PARTIAL(可启动但数据=$c2)" }
} else { "FAIL(恢复后卡在启动状态)" }
Write-Host "recovery -> $($Result['8_recovery_after_kill'])"

# ---------- 9) 优雅停止后再启动 ----------
Write-Host "模拟优雅停止（pg_ctl stop -m fast）..."
& (Join-Path $PgBin "pg_ctl.exe") -D $DataDir -m fast stop 2>&1 | Out-Null
Start-Sleep -Seconds 2
$proc3 = Start-Process -FilePath (Join-Path $PgBin "postgres.exe") `
    -ArgumentList @("-D", $DataDir, "-p", "$Port") -WindowStyle Hidden -PassThru
$Result["9_restart_after_graceful_stop"] = if (Wait-Ready -P $Port -TimeoutSec 45) { "PASS" } else { "FAIL" }
Write-Host "graceful restart -> $($Result['9_restart_after_graceful_stop'])"

Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
Kill-All
Remove-Item -Recurse -Force $WorkRoot -ErrorAction SilentlyContinue

Write-Host "`n======== 结论 ========"
$Result.GetEnumerator() | ForEach-Object { Write-Output ("  {0,-32} {1}" -f $_.Key, $_.Value) }
Write-Host "`nPG_TEST_DONE"
