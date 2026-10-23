# 02 · 接口与数据

> 所有路径都相对 `https://run.gxapp.iydsj.com`。
> 「来源」列里的 `upstream` 指 `YanamiNeko/NekoSportsWorldTool`，`APK` 指官方包常量，
> `实测` 指我们自己的请求/响应记录。

---

## 1. 通用约定

### 信封

业务数据走 NetSecKit 信封（纯 Python 实现在 `netsec.py`）：

```
POST /api/...
Headers: token / tokenSign / headerSign / 24 个设备头 ...
Body:   加密后的实体（base64）
Response: {status, message, data}，data 可能还是加密串
```

判定成功的两种写法都要认：

```python
if biz.get('error') not in (None, 10000): fail
if biz.get('status') is False: fail        # 有些接口只给 status
```

### 重试

| 类型 | 重试 |
|---|---|
| 只读查询（点位、列表、详情、汇总） | 2 次，指数退避 |
| 提交 `save/record` | **0 次**。超时=未知状态，交给回执锁住时段 |

### 坐标

| 字段 | 坐标系 |
|---|---|
| `lat` / `lon` | BD-09 |
| `gLat` / `gLng`、`glat` / `glon` | GCJ-02 |
| 本地内部（路线、围栏、地图） | GCJ-02 |

---

## 2. 端点总表

| 用途 | 路径 | 方法 | 来源 |
|---|---|---|---|
| 极验预检 | `api/v65/security/checkGeeUse` | POST | upstream |
| 极验预处理 | `api/v31/security/login/geepreprocess` | POST | upstream |
| 极验校验 | `api/v70270/security/geevalidate` | POST | upstream |
| 登录 | `api/v70100/login` | POST | upstream |
| 用户信息 | `api/v70100/user/info` | GET | upstream |
| 校区列表 | `api/v61101/campus/zonelist?unid=` | GET | upstream |
| 跑步策略 | `api/v70103/runModePolicy` | POST | upstream |
| 校园打卡点 | `api/v560/get/1/distance/<目标米>` | POST | upstream / PR #106 |
| 校园围栏 | `api/v1/getGeoFenceForRun` | POST | upstream |
| OBS 临时凭证 | `api/obs/temporary/url` | POST | upstream |
| **提交记录** | `api/v70260/runnings/save/record` | POST | upstream |
| 记录列表 | `api/v70230/runnings/records` | POST | upstream |
| 记录详情 | `api/v61200/runnings/get_one_record` | POST | APK |
| 记录详情（回退） | `api/v70260/runnings/get_one_record` | POST | upstream |
| 学期汇总 | `api/v55/runnings/recordssummary/semester` | POST | upstream |
| 个人完成度 | `api/v41/running/getPersonalSemesterInfo` | POST | upstream |
| 违规自查 | `api/v78/cheat/cheatlist` | POST | upstream |
| 主榜 / 历史榜 | `api/v41/rank`、`api/v41/historyRank` | POST | upstream |
| 室内榜 | `api/v43/runnings/indoor/studentRank` | POST | upstream |

> 记录详情先试 `v61200`（APK 里实际用的），失败再试 `v70260`（上游用的）。
> 两条都失败时错误信息会同时带上两边的原因。

---

## 3. 登录链路

```
checkGeeUse
  → geepreprocess（拿 challenge）
  → 本地解极验 v4 滑块（gt4.py）
  → geevalidate（换 geeToken）
  → login（手机号 + 密码）
```

- 设备指纹按 uid 存在 `data/devices/<uid>.json`，下次复用，避免“设备突然变了”。
- 密码错误 → `10175`；账号在别处在线 → `10121`。

---

## 4. 点位与围栏

### 点位

```json
POST api/v560/get/1/distance/2500
{
  "sportType": 4,
  "longitude": 112.94, "latitude": 22.41,
  "sign": "md5(http://host/path + SALT)",
  "uuid": "...",
  "selectedUnid": "47203",
  "runec": "<信封>",
  "runAreaId": "..."        // 可选
}
```

- 路径末尾是**本次目标距离（米）**，不是固定 `1`。真机抓包见上游 PR #106
  （`.../distance/2000`）；写死 `1` 会让服务端只返回最小/固定点位集。
- 限流码 `10603`：命中后优先用 300s 缓存；没有缓存就报错，不硬刷。
- 缓存键：`uid + unid + runAreaId + 锚点(~11m) + 目标距离`。

