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
| 轨迹点 `lat` / `lng` | 7.2/7.3 是 BD-09；7.4.0 是 `-1.0` 占位 |
| `fivePoint` / 围栏的 `lat` / `lon` | BD-09（7.4.0 仍保留） |
| `gLat` / `gLng`、`glat` / `glon` | GCJ-02（真实轨迹坐标） |
| 本地内部（路线、围栏、地图） | GCJ-02 |

---

## 2. 端点总表

| 用途 | 路径 | 方法 | 来源 |
|---|---|---|---|
| 极验预检 | `api/v65/security/checkGeeUse` | POST | upstream |
| 极验预处理 | `api/v31/security/login/geepreprocess` | POST | upstream |
| 极验校验 | `api/v70270/security/geevalidate` | POST | upstream |
| 登录 | `api/v70100/login` | POST | upstream |
| 服务器时间 | `api/v3/getserver/gmttime` | GET | Inverx 7.3.92 |
| 用户信息 | `api/v70400/user/info`（7.4.0 起；404 时回退 `v70100`） | GET | 7.4.0 dex |
| 校区列表 | `api/v61101/campus/zonelist?unid=` | GET | upstream |
| 跑步策略 | `api/v70103/runModePolicy` | POST | upstream |
| 校园打卡点 | `api/v560/get/1/distance/<目标米>` | POST | upstream / PR #106 |
| 校园围栏 | `api/v1/getGeoFenceForRun` | POST | upstream |
| OBS 临时凭证 | `api/obs/temporary/url` | POST | upstream |
| **提交记录** | `api/v70260/runnings/save/record` | POST | upstream |
| 记录列表 | `api/v70230/runnings/records` | POST | upstream |
| 记录详情 | `api/v70320/runnings/get_one_record` | POST | 7.4.0 dex 字符串池（7.3.70 起） |
| 记录详情（旧版） | `api/v70260/runnings/get_one_record` | POST | upstream / 7.2.x |
| 记录详情（最旧） | `api/v61200/runnings/get_one_record` | POST | 早期 APK |
| 学期汇总 | `api/v55/runnings/recordssummary/semester` | POST | upstream |
| 个人完成度 | `api/v41/running/getPersonalSemesterInfo` | POST | upstream |
| 违规自查 | `api/v78/cheat/cheatlist` | POST | upstream |
| 主榜 / 历史榜 | `api/v41/rank`、`api/v41/historyRank` | POST | upstream |
| 室内榜 | `api/v43/runnings/indoor/studentRank` | POST | upstream |

> `runModePolicy` 的 `operateType`：`0` = 开始跑步，只返回 `policy`；`1` = 学期
> 视图，才返回 `runRuleModel`。设置页「从服务器读取」必须用 `operateType=1`。
>
> 记录详情按 `v70320`（7.3.70/7.4.0 字符串池）→ `v70260`（7.2.x 与上游）
> → `v61200`（早期 APK）依次回退；全部失败时错误信息会同时带上三边的原因。
> 7.4.0 与 7.3.70 的路由没有变化（`PROTO_DIFF_7.4.0`），所以 `v70320` 是第一优先。
>
> `api/v3/getserver/gmttime` 返回服务端毫秒时间戳。工具用请求往返的中点估算
> 偏移，把它应用在**进程内**（`replica.domain.clock`）：生成的轨迹时间、报文
> `timeStamp`、`tokenSign` 和页面显示都走校正后的时钟。响应体为空/被拦截时
> 回退到同一响应的 HTTP `Date` 头（秒级，足够）。不会修改系统时间；登录前
> 未校准时按本机时钟，提交前会强制再校准一次。
>
> 学期汇总必须带 `{"sid": <getPersonalSemesterInfo 返回的 sid>}`。空 body 不会报错，
> 而是返回 `sname: ""`、所有计数为 0 的空记录；早期版本因此把有 10.24 km 的账号
> 显示成 0.0%。
>
> `goalMethod`：`0` = 户外距离目标（米），`1` = 户外次数目标，
> `2` = **室内锻炼**（`semesterGoal`/`semesterValid` 单位是“次”，官方 App 作为
> 独立圆环分段显示）。`2` 不是户外学期目标，不能拿来当整张学期卡的百分比。
>
> 服务器没有户外目标时，程序会用本机 `data/semester_goal.json` 里的
> `target_km` 计算百分比和“还差”，并在卡片上标注“本机目标”。该值和 `sid`
> 绑定，换学期自动失效；服务器有目标时永远以服务器为准。
>
> 详情返回 `jsonFromObs=1` 时，数据库行里的 `allLocJson` / `fivePointJson` 是空的；
> 完整轨迹在 OBS 对象 `run_data/{YYYYMMDDHH}/{uuid}.json`（7.4.0 抓包只有这一条
> PUT）。详情接口会先读该 key，再退回旧的 `run_data/{rrid//1000000}/{rrid}.json`，
> 然后解析 `run_data.allLocJson`；OBS 读取失败只影响轨迹，不影响距离/配速/平台检查项。

