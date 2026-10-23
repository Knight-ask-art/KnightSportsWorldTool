"""Generate NEW_API_LIST.md from extracted evidence."""
import json

api_detail = json.load(open(
    r'C:\Users\20969\AppData\Local\Temp\opencode\jhp_diff\api_detail.json',
    encoding='utf-8'))

wrappers = {
 'addProxyAgent': ('addProxyAgent', 4), 'addStudentAccount': ('addStudentAccount', 1),
 'adminLogin': ('adminLogin', 2), 'admini_changePassword': ('admini_changePassword', 3),
 'admini_getAccountBalance': ('admini_getAccountBalance', 2),
 'checkForUpdates': ('checkForUpdates', 0), 'checkRechargeStatus': ('checkRechargeStatus', 6),
 'checkRequiredFields': ('checkRequiredFields', 1),
 'checkSecretKeyStatus': ('checkSecretKeyStatus', 2), 'check_login_ip': ('check_login_ip', 1),
 'close_window': ('closeWindow', 0), 'connectVM': ('connectVM', 4),
 'deleteProxyAgent': ('deleteProxyAgent', 3),
 'deleteStudentAccount': ('deleteStudentAccount', 1),
 'delete_sport_data': ('delete_sport_data', 1), 'disconnectVM': ('disconnectVM', 0),
 'downloadCS7': ('downloadCS7', 0), 'downloadUpdate': ('downloadUpdate', 1),
 'downloadVM': ('downloadVM', 0), 'getAmapKey': ('getAmapKey', 0),
 'getAppLogo': ('getAppLogo', 0), 'getCampusRunPoints': ('getCampusRunPoints', 3),
 'getCampusRunPointsList': ('getCampusRunPointsList', 1),
 'getGeetestConfig': ('getGeetestConfig', 1), 'getGeetoken': ('getGeetoken', 0),
 'getProxyAgents': ('getProxyAgents', 3),
 'getProxyRechargeAddress': ('getProxyRechargeAddress', 2),
 'getProxyRechargeInfo': ('getProxyRechargeInfo', 2),
 'getSchoolList': ('getSchoolList', 0),
 'getStudentAccountDetail': ('getStudentAccountDetail', 1),
 'getStudentAccounts': ('getStudentAccounts', 1),
 'getStudentLoginInfoById': ('getStudentLoginInfo', 1),
 'getUpdateProgress': ('getUpdateProgress', 1),
 'getUsdtExchangeRate': ('getUsdtExchangeRate', 0), 'getVMStatus': ('getVMStatus', 0),
 'get_Apple_inspector': ('get_Apple_inspector', 0), 'get_TTT': ('get_TTT', 0),
 'get_account_and_password': ('getAccountAndPassword', 0),
 'get_app_set': ('get_app_set', 0),
 'get_cla_task_uuid_info': ('get_cla_task_uuid_info', 1),
 'get_geevalidate': ('get_geevalidate', 0), 'get_location': ('get_location', 1),
 'get_one_record': ('get_one_record', 4),
 'get_run_task_uuid_info': ('get_run_task_uuid_info', 1),
 'get_sava_sport_datas': ('get_sava_sport_datas', 3),
 'get_student_records': ('get_student_records', 2),
 'get_user_proxy': ('getUserProxy', 0), 'get_users_ordrs': ('getRechargeRecords', 5),
 'installUpdate': ('installUpdate', 2), 'is_window_maximized': ('isWindowMaximized', 0),
 'kill_task_uuid': ('kill_task_uuid', 1), 'login_verify': ('login_verify', 1),
 'maximize_window': ('maximizeWindow', 0), 'minimize_window': ('minimizeWindow', 0),
 'open_dirs': ('open_dirs', 1), 'queryProxyUser': ('queryProxyUser', 3),
 'redeemSecretKey': ('redeemSecretKey', 2),
 'repeat_run_task_uuid': ('repeat_run_task_uuid', 1),
 'save_sport_data': ('save_sport_data', 8), 'searchStudents': ('searchStudents', 1),
 'set_account_and_password': ('setAccountAndPassword', 2),
 'set_user_proxy': ('setUserProxy', 2), 'startResize': ('startResize', 1),
 'start_drag': ('startDrag', 0), 'studentLogin': ('studentLogin', 1),
 'studentLogout': ('studentLogout', 1), 'transfer_mileage': ('transfer_mileage', 4),
 'updateProxyAgentStatus': ('updateProxyAgentStatus', 4),
 'updateProxyRechargeAddress': ('updateProxyRechargeAddress', 6),
 'updateStudentAccount': ('updateStudentAccount', 2),
 'uploadRunningData': ('uploadRunningData', 3),
}

