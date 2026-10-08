<#
.SYNOPSIS
    获取本机中间件工具链（免安装 zip 版，无需管理员权限）。

.DESCRIPTION
    本项目不使用 Docker、不注册 Windows 服务、不写系统目录。
    中间件一律下载官方 zip 包解压到本机工具链目录后以普通进程运行。

    下载内容：
      PostgreSQL 16  Windows x64 官方 zip binaries   → <toolchain>\pg16\pgsql
      Redis 5.0.14.1 Windows x64 zip                 → <toolchain>\redis

    工具链默认放在**工作区根目录**的 .toolchain 下（多项目共用，不属于交付物）。
    脚本会自当前位置向上查找既有的 .toolchain；找不到则在工程上两级创建。

.NOTES
    幂等：已存在的下载与解压结果会被复用。
#>
[CmdletBinding()]
param(
    [string]$ToolchainDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ToolchainDir {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    # 向上查找既有 .toolchain
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir ".toolchain"
        if (Test-Path $candidate) { return $candidate }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    # 默认放在工程同级（项目二\.toolchain），使"项目二"成为自包含单元
    return (Join-Path (Split-Path -Parent $ProjectRoot) ".toolchain")
}

$Toolchain = Resolve-ToolchainDir -Explicit $ToolchainDir
$Downloads = Join-Path $Toolchain "downloads"
New-Item -ItemType Directory -Force -Path $Downloads | Out-Null

$PgUrl  = "https://get.enterprisedb.com/postgresql/postgresql-16.6-1-windows-x64-binaries.zip"
$RdsUrl = "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"

# 优先使用系统 curl.exe（Windows 10+ 自带），回退到 Invoke-WebRequest
function Get-RemoteFile {
    param([string]$Url, [string]$OutFile, [string]$Label)
    if ((Test-Path $OutFile) -and -not $Force) {
        Write-Host "  [$Label] 已存在，跳过下载: $([math]::Round((Get-Item $OutFile).Length/1MB,1)) MB"
        return
    }
    Write-Host "  [$Label] 下载中: $Url"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --silent --show-error -o $OutFile $Url
        if ($LASTEXITCODE -ne 0) { throw "[$Label] 下载失败（curl 退出码 $LASTEXITCODE）" }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
    Write-Host "  [$Label] 完成: $([math]::Round((Get-Item $OutFile).Length/1MB,1)) MB"
}

function Expand-ZipOnce {
    param([string]$Zip, [string]$Dest, [string]$Marker, [string]$Label)
    if ((Test-Path $Marker) -and -not $Force) {
        Write-Host "  [$Label] 已解压，跳过"
        return
    }
    Write-Host "  [$Label] 解压中 → $Dest"
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Expand-Archive -Path $Zip -DestinationPath $Dest -Force
}

Write-Host "================================================================"
Write-Host "  获取本机中间件工具链"
Write-Host "  目标目录: $Toolchain"
Write-Host "================================================================"

Write-Host "`n[1/4] PostgreSQL 16 zip"
$pgZip = Join-Path $Downloads "postgresql-16.6-1-windows-x64-binaries.zip"
Get-RemoteFile -Url $PgUrl -OutFile $pgZip -Label "postgres"

Write-Host "`n[2/4] 解压 PostgreSQL"
Expand-ZipOnce -Zip $pgZip -Dest (Join-Path $Toolchain "pg16") `
    -Marker (Join-Path $Toolchain "pg16\pgsql\bin\initdb.exe") -Label "postgres"

Write-Host "`n[3/4] Redis zip"
$rdsZip = Join-Path $Downloads "Redis-x64-5.0.14.1.zip"
Get-RemoteFile -Url $RdsUrl -OutFile $rdsZip -Label "redis"

Write-Host "`n[4/4] 解压 Redis"
Expand-ZipOnce -Zip $rdsZip -Dest (Join-Path $Toolchain "redis") `
    -Marker (Join-Path $Toolchain "redis\redis-server.exe") -Label "redis"

Write-Host "`n================================================================"
Write-Host "  版本确认"
Write-Host "================================================================"
& (Join-Path $Toolchain "pg16\pgsql\bin\postgres.exe") --version
& (Join-Path $Toolchain "redis\redis-server.exe") --version
Write-Host "`n工具链就绪: $Toolchain"
