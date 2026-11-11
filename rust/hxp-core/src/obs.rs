//! OBS trail object assembly.
//!
//! The record page and the replay page read two different places: the submit
//! body (see `rundata.rs`) and this gzipped object in `iydsj-hbase-hot`.  The
//! 27-key point column, the 30-second queue grouping and the lap table are
//! cross-checked against the submit body, so they are generated in one place.
//!
//! `replica/transport/obs.py` keeps the legacy `conv_point` fallback for
//! hand-built payloads; native assembly requires `run.wire_points`.

use std::io::Write;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use flate2::write::GzEncoder;
use flate2::Compression;
use serde_json::{json, Map, Value};

use crate::pyval::{py_float, py_int, py_round, py_round_n, py_str, truthy};

fn as_map<'a>(value: &'a Value, what: &str) -> Result<&'a Map<String, Value>, String> {
    value
        .as_object()
        .ok_or_else(|| format!("{what} must be an object"))
}

fn number(map: &Map<String, Value>, key: &str, default: f64) -> f64 {
    map.get(key)
        .filter(|value| !value.is_null())
        .and_then(py_float)
        .unwrap_or(default)
}

fn int_of(map: &Map<String, Value>, key: &str, default: i64) -> i64 {
    map.get(key).map(py_int).unwrap_or(default)
}

/// base64(gzip(data, 6)) — what every OBS field carries.
pub fn gz(data: &[u8]) -> String {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::new(6));
    encoder.write_all(data).expect("gzip buffer write");
    let packed = encoder.finish().expect("gzip finish");
    B64.encode(packed)
}

pub fn gz_str(text: &str) -> String {
    gz(text.as_bytes())
}

pub fn gz_json(value: &Value) -> Result<String, String> {
    let text = serde_json::to_string(value).map_err(|err| err.to_string())?;
    Ok(gz(text.as_bytes()))
}

fn quadratic(a: &Map<String, Value>, b: &Map<String, Value>) -> f64 {
    let dlat = (number(b, "lat", 0.0) - number(a, "lat", 0.0)) * 110_540.0;
    let dlng = (number(b, "lng", 0.0) - number(a, "lng", 0.0))
        * 111_320.0
        * number(a, "lat", 0.0).to_radians().cos();
    dlat.hypot(dlng)
}