### 围栏

```json
POST api/v1/getGeoFenceForRun   {}
→ data.geoFences[0].points[]  { glat, glon, pointsNumber }
```

这个校区**只填 `glat/glon`**，不填 BD 坐标。`points.fetch_fence()` 负责兼容并算出中心点。

---

## 5. 提交

### 5.1 实体

- 完整构造：`rundata.build_upload_entity()`
- 字段数：43
- 参与签名：31 个，清单在 `crypto.UPLOAD_SIGN_FIELDS`
- 签名算法：字段排序后拼接，再走 `headerSign`

关键字段：

| 字段 | 单位/取值 | 说明 |
|---|---|---|
| `totalDis` / `validDis` | 米 | 距离 |
| `totalTime` / `validTime` | 秒 | 时长 |
| `startTime` / `stopTime` | 毫秒 | 时间窗；补跑时按所选日期/时间生成（今天..3 天前，09:00–22:00，结束也须 ≤ 22:00） |
| `speed` | 秒/公里 × 1000 | 2.5km/900s → `6000` |
| `avgStepFreq` | 步/分 | 步频 |
| `totalSteps` | 步 | 总步数 |
| `totalAscent` / `totalDescent` | 米 | 爬升/下降 |
| `calorie` | kcal | 热量 |
| `policy` / `selDistance` | — | **取自服务器**的 `runModePolicy` |
| `address` | 城市名 | 如 `江门市`（不是学校名）|
| `sportType` | 1/3 | 3 时才发 `roomId` |
| `allLocJson` | JSON 字符串 | 完整轨迹（见 §6）|

### 5.2 OBS 十个键

轨迹另存 OBS，全部是 `base64(gzip(...))`：

```
rrid, uuid, uid,
run_data, fixed_point_json, segment_json,
speed_json, step_freq_json, laps_json, runFaceCheck
```

- Bucket：`iydsj-hbase-hot`，前缀 `run_data/`
- 上传后**回读校验**（解压后比对点数/首尾），失败会报出来
- `run_data` 内部：`{"allLocJson": "<数组字符串>", "useZip": false}`
- `fivePointJson[].position` 是真机数组下标 `0,1,2,…`（上游 PR #106），
  不是服务端点位字段，也不能用 `999` 占位。
- `speed_json` / `step_freq_json` 的 `queueNum` 是窗内下标，`id` 是本次
  跑步内递增序号；末窗不足 10 秒时距离/步数按实际时长缩放，避免回放
  算出 300+ 步/分。

---

## 6. 记录详情：轨迹怎么解

响应里的值是**多层编码**，少剥一层就只能拿到一串文本：

```
data.run_data           → JSON 字符串
  .allLocJson           → 又是 JSON 字符串
data.fixed_point_json   → JSON 字符串（fivePointJson）
data.segment_json       → JSON 字符串
```

也可能是 `base64(gzip(json))`（App 里有 `decompress_gzip_base64_values`）。
`readonly._unwrap()` 最多剥 4 层，剥不动就原样返回。

轨迹点字段：

| 字段 | 含义 |
|---|---|
| `gLat` / `gLng` | GCJ-02（地图直接用）|
| `lat` / `lng` | BD-09 |
| `bdA` | 海拔（米）|
| `speed` | 原样，**单位未验证**，不做换算 |
| `totalDis` / `totalTime` | 累计距离/时长 |
| `steps` | 累计步数 |
| `gainTime` | 采样时刻字符串 |

> 缺 `gLat/gLng` 时，`readonly.parse_track()` 会把 `lat/lng` 从 BD-09 转成 GCJ-02，
> 并在每个点上标 `crs='bd09->gcj02'`；两者都没有的点直接丢弃。

---

## 7. 只读接口

| 方法 | 返回要点 |
|---|---|
| `readonly.recent_records(limit)` | 列表归一化：公里、配速、日期（北京时间）、complete/status |
| `readonly.record_detail(rrid, uuid)` | 轨迹 + 步频 + 海拔 + 爬升 + 分段 + `fields` |
| `readonly.weekly_summary(weeks)` | 按周（周一~周日）聚合，空周补 0，`truncated` 标明页数用尽 |
| `readonly.check_violations()` | `none_found` / `violations` / `unknown` / `failed` 四态，失败不伪装成“干净” |
| `readonly.semester_summary()` | 学期汇总 |
| `readonly.rank(kind)` | `main` / `history` / `indoor` |
| `readonly.daily_count(client, acct, day)` | 指定北京日期已有几条记录；读不到返回 `(None, 'unknown')`，不伪装成 0 |

