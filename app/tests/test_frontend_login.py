"""Static guards for the login gate and frontend asset split.

The full interaction test is Playwright-based (mocked bridge), but these checks
make sure a future edit cannot silently remove the gate, re-introduce a
blocking external font request, or break the external CSS/JS links.
"""
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')
CSS = (FRONTEND / 'css' / 'app.css').read_text(encoding='utf-8')
SOURCE = HTML + '\n' + JS


def test_frontend_assets_are_external_and_present():
    assert '<link rel="stylesheet" href="css/app.css">' in HTML
    assert '<script src="js/app.js"></script>' in HTML
    assert '<style>' not in HTML
    assert (FRONTEND / 'css' / 'app.css').is_file()
    assert (FRONTEND / 'js' / 'app.js').is_file()


def test_login_gate_locks_app_until_session_is_ready():
    assert '<body class="login-locked">' in HTML
    assert 'id="loginGate"' in HTML
    assert 'id="loginForm"' in HTML
    assert 'id="loginLoader"' in HTML
    assert '.login-loader::before' in CSS
    assert 'login-loading-bounce' in CSS
    assert 'rgba(28,28,28,.35)' in CSS
    assert '.login-card.loading' in CSS
    assert 'function setLoginLoader' in JS
    assert "classList.toggle('loading'" in JS
    assert 'setLoginLoader(true)' in JS
    assert 'setLoginLoader(false)' in JS
    assert 'id="loginUser"' in HTML
    assert 'id="loginPass"' in HTML
    assert 'id="loginBtn"' in HTML
    assert 'id="appShell" aria-hidden="true" inert' in HTML


def test_login_gate_uses_the_real_bridge_and_auto_keepalive():
    assert "liveApi('real_login'" in SOURCE
    assert 'async function ensureKeepalive()' in SOURCE
    # “随登随退”: local logout does not wait for the server proof.
    assert "liveApi('real_logout', {local_only: true})" in SOURCE
    assert "showLoginGate(verified ? '已退出登录" in SOURCE
    assert '已退出本机登录，请重新登录。' in SOURCE


def test_login_requires_the_disclaimer_acknowledgement():
    assert 'id="loginAgree"' in HTML
    assert '我同意并认真阅读了' in HTML
    assert 'id="disclaimerOpen"' in HTML
    assert 'id="disclaimerModal"' in HTML
    assert '软件使用协议与免责声明' in HTML
    assert '本项目完全免费开源' in HTML
    assert '完全免费原则' in HTML
    assert '二次修改免责' in HTML
    assert 'function disclaimerAccepted()' in JS
    assert "liveApi('real_get_ui_state'" in JS
    assert 'disclaimer_version' in JS
    assert '使用前请阅读并勾选' in JS
    # The gate must survive a reload with an old token: force a local logout.
    assert 'enforceDisclaimerGate' in JS
    assert "liveApi('real_logout', {local_only: true}" in JS


def test_announcements_are_data_driven_and_use_the_bundled_qr():
    ann_js = (FRONTEND / 'js' / 'announcements.js').read_text(encoding='utf-8')
    catalog = (ROOT / 'replica' / 'application' / 'announcements.py').read_text(
        encoding='utf-8')
    assert '<script src="js/announcements.js"></script>' in HTML
    assert 'data-announcement-id="qq-group-2026-11"' in HTML
    assert 'Announcements.maybeShow' in JS
    assert 'announcement_seen' in ann_js
    assert 'real_get_announcements' in ann_js
    assert 'real_set_ui_state' in ann_js
    assert "'id': 'qq-group-2026-11'" in catalog
    assert "'image': 'assets/qq-group.jpg'" in catalog
    assert '加群暗号：皮卡丘' in catalog
    assert '1125996019' not in catalog
    assert 'announcementNote' in ann_js
    assert 'announcement-head' in ann_js
    assert 'announcement-kicker' in ann_js
    assert '<figure class="announcement-image-wrap"' in ann_js
    assert '.announcement-kicker' in CSS
    assert '.announcement-image-wrap' in CSS
    assert 'announcement-rise' in CSS
    assert (FRONTEND / 'assets' / 'qq-group.jpg').is_file()


def test_login_remembers_password_only_through_the_encrypted_bridge():
    assert 'id="loginRemember"' in HTML
    assert '使用系统加密' in HTML
    assert "liveApi('get_saved_login'" in SOURCE
    assert "liveApi('save_login'" in SOURCE
    assert "liveApi('clear_saved_login'" in SOURCE
    # Never park the credential in browser storage (routes may use it).
    for line in SOURCE.splitlines():
        if 'localStorage' in line or 'sessionStorage' in line:
            assert 'pass' not in line.lower()


def test_points_follow_the_selected_target_distance():
    # The checkpoint endpoint is distance-scoped; a fixed 2500 m can be an
    # invalid distance for another school and return no points.
    assert 'distance_m' in SOURCE
    assert 'schedulePointsForDistance' in SOURCE
    assert '无法确认该校区是否允许自由跑' in SOURCE
    assert '跳过打卡点（仅无点位学校）' in HTML
    assert "id=\"setCampusBtn\"" in HTML


def _js_function(name):
    start = JS.index('function ' + name)
    depth = 0
    for i in range(JS.index('{', start), len(JS)):
        if JS[i] == '{':
            depth += 1
        elif JS[i] == '}':
            depth -= 1
            if depth == 0:
                return JS[start:i + 1]
    raise AssertionError('unterminated function ' + name)


def test_every_distance_change_reloads_distance_scoped_points():
    # Drawing/importing a route, the random preset and the plan "fill" button
    # all change #inKm.  Drawing is the automatic path: it is debounced and
    # budgeted (the endpoint allows 3 calls / 5 min and each reply is a
    # different random five-point draw).
    assert 'schedulePointsForDistance(true)' in _js_function(
        'autoFillDistanceFromRoute')
    assert 'schedulePointsForDistance()' in _js_function('applyPlanSlot')


