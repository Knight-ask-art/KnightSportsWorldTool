//! Official upload body assembly (`UploadFormatEntity`).
//!
//! Byte-compatible with `replica/domain/rundata.py::_build_upload_entity`.
//! Field order, the Gson-style nested JSON strings and the two rounds in
//! `speed` all matter: the business server scores the ticket and silently
//! answers `10003` when the shape drifts.
//!
//! The Python host still owns the run simulation; only the final mapping from
//! a forged run to the submit body lives here.

use serde_json::{json, Map, Value};

fn as_map<'a>(value: &'a Value, what: &str) -> Result<&'a Map<String, Value>, String> {
    value
        .as_object()
        .ok_or_else(|| format!("{what} must be an object"))
}

/// Python truthiness for the value shapes that appear in run/ctx.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `int(x)` for the value shapes that appear in run/ctx.
fn py_int(value: &Value) -> i64 {
    match value {
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(u) = n.as_u64() {
                u as i64
            } else {
                n.as_f64().unwrap_or(0.0) as i64
            }
        }
        Value::Bool(flag) => i64::from(*flag),
        Value::String(text) => {
            let trimmed = text.trim();
            if let Ok(i) = trimmed.parse::<i64>() {
                i
            } else if let Ok(f) = trimmed.parse::<f64>() {
                f as i64
            } else {
                0
            }
        }
        _ => 0,
    }
}

/// Python `round(x)` — nearest integer, ties to even.
fn py_round(value: f64) -> f64 {
    let floor = value.floor();
    if value - floor == 0.5 {
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        value.round()
    }
}

/// Python `round(x, 2)`: Rust's fixed formatting rounds the exact binary value
/// with ties to even, which is the rule CPython uses.
fn py_round2(value: f64) -> f64 {
    format!("{value:.2}").parse().unwrap_or(value)
}

fn get_or(map: &Map<String, Value>, key: &str, default: Value) -> Value {
    map.get(key).cloned().unwrap_or(default)
}

fn get_int(map: &Map<String, Value>, key: &str, default: i64) -> i64 {
    map.get(key).map(py_int).unwrap_or(default)
}

/// `src/api/submit.rs::submit_windows` — submit ids differ from OBS ids.
fn submit_windows(value: &Value) -> Result<Value, String> {
    let items = value.as_array().ok_or("submit windows must be an array")?;
    let mut out = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        let mut entry = as_map(item, "submit window")?.clone();
        // `Map::insert` keeps the position of an existing key, exactly like a
        // Python dict assignment; new keys append.
        entry.insert("id".to_string(), json!(60000 + index));
        entry.insert("queueNum".to_string(), json!(index));
        out.push(Value::Object(entry));
    }
    Ok(Value::Array(out))
}

