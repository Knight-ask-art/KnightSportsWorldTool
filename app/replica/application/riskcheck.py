"""Pre-flight risk checks — the things that get accounts flagged.

Everything here is derived from the community findings in the upstream issue
tracker, not from the app binary:

* **IP city vs campus city** (issues #2 / #16).  The run detail page shows the
  *IP* location, not the GPS coordinates.  A run at 江门 with an IP in 大连 is
  immediately obvious.  Recommendation there: use mobile data, not campus wifi.
* **valid running window** (issue #64).  The scored run is only accepted
  06:30–22:00 Beijing time.
* **device city consistency** (issue #19).  The device fingerprint's city and the
  track's city must agree.
* **stale violation check** (#87): enforcement is retrospective, so a clean
  self-check today says nothing about last week.

`preflight()` returns a list of warnings; it never blocks by itself.
"""

from __future__ import annotations

import json
import re
import urllib.request

from ..domain import safety

#: free, key-less, no-login endpoints; tried in order
IP_APIS = [
    ('http://ip-api.com/json/?lang=zh-CN&fields=status,country,regionName,city,isp,query', 'ip-api'),
    ('https://ipinfo.io/json', 'ipinfo'),
]

CAMPUS_CITY_HINTS = (
    '北京', '上海', '天津', '重庆', '广州', '深圳', '江门', '珠海', '东莞', '佛山',
    '中山', '惠州', '汕头', '湛江', '肇庆', '清远', '韶关', '梅州', '河源', '阳江',
    '茂名', '潮州', '揭阳', '云浮', '汕尾', '杭州', '南京', '苏州', '无锡', '宁波',
    '温州', '合肥', '福州', '厦门', '泉州', '南昌', '济南', '青岛', '烟台', '郑州',
    '武汉', '长沙', '成都', '绵阳', '西安', '兰州', '昆明', '贵阳', '南宁', '桂林',
    '海口', '三亚', '沈阳', '大连', '长春', '哈尔滨', '石家庄', '太原', '呼和浩特',
    '乌鲁木齐', '拉萨', '银川', '西宁',
)


# --------------------------------------------------------------------------
# IP location
# --------------------------------------------------------------------------

def fetch_ip_info(timeout: float = 8.0) -> dict | None:
    """Best-effort public IP + city. Returns None when offline/blocked."""
    for url, kind in IP_APIS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'jhp/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode('utf-8', 'replace'))
            if kind == 'ip-api':
                if body.get('status') != 'success':
                    continue
                return {'ip': body.get('query'), 'country': body.get('country'),
                        'region': body.get('regionName'), 'city': body.get('city'),
                        'isp': body.get('isp'), 'source': kind}
            if kind == 'ipinfo':
                loc = body.get('loc', '')
                return {'ip': body.get('ip'), 'country': body.get('country'),
                        'region': body.get('region'), 'city': body.get('city'),
                        'isp': body.get('org'), 'source': kind,
                        'loc': loc}
        except Exception:  # noqa: BLE001
            continue
    return None


# --------------------------------------------------------------------------
# campus city
# --------------------------------------------------------------------------

def campus_city(campus_name: str, school_name: str = '') -> str | None:
    """Pull the city out of a campus/school name like '广州软件学院 江门校区'.

    The campus segment wins: '广州软件学院 江门校区' is 江门, not 广州.  So the
    name is split on whitespace/dashes and searched from the end.
    """
    parts = [p for p in re.split(r'[\s\-—_]+', campus_name or '') if p]
    seen = []
    for text in list(reversed(parts)) + [campus_name or '', school_name or '']:
        if text and text not in seen:
            seen.append(text)
            hits = [c for c in CAMPUS_CITY_HINTS if c in text]
            if hits:
                # last occurrence wins: "广州软件学院江门校区" is 江门, not 广州
                return max(hits, key=lambda c: (text.rfind(c), len(c)))
    return None


def _norm_city(name: str) -> str:
    if not name:
        return ''
    return re.sub(r'(市|地区|自治州|盟)$', '', str(name)).strip()


