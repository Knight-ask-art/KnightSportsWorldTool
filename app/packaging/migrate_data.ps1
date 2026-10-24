# Copy a live data/ directory into a new versioned install.
#
# The runtime keeps tokens, the device fingerprint, school rules, routes,
# receipts and daily counters under data/.  A new version directory starts
# empty, so upgrading must carry those files over or the operator is logged
# out and the double-submit guard forgets its receipts.
#
# This script copies (never moves) and takes a backup first.  It refuses to
# overwrite a non-empty target unless -Force is given, and it never prints
# file contents (accounts_emu.json contains the session token).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$From,
    [Parameter(Mandatory = $true)][string]$To,
    [string]$BackupRoot = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $From)) {
    throw "source data directory not found: $From"
}
$From = (Resolve-Path -LiteralPath $From).Path
if (-not $BackupRoot) {
    # packaging/ -> app/ -> repo root -> backup/
    $BackupRoot = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'backup'
}

if (-not (Test-Path -LiteralPath $To)) {
    New-Item -ItemType Directory -Path $To -Force | Out-Null
}
$existing = @(Get-ChildItem -LiteralPath $To -Force -ErrorAction SilentlyContinue)
if ($existing.Count -gt 0 -and -not $Force) {
    Write-Host "target data exists, migration skipped: $To"
    exit 0
}

foreach ($name in @('accounts_emu.json', 'device_profile.json', 'school_rules.json',
                    'semester_goal.json', 'routes.json')) {
    $path = Join-Path $From $name
    if (Test-Path -LiteralPath $path) {
        try {
            Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json | Out-Null
        } catch {
            throw "source file is not valid JSON: $name"
        }
    }
}

$srcVersion = Split-Path (Split-Path $From -Parent) -Leaf
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $BackupRoot ("$srcVersion-data-$stamp")
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -Path (Join-Path $From '*') -Destination $backup -Recurse -Force -ErrorAction SilentlyContinue

$files = @(Get-ChildItem -LiteralPath $From -Recurse -Force -File |
    Where-Object { $_.Name -notmatch '\.(lock|tmp)$' })
foreach ($file in $files) {
    $rel = $file.FullName.Substring($From.Length).TrimStart('\')
    $dest = Join-Path $To $rel
    $parent = Split-Path $dest -Parent
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $file.FullName -Destination $dest -Force
}

$copied = @(Get-ChildItem -LiteralPath $To -Recurse -Force -File)
$mb = (($copied | Measure-Object Length -Sum).Sum / 1MB)
Write-Host ("data migrated: {0} files, {1:N1} MB (backup: {2})" -f `
    $copied.Count, $mb, (Split-Path $backup -Leaf))
exit 0
