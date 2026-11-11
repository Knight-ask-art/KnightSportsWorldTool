# Build cpp/hxp-shield/hxp-shield.exe with the MSVC x64 toolchain.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\cpp\hxp-shield\build.ps1
#
# The source and output paths stay relative to this directory so the batch
# file used to initialise vcvars64 contains ASCII only.

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
if (-not $here) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path -LiteralPath $vcvars)) {
    $vswherePath = $null
    foreach ($cand in @(
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe'))) {
        if (-not $cand) { continue }
        if (Test-Path -LiteralPath $cand) {
            $vswherePath = $cand
            break
        }
    }
    if (-not $vswherePath) {
        $found = Get-Command vswhere.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) {
            $vswherePath = $found.Source
        }
    }
    if ($vswherePath) {
        $install = & $vswherePath -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath
        if ($install) {
            $candidate = Join-Path $install.Trim() 'VC\Auxiliary\Build\vcvars64.bat'
            if (Test-Path -LiteralPath $candidate) {
                $vcvars = $candidate
            }
        }
    }
}
if (-not (Test-Path -LiteralPath $vcvars)) {
    [Console]::Error.WriteLine('vcvars64 not found')
    exit 1
}

$exe = Join-Path $here 'hxp-shield.exe'
$obj = Join-Path $here 'hxp-shield.obj'
Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $obj -Force -ErrorAction SilentlyContinue

$bat = Join-Path $env:TEMP ('hxp-shield-build-' + [guid]::NewGuid().ToString('N') + '.bat')
$lines = @(
    '@echo off',
    ('call "' + $vcvars + '" >nul'),
    'if errorlevel 1 exit /b 1',
    'cl /nologo /O2 /GL /GR- /Gy /GS- /DNDEBUG /EHs- /MT /W3 main.cpp /Fo:hxp-shield.obj /link /LTCG /RELEASE /OPT:REF /OPT:ICF /OUT:hxp-shield.exe advapi32.lib'
)
Set-Content -LiteralPath $bat -Value $lines -Encoding ASCII

Push-Location -LiteralPath $here
try {
    & cmd.exe /d /c $bat
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item -LiteralPath $bat -Force -ErrorAction SilentlyContinue
}

if ($code -ne 0) {
    [Console]::Error.WriteLine('build failed')
    exit 1
}
if (-not (Test-Path -LiteralPath $exe)) {
    [Console]::Error.WriteLine('build failed')
    exit 1
}
Remove-Item -LiteralPath $obj -Force -ErrorAction SilentlyContinue
Write-Host ('built: ' + $exe)
exit 0
