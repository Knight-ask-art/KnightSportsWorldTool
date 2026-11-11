//! GeeTest v4 `w` payload construction.
//!
//! Byte-compatible with `replica/transport/gt4_payload.py`; the Python golden
//! digest (`6a2713d3…`) is asserted in the tests so a refactor cannot change a
//! single byte.  No network calls live here.

use md5::{Digest, Md5};
use num_bigint_dig::BigUint;
use rand::Rng;
use serde_json::{json, Map, Value};
use sha2::Sha256;

pub const TRACK_WIDTH: f64 = 340.0;
pub const USERRESPONSE_RATIO: f64 = 1.005_946_666_666_666_5;
pub const IMG_REF_WIDTH: f64 = TRACK_WIDTH * USERRESPONSE_RATIO;
pub const NORMAL_WIDTH_MIN: f64 = 280.0;
pub const NORMAL_WIDTH_MAX: f64 = 420.0;

pub const GOLDEN_LOT: &str = "0123456789abcdef0123456789abcdef";
pub const GOLDEN_POW: &str = "v1|5|sha256|2026-09-09T00:00:00|cid|lot||deadbeefdeadbeef";
pub const GOLDEN_SHA256: &str = "6a2713d391718ba2e3fde333d4278b72f8217da15a85597002d7341f64145f5f";

fn hex16() -> String {
    let mut rng = rand::thread_rng();
    (0..16)
        .map(|_| "0123456789abcdef".as_bytes()[rng.gen_range(0..16)] as char)
        .collect()
}

/// Four 4-hex chunks, the format GeeTest's AES key generator produces.
pub fn str16() -> String {
    let mut rng = rand::thread_rng();
    let mut out = String::with_capacity(16);
    for _ in 0..4 {
        let v = (65536.0 * (1.0 + rng.gen::<f64>())) as u64;
        let text = format!("{v:x}");
        out.push_str(&text[1..5]);
    }
    out
}

fn leading_zero_bits(digest: &[u8]) -> u32 {
    let mut total = 0;
    for &byte in digest {
        if byte == 0 {
            total += 8;
            continue;
        }
        total += byte.leading_zeros();
        break;
    }
    total
}

fn py_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_string(),
        Some(Value::Number(n)) => n.to_string(),
        Some(other) => other.to_string(),
    }
}

fn bits_number(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        Some(Value::Bool(_)) => 0,
        _ => 0,
    }
}

/// Find a nonce that satisfies the PoW the server asked for.
pub fn pow_msg(pow_detail: &Value, captcha_id: &str, lot_number: &str) -> String {
    let bits_display = py_str(pow_detail.get("bits"));
    let bits = bits_number(pow_detail.get("bits"));
    let func = pow_detail
        .get("hashfunc")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_lowercase();
    let hashfunc_display = py_str(pow_detail.get("hashfunc"));
    let version = py_str(pow_detail.get("version"));
    let datetime = py_str(pow_detail.get("datetime"));
    let base = format!(
        "{version}|{bits_display}|{hashfunc_display}|{datetime}|{captcha_id}|{lot_number}||"
    );
    if bits <= 0 {
        return format!("{base}{}", hex16());
    }
    loop {
        let nonce = hex16();
        let text = format!("{base}{nonce}");
        let ok = if func == "md5" {
            let digest = Md5::digest(text.as_bytes());
            leading_zero_bits(&digest) as i64 >= bits
        } else {
            let digest = Sha256::digest(text.as_bytes());
            leading_zero_bits(&digest) as i64 >= bits
        };
        if ok {
            return text;
        }
    }
}

pub fn dynamic_key(lot_number: &str) -> String {
    let chars: Vec<char> = lot_number.chars().collect();
    let slice = |from: usize, to: usize| -> String {
        chars
            .iter()
            .skip(from)
            .take(to.saturating_sub(from))
            .collect()
    };
    format!("{}{}", slice(26, 30), slice(7, 11))
}

pub fn dynamic_value(lot_number: &str) -> String {
    lot_number.chars().skip(6).take(8).collect()
}

