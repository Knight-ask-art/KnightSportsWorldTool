# Update P0 / L2B — Atomic apply + boot-ok + rollback

Status: implemented and reviewed (round-3 blocking fixes included)
Branch: `task/update-p0-l2b`
Base: `task/update-p0-integration` @ `ba82594`

## Goal

Apply a verified zip from L2A before launching the app, with `app.old` kept
until the new app writes a boot-ok marker. If boot-ok never appears, restore
the old app automatically. No installer mode, no self-update, no packaging yet.

## Python: boot-ok hook

In `app/replica/adapters/http_server.py`, after `srv.serve_forever()` thread
starts in `serve()`:

- if `JHP_BOOT_OK_PATH` is empty, do nothing (source/tests unaffected)
- create parent dir; atomically write UTF-8 JSON:
  `{"version": build_info.VERSION, "pid": os.getpid(), "at": <epoch_ms>}`
- never write tokens/account data; a write failure must not break startup
- add `app/tests/test_boot_ok.py`:
  - env set -> file appears with the current VERSION
  - env unset -> no file
  - unwritable path -> `serve()` still succeeds

## Go: apply helper

New `go/hxp-launcher/internal/apply`:

- `ExtractZip(package, staging)`:
  - reject more than 20k entries or >2 GiB uncompressed
  - reject absolute paths, `..`, and top-level `data/`
  - require `好想跑.exe` in staging
- `Apply(opts, deps)`:
  - extract to `<data>/updates/staging-<version>`
  - copy staging to `<appDir>.new`, excluding `data/`
  - move `<appDir>/data` to `<appDir>.new/data` if it exists
  - rename `<appDir>` -> `<appDir>.old`
  - rename `<appDir>.new` -> `<appDir>`
  - start `<appDir>/好想跑.exe` with env
    `JHP_BOOT_OK_PATH=<data>/updates/boot-ok.json`
  - wait up to `bootTimeout` for boot-ok with matching version
  - success: delete `.old`, write state `applied`
  - failure: kill app, remove new dir, restore `.old`, start old app,
    write state `rolled_back`; never leave the app directory missing
- inject `deps` for tests: copyDir, rename, startApp, waitBoot, killApp,
  removeAll, now.

## CLI

```
--apply-helper
--apply-package PATH
--apply-version VERSION
--apply-wait-pid PID
--apply-boot-timeout SEC (default 30)
```

- `--apply-helper` runs the helper and exits; no manifest check.
- normal launcher, before `launchApp`:
  - load `data/updates/download.state.json`
  - if `phase == "ready"`, package exists, version newer than
    `build_info.json`, copy the launcher exe to
    `%TEMP%/hxp-apply-<pid>.exe`, spawn it detached with the apply args and
    `--apply-wait-pid <current launcher pid>`, then exit 0. The helper waits
    for the launcher to exit, applies, and starts the app.
  - if helper spawn fails: log fail-open and launch the current app.
  - `--check` / `--dry-run` / `--no-update` never apply.
- helper must run from the temp copy so it is not inside a directory being
  renamed. Use the existing detached sysproc helper.

## State

Extend `worker.State` with `PhaseApplying`, `PhaseApplied`, `PhaseRolledBack`.
- ready -> applying before swap
- success -> applied, `package` kept or cleared (choose one; document)
- failure -> rolled_back, `last_error` sanitized, package kept for inspection
- never write tokens/URLs to state.

## Tests

Go:
- zip-slip / top-level `data/` / missing exe rejected
- apply success with fake deps: data preserved, old removed, boot-ok path set
- boot-ok failure: old restored, app dir exists, state rolled_back
- `maybeApply` spawns helper only for ready+newer; no spawn for
  check/dry-run/no-update
- helper args include package/version/wait-pid/boot-timeout
- no network in tests

Python:
- `test_boot_ok.py` above

## Acceptance

```powershell
cd go/hxp-launcher
$env:CGO_ENABLED='0'
go test ./...
go vet ./...
go build -trimpath -ldflags="-s -w" -o hxp-launcher.exe ./cmd/hxp-launcher

cd ..\..\app
python -m pytest tests/test_boot_ok.py -q
python -m pytest tests/test_update_mirrors.py tests/test_update_signature.py tests/test_updater.py -q
```

Report files changed, test output, assumptions, and deferred L3 work.
Do not commit.