---

## 3. 登录链路

```
checkGeeUse
  → geepreprocess（拿 challenge）
  → 本地解极验 v4 滑块（gt4.py）
  → geevalidate（换 geeToken）
  → login（手机号 + 密码）
```

- 设备指纹每个 `data` 目录生成一次，保存在 `data/device_profile.json`；登录和提交
  共用这一份。新安装默认 Android；`JHP_IOS_CHANCE` 可开启 iOS 随机池（默认 0，100 强制 iOS）；
  iOS 7.3.40 已真机验证（2026-10-25，rrid `1344757460`，`status=0` / `isCheat=0`）；
  当前 App Store 为 7.4.0，7.4.0 iOS 提交/OBS 仍需重新验证。
  旧版共享指纹（`source=legacy-default` 或旧常量 DeviceId）会在启动时迁移为唯一、
  自洽的新指纹，先备份到 `device_profile.backup-*.json` 并强制退出本机登录；
  唯一但字段不自洽的旧指纹只修字段、保留 DeviceId。CLI 的
  `--device-id` 只对本次运行做手动覆盖，并记录到 `data/devices/<uid>.json` 供审计；
  下次不自动套用。
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
  "selectedUnid": 47203,
  "runec": "<信封>",
  "runAreaId": 22        // 当前跑区 id；没有跑区时 -1
}
```

- `selectedUnid` 是 **int**，不是字符串：反编译
  `CommonApiManager$requestFivePoint$1` 的字段是 `int $unid`，直接
  `jSONObject.put("selectedUnid", this.$unid)`。旧抓包里的字符串只是当时的
  写法，不能作为类型依据。
- `runAreaId` 不是可选：官方 `requestFivePoint` 总是发送，默认 `-1`。
  它来自 `runModePolicy` 的 `runAreaModels[].id`，和围栏的
  `runAreaModels[].fenceIds` 是同一套账号跑区数据。

- 路径末尾是**服务端查询距离（米）**，不是固定 `1`。真机抓包见上游 PR #106
  （`.../distance/2000`）；写死 `1` 会让服务端只返回最小/固定点位集。
- **UI 按当前「目标距离」查询**（`distance_m`），缓存键包含距离。
- **0.1.7 实测（2026-10-30，同一账号/同一锚点）**：`1000 / 1060 / 2500` 三次
  都返回 5 个点，但成员不同（池子至少 7 个），`isFixed=1` 的点也不同。结论：
  距离影响候选点，**每次请求随机抽 5 个**，不是稳定的校区清单。
- 因此画线自动改距离时**不再逐 10 m 重拉**：3 s 防抖、变化 ≥200 m、最多
  1 次/5 分钟；手动输入/快捷距离/计划填入仍按 900 ms 防抖。
- 点位响应里带 `hit_radius_m`（当前 `25.0`，来自 `domain.routes.HIT_RADIUS_M`）。
  官方 `RunSeqLocationManager` 的判定是 `distance <= 25.0`，前端检查表只读这个值，
  不再自己写 40 m。
- **提交复用地图上已经显示的那套点**（`points` 参数），不再按当前 km 重新抽；
  `points_distance_m` 只作为 UI 没有点位时的查询兜底。
- 限流码 `10603`：写入 5 分钟冷却（`data/points_ratelimit.json`），冷却期内
  不发请求；优先精确距离缓存，其次同账号/跑区**任意距离**的最新缓存（24 h），
  返回 `points_stale=true` 与来源距离，UI/提交 note 明确标注。
- 缓存键：`uid + unid + runAreaId + 锚点(~11m) + 查询距离`。
- **必经点看 `isFixed`**：官方 `RunRanLocationManager.passPoint()` 在
  `isFixed == 1` 时把点位记为 type 2（必经），本账号点位只下发 `isFixed`，
  没有 `isMustPoint`。工具两者都保留，并由 `isFixed` 推导必经点。
- **命中半径 25 m**：官方随机跑、顺序跑、`FivePointPlugin` 都是
  `distance <= 25.0` 才算通过；工具的路点吸附/`isPass`/漏点检查统一为
  25 m（吸附余量 20 m）。
- **顺序跑按编号排序**：`policy == 1` 时按 `position` 或 `pointName` 中的
  数字排序（服务端数组可能无序，实测 `点位2/3/1/7/4`），
  `fivePoint.position` 与轨迹顺序一致。

### 围栏

```json
POST api/v1/getGeoFenceForRun   {"geoFenceUpdateTime": 0}
→ data.geoFences[].points[]  { glat, glon, pointsNumber }
```

> `geoFences` 可能有多条（多校区）。官方不从 `fences[0]` 猜：`runModePolicy`
> 的 `runAreaModels[].fenceIds` 才是账号跑区对应的围栏 id。官方选跑区用
> `SportTypeViewModel.selectedPosition`（默认 0），不是 `RunAreaModels.select`
> ——后者在 1.3.0 反编译源码里没有调用。工具为了多账号自动落位，先用当前
> 账号的 `campusName`/`schoolName` 匹配 `runAreaModels[].name`（A 广软、
> B 广州各自匹配），匹配不到才回退 `runAreaModels[0]`；多跑区时也可用下拉框
> 切换。`fenceIds` 命中的全部围栏都保留，任意一条包含轨迹即在内。没有跑区
> 数据时使用全部围栏。`geoFences` 为空表示学校没有配置围栏，官方客户端直接
> 视为「在区域内」。
>
> 多跑区时跑步页会显示跑区下拉框（对应官方 `RunningAreaDialog`），选择后
> 以 `run_area_id` 重拉围栏/点位，提交带同一个 id。`valid == false` 的跑区
> 显示为灰色不可选；默认跑区无效时提交停止并提示重新选择。缺 `valid` 字段
> 按“未知”处理。`runModePolicy` 只采用一个完整回复，不把两个 `runMode` 的
> 跑区/围栏拼接。
>
> 围栏优先信 `runModePolicy` 的 `geoFence`。它明确返回 `{geoFences: []}` 时
> 视为无围栏；返回 `null`/缺段时不直接判“无围栏”，而是回退一次
> `getGeoFenceForRun`（实测冻结账号的 policy 就是这样，但该校围栏仍然存在）。
> 回退结果的 `source` 为 `legacy-fallback`。两者都取不到才按无围栏处理；
> `runModePolicy` 本身请求失败时停止提交。

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
| `speed` | 分钟/公里 × 1000 | 2.5km/900s → `6000`（即 6'00"；官方 `setSpeed` 存分钟×1000） |
| `avgStepFreq` | 步/分 | 步频 |
| `totalSteps` | 步 | 总步数 |
| `totalAscent` / `totalDescent` | 米 | 爬升/下降 |
| `calorie` | kcal | 热量 |
| `policy` / `selDistance` | — | **取自服务器**的 `runModePolicy` |
| `address` | 城市名 | 如 `江门市`（不是学校名）|
| `sportType` | 1/3 | 3 时才发 `roomId` |
| `allLocJson` | JSON 字符串 | 完整轨迹（见 §6）|

**不发送的三个字段**（已确认，不要再补）：

| 字段 | 是什么 | 为什么不上传 |
|---|---|---|
| `prizeName` | 奖品名称，详情页显示奖品图标 | 服务端 `RunHistoryBean` / `PrizeListBean` 字段。客户端只在 `RunHistoryDetailPImpl` 回显时 `setPrizeName`，**提交路径不设置** |
| `recordUrl` | 记录分享链接 | 同上，服务端生成、详情页展示；提交路径不设置 |
| `segmentJson` | 分段分析（每段步频/配速/合格状态） | `CreditRunPIml` 提交前用它计算 `useMobilityTools` / `status`，但 `UploadFormatEntity.setSegmentJson()` 算完会把 `segmentJson` 置为 `""` 再上传（L494）；最终报文里就是空串。我们不发 = 服务端 null = 空串 |

三者都不在 31 个签名字段里。不发不影响入库、签名、完成度，只影响详情页的奖品/分享/分段展示。OBS 里的 `segment_json` 是另一个对象（见 §5.2），不受影响。

依据：`UploadFormatEntity.java` L462–496、`CreditRunPIml.java` L396、`RunHistoryDetailPImpl.java` L1066/L1069/L1145；Rust 参考 `src/api/submit.rs` 也没有这三个字段。

### 5.2 OBS 十一个键

轨迹另存 OBS，全部是 `base64(gzip(...))`：

```
rrid, uuid, uid,
run_data, extension_json, fixed_point_json, segment_json,
speed_json, step_freq_json, laps_json, runFaceCheck
```

> 7.4.0 抓包是 **11 个键**；`extension_json` 和 `segment_json` 解码都是空串。
> 少发 `extension_json` 本身就是 schema 差异，值空也必须带上。

- Bucket：`iydsj-hbase-hot`，前缀 `run_data/`
- 上传后**回读校验**：重新签 GET，比对长度 + **SHA-256**；不一致重试 3 次并报错；
  提交结果里 `uploaded` 是 PUT 成功数、`verified` 是哈希一致数
- `run_data` 内部：`{"allLocJson": "<数组字符串>", "useZip": false}`
- `fivePointJson[].position` 是真机数组下标 `0,1,2,…`（上游 PR #106），
  不是服务端点位字段，也不能用 `999` 占位。
- `speed_json` / `step_freq_json` 的 `queueNum` 从 0 开始，**每 3 个 10 秒窗
  （30 s）加 1**；`id` 是本次跑步内递增序号。真实 4 km 记录的窗口队列是
  `0,0,0,1,1,1,…`，点队列也是 30 s 一批。末窗不足 10 秒时，生成器只分配
  该窗口实际时长对应的距离/步数；OBS 序列化**不得再按 `window_s / 10`
  二次缩放**，否则窗口合计会比 `juli` / `totalSteps` 少一截。
- `laps_json[].avgPace` 的单位是**分钟/公里**：官方 `RunLapsFullAdapter`
  用 `(int)(avgPace * 60)` 格式化成 `i'rr"`。写秒会让手机把 6'00" 显示成
  360'00"。工具内部 `km_splits[].pace` 才是秒/公里，两者不要混用。

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

