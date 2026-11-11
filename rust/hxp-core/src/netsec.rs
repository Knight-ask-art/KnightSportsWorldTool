//! NetSecKit envelope — byte-compatible with `replica/transport/netsec.py`.
//!
//! The Python module remains the reference and the fallback; this is the
//! native implementation used by the packaged build.  Test vectors below come
//! from the upstream `NekoSportsWorldTool` suite and from the Python module's
//! own `self_test()`.

use std::sync::OnceLock;

use aes::cipher::{generic_array::GenericArray, BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use aes::Aes128;
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use num_bigint_dig::BigUint;
use rsa::pkcs8::DecodePublicKey;
use rsa::traits::PublicKeyParts;
use rsa::{Pkcs1v15Encrypt, RsaPublicKey};
use serde::Serialize;
use serde_json::Value;

pub const KEYDATA_ONE: &str = "nhang.school";
pub const KEYDATA_TWO: &str = "5K0E8400-E29";
pub const KEYDATA_THREE: &str = "597DEA1AFB49";
pub const SALT_ALPHABET: &str =
    "+kot8A*B45jF6CD@a!UVWubcdKLZ{efgMpNOxyz01PQ}Rn)Tvw23XYh(iG7rsEqJHI9+Slm/";
pub const DEFAULT_PLATFORM: i32 = 2;

const RSA_PUB_PEM: &str = "-----BEGIN PUBLIC KEY-----\n\
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC5pqlTzsGNZk1RxhH4O4x3JNTD\n\
V7FbVH66mPfW5v1tnIy4ty7xv8DGMG4Zn/TvstwlJWeYOADHdi8uF21lJaBvzvPt\n\
VEhifHXZq825fI9hGYtDoaVQmCN/Nfs2dKmt89XDrhtl3SZxO6TumOCTQt+5oqjF\n\
2Jo3o1YtkAyGzjaJnwIDAQAB\n\
-----END PUBLIC KEY-----";

const ZERO_IV: [u8; 16] = [0u8; 16];
const MASK32: u32 = 0xFFFF_FFFF;

type Aes128CbcEnc = cbc::Encryptor<Aes128>;
type Aes128CbcDec = cbc::Decryptor<Aes128>;

fn public_key() -> &'static RsaPublicKey {
    static PUB: OnceLock<RsaPublicKey> = OnceLock::new();
    PUB.get_or_init(|| {
        RsaPublicKey::from_public_key_pem(RSA_PUB_PEM).expect("embedded RSA public key must parse")
    })
}

/// UTF8String/strlen semantics: stop at the first NUL.
fn c_string_bytes(value: &str) -> &[u8] {
    let raw = value.as_bytes();
    match raw.iter().position(|&b| b == 0) {
        Some(nul) => &raw[..nul],
        None => raw,
    }
}

fn ror32(value: u32, bits: u32) -> u32 {
    let bits = bits & 31;
    if bits == 0 {
        return value & MASK32;
    }
    let v = value & MASK32;
    ((v >> bits) | (v << (32 - bits))) & MASK32
}

/// 0x103f5684c — rolling 32-bit fold over the string bytes.
pub fn fold32(value: &str) -> u32 {
    let mut h: u32 = 0;
    for &c in c_string_bytes(value) {
        h = ((((h & 0x00FF_FFFF) << 8) | c as u32) ^ (h >> 24)) & MASK32;
    }
    h
}

/// 0x103f5689c — MBA mix over the four fold32 values -> 16-byte AES key.
pub fn derive_paes_key(one: &str, two: &str, three: &str, four: &str) -> Result<[u8; 16], String> {
    for v in [one, two, three, four] {
        let n = c_string_bytes(v).len();
        if !(8..=12).contains(&n) {
            return Err(format!(
                "keyData must be 8..12 UTF-8 bytes, got {n} for {v:?}"
            ));
        }
    }

    let (a, b, c, d) = (fold32(one), fold32(two), fold32(three), fold32(four));

    let mut w9 = c ^ a;
    let mut w10 = w9 ^ ror32(w9, 24);
    w9 = w10 ^ ror32(w9, 8);
    w10 = w9 ^ b;
    w9 ^= d;

    let mut w8 = d ^ b;
    let mut w11 = w8 ^ ror32(w8, 24);
    w8 = w11 ^ ror32(w8, 8);
    w11 = w8 ^ a;
    w8 ^= c;

    let mut w12 = w8 & w10;
    w11 ^= w12;
    w12 = w8 | w9;
    w8 ^= w9;
    w10 ^= w12;
    w8 = w10 ^ (!w8 & MASK32);
    w12 = w8 ^ w11;
    w8 |= w11;
    w8 ^= w10;
    w10 = w12 & (!w10 & MASK32);
    w9 ^= w10;

    let mut out = [0u8; 16];
    out[0..4].copy_from_slice(&(w9 & MASK32).to_le_bytes());
    out[4..8].copy_from_slice(&(w8 & MASK32).to_le_bytes());
    out[8..12].copy_from_slice(&(w12 & MASK32).to_le_bytes());
    out[12..16].copy_from_slice(&(w11 & MASK32).to_le_bytes());
    Ok(out)
}

