# Build a release with the four mirror URLs in the right place.
#
# The old failure mode: calling build.ps1 with -File and an array argument
# silently bound the last URL to the next parameter.  Splatting a hashtable
# from this wrapper keeps the arrays intact.  -Plan prints the build call
# without compiling anything.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.1
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.1 -Plan
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$RollbackVersion = '0.1.9',
    [string]$Repo = 'Knight-ask-art/KnightSportsWorldTool',
    [switch]$SkipTests,
    [switch]$NoDataMigration,
    [switch]$AllowDirty,
    [switch]$Plan,
    [string]$UpdateSignKey = '',
    [int]$MinHealthyMirrors = 5,
    [switch]$SkipMirrorCheck
)

$ErrorActionPreference = 'Stop'
$ROOT = Split-Path $PSScriptRoot -Parent
$BUILD = Join-Path $ROOT 'app\packaging\build.ps1'

if (-not $UpdateSignKey) {
    $UpdateSignKey = Join-Path $env:USERPROFILE '.hxp\update-signing.pem'
}
if (-not (Test-Path -LiteralPath $UpdateSignKey)) {
    if ($Plan) {
        Write-Host ('WARN: update signing key missing: {0}' -f $UpdateSignKey)
        $UpdateSignKey = '<missing>'
    } else {
        $msg = ('update signing key not found: {0}' +
                "`nCreate one with python scripts\update_keygen.py, or pass " +
                '-UpdateSignKey') -f $UpdateSignKey
        throw $msg
    }
} else {
    $UpdateSignKey = (Resolve-Path -LiteralPath $UpdateSignKey).Path
}
if ($MinHealthyMirrors -lt 0) {
    throw '-MinHealthyMirrors must be >= 0'
}

Push-Location $ROOT
try {
    $dirty = & git status --porcelain
    if ($dirty -and -not $AllowDirty) {
        throw 'working tree is dirty; commit the version bump (or pass -AllowDirty)'
    }
    Write-Host '== preflight: version / BOM / working tree =='
    python scripts\release_check.py --source-only
    if ($LASTEXITCODE -ne 0) { throw 'release_check.py --source-only failed' }

    $base = "https://github.com/$Repo/releases/download/v$Version"
    $rollbackBase = "https://github.com/$Repo/releases/download/v$RollbackVersion"
    $mirrors = @(
        "https://gh-proxy.com/$base",
        "https://ghfast.top/$base",
        "https://gh.llkk.cc/$base",
        $base
    )
    $rollbackMirrors = @(
        "https://gh-proxy.com/$rollbackBase",
        "https://ghfast.top/$rollbackBase",
        "https://gh.llkk.cc/$rollbackBase",
        $rollbackBase
    )

    $buildArgs = @{
        Version                    = $Version
        RollbackVersion            = $RollbackVersion
        DownloadBaseUrl            = $mirrors[0]
        RegistryBaseUrl            = $base
        MirrorBaseUrl              = $mirrors[1..($mirrors.Count - 1)]
        RollbackBaseUrl            = $rollbackMirrors[0]
        RollbackRegistryBaseUrl    = $rollbackBase
        RollbackMirrorBaseUrl      = $rollbackMirrors[1..($rollbackMirrors.Count - 1)]
        SignKey                    = $UpdateSignKey
        RequireSignature           = $true
        MinHealthyMirrors          = $MinHealthyMirrors
    }
    if ($NoDataMigration) { $buildArgs['NoDataMigration'] = $true }
    if ($SkipMirrorCheck) { $buildArgs['SkipMirrorCheck'] = $true }

    if ($Plan) {
        Write-Host '== plan =='
        $buildArgs.GetEnumerator() | Sort-Object Name | ForEach-Object {
            if ($_.Value -is [array]) {
                Write-Host ("{0}:" -f $_.Name)
                $_.Value | ForEach-Object { Write-Host "  $_" }
            } else {
                Write-Host ("{0}: {1}" -f $_.Name, $_.Value)
            }
        }
        return
    }

    if (-not $SkipTests) {
        Write-Host '== preflight: local Windows tests =='
        Push-Location (Join-Path $ROOT 'app')
        try {
            python -m pytest tests -q
            if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
        } finally {
            Pop-Location
        }
    }

    Write-Host '== build =='
    & $BUILD @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "build.ps1 failed with $LASTEXITCODE" }

    Write-Host '== post-build: manifest / assets =='
    python scripts\release_check.py --deep
    if ($LASTEXITCODE -ne 0) { throw 'release_check.py --deep failed' }

    Write-Host ''
    Write-Host 'Next (manual, irreversible):'
    Write-Host "  1. copy github-release/README.md + latest.json to the release branch"
    Write-Host "  2. tag v$Version and push main + tag"
    Write-Host "  3. gh release create v$Version --latest <five assets>"
} finally {
    Pop-Location
}