轨迹点字段（7.4.0 是 27 个）：

| 字段 | 含义 |
|---|---|
| `gLat` / `gLng` | GCJ-02（地图直接用）；7.4.0 的真实坐标在这里 |
| `lat` / `lng` | 7.2/7.3 是 BD-09；7.4.0 构造器写死 `-1.0` 占位 |
| `bdA` | 海拔（米）|
| `speed` | **配速 min/km**（越大越慢）；`avgSpeed` / `bdS` 才是 m/s |
| `totalDis` / `totalTime` | 累计距离（米）/ 活跃秒 |
| `gainTime` | 墙钟采样时刻字符串；与 `totalTime` 不是同一个时钟 |
| `steps` | 7.4.0 **没有**这个字段；旧版记录也没有，只有第三方实现写过 |

> 7.4.0 的 `MyLocation` 有 30 个字段，`im/mp/ra` 是 DB-only，序列化后正好 27 个。
> 读旧记录时 `parse_track()` 仍兼容 `steps`，但不能把它当成 7.4.0 的必需字段。

> 缺 `gLat/gLng` 时，`readonly.parse_track()` 会把 `lat/lng` 从 BD-09 转成 GCJ-02，
> 并在每个点上标 `crs='bd09->gcj02'`；`lat/lng=-1` 的 7.4.0 占位点和两者都没有的
> 点直接丢弃，不能把 -1 当 BD 坐标换算。

