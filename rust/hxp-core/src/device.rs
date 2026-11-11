//! Device fingerprint generation and validation.
//!
//! The wire identity is submitted on every request; the pools below keep
//! model, OS version and both resolutions in one row so a generated profile
//! cannot pair a Pixel 8 with Android 12 or a 1440p panel with a 1080p
//! physical resolution.
//!
//! File I/O, legacy migration and the UI catalog stay in Python; this module
//! only creates and checks the fingerprint itself.

use rand::seq::SliceRandom;
use rand::Rng;
use serde_json::{json, Map, Value};

use crate::pyval::{py_str, truthy};

pub const PROFILE_VERSION: i64 = 1;
const DAY_MS: i64 = 86_400_000;
const IOS_CPU_MODEL: &str = "arm64e";
const IDFA_ZERO: &str = "00000000-0000-0000-0000-000000000000";

const SOURCES: &[&str] = &[
    "generated",
    "legacy-default",
    "legacy-session",
    "rotated",
    "session",
    "migrated",
    "repaired",
    "custom",
];

/// (model, OS versions, physical pixels, logical pixels)
const ANDROID_MODELS: &[(&str, &[&str], &str, &str)] = &[
    ("22081212C", &["13", "14"], "1220x2712", "444x986"),
    ("2211133C", &["13", "14", "15"], "1080x2400", "393x873"),
    ("23127PN0CC", &["14", "15"], "1200x2670", "400x890"),
    ("PJE110", &["14", "15"], "1240x2772", "441x986"),
    ("SM-S9210", &["14", "15"], "1080x2340", "384x832"),
    ("Pixel 8", &["14", "15"], "1080x2400", "411x914"),
    ("V2309A", &["14", "15"], "1260x2800", "420x933"),
    ("23013RK75C", &["13", "14"], "1220x2712", "444x986"),
];

const IOS_MODELS: &[(&str, &[&str], &str, &str)] = &[
    (
        "iPhone 13",
        &["16.7.10", "17.7.2", "18.1"],
        "1170x2532",
        "390x844",
    ),
    (
        "iPhone 14",
        &["16.7.10", "17.7.2", "18.1"],
        "1170x2532",
        "390x844",
    ),
    (
        "iPhone 15",
        &["17.5.1", "18.0.1", "18.1", "18.2", "26.5.2"],
        "1179x2556",
        "393x852",
    ),
    (
        "iPhone 15 Pro",
        &["17.5.1", "18.0.1", "18.1", "18.2", "26.5.2"],
        "1179x2556",
        "393x852",
    ),
    (
        "iPhone 15 Pro Max",
        &["17.5.1", "18.0.1", "18.1", "18.2", "26.5.2"],
        "1290x2796",
        "430x932",
    ),
    (
        "iPhone 16",
        &["18.0.1", "18.1", "18.2", "26.5.2"],
        "1179x2556",
        "393x852",
    ),
    (
        "iPhone 16 Pro",
        &["18.0.1", "18.1", "18.2", "26.5.2"],
        "1206x2622",
        "402x874",
    ),
    (
        "iPhone 16 Pro Max",
        &["18.0.1", "18.1", "18.2", "26.5.2"],
        "1320x2868",
        "440x956",
    ),
];

fn strict_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_u64().map(|u| u as i64))
            .or_else(|| n.as_f64().map(|f| f as i64)),
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
    }
}

fn text_or(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(value) if truthy(value) => py_str(value).trim().to_string(),
        _ => default.to_string(),
    }
}

fn is_ascii_alnum(byte: u8) -> bool {
    byte.is_ascii_alphanumeric()
}

fn valid_device_id(text: &str) -> bool {
    let bytes = text.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 || !is_ascii_alnum(bytes[0]) {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|byte| is_ascii_alnum(*byte) || matches!(*byte, b'.' | b'_' | b':' | b'-'))
}

fn valid_idfa(text: &str) -> bool {
    let bytes = text.as_bytes();
    (8..=64).contains(&bytes.len())
        && bytes
            .iter()
            .all(|byte| is_ascii_alnum(*byte) || *byte == b'-')
}

