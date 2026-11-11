# 10 · 协议链路

> 证据等级：**A**（源码 / 抓包 / `docs/api.md`），单独标注的除外
> 最后更新：2026-11-11 · 对应版本 **0.2.2**

## 1. 全链路

```text
登录
  checkGeeUse → geepreprocess → 本地极验 v4 → geevalidate → login
      ↓ token / uid / unid
点位与围栏
  campus/zonelist → get/1/distance/<米> → getGeoFenceForRun
      ↓
跑步策略
  runModePolicy（operateType=0 取 policy；operateType=1 取 runRuleModel）
      ↓
造数与签名
  build_upload_entity（43 字段，31 个参与签名）
      ↓
OBS
  temporary/url → PUT 11 个键 → GET 回读 + SHA-256
      ↓
提交
  runnings/save/record
      ↓
回查
  records → get_one_record → OBS 轨迹 → 只读对账
```

## 2. 端点总表（常用）

| 用途 | 路径 | 备注 |
|---|---|---|
| 极验预检 | `api/v65/security/checkGeeUse` | POST |
| 极验预处理 | `api/v31/security/login/geepreprocess` | 拿 challenge |
| 极验校验 | `api/v70270/security/geevalidate` | 换 `geeToken` |
| 登录 | `api/v70100/login` | 手机号 + 密码 |
| 服务器时间 | `api/v3/getserver/gmttime` | 进程内时钟校正，不改系统时间 |
| 用户信息 | `api/v70400/user/info` | 404 回退 `v70100` |
| 校区列表 | `api/v61101/campus/zonelist?unid=` | GET |
| 跑步策略 | `api/v70103/runModePolicy` | `operateType=0/1` 返回不同结构 |
| 打卡点 | `api/v560/get/1/distance/<目标米>` | POST，10603 会冷却 |
| 围栏 | `api/v1/getGeoFenceForRun` | POST |
| OBS 凭证 | `api/obs/temporary/url` | POST |
| 提交 | `api/v70260/runnings/save/record` | POST |
| 记录列表 | `api/v70230/runnings/records` | POST |
| 记录详情 | `api/v70320/runnings/get_one_record` | 7.3.70/7.4.0 |
| 详情回退 | `v70260` → `v61200` | 依次回退 |
| 学期汇总 | `api/v55/runnings/recordssummary/semester` | 必须带 `sid` |
| 违规自查 | `api/v78/cheat/cheatlist` | POST |

完整表见 [`../api.md`](../api.md) §2。

## 3. 登录与极验

```text
checkGeeUse
  → geepreprocess（challenge）
  → 本地解极验 v4 滑块（transport/gt4.py）
  → geevalidate（geeToken）
  → login
```

- 极验 `w` 参数里最容易丢的两块：轨迹与时间/距离的一致性、`pow_msg` 相关字段。
  细节见 [`../geetest.md`](../geetest.md)。
- 三种失效要分开：静态资源版本变了 / 字段结构变了 / 风控升级。
- 失败现场落在 `data/diagnostics/gt4/<时间戳>/`，含 `attempt.json`、`bg.png`、
  `slice.png`；**含 `w` 明文，不能外发**。
- 重试策略刻意保守，避免把账号打到风控。
- 无可用 token 时全链路明确报错，不伪装成空数据。

## 4. 点位与围栏

- 点位按目标距离拉，缓存 300 秒；10603 冷却跨重启保留。
- `fivePointJson[].position` 是**真机数组下标**（`0,1,2,…`），不是服务端点位字段，
  也不能用 `999` 占位。
- 围栏来自 `getGeoFenceForRun`；没有围栏/没有打卡点的校区有单独分支。
- 提交前会用服务器最新 `runModePolicy` 刷新有效时段，避免用旧配置。

## 5. 提交体

- 构造：`app/replica/domain/rundata.py::build_upload_entity()`
- 字段数：**43**；参与签名：**31**，清单在
  `app/replica/domain/crypto.py::UPLOAD_SIGN_FIELDS`
- 签名：字段排序后拼接，再走 `headerSign`
- 单位：距离米、时长秒、时间戳毫秒、`speed` 是分钟/公里 ×1000
- `policy` / `selDistance` 取自服务器 `runModePolicy`

