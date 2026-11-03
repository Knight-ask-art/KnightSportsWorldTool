"""FIT (Flexible and Interoperable Data Transfer) export for one run.

Pure stdlib: the encoder here writes the minimum a mainstream parser
(Garmin SDK, ``fitparse``, intervals.icu) needs to show a session:

* a ``FileId`` message (FIT type 0),
* one ``Record`` (20) per track point, with a *base-type profile* chosen from
  the fields that actually exist,
* one ``Lap`` (19), header-only unless length/time are known,
* one ``Session`` (18) with totals,
* one ``Activity`` (34).

The layout follows the FIT SDK profile: every message carries a definition
message (``0x40 | local``) followed by data messages, and the file is closed
with the 14-byte header CRC plus the file CRC.  Positions are written as
semicircles on the WGS-84 ellipsoid; the app's tracks arrive in GCJ-02, so
they are converted to WGS-84 first — leaving them as-is would shift the track
by several hundred metres in a third-party viewer.

No network, no disk: the caller gets ``{filename, mime, data_b64}``.
"""

from __future__ import annotations

import base64
import datetime as _dt
import struct

MIME = 'application/vnd.garmin.fit'

# FIT base types: (struct char, size, invalid sentinel)
_TYPE = {
    'enum': ('B', 1, 0xFF),
    'uint8': ('B', 1, 0xFF),
    'sint8': ('b', 1, 0x7F),
    'uint16': ('H', 2, 0xFFFF),
    'sint16': ('h', 2, 0x7FFF),
    'uint32': ('I', 4, 0xFFFFFFFF),
    'sint32': ('i', 4, 0x7FFFFFFF),
    'uint32z': ('I', 4, 0xFFFFFFFF),
}

#: 1989-12-31T00:00:00Z, the FIT epoch.
FIT_EPOCH = _dt.datetime(1989, 12, 31, tzinfo=_dt.timezone.utc)

MSG = {'file_id': 0, 'lap': 19, 'record': 20, 'session': 18, 'activity': 34}

#: Field numbers from the SDK profile (only the ones written here).
F = {
    'file_id': {'type': (0, 'enum'), 'manufacturer': (1, 'uint16'),
                'product': (2, 'uint16'), 'serial_number': (3, 'uint32z'),
                'time_created': (4, 'uint32')},
    'record': {'timestamp': (253, 'uint32'), 'position_lat': (0, 'sint32'),
               'position_long': (1, 'sint32'), 'altitude': (2, 'uint16'),
               'heart_rate': (3, 'uint8'), 'distance': (5, 'uint32'),
               'speed': (6, 'uint16'), 'enhanced_speed': (6, 'uint32')},
    'lap': {'timestamp': (253, 'uint32'), 'start_time': (2, 'uint32'),
            'total_elapsed_time': (7, 'uint32'),
            'total_distance': (9, 'uint32'),
            'avg_speed': (13, 'uint16')},
    'session': {'timestamp': (253, 'uint32'), 'start_time': (2, 'uint32'),
                'sport': (5, 'enum'), 'sub_sport': (6, 'enum'),
                'total_elapsed_time': (7, 'uint32'),
                'total_distance': (9, 'uint32'),
                'total_calories': (11, 'uint16'),
                'avg_speed': (14, 'uint16'),
                'total_ascent': (22, 'uint16'),
                'total_descent': (23, 'uint16'),
                'num_laps': (26, 'uint16'),
                'training_load_peak': (31, 'uint32x'),
                'avg_cadence': (18, 'uint8')},
    'activity': {'timestamp': (253, 'uint32'),
                 'total_timer_time': (0, 'uint32'),
                 'num_sessions': (1, 'uint16'), 'type': (2, 'enum'),
                 'event': (3, 'enum'), 'event_type': (4, 'enum')},
}

#: Field numbers are not ordering hints; the definition writes them ascending
#: so a reader that binary-searches the field list is still happy.
for _msg in F.values():
    _msg_ordered = dict(sorted(_msg.items(), key=lambda kv: kv[1][0]))
    _msg.clear()
    _msg.update(_msg_ordered)
del _msg, _msg_ordered

CRC_TABLE = None


def _crc_table():
    global CRC_TABLE
    if CRC_TABLE is None:
        table = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
            table.append(c)
        CRC_TABLE = table
    return CRC_TABLE


