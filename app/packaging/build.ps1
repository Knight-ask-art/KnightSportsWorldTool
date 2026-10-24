# One-click packaging for the pure-Python build.
# Usage:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#         powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 0.1.1
# Output: dist/好想跑_v<version>/好想跑.exe  (+ _internal)
#         dist/好想跑_v<version>_win64.zip
#
# The output is versioned on purpose.  The old `dist/好想跑/` belongs to an
# existing install and may contain live data; this script never deletes it.
# If the versioned target already exists, its `data/` folder is preserved
# across a rebuild (the archive itself is built before that folder is
# restored, so live data never ends up in the zip).
#
# No JRE / unidbg runtime is copied: login and the submit path are pure Python.
param(
    [string]$Version = ''
)
$ErrorActionPreference = 'Stop'
$ROOT = Split-Path $PSScriptRoot -Parent
$DIST = Join-Path $ROOT 'dist'
$WORK = Join-Path $ROOT 'build_tmp'
$STAGE = Join-Path $ROOT 'build_stage'
$BUILT = Join-Path $STAGE '好想跑'
$EXE = Join-Path $BUILT '好想跑.exe'

if (-not $Version) {
    $api = Join-Path $ROOT 'replica\adapters\bridge_api.py'
    $match = Select-String -Path $api -Pattern "^APP_VERSION\s*=\s*'([^']+)'" -Encoding UTF8 |
        Select-Object -First 1
    if (-not $match) { throw "Cannot read APP_VERSION from $api" }
    $Version = $match.Matches[0].Groups[1].Value
}
$NAME = '好想跑_v' + $Version
$TARGET = Join-Path $DIST $NAME
$ZIP = Join-Path $DIST ($NAME + '_win64.zip')
Write-Host ('== 1/5 PyInstaller (v{0}) ==' -f $Version)
if (Test-Path $STAGE) { Remove-Item $STAGE -Recurse -Force }
python -m PyInstaller (Join-Path $PSScriptRoot 'build.spec') --noconfirm --clean `
    --distpath $STAGE --workpath $WORK
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $EXE)) { throw "PyInstaller did not produce $EXE" }

Write-Host '== 2/5 Frozen import check =='
$check = Start-Process -FilePath $EXE -ArgumentList '--check' -Wait -PassThru
if ($check.ExitCode -ne 0) {
    throw "Frozen import check failed with exit code $($check.ExitCode)"
}

Write-Host '== 3/5 Frozen HTTP smoke test =='
$listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$proc = Start-Process -FilePath $EXE -ArgumentList "--no-window --port $port" -PassThru
try {
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/"
            if ($r.StatusCode -eq 200 -and $r.Content -match 'id="loginGate"') {
                $ok = $true
                break
            }
        } catch { }
    }
    if (-not $ok) { throw 'Frozen HTTP smoke test failed (no login page)' }
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}

Write-Host '== 4/5 Package =='
# The smoke test can create data/mock_rsa.*; test artifacts never ship.
$stageData = Join-Path $BUILT 'data'
if (Test-Path $stageData) { Remove-Item $stageData -Recurse -Force }
Copy-Item (Join-Path $PSScriptRoot 'USAGE.txt') (Join-Path $BUILT '使用说明.txt') -Force
if (Test-Path $ZIP) { Remove-Item $ZIP -Force }
Compress-Archive -Path (Join-Path $BUILT '*') -DestinationPath $ZIP -CompressionLevel Optimal

# Never ship a live data/ directory (tokens, receipts, device_profile.json).
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipCheck = [System.IO.Compression.ZipFile]::OpenRead($ZIP)
try {
    $entries = @($zipCheck.Entries | ForEach-Object { $_.FullName })
    if ($entries | Where-Object { $_ -like 'data/*' -or $_ -like 'data\*' }) {
        throw 'Refusing to ship: zip contains a data/ directory'
    }
    if (-not ($entries -contains '好想跑.exe')) {
        throw 'Refusing to ship: zip has no 好想跑.exe'
    }
} finally {
    $zipCheck.Dispose()
}
$HASH = Join-Path $DIST ($NAME + '_win64.sha256')
if (Test-Path $HASH) { Remove-Item $HASH -Force }
$hashValue = (Get-FileHash $ZIP -Algorithm SHA256).Hash
($hashValue + '  ' + (Split-Path $ZIP -Leaf)) |
    Set-Content -Path $HASH -Encoding ASCII

Write-Host '== 5/5 Install (preserve live data) =='
# If rebuilding the same version, move its data aside before replacing the
# binaries, then put it back.  The archive above was built without it.
$backup = Join-Path $ROOT ('build_target_backup_' + [Guid]::NewGuid().ToString('N'))
$hasBackup = $false
if (Test-Path $TARGET) {
    $oldData = Join-Path $TARGET 'data'
    if (Test-Path $oldData) {
        New-Item -ItemType Directory -Path $backup | Out-Null
        Move-Item $oldData (Join-Path $backup 'data')
        $hasBackup = $true
    }
    Remove-Item $TARGET -Recurse -Force
}
Copy-Item -Path $BUILT -Destination $TARGET -Recurse -Force
if ($hasBackup) {
    Move-Item (Join-Path $backup 'data') (Join-Path $TARGET 'data')
    Remove-Item $backup -Recurse -Force
}
if (Test-Path $STAGE) { Remove-Item $STAGE -Recurse -Force }
if (Test-Path $WORK) { Remove-Item $WORK -Recurse -Force }

$size = (Get-ChildItem $TARGET -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ('完成: {0}' -f (Join-Path $TARGET '好想跑.exe'))
Write-Host ('压缩包: {0}' -f $ZIP)
Write-Host ('总体积: {0:N1} MB' -f $size)
Write-Host '旧 dist/好想跑/ 未改动；本次版本目录的 data/ 已保留。'
