<#
.SYNOPSIS
    启动本工程服务并运行工作台验证（使用本工程专属端口）。

.DESCRIPTION
    本工程默认 API 端口为 **8011**、Mock 为 8010、工作台为 8502，
    刻意避开 8000/8501 —— 这两个端口常被机器上其它工程占用。
    端口被占时新进程会绑定失败，而**旧服务仍在响应**，
    表现为"接口全 404 但 /health 正常"，极难定位（实测踩到过）。

    用法：
        pwsh -File scripts/run-workbench-check.ps1
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8011,
    [int]$MockPort = 8010
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$env:VERIFY_API_BASE = "http://127.0.0.1:$ApiPort"

function Test-Port {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

Write-Host "=== 端口占用检查 ==="
foreach ($p in @($ApiPort, $MockPort)) {
    if (Test-Port -Port $p) {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        Write-Host "  [$p] 被占用 PID=$($c.OwningProcess) $($proc.ProcessName) —— 本工程将无法绑定，请先释放"
    } else {
        Write-Host "  [$p] 空闲"
    }
}

Write-Host "`n=== 启动 Mock 与 API ==="
$mock = Start-Process -FilePath $Py `
    -ArgumentList @("-m", "uvicorn", "app.mocks.server:app", "--host", "127.0.0.1", "--port", "$MockPort") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput (Join-Path $ProjectRoot "logs\mock.log") `
    -RedirectStandardError (Join-Path $ProjectRoot "logs\mock.log.err") `
    -WindowStyle Hidden -PassThru

$api = Start-Process -FilePath $Py `
    -ArgumentList @("-m", "app.main") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput (Join-Path $ProjectRoot "logs\api.log") `
    -RedirectStandardError (Join-Path $ProjectRoot "logs\api.log.err") `
    -WindowStyle Hidden -PassThru

Write-Host "  mock PID=$($mock.Id)  api PID=$($api.Id)  （API 以 python -m app.main 启动，确保事件循环策略生效）"

# 就绪判定：用**本工程特有的**接口探测，而不是 /health。
# 因为端口被别的服务占着时 /health 也会返回 200，会造成"服务就绪"的假象。
$ready = $false
$probe = "http://127.0.0.1:$ApiPort/api/v1/metrics/overview"
for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -Uri $probe -Headers @{ "X-Tenant-ID" = "t-001" } -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; Write-Host "  本工程 API 就绪（第 $i 次探测）"; break }
    } catch { }
}
if (-not $ready) {
    Write-Host "  API 未能就绪，日志尾部："
    Get-Content (Join-Path $ProjectRoot "logs\api.log.err") -Tail 15 -ErrorAction SilentlyContinue
}

try {
    Write-Host "`n=== 运行工作台验证 ==="
    & $Py (Join-Path $ProjectRoot "scripts\verify_workbench.py") 2>&1 | Tee-Object -FilePath (Join-Path $ProjectRoot "docs\evidence\D19-workbench\verify-workbench.txt")
} finally {
    Write-Host "`n=== 停止服务 ==="
    foreach ($p in @($api, $mock)) {
        if ($p) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}