def test_points_failure_keeps_the_last_set_and_reports_provenance():
    load = _js_function('loadPoints')
    # A points failure arrives as a successful envelope with checkpoints=null;
    # it must not be turned into an empty list (that wiped the map and the
    # checklist on every 10603).
    assert 'Array.isArray(d.checkpoints)' in load
    assert 'points_distance_m' in load
    assert 'points_stale' in load
    assert 'points_retry_after_s' in load
    # Drawing only updates the "distance changed" state; the actual fetch
    # happens when the line is finalized and is still budgeted.
    sched = _js_function('schedulePointsForDistance')
    assert 'finalizeRoute' in sched
    assert 'refreshPointsDistanceState' in sched
    fin = _js_function('finalizeRoute')
    assert 'POINTS_AUTO_GAP_MS' in fin
    assert 'pointsCooldownLeft()' in fin
    assert 'distance_m: want' in fin
    # Submit sends the distance of the set the operator saw.
    assert 'points_distance_m: LIVE.pointsDistance' in SOURCE
    assert 'function livePointsPayload' in SOURCE
    assert 'points: (!freeRunOn() && livePoints.length) ? livePointsPayload() : null' in SOURCE


def test_background_refresh_never_refits_the_map():
    # Only an explicit fit may move the camera; otherwise every 900 ms refresh
    # snapped the view back to the fence while the line was being drawn.
    assert 'opts.fit === true' in _js_function('setFences')
    assert 'opts.fit === true' in _js_function('renderPoints')
    assert 'const wantFit' in _js_function('loadPoints')


def test_multi_fence_ui_uses_all_rings_and_marks_invalid_areas():
    assert 'disabled' in _js_function('renderRunAreas')
    assert '（无效）' in _js_function('renderRunAreas')
    assert 'rings.some' in _js_function('fenceOutside')
    assert 'count === 1' in _js_function('updateCampusControls')
    assert '跳过打卡点（仅无点位学校）' in HTML
    assert 'setFences' in JS
    start = JS.index("['inKm', 'inPace', 'inCad', 'inAlt']")
    assert 'schedulePointsForDistance()' in JS[start:JS.index('});', start)]


def test_skip_checkpoints_cannot_leak_into_the_next_account():
    # "Skip checkpoints" is a per-run confirmation; a logout/account switch
    # must not leave the next account running without checkpoints.
    assert 'free.checked = false' in _js_function('renderEmptyData')


def test_run_area_selector_matches_the_official_dialog():
    assert 'id="runAreaSel"' in HTML
    assert 'function renderRunAreas' in JS
    assert 'function updateCampusControls' in JS
    assert "run_area_id: chosenRunAreaId()" in JS
    # The onboarding must not promise a manual campus override when the
    # server already returned a fence (the fence centre wins).
    assert '地图不是你的学校时，把地图拖到学校后点「设为校区」' not in JS
    assert '没有围栏' in JS


def test_windows_icon_ships_every_size():
    icon = (ROOT / 'packaging' / 'assets' / 'logo.ico').read_bytes()
    reserved, kind, count = struct.unpack_from('<HHH', icon, 0)
    assert reserved == 0 and kind == 1
    sizes = set()
    for i in range(count):
        offset = 6 + i * 16
        width = icon[offset] or 256
        height = icon[offset + 1] or 256
        sizes.add((width, height))
    # A 16-only ICO gets stretched by the taskbar/Explorer and looks blurry.
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes


def test_window_icon_comes_from_a_high_resolution_file():
    assert (ROOT / 'frontend' / 'app.ico').is_file()
    assert "webview.start(icon=" in (ROOT / 'app.py').read_text(encoding='utf-8')


def test_desktop_downloads_are_enabled_for_exports():
    # pywebview cancels every download unless ALLOW_DOWNLOADS is on; CSV / FIT /
    # GPX / diagnostics exports are all anchor downloads.
    app_py = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert "webview.settings['ALLOW_DOWNLOADS'] = True" in app_py


def test_csv_export_covers_more_than_the_visible_table():
    # The records table only loads 20 rows; the export asks for the full list.
    assert 'async function exportRecordsCsv' in JS
    assert "liveMaybe('real_recent_records', {limit: 200}" in JS
    assert 'haoxiangpao-records.csv' in JS
    assert "toast('已导出 '" in JS


def test_map_watermark_uses_the_login_account():
    assert '.map-watermark' in CSS
    assert 'function updateMapWatermarks' in JS
    assert 's.username || s.phone' in JS
    # Leaflet's .leaflet-map-pane is a stacking context at z 400, so a child
    # of the container must sit above it to cover the tile pane.  The old
    # 250 painted the watermark behind the tiles and it disappeared on the map.
    assert 'z-index:401' in CSS
    assert 'z-index:250' not in CSS
    assert 'pointer-events:none' in CSS


def test_freeze_and_face_warnings_are_wired():
    assert 'LIVE.freezeSeconds' in SOURCE
    assert 'LIVE.faceRequired' in SOURCE


def test_checkpoint_radius_comes_from_the_backend():
    # The submit rule is 25 m (RunSeqLocationManager).  The checklist used a
    # hard-coded 40 m, so it could pass a route the server scores as missed.
    assert 'hit_radius_m' in JS
    assert 'LIVE.hitRadiusM' in JS
    assert 'function hitRadiusM' in JS
    assert '在 40m 内' not in JS
    assert '< 40)' not in JS


def test_submit_checklist_covers_freeze_face_and_distance():
    for label in ('跑步冻结期', '人脸抽检', '距离达标'):
        assert label in JS, label
    assert 'LIVE.freezeSeconds > 0' in JS
    assert 'min_km' in JS and 'max_km' in JS
    assert '账号处于跑步冻结期' in SOURCE
    assert '学校启用了人脸抽检' in SOURCE
    assert 'sequential: !!LIVE.sequential' in SOURCE


def test_network_egress_is_diagnostic_not_a_checklist_gate():
    # v0.1.8: IP geolocation is advisory.  It must never appear in
    # `refreshChecks()` or change `done === checks.length`.
    assert "label: '网络出口'" not in SOURCE
    assert 'id="preflightBtn"' in HTML
    assert '网络诊断（仅提醒）' in HTML
    assert '仅提醒，不影响提交' in HTML
    assert 'LIVE.preflight =' in SOURCE


