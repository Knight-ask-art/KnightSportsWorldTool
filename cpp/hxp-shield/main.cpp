// hxp-shield: short-lived device credential sidecar (Phase 1).
//
// One process, binary frames on stdin/stdout, numeric opcodes only.
// No exports, no files, no network, no tokens in logs.  See README.md.
//
// The seal state machine is driven by a vectored exception handler: each
// state action touches one fragment (nonce, go blob, rust blob, final HMAC)
// and the transition is a RaiseException(0xE0000A11) continued by the VEH.
// If the handler cannot be installed, the loop advances by itself and still
// produces a valid token instead of crashing.
//
// check() only accepts a token minted by this process instance (bounded
// in-memory cache).  A token from another instance is rejected by design:
// fail closed.

#include <windows.h>
#include <winreg.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#pragma comment(lib, "advapi32.lib")

namespace {

constexpr uint32_t kMagic = 0x53505848u;  // 'HXPS'
constexpr uint16_t kVersion = 1;
constexpr uint32_t kMaxPayload = 4096;
constexpr uint32_t kTtlMs = 3000;
constexpr uint32_t kExcCode = 0xE0000A11u;

enum : uint16_t {
    kOpSeal = 0x01,
    kOpCheck = 0x02,
    kOpPing = 0x03,
    kOpSelftest = 0x7f,
};

// ---------------------------------------------------------------- endian ---

uint16_t Rd16(const uint8_t* p) {
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

uint32_t Rd32(const uint8_t* p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

uint64_t Rd64(const uint8_t* p) {
    return (uint64_t)Rd32(p) | ((uint64_t)Rd32(p + 4) << 32);
}

void Wr16(uint8_t* p, uint16_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

void Wr32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)(v >> 24);
}

void Wr64(uint8_t* p, uint64_t v) {
    Wr32(p, (uint32_t)v);
    Wr32(p + 4, (uint32_t)(v >> 32));
}

// ----------------------------------------------------------------- SHA-256 -

struct ShaCtx {
    uint32_t h[8];
    uint64_t bits;
    uint8_t buf[64];
    uint32_t idx;
};

const uint32_t kShaK[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu,
    0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u, 0xd807aa98u, 0x12835b01u,
    0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u,
    0xc19bf174u, 0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau, 0x983e5152u,
    0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u,
    0x06ca6351u, 0x14292967u, 0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu,
    0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u,
    0xd6990624u, 0xf40e3585u, 0x106aa070u, 0x19a4c116u, 0x1e376c08u,
    0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu,
    0x682e6ff3u, 0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

const uint32_t kShaH[8] = {
    0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
    0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u,
};

inline uint32_t Rotr(uint32_t x, uint32_t n) {
    return (x >> n) | (x << (32u - n));
}

inline uint32_t Ch(uint32_t x, uint32_t y, uint32_t z) {
    return (x & y) ^ (~x & z);
}

inline uint32_t Maj(uint32_t x, uint32_t y, uint32_t z) {
    return (x & y) ^ (x & z) ^ (y & z);
}

inline uint32_t Bs0(uint32_t x) {
    return Rotr(x, 2) ^ Rotr(x, 13) ^ Rotr(x, 22);
}

inline uint32_t Bs1(uint32_t x) {
    return Rotr(x, 6) ^ Rotr(x, 11) ^ Rotr(x, 25);
}

inline uint32_t Ss0(uint32_t x) {
    return Rotr(x, 7) ^ Rotr(x, 18) ^ (x >> 3);
}

inline uint32_t Ss1(uint32_t x) {
    return Rotr(x, 17) ^ Rotr(x, 19) ^ (x >> 10);
}

void ShaBlock(ShaCtx* s, const uint8_t* p) {
    uint32_t w[64];
    for (int i = 0; i < 16; ++i) {
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    }
    for (int i = 16; i < 64; ++i) {
        w[i] = Ss1(w[i - 2]) + w[i - 7] + Ss0(w[i - 15]) + w[i - 16];
    }
    uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3];
    uint32_t e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
    for (int i = 0; i < 64; ++i) {
        uint32_t t1 = h + Bs1(e) + Ch(e, f, g) + kShaK[i] + w[i];
        uint32_t t2 = Bs0(a) + Maj(a, b, c);
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    s->h[0] += a;
    s->h[1] += b;
    s->h[2] += c;
    s->h[3] += d;
    s->h[4] += e;
    s->h[5] += f;
    s->h[6] += g;
    s->h[7] += h;
    memset(w, 0, sizeof(w));
}

void ShaInit(ShaCtx* s) {
    memcpy(s->h, kShaH, sizeof(kShaH));
    s->bits = 0;
    s->idx = 0;
}

void ShaUpdate(ShaCtx* s, const uint8_t* p, size_t n) {
    s->bits += (uint64_t)n * 8u;
    while (n > 0) {
        uint32_t room = 64u - s->idx;
        uint32_t take = (n < (size_t)room) ? (uint32_t)n : room;
        memcpy(s->buf + s->idx, p, take);
        s->idx += take;
        p += take;
        n -= take;
        if (s->idx == 64u) {
            ShaBlock(s, s->buf);
            s->idx = 0;
        }
    }
}

void ShaFinal(ShaCtx* s, uint8_t out[32]) {
    uint64_t bits = s->bits;
    s->buf[s->idx++] = 0x80u;
    if (s->idx > 56u) {
        while (s->idx < 64u) {
            s->buf[s->idx++] = 0;
        }
        ShaBlock(s, s->buf);
        s->idx = 0;
    }
    while (s->idx < 56u) {
        s->buf[s->idx++] = 0;
    }
    for (int i = 0; i < 8; ++i) {
        s->buf[56 + i] = (uint8_t)(bits >> (56 - 8 * i));
    }
    ShaBlock(s, s->buf);
    for (int i = 0; i < 8; ++i) {
        out[i * 4] = (uint8_t)(s->h[i] >> 24);
        out[i * 4 + 1] = (uint8_t)(s->h[i] >> 16);
        out[i * 4 + 2] = (uint8_t)(s->h[i] >> 8);
        out[i * 4 + 3] = (uint8_t)s->h[i];
    }
    memset(s, 0, sizeof(*s));
}

void Sha256(const uint8_t* p, size_t n, uint8_t out[32]) {
    ShaCtx s;
    ShaInit(&s);
    ShaUpdate(&s, p, n);
    ShaFinal(&s, out);
}

void HmacSha256(const uint8_t key[32], const uint8_t* msg, size_t n,
                uint8_t out[32]) {
    uint8_t ipad[64];
    uint8_t opad[64];
    uint8_t inner[32];
    memset(ipad, 0x36, sizeof(ipad));
    memset(opad, 0x5c, sizeof(opad));
    for (int i = 0; i < 32; ++i) {
        ipad[i] ^= key[i];
        opad[i] ^= key[i];
    }
    ShaCtx s;
    ShaInit(&s);
    ShaUpdate(&s, ipad, sizeof(ipad));
    ShaUpdate(&s, msg, n);
    ShaFinal(&s, inner);
    ShaInit(&s);
    ShaUpdate(&s, opad, sizeof(opad));
    ShaUpdate(&s, inner, sizeof(inner));
    ShaFinal(&s, out);
    memset(ipad, 0, sizeof(ipad));
    memset(opad, 0, sizeof(opad));
    memset(inner, 0, sizeof(inner));
}

// ------------------------------------------------------------ machine key --

uint8_t g_key[32];
volatile LONG g_key_ok = 0;

// Salt, stored XOR-encoded.  Runtime byte i = kSaltEnc[i] ^ (0x5A + 0x1D*i).
const uint8_t kSaltEnc[32] = {
    0xC4u, 0x6Au, 0xE5u, 0x75u, 0xE5u, 0x63u, 0x6Bu, 0xD0u,
    0x48u, 0x88u, 0x48u, 0x02u, 0xE8u, 0x31u, 0xE7u, 0x61u,
    0x94u, 0x04u, 0xEEu, 0x70u, 0xB9u, 0x63u, 0x8Du, 0xFBu,
    0x84u, 0x13u, 0xA5u, 0x6Du, 0xF9u, 0x11u, 0xAAu, 0x0Cu,
};

bool MachineSecret(uint8_t* out, uint32_t cap, uint32_t* out_len) {
    wchar_t guid[128];
    DWORD cb = (DWORD)sizeof(guid);
    LONG st = RegGetValueW(HKEY_LOCAL_MACHINE,
                           L"SOFTWARE\\Microsoft\\Cryptography",
                           L"MachineGuid",
                           RRF_RT_REG_SZ | RRF_SUBKEY_WOW6464KEY, nullptr,
                           guid, &cb);
    if (st == ERROR_SUCCESS) {
        int n = WideCharToMultiByte(CP_UTF8, 0, guid, -1, (char*)out, (int)cap,
                                    nullptr, nullptr);
        if (n > 1) {
            n -= 1;  // drop the terminating NUL
            while (n > 0 && (out[n - 1] == 0u || out[n - 1] == ' ')) {
                n -= 1;
            }
            if (n > 0) {
                *out_len = (uint32_t)n;
                return true;
            }
        }
    }

    // Fallback: computer name + system volume serial.
    wchar_t name[128];
    DWORD nlen = (DWORD)(sizeof(name) / sizeof(name[0]));
    DWORD serial = 0;
    if (!GetComputerNameW(name, &nlen)) {
        return false;
    }
    if (!GetVolumeInformationW(L"C:\\", nullptr, 0, &serial, nullptr, nullptr,
                               nullptr, 0)) {
        return false;
    }
    int n = WideCharToMultiByte(CP_UTF8, 0, name, (int)nlen, (char*)out,
                                (int)cap, nullptr, nullptr);
    if (n <= 0 || (uint32_t)n + 8u > cap) {
        return false;
    }
    static const char kHex[] = "0123456789abcdef";
    for (int i = 0; i < 8; ++i) {
        out[n + i] = (uint8_t)kHex[(serial >> (28 - 4 * i)) & 0xFu];
    }
    *out_len = (uint32_t)n + 8u;
    return true;
}

bool DeriveKey() {
    uint8_t ms[256];
    uint32_t ms_len = 0;
    if (!MachineSecret(ms, (uint32_t)sizeof(ms), &ms_len)) {
        return false;
    }

    static const char kPrefix[] = "hxp-shield.kdf.v1|";
    uint8_t in[sizeof(kPrefix) + 256 + 1 + 32];
    uint32_t n = (uint32_t)(sizeof(kPrefix) - 1);
    memcpy(in, kPrefix, n);
    memcpy(in + n, ms, ms_len);
    n += ms_len;
    in[n++] = '|';
    for (uint32_t i = 0; i < 32; ++i) {
        in[n++] = (uint8_t)(kSaltEnc[i] ^ (uint8_t)(0x5A + 0x1D * i));
    }
    Sha256(in, n, g_key);
    memset(ms, 0, sizeof(ms));
    memset(in, 0, sizeof(in));
    return true;
}

// ------------------------------------------------------------- state machine

struct SealCtx {
    const uint8_t* nonce;
    const uint8_t* go;
    const uint8_t* rust;
    uint8_t key[32];
    uint8_t msg[88];
    uint8_t token[32];
    uint64_t expiry;
    volatile LONG state;
};

volatile LONG g_veh_ready = 0;

// Opaque predicate: both targets are non-zero, the volatile index keeps the
// compiler from folding the call away.
volatile uint32_t g_entropy = 0x6D5A56F3u;

__declspec(noinline) int GateOne() {
    return 1;
}

__declspec(noinline) int GateTwo() {
    return (int)((g_entropy | 1u) != 0u);
}

int (*const volatile g_gate[2])() = {GateOne, GateTwo};

__declspec(noinline) bool Opaque() {
    return g_gate[g_entropy & 1u]() != 0;
}

__declspec(noinline) void StateNonce(SealCtx* c) {
    memcpy(c->msg, c->nonce, 16);
}

__declspec(noinline) void StateGo(SealCtx* c) {
    memcpy(c->msg + 16, c->go, 32);
}

__declspec(noinline) void StateRust(SealCtx* c) {
    memcpy(c->msg + 48, c->rust, 32);
    Wr64(c->msg + 80, c->expiry);
}

__declspec(noinline) void StateFinal(SealCtx* c) {
    HmacSha256(c->key, c->msg, sizeof(c->msg), c->token);
}

void (*const volatile g_states[4])(SealCtx*) = {
    StateNonce,
    StateGo,
    StateRust,
    StateFinal,
};

LONG CALLBACK ShieldVeh(EXCEPTION_POINTERS* ep) {
    if (ep != nullptr && ep->ExceptionRecord != nullptr &&
        ep->ExceptionRecord->ExceptionCode == kExcCode) {
        if (ep->ExceptionRecord->NumberParameters >= 1) {
            SealCtx* c = (SealCtx*)(uintptr_t)
                             ep->ExceptionRecord->ExceptionInformation[0];
            if (c != nullptr) {
                c->state++;
            }
        }
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

__declspec(noinline) void Advance(SealCtx* c) {
    if (g_veh_ready) {
        ULONG_PTR info[1];
        info[0] = (ULONG_PTR)c;
        RaiseException(kExcCode, 0, 1, info);
    } else {
        c->state++;
    }
}

bool RunSeal(SealCtx* c) {
    int hops = 0;
    for (;;) {
        LONG s = c->state;
        if (s < 0 || s >= 4) {
            break;
        }
        if (!Opaque()) {
            return false;
        }
        void (*fn)(SealCtx*) = g_states[(uint32_t)s & 3u];
        fn(c);
        Advance(c);
        if (++hops > 6) {
            return false;
        }
    }
    return c->state >= 4;
}

// --------------------------------------------------------- token cache -----

constexpr uint32_t kCacheN = 8;

struct CacheEnt {
    uint8_t tok[32];
    uint64_t exp;
    uint32_t used;
};

CacheEnt g_cache[kCacheN];
uint32_t g_cache_pos = 0;

bool CtEq(const uint8_t* a, const uint8_t* b, uint32_t n) {
    uint8_t d = 0;
    for (uint32_t i = 0; i < n; ++i) {
        d |= (uint8_t)(a[i] ^ b[i]);
    }
    return d == 0;
}

void CacheInsert(const uint8_t* tok, uint64_t exp) {
    CacheEnt* e = &g_cache[g_cache_pos];
    g_cache_pos = (g_cache_pos + 1u) % kCacheN;
    memset(e, 0, sizeof(*e));
    memcpy(e->tok, tok, 32);
    e->exp = exp;
    e->used = 1;
}

bool CacheCheck(const uint8_t* tok, uint64_t exp, uint64_t now) {
    for (uint32_t i = 0; i < kCacheN; ++i) {
        const CacheEnt* e = &g_cache[i];
        if (!e->used) {
            continue;
        }
        if (CtEq(e->tok, tok, 32)) {
            return e->exp == exp && now < e->exp;
        }
    }
    return false;
}

uint64_t NowMs() {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    uint64_t t = ((uint64_t)ft.dwHighDateTime << 32) | (uint64_t)ft.dwLowDateTime;
    return (t - 116444736000000000ULL) / 10000ULL;
}

bool SealMake(const uint8_t* nonce, const uint8_t* go, const uint8_t* rust,
              uint64_t expiry, uint8_t out_tok[32], uint64_t* out_exp) {
    if (!g_key_ok) {
        return false;
    }
    SealCtx c;
    memset(&c, 0, sizeof(c));
    c.nonce = nonce;
    c.go = go;
    c.rust = rust;
    c.expiry = expiry;
    memcpy(c.key, g_key, 32);
    c.state = 0;
    bool ok = RunSeal(&c);
    if (ok) {
        memcpy(out_tok, c.token, 32);
        *out_exp = expiry;
        CacheInsert(c.token, expiry);
    }
    memset(&c, 0, sizeof(c));
    return ok;
}

// ------------------------------------------------------------ self check ---
//
// Conservative by design: every test below must be true on a clean machine
// running a normal build, because a false positive sends the maintainer after
// a bug that does not exist.  The result is diagnostic; it never gates a
// login.

bool NoDebugger() {
    if (IsDebuggerPresent()) {
        return false;
    }
    BOOL remote = FALSE;
    if (CheckRemoteDebuggerPresent(GetCurrentProcess(), &remote) && remote) {
        return false;
    }
    return true;
}

bool ImageLooksIntact() {
    const uint8_t* base = (const uint8_t*)GetModuleHandleW(nullptr);
    if (base == nullptr) {
        return false;
    }
    const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0 ||
        dos->e_lfanew > 0x1000) {
        return false;
    }
    const IMAGE_NT_HEADERS* nt =
        (const IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) {
        return false;
    }
    const IMAGE_FILE_HEADER* fh = &nt->FileHeader;
    const IMAGE_OPTIONAL_HEADER* oh = &nt->OptionalHeader;
    if (fh->NumberOfSections == 0 || fh->NumberOfSections > 96) {
        return false;
    }
    if (oh->SectionAlignment == 0 ||
        (oh->SectionAlignment & (oh->SectionAlignment - 1u)) != 0) {
        return false;
    }

    // Everything we are about to read must sit inside the header region the
    // loader mapped.  A corrupt e_lfanew or section count must not walk past
    // SizeOfHeaders.
    const uint64_t table_end =
        (uint64_t)dos->e_lfanew + 4u + sizeof(IMAGE_FILE_HEADER) +
        fh->SizeOfOptionalHeader +
        (uint64_t)fh->NumberOfSections * sizeof(IMAGE_SECTION_HEADER);
    if (oh->SizeOfHeaders == 0 || table_end > (uint64_t)oh->SizeOfHeaders ||
        table_end > (uint64_t)oh->SizeOfImage) {
        return false;
    }

    const IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    const uint32_t entry = oh->AddressOfEntryPoint;
    uint32_t span = 0;
    bool entry_ok = false;
    for (uint32_t i = 0; i < fh->NumberOfSections; ++i) {
        const uint32_t va = sec[i].VirtualAddress;
        const uint32_t vs = sec[i].Misc.VirtualSize;
        const uint32_t rounded =
            (vs + oh->SectionAlignment - 1u) & ~(oh->SectionAlignment - 1u);
        if (va + rounded > span) {
            span = va + rounded;
        }
        if (entry >= va && entry < va + (vs ? vs : 1u)) {
            const uint32_t ch = sec[i].Characteristics;
            entry_ok = (ch & IMAGE_SCN_MEM_EXECUTE) != 0 &&
                       (ch & IMAGE_SCN_MEM_WRITE) == 0;
        }
    }
    // SizeOfImage is the rounded end of the last section.  Allow the tail
    // padding a linker may leave, but not a section table that disagrees.
    if (!entry_ok || span > oh->SizeOfImage ||
        oh->SizeOfImage - span >= oh->SectionAlignment) {
        return false;
    }

    // A trampoline or an inline patch needs the code page writable.  Windows
    // maps .text PAGE_EXECUTE_READ, so RWX here means someone changed it.
    // WRITECOPY is not flagged: a few loaders/EDR setups use it legitimately
    // and the build treats "image" as a hard defect.
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(base + entry, &mbi, sizeof(mbi)) == 0) {
        return false;
    }
    if ((mbi.Protect & PAGE_EXECUTE_READWRITE) != 0) {
        return false;
    }
    return true;
}

// ------------------------------------------------------------- opcode path -

bool RunSelftest() {
    if (!g_key_ok) {
        return false;
    }
    uint8_t nonce[16];
    uint8_t go[32];
    uint8_t rust[32];
    for (uint32_t i = 0; i < 16; ++i) {
        nonce[i] = (uint8_t)(0x11u + i * 7u);
    }
    for (uint32_t i = 0; i < 32; ++i) {
        go[i] = (uint8_t)(0x80u + i * 5u);
        rust[i] = (uint8_t)(0x23u + i * 3u);
    }

    uint64_t now = NowMs();
    uint64_t exp = 0;
    uint8_t tok[32];
    if (!SealMake(nonce, go, rust, now + kTtlMs, tok, &exp)) {
        return false;  // 1. seal with fixed fragments
    }
    if (!CacheCheck(tok, exp, NowMs())) {
        return false;  // 2. immediate check is valid
    }

    uint8_t bad[32];
    memcpy(bad, tok, 32);
    bad[9] ^= 0x40u;
    if (CacheCheck(bad, exp, NowMs())) {
        return false;  // 3. tampered token is rejected
    }

    uint64_t past = (now > 6000u) ? (now - 5000u) : 0u;
    uint64_t exp2 = 0;
    uint8_t tok2[32];
    if (!SealMake(nonce, go, rust, past, tok2, &exp2)) {
        return false;
    }
    if (CacheCheck(tok2, exp2, NowMs())) {
        return false;  // 4. expired token is rejected
    }
    if (CacheCheck(tok, exp + 1u, NowMs())) {
        return false;  // request expiry must match the minted one
    }
    memset(tok, 0, sizeof(tok));
    memset(tok2, 0, sizeof(tok2));
    memset(bad, 0, sizeof(bad));
    return true;
}

uint32_t Dispatch(uint16_t op, const uint8_t* p, uint32_t n, uint8_t* out) {
    switch (op) {
        case kOpSeal: {
            if (n != 80u) {
                out[0] = 0;
                return 1;
            }
            uint8_t tok[32];
            uint64_t exp = 0;
            if (SealMake(p, p + 16, p + 48, NowMs() + kTtlMs, tok, &exp)) {
                memcpy(out, tok, 32);
                Wr64(out + 32, exp);
                memset(tok, 0, sizeof(tok));
                return 40;
            }
            memset(tok, 0, sizeof(tok));
            out[0] = 0;
            return 1;
        }
        case kOpCheck: {
            if (n != 40u) {
                out[0] = 0;
                return 1;
            }
            uint64_t exp = Rd64(p + 32);
            bool ok = g_key_ok && CacheCheck(p, exp, NowMs());
            out[0] = ok ? 1u : 0u;
            return 1;
        }
        case kOpPing: {
            out[0] = (n == 0u) ? 1u : 0u;
            return 1;
        }
        case kOpSelftest: {
            out[0] = (n == 0u && RunSelftest()) ? 1u : 0u;
            return 1;
        }
        default: {
            out[0] = 0;
            return 1;
        }
    }
}

struct Hdr {
    uint16_t op;
    uint32_t len;
};

bool ParseHdr(const uint8_t* h, Hdr* out) {
    if (Rd32(h) != kMagic || Rd16(h + 4) != kVersion) {
        return false;
    }
    out->op = Rd16(h + 6);
    out->len = Rd32(h + 8);
    return out->len <= kMaxPayload;
}

// One complete frame in memory; the same header checks as FrameLoop.
bool ProcessFrame(const uint8_t* in, uint32_t in_len, uint8_t* out,
                  uint32_t* out_len) {
    if (in_len < 12u) {
        return false;
    }
    Hdr h;
    if (!ParseHdr(in, &h) || in_len != 12u + h.len) {
        return false;
    }
    uint8_t payload[kMaxPayload];
    uint32_t plen = Dispatch(h.op, in + 12, h.len, payload);
    Wr32(out, kMagic);
    Wr16(out + 4, kVersion);
    Wr16(out + 6, h.op);
    Wr32(out + 8, plen);
    memcpy(out + 12, payload, plen);
    *out_len = 12u + plen;
    return true;
}

// --------------------------------------------------------------- frame I/O -

void Say(const char* s) {
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    if (h == nullptr || h == INVALID_HANDLE_VALUE) {
        return;
    }
    DWORD w = 0;
    WriteFile(h, s, (DWORD)strlen(s), &w, nullptr);
}

void Err(const char* s) {
    HANDLE h = GetStdHandle(STD_ERROR_HANDLE);
    if (h == nullptr || h == INVALID_HANDLE_VALUE) {
        return;
    }
    DWORD w = 0;
    WriteFile(h, s, (DWORD)strlen(s), &w, nullptr);
}

// 1 = read all, 0 = clean EOF at a frame boundary, -1 = truncated/error.
int ReadFull(HANDLE h, uint8_t* buf, uint32_t n) {
    uint32_t got = 0;
    while (got < n) {
        DWORD r = 0;
        if (!ReadFile(h, buf + got, n - got, &r, nullptr)) {
            // The parent closed the write end: Windows reports
            // ERROR_BROKEN_PIPE instead of a zero-byte read.
            if (GetLastError() == ERROR_BROKEN_PIPE && got == 0) {
                return 0;
            }
            return -1;
        }
        if (r == 0) {
            // EOF at a frame boundary: the parent closed the pipe after its
            // last request.  That is a clean shutdown, not a bad frame.
            if (got == 0) {
                return 0;
            }
            return -1;
        }
        got += r;
    }
    return 1;
}

bool WriteFull(HANDLE h, const uint8_t* buf, uint32_t n) {
    uint32_t sent = 0;
    while (sent < n) {
        DWORD w = 0;
        if (!WriteFile(h, buf + sent, n - sent, &w, nullptr) || w == 0) {
            return false;
        }
        sent += w;
    }
    return true;
}

bool HasConsole() {
    SetLastError(ERROR_SUCCESS);
    HANDLE h = GetStdHandle(STD_INPUT_HANDLE);
    if (h == nullptr || h == INVALID_HANDLE_VALUE) {
        return false;
    }
    if (GetFileType(h) != FILE_TYPE_UNKNOWN) {
        return true;
    }
    return GetLastError() != ERROR_INVALID_HANDLE;
}

int FrameLoop() {
    HANDLE hin = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE hout = GetStdHandle(STD_OUTPUT_HANDLE);
    if (hin == nullptr || hin == INVALID_HANDLE_VALUE || hout == nullptr ||
        hout == INVALID_HANDLE_VALUE) {
        return 2;
    }
    uint8_t hdr[12];
    uint8_t payload[kMaxPayload];
    uint8_t resp[12 + kMaxPayload];
    for (;;) {
        int r = ReadFull(hin, hdr, 12);
        if (r == 0) {
            return 0;
        }
        if (r < 0) {
            Err("bad frame\n");
            return 2;
        }
        Hdr h;
        if (!ParseHdr(hdr, &h)) {
            Err("bad frame\n");
            return 2;
        }
        if (ReadFull(hin, payload, h.len) != 1) {
            Err("bad frame\n");
            return 2;
        }
        uint32_t plen = Dispatch(h.op, payload, h.len, resp + 12);
        Wr32(resp, kMagic);
        Wr16(resp + 4, kVersion);
        Wr16(resp + 6, h.op);
        Wr32(resp + 8, plen);
        if (!WriteFull(hout, resp, 12u + plen)) {
            return 2;
        }
    }
}

// Same in-memory frame path as FrameLoop, minus the OS handles.
bool RunFrameTest() {
    uint8_t req[12 + 80];
    uint8_t resp[12 + kMaxPayload];
    uint32_t rlen = 0;
    uint8_t nonce[16];
    uint8_t go[32];
    uint8_t rust[32];
    for (uint32_t i = 0; i < 16; ++i) {
        nonce[i] = (uint8_t)(0x31u + i * 3u);
    }
    for (uint32_t i = 0; i < 32; ++i) {
        go[i] = (uint8_t)(0x40u + i * 7u);
        rust[i] = (uint8_t)(0x91u + i * 5u);
    }

    Wr32(req, kMagic);
    Wr16(req + 4, kVersion);
    Wr16(req + 6, kOpSeal);
    Wr32(req + 8, 80);
    memcpy(req + 12, nonce, 16);
    memcpy(req + 28, go, 32);
    memcpy(req + 60, rust, 32);
    if (!ProcessFrame(req, (uint32_t)sizeof(req), resp, &rlen) ||
        rlen != 52u || Rd16(resp + 6) != kOpSeal || Rd32(resp + 8) != 40u) {
        return false;
    }
    uint64_t exp = Rd64(resp + 12 + 32);
    uint8_t tok[32];
    memcpy(tok, resp + 12, 32);

    uint8_t creq[12 + 40];
    Wr32(creq, kMagic);
    Wr16(creq + 4, kVersion);
    Wr16(creq + 6, kOpCheck);
    Wr32(creq + 8, 40);
    memcpy(creq + 12, tok, 32);
    Wr64(creq + 44, exp);
    if (!ProcessFrame(creq, (uint32_t)sizeof(creq), resp, &rlen) ||
        rlen != 13u || Rd16(resp + 6) != kOpCheck || resp[12] != 1u) {
        return false;
    }

    uint8_t bad[32];
    memcpy(bad, tok, 32);
    bad[3] ^= 0x80u;
    memcpy(creq + 12, bad, 32);
    if (!ProcessFrame(creq, (uint32_t)sizeof(creq), resp, &rlen) ||
        resp[12] != 0u) {
        return false;
    }

    memcpy(creq + 12, tok, 32);
    Wr64(creq + 44, exp - 1000u);
    if (!ProcessFrame(creq, (uint32_t)sizeof(creq), resp, &rlen) ||
        resp[12] != 0u) {
        return false;
    }

    memset(tok, 0, sizeof(tok));
    memset(bad, 0, sizeof(bad));
    return true;
}

void Init() {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX |
                 SEM_NOOPENFILEERRORBOX);
    if (AddVectoredExceptionHandler(1, ShieldVeh) != nullptr) {
        g_veh_ready = 1;
    }
    if (DeriveKey()) {
        g_key_ok = 1;
    }
}

}  // namespace

int main(int argc, char** argv) {
    Init();
    if (argc > 1) {
        if (strcmp(argv[1], "--selftest") == 0) {
            if (RunSelftest()) {
                Say("selftest: ok\n");
                return 0;
            }
            Err("selftest: fail\n");
            return 1;
        }
        if (strcmp(argv[1], "--frame-test") == 0) {
            if (RunFrameTest()) {
                Say("frame-test: ok\n");
                return 0;
            }
            Err("frame-test: fail\n");
            return 1;
        }
        if (strcmp(argv[1], "--selfcheck") == 0) {
            // No path, module name or address is printed: the caller only
            // needs a verdict, and a support log is not a map for a patcher.
            if (!NoDebugger()) {
                Err("selfcheck: debugger\n");
                return 1;
            }
            if (!ImageLooksIntact()) {
                Err("selfcheck: image\n");
                return 1;
            }
            Say("selfcheck: ok\n");
            return 0;
        }
        Err("bad arg\n");
        return 2;
    }
    if (!HasConsole()) {
        // Runs inside the parent's shell helpers (e.g. its command timeout
        // without a CDP handle): never consume shared input, fail closed.
        return 2;
    }
    return FrameLoop();
}