pub fn aes_cbc_encrypt(key: &[u8], plaintext: &[u8]) -> Result<Vec<u8>, String> {
    let mut buf = plaintext.to_vec();
    let pad = 16 - (buf.len() % 16);
    buf.extend(std::iter::repeat(pad as u8).take(pad));
    let mut enc = Aes128CbcEnc::new_from_slices(key, &ZERO_IV).map_err(|e| e.to_string())?;
    for chunk in buf.chunks_exact_mut(16) {
        enc.encrypt_block_mut(GenericArray::from_mut_slice(chunk));
    }
    Ok(buf)
}

/// Tolerates a missing PKCS#7 block instead of losing the payload, matching
/// the Python fallback.
pub fn aes_cbc_decrypt(key: &[u8], ciphertext: &[u8]) -> Result<Vec<u8>, String> {
    if ciphertext.is_empty() || ciphertext.len() % 16 != 0 {
        return Err(format!(
            "ciphertext length {} is not a block multiple",
            ciphertext.len()
        ));
    }
    let mut buf = ciphertext.to_vec();
    let mut dec = Aes128CbcDec::new_from_slices(key, &ZERO_IV).map_err(|e| e.to_string())?;
    for chunk in buf.chunks_exact_mut(16) {
        dec.decrypt_block_mut(GenericArray::from_mut_slice(chunk));
    }
    if let Some(&last) = buf.last() {
        let n = last as usize;
        if (1..=16).contains(&n)
            && buf.len() >= n
            && buf[buf.len() - n..].iter().all(|&b| b == last)
        {
            buf.truncate(buf.len() - n);
        }
    }
    Ok(buf)
}