def test_obs_track_fallback_is_wired():
    assert "meta.obs_error" in SOURCE
    assert '这条记录的轨迹在上传服务' in SOURCE


def test_update_icon_is_quiet_until_the_manifest_has_a_newer_build():
    assert 'id="updateBtn"' in HTML
    assert 'id="updateDlg"' in HTML
    assert 'id="updateCheckBtn"' in HTML
    assert "liveApi('real_check_update'" in SOURCE
    assert "liveApi('real_start_download_update'" in SOURCE
    assert "liveApi('real_apply_update'" in SOURCE
    assert "liveApi('real_quit_app'" in SOURCE
    assert 'btn.hidden = !avail' in SOURCE
    # The package is verified locally and then swapped after the app exits.
    assert 'sha256 已校验' in SOURCE
    assert '更新会关闭程序、自动安装并重启；data 文件夹保留。' in SOURCE


def test_partial_school_rules_and_issue_records_are_visible():
    assert '留空 = 学校没有这项要求' in SOURCE
    assert 'data-rec-filter="issue"' in SOURCE
    assert 'id="netProxy"' in SOURCE
    assert 'id="netAmap"' in SOURCE
    assert 'logo-128.png' in SOURCE


def test_run_parameter_inputs_are_numeric():
    for input_id in ('inKm', 'inPace', 'inCad', 'inAlt'):
        tag = re.search(r'<input\b[^>]*\bid="%s"[^>]*>' % input_id, HTML)
        assert tag, input_id
        assert 'type="number"' in tag.group(0), input_id


def test_run_input_ranges_are_enforced_in_js():
    assert 'function validateRunParams(p)' in JS
    for marker in ("目标距离', unit: 'km'", "配速', unit: 'min/km'",
                   "步频', unit: 'spm'", "海拔基准', unit: 'm'"):
        assert marker in JS


def test_free_run_toggle_keeps_normalised_points():
    # renderPoints must accept the shape it produces itself; otherwise
    # toggling 自由跑 wiped every checkpoint because p.lon is undefined.
    assert "p.name || p.pointName || p.n" in JS
    assert "p.lon != null ? p.lon : p.lng" in JS
    assert 'role="button"' in JS


def test_plan_fill_sets_submit_date_and_blocks_future_rows():
    assert 'function planSlotAllowed(day)' in JS
    assert 'function applyPlanSlot(slot, opts)' in JS
    assert '未来日期不能提交' in JS
    assert '超过 3 天，不能再补交' in JS
    assert 'planSlotAllowed(s.day)' in JS


def test_account_switch_invalidates_inflight_replies():
    assert 'function invalidateLiveRequests()' in JS
    assert 'if(seq !== _overviewSeq) return;' in JS
    assert 'if(seq !== _receiptsSeq) return;' in JS


def test_today_nav_indicator_is_wired():
    assert 'id="runNavDot"' in HTML
    assert 'function updateTodayNav()' in JS
    assert "今天已跑 ' + n + ' 次" in JS


def test_profile_and_violation_pages_are_wired():
    assert 'id="page-profile"' in HTML
    assert 'data-page="profile"' in HTML
    assert "liveApi('real_profile'" in JS
    assert "liveApi('real_violations'" in JS
    assert '结果未知不会当成正常' in HTML
    assert 'id="violState"' in HTML


def test_device_profile_is_single_source_and_visible():
    # The account page must show the effective installation fingerprint, not
    # "device not loaded" while submissions silently use a compiled-in id.
    assert 'device_source' in JS
    assert 'device_name' in JS
    assert 'device_os' in JS
    assert 'device_platform' in JS
    assert "s.device_platform === 'ios'" in JS
    # The iOS chain was verified on a real device (rrid 1344757460); the old
    # "unverified" tooltip contradicts docs/handoff.md.
    assert 'iOS 链路已真机验证' in JS
    assert '提交链尚未真实验证' not in JS
    assert 'function deviceLabelText' in JS
    assert "deviceLabelText(s)" in JS
    assert "Android ' + s.device_os" not in JS
    assert '设备指纹在本机生成一次' in JS
    assert 's.deviceName' not in JS
    assert 's.deviceModel' not in JS
    assert 's.DeviceId' not in JS


def test_device_rotation_is_advanced_and_double_confirmed():
    assert 'id="deviceRotateBtn"' in HTML
    assert 'id="deviceRotateConfirm"' in HTML
    assert 'id="deviceRotatePlatform"' in HTML
    assert '<option value="ios"' in HTML
    assert '高级功能：更换设备指纹' in HTML
    assert '更换后需要重新登录' in HTML
    assert '跨平台切换' in HTML
    assert "liveApi('real_rotate_device'" in JS
    assert "input.value.trim() !== '更换'" in JS
    assert "platform: platform" in JS
    assert '不要频繁更换' in JS


def test_device_settings_live_on_the_settings_page():
    accounts = HTML.split('id="page-accounts"', 1)[1].split('id="page-profile"', 1)[0]
    settings = HTML.split('id="page-settings"', 1)[1]
    assert 'deviceRotateBtn' not in accounts
    assert 'deviceRotateBtn' in settings
    assert 'customDeviceSave' in settings
    assert 'id="deviceSection"' in settings
    assert '设备指纹、更换和自定义设备已移到设置页' in accounts