fn digit_run(text: &str, min: usize, max: usize) -> bool {
    let bytes = text.as_bytes();
    (min..=max).contains(&bytes.len()) && bytes.iter().all(u8::is_ascii_digit)
}

fn valid_pixels(text: &str) -> bool {
    match text.split_once('x') {
        Some((width, height)) => digit_run(width, 3, 5) && digit_run(height, 3, 5),
        None => false,
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn uuid4_upper() -> String {
    let mut buf = [0u8; 16];
    rand::thread_rng().fill(&mut buf);
    buf[6] = (buf[6] & 0x0f) | 0x40;
    buf[8] = (buf[8] & 0x3f) | 0x80;
    let hex = hex_lower(&buf).to_uppercase();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..32]
    )
}

fn token_hex_16() -> String {
    let mut buf = [0u8; 16];
    rand::thread_rng().fill(&mut buf);
    hex_lower(&buf)
}

/// Return a normalised copy, or an error like `validate_profile`.
pub fn validate_profile(profile: &Value, now_ms: i64) -> Result<Value, String> {
    let source = profile.as_object().ok_or("设备指纹不是 JSON 对象")?;

    let version = match source.get("version") {
        None => PROFILE_VERSION,
        Some(value) => strict_int(value).ok_or("设备指纹版本无效")?,
    };
    if version > PROFILE_VERSION {
        return Err(format!(
            "设备指纹版本 {version} 高于当前程序支持的 {PROFILE_VERSION}"
        ));
    }

    let platform = text_or(source.get("platform"), "android").to_lowercase();
    if platform != "android" && platform != "ios" {
        return Err("设备指纹平台必须是 android 或 ios".into());
    }

    let device_id = text_or(source.get("DeviceId"), "");
    if !valid_device_id(&device_id) {
        return Err("DeviceId 格式无效".into());
    }

    let device_name = text_or(source.get("deviceName"), "");
    if device_name.is_empty() || device_name.chars().count() > 64 {
        return Err("deviceName 无效".into());
    }

    let os_version = text_or(source.get("osVersion"), "");
    if os_version.is_empty() || os_version.chars().count() > 16 {
        return Err("osVersion 无效".into());
    }

    let cpu_model = text_or(source.get("cpuModel"), "");
    if cpu_model.is_empty() || cpu_model.chars().count() > 32 {
        return Err("cpuModel 无效".into());
    }

    let physic = text_or(source.get("physicPixel"), "");
    let logic = text_or(source.get("logicPixel"), "");
    if !valid_pixels(&physic) || !valid_pixels(&logic) {
        return Err("分辨率格式无效".into());
    }

    let is_root = match source.get("isRoot") {
        None | Some(Value::Null) => "0".to_string(),
        Some(value) => py_str(value).trim().to_string(),
    };
    if is_root != "0" && is_root != "1" {
        return Err("isRoot 必须是 0 或 1".into());
    }

    let idfa = text_or(source.get("idfa"), "");
    if !idfa.is_empty() && !valid_idfa(&idfa) {
        return Err("idfa 格式无效".into());
    }

    let as_digits = |key: &str, label: &str| -> Result<String, String> {
        let text = match source.get(key) {
            None | Some(Value::Null) => String::new(),
            Some(value) => py_str(value).trim().to_string(),
        };
        if !digit_run(&text, 10, 16) {
            return Err(format!("{label} 不是毫秒时间戳"));
        }
        Ok(text)
    };
    let install = as_digits("appInstallTime", "appInstallTime")?;
    let update = as_digits("appUpdateTime", "appUpdateTime")?;
    if install.parse::<i64>().unwrap_or(0) > update.parse::<i64>().unwrap_or(0) {
        return Err("appInstallTime 晚于 appUpdateTime".into());
    }

    let raw_source = text_or(source.get("source"), "unknown");
    let source_name = if SOURCES.contains(&raw_source.as_str()) {
        raw_source
    } else {
        "unknown".to_string()
    };

    let created_at = match source.get("created_at").filter(|value| truthy(value)) {
        Some(value) => strict_int(value).ok_or("created_at 无效")?,
        None => now_ms,
    };

    let mut out = Map::new();
    out.insert("version".to_string(), json!(PROFILE_VERSION));
    out.insert("platform".to_string(), json!(platform));
    out.insert("source".to_string(), json!(source_name));
    out.insert("created_at".to_string(), json!(created_at));
    out.insert("DeviceId".to_string(), json!(device_id));
    out.insert("deviceName".to_string(), json!(device_name));
    out.insert("osVersion".to_string(), json!(os_version));
    out.insert("cpuModel".to_string(), json!(cpu_model));
    out.insert("physicPixel".to_string(), json!(physic));
    out.insert("logicPixel".to_string(), json!(logic));
    out.insert("isRoot".to_string(), json!(is_root));
    out.insert("appInstallTime".to_string(), json!(install));
    out.insert("appUpdateTime".to_string(), json!(update));
    if !idfa.is_empty() {
        out.insert("idfa".to_string(), json!(idfa));
    }

    for key in ["rotated_at", "custom_at", "migrated_at", "repaired_at"] {
        if let Some(value) = source.get(key).filter(|value| truthy(value)) {
            if let Some(stamp) = strict_int(value) {
                out.insert(key.to_string(), json!(stamp));
            }
        }
    }
    for key in ["rotated_from", "custom_from"] {
        if let Some(value) = source.get(key).filter(|value| truthy(value)) {
            let text: String = py_str(value).chars().take(32).collect();
            out.insert(key.to_string(), json!(text));
        }
    }
    for key in [
        "migrated_from",
        "migration_reason",
        "repaired_from",
        "repair_reason",
        "previous_device_masked",
    ] {
        if let Some(value) = source.get(key).filter(|value| truthy(value)) {
            let text: String = py_str(value).chars().take(64).collect();
            out.insert(key.to_string(), json!(text));
        }
    }
    Ok(Value::Object(out))
}

