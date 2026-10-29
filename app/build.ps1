# Compatibility wrapper: the real build script lives in packaging/.
param(
    [string]$Version = '',
    [string]$MigrateDataFrom = '',
    [switch]$NoDataMigration,
    [string]$DownloadBaseUrl = '',
    [string]$Channel = 'stable',
    [string]$RollbackVersion = '0.1.4',
    [string]$RollbackBaseUrl = '',
    [ValidateSet('nuitka', 'pyinstaller')]
    [string]$Backend = 'nuitka'
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'packaging\build.ps1'
$buildArgs = @{}
if ($Version) { $buildArgs.Version = $Version }
if ($MigrateDataFrom) { $buildArgs.MigrateDataFrom = $MigrateDataFrom }
if ($NoDataMigration) { $buildArgs.NoDataMigration = $true }
if ($DownloadBaseUrl) { $buildArgs.DownloadBaseUrl = $DownloadBaseUrl }
if ($Channel) { $buildArgs.Channel = $Channel }
$buildArgs.RollbackVersion = $RollbackVersion
if ($RollbackBaseUrl) { $buildArgs.RollbackBaseUrl = $RollbackBaseUrl }
$buildArgs.Backend = $Backend
& $script @buildArgs
exit $LASTEXITCODE