---

## 7. 只读接口

| 方法 | 返回要点 |
|---|---|
| `readonly.recent_records(limit)` | 列表归一化：公里、配速、日期（北京时间）、complete/status |
| `readonly.record_detail(rrid, uuid)` | 轨迹 + 步频 + 海拔 + 爬升 + 分段 + `fields`；`detail` 另含风险字段（见下） |
| `readonly.weekly_summary(weeks)` | 按周（周一~周日）聚合，空周补 0，`truncated` 标明页数用尽 |
| `readonly.check_violations()` | `none_found` / `violations` / `unknown` / `failed` 四态，失败不伪装成“干净” |
| `readonly.semester_summary(client, acct, sid)` | 学期汇总；不带 `sid` 只会拿到空记录 |
| `readonly.rank(kind)` | `main` / `history` / `indoor` |
| `readonly.daily_count(client, acct, day)` | 指定北京日期已有几条记录；读不到返回 `(None, 'unknown')`，不伪装成 0 |

> 周汇总**没有服务器端点**，是从记录列表本地聚合的。依据见
> [decisions.md](decisions.md)。

`record_detail` 的 `detail` 原样暴露服务器的反作弊字段，便于在提交后自查：

| 字段 | 含义 |
|---|---|
| `is_cheat` / `policy` / `step_freq_status` | 服务器判定标记；任一为 1 都会在详情页标红 |
| `pace_fast` / `pace_slow` | `speedTop` / `speedBottom`，单位是 min/km，不是 m/s |
| `step_min` / `step_max` | `stepBottom` / `stepTop`，每分钟步频边界 |
| `appeal_status` / `appeal_info` | 申诉状态与说明；无申诉时 `appeal_info` 为空 |
| `json_from_obs` / `obs_error` | 轨迹是否来自 OBS；OBS 读取失败时 `obs_error` 非空 |