def crc16(data: bytes, crc: int = 0) -> int:
    """FIT CRC-16 (polynomial 0xA001, initial 0)."""
    table = _crc_table()
    for byte in data:
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc & 0xFFFF


# --------------------------------------------------------------------------
# encoding primitives
# --------------------------------------------------------------------------

def fit_time(ms: int) -> int:
    """Epoch ms -> FIT seconds (saturating, never negative)."""
    moment = _dt.datetime.fromtimestamp(max(0, int(ms or 0)) / 1000.0,
                                        tz=_dt.timezone.utc)
    return max(0, int((moment - FIT_EPOCH).total_seconds()))


def semicircles(lat: float, lng: float) -> tuple[int, int]:
    return (int(round(lat / 180.0 * (2 ** 31))),
            int(round(lng / 180.0 * (2 ** 31))))


def _size(kind: str) -> int:
    return _TYPE[kind][1]


def _pack(kind: str, value) -> bytes:
    char, size, invalid = _TYPE[kind]
    if value is None:
        return struct.pack('<' + char, invalid) if size == 1 else \
            b'\xff' * size
    if kind == 'uint32z':
        return struct.pack('<I', int(value) & 0xFFFFFFFF)
    if kind in ('uint8', 'uint16', 'uint32'):
        top = (1 << (size * 8)) - 1
        return struct.pack('<' + char, max(0, min(top, int(value))))
    return struct.pack('<' + char, max(-(2 ** (size * 8 - 1)),
                                       min(2 ** (size * 8 - 1) - 1,
                                           int(value))))


def _definition(local: int, name: str, fields) -> bytes:
    """Definition message for ``local`` with ``[(num, kind, size)]`` fields."""
    body = bytearray([0x40 | local, 0, 0])
    body += struct.pack('<H', MSG[name])
    body.append(len(fields))
    for num, kind in fields:
        body += bytes([num, _size(kind), 0])
    return bytes(body)


def _data(local: int, fields, values) -> bytes:
    out = bytearray([local])
    for (num, kind), value in zip(fields, values):
        out += _pack(kind, value)
    return bytes(out)


def _wgs84(lat: float, lng: float) -> tuple[float, float]:
    try:
        from ..domain import coords
        return coords.gcj02_to_wgs84(float(lat), float(lng))
    except Exception:  # noqa: BLE001 - export must not fail on a math edge
        return float(lat), float(lng)


# --------------------------------------------------------------------------
# track input
# --------------------------------------------------------------------------

def _track_rows(track) -> list[dict]:
    """Normalise ``parse_track`` rows / preview points into one shape."""
    out = []
    for p in track or []:
        if not isinstance(p, dict):
            continue
        try:
            raw_lat = p.get('lat', p.get('gLat'))
            raw_lng = p.get('lng', p.get('lon', p.get('gLng')))
            # 7.4.0 MyLocation uses -1/-1 as a placeholder; fall back to the
            # real GCJ pair instead of exporting a point off the map.
            if raw_lat is None or float(raw_lat) == -1.0:
                raw_lat = p.get('gLat')
            if raw_lng is None or float(raw_lng) == -1.0:
                raw_lng = p.get('gLng', p.get('lon'))
            lat = float(raw_lat)
            lng = float(raw_lng)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0
                                                             and lng == 0):
            continue
        alt = p.get('altitude', p.get('alt'))
        dist = p.get('distance_m', p.get('totalDis'))
        dur = p.get('duration_s', p.get('totalTime'))
        out.append({
            'lat': lat, 'lng': lng,
            'alt': float(alt) if alt is not None else None,
            'dist': max(0.0, float(dist)) if dist is not None else None,
            'dur': max(0.0, float(dur)) if dur is not None else None,
        })
    return out


