# Run one read-only Claude Code review round and save the evidence.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
#     -Scope "issue #12 altitude chart" -Round 1
#
#   powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
#     -Scope "release scripts" -Plan
#
# The reviewer is forced into plan mode and may only read, search, inspect git
# and run the test suite.  It must return file:line, a reproduction command and
# the actual output for every finding, then a PASS/BLOCK verdict.
param(
    [Parameter(Mandatory = $true)]
    [string]$Scope,
    [int]$Round = 0,
    [string]$OutDir = 'output/reviews',
    [string]$Model = '',
    [switch]$WithTests,
    [switch]$FailOnBlock,
    [switch]$Plan
)

$ErrorActionPreference = 'Stop'
$ROOT = Split-Path $PSScriptRoot -Parent
Push-Location $ROOT
try {
    $version = (Select-String -Path 'app\replica\build_info.py' `
        -Pattern "^VERSION\s*=\s*'([^']+)'" -Encoding UTF8).Matches[0].Groups[1].Value
    $folder = Join-Path $ROOT (Join-Path $OutDir $version)
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $name = if ($Round -gt 0) { "round-$Round.md" } else {
        'review-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.md'
    }
    $outFile = Join-Path $folder $name

    $prompt = @"
你是「好想跑」项目的只读审查员。只读，不改代码，不运行会写文件的命令。

审查范围：$Scope

要求：
1. 每条问题必须给 file:line、复现命令、实际输出；没有复现就明确写“静态推断”。
2. 重点找：Windows/Linux 差异、data/ 凭据泄漏、发布清单与版本不一致、回滚/降级、
   测试是否真正覆盖、脚本参数绑定、路径/编码/BOM。
3. 不要泛泛而谈，不要重复已经确认正常的项。
4. 最后必须明确给 PASS 或 BLOCK，并列出阻塞项和非阻塞风险。
"@

    $allowed = @(
        'Read',
        'Glob',
        'Grep',
        'Bash(git diff:*)',
        'Bash(git show:*)',
        'Bash(git status)'
    )
    if ($WithTests) {
        $allowed += @('Bash(python -m pytest:*)', 'Bash(node --check:*)')
    }
    $claudeArgs = @(
        '-p',
        '--permission-mode', 'plan',
        '--allowedTools'
    ) + $allowed
    if ($Model) { $claudeArgs += @('--model', $Model) }
    $claudeArgs += @('--output-format', 'text', $prompt)

    if ($Plan) {
        Write-Host "output: $outFile"
        Write-Host ("command: claude " + ($claudeArgs -join ' '))
        return
    }

    Write-Host "Claude Code review round $Round -> $outFile"
    $text = & claude @claudeArgs 2>&1 | Tee-Object -FilePath $outFile
    $exit = $LASTEXITCODE
    if ($exit -ne 0) {
        throw "claude exited with $exit; see $outFile"
    }
    if ($FailOnBlock -and ($text -join "`n") -match '\bBLOCK\b') {
        throw "review returned BLOCK; see $outFile"
    }
} finally {
    Pop-Location
}
