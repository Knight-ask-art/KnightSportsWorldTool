//! Native run core: the pace profile, 10 s windows and 30 s analysis table.
//!
//! The route geometry, altitude and checkpoint snapping stay in Python; those
//! are cheap to inspect while tuning and only need the sampled fractions.  The
//! columns that the server cross-checks against each other -- cumulative
//! distance, `speedPerTenSec`, `stepsPerTenSec`, `segmentJson` -- are built
//! here so their sums and shapes cannot drift apart.

use serde_json::{json, Map, Value};

use crate::pyval::{py_round, py_round_n};
use crate::rng::Rng;

const SEG_SECONDS: usize = 10;
const TIME_INTERVAL_SECONDS: usize = 30;
const PACE_TAU_S: f64 = 24.0;
const PACE_SIGMA: f64 = 0.020;
const WINDOW_TAU_S: f64 = 10.0;
const WINDOW_SIGMA: f64 = 0.20;
const CADENCE_TAU_S: f64 = 30.0;
const CADENCE_SIGMA: f64 = 0.035;

fn get_i(map: &Map<String, Value>, key: &str) -> Result<i64, String> {
    map.get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{key} required"))
}

fn get_f(map: &Map<String, Value>, key: &str) -> Result<f64, String> {
    map.get(key)
        .and_then(Value::as_f64)
        .ok_or_else(|| format!("{key} required"))
}

fn get_arr<'a>(map: &'a Map<String, Value>, key: &str) -> Result<&'a Vec<Value>, String> {
    map.get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("{key} must be an array"))
}

fn f_at(values: &[Value], index: usize, what: &str) -> Result<f64, String> {
    values
        .get(index)
        .and_then(Value::as_f64)
        .ok_or_else(|| format!("{what}[{index}] must be numeric"))
}

fn i_at(values: &[Value], index: usize, what: &str) -> Result<i64, String> {
    values
        .get(index)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{what}[{index}] must be an integer"))
}

fn dist_at(cum: &[f64], total_seconds: usize, sec: f64) -> f64 {
    if sec <= 0.0 {
        return 0.0;
    }
    if sec >= total_seconds as f64 {
        return *cum.last().unwrap_or(&0.0);
    }
    let index = sec as usize;
    let frac = sec - index as f64;
    cum[index] + (cum[index + 1] - cum[index]) * frac
}

/// One multiplicative cadence level per kilometre (2% minimum neighbour gap).
fn cadence_factors(rng: &mut Rng, n_km: usize) -> Vec<f64> {
    let mut out = Vec::with_capacity(n_km);
    let mut prev = 1.0f64;
    for k in 0..n_km {
        let mut factor = (1.0 + rng.gauss(0.0, 0.025)).clamp(0.96, 1.04);
        if k > 0 {
            let gap = factor - prev;
            if gap.abs() < 0.008 {
                let step = if gap >= 0.0 { 0.008 } else { -0.008 };
                factor = prev + step;
                if !(0.96..=1.04).contains(&factor) {
                    factor = prev - step;
                }
            }
        }
        out.push(factor);
        prev = factor;
    }
    out
}

