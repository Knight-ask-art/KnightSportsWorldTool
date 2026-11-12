"""Ed25519 signature primitives for ``latest.json`` manifests.

This module is pure in-memory cryptography and canonical JSON serialization:
no disk, network, environment, or logging access.

``cryptography`` is imported lazily: the frozen app only needs
``verify_manifest_raw`` (which uses the dependency-free
``replica.domain.ed25519``), while the build machine uses ``sign_manifest`` /
``verify_manifest`` with the real library.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping

from ..domain import ed25519

ALGORITHM = 'ed25519'
_KEY_BYTES = 32
_SIG_BYTES = 64

#: Key-id -> 32-byte raw Ed25519 public key (or base64 / Ed25519PublicKey).
#: Intentionally empty in source; release builds inject the real key into
#: ``replica/_update_pubkey.py``.
PUBLIC_KEYS: dict[str, bytes] = {}


class ManifestSignatureError(ValueError):
    """Raised when a manifest signature is missing, malformed, or invalid."""


def canonical_payload(manifest: dict) -> bytes:
    """Return the canonical UTF-8 JSON bytes for ``manifest`` without ``signature``."""
    if not isinstance(manifest, dict):
        raise ManifestSignatureError('manifest must be a dict')
    payload = dict(manifest)
    payload.pop('signature', None)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ManifestSignatureError(f'manifest cannot be canonicalized: {exc}') from exc
    try:
        return encoded.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ManifestSignatureError(
            f'manifest cannot be encoded as UTF-8: {exc}'
        ) from exc


def _validate_key_id(key_id: object) -> str:
    if not isinstance(key_id, str) or not key_id.strip() or '\x00' in key_id:
        raise ManifestSignatureError('key_id must be a non-empty string')
    return key_id


def _signing_bytes(manifest: dict, key_id: str) -> bytes:
    validated_id = _validate_key_id(key_id)
    try:
        encoded_id = validated_id.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ManifestSignatureError(
            f'key_id cannot be encoded as UTF-8: {exc}'
        ) from exc
    return encoded_id + b'\x00' + canonical_payload(manifest)


def _signature_parts(manifest: dict, required: bool):
    """Return ``(key_id, signature_bytes)`` or ``(None, None)`` when optional."""
    if not isinstance(manifest, dict):
        raise ManifestSignatureError('manifest must be a dict')

    if 'signature' not in manifest or manifest['signature'] is None:
        if not required:
            return None, None
        raise ManifestSignatureError('missing manifest signature')

    sig = manifest['signature']
    if not isinstance(sig, dict):
        raise ManifestSignatureError('manifest signature must be an object')

    alg = sig.get('alg')
    if not isinstance(alg, str) or alg != ALGORITHM:
        raise ManifestSignatureError(f'unsupported signature algorithm: {alg!r}')

    key_id = _validate_key_id(sig.get('key_id'))

    value = sig.get('value')
    if not isinstance(value, str) or not value:
        raise ManifestSignatureError('manifest signature value must be a non-empty string')

    try:
        sig_bytes = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestSignatureError(f'invalid base64 signature: {exc}') from exc

    if len(sig_bytes) != _SIG_BYTES:
        raise ManifestSignatureError(
            f'Ed25519 signature must be {_SIG_BYTES} bytes, got {len(sig_bytes)}'
        )
    return key_id, sig_bytes


def _raw_public_key(public_key: object) -> bytes:
    if isinstance(public_key, str):
        text = public_key.strip()
        if not text:
            raise ManifestSignatureError('public key string is empty')
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ManifestSignatureError(f'invalid base64 public key: {exc}') from exc
    elif isinstance(public_key, (bytes, bytearray, memoryview)):
        raw = bytes(public_key)
    else:
        raw = None
        try:
            raw = public_key.public_bytes_raw()
        except Exception:  # noqa: BLE001 - duck-typed Ed25519PublicKey
            raise ManifestSignatureError('unsupported public key type') from None
    if not isinstance(raw, (bytes, bytearray)):
        raise ManifestSignatureError('unsupported public key type')
    raw = bytes(raw)
    if len(raw) != _KEY_BYTES:
        raise ManifestSignatureError(
            f'Ed25519 public key must be {_KEY_BYTES} bytes, got {len(raw)}'
        )
    return raw


def _coerce_private_key(private_key: object):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    if isinstance(private_key, Ed25519PrivateKey):
        return private_key
    if isinstance(private_key, (bytes, bytearray, memoryview)):
        raw = bytes(private_key)
        if len(raw) != _KEY_BYTES:
            raise ManifestSignatureError(
                f'Ed25519 private key seed must be {_KEY_BYTES} bytes, got {len(raw)}'
            )
        try:
            return Ed25519PrivateKey.from_private_bytes(raw)
        except ValueError as exc:
            raise ManifestSignatureError(f'invalid Ed25519 private key: {exc}') from exc
    raise ManifestSignatureError('unsupported private key type')


def sign_manifest(manifest: dict, private_key, key_id: str) -> dict:
    """Return a shallow copy of ``manifest`` with a valid ``signature`` object."""
    if not isinstance(manifest, dict):
        raise ManifestSignatureError('manifest must be a dict')
    validated_id = _validate_key_id(key_id)
    key = _coerce_private_key(private_key)
    message = _signing_bytes(manifest, validated_id)
    sig_bytes = key.sign(message)
    out = dict(manifest)
    out['signature'] = {
        'alg': ALGORITHM,
        'key_id': validated_id,
        'value': base64.b64encode(sig_bytes).decode('ascii'),
    }
    return out


def verify_manifest_raw(
    manifest: dict,
    public_keys: dict | None = None,
    *,
    required: bool = True,
) -> dict:
    """Verify with raw 32-byte keys; no ``cryptography`` needed at runtime."""
    key_id, sig_bytes = _signature_parts(manifest, required)
    if key_id is None:
        return manifest

    keys = PUBLIC_KEYS if public_keys is None else public_keys
    if not isinstance(keys, Mapping):
        raise ManifestSignatureError('public_keys must be a mapping')
    if key_id not in keys:
        raise ManifestSignatureError(f'unknown key_id: {key_id}')

    raw = _raw_public_key(keys[key_id])
    message = _signing_bytes(manifest, key_id)
    if not ed25519.verify(raw, message, sig_bytes):
        raise ManifestSignatureError('invalid manifest signature')
    return manifest


def verify_manifest(
    manifest: dict,
    public_keys: dict | None = None,
    *,
    required: bool = True,
) -> dict:
    """Verify ``manifest['signature']`` with ``cryptography`` and return it."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    key_id, sig_bytes = _signature_parts(manifest, required)
    if key_id is None:
        return manifest

    keys = PUBLIC_KEYS if public_keys is None else public_keys
    if not isinstance(keys, Mapping):
        raise ManifestSignatureError('public_keys must be a mapping')
    if key_id not in keys:
        raise ManifestSignatureError(f'unknown key_id: {key_id}')

    try:
        pub = Ed25519PublicKey.from_public_bytes(_raw_public_key(keys[key_id]))
    except ValueError as exc:
        raise ManifestSignatureError(f'invalid Ed25519 public key: {exc}') from exc

    message = _signing_bytes(manifest, key_id)
    try:
        pub.verify(sig_bytes, message)
    except InvalidSignature as exc:
        raise ManifestSignatureError('invalid manifest signature') from exc

    return manifest