**不发送的三个字段**（已确认，不要再补）：

| 字段 | 原因 |
|---|---|
| `prizeName` | 服务端详情页字段，提交路径不设置 |
| `recordUrl` | 服务端生成，提交路径不设置 |
| `segmentJson` | 客户端算完会置空再上传，最终报文就是空串 |

三者都不在 31 个签名字段里，不发不影响入库、签名和完成度。
依据见 [`../api.md`](../api.md) §5.1。

## 6. OBS

11 个键，全部 `base64(gzip(...))`：

```text
rrid, uuid, uid,
run_data, extension_json, fixed_point_json, segment_json,
speed_json, step_freq_json, laps_json, runFaceCheck
```

- Bucket：`iydsj-hbase-hot`，前缀 `run_data/`
- 7.4.0 抓包只有一条 PUT：`run_data/{YYYYMMDDHH}/{uuid}.json`
- 上传后**必须回读校验**：长度 + SHA-256，不一致重试 3 次
- `run_data` 内部：`{"allLocJson": "<数组字符串>", "useZip": false}`
- `speed_json` / `step_freq_json` 的 `queueNum` 每 3 个 10 秒窗（30 s）加 1
- 末窗不足 10 秒时只分配实际时长的距离/步数，OBS 序列化**不得再按
  `window_s / 10` 二次缩放**
- `laps_json[].avgPace` 单位是**分钟/公里**；内部 `km_splits[].pace` 是秒/公里，
  两者不要混用

## 7. 轨迹点

7.4.0 的 `MyLocation` 有 30 个 DB 字段，Gson 实际序列化 **27 键**。

| 字段 | 含义 |
|---|---|
| `gLat` / `gLng` | GCJ-02，7.4.0 的真实坐标 |
| `lat` / `lng` | 7.2/7.3 是 BD-09；7.4.0 写死 `-1.0` 占位 |
| `bdA` | 海拔（米） |
| `speed` | 配速 min/km（越大越慢） |
| `totalDis` / `totalTime` | 累计距离（米）/ 活跃秒 |
| `gainTime` | 墙钟采样时刻字符串 |
| `steps` | 7.4.0 **没有**；读旧记录时兼容 |

- 缺 `gLat/gLng` 时，`parse_track()` 把 `lat/lng` 从 BD-09 转 GCJ-02，并标
  `crs='bd09->gcj02'`
- `lat/lng=-1` 的占位点和两者都没有的点直接丢弃
- 内部统一 GCJ-02

## 8. 记录详情

值是**多层编码**，少剥一层只能拿到文本：

```text
data.run_data          → JSON 字符串
  .allLocJson          → 又是 JSON 字符串
data.fixed_point_json  → JSON 字符串
data.segment_json      → JSON 字符串
```

也可能是 `base64(gzip(json))`；`readonly._unwrap()` 最多剥 4 层。

`jsonFromObs=1` 时，数据库行的 `allLocJson` / `fivePointJson` 是空的，完整轨迹
在 OBS；读取顺序是：新 key → 旧 key → 行内 `allLocJson`。OBS 失败只影响轨迹，
不影响距离/配速/平台检查项。

## 9. 代码位置

| 内容 | 位置 |
|---|---|
| 信封 / 加密 / HTTP | `app/replica/transport/netsec.py`、`realclient.py` |
| 签名原语 | `app/replica/domain/crypto.py` |
| 极验 | `app/replica/transport/gt4.py` |
| 提交体 | `app/replica/domain/rundata.py` |
| OBS | `app/replica/transport/obs.py` |
| 点位/围栏 | `app/replica/application/points.py`、`policy.py` |
| 只读查询 | `app/replica/application/readonly.py` |
| UI 桥 | `app/replica/adapters/bridge_api.py` |

## 10. 未验证 / 风险

- 7.4.0 **iOS** 提交链尚未重新真机验证；新安装默认 Android，需要 iOS 时再设 `JHP_IOS_CHANCE`。
- 官方若再改路由或字段数，`v70400/user/info` 和 27 键轨迹都要重新抓包确认。
- 提交体字段语义只证明与已知证据一致，不证明服务端接受；服务端判定仍以
  真实返回为准。
- 参考项目结论见 [`../reference-projects.md`](../reference-projects.md)；它们不是依赖。