def test_custom_device_is_advanced_and_explicit():
    assert '高级功能：自定义设备' in HTML
    assert 'id="customDevicePlatform"' in HTML
    assert 'id="customDeviceModel" type="text"' in HTML
    assert 'id="customDeviceOs" type="text"' in HTML
    assert 'id="customDevicePhysic" type="text"' in HTML
    assert 'id="customDeviceLogic" type="text"' in HTML
    assert 'id="customDeviceCpu" type="text"' in HTML
    assert 'id="customDeviceId"' in HTML
    assert 'id="customDeviceAge"' in HTML
    assert 'id="customDeviceConfirm"' in HTML
    assert 'id="customDeviceSave"' in HTML
    assert "<select id=\"customDeviceModel\"" not in HTML
    assert "/bridge/real_device_options" not in HTML
    assert "liveApi('real_get_device_options'" in JS
    assert "liveApi('real_set_custom_device'" in JS
    assert "physicPixel: physicPixel" in JS
    assert "logicPixel: logicPixel" in JS
    assert "cpuModel: cpuModel" in JS
    assert "confirmInput.value.trim() !== '自定义'" in JS
    assert "value('#customDeviceConfirm') !== '自定义'" in JS
    assert '自定义设备不自洽' not in JS


def test_distance_follows_drawn_route_until_manual_edit():
    assert 'function autoFillDistanceFromRoute(m)' in JS
    assert 'distanceManual' in JS
    assert '画好路线后会自动按线路长度填写目标距离' in HTML
    assert 'Math.ceil(target / m - 0.005)' in JS


def test_quick_fill_block_is_removed():
    assert '快速填充' not in HTML
    for marker in ('id="randomParams"', 'id="planFill"',
                   'id="fillRouteDistance"', 'data-km=', 'data-pace='):
        assert marker not in SOURCE, marker
    # Presets and the computed summary stay.
    assert 'id="presetCard"' in HTML
    assert 'id="sumTime"' in HTML


def test_clipboard_has_a_fallback():
    assert 'async function copyPlainText(text)' in JS
    assert 'document.execCommand' in JS
    assert '复制失败，请手动选择' in JS


def test_submit_is_blocked_while_account_switch_is_busy():
    assert "提交进行中，等结果出来后再切换账号" in JS
    assert "提交进行中，等结果出来后再退出" in JS
    assert "提交进行中，等结果出来后再核对回执" in JS


def test_no_blocking_external_font_request():
    assert 'cdn.jsdelivr.net' not in SOURCE


def test_overview_state_is_reactive():
    # Points, rules and the model arrive after the first overview paint.
    # Baking "未加载/未配置" into renderOverview left the checklist stale.
    assert 'function refreshOverviewState' in JS
    assert 'refreshOverviewState();' in JS
    assert 'LIVE.weekLoaded' in JS
    assert "点位已加载 · ' + livePoints.length" in JS
    assert '学校要求已配置' in JS


def test_semester_indoor_goal_is_rendered_separately():
    # goalMethod=2 is the indoor-exercise counter, not the semester goal.
    assert "const hasIndoor = v.indoor_goal != null" in JS
    assert "室内锻炼 ' + (v.indoor_valid || 0)" in JS
    assert "户外未设目标" in JS
    # the remaining indoor count must land in the 还差 cell, not only in text
    assert "hasIndoor && v.indoor_remaining != null" in JS
    assert "leftLabel.textContent = (distanceMode || countMode)" in JS


def test_semester_local_goal_is_configurable_in_the_ui():
    # A local target fills in an outdoor goal the server never sent.
    assert 'id="rSemesterKm"' in HTML
    assert 'id="ovSemesterGoalLink"' in HTML
    assert "liveApi('get_semester_goal'" in JS
    assert "liveApi('save_semester_goal'" in JS
    assert "v.goal_source === 'local'" in JS
    assert "学期汇总 · 本机目标" in JS


def test_semester_count_goal_is_rendered_in_runs():
    # A real count goal still renders in 次.
    assert "const countMode = v.goal_km == null && v.goal_count != null" in JS
    assert "got + '<small> 次</small>'" in JS
    assert "v.remaining_count + '<small> 次</small>'" in JS


def test_plan_horizon_stops_at_the_semester_end():
    # A 14-day plan must not recommend runs after the semester deadline.
    assert 'function planHorizonDays' in JS
    assert '+view.days_left + 1' in JS
    assert "liveApi('real_plan', {days: planDays})" in JS
    assert 'const planDays = planHorizonDays(semView);' in JS
    assert 'id="ovPlanTitle"' in HTML
    assert '本学期已结束' in JS


def test_semester_card_hides_an_empty_progress_bar():
    # 0% with no goal behind it reads as "no data"; hide the track instead.
    assert 'const showBar = v.completed_pct != null' in JS
    assert 'bar.parentElement.hidden = !showBar' in JS
    assert 'id="ovSemesterNote"' in HTML
    assert 'id="profSemesterNote"' in HTML


def test_first_run_guide_copy_is_written_for_the_post_login_moment():
    assert '已登录：工具已经知道你在哪个校区' in JS
    assert '先登录，工具才知道你在哪个校区' not in JS
    assert "id=\"onboardSkip\">本次跳过" in HTML
    assert "$('#onboardSkip').addEventListener('click', () => closeOnboard(null));" in JS


def test_login_cover_reset_is_disabled_without_a_custom_cover():
    assert "$('#coverReset').disabled = !hasCustom" in JS
    assert '<link rel="stylesheet" href="/login-cover.css"' in HTML


def test_preview_footer_stays_visible_in_the_card():
    block = re.search(r'\.dp-foot\{([^}]*)\}', CSS, re.S)
    assert block and 'position:sticky' in block.group(1)


def test_toasts_wait_for_the_boot_splash():
    assert 'const toastQueue = []' in JS
    assert 'function flushBootToasts' in JS
    assert "addEventListener('hxp:boot-done'" in JS


def test_version_footer_drops_the_commit_hash():
    assert "const short = 'v' + (d.version || '?');" in JS


def test_submit_result_is_a_persistent_card_not_a_toast():
    assert 'id="submitResult"' in HTML
    assert 'id="submitResultRecords"' in HTML
    assert 'function submitLocked' in JS
    assert '先核对回执再提交' in JS
    # An unknown result must lock submit until the read-only receipt check.
    assert "setSubmitResult('unknown'" in JS
    assert "await reconcileReceipts()" in JS
    assert 'state === \'unknown\' &&\n       !LIVE.submitResult.checked' in JS