/// Serialise nested run data the way `json.dumps` does: compact separators and
/// `ensure_ascii=True`.  Non-ASCII can only occur inside string literals, so a
/// character-wise pass is safe.
fn gson_json(value: &Value) -> Result<String, String> {
    let text = serde_json::to_string(value).map_err(|err| err.to_string())?;
    if text.is_ascii() {
        return Ok(text);
    }
    let mut out = String::with_capacity(text.len() + 16);
    for ch in text.chars() {
        if ch.is_ascii() {
            out.push(ch);
        } else {
            let mut buf = [0u16; 2];
            for unit in ch.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    Ok(out)
}

/// Map a forged run plus submit context to the official body.
pub fn build_upload_entity(run: &Value, ctx: &Value) -> Result<Value, String> {
    let run = as_map(run, "run")?;
    let ctx = ctx.as_object().cloned().unwrap_or_default();

    let locs = run
        .get("allLocJson")
        .and_then(Value::as_array)
        .ok_or("run.allLocJson must be an array")?;
    let first = locs.first().ok_or("run.allLocJson must not be empty")?;
    let first = as_map(first, "run.allLocJson[0]")?;
    let last = locs.last().ok_or("run.allLocJson must not be empty")?;
    let last = as_map(last, "run.allLocJson[-1]")?;
    let first_ts = first.get("ts").ok_or("run.allLocJson[0].ts required")?;
    let last_ts = last.get("ts").ok_or("run.allLocJson[-1].ts required")?;

    let sport_time = run.get("sportTime").ok_or("run.sportTime required")?;
    let juli = run.get("juli").ok_or("run.juli required")?;
    let sport_seconds = py_int(sport_time) as f64;
    let meters = py_int(juli) as f64;
    // UploadFormatEntity.speed is the AVERAGE PACE in minutes/km, stored as
    // round(pace, 2) * 1000.
    let min_per_km = (sport_seconds / 60.0) / f64::max(0.001, meters / 1000.0);

    let stop_time = match run.get("wall_stop_ms") {
        Some(value) if truthy(value) => py_int(value),
        _ => py_int(last_ts),
    };
    let start_time = py_int(first_ts);

    let steps = get_int(run, "total_steps", 0);
    let denominator = f64::max(1.0, py_int(sport_time) as f64);
    let avg_step_freq = (py_round(steps as f64 / denominator * 60.0) as i64).max(1);

    let mut ent = Map::new();
    ent.insert("address".to_string(), get_or(&ctx, "address", json!("")));
    ent.insert(
        "allLocJson".to_string(),
        Value::String(gson_json(
            run.get("locJsonEntity")
                .ok_or("run.locJsonEntity required")?,
        )?),
    );
    ent.insert(
        "avgPower".to_string(),
        json!(run
            .get("avgPower")
            .filter(|value| truthy(value))
            .map(py_int)
            .unwrap_or(0)),
    );
    ent.insert("avgStepFreq".to_string(), json!(avg_step_freq));
    ent.insert(
        "calorie".to_string(),
        json!(run
            .get("calorie")
            .filter(|value| truthy(value))
            .map(py_int)
            .unwrap_or(0)),
    );
    ent.insert("complete".to_string(), json!(true));
    ent.insert("errorCode".to_string(), json!(0));
    ent.insert("faceCheck".to_string(), get_or(&ctx, "faceCheck", json!(0)));
    ent.insert(
        "fivePointJson".to_string(),
        Value::String(gson_json(
            run.get("fivePointJson")
                .ok_or("run.fivePointJson required")?,
        )?),
    );
    ent.insert("geeToken".to_string(), get_or(&ctx, "geeToken", json!("")));
    ent.insert("getPrize".to_string(), json!(false));
    ent.insert("goalId".to_string(), Value::Null);
    ent.insert("isUpload".to_string(), json!(false));
    ent.insert(
        "latitude".to_string(),
        first
            .get("lat")
            .cloned()
            .ok_or("run.allLocJson[0].lat required")?,
    );
    ent.insert(
        "longitude".to_string(),
        first
            .get("lng")
            .cloned()
            .ok_or("run.allLocJson[0].lng required")?,
    );
    ent.insert("maxRunTime".to_string(), json!(0));
    ent.insert("minSteps".to_string(), json!(0));
    ent.insert("more".to_string(), json!(false));
    ent.insert("originalSign".to_string(), json!(""));
    ent.insert("policy".to_string(), get_or(&ctx, "policy", json!(1)));
    ent.insert(
        "selDistance".to_string(),
        get_or(&ctx, "selDistance", juli.clone()),
    );
    ent.insert("selRunTime".to_string(), sport_time.clone());
    ent.insert(
        "selectedUnid".to_string(),
        get_or(&ctx, "selectedUnid", json!(0)),
    );
    ent.insert("signature".to_string(), json!(""));
    ent.insert(
        "speed".to_string(),
        json!(py_round(py_round2(min_per_km) * 1000.0) as i64),
    );
    ent.insert(
        "speedPerTenSec".to_string(),
        submit_windows(run.get("speed_json").ok_or("run.speed_json required")?)?,
    );
    ent.insert("sportType".to_string(), get_or(&ctx, "sportType", json!(1)));
    ent.insert("startTime".to_string(), json!(start_time));
    ent.insert("status".to_string(), json!(0));
    ent.insert(
        "stepsPerTenSec".to_string(),
        submit_windows(
            run.get("step_freq_json")
                .ok_or("run.step_freq_json required")?,
        )?,
    );
    ent.insert("stopTime".to_string(), json!(stop_time));
    ent.insert("themeId".to_string(), json!(0));
    ent.insert(
        "totalAscent".to_string(),
        json!(run
            .get("totalAscent")
            .filter(|value| truthy(value))
            .map(py_int)
            .unwrap_or(0)),
    );
    ent.insert("totalDis".to_string(), juli.clone());
    ent.insert(
        "totalSteps".to_string(),
        get_or(run, "total_steps", json!(0)),
    );
    ent.insert("totalTime".to_string(), sport_time.clone());
    ent.insert("uid".to_string(), get_or(&ctx, "uid", json!("")));
    ent.insert("unCompleteReason".to_string(), json!(0));
    ent.insert("unauthorized".to_string(), json!(0));
    ent.insert("useMobilityTools".to_string(), json!(0));
    ent.insert(
        "uuid".to_string(),
        run.get("sportunid")
            .cloned()
            .ok_or("run.sportunid required")?,
    );
    ent.insert("validDis".to_string(), juli.clone());
    ent.insert("validTime".to_string(), sport_time.clone());

    // roomId only exists for indoor runs (src/crypto/sign.rs: sportType=3).
    let indoor = ent
        .get("sportType")
        .and_then(Value::as_f64)
        .map(|value| value == 3.0)
        .unwrap_or(false);
    if indoor {
        ent.insert("roomId".to_string(), get_or(&ctx, "roomId", json!(0)));
    }
    Ok(Value::Object(ent))
}

pub fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(data)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = include_str!("../tests/fixtures/upload_entity.json");

    #[test]
    fn upload_entity_matches_the_python_fixture() {
        let fixture: Value = serde_json::from_str(FIXTURE).expect("fixture json");
        let ent = build_upload_entity(&fixture["run"], &fixture["ctx"]).expect("entity");
        let text = serde_json::to_string(&ent).expect("serialise");
        assert_eq!(sha256_hex(text.as_bytes()), fixture["expected_sha256"]);
    }

    #[test]
    fn submit_windows_keeps_existing_key_positions() {
        let fixture: Value = serde_json::from_str(FIXTURE).expect("fixture json");
        let ent = build_upload_entity(&fixture["run"], &fixture["ctx"]).expect("entity");
        let first = &ent["speedPerTenSec"][0];
        assert_eq!(first["id"], json!(60000));
        assert_eq!(first["queueNum"], json!(0));
        assert_eq!(first["beginTime"], json!(1));
        // insertion order is part of the protocol shape
        let keys: Vec<&String> = first.as_object().unwrap().keys().collect();
        assert_eq!(keys[0], "beginTime");
        assert_eq!(keys[4], "id");
        assert_eq!(keys[5], "queueNum");
    }

    #[test]
    fn indoor_runs_get_room_id_last() {
        let mut fixture: Value = serde_json::from_str(FIXTURE).expect("fixture json");
        fixture["ctx"]["sportType"] = json!(3);
        fixture["ctx"]["roomId"] = json!(42);
        let ent = build_upload_entity(&fixture["run"], &fixture["ctx"]).expect("entity");
        assert_eq!(ent["roomId"], json!(42));
        let keys: Vec<&String> = ent.as_object().unwrap().keys().collect();
        assert_eq!(keys.last().copied(), Some(&"roomId".to_string()));
    }
}
