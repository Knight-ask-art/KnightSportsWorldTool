# Update P0 / Slice 1 — Signed manifest primitives

Status: implement
Branch: `task/update-p0-signature`
Base: `master` @ `080e845`

## Scope (strict)

Add Ed25519 signature primitives for `latest.json` and signing support in
`packaging/make_latest.py`. **Do not change** `updater.check` / `updater.normalize`
behavior, the frontend, or the update flow yet. Old clients must keep reading
`schema: 1` and the existing `windows-x64` zip + `url` fields unchanged.

## Files

- NEW `app/replica/application/update_signature.py`
- MODIFY `app/packaging/make_latest.py`
- NEW `app/tests/test_update_signature.py`
- MODIFY `docs/update.md`

## Signature format

Top-level manifest fields:

```json
"signature": {
  "alg": "ed25519",
  "key_id": "release",
  "value": "<base64 signature>"
}
```

Canonical payload: copy the manifest, remove the top-level `signature` field,
then:

```python
json.dumps(payload, sort_keys=True, separators=(',', ':'),
           ensure_ascii=False, allow_nan=False).encode('utf-8')
```

`key_id` is part of the signed payload.

## API

```python
class ManifestSignatureError(ValueError): ...

def canonical_payload(manifest: dict) -> bytes: ...

def sign_manifest(manifest: dict, private_key, key_id: str) -> dict: ...
# returns a copy with signature; never mutates the input

def verify_manifest(manifest: dict, public_keys: dict | None = None,
                    *, required: bool = True) -> dict: ...
# returns the manifest unchanged on success
# missing signature + required=False -> return manifest
# missing signature + required=True  -> ManifestSignatureError

PUBLIC_KEYS: dict[str, bytes] = {}
```

Key coercion:
- accept `Ed25519PrivateKey` / `Ed25519PublicKey`
- accept 32-byte raw private seed / 32-byte raw public key
- accept base64 strings for public keys
- reject wrong lengths, unknown key id, `alg != 'ed25519'`, missing/empty
  `key_id` or `value`, invalid base64, and invalid signatures
- catch `cryptography.exceptions.InvalidSignature` and raise
  `ManifestSignatureError`
- no disk, network, environment, or logging in this module

`PUBLIC_KEYS` is intentionally empty in source. A release build injects the
real public key in Slice 2; tests pass keys explicitly.

## `packaging/make_latest.py`

- add `--sign-key PATH` (unencrypted Ed25519 PEM private key)
- add `--key-id` (default `release`)
- add `--require-signature`; with no `--sign-key` this must exit 2
- sign the manifest after `build_manifest` and before `write_manifest`
- no key supplied -> unchanged unsigned output (backward compatible)
- never print or log the private key
- do not change `build_manifest`'s public return shape beyond adding
  `signature` when signing

## Tests (`app/tests/test_update_signature.py`)

Use `tmp_path`; no network.

- canonical payload is stable across dict insertion order and excludes
  `signature`
- sign/verify round trip with a test Ed25519 key
- tamper `version` or `assets` -> fail
- wrong key -> fail
- unknown `key_id` -> fail
- missing signature: `required=True` fails, `required=False` passes
- invalid `alg` -> fail
- `sign_manifest` does not mutate its input
- `make_latest.main([...])` with `--sign-key` writes a verifiable manifest
- `make_latest.main([...])` without a key stays unsigned
- `--require-signature` without a key exits 2

For the CLI test, create a tiny zip with `PK\x03\x04` bytes, pass
`--rollback-version ''` to avoid unrelated rollback warnings.

## Verify

```powershell
cd app
python -m pytest tests/test_update_signature.py tests/test_updater.py -q
python -m ruff check --select F,E9 replica/application/update_signature.py packaging/make_latest.py tests/test_update_signature.py
```

## Report

Return: files changed, exact test commands/results, assumptions, and anything
intentionally left for Slice 2. Do not commit.