def test_submit_checklist_rows_only_look_clickable_when_they_go_somewhere():
    checks = _js_function('refreshChecks')
    for target in ("goto: 'accounts'", "goto: 'settings'",
                   "goto: 'records'", "rtab: 'route'"):
        assert target in checks, target
    assert 'function followDataGoto' in JS
    assert '.check-row[data-goto]{cursor:pointer}' in CSS
    # Rows without a destination stay a plain label, not a fake button.
    assert 'cursor:default' in CSS


def test_route_undo_keeps_full_snapshots_and_supports_redo():
    assert 'const ROUTE_HISTORY_LIMIT = 50' in JS
    for name in ('routeSnapshot', 'pushRouteSnapshot', 'undoRoute',
                 'redoRoute', 'deleteRoutePoint', 'beginRoutePointDrag'):
        assert 'function ' + name in JS, name
    assert "ev.key === 'Delete'" in JS
    assert "String(ev.key || '').toLowerCase() === 'z'" in JS
    # Every point carries its order, not only the start.
    assert "'起点 · 第 1 点'" in JS
    assert "('第 ' + (i + 1) + ' 点')" in JS


def test_points_cooldown_button_ticks_and_disables_while_cooling():
    assert 'setInterval(syncPointsCooldownUI, 1000)' in JS
    assert "btn.textContent = '冷却 ' + left + 's'" in JS
    assert "btn.disabled = true" in _js_function('syncPointsCooldownUI')
    assert '.btn:disabled{opacity:.45' in CSS


def test_run_area_dropdown_can_return_to_automatic_matching():
    render = _js_function('renderRunAreas')
    assert '自动（按账号校区）' in render
    assert 'value=""' in render
    assert 'run_area_choice: {[key]: null}' in JS


def test_target_distance_starts_at_zero_and_is_filled_by_the_route():
    assert 'min="0" max="42.2" step="0.01" value="0"' in HTML
    assert "if(key === 'km' && !(v > 0))" in JS
    assert "先手绘一条路线，或在参数页填写目标距离" in JS
    # Deleting the last point puts the auto-filled target back to 0.
    assert "kmEl.value = '0'" in JS


def test_points_are_fetched_on_route_finalize_not_every_10m():
    sched = _js_function('schedulePointsForDistance')
    # The auto/drawing branch must not call the endpoint.
    auto_branch = sched.split('if(auto){', 1)[1].split('}', 1)[0]
    assert 'loadPoints' not in auto_branch
    assert 'refreshPointsDistanceState' in auto_branch
    fin = _js_function('finalizeRoute')
    assert 'pointsCooldownLeft()' in fin
    assert 'POINTS_AUTO_GAP_MS' in fin
    assert 'distance_m: want' in fin
    for trigger in ('scheduleRouteFinalize(300, \'pen-off\')',
                    "scheduleRouteFinalize(150, 'tab:'",
                    "scheduleRouteFinalize(300, 'import')",
                    "scheduleRouteFinalize(1500, 'drag')"):
        assert trigger in JS, trigger


def test_stale_points_distance_is_visible_and_actionable():
    assert 'function pointsDistanceStale' in JS
    assert 'function pointsDistanceText' in JS
    assert '点位与目标距离' in JS
    assert "action: 'refresh-points'" in JS
    assert "checkAction === 'refresh-points'" in JS
    assert '点位按 %d m 加载，本次目标 %d m' in (
        ROOT / 'replica' / 'adapters' / 'bridge_run.py'
    ).read_text(encoding='utf-8')


def test_submit_button_and_preview_confirm_report_unfinished_checks():
    checks = _js_function('refreshChecks')
    # The button carries the count, so it is not a silent disabled state.
    assert "missingNow = checks.filter(c => !c.ok)" in checks
    assert "' 项未完成）'" in checks
    assert "提交这次跑步（' + missingNow.length" in checks
    # The preview primary action names the same count and asks once.
    preview = _js_function('renderDataPreview')
    assert "仍有 ' + missingCount + ' 项未通过，继续提交" in preview
    assert "if(pending.previewError){ cancelDataPreview(); return; }" in JS
    assert "confirm('还有 ' + missing.length + ' 项检查未通过" in JS


def test_failed_preview_offers_return_instead_of_one_click_submit():
    preview = _js_function('renderDataPreview')
    assert "p.previewError = failed ? String(errorText) : ''" in preview
    assert "$('#dpConfirm').textContent = '返回修改'" in preview
    assert "dataset.action = 'back'" in preview
    # The old one-click escape hatch must never come back.
    assert "'仍然提交'" not in JS
    assert '不能提交没有预览过的数据' in preview
    # The raw bridge error (it can contain an absolute path) belongs in the
    # collapsed details block, never in the footnote or the chart placeholders.
    assert "$('#dpErrorRaw').textContent = failed ? String(errorText) : ''" in preview
    assert "$('#dpErrorDetails').hidden = !failed" in preview
    assert 'id="dpErrorDetails"' in HTML
    foot = preview[preview.index("if($('#dpFootNote'))"):
                   preview.index("if($('#dpErrorDetails'))")]
    assert 'errorText' not in foot
    assert '预览没有生成，请返回修改后重试' in foot
    assert 'esc(errorText)' not in preview
    assert '预览没有生成，请返回修改后重试。</div>' in preview


def test_logout_warns_before_discarding_an_unsaved_route():
    logout = _js_function('liveLogout')
    # Dirty + has content, not "length >= 2": a loaded saved route must stay
    # silent, and a one-point scratch line must still warn.
    assert 'const unsavedRoute = routeDirty && route.length > 0;' in logout
    assert '当前路线未保存，退出后会清空。继续退出？' in logout
    assert 'if(!confirm(ask)) return;' in logout
    assert 'let routeDirty = false;' in JS
    # Edits mark the line dirty.
    assert 'routeDirty = true;' in _js_function('deleteRoutePoint')
    assert 'routeDirty = true;' in _js_function('applyRouteSnapshot')
    assert 'routeDirty = true;' in _js_function('importRouteFile')
    assert "pushRouteHistory('添加第 ' + (route.length + 1) + ' 点');\n  routeDirty = true;" in JS
    assert "if(route.length) pushRouteHistory(routeClosed ? '关闭闭合' : '打开闭合');\n  routeDirty = true;" in JS
    # Saving, clearing and loading a library route clear it again.
    assert 'routeDirty = false;' in _js_function('saveCurrentRoute')
    assert 'routeDirty = false;' in _js_function('clearCurrentRouteAfterSubmit')
    assert "route = line;\n    routeDirty = false;" in JS
    delete = _js_function('deleteSelectedRoute')
    assert '只删除本机路线库，无法撤销' in delete
    assert "if(route.length && !confirm('清空当前路线？" in JS