def device_city(campus_name: str = '', school_name: str = '') -> str:
    """Value for the submit body's `address` field.

    Upstream fills `address` from the device identity's `city`
    (`src/api/flow.rs` L274: `address: client.identity.city.clone()`), and that
    field's default is ``"大连市"`` (`src/crypto/header.rs` `default_city`).
    Issue #16 — "修改锚点经纬度后，跑步详情点进去后仍然显示大连市" — is exactly
    that default leaking through.  So `address` must be a *city name*, not a
    school name, and it must be the city the run happened in.
    """
    city = campus_city(campus_name, school_name)
    if not city:
        return ''
    return city if city.endswith('市') else city + '市'


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def preflight(campus_name: str = '', school_name: str = '',
              start_ms: int | None = None,
              expect_city: str | None = None,
              ip_info: dict | None = None,
              explicit_start: bool = False) -> dict:
    """Run every cheap check and return {'ok', 'warnings', 'info'}.

    ``explicit_start`` means the operator picked the start time deliberately
    (09:00-22:00 backfill).  In that case the 07:00-20:00 sampling-window
    warning would be noise: that window describes where the reference *samples*
    a random start, not a server rule.
    """
    warnings: list[str] = []
    info: dict = {}

    # --- 1. IP city ----------------------------------------------------
    ip = ip_info if ip_info is not None else fetch_ip_info()
    info['ip'] = ip
    if ip:
        ip_city = _norm_city(ip.get('city'))
        want = expect_city or campus_city(campus_name, school_name)
        info['expected_city'] = want
        if want and ip_city and want not in ip_city and ip_city not in want:
            warnings.append(
                'IP 城市「%s」与校区城市「%s」不一致 —— 跑步详情页显示的是 IP 归属地，'
                '这会直接暴露。建议改用手机流量热点。' % (ip_city, want))
        elif want and ip_city:
            info['ip_city_matches'] = True
    else:
        warnings.append('无法获取公网 IP 归属地（离线或被拦截），请自行确认与校区同城。')

    # --- 2. valid running window ---------------------------------------
    start_ms = start_ms if start_ms is not None else safety.now_ms()
    info['start_beijing'] = safety.fmt_beijing(start_ms)
    info['start_explicit'] = bool(explicit_start)
    if not explicit_start and not safety.within_valid_window(start_ms):
        warnings.append(
            '开始时间 %s 不在 07:00–20:00 内。这是上游随机抽样的窗口'
            '（run.rs RAND_HOUR_LO/HI），不是服务端规则——服务端到底怎么判没有公开证据，'
            '但落在窗口外会让记录看起来不像这个客户端产生的。'
            % safety.fmt_beijing(start_ms))

    # --- 3. future / stale start ---------------------------------------
    skew = start_ms - safety.now_ms()
    if skew > 60_000:
        warnings.append('开始时间比当前时间晚 %d 分钟，服务器可能判定异常。'
                        % (skew // 60000))

    # --- 4. reminder ----------------------------------------------------
    warnings.append(
        '提醒：平台的审查是回溯性的（会批量清理历史记录），'
        '今天自查干净不代表之前没问题。')

    return {'ok': not [w for w in warnings if not w.startswith('提醒')],
            'warnings': warnings, 'info': info}


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def self_test() -> list[tuple[str, bool, str]]:
    out = []

    out.append(('campus city 江门',
                campus_city('广州软件学院 江门校区', '广州软件学院') == '江门',
                str(campus_city('广州软件学院 江门校区', '广州软件学院'))))
    out.append(('campus city 广州',
                campus_city('广州软件学院 本校区', '广州软件学院') == '广州',
                str(campus_city('广州软件学院 本校区', '广州软件学院'))))
    out.append(('campus city 江门 (no space)',
                campus_city('广州软件学院江门校区') == '江门',
                str(campus_city('广州软件学院江门校区'))))

    fake_ip = {'ip': '1.2.3.4', 'city': '大连', 'region': '辽宁',
               'country': '中国', 'source': 'test'}
    r = preflight('广州软件学院 江门校区', ip_info=fake_ip,
                  start_ms=_noon_ms())
    out.append(('city mismatch warned',
                any('不一致' in w for w in r['warnings']),
                [w for w in r['warnings'] if '不一致' in w][:1]))

    good_ip = {'ip': '1.2.3.4', 'city': '江门', 'region': '广东',
               'country': '中国', 'source': 'test'}
    r2 = preflight('广州软件学院 江门校区', ip_info=good_ip, start_ms=_noon_ms())
    out.append(('city match clean',
                not any('不一致' in w for w in r2['warnings']),
                'no mismatch'))

    late = _late_ms()
    r3 = preflight('广州软件学院 江门校区', ip_info=good_ip, start_ms=late)
    out.append(('late start warned',
                any('07:00' in w for w in r3['warnings']),
                [w for w in r3['warnings'] if '07:00' in w][:1]))

    out.append(('_norm_city strips 市', _norm_city('江门市') == '江门', '江门'))
    out.append(('device_city 江门市',
                device_city('广州软件学院 江门校区', '广州软件学院') == '江门市',
                device_city('广州软件学院 江门校区', '广州软件学院')))
    out.append(('device_city unknown -> empty', device_city('某学院', '') == '',
                repr(device_city('某学院', ''))))
    return out


def _noon_ms() -> int:
    import datetime as dt
    d = dt.datetime.now(safety.BEIJING).replace(hour=12, minute=0,
                                                second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _late_ms() -> int:
    import datetime as dt
    d = dt.datetime.now(safety.BEIJING).replace(hour=23, minute=0,
                                                second=0, microsecond=0)
    return int(d.timestamp() * 1000)


if __name__ == '__main__':
    ok = True
    for name, passed, detail in self_test():
        print('%-30s %s   %s' % (name, 'PASS' if passed else 'FAIL', detail))
        ok = ok and passed
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    raise SystemExit(0 if ok else 1)