def build(track, start_ms: int = 0, *, calories=0, ascent=None,
          descent=None, avg_cadence=None) -> bytes:
    """Encode one run.  Raises ``ValueError`` when there is no track."""
    try:
        start_ms = int(start_ms or 0)
    except (TypeError, ValueError):
        start_ms = 0
    rows = _track_rows(track)
    if len(rows) < 2:
        raise ValueError('没有可导出的轨迹点（至少需要 2 个）')
    rows.sort(key=lambda r: r['dur'] if r['dur'] is not None else 0)

    total_dur = rows[-1]['dur'] if rows[-1]['dur'] is not None else 0.0
    total_dist = max([r['dist'] for r in rows if r['dist'] is not None],
                     default=0.0)
    start_s = fit_time(start_ms) if start_ms else 0
    if start_s and total_dur:
        end_s = start_s + int(round(total_dur))
    else:
        end_s = start_s

    out = bytearray()
    # --- file_id -----------------------------------------------------------
    ff = [(num, kind) for num, kind in
          [(F['file_id'][k]) for k in
           ('type', 'manufacturer', 'product', 'serial_number',
            'time_created')]]
    out += _definition(0, 'file_id', ff)
    out += _data(0, ff, [4,                      # Garmin
                         255,                    # development
                         0, 0, end_s or start_s])

    # --- records -----------------------------------------------------------
    has_alt = any(r['alt'] is not None for r in rows)
    has_dist = any(r['dist'] is not None for r in rows)
    rf = [(F['record']['position_lat'][0], 'sint32'),
          (F['record']['position_long'][0], 'sint32')]
    if start_s:
        rf.append((F['record']['timestamp'][0], 'uint32'))
    if has_alt:
        rf.append((F['record']['altitude'][0], 'uint16'))
    if has_dist:
        rf.append((F['record']['distance'][0], 'uint32'))
    out += _definition(1, 'record', rf)
    last_dist = None
    for r in rows:
        lat, lng = _wgs84(r['lat'], r['lng'])
        slat, slng = semicircles(lat, lng)
        values = {'position_lat': slat, 'position_long': slng}
        if start_s:
            offset = r['dur'] if r['dur'] is not None else 0.0
            values['timestamp'] = start_s + int(round(offset))
        if has_alt:
            values['altitude'] = (int(round((r['alt'] + 500.0) * 5))
                                  if r['alt'] is not None else None)
        if has_dist:
            d = r['dist'] if r['dist'] is not None else last_dist
            if d is not None:
                values['distance'] = int(round(d * 100))
                last_dist = d
        out += _data(1, rf, [values.get(_name('record', num))
                             for num, _kind in rf])

    # --- lap ---------------------------------------------------------------
    lap_values = {'timestamp': end_s or None, 'start_time': start_s or None,
                  'total_elapsed_time': int(round(total_dur * 1000)),
                  'total_distance': int(round(total_dist * 100)),
                  'avg_speed': (int(round(total_dist / total_dur * 1000))
                                if total_dur > 0 else None)}
    lf = _present('lap', lap_values, always=('total_elapsed_time',
                                             'total_distance'))
    out += _definition(2, 'lap', lf)
    out += _data(2, lf, [lap_values.get(_name('lap', num)) for num, _k in lf])

    # --- session -----------------------------------------------------------
    session_values = {
        'timestamp': end_s or None, 'start_time': start_s or None,
        'sport': 1,                       # running
        'sub_sport': 0,                   # generic
        'total_elapsed_time': int(round(total_dur * 1000)),
        'total_distance': int(round(total_dist * 100)),
        'total_calories': int(calories) if calories else None,
        'avg_speed': (int(round(total_dist / total_dur * 1000))
                      if total_dur > 0 else None),
        'total_ascent': int(ascent) if ascent is not None else None,
        'total_descent': int(descent) if descent is not None else None,
        'num_laps': 1,
        'avg_cadence': int(avg_cadence) if avg_cadence else None,
    }
    sf = _present('session', session_values,
                  always=('sport', 'sub_sport', 'total_elapsed_time',
                          'total_distance', 'num_laps'))
    out += _definition(3, 'session', sf)
    out += _data(3, sf, [session_values.get(_name('session', num))
                         for num, _ in sf])

    # --- activity ----------------------------------------------------------
    act = {'timestamp': end_s or None,
           'total_timer_time': int(round(total_dur * 1000)),
           'num_sessions': 1, 'type': 0, 'event': 26,   # activity, stop_all
           'event_type': 1}                             # stop
    af = _present('activity', act)
    out += _definition(4, 'activity', af)
    out += _data(4, af, [act.get(_name('activity', num)) for num, _ in af])

    return _finish(bytes(out))


def _name(msg: str, num: int) -> str:
    for key, (n, _kind) in F[msg].items():
        if n == num:
            return key
    raise KeyError(num)


def _present(msg: str, values: dict, always=()) -> list:
    """Fields to write: always the required ones, plus every set value."""
    out = []
    for key in F[msg]:
        num, kind = F[msg][key]
        if key in always or (values.get(key) is not None
                             and values.get(key) != 0):
            out.append((num, kind))
    return out


