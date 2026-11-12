# Update P0 / L2A — Background download worker + persistent retry

Status: implement L2A
Branch: `task/update-p0-l2a`
Base: `task/update-p0-integration` @ `ccce130`

## Goal

The launcher must open the app first, then a detached worker downloads the
next signed zip in the background. Failures retry with exponential backoff and
survive restarts. L2A does **not** apply/replace/rollback; that is L2B.

## Files

- MODIFY `go/hxp-launcher/cmd/hxp-launcher/main.go`
- NEW `go/hxp-launcher/internal/worker/worker.go`
- NEW `go/hxp-launcher/internal/worker/state.go`
- NEW `go/hxp-launcher/internal/worker/download.go`
- NEW `go/hxp-launcher/internal/worker/*_test.go`
- MODIFY `go/hxp-launcher/README.md`
- MODIFY `docs/plans/update-p0/LAUNCHER.md` (status)

## CLI

```
hxp-launcher.exe --update-worker --app PATH --data DIR \
  --manifest URL[,URL] --timeout SEC --log PATH \
  [--once] [--parent-pid PID] [--pubkey PATH --allow-local]
```

- `--update-worker`: run worker only; never launch the app.
- `--once`: one full pass over mirrors; no sleep; exit 0 if verified, 1 if not.
- normal mode: retry with backoff until verified or parent exits.
- `--parent-pid`: if >0, check every 2 s while sleeping; stop when the process
  disappears (state persists). Tests pass 0.
- `--pubkey` remains `--allow-local` only.
- normal launcher: after starting the app, if a newer version is available and
  keys are configured, spawn a detached worker (same exe, `--update-worker`)
  and exit. `--check` must not spawn. If spawn fails, log and continue.

## State

`<data>/updates/download.state.json`, atomic write (`.tmp` + rename):

```json
{
  "schema": 1,
  "version": "0.3.0",
  "file": "haoxiangpao_v0.3.0_win64.zip",
  "sha256": "…",
  "size": 123,
  "urls_hash": "…",
  "last_good_index": 0,
  "next_index": 0,
  "attempts": 0,
  "next_retry_at": 0,
  "phase": "downloading",
  "package": "",
  "last_error": "",
  "updated_at": 0
}
```

- Do **not** store full mirror URLs or query tokens in state. Store a SHA-256
  of the manifest URL list (`urls_hash`) and indices; re-read the manifest each
  run.
- If version/sha/urls_hash changes, reset indices/attempts.
- `phase`: `downloading` -> `ready` (package set) or stays `downloading` with
  `next_retry_at`.
- On success `phase=ready`, `package=<data>/updates/<file>`, `last_error=""`.

## Download

- Select `assets.windows-x64` (zip). L2A does not apply installers.
- Require `url`, `urls`, `sha256`, `size > 0`; max size 400 MB.
- Try mirrors starting at `last_good_index`, then official, then the rest.
- On each mirror:
  - resume `.part` if it belongs to the same version/sha and size;
  - send `Range: bytes=<part>-`; if server answers 200, restart from zero;
  - reject > max size; stream to `.part`;
  - verify size and SHA-256; on success rename to final package, set
    `last_good_index` to that mirror index, `phase=ready`;
  - on failure keep `.part` only for resume, sanitize the error, try next.
- After all mirrors fail: increment attempts, set `next_retry_at` using:
  `15s, 30s, 1m, 2m, 5m, 10m, 30m, 1h` (cap), ±20% jitter.
- Do not log query strings/fragments/userinfo. State `last_error` must be
  sanitized too.
- Verify zip magic `PK\x03\x04` before `phase=ready`.

## Backoff loop

- Normal worker: loop fetch manifest -> download -> on failure sleep until
  `next_retry_at` (checking parent each 2 s) -> repeat.
- `--once`: one pass only.
- If manifest fetch/verify fails, treat as a retryable failure (backoff), but
  do not write a fake `ready` state.
- If parent disappears, exit without deleting `.part`/state.

## Tests

Use `httptest`, `t.TempDir`, local signed manifest fixtures; no real network.

- successful download -> state ready, sha/size verified, package path exists
- mirror fallback: first URL 500, second 200
- resume: server returns 206 for the remaining bytes; final sha matches
- checksum mismatch -> failure, `.part` kept, next mirror tried
- all mirrors fail -> attempts increment, `next_retry_at` in the future
- `--once` exits 1 on failure; 0 on success
- state survives a new worker with same version/sha and resumes
- changed urls_hash resets index/attempts
- logs/state never contain `token=`, query tokens, or userinfo
- normal launcher `--dry-run`/`--check` never spawns a worker
- spawn helper test: worker command includes `--update-worker` (no real process)

## Acceptance

```powershell
cd go/hxp-launcher
$env:CGO_ENABLED='0'
go test ./...
go vet ./...
go build -trimpath -ldflags="-s -w" -o hxp-launcher.exe ./cmd/hxp-launcher
```

## Deferred to L2B

- apply zip atomically with `app.old` / `app.new`
- boot-ok file + app startup hook
- automatic rollback and `.old` cleanup
- installer mode and launcher self-update
- packaging/build integration and release key injection
