"""One-shot pipeline: login -> checkpoints -> forge -> sign -> guard -> submit.

    python -m replica.pipeline --user 137... --pass *** --km 2.5
    python -m replica.pipeline --token <tok> --uid <uid> --km 2.5
    python -m replica.pipeline --token ... --dry-run        # no upload
    python -m replica.pipeline --token ... --custom "A地址" "B地址"

Everything is pure protocol: no emulator, no unidbg, no browser (see netsec.py
and gt4.py).  A write-ahead receipt blocks accidental double submission.

Exit codes: 0 ok, 1 login/validation failure, 2 submission rejected,
            3 guarded (overlap / unknown previous outcome),
            4 blocked by the local gate (daily cap / night window).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from ..domain import health, safety
from ..application.guard import AlreadySubmitted, GuardError, ReceiptStore, UnknownPending
from ..transport.realclient import Account, RealClient, as_dict
from ..domain.rundata import (build_upload_entity, generate_run_data,
                              new_run_uuid, sign_upload_entity, validate_run_data)


_LOG_STREAM = None


def log(msg):
    print(msg, file=_LOG_STREAM or sys.stdout, flush=True)


def _fail(msg, code=1):
    log('!! ' + msg)
    return code


def _cli_fmt_seconds(seconds):
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return '%d 小时 %d 分' % (hours, minutes)
    if minutes:
        return '%d 分 %d 秒' % (minutes, secs)
    return '%d 秒' % secs


def _keepalive_cli(args):
    """`--keepalive` / `--keepalive-once`: probe, optionally re-login.

    There is no refresh-token endpoint in the official API.  A probe proves
    liveness; a fresh password login is the only reliable renewal.  The
    password is held in memory and is never written to the session file.
    """
    from ..application.tokenkeeper import TokenKeeper, probe_real_session, relogin_real, ensure_state
    from ..application import session_store
    from ..build_info import app_root

    path = os.path.join(app_root(), 'data', 'accounts_emu.json')

    if args.token:
        if not args.uid:
            return _fail('--token 需要配合 --uid', 1)
        session = session_store.read(path)
        session['uid'] = str(args.uid)
        session['token'] = args.token
        session.pop('disabled', None)
        session.pop('disabled_reason', None)
        session.pop('disabled_at', None)
        state = ensure_state(session)
        state['token_updated_at'] = int(time.time())
        state['token_expired'] = False
        state['last_error'] = ''
        session_store.write(path, session)
        log('已写入本机会话 token（DPAPI 封存，不打印）')

    session = session_store.read(path)
    if session.get('disabled'):
        return _fail('该账号已禁用；请换账号，或显式写入新 token 后再试', 1)
    if not session.get('token'):
        return _fail('未配置 token；先登录，或传 --token/--uid', 1)

    auto = bool(args.auto_relogin)
    if auto and not (args.user and args.password):
        return _fail('--auto-relogin 需要 --user/--pass（密码只在内存）', 1)

    keeper = TokenKeeper(
        path,
        probe=lambda s: probe_real_session(
            s, proxy=args.proxy, store_dir=os.path.dirname(path)),
        relogin=(lambda u, p: relogin_real(
            u, p, store_dir=os.path.dirname(path))) if auto else None,
        interval_s=args.keepalive_interval,
        auto_relogin=auto,
        username=args.user,
        password=args.password,
    )
    if args.keepalive_once:
        res = keeper.run_once(force=True)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get('ok') else 1

    log('token 保活已启动：每 %d 秒探测一次；Ctrl+C 停止'
        % keeper.interval_s)
    log('只做轻量 user/info 探测；自动重登：%s'
        % ('开（密码仅在内存）' if auto else '关'))
    keeper.run_forever()
    return 0


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def do_login(args):
    from ..application import device_profile as DP
    from ..application.login_flow import login
    store = DP.default_store_dir()
    device = DP.load_or_create(store, legacy_session=DP.read_session(store))
    if args.device_id:
        device = dict(device, DeviceId=args.device_id)
    if args.device_model:
        device = dict(device, deviceName=args.device_model)
    if args.os_version:
        device = dict(device, osVersion=args.os_version)
    log('[1/6] 登录（纯协议，本地解极验）...')
    sess = login(args.user, args.password, captcha_id=args.captcha_id, log=log,
                 device=device)
    log('      uid=%s unid=%s name=%s'
        % (sess['uid'], sess['unid'], sess['profile'].get('name')))
    return sess


def _ensure_hits(route, cps, args, RT):
    """Refuse (or repair) a user route that does not pass the checkpoints."""
    if not route or len(route) < 2 or not cps:
        return route
    missed = RT.missed_points(route, cps)
    if not missed:
        log('      路线经过全部 %d 个点位' % len(cps))
        return route

    must = [m for m in missed if m['isMustPoint']]
    log('      ! 路线没经过 %d 个点位（其中必经 %d 个）'
        % (len(missed), len(must)))
    for m in missed[:6]:
        log('        - %s 差 %.1fm%s'
            % (m['name'] or '(未命名)', m['distance_m'],
               ' [必经]' if m['isMustPoint'] else ''))

    if getattr(args, 'snap_route', False):
        route = RT.snap_through(route, missed)
        log('      已把 %d 个点位插入路线（会有绕行）' % len(missed))
        return route
    if getattr(args, 'allow_missed_points', False):
        log('      ! 按 --allow-missed-points 继续：记录很可能不达标')
        return route
    raise ValueError(
        '路线没经过 %d 个点位。加 --snap-route 自动插入，'
        '或 --allow-missed-points 强行提交' % len(missed))


def _tagged_route(pts, args, RT, closed=None):
    """Close (optionally) and smooth a user-supplied polyline."""
    if closed is None:
        closed = bool(getattr(args, 'route_close', False))
    if closed and pts[0] != pts[-1]:
        pts = list(pts) + [pts[0]]
    samples = int(getattr(args, 'smooth', 10) or 0)
    return RT.smooth(pts, samples=samples, closed=closed) if samples else list(pts)


def do_points(client, acct, args, must_points=None, sequential=False,
              policy_data=None):
    """Return (checkpoints, waypoints).

    Checkpoints are fetched **first** and independently of the route: a route
    without them yields a record that can never be complete (this used to be
    the behaviour of both `--custom` and the UI's hand-drawn waypoints).

    An empty checkpoint list is not a free run by default.  `--no-points` is
    the explicit operator confirmation; otherwise the CLI stops, matching the
    GUI's "跳过打卡点" checkbox.
    """
    from ..application import points as P
    from ..domain import routes as RT
    from ..application import policy as POL

    if args.no_points and must_points:
        raise ValueError('学校策略要求必经点位，不能使用 --no-points（会生成'
                         '永远不完整的记录）')
    if args.no_points and sequential:
        raise ValueError('学校要求顺序经过点位，不能使用 --no-points')

    cps = []
    if not args.no_points:
        log('[2/6] 拉取校园实时点位...')
        try:
            cps = P.fetch_points(client, acct, acct.center_lat,
                                 acct.center_lng,
                                 force=args.force_points, log=log,
                                 distance_m=float(getattr(args, 'km', 2.5)) * 1000.0)
            log('      %d 个点位（%d 个必经）'
                % (len(cps), sum(1 for p in cps if p['isMustPoint'])))
        except Exception as e:  # noqa: BLE001
            raise ValueError('点位获取失败，已停止（避免生成不完整记录）: %s'
                             % e)
        if not cps:
            raise ValueError('服务器没有返回打卡点，无法确认该校区是否允许'
                             '自由跑；如已确认，请显式使用 --no-points')
    else:
        log('[2/6] 跳过点位（--no-points；这不是官方自由跑）')

    # ---- pick the route ------------------------------------------------
    if getattr(args, 'route_file', None):
        log('      读取路线文件 %s ...' % args.route_file)
        pts = RT.load_route(args.route_file,
                            frame=getattr(args, 'route_frame', 'auto'))
        pts = _tagged_route(pts, args, RT)
        log('      文件路线 %d 点（平滑后）' % len(pts))
        source = 'file'

    elif args.custom:
        from ..transport import amap
        log('      高德路线（%s）...' % args.custom_mode)
        pts = amap.custom_route(start_address=args.custom[0],
                                end_address=args.custom[1],
                                mode=args.custom_mode,
                                city=acct.campusName or '',
                                key=args.amap_key)
        pts = _tagged_route(
            pts, args, RT,
            closed=True if args.custom_mode == 'loop'
            else bool(getattr(args, 'route_close', False)))
        log('      高德路线 %d 点（平滑后）' % len(pts))
        source = 'custom'

    elif getattr(args, 'stadium', None):
        from ..domain import stadium
        log('      田径场模式（周长 %.0fm）...' % args.stadium)
        if cps:
            fit = stadium.make_stadium_ring(
                [(c['lat'], c['lon']) for c in cps], args.stadium)
            log('      拟合跑道：角度 %+.0f°，最大偏差 %.1fm（允许 %.1f）'
                % (fit['angle_deg'], fit['max_deviation_m'],
                   fit['tolerance_m']))
            pts = fit['ring']
            source = 'stadium'
        else:
            pts = stadium.stadium_route(acct.center_lat, acct.center_lng,
                                        perimeter_m=args.stadium,
                                        bearing_deg=getattr(args, 'bearing', 0.0),
                                        n_points=360)
            log('      无点位，使用中心点几何 %d 点，实测周长 %.0fm'
                % (len(pts), stadium.perimeter_of(pts + [pts[0]])))
            source = 'stadium'

    elif args.no_points:
        return [], []

    else:
        if not cps:
            log('      没有点位可用，使用本地环形路线')
            return [], []
        pts = P.route_order(cps, acct.center_lat, acct.center_lng,
                            sequential=sequential)
        if must_points:
            if sequential:
                log('      策略必经点 %d 个（顺序跑，按点位编号保留）'
                    % len(must_points))
                missing = [p for p in must_points
                           if not any(abs(p[0] - q[0]) < 1e-6
                                      and abs(p[1] - q[1]) < 1e-6
                                      for q in pts)]
                if missing:
                    pts.extend(missing)
                    log('      顺序跑：策略必经点不在点位列表，已追加 %d 个'
                        % len(missing))
            else:
                before = len(pts)
                pts = P.merge_required(must_points, pts)
                log('      策略必经点 %d 个（并入后 +%d 个航点）'
                    % (len(must_points), len(pts) - before))
        log('      点位路线 %d 个航点' % len(pts))
        source = 'points'

    # ---- policy must-points must be on a user route too ----------------
    # They are prepended by merge_required; rotate back so the user's first
    # tap remains the start of the loop.  Only do this for closed loops —
    # rotating an open A→B route would reorder it incorrectly.
    closed_user_route = (
        (source == 'custom' and getattr(args, 'custom_mode', 'loop') == 'loop')
        or source == 'stadium'
        or bool(getattr(args, 'route_close', False))
    )
    if (source != 'points' and must_points and pts and closed_user_route
            and not sequential):
        first = pts[0]
        pts = P.merge_required(must_points, pts)
        pts = P.rotate_to(pts, first)
    elif source != 'points' and sequential and must_points:
        log('      顺序跑：保留当前路线顺序，不把策略必经点前置')
        log('      策略必经点并入用户路线 %d 个' % len(must_points))

    # ---- a user route must still pass the checkpoints -------------------
    if source != 'points' and cps:
        pts = _ensure_hits(pts, cps, args, RT)

    # ---- geofence: the run has to happen inside the campus -----------------
    if pts:
        try:
            area = POL.select_run_area(
                policy_data, 0,
                campus_name=acct.campusName,
                school_name=acct.schoolName) \
                if policy_data else None
            if area is not None and area.get('valid') is False:
                raise ValueError('跑区有效时间已更新，请重新选择')
            fence = P.fetch_fence(client, acct, policy_data=policy_data,
                                  run_area=area)
            rings = fence.get('polygons') or (
                [fence['polygon']] if fence.get('polygon') else [])
            if not rings:
                log('      该校区没有配置围栏，跳过围栏校验')
            else:
                bad = RT.outside_fences(pts, rings)
                bad_seg = RT.outside_fence_segments_multi(pts, rings)
                if bad['count'] or bad_seg['count']:
                    worst = max([p['distance_m'] for p in bad['points']] or [0])
                    seg_worst = max([s['distance_m'] for s in bad_seg['segments']] or [0])
                    log('      ! 路线有 %d/%d 个点不在任何围栏内（最远 %.0fm）'
                        % (bad['count'], bad['total'], worst))
                    log('      ! 路线有 %d/%d 段穿过围栏外（最远 %.0fm）'
                        % (bad_seg['count'], bad_seg['total'], seg_worst))
                    for b in bad['points'][:3]:
                        log('        · 第 %d 点 越界 %.0fm'
                            % (b['index'] + 1, b['distance_m']))
                    for s in bad_seg['segments'][:3]:
                        log('        · 第 %d→%d 段 越界 %.0fm'
                            % (s['index'] + 1, s['index'] + 2, s['distance_m']))
                    log('        ! 围栏外的路段很可能被判无效')
                else:
                    log('      路线全部在围栏内（%d 个围栏，%d 段）'
                        % (len(rings), bad_seg['total']))
        except Exception as e:  # noqa: BLE001
            raise ValueError('围栏校验失败，已停止: %s' % e)

    return cps, pts


def _altitude_kwargs(args):
    """Resolve the elevation flags into generator kwargs.

    An explicit ``--altitude-range`` implies ``range`` mode and an explicit
    template implies ``template`` mode.  The template is read and parsed on
    this machine only; it is never written back or uploaded.
    """
    from ..domain.rundata import parse_altitude_template

    mode = getattr(args, 'altitude_mode', None)
    rng = getattr(args, 'altitude_range', None)
    path = getattr(args, 'altitude_template', None)
    profile = None
    if path:
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(p):
            raise ValueError('海拔模板不存在: %s' % p)
        if os.path.getsize(p) > 8 * 1024 * 1024:
            raise ValueError('海拔模板超过 8 MiB')
        with open(p, 'r', encoding='utf-8-sig', errors='replace') as f:
            profile = parse_altitude_template(f.read())
        mode = mode or 'template'
    if mode is None:
        mode = 'range' if rng else 'auto'
    if mode == 'range' and not rng:
        raise ValueError('--altitude-mode range 需要 --altitude-range LO HI')
    if mode == 'template' and not profile:
        raise ValueError('--altitude-mode template 需要 --altitude-template PATH')
    return {'altitude_mode': mode, 'altitude_range': rng,
            'altitude_profile': profile}


def do_forge(acct, cps, wps, args, start_ms=None, sequential=False):
    log('[3/6] 造数...')
    try:
        alt = _altitude_kwargs(args)
        raw_kt = str(getattr(args, 'km_times', '') or '').strip()
        km_times = ([float(x) for x in raw_kt.split(',') if x.strip()]
                    if raw_kt else None)
        run = generate_run_data(
            distance_km=args.km, pace_min_per_km=args.pace,
            cadence=args.cadence,
            center_lat=acct.center_lat, center_lng=acct.center_lng,
            waypoints=wps or None, checkpoints=cps or None,
            weight_kg=args.weight,
            altitude_base=getattr(args, 'altitude', 25.0),
            start_ms=start_ms,
            run_uuid=new_run_uuid(getattr(acct, 'platform', None)),
            km_times=km_times,
            sequential=sequential,
            **alt)
    except ValueError as e:
        raise SystemExit(_fail('造数参数无效: %s' % e, 1))
    issues = validate_run_data(run)
    if issues:
        raise SystemExit(_fail('造数校验未通过: %s' % issues, 1))
    locs = run['allLocJson']
    log('      %.2fkm / %ds / %d 点 / %.0fkcal / 爬升 %.1fm'
        % (run['juli'] / 1000.0, run['sportTime'], len(locs),
           run['calorie'], run['totalAscent']))
    mode = alt.get('altitude_mode', 'auto')
    if mode == 'template':
        alt_note = '模板 %d 点' % len(alt.get('altitude_profile') or [])
    elif mode == 'range':
        lo, hi = alt.get('altitude_range') or ('?', '?')
        alt_note = '区间 %s–%s m' % (lo, hi)
    elif mode == 'fixed':
        alt_note = '固定 %.0f m' % getattr(args, 'altitude', 25.0)
    else:
        alt_note = '基准 %.0f m' % getattr(args, 'altitude', 25.0)
    log('      海拔 %s（%s）' % (mode, alt_note))
    log('      起止 %s → %s'
        % (safety.fmt_beijing(locs[0]['ts'], '%m-%d %H:%M:%S'),
           safety.fmt_beijing(locs[-1]['ts'], '%H:%M:%S')))
    return run


def _raw_record(client, acct, args):
    """Print one record's raw detail JSON on stdout; diagnostics go to stderr.

    Piping the output therefore stays valid JSON even when the bridge logs
    progress or an OBS fallback warning.
    """
    global _LOG_STREAM
    from ..application import readonly as RO
    from ..build_info import version_string

    _LOG_STREAM = sys.stderr
    log('[record] 读取 rrid=%s ...' % args.record_json)
    try:
        out = RO.raw_record_detail(
            client, acct, int(args.record_json),
            uuid=getattr(args, 'record_uuid', None),
            include_obs=bool(getattr(args, 'record_obs', False)))
    except Exception as e:  # noqa: BLE001
        return _fail('原始详情读取失败: %s' % e, 1)
    out['fetched_at'] = safety.fmt_beijing(safety.now_ms())
    out['build'] = version_string()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def do_sign(run, acct, args, policy_data=None):
    log('[4/6] 签名...')
    from ..application import policy as POL
    from ..application import riskcheck

    # `address` is the device city (upstream flow.rs fills it from
    # identity.city, whose default is 大连市 — that default is issue #16).
    city = riskcheck.device_city(acct.campusName, acct.schoolName)
    if not city:
        city = '%s %s' % (acct.schoolName, acct.campusName)

    ctx = {
        'uid': acct.uid,
        'selectedUnid': acct.unid,
        'sportType': 1,
        'geeToken': '',
        'address': city,
        # policy comes from the server, not from us (flow.rs uses pol.policy)
        'policy': POL.policy_value(policy_data, args.policy),
    }
    # upstream puts runRuleModel.minDistance in selDistance
    min_d = POL.rule_min_distance(policy_data)
    if min_d > 0:
        ctx['selDistance'] = min_d

    ent = sign_upload_entity(build_upload_entity(run, ctx))
    log('      policy=%s address=%s selDistance=%s'
        % (ent['policy'], ent['address'],
           ent['selDistance']))
    log('      avgStepFreq=%s signature=%s'
        % (ent['avgStepFreq'], ent['signature'][:16] + '...'))
    return ent


def do_guard(acct, ent, run):
    log('[5/6] 回执...')
    store = ReceiptStore()
    store.reserve(acct.uid, ent['uuid'], ent['startTime'], ent['stopTime'],
                  school=str(acct.unid), distance_m=run['juli'])
    log('      已锁定时段（uuid=%s）' % ent['uuid'][:8])
    return store


def do_policy(client, acct):
    """Fetch the server policy.

    A malformed must-point list is an error, not an omission: silently dropping
    a required checkpoint would produce a record that can never be complete
    (policy.rs raises the same way).  Callers may override deliberately.
    """
    from ..application import policy as POL
    pol = None
    for rm in (1, 2):
        try:
            pol = POL.fetch_policy(client, acct, run_mode=rm)
            break
        except Exception as e:  # noqa: BLE001
            log('      runMode=%d 不可用: %s' % (rm, e))
    if pol is None:
        return None, None

    ts = POL.policy_timestamp(pol)
    try:
        mp = POL.extract_must_points(pol)
    except Exception as e:  # noqa: BLE001
        # Keep the policy/fence; the caller decides whether to continue with
        # --ignore-must-points.  Returning None here would also discard the
        # only authoritative fence source.
        pol['_must_points_error'] = str(e)
        mp = []
    freeze = POL.freeze_seconds(pol)
    face_on, face_why = POL.face_required(pol)
    log('      runMode=%d policy=%s timestamp=%s minDistance=%s 必经点=%d'
        % (rm, pol.get('policy'), ts,
           POL.rule_min_distance(pol) or '-', len(mp)))
    if pol.get('_must_points_error'):
        log('      ! 策略必经点无法解析: %s' % pol['_must_points_error'])
    if freeze > 0:
        log('      ! 账号处于跑步冻结期（剩余 %s）；仅提醒，不阻止提交'
            % _cli_fmt_seconds(freeze))
    if face_on:
        log('      ! 学校启用了人脸抽检（%s）；本工具无法完成，将停止提交'
            % face_why)
    if mp:
        pol['_must_points'] = mp
    return pol, ts


def do_submit(client, acct, run, ent, store, policy_ts=None):
    log('[6/6] 提交...')
    res = client.submit_run(acct, run, ent, ent, policy_ts=policy_ts)
    log('      HTTP %s  error=%s  %s'
        % (res.get('status'), res.get('error'), res.get('message') or ''))
    log('      runes=%s runef=%s' % (res.get('runes'), res.get('runef')))

    if res.get('error') != 10000 or not res.get('rrid'):
        if res.get('error') in (None, 0):
            state, head = 'unknown', '提交结果未知（已锁定该时段，请勿重试）'
        elif res.get('error') == 10000:
            state, head = 'unknown', '服务器已受理但未返回记录号，结果未知（已锁定该时段）'
        else:
            state, head = 'rejected', '提交被拒'
        store.mark(acct.uid, ent['uuid'], state,
                   school=str(acct.unid), note=str(res.get('message'))[:120])
        log('      %s: error=%s %s' % (head, res.get('error'), res.get('message')))
        return res, 2

    store.mark(acct.uid, ent['uuid'], 'accepted',
               rrid=res['rrid'], school=str(acct.unid))
    log('      rrid=%s uuid=%s' % (res['rrid'], res['uuid']))

    # OBS trail: this is what the replay reads
    try:
        from ..transport import obs as OBS
        time.sleep(1.0)                      # reference: flow.rs sleeps 1 s here
        fpj = json.dumps(run['fixed_point_json'], separators=(',', ':'))
        o = OBS.upload_obs(client, acct, run, res['rrid'], res['uuid'], fpj, log=log)
        log('      OBS %d/%d 个对象，回读校验 %d 个，%d 字节'
            % (o['uploaded'], o['of'], o['verified'], o['bytes']))
        if o['uploaded'] < o['of'] or o['verified'] < o['of']:
            log('      ! OBS 未全部成功或校验不一致: %s' % (o.get('errors') or []))
        res['obs'] = o
        time.sleep(2.0)                      # reference: sleeps 2 s before verify
    except Exception as e:  # noqa: BLE001
        log('      OBS 上传失败: %s' % e)
        res['obs_error'] = str(e)
    return res, 0


def do_readback(client, acct, res):
    try:
        back = client.request(acct, 'api/v70260/runnings/get_one_record', {
            'uuid': res['uuid'], 'rrid': res['rrid'], 'calculateBadge': False})
        d = as_dict(as_dict(back.get('json')).get('data'))
    except Exception as e:  # noqa: BLE001
        log('      回读失败: %s' % e)
        return None

    reasons = d.get('reasonList') or []
    bad = [r for r in reasons if not r.get('complete')]
    log('      isCheat=%s complete=%s policy=%s dis=%s time=%s'
        % (d.get('isCheat'), d.get('complete'), d.get('policy'),
           d.get('totalDis'), d.get('totalTime')))
    log('      完成度 %d/%d' % (len(reasons) - len(bad), len(reasons)))
    for r in reasons:
        log('        %s type=%-3s %s'
            % ('OK ' if r.get('complete') else 'MISS', r.get('type'), r.get('reason')))
    if d.get('statusInfo'):
        log('      "%s"' % d['statusInfo'])
    return d


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None, client_factory=None):
    global _LOG_STREAM
    from ..build_info import version_string
    ap = argparse.ArgumentParser(description='运动世界 一键全链（纯协议）')
    ap.add_argument('--version', action='version', version=version_string(),
                    help='打印版本与构建标识后退出')
    auth = ap.add_argument_group('账号')
    auth.add_argument('--user', help='手机号（走纯协议登录）')
    auth.add_argument('--pass', dest='password', help='密码')
    auth.add_argument('--captcha-id', default=None)
    auth.add_argument('--token', help='已有 token（跳过登录）')
    auth.add_argument('--uid', default=None)
    auth.add_argument('--unid', type=int, default=47203)
    auth.add_argument('--device-id', default=None)
    auth.add_argument('--device-model', default=None)
    auth.add_argument('--os-version', default=None)

    run_g = ap.add_argument_group('跑步')
    run_g.add_argument('--km', type=float, default=2.5)
    run_g.add_argument('--pace', type=float, default=6.0, help='分钟/公里')
    run_g.add_argument('--cadence', type=int, default=150)
    run_g.add_argument('--km-times', default='', metavar='S1,S2,...',
                       help='每公里用时（秒），逗号分隔；覆盖 --pace 的总时长')
    run_g.add_argument('--weight', type=float, default=health.DEFAULT_WEIGHT_KG)
    run_g.add_argument('--altitude', type=float, default=25.0,
                       help='海拔基准（auto 模式）或固定海拔（fixed 模式），米')
    run_g.add_argument('--altitude-mode', choices=('auto', 'fixed', 'range', 'template'),
                       default=None,
                       help='海拔来源：auto=合成，fixed=固定，range=区间，template=本地 GPX/JSON')
    run_g.add_argument('--altitude-range', type=float, nargs=2, metavar=('LO', 'HI'),
                       default=None,
                       help='海拔区间（米）；给定时默认 range 模式')
    run_g.add_argument('--altitude-template', default=None, metavar='PATH',
                       help='本地 GPX/JSON 海拔模板；只在本机解析，不上传')
    run_g.add_argument('--days-ago', type=int, default=None, metavar='N',
                       help='补跑日期：0=今天，1=昨天，最多 3 天前（北京时间）；'
                            '不填且不给 --time 时保持“以当前时间倒推”的旧行为')
    run_g.add_argument('--time', default=None, metavar='HH:MM',
                       help='开始时间，必须在 09:00–22:00；不填则在该窗口内随机')
    run_g.add_argument('--policy', type=int, default=1)

    route = ap.add_argument_group('路线')
    route.add_argument('--no-points', action='store_true', help='不拉校园点位')
    route.add_argument('--force-points', action='store_true', help='忽略点位缓存')
    route.add_argument('--custom', nargs=2, metavar=('START', 'END'),
                       help='高德自定义路线：起点/终点地址')
    route.add_argument('--custom-mode', choices=('single', 'loop'), default='loop')
    route.add_argument('--amap-key', default=None)
    route.add_argument('--route-file', default=None, metavar='PATH',
                       help='自己画的路线：json / gpx / csv（每行 lat,lng）')
    route.add_argument('--route-frame', choices=('auto', 'gcj', 'wgs84'),
                       default='auto',
                       help='坐标参考系；auto 时 gpx 按 wgs84、其余按 gcj')
    route.add_argument('--route-close', action='store_true',
                       help='把路线首尾相连（绕圈）')
    route.add_argument('--smooth', type=int, default=10, metavar='N',
                       help='每段插值点数（0=不插值，默认 10）')
    route.add_argument('--snap-route', action='store_true',
                       help='路线没经过的点位自动插入（会有绕行）')
    route.add_argument('--allow-missed-points', action='store_true',
                       help='即使漏点位也提交（记录很可能不达标）')
    route.add_argument('--stadium', type=float, nargs='?', const=400.0,
                       default=None, metavar='PERIMETER',
                       help='田径场模式，可指定周长（默认 400m）')
    route.add_argument('--bearing', type=float, default=0.0,
                       help='田径场朝向（度，0=长轴朝北）')

    ap.add_argument('--proxy', default=None,
                    help='出网代理（http://host:port）——让出口 IP 和校区同城')
    ap.add_argument('--dry-run', action='store_true', help='只造数不提交')
    ap.add_argument('--no-preflight', action='store_true', help='跳过风控预检')
    ap.add_argument('--random', action='store_true',
                    help='随机档位（距离/配速/步频按上游实现的区间抽样）')
    ap.add_argument('--seed', type=int, default=None, help='--random 的可复现种子')
    ap.add_argument('--km-range', type=float, nargs=2, default=(2.0, 3.0),
                    metavar=('LO', 'HI'),
                    help='--random 的距离区间（默认 2.0 3.0；这是配置选择，不是统计分布）')
    ap.add_argument('--plan', type=int, default=None, metavar='DAYS',
                    help='只打印未来 N 天的跑步节奏计划，不联网')
    ap.add_argument('--no-history', action='store_true',
                    help='不拉历史记录做同质化检查')
    ap.add_argument('--ignore-must-points', action='store_true',
                    help='策略必经点解析失败时继续（默认中止）')
    ap.add_argument('--ignore-model', action='store_true',
                    help='合格模型判定不合格也继续（默认中止）')
    ap.add_argument('--max-per-day', type=int, default=2,
                    help='每天最多提交几次（0=不限，默认 2）')
    ap.add_argument('--allow-night', action='store_true',
                    help='允许 23:00–06:00 提交')
    ap.add_argument('--no-limit', action='store_true',
                    help='关闭本地风控闸门（次数与深夜）')
    ap.add_argument('--json', action='store_true', help='输出 JSON 报告')
    rec = ap.add_argument_group('记录调试')
    rec.add_argument('--record-json', type=int, default=None, metavar='RRID',
                     help='只读取一条记录并打印原始详情 JSON 后退出'
                          '（stdout 仅 JSON，日志走 stderr）')
    rec.add_argument('--record-uuid', default=None, metavar='UUID',
                     help='--record-json 详情查询用的 uuid（可选）')
    rec.add_argument('--record-obs', action='store_true',
                     help='--record-json 时附带 OBS 原始对象'
                          '（jsonFromObs=1 会自动附带）')
    ap.add_argument('--logout', action='store_true',
                    help='退出登录并验证 token 失效（需要 --token/--uid）')
    ka = ap.add_argument_group('token 保活')
    ka.add_argument('--keepalive', action='store_true',
                    help='后台定时探测 token（Ctrl+C 停止）')
    ka.add_argument('--keepalive-once', action='store_true',
                    help='只探测一次并退出，适合 cron/计划任务')
    ka.add_argument('--keepalive-interval', type=int, default=6 * 3600,
                    metavar='SECONDS',
                    help='探测间隔秒数（默认 21600 = 6 小时）')
    ka.add_argument('--auto-relogin', action='store_true',
                    help='探测到失效时用 --user/--pass 自动重登；密码只在内存')
    args = ap.parse_args(argv)

    # Raw-record mode promises "stdout is JSON".  Switch every progress line
    # to stderr before login/device/profile code can print anything.
    if args.record_json is not None:
        _LOG_STREAM = sys.stderr

    if getattr(args, 'logout', False):
        from ..transport import realclient as R
        from ..application import device_profile as DP
        if not args.token or not args.uid:
            return _fail('--logout 需要 --token 和 --uid', 1)
        acct = R.Account(name='logout', uid=str(args.uid), token=args.token)
        store = DP.default_store_dir()
        session = DP.read_session(store)
        device = DP.effective_device(
            DP.load_or_create(store, legacy_session=session), session)
        DP.apply_to_account(acct, device)
        try:
            res = R.RealClient().logout_and_verify(acct)
        except Exception as e:  # noqa: BLE001
            return _fail('退出登录失败: %s' % e, 1)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get('ok') else 1

    if args.keepalive or args.keepalive_once:
        return _keepalive_cli(args)

    # ---- plan mode (offline) -------------------------------------------
    # `if args.plan:` used to be falsy for 0, which fell through to the account
    # path and asked for --user/--pass.
    if args.plan is not None:
        if args.plan < 1:
            return _fail('--plan 需要 >= 1 天', 1)
        from ..application import variation
        rng = random.Random(args.seed) if args.seed is not None else None
        slots = variation.plan(args.plan, rng=rng,
                               dist_band=tuple(args.km_range))
        print(variation.render_plan(slots))
        return 0

    report = {'ok': False}

    # ---- account ------------------------------------------------------
    if args.token:
        if not args.uid:
            return _fail('--token 需要配合 --uid', 1)
        acct = Account(name='cli', uid=str(args.uid), token=args.token,
                       unid=args.unid)
        log('[1/6] 使用现有 token uid=%s' % args.uid)
    else:
        if not (args.user and args.password):
            return _fail('需要 --user/--pass 或 --token/--uid', 1)
        try:
            sess = do_login(args)
        except Exception as e:  # noqa: BLE001
            return _fail('登录失败: %s' % e, 1)
        acct = Account(name=args.user, uid=sess['uid'], token=sess['token'],
                       unid=int(sess.get('unid') or args.unid))
        report['login'] = {k: sess['profile'].get(k) for k in
                           ('uid', 'name', 'campusName', 'unid')}

    # Device fingerprint: the installation profile is the single source of
    # truth.  --device-id/--device-model/--os-version are one-run overrides so
    # login and submit cannot drift apart.
    from ..application import device_profile as DP
    from ..application import identity
    store = DP.default_store_dir()
    device = DP.load_or_create(store, legacy_session=DP.read_session(store))
    if args.device_id:
        device['DeviceId'] = args.device_id
    if args.device_model:
        device['deviceName'] = args.device_model
    if args.os_version:
        device['osVersion'] = args.os_version
    try:
        DP.apply_to_account(acct, device)
    except Exception as e:  # noqa: BLE001
        return _fail('设备指纹无效: %s' % e, 1)
    if args.device_id:
        try:
            identity.remember(acct.uid, DeviceId=args.device_id)
        except Exception as e:  # noqa: BLE001
            log('   设备号记录失败（不影响本次提交）: %s' % e)
        log('   本次运行使用手动设备号；不传时回到本机设备指纹')

    if args.dry_run:
        log('   (--dry-run：不提交)')

    client = (client_factory or RealClient)(proxy=args.proxy)
    log('   crypto backend = %s' % client.backend)
    if client.proxy:
        log('   出口代理 = %s' % client.proxy)

    # ---- raw record debug (read-only, no submit) -----------------------
    if args.record_json is not None:
        return _raw_record(client, acct, args)

    # ---- policy + campus centre ----------------------------------------
    # policy supplies `runes`, the rule set, the run areas, and the only
    # authoritative fence.  The separate getGeoFenceForRun reply is never used
    # for validation (the official isInArea does not read it).
    try:
        pol, policy_ts = do_policy(client, acct)
    except Exception as e:  # noqa: BLE001
        if not getattr(args, 'ignore_must_points', False):
            return _fail('策略必经点无法解析，已中止（确认无影响可加 '
                         '--ignore-must-points）: %s' % e)
        log('      ! 已忽略策略必经点: %s' % e)
        pol, policy_ts = None, None
    if pol is None:
        return _fail('无法读取跑步策略，已停止（不会用旧围栏兜底）', 1)
    if pol.get('_must_points_error'):
        if not getattr(args, 'ignore_must_points', False):
            return _fail('策略必经点无法解析，已中止（确认无影响可加 '
                         '--ignore-must-points）: %s'
                         % pol['_must_points_error'])
        log('      ! 已忽略策略必经点: %s' % pol['_must_points_error'])
        pol['_must_points'] = []

    from ..application import policy as POL
    area = POL.select_run_area(
        pol, 0, campus_name=acct.campusName, school_name=acct.schoolName)
    if area is not None and area.get('valid') is False:
        return _fail('跑区有效时间已更新，请重新选择', 1)

    # Resolve the campus centre before planning anything.  There is no
    # built-in campus default: submitting from a guessed coordinate writes a
    # record for the wrong campus and cannot be undone.
    try:
        from ..application import points as P
        fence = P.fetch_fence(client, acct, policy_data=pol, run_area=area)
        polygons = fence.get('polygons') or (
            [fence['polygon']] if fence.get('polygon') else [])
        if not polygons:
            return _fail('学校没有配置跑步围栏，CLI 无法确定校区中心；'
                         '请改用图形界面设置校区', 1)
        if len(polygons) > 1:
            return _fail('检测到多个跑步围栏，CLI 无法确定校区中心；'
                         '请先在图形界面选择跑区或设置校区', 1)
        center = fence['center']
        if not center:
            return _fail('围栏中心无效，已停止', 1)
        acct.center_lat, acct.center_lng = float(center[0]), float(center[1])
        log('   校区围栏中心 %.6f, %.6f'
            % (acct.center_lat, acct.center_lng))
    except Exception as e:  # noqa: BLE001
        return _fail('无法获取校区围栏中心，已停止（不会使用默认坐标）: %s'
                     % e, 1)

    # ---- variation: pick this run's shape ------------------------------
    from ..application import variation as V

    history = []
    if not args.no_history:
        try:
            history = V.fetch_recent(client, acct)
        except Exception as e:  # noqa: BLE001
            log('[var] 历史记录读取失败（不影响提交）: %s' % e)

    chosen = None
    if args.random:
        rng = random.Random(args.seed) if args.seed is not None else None
        chosen = V.make_profile(rng=rng,
                                avoid=history[0] if history else None,
                                weight_kg=args.weight,
                                dist_band=tuple(args.km_range))
        args.km = chosen['distance_km']
        args.pace = chosen['pace_min_per_km']
        args.cadence = chosen['cadence']
        if not (args.stadium or args.custom or args.route_file) \
                and chosen['route'] == 'stadium':
            args.stadium = 400.0
        # The record is stamped with "now" — a tool cannot wait until 19:00.
        # The sampled hour is only a hint for when to run the tool.
        chosen['planned_hhmm'] = chosen['start_hhmm']
        chosen['start_ms'] = safety.now_ms()
        chosen['start_hhmm'] = safety.to_beijing(
            chosen['start_ms']).strftime('%H:%M')
        log('[var] 随机档位：%.2fkm %s %dspm 路线=%s'
            % (chosen['distance_km'], chosen['pace_hhmmss'],
               chosen['cadence'], chosen['route']))
    else:
        chosen = {'distance_km': args.km, 'pace_min_per_km': args.pace,
                  'start_ms': safety.now_ms(),
                  'pace_hhmmss': "%d'%02d\""
                                 % (int(args.pace), int(round(args.pace * 60)) % 60)}

    if history:
        log('[var] 最近 %d 条记录' % len(history))
        for w in V.similarity_warnings(chosen, history):
            log('[var] ! ' + w)
    elif not args.no_history:
        log('[var] 无历史记录可对比')

    report['variation'] = chosen

    if not (args.km > 0) or not (args.pace > 0):
        return _fail('--km 和 --pace 必须为正数（当前 %.2f / %.2f）'
                     % (args.km, args.pace), 1)
    if not (args.cadence > 0):
        return _fail('--cadence 必须为正数（当前 %s）' % args.cadence, 1)

    # ---- backfill start time (optional) --------------------------------
    # With neither --days-ago nor --time the tool keeps its historical
    # behaviour: the run ends 1-5 s before "now".  Supplying either one opts
    # into the 09:00-22:00 window (today .. 3 days ago).
    explicit_day = args.days_ago is not None
    explicit_time = args.time is not None and str(args.time).strip() != ''
    backfill = explicit_day or explicit_time
    resolved_start = None
    if backfill:
        days_ago = 0 if args.days_ago is None else int(args.days_ago)
        duration_s = args.km * args.pace * 60.0
        try:
            resolved_start = safety.resolve_start_ms(
                days_ago, args.time, duration_s)
        except ValueError as e:
            return _fail('开始时间无效: %s' % e, 1)
        chosen['start_ms'] = resolved_start
        chosen['start_hhmm'] = safety.to_beijing(
            resolved_start).strftime('%H:%M')
        log('[time] %s 开始（%s）'
            % (safety.fmt_beijing(resolved_start),
               '今天' if days_ago == 0 else '%d 天前' % days_ago))
    else:
        chosen['start_ms'] = safety.now_ms()

    # ---- pre-flight risk checks ---------------------------------------
    if not getattr(args, 'no_preflight', False):
        from ..application import riskcheck
        rc = riskcheck.preflight(acct.campusName, acct.schoolName,
                                 start_ms=resolved_start,
                                 explicit_start=backfill)
        ip = rc.get('info', {}).get('ip') or {}
        if ip:
            log('[pre] IP %s · %s%s%s'
                % (ip.get('ip'), ip.get('country') or '',
                   ip.get('region') or '', ip.get('city') or ''))
        for w in rc.get('warnings', []):
            log('[pre] ' + w)
        report['preflight'] = rc

    # ---- network steps -------------------------------------------------
    from ..application import policy as POL
    freeze = POL.freeze_seconds(pol) if pol else 0
    face_on, face_why = POL.face_required(pol) if pol else (False, '')
    sequential = bool(pol) and POL.policy_value(pol, 0) == 1
    if face_on:
        return _fail('学校启用了人脸抽检（%s），本工具无法完成人脸校验，'
                     '已停止提交（不会伪造 faceCheck=1）' % face_why)
    report['freeze_seconds'] = freeze
    report['face_required'] = face_on
    report['sequential'] = sequential

    try:
        cps, wps = do_points(client, acct, args,
                             must_points=(pol or {}).get('_must_points'),
                             sequential=sequential, policy_data=pol)
    except ValueError as e:
        return _fail('路线不可用: %s' % e, 1)
    run = do_forge(acct, cps, wps, args, start_ms=resolved_start,
                   sequential=sequential)

    # ---- user-defined school rules -> qualification model --------------
    from ..application import qualification as Q
    from ..application import limits as L
    from ..application import readonly as RO
    rules = Q.load_rules()
    run_start = int(run['locPoints'][0]['flag'])
    run_bj = safety.to_beijing(run_start)
    day_key = run_bj.strftime('%Y-%m-%d')
    report['start'] = {
        'ms': run_start, 'text': safety.fmt_beijing(run_start),
        'days_ago': (safety.to_beijing(safety.now_ms()).date()
                     - run_bj.date()).days,
    }
    day_runs, day_source = RO.daily_count(client, acct, day_key)
    try:
        local_runs = int(L.load(acct.uid, run_start).get('runs', 0))
    except Exception:  # noqa: BLE001
        local_runs = 0
    if day_runs is None:
        day_runs, day_source = local_runs, 'local'
    else:
        day_runs = max(day_runs, local_runs)
    model = Q.evaluate(run, rules, checkpoints=cps, daily_runs=day_runs,
                       daily_source=day_source)
    report['model'] = model
    report['daily'] = {'date': day_key, 'runs': day_runs,
                       'source': day_source}
    if not model['configured']:
        log('[model] 学校要求未配置，跳过模型校验（设置 → 学校要求）')
    else:
        log('[model] %d 分 · %s' % (model['score'], model['summary']))
        for w in model['warnings'][:6]:
            log('[model] ! ' + w)
        if model['verdict'] == 'fail' and not getattr(args, 'ignore_model', False):
            bad = [c['label'] + ' ' + c['value'] + '（' + c['detail'] + '）'
                   for c in model['checks'] if c['status'] == 'fail']
            return _fail('合格模型未通过: %s（确认无影响可加 --ignore-model）'
                         % '；'.join(bad), 5)

    ent = do_sign(run, acct, args, pol)
    report['upload'] = {'uuid': ent['uuid'], 'policy': ent['policy'],
                        'km': args.km, 'pace': args.pace,
                        'address': ent['address'],
                        'selDistance': ent['selDistance'],
                        'checkpoints': len(cps), 'waypoints': len(wps)}

    if args.dry_run:
        log('\n[dry-run] 造数+签名完成，未提交。')
        report['ok'] = True
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    # ---- local risk gate: daily cap + night window --------------------
    from ..application import limits
    if not args.no_limit:
        refusal = limits.check(
            acct.uid, ent['startTime'],
            max_per_day=args.max_per_day,
            block_night=not args.allow_night)
        if refusal:
            return _fail(refusal + '（--no-limit 可完全关闭）', 4)
        st = limits.load(acct.uid, ent['startTime'])
        gate_day = '今天' if day_key == safety.to_beijing(
            safety.now_ms()).strftime('%Y-%m-%d') else '所选日期'
        log('[gate] %s已提交 %d 次（上限 %s）'
            % (gate_day, st.get('runs', 0),
               args.max_per_day if args.max_per_day > 0 else '不限'))

    # ---- guard + submit ------------------------------------------------
    try:
        store = do_guard(acct, ent, run)
    except (AlreadySubmitted, UnknownPending) as e:
        return _fail('回执拦下: %s' % e, 3)
    except GuardError as e:
        return _fail('回执写入失败，已停止: %s' % e, 3)

    try:
        res, code = do_submit(client, acct, run, ent, store, policy_ts=policy_ts)
    except Exception as e:  # noqa: BLE001
        store.mark(acct.uid, ent['uuid'], 'unknown',
                   school=str(acct.unid), note=str(e)[:120])
        return _fail('提交异常，结果未知（已锁定该时段）: %s' % e, 3)

    report['upload'].update({k: res.get(k) for k in
                             ('status', 'error', 'message', 'rrid', 'uuid')})

    if code == 0:
        if not args.no_limit:
            limits.record(acct.uid, ent['startTime'])
        d = do_readback(client, acct, res)
        if d:
            reasons = d.get('reasonList') or []
            report['readback'] = {
                'isCheat': d.get('isCheat'), 'complete': d.get('complete'),
                'policy': d.get('policy'),
                'incomplete': [r.get('reason') for r in reasons if not r.get('complete')],
            }
        report['ok'] = True

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已取消。')
        sys.exit(130)