def test_school_id_chip_stays_in_plain_language_at_runtime():
    # index.html already says 学校识别码; the three runtime writers must not
    # overwrite it back to the internal field name after login/profile refresh.
    for name in ('renderSession', 'renderProfileEmpty', 'renderProfile'):
        body = _js_function(name)
        assert '学校识别码 ' in body, name
        assert "'unid '" not in body, name
        assert "'unid —'" not in body, name
    assert "'unid ' +" not in JS
    assert "'unid —'" not in JS
    assert '学校识别码 —' in HTML


def test_check_snapshot_feeds_both_ready_and_missing():
    # `allReady` and `missing` must come from the same refreshChecks() call;
    # the old code snapshotted the dock before two awaits and read the DOM after.
    checks = _js_function('refreshChecks')
    assert 'return {all, missing: missingNow};' in checks
    submit = _js_function('submitRun')
    assert 'const checkState = refreshChecks() || {all: false, missing: []};' in submit
    assert '!!checkState.all, checkState.missing' in submit
    preview = _js_function('previewPendingFrom')
    assert 'Array.isArray(missingNow)' in preview


def test_submit_button_stays_clickable_with_unfinished_checks():
    checks = _js_function('refreshChecks')
    # Product decision: warn + confirm, do not hard-disable on `!all`.
    assert 'Intentionally NOT disabled by `!all`' in checks
    disabled = checks[checks.index("$('#submitRun').disabled ="):]
    disabled = disabled[:disabled.index(';')]
    assert '!all' not in disabled
    assert 'LIVE.submitting || locked || !hasTarget' in disabled


def test_unknown_submit_result_is_not_described_as_a_failure():
    explain = _js_function('renderSubmitExplain')
    assert "r.state === 'unknown'" in explain
    assert '正在整理这次的结果…' in explain
    assert '正在解释这次失败…' in explain
    # A failed explain request collapses instead of hanging on "正在解释…".
    worker = _js_function('explainSubmitResult')
    assert '暂时无法解释' in worker
    assert '原始信息见下方详情' in worker
    assert 'if(LIVE.submitResult === r) renderSubmitExplain(r);' in worker


def test_first_run_guide_does_not_chain_the_announcement_popup():
    close = _js_function('closeOnboard')
    assert 'Announcements.maybeShow' not in close
    # "Skip" still persists "guide shown", so the next launch can offer the
    # announcement instead of suppressing it forever behind the guide.
    assert 'const keep = remember === false ? false : checked;' in close
    assert 'if(remember !== null || checked){' in close
    assert "liveApi('real_set_ui_state', {onboard_done: keep}, 8000)" in close
    assert "$('#onboardSkip').addEventListener('click', () => closeOnboard(null));" in JS
    # The next launch path and the About entry still offer the notice.
    assert "if(!guide && window.Announcements) Announcements.maybeShow();" in JS
    assert 'id="onboardOpen"' in HTML
    assert 'data-announcement-id="qq-group-2026-11"' in HTML


def test_user_facing_copy_uses_plain_terms():
    # Record ids, upload progress, credentials and the local service.
    assert '<span>记录编号</span>' in HTML
    assert '记录编号 <b>' in JS
    assert '轨迹上传 ' in JS
    assert "' · OBS '" in JS              # raw bridge text stays in 详情
    assert '登录凭证有效' in JS
    assert '该功能还没有接入本地服务' in JS
    # Internal field names are tooltips/advanced labels, not main rows.
    assert 'title="技术字段：unid"' in HTML
    assert 'title="技术字段：DeviceId"' in HTML
    assert 'title="技术字段：UA"' in HTML
    assert '官方自由跑（技术字段 serviceType=3）' in HTML


def test_submit_result_card_explains_failure_in_place():
    assert 'id="submitResultExplain"' in HTML
    assert 'id="submitResultDetails"' in HTML
    assert '<summary>详情</summary>' in HTML
    assert 'function renderSubmitExplain(r)' in JS
    assert 'function explainSubmitResult(r)' in JS
    assert "explainError(code, r.raw || r.detail || r.title || '', {source: 'submit'})" in JS
    assert "const rawSummary = 'rrid='" in JS


def test_docs_match_the_close_toggle_default():
    usage = (ROOT.parent / 'docs' / 'usage.md').read_text(encoding='utf-8')
    assert '闭合开关默认关' in usage
    assert '闭合开关默认开' not in usage
    assert '需要绕圈时手动打开「闭合」' in usage


def test_login_errors_explain_the_guard_and_device_check():
    pretty = _js_function('prettyLoginError')
    assert '/设备校验未通过|账号绑定校验失败/' in pretty
    assert '第一次登录的本地校验没通过' in pretty
    assert '诊断包发到 QQ 群' in pretty
    assert '/一号一设备/' in pretty
    assert '这台电脑已经绑定了另一个账号' in pretty
    assert '联系维护者解绑' in pretty
    assert '找给你软件的同学' not in pretty
    # The raw server/guard sentence stays first, the readable hint is the
    # second line, which needs `white-space: pre-line` to survive.
    assert "return msg + '\\n第一次登录的本地校验没通过" in pretty
    assert "return msg + '\\n这台电脑已经绑定了另一个账号" in pretty
    assert 'white-space:pre-line' in CSS