/// Cumulative distance (m), time (s) and steps for every track point.
fn cumulative(run: &Map<String, Value>) -> Result<(Vec<f64>, Vec<f64>, Vec<i64>), String> {
    let locs = run
        .get("allLocJson")
        .and_then(Value::as_array)
        .ok_or("run.allLocJson must be an array")?;
    if locs.is_empty() {
        return Err("run.allLocJson must not be empty".into());
    }
    let points: Vec<&Map<String, Value>> = locs
        .iter()
        .map(|value| as_map(value, "run.allLocJson point"))
        .collect::<Result<_, _>>()?;
    let t0 = int_of(points[0], "ts", 0);
    let cum_t: Vec<f64> = points
        .iter()
        .map(|point| (((int_of(point, "ts", 0) - t0).max(0)) as f64) / 1000.0)
        .collect();
    let span = *cum_t.last().unwrap_or(&0.0);

    let mut cum_d: Option<Vec<f64>> = None;
    if let Some(pts) = run.get("locPoints").and_then(Value::as_array) {
        if pts.len() == points.len() {
            let mut vals = Vec::with_capacity(pts.len());
            let mut usable = true;
            for point in pts {
                let raw = point.get("totalDis");
                let parsed = match raw {
                    None | Some(Value::Null) => Some(0.0),
                    Some(value) => {
                        if truthy(value) {
                            py_float(value)
                        } else {
                            Some(0.0)
                        }
                    }
                };
                match parsed {
                    Some(value) => vals.push(value),
                    None => {
                        usable = false;
                        break;
                    }
                }
            }
            let monotonic = vals.windows(2).all(|pair| pair[1] >= pair[0]);
            if usable
                && vals.len() == points.len()
                && *vals.last().unwrap_or(&0.0) > 0.0
                && monotonic
            {
                let total = number(run, "juli", 0.0);
                let scale = total / vals.last().copied().unwrap_or(1.0);
                let mut scaled: Vec<f64> = vals.iter().map(|value| value * scale).collect();
                if let Some(last) = scaled.last_mut() {
                    *last = total;
                }
                cum_d = Some(scaled);
            }
        }
    }

    let total_m = number(run, "juli", 0.0);
    let cum_d = match cum_d {
        Some(values) => values,
        None if span > 0.0 => cum_t.iter().map(|t| total_m * t / span).collect(),
        None => {
            let mut raw = Vec::with_capacity(points.len());
            raw.push(0.0);
            for pair in points.windows(2) {
                raw.push(raw.last().copied().unwrap_or(0.0) + quadratic(pair[0], pair[1]));
            }
            let scale = if *raw.last().unwrap_or(&0.0) > 0.0 {
                total_m / raw.last().copied().unwrap_or(1.0)
            } else {
                0.0
            };
            raw.iter().map(|value| value * scale).collect()
        }
    };

    let span = if span <= 0.0 { 1.0 } else { span };
    let total_steps = int_of(run, "total_steps", 0);
    let mut cum_s: Option<Vec<i64>> = None;
    if let Some(windows) = run.get("step_freq_json").and_then(Value::as_array) {
        if !windows.is_empty() && cum_t.len() > 1 {
            let mut bounds: Vec<(f64, f64, f64)> = Vec::with_capacity(windows.len());
            for window in windows {
                let window = as_map(window, "step window")?;
                let begin = ((number(window, "beginTime", 0.0) - t0 as f64) / 1000.0).max(0.0);
                let end = ((number(window, "endTime", 0.0) - t0 as f64) / 1000.0).max(begin);
                let steps = number(window, "stepsNum", 0.0).max(0.0);
                bounds.push((begin, end, steps));
            }
            let mut out = Vec::with_capacity(cum_t.len());
            let mut index = 0usize;
            let mut done = 0.0f64;
            for &t in &cum_t {
                while index < bounds.len() && t >= bounds[index].1 {
                    done += bounds[index].2;
                    index += 1;
                }
                let mut extra = 0.0;
                if index < bounds.len() && t > bounds[index].0 {
                    let (begin, end, steps) = bounds[index];
                    extra = steps * (t - begin) / (end - begin).max(1e-6);
                }
                out.push(py_round(done + extra) as i64);
            }
            if let Some(last) = out.last_mut() {
                *last = total_steps;
            }
            cum_s = Some(out);
        }
    }
    let cum_s = match cum_s {
        Some(values) => values,
        None => {
            let mut values: Vec<i64> = cum_t
                .iter()
                .map(|t| py_round(total_steps as f64 * t / span) as i64)
                .collect();
            if let Some(last) = values.last_mut() {
                *last = total_steps;
            }
            values
        }
    };
    Ok((cum_d, cum_t, cum_s))
}

/// One lap per full kilometre, the last one partial.
fn build_laps(run: &Map<String, Value>, start_ms: i64) -> Result<Value, String> {
    let locs = run
        .get("allLocJson")
        .and_then(Value::as_array)
        .ok_or("run.allLocJson must be an array")?;
    if locs.is_empty() {
        return Ok(json!([]));
    }
    let points: Vec<&Map<String, Value>> = locs
        .iter()
        .map(|value| as_map(value, "run.allLocJson point"))
        .collect::<Result<_, _>>()?;
    let (cum_d, cum_t, cum_s) = cumulative(run)?;
    let total_d = number(run, "juli", 0.0);
    let alt0 = number(points[0], "alt", 0.0);
    let alts: Vec<f64> = points
        .iter()
        .map(|point| number(point, "alt", 0.0))
        .collect();

    let mut laps: Vec<Value> = Vec::new();
    let mut prev_d = 0.0f64;
    let mut prev_t = 0.0f64;
    let mut prev_steps = 0.0f64;
    let mut prev_gain = 0.0f64;
    let mut gain_total = 0.0f64;
    for index in 1..points.len() {
        gain_total += (alts[index] - alts[index - 1]).max(0.0);
    }

    let mut d_now = 0.0f64;
    while d_now < total_d {
        d_now = (d_now + 1000.0).min(total_d);
        let mut t_now = *cum_t.last().unwrap_or(&0.0);
        let mut s_now = *cum_s.last().unwrap_or(&0) as f64;
        let mut alt_now = *alts.last().unwrap_or(&0.0);
        let mut gain_now = gain_total;
        for index in 1..points.len() {
            if cum_d[index] >= d_now && cum_d[index] > cum_d[index - 1] {
                let k = (d_now - cum_d[index - 1]) / (cum_d[index] - cum_d[index - 1]);
                t_now = cum_t[index - 1] + (cum_t[index] - cum_t[index - 1]) * k;
                s_now = cum_s[index - 1] as f64 + (cum_s[index] - cum_s[index - 1]) as f64 * k;
                alt_now = alts[index - 1] + (alts[index] - alts[index - 1]) * k;
                let mut gain = 0.0f64;
                for j in 1..index {
                    gain += (alts[j] - alts[j - 1]).max(0.0);
                }
                gain_now = gain + (alt_now - alts[index - 1]).max(0.0);
                break;
            }
        }

        let lap_d = d_now - prev_d;
        let lap_t = t_now - prev_t;
        let lap_steps = s_now - prev_steps;
        let id = laps.len() as i64 + 1;
        laps.push(json!({
            "avgCadence": if lap_t > 0.0 {
                json!(py_round_n(lap_steps / (lap_t / 60.0), 2))
            } else {
                Value::Null
            },
            "avgPace": if lap_d > 0.0 {
                json!(py_round_n(lap_t / 60.0 / (lap_d / 1000.0), 4))
            } else {
                Value::Null
            },
            "avgStride": if lap_steps > 0.0 {
                json!(py_round_n(lap_d / lap_steps * 100.0, 2))
            } else {
                Value::Null
            },
            "cumulativeDuration": py_round(t_now) as i64,
            "distance": py_round_n(lap_d, 4),
            "duration": py_round(lap_t) as i64,
            "elevationGain": py_round_n(gain_now - prev_gain, 2),
            "endAltAbs": py_round_n(alt_now, 2),
            "endAltRel": py_round_n(alt_now - alt0, 2),
            "flag": start_ms,
            "id": id,
            "isFullLap": lap_d >= 1000.0,
            "lapIndex": id,
            "step": py_round(lap_steps) as i64,
        }));
        prev_d = d_now;
        prev_t = t_now;
        prev_steps = s_now;
        prev_gain = gain_now;
    }
    Ok(Value::Array(laps))
}