pub fn md5_hex(data: &[u8]) -> String {
    use md5::{Digest, Md5};
    let digest = Md5::digest(data);
    let mut out = String::with_capacity(32);
    for byte in digest.iter() {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

fn b64e(data: &[u8]) -> String {
    B64.encode(data)
}

fn b64d(text: &str) -> Result<Vec<u8>, String> {
    B64.decode(text.trim())
        .map_err(|e| format!("bad base64: {e}"))
}

/// b64(MD5(plain)) — kept as a helper for diagnostics.
pub fn digest_b64(data: &[u8]) -> String {
    b64e(md5_hex(data).as_bytes())
}

/// b64(RSA-1024 PKCS1v15(reqKey)) — the envelope's `k`.
pub fn rsa_encrypt_req_key(req_key: &str) -> Result<String, String> {
    let mut rng = rand::thread_rng();
    let ct = public_key()
        .encrypt(&mut rng, Pkcs1v15Encrypt, req_key.as_bytes())
        .map_err(|e| format!("rsa encrypt failed: {e}"))?;
    Ok(b64e(&ct))
}

/// Recover the 32 lowercase hex chars from the PKCS#1 v1.5 type-1 block.
pub fn rsa_recover_digest(s_b64: &str) -> Result<String, String> {
    let key = public_key();
    let sig = b64d(s_b64)?;
    let size = ((key.n().bits() + 7) / 8) as usize;
    if sig.len() != size {
        return Err(format!(
            "response s length {} != key size {}",
            sig.len(),
            size
        ));
    }
    let em = BigUint::from_bytes_be(&sig)
        .modpow(key.e(), key.n())
        .to_bytes_be();
    let mut em = {
        let mut padded = vec![0u8; size.saturating_sub(em.len())];
        padded.extend_from_slice(&em);
        padded
    };
    if em.len() < 2 || em[0] != 0x00 || em[1] != 0x01 {
        return Err("s is not an RSA PKCS#1 type-1 block".into());
    }
    let marker = em[2..]
        .iter()
        .position(|&b| b == 0x00)
        .map(|i| i + 2)
        .ok_or("s is missing the PKCS#1 separator")?;
    if marker < 10 || em[2..marker].iter().any(|&b| b != 0xFF) {
        return Err("s has invalid PKCS#1 type-1 padding".into());
    }
    let digest = em.split_off(marker + 1);
    if digest.len() != 32
        || !digest
            .iter()
            .all(|&b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("s tail is not 32 lowercase hex bytes".into());
    }
    String::from_utf8(digest).map_err(|e| e.to_string())
}

fn random_fragment(n: usize) -> String {
    use rand::Rng;
    let pool: Vec<char> = SALT_ALPHABET.chars().collect();
    let mut rng = rand::thread_rng();
    (0..n).map(|_| pool[rng.gen_range(0..pool.len())]).collect()
}

pub fn new_req_key() -> String {
    random_fragment(16)
}

/// native `stringWithFormat:"%f"` (ms as double, 6 dp) -> tail 12 chars.
pub fn key_data_four_from_ms(ms: f64) -> String {
    let value = format!("{ms:.6}");
    let raw_len = value.len();
    if raw_len <= 7 {
        let mut out = value;
        out.push_str(&random_fragment(12 - raw_len));
        return out;
    }
    if raw_len >= 13 {
        return value[raw_len - 12..].to_string();
    }
    value
}

#[derive(Serialize)]
struct Container<'a> {
    data: &'a str,
    #[serde(rename = "timeStamp")]
    time_stamp: i64,
    platform: i32,
    #[serde(rename = "keyDataOne")]
    key_data_one: &'a str,
    #[serde(rename = "keyDataTwo")]
    key_data_two: &'a str,
    #[serde(rename = "keyDataThree")]
    key_data_three: &'a str,
    #[serde(rename = "keyDataFour")]
    key_data_four: &'a str,
}

pub struct Envelope {
    pub json: String,
    pub d: String,
    pub h: String,
    pub k: String,
    pub container: Vec<u8>,
    pub req_key: String,
}

pub struct Decrypted {
    pub business: Value,
    pub plaintext: String,
    pub expected_md5: String,
    pub actual_md5: String,
    pub encrypted: bool,
}

pub fn serialize_container(
    data_b64: &str,
    ts_ms: i64,
    key_data: [&str; 4],
    platform: i32,
) -> String {
    let payload = Container {
        data: data_b64,
        time_stamp: ts_ms,
        platform,
        key_data_one: key_data[0],
        key_data_two: key_data[1],
        key_data_three: key_data[2],
        key_data_four: key_data[3],
    };
    serde_json::to_string(&payload).expect("container serializes")
}

/// Build one request envelope.
///
/// `order` is `"insert"` (request body) or `"observed"` (headerSign input).
pub fn build_envelope(
    plain: &str,
    order: &str,
    four: &str,
    ts_ms: i64,
    platform: i32,
) -> Result<Envelope, String> {
    let key_data = [KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, four];
    let container = serialize_container(&b64e(plain.as_bytes()), ts_ms, key_data, platform);
    let _key = derive_paes_key(KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, four)?;
    let req_key = new_req_key();

    let d = b64e(&aes_cbc_encrypt(req_key.as_bytes(), container.as_bytes())?);
    let h = md5_hex(container.as_bytes());
    let k = rsa_encrypt_req_key(&req_key)?;

    let json = match order {
        "observed" => format!("{{\"k\":\"{k}\",\"p\":101,\"d\":\"{d}\",\"h\":\"{h}\",\"t\":0}}"),
        "insert" => format!("{{\"d\":\"{d}\",\"h\":\"{h}\",\"k\":\"{k}\",\"p\":101,\"t\":0}}"),
        other => {
            return Err(format!(
                "order must be 'insert' or 'observed', got {other:?}"
            ))
        }
    };

    Ok(Envelope {
        json,
        d,
        h,
        k,
        container: container.into_bytes(),
        req_key,
    })
}

/// Full two-layer decrypt of a server response.  `verify=false` keeps the
/// Python debug behaviour; the production path is fail-closed.
pub fn decrypt_response(text: &str, four: &str, verify: bool) -> Result<Decrypted, String> {
    let text = text.trim();
    let parsed: Value =
        serde_json::from_str(text).map_err(|e| format!("response is not JSON: {e}"))?;

    // some gateways wrap the real body in {"resp": "<json>"}
    let mut obj = parsed;
    if let Some(inner) = obj.get("resp").cloned() {
        obj = match inner {
            Value::String(s) => {
                serde_json::from_str(&s).map_err(|e| format!("response.resp is not JSON: {e}"))?
            }
            other => other,
        };
    }

    let has_envelope = obj.get("r").is_some() && obj.get("s").is_some() && obj.get("v").is_some();
    if !has_envelope {
        return Ok(Decrypted {
            business: obj,
            plaintext: text.to_string(),
            expected_md5: String::new(),
            actual_md5: String::new(),
            encrypted: false,
        });
    }

    let version = obj
        .get("v")
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("response v is not an integer: {:?}", obj.get("v")))?;
    if version != 101 {
        return Err(format!("unexpected response version {version} (want 101)"));
    }

    let key = derive_paes_key(KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, four)?;
    let s = obj.get("s").and_then(Value::as_str).unwrap_or("");
    let r = obj.get("r").and_then(Value::as_str).unwrap_or("");
    let expected = rsa_recover_digest(s)?;

    let layer1 = aes_cbc_decrypt(&key, &b64d(r)?)?;
    let actual = md5_hex(&layer1);
    if verify && actual != expected {
        return Err(format!(
            "response signature mismatch: md5={actual} expected={expected} \
             (signed payload may belong to another session)"
        ));
    }

    let plaintext = String::from_utf8_lossy(&layer1).to_string();
    let inner: Value =
        serde_json::from_str(&plaintext).map_err(|e| format!("inner envelope is not JSON: {e}"))?;

    let outer = match inner.get("d").and_then(Value::as_str) {
        Some(d) => {
            let layer2 = aes_cbc_decrypt(&key, &b64d(d)?)?;
            serde_json::from_str(&String::from_utf8_lossy(&layer2))
                .map_err(|e| format!("inner payload is not JSON: {e}"))?
        }
        None => inner,
    };

    let business = match outer.get("data").and_then(Value::as_str) {
        Some(data) => match b64d(data)
            .ok()
            .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
        {
            Some(v) => v,
            None => outer,
        },
        None => outer,
    };

    Ok(Decrypted {
        business,
        plaintext,
        expected_md5: expected,
        actual_md5: actual,
        encrypted: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect()
    }

    #[test]
    fn fold_vectors_match_upstream() {
        let got = [
            fold32("nhang.school"),
            fold32("5K0E8400-E29"),
            fold32("597DEA1AFB49"),
            fold32("86123.456789"),
        ];
        assert_eq!(got, [0x61297D61, 0x203A324C, 0x363A323C, 0x3D2F3D3E]);
    }

    #[test]
    fn paes_key_vector_matches_python() {
        let key = derive_paes_key(
            "nhang.school",
            "5K0E8400-E29",
            "597DEA1AFB49",
            "86123.456789",
        )
        .expect("key");
        assert_eq!(hex(&key), "faaed5a4d99af386a7d3f5d109071f13");
    }

    #[test]
    fn envelope_roundtrip_and_order() {
        let env = build_envelope(
            "{\"hello\":\"世界\"}",
            "insert",
            "86123.456789",
            1_700_000_000_000,
            2,
        )
        .expect("envelope");
        let back =
            aes_cbc_decrypt(env.req_key.as_bytes(), &b64d(&env.d).expect("d")).expect("decrypt");
        assert_eq!(back, env.container);
        assert_eq!(md5_hex(&env.container), env.h);
        assert_eq!(b64d(&env.k).expect("k").len(), 128);
        assert!(env.json.starts_with("{\"d\":"));
        let obs = build_envelope("{}", "observed", "86123.456789", 1_700_000_000_000, 2)
            .expect("observed");
        assert!(obs.json.starts_with("{\"k\":"));
    }

    #[test]
    fn container_key_order_is_fixed() {
        let text = serialize_container("AAAA", 42, ["a", "b", "c", "d"], 2);
        assert_eq!(
            text,
            "{\"data\":\"AAAA\",\"timeStamp\":42,\"platform\":2,\
             \"keyDataOne\":\"a\",\"keyDataTwo\":\"b\",\
             \"keyDataThree\":\"c\",\"keyDataFour\":\"d\"}"
        );
    }

    #[test]
    fn four_from_ms_matches_python_shape() {
        let four = key_data_four_from_ms(86123.456789);
        assert!(four.len() >= 8 && four.len() <= 12, "{four:?}");
    }
}