> `stats.has_obs_track` 与 `detail.json_from_obs` 一起用于详情页的「轨迹来源」；
> v0.1.2 起显示 `OBS · N 点` / `记录内联 · N 点`，OBS 读取失败会标红。

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
| `real_preview_model` | `date`、`start_time`、`daily_runs`、`daily_source`、`pause_profile`、`gps_profile`、`terrain_coupling` | 用所选日期/时间造预览轨迹；`daily_runs` 是 UI 已知的服务器次数；返回 `param_warnings` |
| `real_submit_run` | `date`、`start_time`、`pause_profile`、`gps_profile`、`terrain_coupling` | 用所选日期/时间造数并提交；每日次数在提交前重新查服务器；返回 `param_warnings` |
| `real_audit_run` | `rrid`/`uuid` 或预览参数 | 对预览参数现场生成并审计、上一次生成数据、或一条既有记录做 7.4.0 形状审计；生成路径离线，记录路径只读 |
| `real_explain_error` | `code`、`message`、`context` | 纯函数错误解释，返回 `title/cause/action/retryable` |
| `real_verify_record` | `rrid`/`uuid` | 只读核验提交结果，返回 `verdict` 与逐项时间线；绝不重提 |
| `real_obs_key_mode` | `mode`、`pause_profile`、`gps_profile`、`terrain_coupling`（均可选） | 读取/设置 OBS key 模式（`single` 默认 / `legacy` 双 key）与暂停/GPS/坡度档案；写 `data/settings.json` |
| `real_presets` | `action`、`name`、`data` | 命名预设的 `list/save/delete/get`，存 `data/presets.json` |
| `real_stats` | `weeks` | 本地回执 + 只读记录统计；读不到服务器时明确 `source=local` |
| `real_export_fit` | `rrid`/`uuid` | 从一条记录生成 FIT，返回 `filename`/`mime`/`data_b64` |
| `real_diagnostics_zip` | — | 生成脱敏 ZIP（诊断、审计、时钟、设置、回执） |
| `real_inspection` | `freeze_seconds`（可选） | 只读巡检：token、冻结剩余、今日次数、最近记录、时钟、最近回执；不提交 |
| `real_daily_count` | `date` | 查该北京日期已有几条记录；读不到返回 `source=unknown` |
| `real_daily_counts` | `dates`（可选数组） | 一次返回最近 4 天的次数，供日期日历标注；读不到时 `runs=null` |
| `real_preflight` | `start_ms`、`campus_name`、`school_name` | 显式网络诊断（仅提醒）；会查询公网 IP 与服务器时间，只在 UI 点击时调用 |
| `real_receipts` | `limit` | 列出写前回执（pending/accepted/rejected/unknown） |
| `real_reconcile_receipts` | `uuid`、`limit` | **只读**核对未决回执与服务器记录；匹配到才回写 accepted，绝不重发 |
| `real_semester` | — | 学期汇总 + 个人完成度，返回归一化 `view` 与原始数据。服务器没有户外目标时，回落本机设置的目标（`goal_source=local`） |
| `real_plan` | `days`、`seed`、`km_min`、`km_max` | 未来跑步计划（离线生成）；`days` 限制在 1–60。UI 传学期剩余天数（最多 14），不会排到学期结束之后 |
| `get_semester_goal` | `sid`（可选） | 读取本机户外学期目标；换了学期（sid 不同）会返回未设置 |
| `save_semester_goal` | `target_km`、`sid`（可选） | 保存/清除本机户外学期目标；只写本机，不上传 |
| `real_profile` | — | 合并 `user/info` 资料与学期进度；`user/info` 失败时回落本地会话并返回 `errors` |
| `real_violations` | `page`、`page_size` | **只读** `v78/cheat/cheatlist`；返回 `state`（`none_found`/`violations`/`unknown`/`failed`）、`records`、`stale` |
| `real_fetch_school_rules` | `run_mode` | 只读 `runModePolicy.runRuleModel`，映射成设置页规则；不猜未返回字段 |
| `real_check_update` | — | 读取 `latest.json`（GitHub raw，失败回退 jsDelivr）；返回 `state`（`available`/`current`/`ahead`/`failed`）、`latest`、`notes`、`url`、`urls`（全部镜像）、`sha256`、`size` |
| `real_download_update` | `url`、`urls`、`sha256`、`size`、`file` | 按实测速度排序 `urls` 后下载，逐个回退；校验大小与 sha256 |
| `real_apply_update` | `file`（可选） | 解压到 `staging/`，由分离的 PowerShell 脚本在本进程退出后覆盖安装目录（跳过 `data/`）并重启 |
| `real_quit_app` | — | 约 1.2 秒后退出，让更新脚本接管 |
| `real_open_updates` | — | 打开 `data/updates/` 目录（自动更新失败时排查用） |

