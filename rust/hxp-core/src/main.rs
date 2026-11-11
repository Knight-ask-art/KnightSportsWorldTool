//! Line-oriented JSON sidecar for the Python host.
//!
//! One request per line:  {"id":1,"op":"build_envelope","args":{...}}
//! One reply per line:    {"id":1,"ok":true,"data":{...}}
//!
//! Staying stateless keeps the protocol simple and lets the Python side decide
//! when to latch `keyDataFour`; the salts and RSA operations stay here.

use std::collections::BTreeMap;
use std::io::{self, BufRead, Write};

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use hxp_core::{device, gt4, netsec, obs, runcore, rundata, sign};
use serde_json::{json, Value};

fn string_map(value: Option<&Value>) -> Result<BTreeMap<String, String>, String> {
    let obj = value
        .and_then(Value::as_object)
        .ok_or("params object required")?;
    let mut out = BTreeMap::new();
    for (key, raw) in obj {
        let text = match raw {
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => {
                if *b {
                    "True".to_string()
                } else {
                    "False".to_string()
                }
            }
            Value::Null => continue,
            other => return Err(format!("param {key} must be scalar, got {other}")),
        };
        out.insert(key.clone(), text);
    }
    Ok(out)
}

fn arg_str<'a>(args: &'a Value, name: &str) -> Result<&'a str, String> {
    args.get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{name} required"))
}

