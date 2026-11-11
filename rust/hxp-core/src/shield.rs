//! One-way fragment mixing for the distributed device credential.
//!
//! This layer never sees a token, a nonce it did not receive, or any file:
//! it folds the Go fragment and the nonce into 32 bytes with two integer
//! hashes plus SHA-256.  The result is an input to the C++ shield, not a
//! credential by itself.

use sha2::{Digest, Sha256};

/// FNV-1a 64-bit offset basis.
const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
/// FNV-1a 64-bit prime.
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;
/// Polynomial rolling constant (mod 2^64).
const POLY_PRIME: u64 = 0x9e37_79b9_7f4a_7c15;
/// Seed for the nonce bit mixer.
const NONCE_SEED: u64 = 0x243f_6a88_85a3_08d3;

/// Mix `blob` and `nonce` into 32 opaque bytes.
///
/// Steps:
///   * FNV-1a 64 over `blob`,
///   * polynomial rolling hash over `blob` (wrapping, mod 2^64),
///   * a separate rotate/multiply mixer over `nonce`,
///   * `sha256(state_le || nonce || blob)`.
pub fn mix(blob: &[u8], nonce: &[u8]) -> [u8; 32] {
    let mut fnv = FNV_OFFSET;
    let mut poly: u64 = 1;
    for &byte in blob {
        fnv ^= u64::from(byte);
        fnv = fnv.wrapping_mul(FNV_PRIME);
        poly = poly.wrapping_mul(POLY_PRIME).wrapping_add(u64::from(byte) + 1);
    }

    let mut bits = NONCE_SEED;
    for &byte in nonce {
        bits = (bits ^ u64::from(byte))
            .rotate_left(13)
            .wrapping_mul(FNV_PRIME);
    }
    bits ^= bits >> 29;
    bits = bits.wrapping_mul(0xbf58_476d_1ce4_e5b9);
    bits ^= bits >> 32;

    let state = fnv ^ poly.rotate_left(23) ^ bits;

    let mut hasher = Sha256::new();
    hasher.update(state.to_le_bytes());
    hasher.update(nonce);
    hasher.update(blob);
    let digest = hasher.finalize();

    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    out
}

#[cfg(test)]
mod tests {
    use super::mix;

    const BLOB: [u8; 32] = [
        0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd,
        0xee, 0xff, 0x10, 0x21, 0x32, 0x43, 0x54, 0x65, 0x76, 0x87, 0x98, 0xa9, 0xba, 0xcb,
        0xdc, 0xed, 0xfe, 0x0f,
    ];
    const NONCE: [u8; 16] = [
        0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54,
        0x32, 0x10,
    ];
    const FIXED_HEX: &str = "c80c71835f35eeb7c87b4871049c191b33cdf613a9b79ab9208115e387856597";

    #[test]
    fn fixed_vector_matches_the_implementation() {
        // Pinned so an accidental change to either hash is visible; the
        // value carries no protocol meaning and is never sent anywhere.
        let got = mix(&BLOB, &NONCE);
        let hex: String = got.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(hex, FIXED_HEX);
    }

    #[test]
    fn a_different_nonce_changes_the_output() {
        let mut other = NONCE;
        other[0] ^= 0x80;
        assert_ne!(mix(&BLOB, &NONCE), mix(&BLOB, &other));
    }

    #[test]
    fn empty_blob_is_handled_and_deterministic() {
        let first = mix(&[], &NONCE);
        let second = mix(&[], &NONCE);
        assert_eq!(first, second);
        assert_ne!(first, mix(&[0x00], &NONCE));
    }

    #[test]
    fn empty_nonce_is_handled() {
        let first = mix(&BLOB, &[]);
        assert_eq!(first, mix(&BLOB, &[]));
    }
}
