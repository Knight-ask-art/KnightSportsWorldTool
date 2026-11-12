# hxp-launcher (L1)

Standalone Go launcher (`CGO_ENABLED=0`, Go stdlib only) for `好想跑.exe`.

## L1 Scope

- Read `<app dir>/build_info.json` and compare version against remote manifest.
- Fetch release manifest over HTTPS (`--allow-local` enables `http://localhost`, `127.0.0.1`, `::1`, and `file://` for tests).
- Canonicalize manifest JSON and verify Ed25519 signature (`key_id + "\x00" + canonical_json`).
- Fail-open in L1: if public keys are unconfigured or manifest check/verification fails, log the reason and launch `好想跑.exe`.
- Support `--check` (print JSON summary, do not launch), `--no-update`, and `--dry-run`.

## Build & Test

```powershell
cd go/hxp-launcher
$env:CGO_ENABLED = '0'
go test ./...
go vet ./...
.\build.ps1
```

## Deferred to L2

- Release public key injection into `PUBLIC_KEYS`
- Package download, mirror ranking, SHA-256 verification, atomic apply, and rollback / boot-ok gate
- NSIS installer update mode
- Launcher self-update and packaging integration
