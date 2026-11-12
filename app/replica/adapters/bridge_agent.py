"""Route-memory and Agent bridge mixin.

Kept separate from the main adapter and the run-flow mixin: these methods own
the learned-route progress/thread state and the Agent tool wiring.  Moving them
does not change any bridge signature and does not touch the native boundary.
"""

from __future__ import annotations

import math
import os
import threading
import time

from ..application import device_profile as DP
from .bridge_common import _clean_route, _fail, _ok, _read_json, _write_json


class AgentBridgeMixin:
    @staticmethod
    def _rm_default_progress() -> dict:
        return {
            'running': False,
            'phase': 'idle',
            'done': 0,
            'total': 0,
            'error': '',
            'owner': '',
            'started_at_ms': 0,
            'finished_at_ms': 0,
        }

    def _rm_ensure_state(self):
        lock = getattr(self, '_rm_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._rm_lock = lock
        if not isinstance(getattr(self, '_rm_progress', None), dict):
            self._rm_progress = self._rm_default_progress()
        return lock

    def _rm_get_progress(self) -> dict:
        lock = self._rm_ensure_state()
        with lock:
            return dict(self._rm_progress)

    def _rm_progress_for(self, owner: str) -> dict:
        prog = self._rm_get_progress()
        if prog.get('owner') and prog.get('owner') != owner:
            return self._rm_default_progress()
        return prog

    def _rm_exec_lock(self):
        lock = getattr(self, '_rm_exec', None)
        if lock is None:
            lock = threading.Lock()
            self._rm_exec = lock
        return lock

    def _rm_suggest_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_suggest_cache is None:
                from ..application import agent_cache
                self._rm_suggest_cache = agent_cache.TTLCache(
                    ttl_s=90.0, max_entries=16)
            return self._rm_suggest_cache

    def _rm_apply_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_apply_cache is None:
                from ..application import agent_cache
                self._rm_apply_cache = agent_cache.TTLCache(
                    ttl_s=300.0, max_entries=24)
            return self._rm_apply_cache

    def _rm_fence_cache_obj(self):
        with self._rm_cache_lock:
            if self._rm_fence_cache is None:
                from ..application import agent_cache
                self._rm_fence_cache = agent_cache.TTLCache(
                    ttl_s=60.0, max_entries=4)
            return self._rm_fence_cache

    def _agent_choice_cache(self):
        with self._rm_cache_lock:
            if self._agent_choice_cache_obj is None:
                from ..application import agent_cache
                self._agent_choice_cache_obj = agent_cache.ChoiceCache(
                    self.store_dir)
            return self._agent_choice_cache_obj

    def _rm_cache_clear(self):
        with self._rm_cache_lock:
            if self._rm_suggest_cache is not None:
                self._rm_suggest_cache.clear()
            if self._rm_apply_cache is not None:
                self._rm_apply_cache.clear()

    def _rm_run_learn_guarded(self, acct, limit_n: int, campus: dict) -> dict:
        # Serialize every learn (sync or background) so two callers never
        # rewrite progress.json / the memory doc at the same time.
        with self._rm_exec_lock():
            return self._rm_run_learn(acct, limit_n, campus)

    def _rm_error_text(self, prefix: str, err, acct=None) -> str:
        from ..domain import safety as S

        text = S.redact_text(str(err or '')).strip()
        token = str(getattr(acct, 'token', '') or '')
        if token:
            text = text.replace(token, '***')
        return (prefix + '：' + text) if text else prefix

    def _rm_set_progress(self, mirror: bool = True, **fields) -> dict:
        lock = self._rm_ensure_state()
        with lock:
            self._rm_progress.update(fields)
            snap = dict(self._rm_progress)
            if mirror:
                try:
                    _write_json(
                        os.path.join(self.store_dir, 'route_memory', 'progress.json'),
                        snap,
                    )
                except Exception:  # noqa: BLE001 - best-effort UI progress mirror
                    pass
        return snap

    def _rm_require_account(self):
        try:
            acct = self._real_account()
        except DP.DeviceProfileError as e:
            return None, _fail(str(e))
        if acct is None or not getattr(acct, 'token', ''):
            return None, _fail('未配置 token（先在"账号"面板保存）')
        return acct, None

    @staticmethod
    def _rm_normalise_points(points) -> list[dict]:
        out: list[dict] = []
        for p in points or []:
            if not isinstance(p, dict):
                continue
            lat = next((p[k] for k in ('lat', 'glat', 'gLat', 'latitude')
                        if p.get(k) is not None), None)
            lon = next((p[k] for k in ('lon', 'lng', 'glon', 'gLng', 'longitude')
                        if p.get(k) is not None), None)
            if lat is None or lon is None:
                continue
            try:
                flat, flon = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(flat) and math.isfinite(flon)):
                continue
            if not (-90.0 <= flat <= 90.0 and -180.0 <= flon <= 180.0):
                continue
            if abs(flat) < 1e-9 or abs(flon) < 1e-9:
                continue
            out.append({
                'lat': flat,
                'lon': flon,
                'isMustPoint': bool(
                    p.get('isMustPoint') if 'isMustPoint' in p else p.get('must')),
                'name': str(p.get('name') or p.get('n') or p.get('pointName') or ''),
            })
        return out

    @staticmethod
    def _rm_normalise_fence(fence) -> list[list[tuple[float, float]]] | None:
        if not fence:
            return None
        if isinstance(fence, dict):
            for key in ('polygons', 'rings', 'points'):
                if isinstance(fence.get(key), (list, tuple)):
                    fence = fence[key]
                    break
            else:
                if isinstance(fence.get('polygon'), (list, tuple)):
                    fence = [fence['polygon']]
                else:
                    return None
        if not isinstance(fence, (list, tuple)) or not fence:
            return None
        first = fence[0]
        is_point = (
            (isinstance(first, dict)
             and any(k in first for k in ('lat', 'glat', 'gLat', 'latitude')))
            or (isinstance(first, (list, tuple)) and len(first) >= 2
                and not isinstance(first[0], (list, tuple, dict)))
        )
        raw_rings = [fence] if is_point else fence
        rings: list[list[tuple[float, float]]] = []
        for raw_ring in raw_rings:
            if isinstance(raw_ring, dict) and isinstance(raw_ring.get('points'), (list, tuple)):
                raw_ring = raw_ring['points']
            if not isinstance(raw_ring, (list, tuple)):
                continue
            pts: list[tuple[float, float]] = []
            for item in raw_ring:
                if isinstance(item, dict):
                    lat = next((item[k] for k in ('lat', 'glat', 'gLat', 'latitude')
                                if item.get(k) is not None), None)
                    lng = next((item[k] for k in ('lon', 'lng', 'glon', 'gLng', 'longitude')
                                if item.get(k) is not None), None)
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    lat, lng = item[0], item[1]
                else:
                    continue
                try:
                    flat, flng = float(lat), float(lng)
                except (TypeError, ValueError):
                    continue
                if (math.isfinite(flat) and math.isfinite(flng)
                        and -90.0 <= flat <= 90.0 and -180.0 <= flng <= 180.0
                        and (abs(flat) > 1e-9 or abs(flng) > 1e-9)):
                    pts.append((flat, flng))
            if len(pts) >= 3:
                rings.append(pts)
        return rings or None

    def _rm_tool_receipt_ids(self, acct) -> tuple[set[str], set[str]]:
        uuids: set[str] = set()
        rrids: set[str] = set()
        uid_s = str(getattr(acct, 'uid', '') or '')
        unid_raw = getattr(acct, 'unid', None)
        unid_s = str(unid_raw) if unid_raw not in (None, '') else 'default'
        raw = f'{uid_s}__{unid_s}'
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in raw)
        path = os.path.join(self.store_dir, 'submission_receipts', safe + '.json')
        data = _read_json(path, {})
        if isinstance(data, dict):
            for k, v in data.items():
                if k:
                    uuids.add(str(k))
                if isinstance(v, dict):
                    if v.get('uuid'):
                        uuids.add(str(v['uuid']))
                    if v.get('rrid') not in (None, ''):
                        rrids.add(str(v['rrid']))
        return uuids, rrids

    def _rm_campus(self, acct) -> dict:
        campus: dict = {'center': [], 'fence_hash': ''}
        d = self._read_session()
        if self._center_ok(d.get('lat'), d.get('lon')):
            campus['center'] = [round(float(d['lat']), 6), round(float(d['lon']), 6)]
        elif self._center_ok(getattr(acct, 'center_lat', 0), getattr(acct, 'center_lng', 0)):
            campus['center'] = [
                round(float(acct.center_lat), 6),
                round(float(acct.center_lng), 6),
            ]
        return campus

    def _rm_run_learn(self, acct, limit_n: int, campus: dict) -> dict:
        from ..application import readonly as RO
        from ..application import route_memory as RM
        from ..domain import safety as S
        from ..transport import realclient as R

        try:
            rc = R.RealClient()
            listing = RO.recent_records(rc, acct, limit=limit_n)
            if not isinstance(listing, dict) or not listing.get('ok'):
                raw = (listing.get('error') or listing.get('message')
                       if isinstance(listing, dict) else '')
                err_msg = self._rm_error_text('记录列表查询失败', raw, acct)
                self._rm_set_progress(
                    running=False, phase='error',
                    error=err_msg,
                    finished_at_ms=int(time.time() * 1000),
                )
                return _fail(err_msg)

            rows = list(listing.get('records') or [])
            total = len(rows)
            self._rm_set_progress(phase='detail', done=0, total=total)

            receipt_uuids, receipt_rrids = self._rm_tool_receipt_ids(acct)
            tracks: list[dict] = []
            skipped: list[dict] = []

            for idx, row in enumerate(rows):
                if idx > 0:
                    time.sleep(0.15)
                row_rrid = row.get('rrid') if isinstance(row, dict) else None
                row_uuid = (row.get('uuid') or '') if isinstance(row, dict) else ''
                try:
                    detail = RO.record_detail(
                        rc, acct, row_rrid,
                        uuid=str(row_uuid) if row_uuid else None)
                except Exception as e:  # noqa: BLE001
                    if len(skipped) < 50:
                        skipped.append({
                            'rrid': str(row_rrid or ''),
                            'reason': S.redact_text(str(e))[:160],
                        })
                    self._rm_set_progress(done=idx + 1)
                    continue

                if not isinstance(detail, dict) or not detail.get('ok'):
                    reason = (detail.get('error')
                              if isinstance(detail, dict) else '') or '详情读取失败'
                    if len(skipped) < 50:
                        skipped.append({
                            'rrid': str(row_rrid or ''),
                            'reason': S.redact_text(str(reason))[:160],
                        })
                    self._rm_set_progress(done=idx + 1)
                    continue

                d = detail.get('detail') or {}
                detail_rrid = d.get('rrid') if d.get('rrid') not in (None, '') else row_rrid
                detail_uuid = d.get('uuid') or row_uuid or ''
                is_tool = (
                    (bool(detail_uuid) and str(detail_uuid) in receipt_uuids)
                    or (bool(row_uuid) and str(row_uuid) in receipt_uuids)
                    or (detail_rrid not in (None, '') and str(detail_rrid) in receipt_rrids)
                    or (row_rrid not in (None, '') and str(row_rrid) in receipt_rrids)
                )
                dist_m = d.get('distance_m')
                if dist_m is None and isinstance(row, dict):
                    dist_m = row.get('distance_m', 0.0)
                dur_s = d.get('duration_s')
                if dur_s is None and isinstance(row, dict):
                    dur_s = row.get('duration_s', 0)
                start_ms = d.get('start_ms')
                if start_ms is None and isinstance(row, dict):
                    start_ms = row.get('start_ms', 0)
                complete = d.get('complete')
                if complete is None and isinstance(row, dict):
                    complete = row.get('complete', False)

                tracks.append({
                    'rrid': str(detail_rrid or ''),
                    'line': RM.track_from_detail(detail),
                    'distance_m': dist_m if dist_m is not None else 0.0,
                    'duration_s': dur_s if dur_s is not None else 0,
                    'start_ms': start_ms if start_ms is not None else 0,
                    'is_cheat': d.get('is_cheat', 0),
                    'complete': bool(complete),
                    'source': 'tool' if is_tool else 'server',
                })
                self._rm_set_progress(done=idx + 1)

            self._rm_set_progress(phase='learning')
            owner = f'{acct.uid}:{acct.unid}'
            doc = RM.learn_from_tracks(tracks, owner, campus=campus)
            doc['records_seen'] = len(rows)
            doc['records_skipped'] = max(0, len(rows) - int(doc.get('records_readable') or 0))

            self._rm_set_progress(phase='saving')
            RM.save(self.store_dir, acct.uid, acct.unid, doc)
            # The memory revision changed, so every cached suggestion/apply
            # result is stale even if the file signature happens to collide.
            self._rm_cache_clear()
            summary = RM.summarize(doc)
            progress = self._rm_set_progress(
                running=False,
                phase='done',
                done=total,
                total=total,
                error='',
                finished_at_ms=int(time.time() * 1000),
            )
            templates = [dict(t) for t in (summary.get('templates_detail') or [])]
            return _ok({
                'summary': summary,
                'templates': templates,
                'skipped': skipped,
                'cached': False,
                'progress': progress,
            })
        except Exception as e:  # noqa: BLE001
            err_msg = self._rm_error_text('路线学习失败', e, acct)
            self._rm_set_progress(
                running=False,
                phase='error',
                error=err_msg,
                finished_at_ms=int(time.time() * 1000),
            )
            return _fail(err_msg)

    def real_route_memory_status(self):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        summary = RM.summarize(doc)
        templates = [dict(t) for t in (summary.get('templates_detail') or [])]
        return _ok({
            'summary': summary,
            'templates': templates,
            'progress': self._rm_progress_for(f'{acct.uid}:{acct.unid}'),
            'cache': {
                'suggest': self._rm_suggest_cache_obj().stats(),
                'apply': self._rm_apply_cache_obj().stats(),
                'fence': self._rm_fence_cache_obj().stats(),
            },
        })

    def real_route_memory_learn(self, limit=50, force=False, wait=True):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err

        try:
            limit_n = int(limit)
        except (TypeError, ValueError):
            limit_n = 50
        limit_n = max(1, min(100, limit_n))

        lock = self._rm_ensure_state()
        existing = None
        with lock:
            if self._rm_progress.get('running'):
                if not wait:
                    return _ok({'started': False, 'progress': dict(self._rm_progress)})
                existing = getattr(self, '_rm_thread', None)
        if existing is not None and existing.is_alive():
            existing.join(timeout=120.0)
            if existing.is_alive():
                return _fail('路线学习仍在进行，请稍后再看状态')

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        now_ms = int(time.time() * 1000)
        owner = f'{acct.uid}:{acct.unid}'
        if (not force and doc.get('templates') and doc.get('updated_at_ms')
                and (now_ms - int(doc['updated_at_ms'])) < 24 * 3600 * 1000):
            summary = RM.summarize(doc)
            templates = [dict(t) for t in (summary.get('templates_detail') or [])]
            payload = {
                'summary': summary,
                'templates': templates,
                'skipped': [],
                'cached': True,
                'progress': self._rm_progress_for(owner),
            }
            if not wait:
                payload['started'] = False
            return _ok(payload)

        campus = self._rm_campus(acct)
        with lock:
            if self._rm_progress.get('running'):
                if not wait:
                    return _ok({'started': False,
                                'progress': self._rm_progress_for(owner)})
            self._rm_set_progress(
                running=True,
                phase='listing',
                done=0,
                total=0,
                error='',
                owner=owner,
                started_at_ms=now_ms,
                finished_at_ms=0,
            )
            if not wait:
                t = threading.Thread(
                    target=self._rm_run_learn_guarded,
                    args=(acct, limit_n, campus),
                    daemon=True,
                )
                self._rm_thread = t
                t.start()
                return _ok({'started': True,
                            'progress': self._rm_progress_for(owner)})

        return self._rm_run_learn_guarded(acct, limit_n, campus)

    def real_route_memory_suggest(self, km=2.5, points=None, fence=None,
                                  run_area_id=None):
        from ..application import agent_cache
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        owner = f'{acct.uid}:{acct.unid}'

        try:
            km_val = float(km)
        except (TypeError, ValueError):
            km_val = 2.5
        if not math.isfinite(km_val):
            km_val = 2.5
        km_val = max(0.5, min(20.0, km_val))

        cps = self._rm_normalise_points(points)
        fence_norm = self._rm_normalise_fence(fence) if fence is not None else None
        fence_center = None
        if fence is None:
            # The caller usually passes LIVE.fences.  When it does not, try the
            # same fence endpoint the map uses; a failure simply means "no
            # fence filter", never an error.  A short-lived fence cache keeps
            # repeated suggest calls from re-hitting the network.
            fence_cache = self._rm_fence_cache_obj()
            fence_key = f'{owner}|{run_area_id}'
            cached_fence = fence_cache.get(fence_key)
            if isinstance(cached_fence, dict):
                fence_norm = cached_fence.get('fence')
                cached_center = cached_fence.get('center')
                if (isinstance(cached_center, (list, tuple))
                        and len(cached_center) == 2):
                    fence_center = (float(cached_center[0]),
                                    float(cached_center[1]))
            else:
                got = None
                try:
                    got = self.real_fetch_fence()
                except Exception:  # noqa: BLE001 - offline or policy unavailable
                    got = None
                data = (got.get('data') if isinstance(got, dict)
                        and got.get('code') == 200 else None)
                if isinstance(data, dict):
                    fence_norm = self._rm_normalise_fence(
                        data.get('polygons') if data.get('polygons') is not None
                        else data.get('polygon'))
                    c = data.get('center')
                    if isinstance(c, dict) and self._center_ok(c.get('lat'), c.get('lon')):
                        fence_center = (float(c['lat']), float(c['lon']))
                    if fence_norm is not None or fence_center is not None:
                        fence_cache.put(fence_key, {
                            'fence': fence_norm,
                            'center': list(fence_center) if fence_center else None,
                        })

        center = None
        if cps:
            center = (cps[0]['lat'], cps[0]['lon'])
        elif fence_center is not None:
            center = fence_center
        else:
            d = self._read_session()
            if self._center_ok(d.get('lat'), d.get('lon')):
                center = (float(d['lat']), float(d['lon']))
            elif self._center_ok(getattr(acct, 'center_lat', 0),
                                 getattr(acct, 'center_lng', 0)):
                center = (float(acct.center_lat), float(acct.center_lng))

        # The geometry is deterministic for a given memory revision and input
        # set.  The cache key therefore includes the memory file signature so
        # learning a new route invalidates every old answer.
        cache_key = agent_cache.stable_hash({
            'owner': owner,
            'doc': agent_cache.file_signature(
                RM.memory_path(self.store_dir, acct.uid, acct.unid)),
            'km': round(km_val, 3),
            'points': cps,
            'fence': fence_norm,
            'center': center,
        })
        cache = self._rm_suggest_cache_obj()
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            data = dict(cached)
            data['progress'] = self._rm_progress_for(owner)
            data['cached'] = True
            return _ok(data, message=data.get('message') or 'success')

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        summary = RM.summarize(doc)
        status = summary.get('status') or 'empty'
        candidates = RM.suggest(doc, km_val, cps, fence=fence_norm, center=center)

        msg = ''
        if status == 'empty':
            msg = '还没有学习路线，请先从历史跑步记录学习'
        elif status == 'weak':
            usable = summary.get('usable', 0)
            details = summary.get('templates_detail') or []
            max_samples = max((t.get('samples', 0) for t in details), default=0)
            worst_dist = max((t.get('medoid_distance_m', 0.0) for t in details), default=0.0)
            if usable < 5:
                msg = f'可用跑步记录不足 5 条（当前 {usable} 条），暂不提供学习路线'
            elif max_samples < 3:
                msg = '单条路线重复次数不足 3 次，暂不提供学习路线'
            elif worst_dist > 60.0:
                msg = '历史路线聚合偏差过大，暂不提供学习路线'
            else:
                msg = '记录还不够，暂不提供学习路线'
        elif not candidates:
            msg = '当前围栏或必经点位条件下没有匹配的学习路线'

        data = {
            'candidates': candidates,
            'templates': summary.get('templates', 0),
            'target_m': round(km_val * 1000.0, 1),
            'status': status,
            'progress': self._rm_progress_for(owner),
            'cached': False,
        }
        if msg:
            data['message'] = msg
        if status == 'ok' and candidates:
            cache.put(cache_key, {
                key: value for key, value in data.items() if key != 'progress'
            })
        return _ok(data, message=msg or 'success')

    def real_route_memory_apply(self, template_id, km=2.5, points=None):
        from ..application import agent_cache
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err

        try:
            km_val = float(km)
        except (TypeError, ValueError):
            km_val = 2.5
        if not math.isfinite(km_val):
            km_val = 2.5
        km_val = max(0.5, min(20.0, km_val))

        cps = self._rm_normalise_points(points)
        owner = f'{acct.uid}:{acct.unid}'
        cache_key = agent_cache.stable_hash({
            'owner': owner,
            'doc': agent_cache.file_signature(
                RM.memory_path(self.store_dir, acct.uid, acct.unid)),
            'template_id': str(template_id or ''),
            'km': round(km_val, 3),
            'points': cps,
        })
        cache = self._rm_apply_cache_obj()
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            out = dict(cached)
            out['cached'] = True
            return _ok(out)

        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        res = RM.apply_template(doc, str(template_id or ''), km_val, points=cps)
        if not res.get('ok'):
            return _fail(res.get('error') or '模板应用失败')
        out = dict(res)
        out['cached'] = False
        cache.put(cache_key, {k: v for k, v in out.items() if k != 'cached'})
        return _ok(out)

    def real_route_memory_clear(self):
        from ..application import route_memory as RM

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        RM.clear(self.store_dir, acct.uid, acct.unid)
        self._rm_cache_clear()
        self._rm_set_progress(**self._rm_default_progress())
        return _ok({'cleared': True})

    def real_route_memory_save(self, template_id):
        from ..application import route_memory as RM
        from ..domain import safety as S

        acct, err = self._rm_require_account()
        if err is not None:
            return err
        doc = RM.load(self.store_dir, acct.uid, acct.unid)
        tpl = next(
            (t for t in (doc.get('templates') or [])
             if isinstance(t, dict) and t.get('id') == template_id),
            None,
        )
        if tpl is None:
            return _fail('模板不存在')

        today_bj = S.fmt_beijing(S.now_ms(), '%Y-%m-%d')
        candidate = _clean_route({
            'name': tpl.get('name') or '学习路线',
            'type': 'learned',
            'line': tpl.get('line'),
            'closed': bool(tpl.get('closed')),
            'saved_at': today_bj,
        })
        if candidate is None:
            return _fail('模板不存在')

        raw = _read_json(self._routes_path(), {})
        rows = raw.get('routes') if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            rows = []

        out: list[dict] = []
        seen_keys: set[tuple] = set()
        for r in rows:
            c = _clean_route(r)
            if c is None:
                continue
            p0 = c['line'][0]
            key = (c['name'], round(p0[0], 6), round(p0[1], 6))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(c)

        np0 = candidate['line'][0]
        new_key = (candidate['name'], round(np0[0], 6), round(np0[1], 6))
        if new_key in seen_keys:
            out = [
                candidate
                if (r['name'], round(r['line'][0][0], 6), round(r['line'][0][1], 6)) == new_key
                else r
                for r in out
            ]
        else:
            out.append(candidate)
        out = out[-300:]

        try:
            _write_json(self._routes_path(), {'version': 1, 'routes': out})
        except Exception as e:  # noqa: BLE001
            return _fail('路线保存失败: %s' % e)
        return _ok({'routes': out, 'count': len(out)})

    # -- route-memory agent ------------------------------------------------
    def _agent_owner(self) -> str:
        d = self._read_session()
        uid = str(d.get('uid') or '')
        unid = str(d.get('unid') or '')
        return f'{uid}:{unid}' if (uid or unid) else ''

    def _agent_build_tools(self) -> dict:
        def _llm_choose(candidates, target_m):
            try:
                from ..application import agent_cache, llm_client, llm_settings
            except Exception:  # noqa: BLE001
                return None
            settings = llm_settings.effective(self.store_dir)
            if settings is None:
                return None

            # Choosing a candidate is a classification task, not creative
            # writing.  Temperature 0 makes the same prompt reproducible and
            # lets the local cache return the same decision safely.
            settings = dict(settings)
            settings['temperature'] = 0.0
            model = str(settings.get('model') or '')
            messages = llm_settings.build_choice_prompt(candidates, target_m)
            cache_key = agent_cache.stable_hash({
                'protocol': settings.get('protocol'),
                'base_url': settings.get('base_url'),
                'model': model,
                'temperature': 0.0,
                'messages': messages,
            })

            cache = self._agent_choice_cache()
            hit = cache.get(cache_key)
            if isinstance(hit, dict):
                cached_id = str(hit.get('template_id') or '')
                if any(
                    isinstance(c, dict)
                    and str(c.get('template_id') or '') == cached_id
                    for c in (candidates or [])
                ):
                    return {
                        'template_id': cached_id,
                        'model': model,
                        'error': '',
                        'cached': True,
                        'prompt_sha256': cache_key,
                    }

            proxy = (os.environ.get('JHP_PROXY') or '').strip()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            res = llm_client.chat(settings, messages, proxies=proxies)
            if not isinstance(res, dict) or not res.get('ok'):
                err = (res.get('error') if isinstance(res, dict) else '') or 'LLM 调用失败'
                raise RuntimeError(err)
            picked = llm_settings.parse_choice(res.get('text') or '', candidates)
            if not picked:
                return {
                    'template_id': '',
                    'model': model,
                    'error': '模型返回无效候选序号',
                    'cached': False,
                    'prompt_sha256': cache_key,
                }
            cache.put(cache_key, picked, model)
            return {
                'template_id': picked,
                'model': model,
                'error': '',
                'cached': False,
                'prompt_sha256': cache_key,
            }

        return {
            'route_memory_status': self.real_route_memory_status,
            'route_memory_learn': self.real_route_memory_learn,
            'route_memory_suggest': self.real_route_memory_suggest,
            'route_memory_apply': self.real_route_memory_apply,
            'preview_route': self.real_preview_model,
            'audit_run': self.real_audit_run,
            'llm_choose': _llm_choose,
        }

    def _agent_get_or_create_session(self):
        from ..application.agent import AgentSession

        lock = getattr(self, '_agent_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._agent_lock = lock
        with lock:
            owner = self._agent_owner()
            session = getattr(self, '_agent_session', None)
            if session is None or getattr(session, 'owner', None) != owner:
                if session is not None:
                    try:
                        session.cancel()
                    except Exception:  # noqa: BLE001
                        pass
                session = AgentSession(self._agent_build_tools(), self.store_dir, owner)
                self._agent_session = session
            return session

    def real_agent_start(self, text='', km=None, points=None, fence=None,
                         run_area_id=None):
        session = self._agent_get_or_create_session()
        return _ok(session.start(
            text=text,
            km=km,
            points=points,
            fence=fence,
            run_area_id=run_area_id,
        ))

    def _agent_current_session(self):
        """Return the session only while it still belongs to the active account."""
        lock = getattr(self, '_agent_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._agent_lock = lock
        with lock:
            session = getattr(self, '_agent_session', None)
            if session is None:
                return None
            if getattr(session, 'owner', '') != self._agent_owner():
                try:
                    session.cancel()
                except Exception:  # noqa: BLE001
                    pass
                self._agent_session = None
                return None
            return session

    def real_agent_status(self):
        from ..application.agent import _idle_status

        session = self._agent_current_session()
        if session is None:
            return _ok(_idle_status())
        return _ok(session.status())

    def real_agent_message(self, text):
        session = self._agent_current_session()
        if session is None:
            return _fail('Agent 还没有开始')
        return _ok(session.message(text))

    def real_agent_confirm(self):
        session = self._agent_current_session()
        if session is None:
            return _fail('Agent 还没有开始')
        res = session.confirm()
        if not isinstance(res, dict) or res.get('ok') is False or 'preview_args' not in res:
            err = res.get('error') if isinstance(res, dict) else '当前状态不可确认'
            return _fail(err or '当前状态不可确认', data=res)
        return _ok(res)

    def real_agent_cancel(self):
        from ..application.agent import _idle_status

        session = self._agent_current_session()
        if session is None:
            return _ok(_idle_status())
        return _ok(session.cancel())
