# Compatibility wrapper: the real build script lives in packaging/.
param(
    [string]$Version = ''
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'packaging\build.ps1'
if ($Version) {
    & $script -Version $Version
} else {
    & $script
}
exit $LASTEXITCODE