fn normalised_source(source: &str) -> &str {
    if SOURCES.contains(&source) {
        source
    } else {
        "generated"
    }
}

/// One coherent Android profile.  Never called per request.
pub fn generate_android(now_ms: i64, source: &str) -> Value {
    let mut rng = rand::thread_rng();
    let (model, versions, physic, logic) = ANDROID_MODELS.choose(&mut rng).expect("android pool");
    let os_version = versions.choose(&mut rng).expect("android versions");
    let install = now_ms - (30 + rng.gen_range(0..371)) * DAY_MS;
    let mut update = now_ms - (1 + rng.gen_range(0..21)) * DAY_MS;
    let mut install = install;
    if update <= install {
        update = install + DAY_MS;
        if update > now_ms {
            update = now_ms;
            install = now_ms - DAY_MS;
        }
    }
    json!({
        "version": PROFILE_VERSION,
        "platform": "android",
        "source": normalised_source(source),
        "created_at": now_ms,
        "DeviceId": token_hex_16(),
        "deviceName": model,
        "osVersion": os_version,
        "cpuModel": "arm64-v8a",
        "physicPixel": physic,
        "logicPixel": logic,
        "isRoot": "0",
        "appInstallTime": install.to_string(),
        "appUpdateTime": update.to_string(),
    })
}

/// One coherent iOS profile.  Never called per request.
pub fn generate_ios(now_ms: i64, source: &str) -> Value {
    let mut rng = rand::thread_rng();
    let (model, versions, physic, logic) = IOS_MODELS.choose(&mut rng).expect("ios pool");
    let os_version = versions.choose(&mut rng).expect("ios versions");
    let install = now_ms - (2 + rng.gen_range(0..60)) * DAY_MS;
    let idfa = if rng.gen_range(0..100) < 80 {
        IDFA_ZERO.to_string()
    } else {
        uuid4_upper()
    };
    json!({
        "version": PROFILE_VERSION,
        "platform": "ios",
        "source": normalised_source(source),
        "created_at": now_ms,
        "DeviceId": uuid4_upper(),
        "deviceName": model,
        "osVersion": os_version,
        "cpuModel": IOS_CPU_MODEL,
        "physicPixel": physic,
        "logicPixel": logic,
        "isRoot": "0",
        "idfa": idfa,
        "appInstallTime": install.to_string(),
        "appUpdateTime": install.to_string(),
    })
}

