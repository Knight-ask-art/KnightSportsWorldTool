# hxp-launcher (L1 + L2A)

Standalone Go launcher (`CGO_ENABLED=0`, Go stdlib only) for `好想跑.exe`.

## L1 + L2A Scope

- Launch `好想跑.exe` first in normal mode (`--no-update` skips check; `--check` prints JSON summary without launching; `--dry-run` skips `StartProcess` and worker spawn).
- Read `<app dir>/build_info.json`, fetch release manifest over HTTPS (`--allow-local` enables `http://localhost`, `127.0.0.1`, `::1`, and `file://` for tests), canonicalize JSON, and verify Ed25519 signature (`key_id + "\x00" + canonical_json`).
- Fail-open launch if keys are unconfigured or manifest check/verification fails.
- When a newer version is verified/available in normal mode, spawn a detached `--update-worker` process (`--parent-pid` tracks the running app).
- `--update-worker` downloads `assets.windows-x64` (zip, max 400 MB) across mirrors (`last_good_index`, official, then remaining mirrors), resumes `.part` via `Range` / HTTP 206 (restarting from zero on HTTP 200), verifies `PK\x03\x04` magic + size + SHA-256, and persists `<data>/updates/download.state.json` atomically with exponential backoff (`15s`–`1h`, ±20% jitter). `--once` runs a single pass and exits `0` on ready or `1` on failure.

## Build & Test

```powershell
cd go/hxp-launcher
$env:CGO_ENABLED = '0'
go test ./...
go vet ./...
.\build.ps1
```

## Deferred to L2B

- Release public key injection into `PUBLIC_KEYS`
- Atomic apply (`app.old` / `app.new`), boot-ok gate, and automatic rollback
- NSIS installer update mode
- Launcher self-update and Python/packaging integration
