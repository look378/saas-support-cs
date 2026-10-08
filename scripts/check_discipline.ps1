<#
.SYNOPSIS
    交付纪律自检：扫描交付物中的禁用表述与敏感信息。

.DESCRIPTION
    扫描交付目录（排除虚拟环境、缓存、运行期数据），检查：

    1. 外源标识：外部参考项目名及其变体
    2. 派生性动作词：改造 / 二次开发 / 魔改 / 移植 / 剥离 / 迁移（指代来源时）
    3. 指向来源的关系词：底座 / 上游项目 / 原项目 / 原框架
    4. 密钥泄漏：真实 API Key / Token 形态的字符串出现在交付物中

    退出码 0 表示未发现问题。

.NOTES
    「迁移」在数据库语境（Alembic 迁移、schema 迁移）是正常用词，
    因此对数据库上下文做白名单放行，避免误报。
#>
[CmdletBinding()]
param(
    [string]$Target = "",
    [switch]$Detailed
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Target) { $Target = $ProjectRoot }

Write-Host "================================================================"
Write-Host "  交付纪律自检"
Write-Host "  扫描目录：$Target"
Write-Host "================================================================"

# 排除：虚拟环境、缓存、工具链、运行期数据、字节码
$exclude = @(
    '\\\.venv\\', '\\__pycache__\\', '\\\.pytest_cache\\', '\\\.toolchain\\',
    '\\data\\rag\\', '\\data\\pg\\', '\\data\\redis\\', '\\data\\run\\',
    '\\logs\\', '\\\.probe\\', '\\node_modules\\',
    # 测试与环境产生的临时目录：不属于交付物，且受限环境下可能连列举都被拒
    # （曾导致本自检直接中断，属于"工具被环境噪声干扰"而非真实违规）
    '\\\.pytest_tmp\\', '\\\.pytest_work\\', '\\\.tmp\\', '\\\.ruff_cache\\',
    # 本自检脚本自身：它必须包含这些词才能做检查，不应被自己命中
    '\\scripts\\check_discipline\.ps1$'
)

# 目录级预剪枝：对**连列举都被拒绝**的目录直接跳过，
# 否则 Get-ChildItem -Recurse 会抛错中断整次自检。
$files = Get-ChildItem -Path $Target -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '^(\.venv|\.pytest_tmp|\.pytest_work|\.tmp|\.ruff_cache|\.toolchain|node_modules|logs|data)$' } |
    ForEach-Object {
        Get-ChildItem -Path $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue
    }
$files += Get-ChildItem -Path $Target -File -Force -ErrorAction SilentlyContinue
$files = $files | Where-Object {
    $p = $_.FullName
    $skip = $false
    foreach ($e in $exclude) { if ($p -match $e) { $skip = $true; break } }
    if ($skip) { return $false }
    $_.Extension -in @('.py', '.md', '.txt', '.ps1', '.html', '.yml', '.yaml', '.ini', '.example', '.json', '.cfg', '.toml')
}

Write-Host "  待扫描文件数：$($files.Count)"

# 规则说明区白名单：文档中"声明规则本身"的段落必然要引用这些词，
# 这属于说明而非违规。命中行若出现以下标记，视为说明性引用，跳过。
$ruleContext = @(
    '不得出现', '不得使用', '禁用词', '禁止使用', '应写', '不这样写',
    '表述纪律', '禁用表述', '替代表述'
)

# ---- 1) 禁用表述 ----
$banned = @{
    '外部项目名' = @('SKILL-kefu', 'skill-kefu', 'liangdabiao', '星辰科技', 'StarPods', 'StarWatch', 'StarBox')
    '派生性动作' = @('二次开发', '魔改', '移植', '剥离', '改造')
    '来源关系词' = @('底座', '上游项目', '原项目', '原框架')
}
# 正常语境放行（数据库迁移等）
$whitelist = @('数据库迁移', 'schema 迁移', '结构迁移', '迁移脚本', 'Alembic', 'alembic', 'migrate', 'Migration')

$hits = @()
foreach ($f in $files) {
    $content = Get-Content $f.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $content) { continue }
    foreach ($cat in $banned.Keys) {
        foreach ($word in $banned[$cat]) {
            if ($content -notlike "*$word*") { continue }
            $lines = ($content -split "`n") | Where-Object { $_ -like "*$word*" }
            $real = @($lines | Where-Object {
                $ln = $_
                $ok = $false
                foreach ($w in $whitelist) { if ($ln -like "*$w*") { $ok = $true; break } }
                if (-not $ok) {
                    foreach ($r in $ruleContext) { if ($ln -like "*$r*") { $ok = $true; break } }
                }
                -not $ok
            })
            if ($real.Count -gt 0) {
                $sample = ([string]$real[0]).Trim() -replace '\s+', ' '
                if ($sample.Length -gt 70) { $sample = $sample.Substring(0, 70) + '…' }
                $hits += [pscustomobject]@{
                    类别 = $cat
                    词 = $word
                    文件 = $f.FullName.Replace($Target, '.')
                    命中 = $real.Count
                    示例 = $sample
                }
            }
        }
    }
}

# ---- 2) 密钥泄漏（真实 Key/Token 形态）----
$secretPatterns = @(
    @{ 名称 = '疑似 API Key'; 正则 = '(?i)\bsk-[a-z0-9]{20,}\b' },
    @{ 名称 = '疑似长 Token'; 正则 = '\b[a-f0-9]{64}\b' },
    @{ 名称 = '真实地址泄漏'; 正则 = '(?i)(zillizcloud|bigmodel\.cn/api/paas)' }
)
$secretHits = @()
$placeholder = @('your-key-here', 'your-api-key', 'sk-your', 'replace-with', 'change-me', 'xxx', '****')
foreach ($f in $files) {
    $content = Get-Content $f.FullName -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $content) { continue }
    foreach ($sp in $secretPatterns) {
        foreach ($m in [regex]::Matches($content, $sp.正则)) {
            $val = $m.Value
            $isPlaceholder = $false
            foreach ($ph in $placeholder) { if ($val -like "*$ph*") { $isPlaceholder = $true; break } }
            if (-not $isPlaceholder) {
                $secretHits += [pscustomobject]@{ 类型 = $sp.名称; 文件 = $f.FullName.Replace($Target, '.'); 片段 = $val.Substring(0, [Math]::Min(16, $val.Length)) + '…' }
            }
        }
    }
}

# ---- 输出 ----
Write-Host "`n[1] 禁用表述检查"
if ($hits.Count -eq 0) {
    Write-Host "  [OK ] 未发现禁用表述"
} else {
    $hits | Sort-Object 文件, 词 | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host "  [NG ] 共 $($hits.Count) 处"
}

Write-Host "`n[2] 密钥泄漏检查"
if ($secretHits.Count -eq 0) {
    Write-Host "  [OK ] 未发现真实密钥形态的字符串"
} else {
    $secretHits | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host "  [NG ] 共 $($secretHits.Count) 处，请立即移除并轮换"
}

Write-Host "`n================================================================"
if ($hits.Count -eq 0 -and $secretHits.Count -eq 0) {
    Write-Host "  交付纪律自检通过"
    Write-Host "================================================================"
    exit 0
} else {
    Write-Host "  交付纪律自检未通过，请按上表修正"
    Write-Host "================================================================"
    exit 1
}