/// `platform` decides the pool; Python already applied `JHP_IOS_CHANCE`.
pub fn generate(now_ms: i64, source: &str, platform: &str) -> Result<Value, String> {
    match platform.trim().to_lowercase().as_str() {
        "android" => Ok(generate_android(now_ms, source)),
        "ios" => Ok(generate_ios(now_ms, source)),
        other => Err(format!(
            "device platform must be android or ios, got {other}"
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Value {
        json!({
            "version": 1,
            "platform": "android",
            "source": "generated",
            "created_at": 1_792_800_000_000i64,
            "DeviceId": "afa505ae1e60377972564c220fb24311",
            "deviceName": "2211133C",
            "osVersion": "14",
            "cpuModel": "arm64-v8a",
            "physicPixel": "1080x2400",
            "logicPixel": "393x873",
            "isRoot": "0",
            "appInstallTime": "1785436971435",
            "appUpdateTime": "1788581965711"
        })
    }

    #[test]
    fn validate_normalises_and_keeps_order() {
        let out = validate_profile(&sample(), 0).expect("valid");
        assert_eq!(out["version"], json!(1));
        assert_eq!(out["platform"], json!("android"));
        assert_eq!(out["isRoot"], json!("0"));
        let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
        assert_eq!(keys[4], "DeviceId");
        assert_eq!(keys[12], "appUpdateTime");
    }

    #[test]
    fn validate_rejects_broken_fields() {
        let mut bad = sample();
        bad["DeviceId"] = json!("-bad");
        assert!(validate_profile(&bad, 0).is_err());
        bad = sample();
        bad["physicPixel"] = json!("1080");
        assert!(validate_profile(&bad, 0).is_err());
        bad = sample();
        bad["appInstallTime"] = json!("1788581965712");
        assert!(validate_profile(&bad, 0).is_err());
        bad = sample();
        bad["platform"] = json!("windows");
        assert!(validate_profile(&bad, 0).is_err());
    }

    #[test]
    fn android_profiles_are_coherent() {
        for _ in 0..40 {
            let profile = generate_android(1_792_800_000_000, "generated");
            let row = ANDROID_MODELS
                .iter()
                .find(|row| row.0 == profile["deviceName"].as_str().unwrap())
                .expect("model row");
            assert!(row.1.contains(&profile["osVersion"].as_str().unwrap()));
            assert_eq!(profile["physicPixel"], json!(row.2));
            assert_eq!(profile["logicPixel"], json!(row.3));
            assert_eq!(profile["cpuModel"], json!("arm64-v8a"));
            let install: i64 = profile["appInstallTime"].as_str().unwrap().parse().unwrap();
            let update: i64 = profile["appUpdateTime"].as_str().unwrap().parse().unwrap();
            assert!(update > install);
            assert!(install < 1_792_800_000_000);
        }
    }

    #[test]
    fn ios_profiles_use_arm64e_and_a_valid_uuid() {
        for _ in 0..40 {
            let profile = generate_ios(1_792_800_000_000, "generated");
            assert_eq!(profile["cpuModel"], json!("arm64e"));
            let device = profile["DeviceId"].as_str().unwrap();
            assert_eq!(device.len(), 36);
            assert!(device.chars().all(|c| c.is_ascii_hexdigit() || c == '-'));
            assert_eq!(device.chars().filter(|c| *c == '-').count(), 4);
            assert_eq!(profile["appInstallTime"], profile["appUpdateTime"]);
        }
    }

    #[test]
    fn platform_is_case_insensitive() {
        assert_eq!(
            generate(0, "generated", "iOS").unwrap()["platform"],
            json!("ios")
        );
        assert!(generate(0, "generated", "windows").is_err());
    }
}
