"""Static guards for the login gate and frontend asset split.

The full interaction test is Playwright-based (mocked bridge), but these checks
make sure a future edit cannot silently remove the gate, re-introduce a
blocking external font request, or break the external CSS/JS links.
"""
import re
import struct
from pathlib import Path

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
    assert '跳过打卡点（无点位学校，非官方自由跑）' in HTML
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
    # Auto refresh is budgeted and honours the persisted cooldown.
    sched = _js_function('schedulePointsForDistance')
    assert 'POINTS_AUTO_GAP_MS' in sched
    assert 'pointsCooldownLeft()' in sched
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
    assert '非官方自由跑' in HTML
    assert 'setFences' in JS
    start = JS.index("$('#randomParams')")
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


def test_network_egress_is_back_in_the_checklist():
    assert "label: '网络出口'" in SOURCE
    assert 'LIVE.preflight =' in SOURCE
    assert '未自检；点「开始自检」确认与校区同城（仅提醒）' in SOURCE


def test_obs_track_fallback_is_wired():
    assert "meta.obs_error" in SOURCE
    assert '这条记录的轨迹在 OBS' in SOURCE


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
    assert '高级：更换设备指纹' in HTML
    assert '更换后需要重新登录' in HTML
    assert '跨平台切换' in HTML
    assert "liveApi('real_rotate_device'" in JS
    assert "input.value.trim() !== '更换'" in JS
    assert "platform: platform" in JS
    assert '不要频繁更换' in JS


def test_distance_follows_drawn_route_until_manual_edit():
    assert 'function autoFillDistanceFromRoute(m)' in JS
    assert 'distanceManual' in JS
    assert 'id="fillRouteDistance"' in HTML
    assert '画好路线后会自动按线路长度填写目标距离' in HTML
    assert 'Math.ceil(target / m - 0.005)' in JS


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
    assert 'const days = planHorizonDays(LIVE.semester && LIVE.semester.view)' in JS
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


def test_run_area_dropdown_can_return_to_automatic_matching():
    render = _js_function('renderRunAreas')
    assert '自动（按账号校区）' in render
    assert 'value=""' in render
    assert 'run_area_choice: {[key]: null}' in JS