fn handle(req: &Value) -> Result<Value, String> {
    let op = req.get("op").and_then(Value::as_str).ok_or("missing op")?;
    let args = req.get("args").cloned().unwrap_or(Value::Null);

    match op {
        "ping" => Ok(json!({"pong": true, "version": hxp_core::VERSION})),

        "build_envelope" => {
            let plain = arg_str(&args, "plain")?;
            let order = args
                .get("order")
                .and_then(Value::as_str)
                .unwrap_or("insert");
            let four = arg_str(&args, "four")?;
            let ts_ms = args
                .get("ts_ms")
                .and_then(Value::as_i64)
                .ok_or("ts_ms required")?;
            let platform = args
                .get("platform")
                .and_then(Value::as_i64)
                .unwrap_or(netsec::DEFAULT_PLATFORM as i64) as i32;
            let env = netsec::build_envelope(plain, order, four, ts_ms, platform)?;
            Ok(json!({
                "json": env.json,
                "d": env.d,
                "h": env.h,
                "k": env.k,
                "container": String::from_utf8_lossy(&env.container),
                "req_key": env.req_key,
                "ts_ms": ts_ms,
                "key_data": [netsec::KEYDATA_ONE, netsec::KEYDATA_TWO,
                             netsec::KEYDATA_THREE, four],
            }))
        }

        "decrypt_response" => {
            let raw = arg_str(&args, "raw")?;
            let four = arg_str(&args, "four")?;
            let verify = args.get("verify").and_then(Value::as_bool).unwrap_or(true);
            let out = netsec::decrypt_response(raw, four, verify)?;
            Ok(json!({
                "business": out.business,
                "plaintext": out.plaintext,
                "expected_md5": out.expected_md5,
                "actual_md5": out.actual_md5,
                "encrypted": out.encrypted,
            }))
        }

        "four_from_ms" => {
            let ms = args
                .get("ms")
                .and_then(Value::as_f64)
                .ok_or("ms required")?;
            Ok(Value::String(netsec::key_data_four_from_ms(ms)))
        }

        "token_sign" => {
            let ts = arg_str(&args, "ts")?;
            let token = arg_str(&args, "token")?;
            let uid = arg_str(&args, "uid")?;
            let salt = args
                .get("salt")
                .and_then(Value::as_str)
                .unwrap_or(sign::TOKEN_SIGN_SALT);
            Ok(Value::String(sign::token_sign_android(
                ts, token, uid, salt,
            )))
        }

        "token_sign_ios" => {
            let ts = arg_str(&args, "ts")?;
            let token = arg_str(&args, "token")?;
            let uid = arg_str(&args, "uid")?;
            let suffix = args
                .get("suffix")
                .and_then(Value::as_str)
                .unwrap_or(sign::IOS_TOKEN_SIGN_SUFFIX);
            Ok(Value::String(sign::token_sign_ios(ts, token, uid, suffix)))
        }

        "official_sign" => {
            let params = string_map(args.get("params"))?;
            let salt = args
                .get("salt")
                .and_then(Value::as_str)
                .unwrap_or(sign::SIGN_SALT_STANDIN);
            Ok(Value::String(sign::official_sign(&params, salt)))
        }

        "runschool_sign" => {
            let params = string_map(args.get("params"))?;
            Ok(Value::String(sign::runschool_sign(&params)))
        }

        "points_sign" => {
            let host = arg_str(&args, "host")?;
            let path = arg_str(&args, "path")?;
            Ok(Value::String(sign::points_sign(host, path)))
        }

        "protocol_constants" => Ok(json!({
            "host": sign::HOST,
            "app_id": sign::APP_ID,
            "runec_key": sign::RUNEC_KEY,
        })),

        "gt4_str16" => Ok(Value::String(gt4::str16())),

        "gt4_pow_msg" => {
            let pow_detail = args.get("pow_detail").cloned().unwrap_or(Value::Null);
            let captcha_id = arg_str(&args, "captcha_id")?;
            let lot_number = arg_str(&args, "lot_number")?;
            Ok(Value::String(gt4::pow_msg(
                &pow_detail,
                captcha_id,
                lot_number,
            )))
        }

        "gt4_userresponse" => {
            let set_left = args
                .get("set_left")
                .and_then(Value::as_i64)
                .ok_or("set_left required")?;
            let bg_width = args.get("bg_width").and_then(Value::as_f64);
            Ok(json!(gt4::userresponse(set_left, bg_width)))
        }

        "gt4_passtime" => {
            let distance = args
                .get("distance")
                .and_then(Value::as_i64)
                .ok_or("distance required")?;
            Ok(json!(gt4::passtime_for(distance)))
        }

        "gt4_payload" => {
            let set_left = args
                .get("set_left")
                .and_then(Value::as_i64)
                .ok_or("set_left required")?;
            let lot_number = arg_str(&args, "lot_number")?;
            let pow_msg = arg_str(&args, "pow_msg")?;
            let pow_sign = arg_str(&args, "pow_sign")?;
            let bg_width = args.get("bg_width").and_then(Value::as_f64);
            let passtime = args.get("passtime").and_then(Value::as_i64);
            Ok(Value::String(gt4::payload_json(
                set_left, lot_number, pow_msg, pow_sign, bg_width, passtime,
            )?))
        }

        "gt4_w" => {
            let set_left = args
                .get("set_left")
                .and_then(Value::as_i64)
                .ok_or("set_left required")?;
            let lot_number = arg_str(&args, "lot_number")?;
            let pow_msg = arg_str(&args, "pow_msg")?;
            let pow_sign = arg_str(&args, "pow_sign")?;
            let str16_key = arg_str(&args, "str16_key")?;
            let modulus_hex = arg_str(&args, "modulus_hex")?;
            let bg_width = args.get("bg_width").and_then(Value::as_f64);
            let passtime = args.get("passtime").and_then(Value::as_i64);
            Ok(Value::String(gt4::build_w(
                set_left,
                lot_number,
                pow_msg,
                pow_sign,
                str16_key,
                bg_width,
                passtime,
                modulus_hex,
            )?))
        }

        "upload_entity" => {
            let run = args.get("run").ok_or("run required")?;
            let ctx = args.get("ctx").cloned().unwrap_or_else(|| json!({}));
            rundata::build_upload_entity(run, &ctx)
        }

        "obs_object" => {
            let run = args.get("run").ok_or("run required")?;
            let rrid = args.get("rrid").cloned().unwrap_or(Value::Null);
            let uuid = args.get("uuid").cloned().unwrap_or(Value::Null);
            let uid = args.get("uid").cloned().unwrap_or(Value::Null);
            let five = args
                .get("five_point_json")
                .and_then(Value::as_str)
                .unwrap_or("");
            obs::build_obs_object(run, &rrid, &uuid, &uid, five)
        }

        "device_validate" => {
            let profile = args.get("profile").ok_or("profile required")?;
            let now_ms = args.get("now_ms").and_then(Value::as_i64).unwrap_or(0);
            device::validate_profile(profile, now_ms)
        }

        "device_generate" => {
            let now_ms = args
                .get("now_ms")
                .and_then(Value::as_i64)
                .ok_or("now_ms required")?;
            let source = args
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or("generated");
            let platform = arg_str(&args, "platform")?;
            device::generate(now_ms, source, platform)
        }

        "run_core" => runcore::build(&args),

        "self_test" => Ok(self_test()),

        other => Err(format!("unknown op {other:?}")),
    }
}

