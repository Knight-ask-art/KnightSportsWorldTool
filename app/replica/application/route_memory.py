"""Route memory core: learn frequent running routes locally and adapt them."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Sequence

from ..domain import routes as ROUTES
from ..domain import trajectory as T

SCHEMA_VERSION = 1
THRESHOLD_M = 50.0
MIN_LAP_M = 200.0
MIN_TRACK_POINTS = 20
MIN_DISTANCE_M = 500.0
MAX_DISTANCE_M = 20000.0

_SANITIZE_RE = re.compile(r'[^A-Za-z0-9_.-]')


def _sanitize_token(value: Any, fallback: str = '_') -> str:
    text = _SANITIZE_RE.sub('_', str(value if value is not None else ''))
    return text or fallback


def _to_json_safe(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_to_json_safe(x) for x in obj]
    if isinstance(obj, list):
        return [_to_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_json_safe(v) for k, v in obj.items()}
    return obj


def _empty_doc(owner: str = '', campus: dict | None = None) -> dict:
    return {
        'version': SCHEMA_VERSION,
        'owner': str(owner or ''),
        'campus': _to_json_safe(campus) if isinstance(campus, dict) else {},
        'updated_at_ms': 0,
        'records_seen': 0,
        'records_readable': 0,
        'records_skipped': 0,
        'templates': [],
    }


def _valid_point(lat: Any, lng: Any) -> tuple[float, float] | None:
    if isinstance(lat, bool) or isinstance(lng, bool) or lat is None or lng is None:
        return None
    try:
        flat = float(lat)
        flng = float(lng)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(flat) and math.isfinite(flng)):
        return None
    if not (-90.0 <= flat <= 90.0 and -180.0 <= flng <= 180.0):
        return None
    if abs(flat) < 1e-9 or abs(flng) < 1e-9:
        return None
    if abs(flat + 1.0) < 1e-9 or abs(flng + 1.0) < 1e-9:
        return None
    return (flat, flng)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _mapping_coord(item: dict) -> tuple[float, float] | None:
    """First valid coordinate pair in a dict, accepting GCJ/BD key aliases."""
    for lat_key in ('lat', 'latitude', 'glat', 'gLat'):
        if lat_key not in item:
            continue
        for lng_key in ('lng', 'lon', 'longitude', 'glon', 'gLng'):
            if lng_key not in item:
                continue
            pt = _valid_point(item[lat_key], item[lng_key])
            if pt is not None:
                return pt
    return None


def _clean_line(points: Sequence[Any] | None) -> list[tuple[float, float]]:
    if not isinstance(points, (list, tuple)) or not points:
        return []
    out: list[tuple[float, float]] = []
    for item in points:
        if isinstance(item, dict):
            pt = _mapping_coord(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pt = _valid_point(item[0], item[1])
        else:
            continue
        if pt is not None:
            out.append(pt)
    return out


def _cp_coord(cp: Any) -> tuple[float, float] | None:
    if isinstance(cp, dict):
        return _mapping_coord(cp)
    if isinstance(cp, (list, tuple)) and len(cp) >= 2:
        return _valid_point(cp[0], cp[1])
    return None


def _clean_checkpoints(points: Any) -> list[dict]:
    """Keep only checkpoints with a valid coordinate; preserve metadata."""
    out: list[dict] = []
    if not isinstance(points, (list, tuple)):
        return out
    for cp in points:
        coord = _cp_coord(cp)
        if coord is None:
            continue
        item = dict(cp) if isinstance(cp, dict) else {}
        item['lat'] = coord[0]
        item['lon'] = coord[1]
        out.append(item)
    return out


def _close_ring(pts: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    ring = list(pts)
    if len(ring) >= 2 and T.haversine_m(ring[0], ring[-1]) > 1e-3:
        ring.append(ring[0])
    return ring


def _has_flying_spike(
    pts: Sequence[tuple[float, float]],
    jump_m: float = 1000.0,
) -> bool:
    """A point that jumps away and back is a GPS/forged spike, not a route."""
    if len(pts) < 2:
        return False
    if T.haversine_m(pts[0], pts[1]) > jump_m:
        return True
    if T.haversine_m(pts[-2], pts[-1]) > jump_m:
        return True
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if T.haversine_m(a, b) > jump_m and T.haversine_m(b, c) > jump_m:
            return True
    return False


def _looks_like_point(item: Any) -> bool:
    if isinstance(item, dict):
        return _mapping_coord(item) is not None
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return _valid_point(item[0], item[1]) is not None
    return False


def _normalize_fence(fence: Any) -> list[list[tuple[float, float]]]:
    if isinstance(fence, dict):
        picked = None
        for key in ('polygons', 'polygon', 'rings', 'points'):
            if isinstance(fence.get(key), (list, tuple)):
                picked = fence[key]
                break
        fence = picked if picked is not None else [fence]
    if not isinstance(fence, (list, tuple)) or not fence:
        return []
    if _looks_like_point(fence[0]):
        fence = [fence]
    rings: list[list[tuple[float, float]]] = []
    for raw_ring in fence:
        pts = _clean_line(raw_ring)
        if len(pts) >= 3:
            rings.append(pts)
    return rings


_LAP_SAMPLES = 240
_MAX_LAPS_DETECT = 12


def _lap_period_count(ring: list[tuple[float, float]]) -> int:
    """How many identical laps a closed ring contains (anchored periodicity).

    Compares arc-uniform samples shifted by one candidate lap.  A true lap
    boundary returns to the *start*, so figure-8 or two-lobe routes that only
    repeat their shape elsewhere do not match.
    """
    if len(ring) < 8:
        return 1
    total = T.polyline_length_m(ring)
    if total <= 0.0:
        return 1
    samples = T.resample(ring, _LAP_SAMPLES + 1)[:-1]
    n = len(samples)
    if n < 8:
        return 1

    def at(pos: float) -> tuple[float, float]:
        pos %= n
        i = int(math.floor(pos))
        f = pos - i
        a = samples[i % n]
        b = samples[(i + 1) % n]
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)

    max_k = min(_MAX_LAPS_DETECT, n // 4)
    for k in range(max_k, 1, -1):
        shift = n / float(k)
        mean_d = sum(T.haversine_m(samples[i], at(i + shift)) for i in range(n)) / n
        # GPS noise plus resampling error; a symmetric oval's half-lap point is
        # the width of the track away (~50 m), so this cannot false-positive.
        tol = max(15.0, 0.05 * total / float(k))
        if mean_d <= tol:
            return k
    return 1


def _cut_one_lap(
    ring: list[tuple[float, float]],
    laps: int,
    per_lap: int = 128,
) -> list[tuple[float, float]]:
    """Cut the first of ``laps`` equal-arc periods out of a closed ring."""
    count = max(2, int(laps) * per_lap + 1)
    return T.resample(ring, count)[:per_lap + 1]


def _out_and_back_half(
    points: Sequence[tuple[float, float]],
    max_distance_m: float = 30.0,
) -> list[tuple[float, float]] | None:
    """Return the outbound half when a closed track is an out-and-back.

    A user who runs out and back returns to the start, so the record is
    ``is_closed``.  Treating the whole retrace as a 2 km lap makes
    ``build_loop`` double it; the outbound half is the real route shape.
    """
    ring = _close_ring(points)
    if len(ring) < 8:
        return None
    total = T.polyline_length_m(ring)
    if total < MIN_LAP_M * 2.0:
        return None
    samples = T.resample(ring, 129)
    outbound = samples[:65]
    inbound = samples[64:]
    a = T.resample(outbound, 128)
    b = T.resample(inbound, 128)
    # Direction-sensitive: the inbound must be the outbound in reverse order.
    # Chamfer would also match the second lap of a normal loop.
    reverse_gap = sum(
        T.haversine_m(a[i], b[-1 - i]) for i in range(128)
    ) / 128.0
    if reverse_gap > max_distance_m:
        return None
    return outbound


def _extract_single_lap(
    simplified: list[tuple[float, float]],
) -> list[tuple[float, float]] | None:
    if not simplified or len(simplified) < 4 or not T.is_closed(simplified):
        return None
    ring = _close_ring(simplified)
    if T.polyline_length_m(ring) < MIN_LAP_M * 0.9:
        return None

    # Try the whole ring first: a 4-6 lap track is periodic without needing
    # extract_loop's 40% scan (which can cut a non-integer segment under noise).
    candidate = ring
    if _lap_period_count(ring) <= 1:
        loop = T.extract_loop(simplified, MIN_LAP_M)
        if loop is not None:
            candidate = _close_ring(loop)
    if T.polyline_length_m(candidate) < MIN_LAP_M * 0.9:
        candidate = ring

    # extract_loop can return 2+ laps (it starts scanning after 40% of the
    # track).  Anchored periodicity reduces it to exactly one lap, including
    # 200 m loops where the legacy 30 m start tolerance would cut early.
    for _ in range(3):
        laps = _lap_period_count(candidate)
        if laps <= 1:
            break
        nxt = _close_ring(_cut_one_lap(candidate, laps))
        nxt_len = T.polyline_length_m(nxt)
        if nxt_len < MIN_LAP_M * 0.9:
            break
        if nxt_len >= T.polyline_length_m(candidate) * 0.98:
            break
        candidate = nxt

    if T.polyline_length_m(_close_ring(candidate)) < MIN_LAP_M * 0.9:
        return None
    return candidate


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def memory_path(store_dir: str, uid, unid) -> str:
    safe_uid = _sanitize_token(uid)
    safe_unid = _sanitize_token(unid)
    stem = f'{safe_uid}_{safe_unid}'
    if len(stem) > 75:
        digest = hashlib.sha1(stem.encode('utf-8')).hexdigest()[:6]
        stem = f'{safe_uid[:32]}_{safe_unid[:32]}_{digest}'
    return os.path.join(store_dir, 'route_memory', f'{stem}.json')


def load(store_dir: str, uid, unid) -> dict:
    owner = f'{uid}:{unid}'
    if not store_dir:
        return _empty_doc(owner)
    path = memory_path(store_dir, uid, unid)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty_doc(owner)
    if not isinstance(data, dict):
        return _empty_doc(owner)
    if data.get('version') != SCHEMA_VERSION:
        return _empty_doc(owner)
    if not isinstance(data.get('templates'), list):
        return _empty_doc(owner)
    doc = _empty_doc(data.get('owner', owner), data.get('campus'))
    doc['updated_at_ms'] = _safe_int(data.get('updated_at_ms'))
    doc['records_seen'] = _safe_int(data.get('records_seen'))
    doc['records_readable'] = _safe_int(data.get('records_readable'))
    doc['records_skipped'] = _safe_int(data.get('records_skipped'))
    doc['templates'] = _to_json_safe(data['templates'])
    return doc


def save(store_dir: str, uid, unid, doc: dict) -> None:
    path = memory_path(store_dir, uid, unid)
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    clean_doc = _to_json_safe(doc)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.rm-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(clean_doc, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear(store_dir: str, uid, unid) -> None:
    if not store_dir:
        return
    path = memory_path(store_dir, uid, unid)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# track extraction & learning
# --------------------------------------------------------------------------

def track_from_detail(detail: dict) -> list[tuple[float, float]]:
    """Extract valid ``[(lat, lng), ...]`` from a ``record_detail`` payload."""
    if not isinstance(detail, dict):
        return []
    raw_track = detail.get('track')
    if not isinstance(raw_track, (list, tuple)):
        return []
    out: list[tuple[float, float]] = []
    for item in raw_track:
        if isinstance(item, dict):
            pt = _mapping_coord(item)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            pt = _valid_point(item[0], item[1])
        else:
            continue
        if pt is not None:
            out.append(pt)
    return out


def _template_id(line: Sequence[tuple[float, float]]) -> str:
    joined = '|'.join(f'{lat:.6f},{lng:.6f}' for lat, lng in line)
    return 'lr_' + hashlib.sha1(joined.encode('utf-8')).hexdigest()[:8]


def _make_cluster(
    line: list[tuple[float, float]],
    rrid: str,
    distance_m: float,
    pace: float | None,
    start_ms: int,
) -> dict:
    return {
        'lines': [line],
        'medoid_idx': 0,
        'rrids': [rrid] if rrid else [],
        'rrid_set': {rrid} if rrid else set(),
        'distances': [distance_m],
        'paces': [pace] if pace is not None else [],
        'starts': [start_ms] if start_ms > 0 else [],
    }


def _add_to_cluster(
    cluster: dict,
    line: list[tuple[float, float]],
    closed: bool,
    rrid: str,
    distance_m: float,
    pace: float | None,
    start_ms: int,
) -> None:
    cluster['lines'].append(line)
    cluster['medoid_idx'] = T.medoid_index(cluster['lines'], closed=closed)
    if rrid and rrid not in cluster['rrid_set']:
        cluster['rrid_set'].add(rrid)
        if len(cluster['rrids']) < 100:
            cluster['rrids'].append(rrid)
    cluster['distances'].append(distance_m)
    if pace is not None:
        cluster['paces'].append(pace)
    if start_ms > 0:
        cluster['starts'].append(start_ms)


def _cluster_to_template(cluster: dict, closed: bool) -> dict:
    lines = cluster['lines']
    m_idx = cluster['medoid_idx']
    medoid = lines[m_idx]
    lap_m = T.polyline_length_m(_close_ring(medoid) if closed else medoid)
    samples = len(lines)
    if samples <= 1:
        medoid_dist = 0.0
    else:
        total_d = sum(
            T.route_distance(line, medoid, closed=closed)
            for i, line in enumerate(lines)
            if i != m_idx
        )
        medoid_dist = total_d / (samples - 1)
    starts = cluster['starts']
    paces = cluster['paces']
    return {
        'id': _template_id(medoid),
        'name': '',
        'line': [[float(p[0]), float(p[1])] for p in medoid],
        'closed': closed,
        'lap_m': round(lap_m, 1),
        'samples': samples,
        'source_rrids': list(cluster['rrids'][:100]),
        'first_seen_ms': min(starts) if starts else 0,
        'last_seen_ms': max(starts) if starts else 0,
        'avg_distance_m': round(sum(cluster['distances']) / len(cluster['distances']), 1),
        'avg_pace': round(sum(paces) / len(paces), 2) if paces else 0.0,
        'quality': {'ok': samples, 'cheat': 0},
        'medoid_distance_m': round(medoid_dist, 1),
    }


def learn_from_tracks(
    tracks: list[dict],
    owner: str,
    campus: dict | None = None,
    threshold_m: float = THRESHOLD_M,
) -> dict:
    """Cluster cleaned track dicts into route templates without writing to disk."""
    now_ms = int(time.time() * 1000)
    raw_tracks = list(tracks or [])
    if not raw_tracks:
        doc = _empty_doc(owner, campus)
        doc['updated_at_ms'] = now_ms
        return doc

    closed_clusters: list[dict] = []
    open_clusters: list[dict] = []
    usable = 0

    for item in raw_tracks:
        if not isinstance(item, dict):
            continue
        if item.get('source', 'server') == 'tool':
            continue
        try:
            is_cheat = int(item.get('is_cheat') or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if is_cheat != 0 or item.get('complete') is not True:
            continue
        try:
            distance_m = float(item.get('distance_m') or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not (math.isfinite(distance_m) and MIN_DISTANCE_M <= distance_m <= MAX_DISTANCE_M):
            continue

        cleaned = _clean_line(item.get('line'))
        if len(cleaned) < MIN_TRACK_POINTS:
            continue
        if _has_flying_spike(cleaned):
            continue

        simplified = T.simplify(cleaned, 2.5, 400)
        if len(simplified) < 2:
            continue

        if T.is_closed(simplified):
            half = _out_and_back_half(cleaned)
            if half is not None:
                line = T.simplify(half, 2.5, 200)
                if len(line) < 2:
                    continue
                closed = False
            else:
                loop = _extract_single_lap(simplified)
                if loop is None:
                    continue
                line = T.canonical_loop(loop)
                if len(line) < 2:
                    continue
                closed = True
        else:
            line = simplified
            closed = False

        usable += 1
        rrid = str(item.get('rrid') or '')
        try:
            duration_s = int(item.get('duration_s') or 0)
        except (TypeError, ValueError, OverflowError):
            duration_s = 0
        pace = (duration_s / (distance_m / 1000.0)) / 60.0 if duration_s > 0 else None
        try:
            start_ms = int(item.get('start_ms') or 0)
        except (TypeError, ValueError, OverflowError):
            start_ms = 0

        clusters = closed_clusters if closed else open_clusters
        best_cluster = None
        best_dist = float('inf')
        for cl in clusters:
            medoid = cl['lines'][cl['medoid_idx']]
            d = T.route_distance(line, medoid, closed=closed)
            if d < best_dist:
                best_dist = d
                best_cluster = cl

        if best_cluster is not None and best_dist <= threshold_m:
            _add_to_cluster(best_cluster, line, closed, rrid, distance_m, pace, start_ms)
        else:
            clusters.append(_make_cluster(line, rrid, distance_m, pace, start_ms))

    templates = [_cluster_to_template(cl, True) for cl in closed_clusters]
    templates.extend(_cluster_to_template(cl, False) for cl in open_clusters)
    templates.sort(key=lambda t: (-t['samples'], t['lap_m'], t['id']))
    for idx, tpl in enumerate(templates, 1):
        tpl['name'] = f'学习路线 {idx}'

    return {
        'version': SCHEMA_VERSION,
        'owner': str(owner or ''),
        'campus': _to_json_safe(campus) if isinstance(campus, dict) else {},
        'updated_at_ms': now_ms,
        'records_seen': len(raw_tracks),
        'records_readable': usable,
        'records_skipped': len(raw_tracks) - usable,
        'templates': templates,
    }


# --------------------------------------------------------------------------
# summary, suggestion & application
# --------------------------------------------------------------------------

def summarize(doc: dict) -> dict:
    if not isinstance(doc, dict):
        doc = {}
    templates = [t for t in (doc.get('templates') or []) if isinstance(t, dict)]
    seen = _safe_int(doc.get('records_seen'))
    usable = _safe_int(doc.get('records_readable'))
    skipped = _safe_int(doc.get('records_skipped'))
    updated_at_ms = _safe_int(doc.get('updated_at_ms'))

    total_samples = sum(max(0, _safe_int(t.get('samples'))) for t in templates)
    last_seen_ms = max((_safe_int(t.get('last_seen_ms')) for t in templates), default=0)

    if not templates:
        status = 'empty'
        medoid_dist = 0.0
    else:
        max_samples = max(max(0, _safe_int(t.get('samples'))) for t in templates)
        worst_dist = max(max(0.0, _safe_float(t.get('medoid_distance_m'))) for t in templates)
        if total_samples > 0:
            weighted = sum(
                max(0.0, _safe_float(t.get('medoid_distance_m')))
                * max(0, _safe_int(t.get('samples')))
                for t in templates
            )
            medoid_dist = round(weighted / total_samples, 1)
        else:
            medoid_dist = round(worst_dist, 1)

        if usable < 5 or max_samples < 3 or worst_dist > 60.0:
            status = 'weak'
        else:
            status = 'ok'

    detail = [
        {
            'id': str(t.get('id') or ''),
            'name': str(t.get('name') or ''),
            'closed': bool(t.get('closed')),
            'lap_m': round(max(0.0, _safe_float(t.get('lap_m'))), 1),
            'samples': max(0, _safe_int(t.get('samples'))),
            'last_seen_ms': _safe_int(t.get('last_seen_ms')),
            'medoid_distance_m': round(
                max(0.0, _safe_float(t.get('medoid_distance_m'))), 1
            ),
        }
        for t in templates
    ]
    return {
        'templates': len(templates),
        'samples': total_samples,
        'usable': usable,
        'seen': seen,
        'skipped': skipped,
        'updated_at_ms': updated_at_ms,
        'status': status,
        'medoid_distance_m': medoid_dist,
        'last_seen_ms': last_seen_ms,
        'templates_detail': detail,
    }


def _rotate_to_anchor(
    pts: list[tuple[float, float]],
    anchor: tuple[float, float] | None,
) -> list[tuple[float, float]]:
    if not pts or anchor is None:
        return list(pts)
    idx = min(range(len(pts)), key=lambda i: T.haversine_m(pts[i], anchor))
    return pts[idx:] + pts[:idx]


def suggest(
    doc: dict,
    km: float,
    points: list[dict],
    fence: list[list[tuple[float, float]]] | None = None,
    center: tuple[float, float] | None = None,
) -> list[dict]:
    """Rank learned templates for the requested distance, checkpoints and fence."""
    if summarize(doc).get('status') != 'ok':
        return []
    try:
        target_m = float(km) * 1000.0
    except (TypeError, ValueError, OverflowError):
        return []
    if not (math.isfinite(target_m) and target_m > 0.0):
        return []

    templates = [t for t in (doc.get('templates') or []) if isinstance(t, dict)]
    if not templates:
        return []

    cps = _clean_checkpoints(points)
    rings = _normalize_fence(fence)
    anchor = _cp_coord(center) if center is not None else None
    if anchor is None and cps:
        anchor = _cp_coord(cps[0])

    max_samples = max((_safe_int(t.get('samples')) for t in templates), default=1)
    max_samples = max(1, max_samples)
    denom_freq = math.log1p(max(1, max_samples))
    now_ms = int(time.time() * 1000)

    candidates: list[dict] = []
    for tpl in templates:
        samples = _safe_int(tpl.get('samples'))
        if samples < 3:
            continue
        if _safe_float(tpl.get('medoid_distance_m')) > 60.0:
            continue
        pts = _clean_line(tpl.get('line'))
        if len(pts) < 2:
            continue
        closed = bool(tpl.get('closed'))
        lap_m = max(0.0, _safe_float(tpl.get('lap_m')))
        if lap_m <= 0.0:
            lap_m = T.polyline_length_m(_close_ring(pts) if closed else pts)
        if lap_m <= 0.0:
            continue
        if closed and lap_m > target_m * 1.5:
            continue
        # generate_run_data rejects an open route longer than the target
        # (target + 1 m tolerance), so never suggest one.
        if not closed and lap_m > target_m + 1.0:
            continue

        if closed:
            laps = max(1, int(math.ceil(target_m / lap_m)))
            if laps > 30:
                continue
        else:
            laps = 1

        if rings and ROUTES.outside_fences(pts, rings)['count'] > 0:
            continue

        if closed:
            base = T.canonical_loop(pts)
            rotated = _rotate_to_anchor(base, anchor)
            work_line = _close_ring(rotated)
        else:
            work_line = list(pts)

        missed_before = ROUTES.missed_points(work_line, cps) if cps else []
        if any(m.get('isMustPoint') for m in missed_before):
            continue

        base_len = T.polyline_length_m(work_line)
        if missed_before:
            snapped = ROUTES.snap_through(work_line, missed_before)
        else:
            snapped = list(work_line)
        if len(snapped) > 400:
            snapped = T.simplify(snapped, 2.5, 400)
        detour_m = max(0.0, T.polyline_length_m(snapped) - base_len)

        if rings and ROUTES.outside_fences(snapped, rings)['count'] > 0:
            continue

        missed_after = ROUTES.missed_points(snapped, cps) if cps else []
        if any(m.get('isMustPoint') for m in missed_after):
            continue

        coverage = 1.0 - (len(missed_before) / len(cps)) if cps else 1.0
        if closed:
            actual_m = laps * lap_m
            distance_fit = max(0.0, 1.0 - abs(actual_m - target_m) / target_m)
            passes = laps
        else:
            # build_open trims when the learned path is long enough, otherwise
            # it retraces out-and-back until the target is covered.
            if lap_m >= target_m:
                actual_m = target_m
                passes = 1
            else:
                passes = max(1, int(math.ceil(target_m / lap_m)))
                actual_m = passes * lap_m
            distance_fit = max(0.0, 1.0 - abs(actual_m - target_m) / target_m)

        d_start = T.haversine_m(work_line[0], anchor) if anchor is not None else 0.0
        start_score = max(0.0, 1.0 - d_start / 1000.0)

        last_seen_ms = _safe_int(tpl.get('last_seen_ms'))
        age_days = max(0.0, (now_ms - last_seen_ms) / 86400000.0) if last_seen_ms > 0 else 365.0
        freshness = max(0.0, 1.0 - max(0.0, age_days - 30.0) / 60.0)

        samples = max(0, _safe_int(tpl.get('samples')))
        frequency = min(1.0, max(0.0, math.log1p(samples) / denom_freq)) if denom_freq > 0 else 0.0
        detour_score = max(0.0, 1.0 - detour_m / 500.0)

        score = (
            0.35 * coverage
            + 0.20 * distance_fit
            + 0.15 * start_score
            + 0.10 * freshness
            + 0.10 * frequency
            + 0.10 * detour_score
        )
        score = min(1.0, max(0.0, score))

        reasons: list[str] = []
        if rings:
            reasons.append('围栏内')
        if cps:
            hit_count = len(cps) - len(missed_before)
            reasons.append(f'{hit_count}/{len(cps)} 点位')
        if closed:
            reasons.append(f'约 {laps} 圈 × {lap_m / 1000.0:.2f} km')
        elif passes == 1:
            reasons.append(f'单程约 {lap_m / 1000.0:.2f} km')
        elif passes == 2:
            reasons.append(f'折返约 {lap_m * 2.0 / 1000.0:.2f} km')
        else:
            reasons.append(f'来回 {passes} 趟约 {actual_m / 1000.0:.2f} km')
        if last_seen_ms > 0:
            reasons.append(f'最近 {int(round(age_days))} 天跑过 {samples} 次')
        else:
            reasons.append(f'跑过 {samples} 次')
        if detour_m >= 1.0:
            reasons.append(f'补点位绕行 {round(detour_m):.0f} m')

        candidates.append({
            'template_id': str(tpl.get('id') or ''),
            'name': str(tpl.get('name') or ''),
            'score': round(score, 3),
            'coverage': round(coverage, 3),
            'reasons': reasons,
            'line': [[float(p[0]), float(p[1])] for p in snapped],
            'closed': closed,
            'lap_m': round(lap_m, 1),
            'laps': laps,
            'samples': samples,
            'target_m': round(target_m, 1),
            'missed': missed_after,
            'fence_ok': True,
            'detour_m': round(detour_m, 1),
        })

    candidates.sort(key=lambda c: (-c['score'], c['detour_m'], c['template_id']))
    return candidates[:3]


def apply_template(
    doc: dict,
    template_id: str,
    km: float,
    points: list[dict] | None = None,
) -> dict:
    """Rotate/snap a template by id into waypoints without forging run data."""
    if not isinstance(doc, dict):
        doc = {}
    templates = [
        t for t in (doc.get('templates') or [])
        if isinstance(t, dict)
    ]
    tpl = next((t for t in templates if t.get('id') == template_id), None)
    if tpl is None:
        return {'ok': False, 'error': '模板不存在'}

    pts = _clean_line(tpl.get('line'))
    if len(pts) < 2:
        return {'ok': False, 'error': '模板不存在'}

    closed = bool(tpl.get('closed'))
    cps = _clean_checkpoints(points)
    anchor = _cp_coord(cps[0]) if cps else None

    if closed:
        base = T.canonical_loop(pts)
        rotated = _rotate_to_anchor(base, anchor)
        work = _close_ring(rotated)
    else:
        work = list(pts)

    if cps:
        missed = ROUTES.missed_points(work, cps)
        if missed:
            work = ROUTES.snap_through(work, missed)

    if len(work) > 400:
        work = T.simplify(work, 2.5, 400)

    missed_after = ROUTES.missed_points(work, cps) if cps else []
    lap_m = T.polyline_length_m(_close_ring(work) if closed else work)
    try:
        target_m = max(0.0, float(km) * 1000.0)
    except (TypeError, ValueError, OverflowError):
        target_m = 0.0
    if not math.isfinite(target_m):
        target_m = 0.0
    if closed and lap_m > 0 and target_m > 0:
        laps = max(1, int(math.ceil(target_m / lap_m)))
        if laps > 30:
            return {
                'ok': False,
                'error': '需要 %d 圈，超过 30 圈上限；请增大目标距离或换更长的路线'
                         % laps,
            }
    else:
        laps = 1

    return {
        'ok': True,
        'waypoints': [[float(p[0]), float(p[1])] for p in work],
        'closed': closed,
        'lap_m': round(lap_m, 1),
        'laps': laps,
        'target_m': round(target_m, 1),
        'missed': missed_after,
        'fence_ok': True,
    }
