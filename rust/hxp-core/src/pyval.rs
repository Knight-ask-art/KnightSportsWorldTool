//! Python-compatible scalar helpers.
//!
//! The sidecar receives JSON, but the cached values were produced by Python
//! and the output is compared byte for byte in tests.  Rounding and `int()`
//! therefore have to follow CPython's rules, not Rust's defaults.

use serde_json::Value;

/// Python truthiness for the value shapes that appear in run/ctx.
pub fn truthy(value: &Value) -> bool {
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
pub fn py_int(value: &Value) -> i64 {
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

/// Python `float(x)`, or `None` where CPython would raise.
pub fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::Bool(flag) => Some(f64::from(u8::from(*flag))),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Python `str(x)` for scalar values.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::String(s) => s.clone(),
        Value::Bool(flag) => {
            if *flag {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Python `round(x)` — nearest integer, ties to even.
pub fn py_round(value: f64) -> f64 {
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

/// Python `round(x, digits)`: Rust's fixed formatting rounds the exact binary
/// value with ties to even, which is the rule CPython uses.
pub fn py_round_n(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounding_matches_cpython_ties_to_even() {
        assert_eq!(py_round(2.5), 2.0);
        assert_eq!(py_round(3.5), 4.0);
        assert_eq!(py_round(-2.5), -2.0);
        assert_eq!(py_round(-1.5), -2.0);
        assert_eq!(py_round_n(194.28, 4), 194.28);
        assert_eq!(py_round_n(1.005, 2), 1.0);
        assert_eq!(py_round_n(1.015, 2), 1.01);
    }

    #[test]
    fn int_and_float_helpers_accept_strings_and_bools() {
        assert_eq!(py_int(&serde_json::json!("-7")), -7);
        assert_eq!(py_int(&serde_json::json!(true)), 1);
        assert_eq!(py_int(&serde_json::json!(3.9)), 3);
        assert_eq!(py_float(&serde_json::json!("2.5")), Some(2.5));
        assert_eq!(py_float(&serde_json::json!(null)), None);
    }
}
