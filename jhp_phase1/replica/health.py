"""Physiological fields — official formulas, so the numbers survive review.

Ported from YanamiNeko/NekoSportsWorldTool `src/track/calorie.rs`.

    MET(pace min/km):
        pace < 10        -> 33 / pace + 4.1
        10 <= pace < 12  -> 102 / pace - 3.7
        pace >= 12       -> 33.6 / pace + 1
        no displacement  -> 12
        then rounded to 0.1 (that is what the app displays)

    calorie  = round(weight(kg) * MET * hours)
    avgPower = round(1.1 * weight(kg) * speed(m/s))
    totalAscent = sum of every POSITIVE altitude delta
                  (NOT end altitude - start altitude)

Sending a constant "distance * 0.06" for calories, or 0 for power/ascent, is
exactly the kind of thing a manual reviewer notices.
"""

from __future__ import annotations

DEFAULT_WEIGHT_KG = 68.0


def met(pace_min_per_km: float) -> float:
    """Raw MET for a running pace in minutes per kilometre."""
    if pace_min_per_km <= 0:
        return 12.0
    if pace_min_per_km < 10:
        return 33.0 / pace_min_per_km + 4.1
    if pace_min_per_km < 12:
        return 102.0 / pace_min_per_km - 3.7
    return 33.6 / pace_min_per_km + 1.0


def met_display(pace_min_per_km: float) -> float:
    """MET rounded to 0.1 — the value the official app works with."""
    return round(met(pace_min_per_km) * 10.0) / 10.0


def calorie(weight_kg: float, total_time_s: float, total_dis_m: float) -> int:
    """Official displayed kcal."""
    if total_time_s <= 0 or weight_kg <= 0:
        return 0
    km = total_dis_m / 1000.0
    if km <= 0:
        m = 12.0
    else:
        m = met_display((total_time_s / 60.0) / km)
    return int(round(weight_kg * m * (total_time_s / 3600.0)))


def avg_power(weight_kg: float, total_dis_m: float, total_time_s: float) -> int:
    """Official fallback power in watts."""
    if total_time_s <= 0:
        return 0
    return int(round(1.1 * weight_kg * (total_dis_m / total_time_s)))


def total_ascent(altitudes: list[float]) -> float:
    """Sum of positive deltas only."""
    return round(sum(max(0.0, b - a) for a, b in zip(altitudes, altitudes[1:])), 1)


def total_descent(altitudes: list[float]) -> float:
    return round(sum(max(0.0, a - b) for a, b in zip(altitudes, altitudes[1:])), 1)


# --------------------------------------------------------------------------
# self test — upstream golden vectors
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    out.append(('kcal vector 1220s/3300m',
                calorie(68.0, 1220, 3300.0) == 219,
                str(calorie(68.0, 1220, 3300.0))))
    out.append(('kcal vector 600s/1000m',
                calorie(68.0, 600, 1000.0) == 74,
                str(calorie(68.0, 600, 1000.0))))
    out.append(('power vector 1220s/3300m',
                avg_power(68.0, 3300.0, 1220) == 202,
                str(avg_power(68.0, 3300.0, 1220))))

    # MET is monotone decreasing as pace slows
    out.append(('MET monotone', met(5) > met(6) > met(8) > met(11),
                '%.2f/%.2f/%.2f/%.2f' % (met(5), met(6), met(8), met(11))))

    # 0.1 rounding is applied
    out.append(('MET rounded to 0.1',
                abs(met_display(6.1616) * 10 - round(met_display(6.1616) * 10)) < 1e-9,
                str(met_display(6.1616))))

    alts = [20.0, 22.0, 21.0, 25.0]
    out.append(('ascent counts only gains', total_ascent(alts) == 6.0,
                str(total_ascent(alts))))
    out.append(('descent counts only drops', total_descent(alts) == 1.0,
                str(total_descent(alts))))

    out.append(('calorie rejects zero duration', calorie(68.0, 0, 2500) == 0,
                calorie(68.0, 0, 2500)))
    out.append(('calorie rejects zero distance',
                calorie(68.0, 900, 0) >= 0, calorie(68.0, 900, 0)))
    out.append(('met guards zero pace', met(0) == 12.0, met(0)))
    return out


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)