def test_login_page_warns_about_device_binding_and_offers_prelogin_help():
    # The rule is only explained after a rejection today; warn before the
    # first login so a shared computer cannot bind by accident.
    assert '第一次登录会把账号绑定到这台电脑' in HTML
    assert '退出登录或换号都不会解除绑定' in HTML
    assert 'id="loginHelp"' in HTML
    assert 'login-help-link' in HTML
    # The old .login-help footer rule was dead but overrode the new button
    # when both used the same class.  Keep the collision gone.
    assert '.login-help{' not in CSS
    # The QQ announcement must be reachable from the login gate, before any
    # session exists; otherwise a rejected user has nowhere to ask for help.
    assert "Announcements.openById('qq-group-2026-11')" in JS
    assert "getElementById('loginHelp')" in JS or "$('#loginHelp')" in JS


def test_ai_route_picker_is_named_separately_from_school_rules():
    # "模型" is the school-rules model; the LLM feature is "AI 选路" so the
    # two settings do not look like the same thing.
    assert '<h2>AI 选路（可选）</h2>' in HTML
    assert '配置 AI 选路（可选）' in HTML
    assert '学校要求未配置' in HTML
    assert 'data-scroll="llmSection"' in HTML
    goto = _js_function('followDataGoto')
    assert 'el.dataset.scroll' in goto
    assert 'scrollIntoView' in goto
    usage = (ROOT.parent / 'docs' / 'usage.md').read_text(encoding='utf-8')
    assert '智能规划与 AI 选路' in usage
    assert '不配置也能正常使用全部功能' in usage


def test_announcement_action_does_not_resize_on_hover():
    # The global .btn hover lifts the button and grows its offset shadow.
    # On the modal action that read as the button growing/shrinking; keep it
    # visually stable and only change color.  The ID selector outranks the
    # poster/archive skin rules, which otherwise put the lift back.
    assert '#announcementModal .announcement-card .btn:hover,' in CSS
    assert '#announcementModal .announcement-card .btn.primary:not(:disabled):hover::after{animation:none}' in CSS
    assert '#announcementModal .announcement-card .btn:active{transform:none}' in CSS
    skins = (FRONTEND / 'css' / 'skins.css').read_text(encoding='utf-8')
    assert 'html[data-skin="poster"] .btn:hover' in skins
    # The ID selector (1,2,0) outranks the skin rule (0,3,1) without relying
    # on source order.  Each skin keeps its own hover palette.
    for skin in ('paper', 'poster', 'archive', 'minimal'):
        assert f'html[data-skin="{skin}"] #announcementModal .announcement-card .btn.primary:hover' in CSS


def test_manual_announcement_does_not_consume_the_auto_notice():
    ann = (FRONTEND / 'js' / 'announcements.js').read_text(encoding='utf-8')
    # The login help button opens the QQ notice manually.  It must not mark
    # the machine-wide announcement as seen unless the user ticks the box.
    assert 'open(item, {manual: true})' in ann
    assert '(!state.manual || keep === true)' in ann
    assert 'never.checked = !state.manual' in ann
    # Two aria-modal dialogs must not be active at once.  Record the opener
    # before inerting the gate, or focus restoration falls back to body.
    assert 'gate.inert = true' in ann
    assert 'state.loginGate.inert = false' in ann
    assert ann.index('state.focus = document.activeElement;') < ann.index('gate.inert = true')


def test_code_from_text_ignores_unrelated_five_digit_numbers():
    fn = _js_function('codeFromText')
    cases = [
        ('提交被拒: error=10001 该时段已锁定 (HTTP 200)', 10001),
        ('错误码：11016 不在有效跑步时间', 11016),
        ('HTTP 403', 403),
        ('sidecar/加密层启动失败: [WinError 10048] 每个套接字地址只允许使用一次', None),
        ('目标距离 10000 米超过上限', None),
        ('本地服务超时（后台可能仍在执行，请勿重复提交）', None),
    ]
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed; codeFromText behaviour test skipped')
    script = '\n'.join([
        fn,
        'const cases = ' + json.dumps([s for s, _ in cases], ensure_ascii=False) + ';',
        'console.log(JSON.stringify(cases.map(s => codeFromText(s))));',
    ])
    proc = subprocess.run([node, '-e', script], check=False, capture_output=True,
                          text=True, encoding='utf-8')
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [c for _, c in cases]


def test_submit_explain_waits_only_when_a_code_exists():
    render = _js_function('renderSubmitExplain')
    assert "if(r.state === 'ok' || code == null){ box.hidden = true; return; }" in render
    worker = _js_function('explainSubmitResult')
    assert "if(code == null){ renderSubmitExplain(r); return; }" in worker
    submit = _js_function('doSubmitRun')
    # The backend classifies the failure; the frontend only renders it.  The
    # prefix rules below remain a fallback for older sidecars.
    assert 'const unknown = submitFailureKind(j) === \'unknown\';' in submit
    assert 'j.code === 500' not in submit
    # A bridge failure's 500 is transport status, not a business error code.
    # Falling back to it made local refusals look like server errors.
    assert 'const errCode = codeFromText(msg);' in submit
    assert 'j.code && j.code !== 200' not in submit
    kind = _js_function('submitFailureKind')
    assert "if(data.failure_kind === 'fail' || data.failure_kind === 'unknown'){" in kind
    assert "if(/^提交被拒[:：]/.test(msg)) return 'fail';" in kind