notes = {
 'studentLogin': 'backend(studentLogin(user, gee_uuid)); frontend pre-checks get_geevalidate==200 then merges getStudentAccountDetail into window.currentStudent',
 'get_geevalidate': 'backend(get_geevalidate(geetestValidateResult{challenge,validate,uuid}))',
 'uploadRunningData': 'CORE: backend builds run_data_ -> 11-step hybrid encrypt -> POST save/record with Appid/Geeid headers',
 'save_sport_data': 'CORE local store, 8 args',
 'get_sava_sport_datas': 'local store paged (unid, page=1, pageSize=20) -> {list,total,page,pageSize,totalPages}',
 'get_student_records': 'frontend asserts Array.isArray(data): server returns bare array',
 'getUsers_ordrs': 'wrapper getRechargeRecords(5 args)',
 'getProxyRechargeAddress': 'default chain ERC20 (was TRC20 in 5.2)',
 'get_TTT': 'returns bool',
 'getAppLogo': 'frontend fallback: builtin base64 svg',
 'getAppName': 'frontend-local, returns "" (no backend)',
 'getChainTypes': 'frontend-local {code:200,data:["ERC20"]}',
 'checkForUpdates': 'backend /api/checkForUpdates; flow downloadUpdate/getUpdateProgress/installUpdate',
 'updateProxyRechargeAddress': '6 args',
 'checkRechargeStatus': '6 args',
 'transfer_mileage': '(id, password, to, km); UI formats km.toFixed(2)',
}

L = []
L.append('# 00201.0.0 新前端 API 清单（71个，React桥接层实测提取）\n')
L.append('约定：`{code:200|500|600, message, data}`；桥接失败默认 `{code:500}`。\n')
L.append('| 后端 API | 前端 wrapper | 参数数 | 备注 |')
L.append('|---|---|---|---|')
for api in sorted(wrappers):
    w, n = wrappers[api]
    L.append(f'| `{api}` | `{w}` | {n} | {notes.get(api, "")} |')
L.append('\n`studentLogin`（无wrapper映射到bridge，页面直调）：`studentLogin(e)`，内部先过极验。\n')
L.append('## 后端 65530 端点（18个，二进制marshal常量）\n')
for p in ['addProxyAgent', 'adminLogin', 'admini_changePassword',
          'admini_getAccountBalance', 'checkForUpdates', 'checkRechargeStatus',
          'checkSecretKeyStatus', 'deleteProxyAgent', 'edclass',
          'getProxyAgents', 'getProxyRechargeAddress', 'getProxyRechargeInfo',
          'get_users_ordrs', 'queryProxyUser', 'redeemSecretKey',
          'transfer_mileage', 'updateProxyAgentStatus',
          'updateProxyRechargeAddress']:
    L.append(f'- `/api/{p}`')
L.append('\n## 本地VM端点\n- `POST /update_geetoken`（上传/领取池）\n- `GET /get_ca`（证书下载，端口65530）\n')
open(r'C:\Users\20969\AppData\Local\Temp\opencode\app\NEW_API_LIST.md',
     'w', encoding='utf-8').write('\n'.join(L))
print('wrote NEW_API_LIST.md')
