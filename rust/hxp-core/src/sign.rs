//! Request-signing primitives shared by Android and iOS clients.
//!
//! Values are accepted as strings: callers must stringify numbers/bools the
//! same way the Python `crypto.official_sign` does before crossing the FFI.

use std::collections::BTreeMap;

use crate::netsec::md5_hex;

pub const TOKEN_SIGN_SALT: &str = "&wh2016_swcampus";
/// Shared by the iOS token suffix, the Android `SALT5` token sign and the
/// campus-checkpoint URL sign.  Kept here so the Python release unit never
/// carries the plaintext.
pub const RUNSCHOOL_SECRET: &str = "2slhe02lsfiwowlcixisla_sls-_slaor";
pub const IOS_TOKEN_SIGN_SUFFIX: &str = RUNSCHOOL_SECRET;
/// Submit-signature salt stand-in (native SportJniUtils.getSecretKey(5)).
pub const SIGN_SALT_STANDIN: &str = RUNSCHOOL_SECRET;

// Endpoint / app identity ownership.  The Python side asks for these through
// the `protocol_constants` opcode so a packed `好想跑.exe` stays clean.
pub const APP_ID: &str = "prj7sr2xk2bntjccmzbjckps7m9ogmlz";
pub const RUNEC_KEY: &str = "d8dba5641eace5ea41cc502ae30d3bb2";
pub const HOST: &str = "run.gxapp.iydsj.com";

/// SignCode's case-insensitive link string; `sign` is filtered out.
pub fn official_link_string(params: &BTreeMap<String, String>) -> String {
    let mut items: Vec<(&String, &String)> = params
        .iter()
        .filter(|(k, _)| !k.eq_ignore_ascii_case("sign"))
        .collect();
    items.sort_by_key(|(k, _)| k.to_ascii_lowercase());
    items
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("&")
}

pub fn official_sign(params: &BTreeMap<String, String>, salt: &str) -> String {
    md5_hex(format!("{}{salt}", official_link_string(params)).as_bytes())
}

pub fn token_sign_android(ts: &str, token: &str, uid: &str, salt: &str) -> String {
    let mut params = BTreeMap::new();
    params.insert("timeStamp".to_string(), ts.to_string());
    params.insert("token".to_string(), token.to_string());
    params.insert("uid".to_string(), uid.to_string());
    official_sign(&params, salt)
}

pub fn token_sign_ios(ts: &str, token: &str, uid: &str, suffix: &str) -> String {
    let link = format!("timeStamp={ts}&token={token}&uid={uid}");
    md5_hex(format!("{link}{suffix}").as_bytes())
}

pub fn runschool_sign(params: &BTreeMap<String, String>) -> String {
    let query = params
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("&");
    md5_hex(query.as_bytes())
}

/// Campus checkpoint sign: `md5("http://" + host + path + salt)`.
pub fn points_sign(host: &str, path: &str) -> String {
    md5_hex(format!("http://{}{}{}", host, path, RUNSCHOOL_SECRET).as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn android_token_sign_matches_python() {
        assert_eq!(
            token_sign_android("1700000000000", "abc123", "2023110421", TOKEN_SIGN_SALT),
            "7fd46d4c3bac075da93ce694a45c19db"
        );
    }

    #[test]
    fn ios_token_sign_matches_python() {
        assert_eq!(
            token_sign_ios(
                "1700000000000",
                "abc123",
                "2023110421",
                IOS_TOKEN_SIGN_SUFFIX
            ),
            "1fe038ba08262f0184a34c2a8e4c9466"
        );
    }

    #[test]
    fn runschool_sign_matches_python() {
        let mut params = BTreeMap::new();
        params.insert("b".to_string(), "2".to_string());
        params.insert("a".to_string(), "1".to_string());
        assert_eq!(runschool_sign(&params), "ed04c91cf6f6ab5a01a31c0295c5da34");
    }

    #[test]
    fn points_sign_matches_python() {
        assert_eq!(
            points_sign("run.gxapp.iydsj.com", "/api/v560/get/1/distance/2000"),
            "45732c140c185c94e096316a297a6065"
        );
    }

    #[test]
    fn official_sign_filters_sign_key_and_sorts_case_insensitively() {
        let mut params = BTreeMap::new();
        params.insert("timeStamp".to_string(), "1700000000000".to_string());
        params.insert("token".to_string(), "abc123".to_string());
        params.insert("uid".to_string(), "2023110421".to_string());
        params.insert("sign".to_string(), "ignored".to_string());
        assert_eq!(
            official_sign(&params, TOKEN_SIGN_SALT),
            "7fd46d4c3bac075da93ce694a45c19db"
        );
    }
}