pub fn build(args: &Value) -> Result<Value, String> {
    let args = args.as_object().ok_or("run_core args must be an object")?;
    let total_meters = get_i(args, "total_meters")?;
    let total_seconds = get_i(args, "total_seconds")? as usize;
    if total_seconds == 0 {
        return Err("total_seconds must be > 0".into());
    }
    let seg_m_values = get_arr(args, "seg_m")?;
    let seg_t_values = get_arr(args, "seg_t")?;
    if seg_m_values.len() != seg_t_values.len() || seg_m_values.is_empty() {
        return Err("seg_m/seg_t must be non-empty and the same length".into());
    }
    let n_km = seg_m_values.len();
    let seg_m: Vec<f64> = (0..n_km)
        .map(|i| f_at(seg_m_values, i, "seg_m"))
        .collect::<Result<_, _>>()?;
    let seg_t: Vec<usize> = (0..n_km)
        .map(|i| i_at(seg_t_values, i, "seg_t").map(|v| v.max(1) as usize))
        .collect::<Result<_, _>>()?;

    let pace_min_per_km = get_f(args, "pace_min_per_km")?;
    let n_points = get_i(args, "n_points")?.max(1) as usize;
    let cadence = get_f(args, "cadence")?;
    let total_steps = get_i(args, "total_steps")?;
    let t0 = get_i(args, "t0")?;
    let seed = args.get("seed").and_then(Value::as_u64).unwrap_or(0);
    let mut rng = Rng::new(seed);

    let n_buckets = ((total_seconds as f64 / SEG_SECONDS as f64).ceil() as usize).max(1);
    let avg_mps = total_meters as f64 / total_seconds as f64;

    // ---- per-second pace shape (OU around the requested pace) ------------
    let phi = (-(SEG_SECONDS as f64) / PACE_TAU_S).exp();
    let innovation = PACE_SIGMA * (1.0 - phi * phi).sqrt();
    let win_phi = (-(SEG_SECONDS as f64) / WINDOW_TAU_S).exp();
    let win_sigma = (WINDOW_SIGMA * (pace_min_per_km / 6.0).powf(1.5)).clamp(0.15, 0.55);
    let win_innovation = win_sigma * (1.0 - win_phi * win_phi).sqrt();

    let mut seg_bounds = Vec::with_capacity(n_km + 1);
    seg_bounds.push(0usize);
    for seconds in &seg_t {
        seg_bounds.push(seg_bounds.last().copied().unwrap_or(0) + seconds);
    }
    if seg_bounds[n_km] != total_seconds {
        return Err(format!(
            "seg_t sums to {} but total_seconds is {}",
            seg_bounds[n_km], total_seconds
        ));
    }
    let mut k_of = vec![0usize; total_seconds];
    for k in 0..n_km {
        for slot in k_of.iter_mut().take(seg_bounds[k + 1]).skip(seg_bounds[k]) {
            *slot = k;
        }
    }

    let mut shapes = vec![1.0f64; total_seconds];
    let mut pace_dev = 0.0f64;
    let mut window_dev = 0.0f64;
    for j in 0..total_seconds {
        if j % SEG_SECONDS == 0 {
            pace_dev = phi * pace_dev + rng.gauss(0.0, innovation);
            window_dev = win_phi * window_dev + rng.gauss(0.0, win_innovation);
        }
        let t = j as f64 / (total_seconds - 1).max(1) as f64;
        let ramp = 0.86 + 0.14 * (t / 0.06).min(1.0);
        let fatigue = 1.0 - 0.06 * t;
        shapes[j] = (ramp * fatigue * (1.0 + pace_dev) * (1.0 + window_dev)).max(0.0);
    }

    // Bimodal stops: either no pause, or a short light/crossing.
    if total_seconds >= 80 && rng.random() < 0.55 {
        for _ in 0..rng.randint(1, 3) {
            let start = rng.randint(
                (total_seconds / 10).max(1) as i64,
                (total_seconds - 30).max(1) as i64,
            ) as usize;
            let end = (start + rng.randint(12, 30) as usize).min(total_seconds);
            for slot in shapes.iter_mut().take(end).skip(start) {
                *slot *= rng.uniform(0.0, 0.04);
            }
        }
    }

    let mut norm = vec![0.0f64; n_km];
    for j in 0..total_seconds {
        norm[k_of[j]] += shapes[j];
    }
    for k in 0..n_km {
        let span = (seg_bounds[k + 1] - seg_bounds[k]).max(1);
        norm[k] = (norm[k] / span as f64).max(1e-9);
    }

    let mut cum_before = Vec::with_capacity(total_seconds + 1);
    cum_before.push(0.0);
    for j in 0..total_seconds {
        let k = k_of[j];
        let v = (seg_m[k] / seg_t[k] as f64) * shapes[j] / norm[k];
        cum_before.push(cum_before.last().copied().unwrap_or(0.0) + v);
    }
    let drift = total_meters as f64 - *cum_before.last().unwrap_or(&0.0);
    if drift.abs() > 1e-6 {
        for j in 1..=total_seconds {
            cum_before[j] += drift * j as f64 / total_seconds as f64;
        }
    }

    let mut bucket_m = Vec::with_capacity(n_buckets);
    for i in 0..n_buckets {
        let a = i * SEG_SECONDS;
        let b = (a + SEG_SECONDS).min(total_seconds);
        bucket_m.push(cum_before[b] - cum_before[a]);
    }

    // ---- sample times: ~5 s apart, normalised to the run duration --------
    let intervals: Vec<f64> = (0..n_points.saturating_sub(1))
        .map(|_| rng.uniform(4.2, 6.0))
        .collect();
    let times: Vec<f64> = if intervals.is_empty() {
        vec![0.0]
    } else {
        let scale = total_seconds as f64 / intervals.iter().sum::<f64>().max(1e-6);
        let mut times = Vec::with_capacity(intervals.len() + 1);
        times.push(0.0);
        for interval in &intervals {
            times.push(times.last().copied().unwrap_or(0.0) + interval * scale);
        }
        if let Some(last) = times.last_mut() {
            *last = total_seconds as f64;
        }
        times
    };
    let total_distance = cum_before.last().copied().unwrap_or(0.0).max(0.001);
    let mut times_fracs: Vec<f64> = times
        .iter()
        .map(|t| dist_at(&cum_before, total_seconds, *t) / total_distance)
        .collect();
    if let Some(first) = times_fracs.first_mut() {
        *first = 0.0;
    }
    if let Some(last) = times_fracs.last_mut() {
        *last = 1.0;
    }

    // ---- steps: distance profile + slow cadence drift --------------------
    let per_seg_steps = cadence * SEG_SECONDS as f64 / 60.0;
    let mean_bucket = (bucket_m.iter().sum::<f64>() / n_buckets as f64).max(1e-6);
    let cad_phi = (-(SEG_SECONDS as f64) / CADENCE_TAU_S).exp();
    let cad_innovation = CADENCE_SIGMA * (1.0 - cad_phi * cad_phi).sqrt();
    let mut cad_dev = 0.0f64;
    let flavours = cadence_factors(&mut rng, n_km);
    let stride_km: Vec<f64> = (0..n_km)
        .map(|k| ((seg_m[k] / seg_t[k] as f64) / avg_mps).powf(0.8))
        .collect();
    let mut raw_s = Vec::with_capacity(n_buckets);
    for i in 0..n_buckets {
        cad_dev = cad_phi * cad_dev + rng.gauss(0.0, cad_innovation);
        let ratio = bucket_m[i] / mean_bucket;
        let k = k_of[(i * SEG_SECONDS).min(total_seconds - 1)];
        let mut step =
            per_seg_steps * ratio / stride_km[k].max(0.5) * flavours[k] * (1.0 + cad_dev);
        step *= 1.0 + rng.gauss(0.0, 0.05);
        raw_s.push(step.max(0.0));
    }
    let s_scale = total_steps as f64 / raw_s.iter().sum::<f64>().max(1e-6);
    let stop_ms = t0 + total_seconds as i64 * 1000;
    let windows_per_batch = (TIME_INTERVAL_SECONDS / SEG_SECONDS).max(1);

    let mut speed_json: Vec<Value> = Vec::with_capacity(n_buckets);
    let mut step_json: Vec<Value> = Vec::with_capacity(n_buckets);
    for i in 0..n_buckets {
        let begin = t0 + i as i64 * SEG_SECONDS as i64 * 1000;
        let end = (begin + SEG_SECONDS as i64 * 1000).min(stop_ms);
        speed_json.push(json!({
            "beginTime": begin,
            "endTime": end,
            "distance": py_round_n(bucket_m[i], 4),
            "flag": t0,
            "id": i as i64 + 1,
            "queueNum": i / windows_per_batch,
            "state": 0,
        }));
        step_json.push(json!({
            "stepsNum": py_round(raw_s[i] * s_scale) as i64,
            "beginTime": begin,
            "endTime": end,
            "flag": t0,
            "id": i as i64 + 1,
            "maxDiff": 0.0,
            "minDiff": 1000.0,
            "avgDiff": 0.0,
            "state": 0,
            "queueNum": i / windows_per_batch,
        }));
    }

    // Zero the step counter where the phone did not move.
    let distances: Vec<f64> = speed_json
        .iter()
        .map(|entry| entry["distance"].as_f64().unwrap_or(0.0))
        .collect();
    let mut steps: Vec<i64> = step_json
        .iter()
        .map(|entry| entry["stepsNum"].as_i64().unwrap_or(0))
        .collect();
    for i in 0..steps.len() {
        if distances[i] < 0.6 {
            steps[i] = 0;
        }
    }
    for step in steps.iter_mut() {
        *step = (*step).clamp(0, 34);
    }
    if steps.len() >= 12 {
        let picks = rng.sample_range(2, steps.len() - 2, (steps.len() / 40 + 2).min(4));
        for index in picks {
            let old = steps[index];
            if old <= 2 {
                continue;
            }
            let drop = (py_round(old as f64 * rng.uniform(0.35, 0.55)) as i64).clamp(1, old - 1);
            steps[index] = old - drop;
        }
    }
    let drift_steps = total_steps - steps.iter().sum::<i64>();
    let mut moving: Vec<usize> = (0..steps.len())
        .filter(|index| distances[*index] >= 0.6)
        .collect();
    if moving.is_empty() {
        moving = (0..steps.len()).collect();
    }
    if drift_steps > 0 {
        for n in 0..drift_steps {
            steps[moving[n as usize % moving.len()]] += 1;
        }
    } else if drift_steps < 0 {
        let mut order = moving.clone();
        order.sort_by(|a, b| steps[*b].cmp(&steps[*a]));
        for n in 0..(-drift_steps) {
            let index = order[n as usize % order.len()];
            if steps[index] > 0 {
                steps[index] -= 1;
            }
        }
    }
    for (entry, value) in step_json.iter_mut().zip(steps.iter()) {
        entry["stepsNum"] = json!(*value);
    }

    // ---- 30 s analysis table ---------------------------------------------
    let segment_count =
        ((total_seconds as f64 / TIME_INTERVAL_SECONDS as f64).ceil() as usize).max(1);
    let mut segment_json: Vec<Value> = Vec::with_capacity(segment_count);
    for i in 0..segment_count {
        let a = i * TIME_INTERVAL_SECONDS;
        let b = (a + TIME_INTERVAL_SECONDS).min(total_seconds);
        let first_win = (i * (TIME_INTERVAL_SECONDS / SEG_SECONDS)).min(n_buckets);
        let last_win = n_buckets
            .min(((b as f64) / SEG_SECONDS as f64).ceil() as usize)
            .max(first_win);
        let seg_dist: f64 = bucket_m[first_win..last_win].iter().sum();
        let step_lo = first_win.min(steps.len());
        let step_hi = last_win.min(steps.len()).max(step_lo);
        let seg_steps: i64 = steps[step_lo..step_hi].iter().sum();
        let seg_sec = (b - a).max(1) as f64;
        let pace_seg = if seg_dist > 0.0 {
            seg_sec * 1000.0 / (60.0 * seg_dist)
        } else {
            0.0
        };
        let avg_step = py_round(seg_steps as f64 / seg_sec * 60.0) as i64;
        segment_json.push(json!({
            "avgSpeed": py_round_n(pace_seg, 4),
            "avgStep": avg_step,
            "distance": py_round_n(seg_dist, 4),
            "endTime": t0 + b as i64 * 1000,
            "flag": t0,
            "id": i as i64 + 1,
            "intervalStepModel": 0,
            "originAvgStep": avg_step,
            "speedStandard": 900,
            "startTime": t0 + a as i64 * 1000,
            "state": 0,
        }));
    }

    Ok(json!({
        "cum_before": cum_before,
        "bucket_m": bucket_m,
        "times": times,
        "times_fracs": times_fracs,
        "avg_mps": avg_mps,
        "stop_ms": stop_ms,
        "speed_json": speed_json,
        "step_freq_json": step_json,
        "segment_json": segment_json,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args() -> Value {
        json!({
            "total_meters": 1000,
            "total_seconds": 360,
            "seg_m": [1000.0],
            "seg_t": [360],
            "pace_min_per_km": 6.0,
            "n_points": 72,
            "cadence": 150,
            "total_steps": 900,
            "t0": 1_794_411_506_893i64,
            "seed": 7,
        })
    }

    #[test]
    fn sums_and_lengths_stay_closed() {
        let out = build(&args()).expect("core");
        let cum = out["cum_before"].as_array().unwrap();
        assert_eq!(cum.len(), 361);
        assert!((cum.last().unwrap().as_f64().unwrap() - 1000.0).abs() < 1e-6);
        let buckets = out["bucket_m"].as_array().unwrap();
        assert_eq!(buckets.len(), 36);
        let bucket_sum: f64 = buckets.iter().map(|v| v.as_f64().unwrap()).sum();
        assert!((bucket_sum - 1000.0).abs() < 1e-6);

        let times = out["times"].as_array().unwrap();
        assert_eq!(times.len(), 72);
        assert_eq!(times[0], json!(0.0));
        assert_eq!(times.last().unwrap(), &json!(360.0));

        let speed = out["speed_json"].as_array().unwrap();
        let speed_sum: f64 = speed.iter().map(|s| s["distance"].as_f64().unwrap()).sum();
        assert!((speed_sum - 1000.0).abs() < 0.01);
        let step_sum: i64 = out["step_freq_json"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["stepsNum"].as_i64().unwrap())
            .sum();
        assert_eq!(step_sum, 900);
    }

    #[test]
    fn segments_are_thirty_seconds_and_add_up() {
        let out = build(&args()).expect("core");
        let segments = out["segment_json"].as_array().unwrap();
        assert_eq!(segments.len(), 12);
        let total: f64 = segments
            .iter()
            .map(|s| s["distance"].as_f64().unwrap())
            .sum();
        assert!((total - 1000.0).abs() < 0.01);
        for (index, segment) in segments.iter().enumerate() {
            assert_eq!(segment["id"], json!(index as i64 + 1));
            let start = segment["startTime"].as_i64().unwrap();
            let end = segment["endTime"].as_i64().unwrap();
            assert!(end - start <= 30_000);
            if segment["distance"].as_f64().unwrap() > 0.0 {
                let pace = ((end - start) as f64 / 1000.0 / 60.0)
                    / (segment["distance"].as_f64().unwrap() / 1000.0);
                assert!((pace - segment["avgSpeed"].as_f64().unwrap()).abs() < 0.01);
            }
        }
    }

    #[test]
    fn different_seeds_move_the_profile() {
        let first = build(&args()).expect("core");
        let mut other = args();
        other["seed"] = json!(99);
        let second = build(&other).expect("core");
        assert_ne!(first["bucket_m"], second["bucket_m"]);
    }
}
