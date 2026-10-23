"""NetSecKit envelope codec — pure Python.

Algorithm (confirmed against unidbg, see archive/unidbg-era/FRIDA_NEXT.md rounds 24-29):

    d = base64( AES-128-CBC( plaintext_json, KEY, iv=0 ) )

KEY is the per-session 16-byte printable-ASCII session key that unidbg derives
and keeps for the life of the process.  Same session => same key => ciphertext
of identical plaintext is identical, which is what the strict-pair captures
show.

plaintext_json layout:
    {"data":"<base64 payload>","keyDataFour":"...","keyDataOne":"...",
     "keyDataThree":"...","keyDataTwo":"...","platform":2,"timeStamp":N}
"""

import base64
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

ZERO_IV = b"\x00" * 16


def decrypt_d(key: bytes, d_b64: str) -> str:
    """Decrypt -> plaintext JSON string.

    Accepts either the raw `d` value (base64) or a full envelope JSON string,
    which is what the server actually returns.
    """
    if isinstance(key, str):
        key = bytes.fromhex(key)
    if len(key) != 16:
        raise ValueError("session key must be 16 bytes, got %d" % len(key))
    d_b64 = d_b64.strip()
    if d_b64.startswith("{"):
        env = json.loads(d_b64)
        # request envelopes carry the ciphertext in `d`; server responses use
        # `r` (plus `s`/`v` companions).
        for field in ("d", "r"):
            if field in env:
                d_b64 = env[field]
                break
        else:
            raise ValueError("no ciphertext field (d/r) in envelope: %s"
                             % list(env.keys()))
    raw = base64.b64decode(d_b64)
    if len(raw) % 16:
        raise ValueError("ciphertext length %d is not a multiple of 16" % len(raw))
    out = AES.new(key, AES.MODE_CBC, iv=ZERO_IV).decrypt(raw)
    try:
        out = unpad(out, 16)
    except ValueError:
        pass  # tolerate missing padding rather than losing the payload
    return out.decode("utf-8", errors="replace")


def decrypt_envelope(key: bytes, envelope: str) -> dict:
    """Decrypt a full envelope -> {'data': <payload str>, ...metadata}."""
    plain = decrypt_d(key, envelope)
    return json.loads(plain)


def payload_of(key: bytes, envelope: str, unwrap: bool = True) -> bytes:
    """The inner `data` field.

    Note the double base64: the app base64-encodes the body once before handing
    it to w3270, and the envelope's `data` field holds that already-encoded
    string. `unwrap=True` peels both layers back to the original bytes.
    """
    raw = base64.b64decode(decrypt_envelope(key, envelope)["data"])
    if unwrap:
        try:
            raw = base64.b64decode(raw)
        except Exception:
            pass
    return raw


def payload_text(key: bytes, envelope: str) -> str:
    """Original body as text, all encoding layers removed."""
    return payload_of(key, envelope).decode("utf-8", errors="replace")


def encrypt_d(key: bytes, payload: bytes, key_data=None, platform=2,
              timestamp=None) -> str:
    """Inverse of decrypt_d — build the JSON and CBC-encrypt it."""
    import os
    import time
    if isinstance(key, str):
        key = bytes.fromhex(key)
    if key_data is None:
        key_data = {}
    import hashlib
    meta = {
        "data": base64.b64encode(base64.b64encode(payload)).decode(),
        "keyDataFour": key_data.get("four", key[4:12].decode("latin1")),
        "keyDataOne": key_data.get("one", hashlib.md5(key).hexdigest()[:10]),
        "keyDataThree": key_data.get("three", hashlib.sha256(key).hexdigest()[:12]),
        "keyDataTwo": key_data.get("two", hashlib.md5(key[::-1]).hexdigest()[:10]),
        "platform": platform,
        "timeStamp": timestamp if timestamp is not None else int(time.time()),
    }
    raw = json.dumps(meta, separators=(",", ":")).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    enc = AES.new(key, AES.MODE_CBC, iv=ZERO_IV).encrypt(raw)
    return base64.b64encode(enc).decode()


if __name__ == "__main__":
    import sys
    # self-test against the strict-pair capture
    path = r"C:\Users\20969\AppData\Local\Temp\opencode\frida\strict2.tsv"
    ok = fail = 0
    for line in open(path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or not parts[2] or not parts[3]:
            continue
        native, key, env = parts[1], parts[2], parts[3]
        try:
            got = payload_of(bytes.fromhex(key), env)
        except Exception as e:
            print("  ERROR %s" % e)
            fail += 1
            continue
        want = base64.b64decode(native)
        if got == want:
            ok += 1
            print("  OK   payload=%r" % got[:32])
        else:
            fail += 1
            print("  FAIL want=%r got=%r" % (want[:32], got[:32]))
    print("\nround-trip: %d ok, %d failed" % (ok, fail))
    sys.exit(0 if fail == 0 and ok else 1)