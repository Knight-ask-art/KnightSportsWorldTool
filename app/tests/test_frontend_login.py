"""Static guards for the login gate and frontend asset split.

The full interaction test is Playwright-based (mocked bridge), but these checks
make sure a future edit cannot silently remove the gate, re-introduce a
blocking external font request, or break the external CSS/JS links.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
HTML = (FRONTEND / 'index.html').read_text(encoding='utf-8')
JS = (FRONTEND / 'js' / 'app.js').read_text(encoding='utf-8')
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


def test_points_are_not_tied_to_target_distance():
    assert '按校区固定范围拉取' in SOURCE
    assert "label: '点位距离'" not in SOURCE
    assert 'distance_m: Math.max' not in SOURCE


def test_network_egress_is_back_in_the_checklist():
    assert "label: '网络出口'" in SOURCE
    assert 'LIVE.preflight =' in SOURCE
    assert '未自检；点「开始自检」确认与校区同城（仅提醒）' in SOURCE


def test_obs_track_fallback_is_wired():
    assert "meta.obs_error" in SOURCE
    assert '这条记录的轨迹在 OBS' in SOURCE


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
    assert '设备指纹在本机生成一次' in JS
    assert 's.deviceName' not in JS
    assert 's.deviceModel' not in JS
    assert 's.DeviceId' not in JS


def test_device_rotation_is_advanced_and_double_confirmed():
    assert 'id="deviceRotateBtn"' in HTML
    assert 'id="deviceRotateConfirm"' in HTML
    assert '高级：更换设备指纹' in HTML
    assert '更换后需要重新登录' in HTML
    assert "liveApi('real_rotate_device'" in JS
    assert "input.value.trim() !== '更换'" in JS
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
