"""Run-flow bridge mixin: fence, points, preview, snap and submit.

The methods stay on ``ReplicaApi`` via inheritance so every existing bridge
call and test keeps the same signature.  Keeping the run flow out of the
5k-line adapter module makes changes local; the four-language native boundary
is untouched.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import time

from ..application import device_profile as DP
from ..domain.rundata import generate_run_data, new_run_uuid, preview_series
from ..transport.realclient import as_dict
from .bridge_common import (
    CAMPUS_POINTS_DISTANCE_M,
    _fail,
    _fail_submit,
    _fmt_seconds,
    _normalise_ui_points,
    _ok,
    plausibility_warnings,
)


class RunBridgeMixin:
    def _ensure_fence(self, acct, client=None, policy_data=None,
                      run_area=None, anchor=None) -> dict:
        """Read this account's policy fence and resolve a campus centre.

        The fence list is authoritative evidence for in-area checks, but it
        does not always identify a single centre: a run area may own several
        polygons and there may be no run area at all.  With one polygon its
        centroid is the anchor; with several, use a saved/manual centre or the
        route's first point only when it is actually inside one of them.
        Never pick the first polygon just because it is first.
        """
        if client is None:
            from ..transport import realclient as R
            client = R.RealClient()
        from ..application import points as P
        if policy_data is None:
            raise P.PolicyUnavailable(
                'runModePolicy 未返回，无法确定围栏；不会用旧围栏兜底')
        fence = P.fetch_fence(client, acct, policy_data=policy_data,
                              run_area=run_area)
        polygons = fence.get('polygons') or (
            [fence['polygon']] if fence.get('polygon') else [])

        if not polygons:
            if self._center_ok(acct.center_lat, acct.center_lng):
                fence['center'] = (acct.center_lat, acct.center_lng)
                fence['center_source'] = 'manual'
                return fence
            # Keep the reason: a failed legacy fallback ("读取失败") must not
            # read like "this school has no fence".
            reason = fence.get('reason') or '学校未配置跑步围栏'
            raise P.FenceNotConfigured('%s，且本地没有校区坐标' % reason)

        from ..domain import routes as RT
        if len(polygons) == 1:
            center = fence.get('center')
            if not center or not self._center_ok(center[0], center[1]):
                raise RuntimeError('围栏中心无效: %r' % (center,))
            lat, lng = float(center[0]), float(center[1])
            acct.center_lat, acct.center_lng = lat, lng
            self._save_center(lat, lng)
            fence['center'] = (lat, lng)
            fence['center_source'] = 'fence'
            return fence

        manual = None
        if self._center_ok(acct.center_lat, acct.center_lng):
            manual = (float(acct.center_lat), float(acct.center_lng))
        if manual and RT.point_in_fences(manual[0], manual[1], polygons):
            fence['center'] = manual
            fence['center_source'] = 'manual'
            return fence

        if anchor and len(anchor) == 2 and RT.point_in_fences(
                float(anchor[0]), float(anchor[1]), polygons):
            lat, lng = float(anchor[0]), float(anchor[1])
            # Transient only: a route's first point must not become the
            # account's campus for later checkpoint queries.
            acct.center_lat, acct.center_lng = lat, lng
            fence['center'] = (lat, lng)
            fence['center_source'] = 'route'
            return fence

        error = P.FenceAmbiguous(
            '检测到多个跑步围栏，无法确定校区中心：请先在跑区下拉框选择跑区，'
            '或把地图拖到学校后点「设为校区」')
        # Keep the polygons on the exception so the preview can still draw
        # them without adopting an out-of-fence map centre.
        error.fence = fence
        raise error

    def real_fetch_points(self, school='', glat=0.0, glon=0.0, unid=None,
                          force=False, distance_m=CAMPUS_POINTS_DISTANCE_M,
                          run_area_id=None):
        """Fetch the campus geofence + campus list from the real server.

        Both replies are real NetSecKit envelopes, decoded via the native w1228
        entry (the earlier "DEC unsolved" note no longer applies).
        """
        acct = self._real_account()
        if acct is None:
            return _fail('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        if unid is not None:
            acct.unid = int(unid)
        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail(f'sidecar 启动失败: {e}')

        try:
            dist_m = max(1, int(round(float(distance_m))))
        except (TypeError, ValueError):
            dist_m = CAMPUS_POINTS_DISTANCE_M
        # The checkpoint hit radius is a protocol constant
        # (RunSeqLocationManager ticks at `distance <= 25.0`).  The run page
        # checklist used to hard-code 40 m, so it could show "all points hit"
        # for a route the server would score as incomplete.  Send the value
        # with the points so the UI never re-invents it.
        from ..domain.routes import HIT_RADIUS_M
        out = {'unid': acct.unid, 'fence': None, 'fences': [],
               'campuses': None, 'polygon': None, 'center': None,
               'errors': [], 'notes': [],
               'fence_missing': False, 'fence_count': 0,
               'points_missing': False, 'points_empty': False,
               'checkpoints': None, 'points_ok': None,
               'points_distance_m': None, 'points_stale': False,
               'points_source': '', 'points_rate_limited': False,
               'points_retry_after_s': 0,
               'run_area': None, 'run_area_valid': None,
               'run_areas': [], 'distance_m': dist_m,
               'hit_radius_m': HIT_RADIUS_M}

        # 0) The account's run areas come from runModePolicy, exactly like
        # `RunPolicyViewModel.getPolicy()` (selectUnid = this account's unid).
        # The official default is selectedPosition=0; the dialog only changes
        # it when the operator explicitly picks another area.
        from ..application import policy as POL
        policy_data = self._fetch_run_policy(rc, acct)
        area = POL.select_run_area(
            policy_data, 0,
            campus_name=acct.campusName,
            school_name=acct.schoolName) \
            if policy_data else None
        if run_area_id not in (None, '', -1, '-1'):
            # Operator picked an area in the run page (official
            # RunningAreaDialog).  Only an area the server currently offers
            # may override the default.
            chosen = POL.find_run_area(policy_data, run_area_id)
            if chosen:
                area = chosen
            else:
                out['notes'].append('所选跑区不可用，已按默认跑区重新匹配')
        out['run_area'] = area
        out['run_area_valid'] = (None if area is None
                                 else area.get('valid') is not False)
        out['run_areas'] = POL.run_area_models(policy_data) \
            if policy_data else []
        if area is not None and area.get('valid') is False:
            out['notes'].append('默认跑区不在有效时间内，请在跑区下拉框选择其他跑区')
        freeze = POL.freeze_seconds(policy_data) if policy_data else 0
        face_on, face_why = (POL.face_required(policy_data)
                             if policy_data else (False, ''))
        sequential = bool(policy_data) and POL.policy_value(policy_data, 0) == 1
        out['freeze_seconds'] = freeze
        out['face_required'] = face_on
        out['face_reason'] = face_why
        out['sequential'] = sequential
        if not policy_data:
            out['notes'].append('跑区信息未读取，围栏/点位使用通用匹配')
        if freeze > 0:
            out['notes'].append(
                '账号处于跑步冻结期（剩余 %s）；官方客户端此时不能开始跑步，'
                '本工具只提醒，不阻止提交' % _fmt_seconds(freeze))
        if face_on:
            out['notes'].append(
                '学校启用了人脸抽检（%s）；本工具无法完成人脸校验，提交会被阻止'
                % face_why)

        # 1) geofence polygon + authoritative centre.  A school without a
        # fence is a valid configuration (the official client treats an empty
        # list as "always in area"), so that is a note, not an error.  The
        # caller's glat/glon is the fallback anchor when the fence is absent.
        from ..application import points as P
        anchor = (float(glat), float(glon)) if self._center_ok(glat, glon) \
            else None
        fence_polygons = []
        try:
            fence = self._ensure_fence(acct, rc, policy_data=policy_data,
                                       run_area=area, anchor=anchor)
            polygons = fence.get('polygons') or (
                [fence['polygon']] if fence.get('polygon') else [])
            fence_polygons = polygons
            out['fences'] = [
                {'name': fence.get('name') or '', 'points': [
                    {'glat': a, 'glon': b} for a, b in ring]}
                for ring in polygons
            ]
            out['fence'] = out['fences']
            out['fence_count'] = len(polygons)
            if polygons:
                out['polygon'] = [{'n': i + 1, 'lat': a, 'lon': b}
                                  for i, (a, b) in enumerate(polygons[0])]
            else:
                out['fence_missing'] = True
                out['notes'].append('该校区没有配置跑步围栏，不限制跑步位置')
            out['center'] = {'lat': round(acct.center_lat, 6),
                             'lon': round(acct.center_lng, 6)}
        except P.FenceNotConfigured as e:
            out['fence'] = []
            out['fences'] = []
            out['fence_count'] = 0
            out['fence_missing'] = True
            out['notes'].append('该校区没有配置跑步围栏，但本地没有校区坐标')
            out['errors'].append(f'fence: {e}')
        except P.PolicyUnavailable as e:
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
            return _fail(str(e), data=out)
        except P.FenceAmbiguous as e:
            ambiguous = getattr(e, 'fence', None) or {}
            fence_polygons = ambiguous.get('polygons') or []
            out['fences'] = [
                {'name': ambiguous.get('name') or '', 'points': [
                    {'glat': a, 'glon': b} for a, b in ring]}
                for ring in fence_polygons
            ]
            out['fence'] = out['fences']
            out['fence_count'] = len(fence_polygons)
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'fence: {e}')
            out['fence_error'] = str(e)
        if not self._center_ok(acct.center_lat, acct.center_lng) \
                and self._center_ok(glat, glon):
            from ..domain import routes as RT
            # Only adopt the caller's map centre when there is no fence, or
            # when it is actually inside one of the fences.  An ambiguous
            # multi-fence school must not persist an out-of-area point.
            if (not fence_polygons
                    or RT.point_in_fences(float(glat), float(glon),
                                          fence_polygons)):
                acct.center_lat, acct.center_lng = float(glat), float(glon)
                self._save_center(glat, glon)
        if self._center_ok(acct.center_lat, acct.center_lng):
            out['center'] = {'lat': round(acct.center_lat, 6),
                             'lon': round(acct.center_lng, 6)}

        if not self._center_ok(acct.center_lat, acct.center_lng):
            return _fail('无法获取校区围栏/坐标：请把地图拖到你的学校，点「设为校区」；'
                         '或先画一条以学校为起点的路线。'
                         + ('（%s）' % '; '.join(out['errors'])
                            if out['errors'] else ''),
                         data=out)

        # 2) campus list for this school
        try:
            res = rc.request(acct, f'api/v61101/campus/zonelist?unid={acct.unid}',
                             method='GET', retries=2)
            obj = res.get('json') or {}
            if obj.get('error') == 10000:
                out['campuses'] = obj.get('data')
            else:
                out['errors'].append(f'zonelist: error={obj.get("error")} {obj.get("message")}')
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'zonelist: {e}')

        # 3) live checkpoints (the 必经点位 the replay draws)
        try:
            meta = P.fetch_points_meta(
                rc, acct, acct.center_lat, acct.center_lng,
                run_area_id=area['id'] if area else None,
                force=bool(force), distance_m=dist_m, allow_empty=True)
        except P.PointsRateLimited as e:
            out['errors'].append(f'points: {e}')
            out['points_error'] = str(e)
            out['points_ok'] = False
            out['points_rate_limited'] = True
            out['points_retry_after_s'] = int(
                getattr(e, 'retry_after', 0) or 0)
        except Exception as e:  # noqa: BLE001
            out['errors'].append(f'points: {e}')
            out['points_error'] = str(e)
            out['points_ok'] = False
        else:
            pts = meta['points']
            out['checkpoints'] = pts
            out['points_ok'] = True
            out['points_distance_m'] = meta['distance_m']
            out['points_stale'] = bool(meta['stale'])
            out['points_source'] = meta['source']
            out['points_rate_limited'] = bool(meta['rate_limited'])
            out['points_retry_after_s'] = int(meta['retry_after_s'] or 0)
            out['mandatory'] = [p for p in pts if p['isMustPoint']]
            out['route_order'] = P.route_order(
                pts, acct.center_lat, acct.center_lng,
                sequential=sequential)
            if meta['stale']:
                out['notes'].append(
                    '点位接口限流，沿用 %d m 的缓存点位（当前目标 %d m）'
                    % (meta['distance_m'], dist_m))
            if not pts:
                # Preview may display "no points", but it is not proof that
                # the campus allows a free run.  Submit defaults to blocking.
                out['points_empty'] = True
                out['notes'].append(
                    '服务器没有返回打卡点；无法确认该校区是否允许自由跑，'
                    '提交时默认阻止')

        # A missing fence/points list is not a failure once a campus centre is
        # known.  Errors stay in ``errors`` so the UI can show them as notes.
        if out['center']:
            return _ok(out)
        return _fail('; '.join(out['errors']) or '未取到围栏', data=out)

    def real_preview_model(self, km=2.5, pace=6.0, cadence=150,
                           altitude=25.0, waypoints=None, checkpoints=None,
                           days_ago=0, start_time=None,
                           daily_runs=None, daily_source=None, date=None,
                           closed=True, altitude_mode='auto',
                           altitude_range=None, altitude_profile=None,
                           km_times=None, with_series=False, seed=None,
                           sequential=False, pause_profile='none',
                           gps_profile='urban', terrain_coupling=False):
        """Evaluate the user-entered rules against a local preview run.

        This is offline (no server call).  It lets the UI show a real score
        before submission instead of a hard-coded demo number.  The final
        submission re-evaluates the actual generated track.

        ``date`` (``YYYY-MM-DD``) / ``start_time`` describe the backfill slot
        the operator picked; ``days_ago`` remains as the CLI shorthand.
        ``daily_runs`` is the server count the caller already knows; when it is
        omitted the check stays explicitly ``unknown`` instead of pretending
        this tool's local receipt counter proves anything about the server.
        """
        from ..domain.rundata import generate_run_data
        from ..application import qualification as Q
        from ..domain import safety as S
        try:
            wps = [(float(a), float(b)) for a, b in (waypoints or [])]
            cps = []
            for p in (checkpoints or []):
                if not isinstance(p, dict):
                    continue
                lat = p.get('lat', p.get('glat'))
                lon = p.get('lon', p.get('glon'))
                if lat is None or lon is None:
                    continue
                cps.append({'lat': float(lat), 'lon': float(lon),
                            'name': p.get('name') or p.get('pointName') or '',
                            'isMustPoint': (bool(p.get('isMustPoint'))
                                            or str(p.get('isFixed')).strip()
                                            .lower() in ('1', 'true', 'yes',
                                                         'y', '是')),
                            'isFixed': p.get('isFixed'),
                            'position': p.get('position'),
                            'seq': p.get('seq')})
            d = self._read_session()
            lat = float(d.get('lat') or 0)
            lng = float(d.get('lon') or 0)
            if not self._center_ok(lat, lng):
                if not wps:
                    return _fail('尚未解析校区坐标，请先加载围栏/点位')
                lat, lng = wps[0]
            from ..domain.rundata import (estimate_pause_seconds,
                                          planned_active_seconds)
            duration_s = planned_active_seconds(km, pace, km_times)
            duration_s += estimate_pause_seconds(
                pause_profile, None, int(duration_s))
            rules = Q.load_rules()
            try:
                start_ms = S.resolve_start_ms(
                    days_ago, start_time, duration_s, date=date,
                    windows=rules.get('windows'))
            except ValueError as e:
                return _fail('开始时间无效: %s' % e)
            try:
                run_seed = int(seed) if seed is not None else secrets.randbits(32)
            except (TypeError, ValueError):
                run_seed = secrets.randbits(32)
            run = generate_run_data(
                float(km), float(pace), int(cadence),
                center_lat=lat, center_lng=lng,
                rng=random.Random(run_seed),
                waypoints=wps or None,
                checkpoints=cps or None,
                altitude_base=float(altitude),
                altitude_mode=altitude_mode or 'auto',
                altitude_range=altitude_range or None,
                altitude_profile=altitude_profile or None,
                start_ms=start_ms,
                closed=bool(closed),
                km_times=km_times,
                sequential=bool(sequential),
                pause_profile=pause_profile or 'none',
                gps_profile=gps_profile or 'urban',
                terrain_coupling=bool(terrain_coupling),
                run_uuid=new_run_uuid(
                    self._effective_device(d).get('platform')))
            self._last_run = run
            if daily_runs is None:
                # The caller explicitly says it does not know the server count.
                # Do not silently substitute this tool's local lower bound:
                # 0 local receipts is not proof that today has 0 runs.
                daily_source = 'unknown'
            else:
                try:
                    daily_runs = int(daily_runs)
                except (TypeError, ValueError):
                    daily_runs, daily_source = None, 'unknown'
            model = Q.evaluate(run, rules,
                               checkpoints=cps or None,
                               daily_runs=daily_runs,
                               daily_source=daily_source)
            return _ok({
                'model': model,
                'param_warnings': plausibility_warnings(pace, cadence),
                'seed': run_seed,
                'preview': preview_series(run) if with_series else None,
                'start': {'ms': start_ms,
                          'text': S.fmt_beijing(start_ms),
                          'days_ago': (S.to_beijing(S.now_ms()).date()
                                       - S.to_beijing(start_ms).date()).days},
                'daily': {'runs': daily_runs, 'source': daily_source,
                          'date': S.to_beijing(start_ms).strftime('%Y-%m-%d')},
                'run': {'km': run.get('km'), 'sportTime': run.get('sportTime'),
                        'juli': run.get('juli'),
                        'splits': run.get('km_splits') or [],
                        'points': len(run.get('allLocJson') or []),
                        'pause_s': run.get('pause_total_s', 0),
                        'gps_profile': run.get('gps_profile', 'urban'),
                        'terrain_coupling': bool(run.get('terrain_coupling'))},
            })
        except Exception as e:  # noqa: BLE001
            return _fail('模型预览失败: %s' % e)

    def real_snap_route(self, points=None, key=None, max_legs=24):
        """Snap a hand-drawn polyline to real walking paths (AMap).

        Each leg between two drawn vertices becomes a walking route; the result
        follows streets instead of cutting across buildings.  Slow by design
        (one request per leg, rate-limited), so the UI shows progress.
        """
        from ..transport import amap
        pts = [(float(a), float(b)) for a, b in (points or [])]
        if len(pts) < 2:
            return _fail('至少需要 2 个点')
        # Without a key every leg would silently degrade to a straight line and
        # the caller would think snapping worked.  Say it plainly instead.
        import os as _os
        if not (key or _os.environ.get(amap.KEY_ENV)):
            return _fail('吸附需要高德「Web服务」Key：在控制台创建后填到「高德Key」')
        try:
            res = amap.snap_polyline(pts, key=key,
                                     max_legs=int(max_legs or 24), delay=0.35)
        except Exception as e:  # noqa: BLE001
            return _fail('吸附失败: %s' % e)
        return _ok(res)

    def real_submit_run(self, km=2.5, pace=6.0, cadence=150,
                        waypoints=None, geeToken='', use_points=True,
                        altitude=25.0, snap_route=True,
                        days_ago=0, start_time=None, date=None,
                        closed=True, altitude_mode='auto',
                        altitude_range=None, altitude_profile=None,
                        km_times=None, seed=None, run_area_id=None,
                        points_distance_m=None, points=None,
                        pause_profile='none', gps_profile='urban',
                        terrain_coupling=False):
        """Forge a run, sign it with the official scheme, submit it for real,
        then read the record back so the caller sees the completion checklist.

        When use_points is true the campus checkpoints are pulled first (300 s
        cache) and the track is routed through them, so the replay draws the
        green ticks instead of an empty map.  policy defaults to 1 (see
        rundata.build_upload_entity): that is the rule set real 学分跑 records
        carry and the only one that reaches 6/6.

        A write-ahead receipt guards against double submission and against
        re-sending when a previous outcome is unknown.
        """
        message = self._integrity_message()
        if message:
            return _fail_submit(message, kind='fail')
        if self._device_migration:
            return _fail_submit(
                self._device_migration.get('message')
                or '设备指纹已修复，请重新登录后再提交')
        try:
            acct = self._real_account()
        except DP.DeviceProfileError as e:
            return _fail_submit(str(e))
        if acct is None:
            return _fail_submit('未配置 token（先在"账号"面板保存）')
        from ..transport import realclient as R
        from ..application import points as P
        from ..domain.rundata import build_upload_entity, sign_upload_entity
        from ..application.guard import ReceiptStore, AlreadySubmitted, UnknownPending, GuardError

        try:
            rc = R.RealClient()
        except Exception as e:  # noqa: BLE001
            return _fail_submit(f'sidecar/加密层启动失败: {e}')

        # Align to the server clock before ANY signed request in this flow:
        # a laptop several minutes fast used to make the first policy call
        # look stale and the generated start time look "in the future".
        # Fail-open: an offline check must not block a submission.
        clock_info = self._sync_server_clock(acct, rc=rc, force=True,
                                             timeout=8.0)

        # The submit coordinate normally comes from this account's real fence.
        # Some schools configure no fence at all (official client: empty fence
        # list means "always in area"), and a manual campus point / the first
        # waypoint is a legitimate anchor there.  A wrong coordinate is still
        # worse than stopping, so never fall back to a built-in city.
        wps = [(float(a), float(b)) for a, b in (waypoints or [])]
        submit_notes = []

        # ---- 0. policy: the account's run areas + fence + runes ----------
        # `RunPolicyViewModel.getPolicy()` sends selectUnid=user.unid and the
        # reply carries `runAreaModels` and `geoFence`.  That is the official,
        # per-account campus mapping; use it before any name/nearest guess.
        from ..application import policy as POL
        policy_data = self._fetch_run_policy(rc, acct)
        if not policy_data:
            return _fail_submit('无法读取跑步策略，已停止提交（不会用旧围栏或猜测校区）')
        area = POL.select_run_area(
            policy_data, 0,
            campus_name=acct.campusName,
            school_name=acct.schoolName)
        if run_area_id not in (None, '', -1, '-1'):
            chosen = POL.find_run_area(policy_data, run_area_id)
            if chosen:
                area = chosen
            else:
                submit_notes.append('所选跑区不可用，已按默认跑区重新匹配')
        if area is not None and area.get('valid') is False:
            return _fail_submit('跑区有效时间已更新，请重新选择')
        policy_ts = POL.policy_timestamp(policy_data)
        freeze = POL.freeze_seconds(policy_data)
        face_on, face_why = POL.face_required(policy_data)
        sequential = POL.policy_value(policy_data, 0) == 1
        if area:
            submit_notes.append('跑区：%s' % (area.get('name') or area.get('id')))
        if freeze > 0:
            # A freeze is a strong warning, not a hard block: the operator may
            # have a legitimate reason and the server decides.
            submit_notes.append(
                '账号处于跑步冻结期（剩余 %s）；官方客户端此时不能开始跑步，'
                '仍按你的要求继续提交' % _fmt_seconds(freeze))
        if face_on:
            # The real client can only report `faceCheck=1` after a face check.
            # This tool cannot perform one, so submitting would be a false
            # claim; stop instead of guessing.
            return _fail_submit(
                '学校启用了人脸抽检（%s），本工具无法完成人脸校验，已停止提交'
                '（不会伪造 faceCheck=1）' % face_why)
        if sequential:
            submit_notes.append('顺序跑：按点位编号排序并依次经过')

        fence = {'polygon': [], 'polygons': []}
        try:
            fence = self._ensure_fence(
                acct, rc, policy_data=policy_data, run_area=area,
                anchor=(wps[0] if wps else None))
            polygons = fence.get('polygons') or (
                [fence['polygon']] if fence.get('polygon') else [])
            if not polygons:
                submit_notes.append('该校区未配置围栏，无法校验轨迹是否在围栏内')
            elif fence.get('center_source') == 'route':
                submit_notes.append(
                    '检测到多个围栏：按路线首点确定本次校区（不会保存为账号校区）')
            elif fence.get('center_source') == 'manual':
                submit_notes.append('检测到多个围栏：按已保存的校区坐标提交')
        except P.PolicyUnavailable as e:
            return _fail_submit(str(e))
        except P.FenceAmbiguous as e:
            return _fail_submit(str(e))
        except P.FenceNotConfigured:
            center = None
            if self._center_ok(acct.center_lat, acct.center_lng):
                center = (acct.center_lat, acct.center_lng)
                self._save_center(*center)
                submit_notes.append('该校区未配置围栏，已按手动校区坐标提交')
            elif wps:
                # A route's first point is a valid transient anchor for a
                # school with no fence, but it is not the account's campus.
                center = wps[0]
                acct.center_lat, acct.center_lng = float(center[0]), float(center[1])
                submit_notes.append(
                    '该校区未配置围栏，已按路线首点确定本次坐标（不会保存为校区）')
            if center is None:
                return _fail_submit(
                    '学校未配置围栏，且没有可用的校区坐标：请先把地图'
                    '拖到学校后点「设为校区」，或画一条以学校为起点的路线')
        except Exception as e:  # noqa: BLE001
            # A saved centre says *where* the campus is, not whether the track
            # is inside its polygon.  Continuing here used to leave `fence`
            # empty and skip the in-area check entirely, so a transient read
            # failure could submit an out-of-fence record.
            return _fail_submit(
                '无法读取校区围栏，已停止提交（避免提交围栏外轨迹）: %s' % e)

        # ---- 1. live checkpoints -> route through them --------------------
        # Checkpoints are fetched even when a route was supplied: a record
        # without them can never be complete (this used to be skipped for any
        # hand-drawn or imported route).
        cps = []
        if use_points:
            # Prefer the exact set the operator saw on the map.  The endpoint
            # returns a different random five-point draw per request (see
            # points.py), so re-querying at submit time would route through
            # points that were never displayed.
            cps = _normalise_ui_points(points)
            if cps:
                submit_notes.append('使用地图上的 %d 个打卡点' % len(cps))
                try:
                    shown = float(points_distance_m or 0)
                    target = float(km) * 1000.0
                except (TypeError, ValueError):
                    shown, target = 0.0, 0.0
                if shown > 0 and target > 0 and abs(shown - target) >= 50:
                    submit_notes.append(
                        '点位按 %d m 加载，本次目标 %d m；提交复用该套点位'
                        % (int(round(shown)), int(round(target))))
            else:
                dist_points = P.points_distance_for_submit(
                    points_distance_m, km)
                try:
                    meta = P.fetch_points_meta(
                        rc, acct, acct.center_lat, acct.center_lng,
                        run_area_id=area['id'] if area else None,
                        distance_m=dist_points, allow_empty=False)
                    cps = meta['points']
                    if meta['stale']:
                        submit_notes.append(
                            '点位沿用 %d m 的缓存（当前目标 %d m；接口限流）'
                            % (meta['distance_m'],
                               int(round(float(km) * 1000))))
                    if not cps:
                        return _fail_submit(
                            '服务器没有返回打卡点，无法确认该校区是否允许自由跑；'
                            '如你确认学校没有点位，请勾选「跳过打卡点」后重试')
                except ValueError as e:
                    # Empty points are not proof of a free-run campus.  The
                    # decompiled client has no such branch on the normal
                    # distance endpoint; official free run is serviceType 3 /
                    # sportType 4.
                    return _fail_submit(
                        '服务器没有返回有效打卡点，无法确认该校区是否允许自由跑；'
                        '如你确认学校没有点位，请勾选「跳过打卡点」后重试: %s' % e)
                except Exception as e:  # noqa: BLE001
                    # A record without the mandatory checkpoint hits cannot be
                    # complete.  Submitting anyway created an
                    # accepted-but-invalid run, so stop before the write-ahead
                    # receipt is reserved.
                    return _fail_submit(
                        '点位加载失败，已停止提交（避免生成不完整记录）: %s' % e)
            if not wps:
                wps = P.route_order(cps, acct.center_lat, acct.center_lng,
                                    sequential=sequential)
        else:
            submit_notes.append(
                '已按操作员确认跳过打卡点；本工具尚未实现官方自由跑'
                '（serviceType=3），提交仍按普通跑步生成')

        # policy's must-points must be on the track even when the checkpoint
        # endpoint does not return them (policy.rs::route_points).  A malformed
        # list aborts, same as the reference: dropping a required point yields
        # a record that can never be complete.
        try:
            from ..application import policy as _POL
            mp = _POL.extract_must_points(policy_data) if policy_data else []
        except Exception as e:  # noqa: BLE001
            return _fail_submit(f'策略必经点无法解析，已中止: {e}')
        if not use_points and mp:
            return _fail_submit(
                '学校策略要求必经点位，不能跳过打卡点'
                '（请取消「跳过打卡点」后重试）')
        if not use_points and sequential:
            return _fail_submit(
                '学校要求顺序经过点位，不能跳过打卡点'
                '（请取消「跳过打卡点」后重试）')
        if mp:
            if sequential:
                # `merge_required` prepends must-points and `rotate_to` moves
                # the user's first point back to index 0; either would break
                # the school's numbering for a sequential run.  The checkpoint
                # list already carries every point the route must visit, so
                # only append a policy point that is genuinely absent.
                missing = []
                for p in mp:
                    if not any(abs(p[0] - q[0]) < 1e-6
                               and abs(p[1] - q[1]) < 1e-6 for q in wps):
                        missing.append(p)
                if missing:
                    wps.extend(missing)
                    submit_notes.append(
                        '顺序跑：策略必经点不在点位列表，已追加到路线末尾')
                else:
                    submit_notes.append('顺序跑：策略必经点已包含在点位序列中')
            else:
                first = wps[0] if wps else None
                wps = P.merge_required(mp, wps)
                # The user's first tap is the start.  Policy must-points are
                # prepended by merge_required, so rotate the loop back to it.
                wps = P.rotate_to(wps, first)

        # ---- 1b. a user route must still pass the checkpoints -----------
        from ..domain import routes as RT
        missed = []
        if wps and cps:
            missed = RT.missed_points(wps, cps)
            if missed:
                if snap_route:
                    wps = RT.snap_through(wps, missed)
                    missed = RT.missed_points(wps, cps)
                if missed:
                    names = '、'.join(m['name'] or '(未命名)'
                                      for m in missed[:8])
                    more = '' if len(missed) <= 8 else (' 等 %d 个' % len(missed))
                    return _fail_submit(
                        '路线没有经过全部打卡点: %s%s' % (names, more))

        # ---- 2. forge ----------------------------------------------------
        # Smooth the control points so the submitted track has curves, not
        # straight chords.  The curve passes through every control point, so
        # checkpoint hits are preserved.
        try:
            if len(wps) >= 3:
                wps = RT.smooth(wps, samples=8, closed=bool(closed))
        except Exception:  # noqa: BLE001
            pass
        # The operator may backfill today .. 3 days ago.  Resolve the slot
        # before forging so every timestamp in the record agrees.  The school's
        # validTime bands decide the allowed starts; merge the policy fetched
        # above so a stale school_rules.json cannot reject a valid 18:00 run.
        from ..domain import safety as S
        from ..domain.rundata import (estimate_pause_seconds,
                                      planned_active_seconds)
        from ..application import qualification as Q
        from ..application import policy as POL
        rules = Q.load_rules()
        rules, rules_refreshed = POL.merge_rules(rules, policy_data)
        if rules_refreshed:
            submit_notes.append(
                '已按服务器最新规则刷新学校要求（手动保存的规则不会被自动覆盖）')
            try:
                Q.save_rules(rules)
            except Exception:  # noqa: BLE001
                pass
        duration_s = planned_active_seconds(km, pace, km_times)
        duration_s += estimate_pause_seconds(
            pause_profile, None, int(duration_s))
        try:
            start_ms = S.resolve_start_ms(
                days_ago, start_time, duration_s, date=date,
                windows=rules.get('windows'))
        except ValueError as e:
            return _fail_submit(f'开始时间无效: {e}')
        day_key = S.to_beijing(start_ms).strftime('%Y-%m-%d')
        start_days_ago = (S.to_beijing(S.now_ms()).date()
                          - S.to_beijing(start_ms).date()).days

        # Keep the night guard, but do not turn the school's daily cap into a
        # hard block here.  The cap is a *reminder* in the model; the operator
        # may still have a legitimate reason to submit (and the server decides).
        from ..application import limits as L
        start_hhmm = S.to_beijing(start_ms).strftime('%H:%M')
        allow_night = bool(rules.get('windows')) and S.in_windows(
            start_hhmm, rules['windows'])
        refusal = L.check(acct.uid, start_ms, max_per_day=0,
                          block_night=not allow_night)
        if refusal:
            return _fail_submit(refusal, data={'start': {
                'ms': start_ms, 'text': S.fmt_beijing(start_ms),
                'days_ago': start_days_ago}})

        try:
            run_seed = None
            if seed is not None:
                try:
                    run_seed = int(seed)
                except (TypeError, ValueError):
                    run_seed = None
            run = generate_run_data(float(km), float(pace), int(cadence),
                                    center_lat=acct.center_lat,
                                    center_lng=acct.center_lng,
                                    rng=(random.Random(run_seed)
                                         if run_seed is not None else None),
                                    waypoints=wps or None,
                                    checkpoints=cps or None,
                                    altitude_base=float(altitude),
                                    altitude_mode=altitude_mode or 'auto',
                                    altitude_range=altitude_range or None,
                                    altitude_profile=altitude_profile or None,
                                    start_ms=start_ms,
                                    closed=bool(closed),
                                    km_times=km_times,
                                    run_area_id=area['id'] if area else None,
                                    geo_fences=fence.get('fences') or None,
                                    sequential=sequential,
                                    pause_profile=pause_profile or 'none',
                                    gps_profile=gps_profile or 'urban',
                                    terrain_coupling=bool(terrain_coupling),
                                    run_uuid=new_run_uuid(
                                        getattr(acct, 'platform', None)))
        except Exception as e:  # noqa: BLE001
            return _fail_submit(f'路线生成失败: {e}')
        self._last_run = run

        # Same pre-submit gate as the CLI: geometry vs claimed distance,
        # BD/GCJ pairs, isPass, bucket sums, lap coverage.
        from ..domain.rundata import validate_run_data
        issues = validate_run_data(run)
        if issues:
            return _fail_submit('提交前自检未通过: %s' % issues)

        # A wrong centre is the one mistake that still yields an *accepted*
        # record.  When the school has a fence, reject the track when it starts
        # outside it, or when most of it is outside.  A school without a fence
        # has nothing to validate against, and saying so is better than
        # refusing a valid run.
        polygons = fence.get('polygons') or (
            [fence['polygon']] if fence.get('polygon') else [])
        if polygons:
            try:
                import json as _json
                from ..domain import routes as RT
                wrap = run.get('locJsonEntity') or {}
                raw = wrap.get('allLocJson') if isinstance(wrap, dict) else None
                if isinstance(raw, str):
                    raw = _json.loads(raw)
                gcj = [(float(p['gLat']), float(p['gLng']))
                       for p in (raw or [])
                       if isinstance(p, dict) and 'gLat' in p and 'gLng' in p]
                if not gcj:
                    return _fail_submit('提交前围栏校验失败：轨迹缺少 GCJ 坐标，已停止提交')
                if not RT.point_in_fences(gcj[0][0], gcj[0][1], polygons):
                    return _fail_submit(
                        '生成路线起点不在任一校区围栏内（坐标未初始化？），'
                        '已停止提交')
                bad = RT.outside_fences(gcj, polygons)
                if bad['count']:
                    worst = max(p['distance_m'] for p in bad['points'])
                    if bad['count'] > len(gcj) * 0.25 or worst > 150:
                        return _fail_submit(
                            '生成路线有 %d/%d 个点不在任何围栏内（最远 %.0fm），'
                            '已停止提交'
                            % (bad['count'], bad['total'], worst))
            except Exception as e:  # noqa: BLE001
                return _fail_submit('提交前围栏校验失败，已停止提交: %s' % e)

        # User-entered school rules -> qualification model.  The rules were
        # already merged with the fresh runModePolicy before start resolution;
        # reuse that copy so the gate and the timestamp agree.
        from ..application import qualification as Q
        from ..application import policy as POL
        from ..application import readonly as RO
        # The school's daily cap applies to the *run date*.  Prefer the
        # server's record list (it also sees other devices/app runs); the local
        # receipt counter is a lower bound but is not proof that the server
        # list is readable.  If the list fails, keep `unknown` and let the
        # model warn instead of claiming a false 0.
        day_runs, day_source = RO.daily_count(rc, acct, day_key)
        try:
            local_runs = int(L.load(acct.uid, start_ms).get('runs', 0))
        except Exception:  # noqa: BLE001
            local_runs = 0
        if day_runs is None:
            day_source = 'unknown'
        else:
            day_runs = max(day_runs, local_runs)
        model = Q.evaluate(run, rules, checkpoints=cps,
                           daily_runs=day_runs, daily_source=day_source)
        rules_warning = ''
        if not model.get('configured'):
            # No usable band.  Do not hard-block: a school that publishes no
            # rule model has nothing for the server to score `useMobilityTools`
            # against either, so refusing here would only break a valid flow.
            # Say it out loud instead of passing silently.
            rules_warning = ('学校要求没有读到，本次提交没有校验配速/步频区间；'
                             '服务端若按 runModePolicy 复核，可能判为不合格。')
            model.setdefault('warnings', []).insert(0, rules_warning)
        if model.get('configured') and model.get('verdict') == 'fail':
            bad = [c['label'] + ' ' + c['value'] +
                   ('（' + c.get('detail') + '）' if c.get('detail') else '')
                   for c in model['checks'] if c['status'] == 'fail']
            return _fail_submit(
                '合格模型未通过: %s' % '；'.join(bad),
                data={'model': model,
                      'param_warnings': plausibility_warnings(pace, cadence)})

        from ..application import policy as POL
        from ..application import riskcheck as RK
        # Official `OutdoorRunViewModel.saveStartPointToDb` reverse-geocodes
        # the run's start point and stores the city in `SportRecord.address`.
        # Use the generated first point, not the campus name; if AMap is not
        # configured fall back to the city in the campus name, then to "".
        first = (run.get('locPoints') or [{}])[0]
        city = RK.address_for_start(
            first.get('gLat'), first.get('gLng'),
            acct.campusName, acct.schoolName,
            key=self._amap_key()) or ''
        ctx = {
            'uid': acct.uid,
            'selectedUnid': acct.unid,
            'sportType': 1,
            'geeToken': geeToken or '',
            'address': city,
            # upstream: policy = the server's rule set, not a hardcoded value
            'policy': POL.policy_value(policy_data, 1),
        }
        min_d = POL.rule_min_distance(policy_data)
        if min_d > 0:
            ctx['selDistance'] = min_d
        ent = sign_upload_entity(build_upload_entity(run, ctx))

        # ---- 3. write-ahead receipt --------------------------------------
        store = ReceiptStore(os.path.join(self.store_dir,
                                          'submission_receipts'))
        try:
            store.reserve(acct.uid, ent['uuid'], ent['startTime'], ent['stopTime'],
                          school=str(acct.unid), distance_m=run['juli'])
        except (AlreadySubmitted, UnknownPending) as e:
            return _fail_submit(f'提交被回执拦下: {e}')
        except GuardError as e:
            return _fail_submit(f'回执写入失败，已停止提交: {e}')

        # ---- 4. submit ----------------------------------------------------
        try:
            res = rc.submit_run(acct, run, ent, ent, policy_ts=policy_ts)
        except Exception as e:  # noqa: BLE001
            store.mark(acct.uid, ent['uuid'], 'unknown',
                       school=str(acct.unid), note=str(e)[:120])
            return _fail_submit(
                f'提交异常，结果未知（已锁定该时段）: {e}',
                kind='unknown')

        res['km'] = run['km']
        res['sportTime'] = run['sportTime']
        res['juli'] = run['juli']
        # `save/record` does not always echo the uuid back; the signed entity
        # is the authoritative copy.  Without this fallback OBS upload and the
        # read-back both received None and the record was left without a trail.
        run_uuid = res.get('uuid') or ent.get('uuid') or run.get('sportunid') or ''
        res['uuid'] = run_uuid
        res['policy'] = ent.get('policy')
        res['model'] = model
        res['freeze_seconds'] = freeze
        res['face_required'] = face_on
        res['sequential'] = sequential
        res['clock'] = clock_info
        if not (isinstance(clock_info, dict) and clock_info.get('applied')):
            res.setdefault('warnings', [])
            if isinstance(res['warnings'], list):
                res['warnings'].append(
                    '未能校准服务器时间，本次按本机时钟生成；'
                    '若提交被软拒绝，重试一次即可（不会修改系统设置）。')
        if freeze > 0:
            res.setdefault('warnings', [])
            if isinstance(res['warnings'], list):
                res['warnings'].append(
                    '账号处于跑步冻结期（剩余 %s）' % _fmt_seconds(freeze))
        res['start'] = {'ms': start_ms, 'text': S.fmt_beijing(start_ms),
                        'days_ago': start_days_ago}
        res['daily'] = {'date': day_key, 'runs': day_runs,
                        'source': day_source}
        res['checkpoints'] = len(cps)
        res['waypoints'] = len(wps)
        res['missed_points'] = missed

        if res.get('error') != 10000 or not res.get('rrid'):
            # Distinguish "explicitly refused" (retry allowed) from "we do not
            # know" (must stay locked) — the old code labelled both as 拒绝.
            if res.get('error') in (None, 0):
                state = 'unknown'
                head = '提交结果未知（已锁定该时段，请勿重试）'
            elif res.get('error') == 10000:
                state = 'unknown'
                head = '服务器已受理但未返回记录号，结果未知（已锁定该时段）'
            else:
                state = 'rejected'
                head = '提交被拒'
            store.mark(acct.uid, ent['uuid'], state,
                       school=str(acct.unid), note=str(res.get('message'))[:120])
            return _fail_submit(
                '%s: error=%s %s (HTTP %s)'
                % (head, res.get('error'), res.get('message'), res.get('status')),
                kind=('unknown' if state == 'unknown' else 'fail'),
                data=res)

        try:
            store.mark(acct.uid, ent['uuid'], 'accepted',
                       rrid=res['rrid'], school=str(acct.unid))
        except Exception as e:  # noqa: BLE001
            # The record exists; do not turn a receipt-file failure into a
            # failed submission.  Surface it so the operator can reconcile.
            res['receipt_error'] = str(e)
        try:
            L.record(acct.uid, start_ms)
        except Exception:  # noqa: BLE001
            pass

        # ---- 4b. OBS trail upload ----------------------------------------
        # The replay/detail page reads the trail from OBS, not from the record
        # column. Without this the run exists but shows no track and no
        # checkpoints (upstream issue #85).
        try:
            from ..transport import obs as OBS
            from ..application import preferences as PREF
            time.sleep(1.0)          # reference: flow.rs sleeps 1 s here
            fpj = json.dumps(run['fixed_point_json'], separators=(',', ':'))
            obs_res = OBS.upload_obs(
                rc, acct, run, res['rrid'], res['uuid'], fpj,
                log=lambda *_: None,
                key_mode=PREF.get(self.store_dir, 'obs_key_mode', 'single'))
            res['obs'] = obs_res
            time.sleep(2.0)          # reference: flow.rs sleeps 2 s before verify
        except Exception as e:  # noqa: BLE001
            res['obs_error'] = str(e)

        # ---- 5. read back -------------------------------------------------
        try:
            back = rc.request(acct, 'api/v70320/runnings/get_one_record', {
                'uuid': res['uuid'], 'rrid': res['rrid'],
                'calculateBadge': False})
            if back.get('error'):
                res['readback_error'] = '解密失败: %s' % back.get('error')
            else:
                obj = as_dict(back.get('json'))
                if obj.get('error') not in (None, 10000):
                    res['readback_error'] = ('error=%s %s'
                                             % (obj.get('error'),
                                                obj.get('message') or ''))
                else:
                    d = as_dict(obj.get('data'))
                    if not d:
                        res['readback_error'] = '回读响应为空'
                    else:
                        res['readback'] = {
                            'isCheat': d.get('isCheat'),
                            'complete': d.get('complete'),
                            'policy': d.get('policy'),
                            'totalDis': d.get('totalDis'),
                            'totalTime': d.get('totalTime'),
                            'speed': d.get('speed'),
                            'calorie': d.get('calorie'),
                            'statusInfo': d.get('statusInfo'),
                            'reasonList': d.get('reasonList'),
                        }
        except Exception as e:  # noqa: BLE001
            res['readback_error'] = str(e)
        res['param_warnings'] = (plausibility_warnings(pace, cadence)
                                 + ([rules_warning] if rules_warning else [])
                                 + submit_notes)
        return _ok(res)