def _finish(messages: bytes) -> bytes:
    header_crc = crc16(struct.pack('<BBHI4s', 14, 0x10, 2144, len(messages),
                                   b'.FIT'))
    header = struct.pack('<BBHI4sH', 14, 0x10, 2144, len(messages), b'.FIT',
                         header_crc)
    file_crc = crc16(messages, crc16(header))
    return header + messages + struct.pack('<H', file_crc)


# --------------------------------------------------------------------------
# structure check (used by tests and the UI's "is this parseable" note)
# --------------------------------------------------------------------------

def inspect(blob: bytes) -> dict:
    """Walk the record headers: header CRC, message counts, file CRC.

    This is not a full decoder; it proves the framing a parser walks is
    intact and reports which message types are present.
    """
    result = {'ok': False, 'size': len(blob), 'messages': {}, 'error': ''}
    if len(blob) < 14 or blob[8:12] != b'.FIT':
        result['error'] = '不是 FIT 文件'
        return result
    header_size = blob[0]
    data_size = struct.unpack('<I', blob[4:8])[0]
    result['header_crc_ok'] = crc16(blob[:12]) == struct.unpack(
        '<H', blob[12:14])[0]
    end = header_size + data_size
    if len(blob) < end + 2:
        result['error'] = '文件被截断'
        return result
    result['file_crc_ok'] = crc16(blob[header_size:end]) == struct.unpack(
        '<H', blob[end:end + 2])[0]
    definitions = {}
    pos = header_size
    counts: dict[int, int] = {}
    while pos < end:
        head = blob[pos]
        pos += 1
        if head & 0x80:                       # compressed timestamp header
            local = (head & 0x60) >> 5
            data = definitions.get(local)
            if data is None:
                result['error'] = '压缩头引用了未定义的 local 类型'
                return result
            pos += data['size']
        elif head & 0x40:                     # definition
            local = head & 0x0F
            pos += 1                          # reserved
            pos += 1                          # architecture (little endian)
            gnum = struct.unpack('<H', blob[pos:pos + 2])[0]
            pos += 2
            count = blob[pos]
            pos += 1
            size = 0
            for _ in range(count):
                size += blob[pos + 1]
                pos += 3
            definitions[local] = {'global': gnum, 'size': size}
            counts.setdefault(gnum, 0)
        else:                                 # data
            local = head & 0x0F
            data = definitions.get(local)
            if data is None:
                result['error'] = '数据消息引用了未定义的 local 类型'
                return result
            counts[data['global']] = counts.get(data['global'], 0) + 1
            pos += data['size']
    names = {v: k for k, v in MSG.items()}
    result['messages'] = {names.get(k, 'msg%d' % k): v
                          for k, v in counts.items()}
    result['ok'] = bool(result['header_crc_ok'] and result['file_crc_ok']
                        and counts.get(MSG['record'], 0) >= 2
                        and counts.get(MSG['session'], 0) == 1)
    return result


def export(track, start_ms: int = 0, filename: str = '', **kwargs) -> dict:
    """``{filename, mime, data_b64, points}`` for the bridge."""
    blob = build(track, start_ms, **kwargs)
    if not filename:
        stamp = (_dt.datetime.fromtimestamp(int(start_ms) / 1000.0,
                                            tz=_dt.timezone.utc)
                 .strftime('%Y%m%d-%H%M') if start_ms else 'run')
        filename = 'haoxiangpao-%s.fit' % stamp
    # CJK names are fine in a download; only path/URL-hostile characters go.
    filename = ''.join(c for c in str(filename)
                       if c.isalnum() or c in '-_. ').strip(' .')[:80]
    filename = filename.strip() or 'run.fit'
    if not filename.lower().endswith('.fit'):
        filename += '.fit'
    return {'filename': filename, 'mime': MIME,
            'data_b64': base64.b64encode(blob).decode('ascii'),
            'points': len(_track_rows(track)), 'bytes': len(blob)}


def self_test() -> list[tuple[str, bool, str]]:
    track = [{'lat': 22.5 + i * 1e-4, 'lng': 113.9, 'altitude': 20.0 + i,
              'distance_m': i * 12.0, 'duration_s': i} for i in range(10)]
    blob = build(track, start_ms=1_700_000_000_000)
    info = inspect(blob)
    return [('fit framing', info['ok'], str(info['messages'])),
            ('fit record count', info['messages'].get('record') == 10,
             str(info['messages']))]