> `real_preview_model` / `real_submit_run` 的 `param_warnings` 是产品级黄色提醒：
> 配速 <4.5 或 >8.5 min/km、步频 <150 或 >180 spm 时各一条，**不阻止提交**。
> 2026-10-25 已验证的 9'40"/117 spm 记录就在提醒区，服务器仍判为正常。
| `get_saved_login` | — | 读取登录页「记住密码」的 DPAPI 密文；没有则 `saved=false` |
| `save_login` | `account`、`password`、`remember` | 勾选时用 Windows DPAPI 加密保存；`remember=false` 立即删除 |
| `clear_saved_login` | — | 删除 `data/saved_login.json` 和旧版明文 `account.json` |
| `real_get_routes` | — | 读取本机 `data/routes.json` 路线库 |
| `real_save_routes` | `routes` | 整体替换并校验路线库，替代仅靠 localStorage |
| `real_diagnostics` | — | 生成脱敏诊断包（不含 token/密码/uid/坐标/IP） |
| `real_get_session` | — | 返回脱敏会话；`device_platform`、`device_name`、`device_os`、`device_source` 是本机实际生效的设备指纹，`device_id_masked` 只露首尾 |
| `real_get_device_options` | — | 返回示例 Android/iOS 机型（含默认像素/CPU）和当前生效的设备摘要；自定义设备不再要求从这里取值 |
| `real_rotate_device` | `confirm`, `platform` | 高级：输入 `更换设备指纹` 才执行；`platform` 为 `random`（默认）/ `android` / `ios`。先退出本机登录，再备份旧指纹并生成新指纹；返回 `device`、`backup`、`relogin_required` |
| `real_set_custom_device` | `confirm`, `platform`, `deviceName`, `osVersion`, `physicPixel`, `logicPixel`, `cpuModel`, `DeviceId`, `installDays` | 高级：输入 `自定义设备` 才执行。机型/系统/双分辨率/CPU 自由填写；后端按平台检查竖屏、比例和 iOS 2x/3x。先校验再退出本机登录，备份旧指纹后写入 `source=custom`。返回 `device`、`backup`、`relogin_required` |

`device_source`：`generated`（本机随机）、`migrated`（旧版共享指纹已迁移）、
`repaired`（旧版字段已修正）、`legacy-session`（旧会话但指纹可用）、
`rotated`（已更换）、`custom`（自定义）、`session`（手动覆盖）、`error`（指纹文件损坏）。
`device_platform`：`android` 或 `ios`。

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
