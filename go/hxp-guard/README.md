# hxp-guard · one device, one account

Small Go sidecar that owns the local device→account binding.  The Python
bridge calls it before a login (`check`) and once the server accepted the
login (`bind`); it is not a daemon and holds no token.

```
hxp-guard check  --state <data>/account_binding.json --account 13800001234
hxp-guard bind   --state <data>/account_binding.json --account 13800001234
hxp-guard status --state <data>/account_binding.json
hxp-guard reset  --state <data>/account_binding.json --yes   # support only
hxp-guard device --state <data>/account_binding.json
hxp-guard op     --state <data>/account_binding.json --code 17   # nonce on stdin
```

`op --code 17` is the device-attestation collector used by the native shield:
it reads **one line of hex nonce from stdin** (never argv, which Windows
exposes to every same-user process), five goroutines gather machine fragments,
a nonce-seeded Fisher-Yates shuffle orders them, and the reply is one opaque
32-byte blob (`{"ok":true,"blob":...}`).  The nonce must be exactly 16 bytes.
No fragment is ever printed.

```bash
printf '00112233445566778899aabbccddeeff\n' |
  hxp-guard op --state data/account_binding.json --code 17
```

Every command prints one JSON object.  Exit codes: `0` ok, `3` blocked by the
binding rule, `2` any other failure (corrupt/tampered state, bad args).

## Rule

- The first account that finishes a login owns the device.
- Logging out keeps the binding; that is the point of the rule.
- Another account is refused with
  `为防止恶意使用软件，软件请一号一设备绑定`.
- The same account may log in again, including after a device-fingerprint
  rotation.
- Copying `data/` to another machine does not carry the binding: the device
  id is derived from that machine (`MachineGuid` on Windows, `/etc/machine-id`
  elsewhere), with a random `install_id` next to the state file as fallback.

## Files

| Path | Meaning |
|---|---|
| `data/account_binding.json` | `{version, device, account, hint, bound_at, mac}` |
| `data/install_id` | fallback device secret when the OS id is unavailable |

`account` is a SHA-256 hash and `hint` is masked (`138****1234`); the raw
phone number is never written.  `mac` is an HMAC over the record keyed by the
machine secret, so editing the JSON is detected on the same machine and fails
closed (`账号绑定数据校验失败…`).  This is tamper *evidence*, not a security
boundary — a local admin can still delete the file.

## Build & test

```bash
cd go/hxp-guard
go test ./...
go build -trimpath -ldflags="-s -w" -o hxp-guard.exe ./cmd/hxp-guard
```

`app/packaging/build.ps1` builds the exe and copies it to
`runtime/go/hxp-guard.exe` in the release.
