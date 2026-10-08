<#
.SYNOPSIS
    重启本工程的全部服务（先释放端口，再启动并保持运行）。

.DESCRIPTION
    等价于：停止占用本项目端口的进程 → 调用 scripts/serve.ps1 重新起服。
    端口的进程清理逻辑放在脚本里，避免在命令行里写循环时被解析歧义干扰。
#>
[CmdletBinding()]
param(
    [int[]]$Ports = @(8011, 8010, 8502),
    [switch]$WithDashboard
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "=== 释放端口 ==="
foreach ($p in $Ports) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Write-Host "  释放 $p （PID $($conn.OwningProcess)）"
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "  $p 空闲"
    }
}
Start-Sleep -Seconds 2

Write-Host ""
& (Join-Path $PSScriptRoot "serve.ps1") -WithDashboard:$WithDashboard