pub fn expected_keys(lot_number: &str) -> Vec<String> {
    [
        "setLeft",
        "passtime",
        "userresponse",
        "device_id",
        "lot_number",
        "pow_msg",
        "pow_sign",
        "geetest",
        "lang",
        "ep",
        "biht",
        "gee_guard",
        "YciC",
        &dynamic_key(lot_number),
        "em",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

/// Image-pixel gap -> slider-track coordinate.
pub fn userresponse(set_left_px: i64, bg_width: Option<f64>) -> f64 {
    let mut x = set_left_px as f64;
    if let Some(width) = bg_width {
        if width > 0.0 && (width < NORMAL_WIDTH_MIN || width > NORMAL_WIDTH_MAX) {
            x *= IMG_REF_WIDTH / width;
        }
    }
    x / USERRESPONSE_RATIO + 2.0
}

pub fn passtime_for(distance_px: i64) -> i64 {
    let base = 900.0 + 4.6 * distance_px.max(0) as f64;
    let mut rng = rand::thread_rng();
    let value = (base + rng.gen_range(0.0..420.0)).min(3400.0);
    value as i64
}

/// Serialise the `w` plaintext exactly as GeeTest expects it.
pub fn payload_json(
    set_left: i64,
    lot_number: &str,
    pow_msg: &str,
    pow_sign: &str,
    bg_width: Option<f64>,
    passtime: Option<i64>,
) -> Result<String, String> {
    let passtime = passtime.unwrap_or_else(|| passtime_for(set_left));
    let key = dynamic_key(lot_number);
    let mut m = Map::new();
    m.insert("setLeft".to_string(), json!(set_left));
    m.insert("passtime".to_string(), json!(passtime));
    m.insert(
        "userresponse".to_string(),
        json!(userresponse(set_left, bg_width)),
    );
    m.insert("device_id".to_string(), json!(""));
    m.insert("lot_number".to_string(), json!(lot_number));
    m.insert("pow_msg".to_string(), json!(pow_msg));
    m.insert("pow_sign".to_string(), json!(pow_sign));
    m.insert("geetest".to_string(), json!("captcha"));
    m.insert("lang".to_string(), json!("zh"));
    m.insert("ep".to_string(), json!("123"));
    m.insert("biht".to_string(), json!("1426265548"));
    m.insert(
        "gee_guard".to_string(),
        json!({"roe": {
            "aup": "3", "sep": "3", "egp": "3", "auh": "3",
            "rew": "3", "snh": "3", "res": "3", "cdc": "3",
        }}),
    );
    m.insert("YciC".to_string(), json!("P3Vn"));
    m.insert(key, json!(dynamic_value(lot_number)));
    m.insert(
        "em".to_string(),
        json!({"ph": 0, "cp": 0, "ek": "11", "wd": 1,
               "nt": 0, "si": 0, "sc": 0}),
    );
    let got: Vec<String> = m.keys().cloned().collect();
    let want = expected_keys(lot_number);
    if got != want {
        return Err(format!("GeeTest w 字段顺序变了: {got:?} (want {want:?})"));
    }
    serde_json::to_string(&m).map_err(|e| e.to_string())
}

/// AES-128-CBC, IV = ascii "0"*16, PKCS#7, lowercase hex.
pub fn aes_hex(plaintext: &str, key: &str) -> Result<String, String> {
    use aes::cipher::{BlockEncryptMut, KeyIvInit};
    let iv = b"0000000000000000";
    let mut buf = plaintext.as_bytes().to_vec();
    let pad = 16 - (buf.len() % 16);
    buf.extend(std::iter::repeat(pad as u8).take(pad));
    let mut enc = cbc::Encryptor::<aes::Aes128>::new_from_slices(key.as_bytes(), iv)
        .map_err(|e| e.to_string())?;
    for chunk in buf.chunks_exact_mut(16) {
        enc.encrypt_block_mut(aes::cipher::generic_array::GenericArray::from_mut_slice(
            chunk,
        ));
    }
    Ok(buf.iter().map(|b| format!("{b:02x}")).collect())
}

/// PKCS#1 v1.5 type-2 padding, then raw RSA; lower-case hex.
pub fn rsa_encrypt_hex(data: &[u8], modulus_hex: &str) -> Result<String, String> {
    let n =
        BigUint::parse_bytes(modulus_hex.trim().as_bytes(), 16).ok_or("GT4 modulus must be hex")?;
    let k = ((n.bits() + 7) / 8) as usize;
    if data.len() > k.saturating_sub(11) {
        return Err("RSA payload too long".into());
    }
    let mut rng = rand::thread_rng();
    let ps_len = k - 3 - data.len();
    let mut em = Vec::with_capacity(k);
    em.extend_from_slice(&[0x00, 0x02]);
    em.extend((0..ps_len).map(|_| rng.gen_range(1u8..=255u8)));
    em.push(0x00);
    em.extend_from_slice(data);
    let c = BigUint::from_bytes_be(&em).modpow(&BigUint::from(65537u32), &n);
    let bytes = c.to_bytes_be();
    let mut out = vec![0u8; k.saturating_sub(bytes.len())];
    out.extend_from_slice(&bytes);
    Ok(out.iter().map(|b| format!("{b:02x}")).collect())
}

/// `hex(AES(payload)) || hex(RSA(str16))` — the value of `w`.
#[allow(clippy::too_many_arguments)]
pub fn build_w(
    set_left: i64,
    lot_number: &str,
    pow_msg: &str,
    pow_sign: &str,
    str16_key: &str,
    bg_width: Option<f64>,
    passtime: Option<i64>,
    modulus_hex: &str,
) -> Result<String, String> {
    let plaintext = payload_json(set_left, lot_number, pow_msg, pow_sign, bg_width, passtime)?;
    let aes = aes_hex(&plaintext, str16_key)?;
    let rsa = rsa_encrypt_hex(str16_key.as_bytes(), modulus_hex)?;
    Ok(format!("{aes}{rsa}"))
}

pub fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn golden_payload_is_byte_identical() {
        let payload =
            payload_json(181, GOLDEN_LOT, GOLDEN_POW, "sign", None, Some(1887)).expect("payload");
        assert_eq!(sha256_hex(payload.as_bytes()), GOLDEN_SHA256);
    }

    #[test]
    fn dynamic_slices_match_python() {
        assert_eq!(dynamic_key(GOLDEN_LOT), "abcd789a");
        assert_eq!(dynamic_value(GOLDEN_LOT), "6789abcd");
        assert_eq!(expected_keys(GOLDEN_LOT).len(), 15);
    }

    #[test]
    fn userresponse_keeps_normal_render_and_rescales_retina() {
        let normal = userresponse(181, Some(342.0));
        let legacy = 181.0 / USERRESPONSE_RATIO + 2.0;
        assert!((normal - legacy).abs() < 1e-9);
        let doubled = userresponse(181, Some(684.0));
        assert!(doubled > 80.0 && doubled < 100.0);
    }

    #[test]
    fn pow_prefix_and_tail_shape() {
        let msg = pow_msg(
            &json!({"version": "v1", "bits": 5, "hashfunc": "sha256",
                    "datetime": "2026-09-09T00:00:00"}),
            "cid",
            "lot",
        );
        assert!(msg.starts_with("v1|5|sha256|2026-09-09T00:00:00|cid|lot||"));
        assert_eq!(msg.rsplit('|').next().unwrap().len(), 16);
    }

    #[test]
    fn w_is_hex_and_longer_than_the_rsa_blob() {
        // A tiny valid modulus is impossible; use a 1024-bit odd number so the
        // shape checks exercise AES+RSA without the real GeeTest key.
        let modulus = format!("{:x}", (BigUint::from(1u32) << 1023) + BigUint::from(1u32));
        let w = build_w(
            123,
            GOLDEN_LOT,
            GOLDEN_POW,
            "sign",
            "0123456789abcdef",
            None,
            Some(1887),
            &modulus,
        )
        .expect("w");
        assert!(w.chars().all(|c| c.is_ascii_hexdigit()));
        assert!(w.len() > 260);
    }
}
