$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptDir
try {
    $env:CGO_ENABLED = '0'
    go build -trimpath -ldflags="-s -w -H windowsgui" -o hxp-launcher.exe ./cmd/hxp-launcher
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