fn self_test() -> Value {
    let mut results: Vec<Value> = Vec::new();

    let folds = [
        netsec::fold32("nhang.school"),
        netsec::fold32("5K0E8400-E29"),
        netsec::fold32("597DEA1AFB49"),
        netsec::fold32("86123.456789"),
    ];
    results.push(json!({
        "name": "fold32 vectors",
        "ok": folds == [0x61297D61u32, 0x203A324Cu32, 0x363A323Cu32, 0x3D2F3D3Eu32],
        "detail": format!("{:08x} {:08x} {:08x} {:08x}", folds[0], folds[1], folds[2], folds[3]),
    }));

    let key = netsec::derive_paes_key(
        "nhang.school",
        "5K0E8400-E29",
        "597DEA1AFB49",
        "86123.456789",
    );
    let key_hex = key
        .as_ref()
        .map(|k| k.iter().map(|b| format!("{b:02x}")).collect::<String>());
    let key_ok = matches!(&key_hex, Ok(s) if s == "faaed5a4d99af386a7d3f5d109071f13");
    results.push(json!({
        "name": "derive_paes_key vector",
        "ok": key_ok,
        "detail": key_hex.clone().unwrap_or_else(|e| e.to_string()),
    }));

    match netsec::build_envelope(
        "{\"hello\":\"世界\"}",
        "insert",
        "86123.456789",
        1_700_000_000_000,
        2,
    ) {
        Ok(env) => {
            let back = {
                let d = B64.decode(&env.d).ok();
                d.and_then(|raw| netsec::aes_cbc_decrypt(env.req_key.as_bytes(), &raw).ok())
            };
            results.push(json!({
                "name": "container aes round-trip",
                "ok": back.as_deref() == Some(env.container.as_slice()),
                "detail": format!("{} bytes", env.container.len()),
            }));
            results.push(json!({
                "name": "container md5 == h",
                "ok": netsec::md5_hex(&env.container) == env.h,
                "detail": env.h.clone(),
            }));
            let k_len = B64.decode(&env.k).map(|v| v.len()).unwrap_or(0);
            results.push(json!({
                "name": "k is 128B RSA-1024",
                "ok": k_len == 128,
                "detail": format!("{k_len} B"),
            }));
            results.push(json!({
                "name": "body order d,h,k,p,t",
                "ok": env.json.starts_with("{\"d\":"),
                "detail": env.json.chars().take(16).collect::<String>(),
            }));
        }
        Err(err) => results.push(json!({
            "name": "envelope build", "ok": false, "detail": err,
        })),
    }

    match gt4::payload_json(
        181,
        gt4::GOLDEN_LOT,
        gt4::GOLDEN_POW,
        "sign",
        None,
        Some(1887),
    ) {
        Ok(payload) => {
            let digest = gt4::sha256_hex(payload.as_bytes());
            results.push(json!({
                "name": "gt4 payload byte-identical",
                "ok": digest == gt4::GOLDEN_SHA256,
                "detail": digest.chars().take(16).collect::<String>(),
            }));
        }
        Err(err) => results.push(json!({
            "name": "gt4 payload byte-identical", "ok": false, "detail": err,
        })),
    }

    let run = json!({
        "allLocJson": [{"lat": 22.4108, "lng": 112.9408, "ts": 1794411504998i64}],
        "locJsonEntity": {"useZip": false, "allLocJson": "[]"},
        "fivePointJson": {"useZip": false, "fivePointJson": "[]"},
        "speed_json": [{"beginTime": 1, "id": 1, "queueNum": 0}],
        "step_freq_json": [{"stepsNum": 23, "id": 1, "queueNum": 0}],
        "juli": 1000, "sportTime": 360, "total_steps": 900,
        "calorie": 65, "avgPower": 208, "totalAscent": 13.4,
        "sportunid": "self-test",
    });
    match rundata::build_upload_entity(&run, &json!({"uid": "self"})) {
        Ok(ent) => {
            let fields = ent.as_object().map(|o| o.len()).unwrap_or(0);
            let speed = ent.get("speed").and_then(Value::as_i64).unwrap_or(0);
            results.push(json!({
                "name": "upload entity shape",
                "ok": fields == 43 && speed == 6000,
                "detail": format!("{fields} fields, speed {speed}"),
            }));
        }
        Err(err) => results.push(json!({
            "name": "upload entity shape", "ok": false, "detail": err,
        })),
    }

    json!(results)
}

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = stdout.lock();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let response = match serde_json::from_str::<Value>(&line) {
            Ok(req) => {
                let id = req.get("id").cloned().unwrap_or(Value::Null);
                match handle(&req) {
                    Ok(data) => json!({"id": id, "ok": true, "data": data}),
                    Err(err) => json!({"id": id, "ok": false, "error": err}),
                }
            }
            Err(err) => json!({"id": null, "ok": false, "error": format!("bad request: {err}")}),
        };
        if writeln!(out, "{response}").is_err() {
            break;
        }
        let _ = out.flush();
    }
}
