# Update P0 / Go launcher — L1

Status: implement L1
Branch: `task/update-p0-launcher`
Base: `master` @ `9d7650c`

## Goal

Add an independent, stable `hxp-launcher.exe` written in Go. It must be able
to start the app even when the Python app is broken, and later own
download/verify/apply/rollback. L1 is the **skeleton + manifest verification +
launch** slice only.

## Hard constraints

- Go stdlib only; `CGO_ENABLED=0`; no `import "C"`, no `//export`, no cgo,
  no DLL, no exported symbols.
- No Python runtime dependency. The launcher must work if `好想跑.exe` cannot
  start.
- Production manifest fetch is HTTPS only. `http://localhost` / `127.0.0.1` and
  local files are allowed only behind `--allow-local` for tests.
- Never log tokens, account ids, device ids, or private keys.
- Do not change the Python app, `updater.py`, frontend, packaging, or existing
  release scripts in L1.

## Layout

```
go/hxp-launcher/
  go.mod
  cmd/hxp-launcher/main.go
  internal/manifest/canonical.go
  internal/manifest/manifest.go
  internal/manifest/signature.go
  internal/manifest/*_test.go
  internal/version/version.go
  internal/version/version_test.go
  internal/app/launch.go
  internal/logx/logx.go
  testdata/manifest.json
  testdata/canonical.bin
  testdata/pubkey.b64
  testdata/README.md
  build.ps1
  README.md
```

`go.mod`: module `haoxiangpao/hxp-launcher`, `go 1.22`, no `require`.
`build.ps1`: `CGO_ENABLED=0 go build -trimpath -ldflags="-s -w -H windowsgui"`.

## CLI

```
hxp-launcher.exe [flags]

--app PATH        default: <launcher dir>\好想跑.exe
--data DIR        default: <app dir>\data
--manifest URL    default: same two URLs as updater.py
--check           verify manifest, print JSON summary, do not launch
--no-update       skip manifest check, launch immediately
--dry-run         do everything except StartProcess
--allow-local     allow file:// / http://localhost manifests
--timeout SEC     manifest fetch timeout, default 8
--log PATH        default: <data>\updates\launcher.log
```

Exit codes: `0` launched/checked current, `1` fatal, `2` bad usage.
If manifest check fails, **launch the current app anyway** (fail-open) and log
the reason; do not brick the app in L1.

## Manifest verification

Use the exact contract from `app/replica/application/update_signature.py`:

- payload = manifest copy with top-level `signature` removed
- canonical JSON: `sort_keys=True, separators=(',', ':'), ensure_ascii=False,
  allow_nan=False`, UTF-8
- signed message = `key_id + "\x00" + canonical JSON`
- signature object: `{"alg":"ed25519","key_id":"...","value":"<base64>"}`

`testdata/manifest.json`, `canonical.bin`, `pubkey.b64` are generated from the
Python implementation. Go tests must prove:
- the Go canonical encoder produces exactly `canonical.bin`
- the embedded signature verifies with `pubkey.b64` and `key_id` prefix
- tampering `version` or `assets` fails
- wrong key / unknown key id / bad alg fails

The Go canonical encoder must match Python for:
- sorted object keys at every level
- no whitespace
- UTF-8 Chinese text
- `"` and `\` escaping
- control characters as lowercase `\u00XX`
- integers/booleans/null/arrays
- no HTML escaping (`<`, `>`, `&` stay literal)
- no U+2028/U+2029 escaping

`PUBLIC_KEYS` in Go source is an empty map in L1; release key injection is L2.
`--allow-local` may load a public key file for tests via `--pubkey PATH`.
Production with no key must log `signature not configured` and launch the
current app (fail-open in L1).

## Version

Read `<app dir>/build_info.json`:

```json
{"version": "0.2.2", "commit": "...", "built_at": "..."}
```

If missing, log and launch without an update check. Version compare is the same
as `updater.parse_version`/`is_newer`: optional `v`, dot-separated integers,
pad with zeros, no prerelease semantics.

## Tests

Use `testing`, `httptest`, `t.TempDir`; no real network.

- canonical/vector/signature tests above
- version compare: `0.1.9 < 0.2.0`, `v0.2.2 == 0.2.2`, malformed -> no update
- `build_info.json` reading
- `--check` against a local httptest manifest + test public key
- `--dry-run` does not start a process
- missing manifest / timeout -> exit 0 and would launch (fail-open)
- bad signature -> fail-open launch, log contains no key material
- no `import "C"` / cgo in source (simple `go list`/grep test not required;
  build command with `CGO_ENABLED=0` is the acceptance)

## Acceptance

```powershell
cd go/hxp-launcher
$env:CGO_ENABLED='0'
go test ./...
go vet ./...
go build -trimpath -ldflags="-s -w" -o hxp-launcher.exe ./cmd/hxp-launcher
```

Report: files created, test output, assumptions, and what is deferred to L2
(download/apply/rollback/boot-ok, installer mode, self-update, packaging).
Do not commit.