def test_submit_failure_kind_separates_refusal_from_unknown():
    """Behaviour test: feed envelopes to the extracted pure function."""
    fn = _js_function('submitFailureKind')
    cases = [
        ({'code': 500, 'message': '提交被拒: error=10001 该时段已锁定 (HTTP 200)',
          'data': {'failure_kind': 'fail'}}, 'fail'),
        ({'code': 500, 'message': '提交被拒：error=10001 该时段已锁定 (HTTP 400)',
          'data': {'failure_kind': 'fail'}}, 'fail'),
        ({'code': 500, 'message': '提交结果未知（已锁定该时段，请勿重试）: timed out',
          'data': {'failure_kind': 'unknown'}}, 'unknown'),
        ({'code': 500, 'message': '服务器已受理但未返回记录号，结果未知（已锁定该时段）',
          'data': {'failure_kind': 'unknown'}}, 'unknown'),
        # The backend decision wins even when the message reads like the
        # other branch; the frontend must not re-derive it.
        ({'code': 500, 'message': '提交结果未知（已锁定该时段）',
          'data': {'failure_kind': 'fail'}}, 'fail'),
        ({'code': 500, 'message': '提交被拒: error=10001 该时段已锁定',
          'data': {'failure_kind': 'unknown'}}, 'unknown'),
        # Legacy envelopes without failure_kind still use the prefixes.
        ({'code': 500, 'message': '提交被拒: error=10001 该时段已锁定 (HTTP 500)',
          'data': None}, 'fail'),
        ({'code': 500, 'message': '提交异常，结果未知（已锁定该时段）: connection reset',
          'data': None}, 'unknown'),
        ({'code': 500, 'message': '本地服务超时（后台可能仍在执行，请勿重复提交）',
          'data': None}, 'unknown'),
        ({'code': 500, 'message': '本地服务不可用：TypeError: Failed to fetch',
          'data': None}, 'unknown'),
        ({'code': 500, 'message': '本地服务返回了无法识别的内容',
          'data': None}, 'unknown'),
        # An unknown enum value or a non-object data payload falls back to the
        # legacy prefixes instead of throwing.
        ({'code': 500, 'message': '提交被拒: error=10001',
          'data': {'failure_kind': 'bogus'}}, 'fail'),
        ({'code': 500, 'message': '提交结果未知: timed out',
          'data': 'oops'}, 'unknown'),
        ({'code': 500, 'message': '服务器拒绝了这次提交。', 'data': None}, 'fail'),
    ]
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed; submitFailureKind behaviour test skipped')
    script = '\n'.join([
        fn,
        'const envelopes = ' + json.dumps([j for j, _ in cases], ensure_ascii=False) + ';',
        'const expected = ' + json.dumps([k for _, k in cases]) + ';',
        'const got = envelopes.map(j => submitFailureKind(j));',
        'console.log(JSON.stringify(got));',
    ])
    proc = subprocess.run([node, '-e', script], capture_output=True, text=True,
                          encoding='utf-8')
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [k for _, k in cases]


def test_rules_guidance_explains_an_empty_school_configuration():
    assert '学校要求全部留空：如果学校确实没有配置要求，可以保存' in JS
    assert '学校没配置要求时，点「从服务器读取」确认一次' in JS
    assert '学校没配置要求时，点「从服务器读取」确认一次' in HTML
    dock = _js_function('liveDockClick')
    assert "switchRunTab('submit')" in dock
    assert "document.querySelector('#checks .check-row.warn')" in dock
    assert 'scrollIntoView' in dock


def test_narrow_layout_scrolls_records_and_stacks_check_notes():
    marker = '#recordTable{overflow-x:auto}'
    start = CSS.rindex('@media (max-width:760px){', 0, CSS.index(marker))
    narrow = CSS[start:CSS.index('\n}', start)]
    assert marker in narrow
    assert '#recordTable .thead,#recordTable .trow{min-width:520px}' in narrow
    # The account table must keep its own minmax template: the old unscoped
    # min-width forced every .simple-table row to 520px and clipped it.
    assert '.simple-table .thead,.simple-table .trow{min-width:520px}' not in CSS
    assert '.check-row{grid-template-columns:19px 1fr}' in narrow
    assert '.check-row .cnote{grid-column:2;text-align:left;max-width:none}' in narrow
    # Same specificity as the wide-screen cap, otherwise 145px still wins.
    assert '.run-panel .check-row .cnote{max-width:none}' in narrow
    wide = CSS[CSS.index('@media (min-width:761px){'):]
    assert '.run-panel .check-row .cnote{max-width:145px}' in wide

    # The record column has to be allowed to shrink below its content width,
    # or the grid track stays 520px and .page{overflow-x:hidden} clips it.
    assert '.records-layout>div{min-width:0}' in CSS
    marker = '.records-layout{grid-template-columns:minmax(0,1fr)}'
    start = CSS.rindex('@media (max-width:900px){', 0, CSS.index(marker))
    mobile = CSS[start:CSS.index('\n}', start)]
    assert marker in mobile


def test_main_copy_drops_the_remaining_internal_terms():
    assert '没有 token 时不会生成假点位' not in HTML
    assert '没有登录时不会生成假点位' in HTML
    assert "toast('请填写设备编号')" in JS
    assert '回读风险标记：' in JS
    # The record detail main row says it in plain language; isCheat only
    # survives as the tooltip/raw value.
    assert "risk.push('isCheat=1')" not in JS
    assert "risk.push('有风险标记')" in JS
    assert "$('#rdCheat').title = cheatRaw;" in JS
    # The raw bridge line (rrid=, OBS) may stay inside 详情/日志.
    assert "const rawSummary = 'rrid='" in JS


def test_usage_docs_explain_account_binding_without_scaring_users():
    usage = (ROOT.parent / 'docs' / 'usage.md').read_text(encoding='utf-8')
    assert '### 账号与这台电脑' in usage
    assert '登录保持（probe-only' in usage
    assert 'probe-only token 保活' not in usage
    assert '退出登录**不会**释放' in usage
    # Same-machine reinstall keeps the binding; moving machines does not.
    assert '同一台电脑重装' in usage
    assert '**换电脑**：绑定不会随 `data/` 迁移' in usage
    assert '整个 `data/` 目录一起带' not in usage
    assert '「设备校验未通过」' in usage
    assert '换电脑就废了' not in usage
    kb = (ROOT.parent / 'docs' / 'kb' / '46-account-guard.md').read_text(
        encoding='utf-8')
    assert '## 7. 用户指引' in kb
    assert '由维护者确认后解绑' in kb
    assert 'reset --state data\\account_binding.json --yes' in kb
    assert '同一台电脑重装' in kb
    assert '换电脑：绑定不会随 `data/` 迁移' in kb
    assert '新机器设备 ID 不同，视为未绑定' in kb
    assert '整个 `data/` 目录一起带' not in kb