/// `wire.rs::build_windows` — real-device OBS window semantics.
fn obs_windows(value: &Value, start_ms: i64) -> Result<Vec<Value>, String> {
    let items = value.as_array().ok_or("windows must be an array")?;
    let id_base = start_ms.div_euclid(1000).rem_euclid(100_000);
    let mut out = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        let mut entry = as_map(item, "window")?.clone();
        if let Some(raw) = entry.get("distance").filter(|v| !v.is_null()) {
            let distance = py_float(raw).ok_or("window.distance must be numeric")?;
            entry.insert("distance".to_string(), json!(py_round_n(distance, 4)));
        }
        if let Some(raw) = entry.get("stepsNum").filter(|v| !v.is_null()) {
            let steps = py_float(raw).ok_or("window.stepsNum must be numeric")?;
            entry.insert(
                "stepsNum".to_string(),
                json!(py_round(steps).max(0.0) as i64),
            );
        }
        entry.insert("id".to_string(), json!(id_base + index as i64));
        entry.insert("queueNum".to_string(), json!(index / 3));
        entry.insert("flag".to_string(), json!(start_ms));
        out.push(Value::Object(entry));
    }
    Ok(out)
}

/// Build the 11-field object that is PUT to the signed OBS URL.
pub fn build_obs_object(
    run: &Value,
    rrid: &Value,
    uuid: &Value,
    uid: &Value,
    five_point_json: &str,
) -> Result<Value, String> {
    let run = as_map(run, "run")?;
    let locs = run
        .get("allLocJson")
        .and_then(Value::as_array)
        .ok_or("run.allLocJson must be an array")?;
    let first = locs.first().ok_or("run.allLocJson must not be empty")?;
    let start_ms = as_map(first, "run.allLocJson[0]")?
        .get("ts")
        .map(py_int)
        .ok_or("run.allLocJson[0].ts required")?;

    let wire = run
        .get("wire_points")
        .and_then(Value::as_array)
        .ok_or("run.wire_points required for native OBS assembly")?;
    if wire.is_empty() {
        return Err("run.wire_points required for native OBS assembly".into());
    }
    let mut pts: Vec<Value> = wire
        .iter()
        .map(|value| as_map(value, "wire point").map(|map| Value::Object(map.clone())))
        .collect::<Result<_, _>>()?;

    if let Some(loc_points) = run.get("locPoints").and_then(Value::as_array) {
        if loc_points.len() == pts.len() {
            for (dst, src) in pts.iter_mut().zip(loc_points.iter()) {
                let step = as_map(src, "loc point")?
                    .get("stepDistance")
                    .cloned()
                    .unwrap_or_else(|| json!(0.0));
                dst.as_object_mut()
                    .expect("wire point object")
                    .insert("stepDistance".to_string(), step);
            }
        }
    }

    let run_wrap = json!({
        "allLocJson": serde_json::to_string(&pts).map_err(|err| err.to_string())?,
        "useZip": false,
    });

    let mut out = Map::new();
    out.insert("rrid".to_string(), Value::String(gz_str(&py_str(rrid))));
    out.insert("uuid".to_string(), Value::String(gz_str(&py_str(uuid))));
    out.insert("uid".to_string(), Value::String(gz_str(&py_str(uid))));
    out.insert("run_data".to_string(), Value::String(gz_json(&run_wrap)?));
    out.insert("extension_json".to_string(), Value::String(gz_str("")));
    out.insert(
        "fixed_point_json".to_string(),
        Value::String(gz_str(five_point_json)),
    );
    out.insert("segment_json".to_string(), Value::String(gz_str("")));
    let empty = Value::Null;
    let speed = obs_windows(run.get("speed_json").unwrap_or(&empty), start_ms)?;
    out.insert(
        "speed_json".to_string(),
        Value::String(gz_json(&json!(speed))?),
    );
    let steps = obs_windows(run.get("step_freq_json").unwrap_or(&empty), start_ms)?;
    out.insert(
        "step_freq_json".to_string(),
        Value::String(gz_json(&json!(steps))?),
    );
    out.insert(
        "laps_json".to_string(),
        Value::String(gz_json(&build_laps(run, start_ms)?)?),
    );
    out.insert("runFaceCheck".to_string(), Value::String(gz_str("")));
    Ok(Value::Object(out))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    fn gunzip(text: &str) -> String {
        let raw = B64.decode(text).expect("base64");
        let mut decoder = flate2::read::GzDecoder::new(&raw[..]);
        let mut out = String::new();
        decoder.read_to_string(&mut out).expect("gzip");
        out
    }

    fn sample_run() -> Value {
        let locs = json!([
            {"lat": 22.4108, "lng": 112.9408, "ts": 1_794_411_506_893i64, "alt": 24.7, "index": 0},
            {"lat": 22.4110, "lng": 112.9414, "ts": 1_794_411_866_893i64, "alt": 25.1, "index": 1},
        ]);
        let wire = json!([
            {"avgSpeed": 0.0, "totalDis": 0.0, "stepDistance": 0.0, "id": 1, "queueNum": 0},
            {"avgSpeed": 2.5661, "totalDis": 1000.0, "stepDistance": 1000.0, "id": 2, "queueNum": 1},
        ]);
        json!({
            "allLocJson": locs,
            "locPoints": [
                {"totalDis": 0.0, "stepDistance": 0.0},
                {"totalDis": 1000.0, "stepDistance": 1000.0}
            ],
            "wire_points": wire,
            "speed_json": [{"distance": 25.68684, "id": 1, "queueNum": 0}],
            "step_freq_json": [{"stepsNum": 22.6, "id": 1, "queueNum": 0}],
            "juli": 2000,
            "total_steps": 1800,
        })
    }

    #[test]
    fn gzip_fields_round_trip() {
        let object = build_obs_object(&sample_run(), &json!(123), &json!("u"), &json!("1"), "")
            .expect("object");
        assert_eq!(object.as_object().unwrap().len(), 11);
        assert_eq!(gunzip(object["uuid"].as_str().unwrap()), "u");
        let run_data: Value =
            serde_json::from_str(&gunzip(object["run_data"].as_str().unwrap())).expect("json");
        assert_eq!(run_data["useZip"], json!(false));
        assert!(run_data["allLocJson"]
            .as_str()
            .unwrap()
            .contains("\"totalDis\":1000.0"));
    }

    #[test]
    fn windows_follow_the_30_second_queue() {
        let value = json!([{"id": 9, "queueNum": 9, "distance": 1.23456}]);
        let out = obs_windows(&value, 1_794_411_506_893).expect("windows");
        assert_eq!(out[0]["id"], json!(11_506));
        assert_eq!(out[0]["queueNum"], json!(0));
        assert_eq!(out[0]["distance"], json!(1.2346));
    }

    #[test]
    fn laps_split_at_each_kilometre() {
        let run = sample_run();
        let laps = build_laps(as_map(&run, "run").unwrap(), 1_794_411_506_893).expect("laps");
        assert_eq!(laps.as_array().unwrap().len(), 2);
        assert_eq!(laps[0]["distance"], json!(1000.0));
        assert_eq!(laps[0]["isFullLap"], json!(true));
        assert_eq!(laps[1]["isFullLap"], json!(true));
        assert_eq!(laps[1]["id"], json!(2));
    }
}
