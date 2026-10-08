<#
.SYNOPSIS
    为本工程的 PostgreSQL 建立 ASCII 目录联接（绕开 PG 的已知缺陷）。

.DESCRIPTION
    为什么需要：
      PostgreSQL 存在已知缺陷——initdb 及部分工具在"**二进制路径含非 ASCII 字符**"时
      会失败：bootstrap 阶段报 `invalid byte sequence for encoding "UTF8"`。
      见 pgsql-bugs #16926（"initdb fails on Windows when binary path contains
      certain non-ASCII characters"）。

      本工程路径为 `...\项目二\saas-support-cs`（含中文），工具链又放在
      `项目二\.toolchain`，因此直接调用 initdb 必然失败。

    解决办法（不移动任何真实文件）：
      建立两个 **目录联接（junction）**，给 PostgreSQL 一个纯 ASCII 的视角：

        D:\pgtool  →  <工程>\..\.toolchain\pg16\pgsql     （二进制）
        D:\pgdata  →  <工程>\data\pg                      （数据目录根）

      真实文件仍全部留在工程内，"项目二 可整体拷贝"这一性质不受影响；
      只要在目标机器上重跑本脚本重建联接即可（serve.ps1 会自动探测并使用）。

    权限说明：
      创建联接需要管理员权限（或已开启开发者模式的 Windows）。
      本脚本不自行提权，请在**管理员 PowerShell** 中执行。

.NOTES
    幂等：已存在且指向正确的联接会被保留；指向错误则重建。
    删除联接请用 `cmd /c rmdir D:\pgtool`，**不要**用 Remove-Item -Recurse
    ——后者会穿透联接删掉真实目录（本项目实测踩到，损失了演示数据）。

.EXAMPLE
    # 在管理员 PowerShell 中：
    pwsh -File scripts/setup-ascii-links.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectTwo = Split-Path -Parent $ProjectRoot

$links = @(
    @{ Link = "D:\pgtool"; Target = (Join-Path $ProjectTwo ".toolchain\pg16\pgsql") },
    @{ Link = "D:\pgdata"; Target = (Join-Path $ProjectRoot "data\pg") }
)

Write-Host "================================================================"
Write-Host "  建立 ASCII 目录联接（PostgreSQL 非 ASCII 路径绕行）"
Write-Host "================================================================"

# 管理员检查
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    Write-Host "  警告：当前不是管理员，创建联接可能失败。" -ForegroundColor Yellow
    Write-Host "        请用管理员 PowerShell 重新执行本脚本。" -ForegroundColor Yellow
    Write-Host ""
}

function Test-IsJunction {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $false }
    $item = Get-Item $Path -Force
    return [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

foreach ($entry in $links) {
    $link = $entry.Link
    $target = $entry.Target

    if (-not (Test-Path $target)) {
        throw "目标不存在：$target（请先执行 scripts/fetch-toolchain.ps1）"
    }

    if (Test-IsJunction $link) {
        $current = (Get-Item $link -Force).Target
        if ($current -and ($current | Select-Object -First 1) -eq $target) {
            Write-Host "  [OK ] $link 已存在且指向正确"
            continue
        }
        Write-Host "  [重建] $link 指向 $current，应为 $target"
        # 用 rmdir 删除联接本身（Remove-Item -Recurse 会穿透删掉真实目录）
        & cmd /c rmdir "$link" | Out-Null
    } elseif (Test-Path $link) {
        throw "$link 已存在且不是联接，请人工处理（避免误删真实目录）"
    }

    & cmd /c mklink /J "$link" "$target" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "创建联接失败：$link → $target" }
    Write-Host "  [新建] $link → $target"
}

Write-Host ""
Write-Host "  完成。scripts/serve.ps1 会自动探测并使用这两个联接。"
