# Update P0 / Release wiring

Status: implemented; real installer build pending on a Windows build machine.

## Keys

- Native integrity manifest: `%USERPROFILE%\.hxp\manifest.key` (existing).
- Update manifest (`latest.json`): `%USERPROFILE%\.hxp\update-signing.pem`.
  Generate once with `python scripts\update_keygen.py`; the private key never
  enters the repository. Rotating it requires rebuilding `hxp-launcher.exe`
  because the public key is linked into the binary.

`scripts\release.ps1` refuses to build without the update key and passes
`-RequireSignature -MinHealthyMirrors 5` to `build.ps1`.

## Artifacts

`build.ps1` now produces, in addition to the existing zip/setup/manifest:

- `dist/好想跑_v<version>/hxp-launcher.exe` — the stable entry point. It is
  linked with `-H=windowsgui` and the update public key.
- `dist/好想跑_v<version>/build_info.json` — `{version, commit, built_at}` for
  the launcher's version comparison and boot-ok check.

The installer shortcuts and `DisplayIcon` point at `hxp-launcher.exe`. The
portable zip also contains it; `好想跑.exe` remains directly runnable.

## Release sequence

1. Bump the version everywhere (`release_check.py --source-only`).
2. `powershell -File scripts\release.ps1 -Version <v>`.
3. `build.ps1` builds the launcher, writes `build_info.json`, packages the zip
   and setup, generates a candidate manifest, probes mirrors, keeps only the
   healthy registry entries, regenerates the final manifest, and signs it.
4. `release_check.py --deep` verifies the packaged launcher, `build_info.json`,
   native integrity manifest, and asset hashes.

The primary `url` may stay behind `gh-proxy.com`; `-RegistryBaseUrl` points the
registry expansion at the official GitHub release base so proxies are not
double-wrapped. `release.ps1` passes both bases automatically.

## Update behavior

- First launch: start the app, then a detached worker downloads with Range
  resume, mirror rotation, and exponential backoff.
- Next launch: `ready` state + matching sha256/size + newer version triggers a
  temp-copy apply helper. It swaps directories, preserves `data/` and
  `Uninstall.exe`, starts the new app, and waits for `boot-ok.json`.
- Failure: reverse the swap, restore `data/`, restart the old app, keep the
  package for inspection, and quarantine the artifact (`rolled_back`). A new
  version or changed sha256 resets the quarantine.
- `--check` / `--dry-run` are read-only. Launcher and helper share
  `Local\Haoxiangpao.Launcher.v1` so double clicks cannot swap concurrently.

## Deferred

- Installer-mode `/S` update instead of the zip directory swap. The zip swap
  preserves `Uninstall.exe`, but the registry's `DisplayVersion` stays stale
  until a full setup is run.
- `min_version` hard block. The manifest field is parsed and reported, but the
  launcher does not yet refuse to start a version below it.
- Bridge release for clients older than the launcher (0.1.2/0.1.3) and an EOL
  schedule.
- A real Windows build + installer install/uninstall/update smoke test on the
  release machine.