> 周汇总**没有服务器端点**，是从记录列表本地聚合的。依据见
> [04-decisions.md](04-decisions.md)。

---

## 8. 错误码

| 码 / 字段 | 含义 | 处理 |
|---|---|---|
| `10000` | 业务成功 | — |
| `10121` | 账号已在其他设备登录 | 等原设备退出或约 1 小时；不要反复试 |
| `10175` | 密码错误 | 检查密码 |
| `10603` | 频次限制 | 用缓存，等 5 分钟 |
| HTTP 401 | token 失效 | 重新登录 |
| `status: false` | 未登录 / 业务失败 | 按失败处理，不要当空数据 |

---

## 9. 本地 bridge（UI → Python）

UI 用 `POST /bridge/<method>`，body 为 `{"args": {...}}`；服务端直接
`fn(**args)` 调用 `ReplicaApi` 的公开方法。与补跑相关的参数：

| 方法 | 新增参数 | 说明 |
|---|---|---|
| `real_preview_model` | `date`、`start_time`、`daily_runs`、`daily_source` | 用所选日期/时间造预览轨迹；`daily_runs` 是 UI 已知的服务器次数 |
| `real_submit_run` | `date`、`start_time` | 用所选日期/时间造数并提交；每日次数在提交前重新查服务器 |
| `real_daily_count` | `date` | 查该北京日期已有几条记录；读不到返回 `source=unknown` |
| `real_daily_counts` | `dates`（可选数组） | 一次返回最近 4 天的次数，供日期日历标注；读不到时 `runs=null` |
| `real_preflight` | `start_ms`、`campus_name`、`school_name` | 显式网络自检；会查询公网 IP，只在 UI 点击时调用 |
| `real_receipts` | `limit` | 列出写前回执（pending/accepted/rejected/unknown） |
| `real_reconcile_receipts` | `uuid`、`limit` | **只读**核对未决回执与服务器记录；匹配到才回写 accepted，绝不重发 |
| `real_semester` | — | 学期汇总 + 个人完成度，返回归一化 `view` 与原始数据 |
| `real_fetch_school_rules` | `run_mode` | 只读 `runModePolicy.runRuleModel`，映射成设置页规则；不猜未返回字段 |
| `real_get_routes` | — | 读取本机 `data/routes.json` 路线库 |
| `real_save_routes` | `routes` | 整体替换并校验路线库，替代仅靠 localStorage |
| `real_corpus` / `real_corpus_collect` / `real_corpus_use` | `limit` / `rrid` | 3 km+ 达标样本的列表、显式只读采集、取轨迹当路线 |
| `real_diagnostics` | — | 生成脱敏诊断包（不含 token/密码/uid/坐标/IP） |

`date` 为 `YYYY-MM-DD`（北京时间），只接受今天..3 天前；`start_time` 为
`HH:MM` 或 `null`（窗口内随机）。CLI 仍可用 `--days-ago N` 作为等价简写。
返回值新增：

```json
{
  "start": {"ms": 1792636200000, "text": "2026-10-22 10:30:00", "days_ago": 1},
  "daily": {"date": "2026-10-22", "runs": 1, "source": "server"},
  "model": {"checks": [{"id": "date"}, {"id": "daily"}]}
}
```

`source` 取值：`server`（记录列表）、`local`（本地回执）、`unknown`（都读不到）。
`model.checks` 里的 `daily` 在未知时是 `warn`，不会静默消失。

---

## 10. 时间窗口证据

| 事实 | 出处 |
|---|---|
| 上游开始时间支持前 3 天 | `README.md`；`src/ui/run.rs`、`src/cli/cmds.rs` 的 `clamp(0, 3)` |
| 服务器计分跑有效时段 06:30–22:00 | `runModePolicy.validTime`；22:00 后返回 `11016` |
| 历史日期是否被服务器接受 | **未验证**；上游 `#64 时间限制根因未确定` |
| 本工具选择的窗口 | 09:00–22:00（更窄）；开始和结束都必须落在窗口内 |
