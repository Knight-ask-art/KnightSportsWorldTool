# hxp-shield

Short-lived device credential sidecar (Phase 1 of `docs/hardening.md`).

`hxp-shield.exe` is an independent console process. It speaks binary frames on
stdin/stdout, exports no symbols, opens no files, and makes no network calls.
It never receives a token from another layer: it mints one and checks one it
minted itself.

| File | Purpose |
|---|---|
| `main.cpp` | Everything: frame codec, SHA-256/HMAC, VEH state machine, self-test |
| `build.ps1` | MSVC x64 build (`vcvars64.bat` + `cl`, no PDB) |
| `hxp-shield.exe` | Build output, ignored by git |

## Frame format (little-endian)

```
offset  size  field
0       4     magic   'HXPS' = 0x53505848
4       2     version 1
6       2     op
8       4     length  payload bytes, <= 4096
12      N     payload
```

The response uses the same header and echoes `op`. A bad magic, bad version,
or an out-of-range length terminates the process with exit code `2`; the only
diagnostic is a short `bad frame` line on stderr. No internal paths are
printed. A known op with the wrong payload size answers with a single `0x00`
byte (fail closed) instead of killing the stream. An unknown op is echoed with
a single `0x00` byte.

Process I/O:

- The process is a console executable and inherits the caller's console
  streams; a caller that redirects stdin/stdout to pipes gets the frame path.
- If stdin is not a console and the parent closes the pipe without a frame,
  that is a clean shutdown: exit `0`. A truncated frame is `2` / `bad frame`.
- Exit codes: `0` clean EOF at a frame boundary (pipe or console), `2` protocol
  error / bad argument / unusable std handles, `1` failing `--selftest`.

## Calling from Python (note for Phase 2)

Do not pass `DETACHED_PROCESS` or `CREATE_NEW_CONSOLE`. A child started with
`CREATE_NO_WINDOW` and pipes works normally (verified); it simply has no
console window. A brand-new console or a detached process is unnecessary and
makes the std handles harder to reason about.

The binary is a console subsystem executable on purpose: a windowed exe would
need its own console/stdio setup, which buys nothing for a pipe-only sidecar.

## Opcodes

| op | name | request payload | response payload |
|---|---|---|---|
| `0x01` | seal | 16 B nonce + 32 B go blob + 32 B rust blob | 32 B token + 8 B expiry_ms |
| `0x02` | check | 32 B token + 8 B expiry_ms | `0x01` valid, `0x00` invalid |
| `0x03` | ping | empty | `0x01` |
| `0x7f` | selftest | empty | `0x01` pass, `0x00` fail |

`seal` failure (no usable machine secret) answers with one `0x00` byte.

## Token

```
expiry = now_unix_ms + 3000
token  = HMAC-SHA256(key, nonce || go_blob || rust_blob || le64(expiry))
key    = SHA256("hxp-shield.kdf.v1|" + machine_secret + "|" + salt)
```

- `machine_secret` is `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`
  (UTF-8). If that fails: `GetComputerNameW` + the serial of `C:\` in hex.
  If both paths fail the process fails closed (`seal`/`check` → `0`); there is
  no hard-coded fallback secret.
- `salt` is never stored in plaintext: `kSaltEnc[i]` is XOR-decoded at runtime
  with `(0x5A + 0x1D*i) & 0xFF`.
- SHA-256 and HMAC are implemented in `main.cpp`; the only extra import is
  `advapi32.lib` (`RegGetValueW`).

`check` is deliberately process-local: the binary keeps a small in-memory ring
of tokens it minted. A token minted by another process instance is rejected.
That is fail closed, and the ring is never written anywhere.

## State machine and VEH

`seal` runs four state actions, dispatched through a volatile function table:

1. copy the nonce fragment,
2. copy the go fragment,
3. copy the rust fragment and append `le64(expiry)`,
4. HMAC the 88-byte buffer.

Each transition is `RaiseException(0xE0000A11, ...)`. The vectored handler
increments the state and returns `EXCEPTION_CONTINUE_EXECUTION`. A volatile
two-slot function table acts as an opaque predicate so `/O2 /GL` cannot fold
the machine back into straight-line code. If `AddVectoredExceptionHandler`
fails, transitions increment the state directly; the loop is capped so a bad
handler can never spin.

## Build

```powershell
# from the repo root
powershell -NoProfile -ExecutionPolicy Bypass -File .\cpp\hxp-shield\build.ps1

# or, if execution policy allows
.\cpp\hxp-shield\build.ps1
```

`build.ps1` looks for
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat`,
then falls back to `vswhere.exe`. Compiler/linker flags:

```
cl /nologo /O2 /GL /GR- /Gy /GS- /DNDEBUG /EHs- /MT /W3 main.cpp
   /link /LTCG /RELEASE /OPT:REF /OPT:ICF /OUT:hxp-shield.exe advapi32.lib
```

No `/Zi`, no `/DEBUG`, no PDB. The `.obj` is removed after a successful link.

## Self-test

```powershell
.\cpp\hxp-shield\hxp-shield.exe --selftest     # exit 0 on pass, 1 on fail
.\cpp\hxp-shield\hxp-shield.exe --frame-test   # same checks through in-memory frames
```

`--selftest` seals fixed nonce/go/rust, checks the token immediately, mutates
one token byte and expects rejection, then seals with a past expiry and expects
rejection. `--frame-test` drives the same frame path (header parse + response
build) without touching the OS handles.

## Verify no exports

```powershell
cmd /c "call `"<vcvars64.bat>`" >nul && dumpbin /exports .\cpp\hxp-shield\hxp-shield.exe"
```

Expected: `Dump of file ... Summary 0` with no export names.

## Constraints

- No `data/` reads, no file writes, no network, no token logging.
- No `system()` / `CreateProcess`.
- No `__declspec(dllexport)`; the executable is the only boundary.
- Phase 1 adds this directory and `.gitignore` entries only; `app/`, `rust/`
  and `go/` are untouched.
