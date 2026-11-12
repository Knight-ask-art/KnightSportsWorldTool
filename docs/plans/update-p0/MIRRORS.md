# Update P0 / M1 — Mirror registry + release health check

Status: implement M1
Branch: `task/update-p0-mirrors`
Base: `task/update-p0-integration` @ `8c5b650`

## Goal

Put 20-30 candidate download mirrors into the **signed** `latest.json`
`assets.*.urls[]`, but never publish dead links. The client already tries every
URL in `urls`; M1 only changes manifest generation and adds a release-time
health check. Download retry/backoff is L2 (Go launcher), not M1.

## Files

- NEW `app/packaging/update_mirrors.json`
- MODIFY `app/packaging/make_latest.py`
- NEW `app/packaging/check_update_mirrors.py`
- NEW `app/tests/test_update_mirrors.py`
- MODIFY `docs/update.md`

## Registry format

```json
{
  "schema": 1,
  "mirrors": [
    {"name": "official", "template": "{base}", "priority": 0, "enabled": true},
    {"name": "gh-proxy", "template": "https://gh-proxy.com/{base}", "priority": 10, "enabled": true},
    {"name": "ghfast", "template": "https://ghfast.top/{base}", "priority": 20, "enabled": true}
  ]
}
```

- `{base}` is the asset directory (`https://github.com/.../releases/download/v0.3.0`).
- `priority` lower first; official must stay first.
- `enabled: false` entries are kept as candidates but not emitted.
- Every non-official template must produce an HTTPS URL. Reject `http://`,
  `file://`, and templates without `{base}`.
- If `--base-url` is relative/empty, registry mirrors are skipped with a
  warning; the manifest keeps a single relative `url` for old clients.

## `make_latest.py`

- default registry: `app/packaging/update_mirrors.json`
- `--mirror-registry PATH` (override), `--no-mirror-registry`
- keep `--mirror-base-url` as extra manual mirrors (appended after registry)
- expand enabled registry entries for:
  - `assets.windows-x64` and `assets.windows-x64-setup`
  - `rollback` (using the rollback base URL)
- `url` remains the official/first URL; `urls[]` only when >1
- dedupe preserving order; cap at 40 URLs per asset
- `--require-mirrors N` (default 0): exit 2 when fewer than N enabled
  non-official URLs can be generated (or base URL is relative)
- never log private keys; never read credentials

## `check_update_mirrors.py`

Probe the actual asset URLs in a generated manifest before release:

```powershell
python packaging/check_update_mirrors.py --manifest dist/latest.json \
  --min-healthy 5 --out dist/mirror-health.json
```

- one ranged GET per URL (`Range: bytes=0-65535`), 6 s budget, max 8 workers
- verify first bytes: zip `PK\x03\x04`, exe `MZ`
- output JSON: `{url: {ok, status, ttfb_ms, speed_bps, error}}`
- exit 0 when healthy >= `--min-healthy`, else 1
- optional `--write-registry PATH`: write a filtered registry containing only
  healthy entries for the next release
- no writes unless `--out`/`--write-registry` is supplied
- tests use a local HTTP server or monkeypatched `requests.get`; no real network

## `docs/update.md`

Add a short section:

- registry is a candidate list, not a guarantee
- release flow: build -> health check -> filter -> `make_latest --require-mirrors`
- all mirrors are covered by the signed manifest `urls[]` and asset `sha256`
- dead/HTTP mirrors are never published
- client policy: probe last-good + official + first few, rotate the rest

## Tests (`app/tests/test_update_mirrors.py`)

- registry expands all enabled entries, official first, dedupe, disabled skipped
- HTTP / non-https / template without `{base}` rejected
- relative `--base-url` skips registry with warning
- `--require-mirrors` passes/fails at the threshold
- manifest `urls[]` is unchanged for old clients when registry is disabled
- health checker marks good/bad/magic-mismatch responses and exit codes
- no network except localhost test server

## Acceptance

```powershell
cd app
python -m pytest tests/test_update_mirrors.py tests/test_update_signature.py tests/test_updater.py -q
python -m ruff check --select F,E9 packaging/make_latest.py packaging/check_update_mirrors.py tests/test_update_mirrors.py
```

## Deferred to L2

- Go launcher download loop: all mirrors, resume `.part`, last-good cache,
  exponential backoff, persistent `download.state.json`
- blocking only below `min_supported`, with manual/offline repair
- apply/rollback/boot-ok (already in L2 scope)
