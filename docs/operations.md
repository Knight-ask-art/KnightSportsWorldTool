# 03 · 运行与排错

## 1. 本地文件

默认根目录：`app/data/`（可用 `JHP_DATA_DIR` 改）。

| 路径 | 内容 | 能不能外发 |
|---|---|---|
| `accounts_emu.json` | 会话（uid、校区、中心点、keepalive 状态）；`token` 由 DPAPI 封存为 `token_enc`，不再明文落盘 | **不能** |
| `device_profile.json` | 本机设备指纹（平台、DeviceId、机型、系统版本、安装/更新时间）；登录和提交共用 | **不能** |
| `device_profile.backup-*.json` | 更换设备指纹前的旧指纹备份（回滚用） | **不能** |
| `device_migration.json` | 旧版指纹迁移的一次性通知（原因、备份文件名、脱敏旧 DeviceId）；登录成功后删除 | 可以（不含完整 DeviceId） |
| `devices/<uid>.json` | CLI 手动 `--device-id` 的本次运行覆盖记录（审计用；不自动生效） | **不能** |
| `limits/<uid>.json` | 按日期存的提交计数（北京时间） | 可以 |
| `submission_receipts/<uid>__<unid>.json` | 提交回执（状态、rrid、时间窗） | 可以（不含 token/坐标） |
| `points_cache.json` | 打卡点缓存（300 秒，按目标距离分键；10603 回退可读到 24 h） | 可以 |
| `points_ratelimit.json` | 点位接口 10603 冷却截止时间（跨重启保留） | 可以 |
| `routes.json` | 路线库（手绘/导入/记录生成） | 可以 |
| `updates/` | 自动更新下载的安装包（已校验 sha256，等待手动解压） | 可以 |
| `diagnostics/gt4/<时间戳>/` | 极验失败现场：`attempt.json`、`bg.png`、`slice.png`；凭证已截断 | **不能**（含 `w` 明文） |
| `cache/gt4/rsa-<版本>.json` | RSA 模数缓存，**仅 `JHP_GT4_RSA=on` 时生成** | 可以 |
| `students.json` / `sport.json` / `account.json` | 旧 mock / 本地账号密码 | **不能** |
| `app/fixtures/campus_fence_47203.json` | 这个校区围栏的样例（10 点） | 可以 |

> `diagnostics` 模块生成的是**脱敏**报告：不含 token、密码、uid、DeviceId、坐标、IP、
> 原始请求体、校区数字 ID、本地路径。要贴日志/提 issue 时用它。

> **token 保活**：`accounts_emu.json` 的 `keepalive` 里只存间隔、上次检查/成功时间、
> 失败次数、过期标记等；**不存密码**。自动重登的密码只在进程内存里，重启即失效。
> 登录页勾选「记住密码」时，密码单独走 Windows DPAPI 加密的 `data/saved_login.json`，
> 不会写进会话、回执或诊断包；取消勾选立即删除。
> `disabled: true` 的会话不会被探测或重登——这是停用旧账号的硬开关。
>
> **设备指纹**：`device_profile.json` 每个 data 目录只生成一次，之后登录、提交、回读
> 都读同一份，不会逐请求漂移。新安装默认 Android；iOS 仍可选，用
> `JHP_IOS_CHANCE=1..100` 开启随机池，或在更换/自定义设备里选 iOS。iOS 7.3.40 已真机验证；
> 当前 App Store 为 7.4.0，提交链需要重新验证。
> **旧版共享指纹不再冻结**：`source=legacy-default` 或 `DeviceId` 等于旧共享常量
> （`afa505…4311`，带 `x86_64`/`isRoot=1`）时，启动会一次性生成唯一、自洽的新指纹，
> 先备份为 `device_profile.backup-*.json`，并强制退出本机登录。旧 token 不会与新设备
> 混用；重新登录后登录/提交/OBS 使用同一份新指纹。唯一但字段不自洽的旧指纹只修
> CPU/root/机型/像素，保留 DeviceId。要手动执行迁移：
> `python -m replica.application.device_profile --data-dir <data> --migrate`。
> 要主动换机仍用 `--rotate`（可选 `--platform android|ios|random`），或在设置页
> 「设备指纹 → 高级功能：更换设备指纹」输入“更换”二次确认；自定义设备在
> 「高级功能：自定义设备」。两种方式都会先备份旧指纹并退出本机登录。
> 回滚时把备份文件复制回 `device_profile.json` 并重启程序。**升级/迁移时复制整个
> `data/` 目录（含 `device_profile.json`）；只复制 `accounts_emu.json` 会被当成旧数据
> 并触发一次迁移。**
> **`data/` 不参与打包；把整个 `data/` 复制给别人等于复制同一台设备。**
>
> **迁移前先自查**：`python -m replica.application.device_profile --data-dir <data>
> --audit`。新版随机池保证机型 / 系统 / 物理像素 / 逻辑像素 / CPU / root 自洽；
> 旧版共享指纹会由 `--migrate` 或启动迁移修复。早期生成器造成的像素错误可用
> `--repair-pixels` 只修这两个字段（保留 DeviceId，先备份）。任何字段修改都会改变
> 指纹，必须由操作者确认后再做。
> **已有账号跨平台切换（Android ↔ iOS）比同平台换机型更显眼**；新安装随机到 iOS
> 是正常分布，但不要把已经提交过的 Android 账号随手换成 iOS。

---

## 2. 三道本地闸门

### 2.1 每日上限

- 默认：**每天 2 次**（`--max-per-day N`，`0`=不限）
- 计数文件：`data/limits/<uid>.json`
- 跨天按**北京时间零点**重置
- 命中时退出码 `4`，消息里会提示 `--no-limit`
- 补跑时按**记录的开始日期**计数（补昨天就占用昨天的额度），不是按提交时刻
- 这是工具自己的闸门；**学校要求的每日上限**另由合格模型检查（见 2.4）

### 2.2 深夜窗口

- 默认：**23:00–06:00 拒绝提交**（`--allow-night` 可关）
- 同样是退出码 `4`

### 2.3 回执（write-ahead）

提交前先写回执，状态四种：

| 状态 | 含义 | 后续 |
|---|---|---|
| `pending` | 请求已发出（或进程中途崩了） | **未知**，不能重提 |
| `accepted` | 服务器返回 10000 + rrid | 完成 |
| `rejected` | 服务器明确拒绝 | 可以手动重试 |
| `unknown` | 传输/解析失败 | 只能用只读接口对账 |

规则：

- **提交永不自动重试**；
- `pending` 会锁住该时段，避免“响应丢了但服务器其实入库了”导致重复；
- 同一数据目录下的多进程用文件锁串行化；
- 回执不含 token、密码、坐标。

> 遇到 `回执拦下`（退出码 3）：先去看记录列表/详情，确认上次到底成没成，
> 不要直接删回执重提。

UI 的「记录 → 提交回执」提供 **只读核对**：按开始时间（±3 分钟）和距离
（±120m 或 8%）匹配最近记录；匹配到才把回执改成 `accepted`，没匹配到保持
未决。它**不会重发、不会补传 OBS**。CLI 仍可用记录列表人工核对。

### 2.4 补跑日期与学校每日上限

**允许的槽位**：最近 3 天（含今天，北京时间日历日），UI 里用日历选具体日期；
开始时间 09:00–22:00，并且**结束时间也要在 22:00 前**；今天的记录不能排到
“现在”之后。越界直接拒绝（退出码 1），不会悄悄改成别的时间。

**每日次数**：合格模型按**所选日期**核对学校要求里的 `max_per_day`：

1. 先查服务器记录列表（`readonly.daily_count`），它也能看到其它设备/官方 App 的记录；
2. 列表读不到时保留 `unknown`：本地回执只用于本地闸门/诊断，不冒充服务器次数；
3. 因此模型会显示 `当日次数 —` 的 **警告**，不会静默当作 0 次。

当所选日期已有次数 **≥ 学校上限** 时，模型 `daily` 检查给 **`warn`**，
文案明确写“已达学校上限……仅提醒，不阻止提交”。它**不会**让模型
`verdict=fail`，也不会让后端拒绝提交；服务器是否把多出来的记录算数，
由学校/服务器决定。UI 里“当日次数”那一行会用琥珀色感叹号提示，
但不会把“提交前检查”整体判为未通过。

> 注意：学校上限是**提醒**；本地 `--max-per-day`（CLI，默认 2）才是工具自己的
> 硬闸门。两者不要混为一谈。

### 2.5 点位与跳过打卡点

- **点位是“按距离随机抽 5 个”**：同一校区 1000/1060/2500 m 三次返回的成员和
  `isFixed` 都不同（2026-10-30 实测）。UI 画线时不再逐 10 m 重拉：3 s 防抖、
  变化 ≥200 m、最多 1 次/5 分钟；手输/快捷/计划填入仍 900 ms 防抖。
- **提交复用地图上显示的那套点**（`points`），避免记录走另一批点；只有 UI
  没有点位时才按 `points_distance_m`/目标距离查询。缓存按距离分键。
- **学校没有围栏**是合法配置：官方客户端在 `geoFences` 为空时直接判定
  `isInArea=true`。工具不再因此拒绝提交；若服务器也没给可用中心，跑步页可以
  把地图拖到学校后点「设为校区」，坐标只保存在本机会话里。
- **学校没有打卡点**默认不报错也不放行：预览显示“无打卡点（需确认）”，
  提交停止；只有显式勾选“跳过打卡点（无点位学校，非官方自由跑）”才继续，
  并在提醒区说明。官方自由跑是 serviceType=3 / sportType=4 的另一条链路，
  本工具尚未实现。
- **多围栏**按账号跑区匹配：`runModePolicy` 返回 `runAreaModels` 和
  `geoFence`；先用当前账号的 `campusName`/`schoolName` 匹配跑区名（A 广软、
  B 广州各自落位），匹配不到才回退官方 `selectedPosition=0`；多跑区时也可用
  下拉框切换，选择按 `uid:unid` 存进 `ui_state.json`，下次登录自动恢复。
  跑区 `fenceIds` 命中的**全部**围栏都保留，任意一条包含轨迹即
  算在区域内；没有跑区数据时用全部围栏。
- **围栏优先信 policy，null 才回退**：`geoFence` 明确返回 `{geoFences: []}`
  视为无围栏；`null`/缺段时回退一次 `getGeoFenceForRun`（冻结账号的 policy
  实测就是这样，而围栏仍存在），结果标记 `legacy-fallback`。
  `runModePolicy` 取不到时直接停止。多围栏且没有手动校区/路线首点可判定时
  也停止，不会挑第一条围栏。

### 2.6 路线库

- 路线存在 `data/routes.json`，不再只靠浏览器 `localStorage`；换端口/换 exe
  不会丢。导入导出是 GPX/GeoJSON/JSON/CSV，纯本地解析。

> 采集与样本库不在产品内：实现与数据都放在 `private/`，默认写入
> `private/data/corpus/`，不参与打包、不注册 bridge、不在界面暴露。

### 2.7 学校要求自动读取

- 登录后若 `data/school_rules.json` 还没有任何规则，UI 自动调用
  `real_fetch_school_rules`，读取 `runModePolicy.runRuleModel` 并保存。
  **`runRuleModel` 只在 `operateType=1`（学期视图）时返回**；开始跑步用的
  `operateType=0` 只返回 `policy`，规则模型是 `null`。旧实现只发 0，所以一直
  “服务器没有返回可用的学校规则”。
- 只映射有源码证据的字段：`minDistance`（米→km）、`minRunTime/maxRunTime`
  （秒→分钟）、`speedTop/speedBottom`（配速）、`stepTop/stepBottom`（步频）、
  `validTime[0]`（时间窗）。
- `dayGoal`、`weekCountGoal`、`minSteps` 等没有可靠换算的字段只在提示里展示，
  不写入规则；服务器返回多个 `validTime` 时只填第一个并提示。
- 已有规则不会被自动覆盖；「从服务器读取」会先确认，再只覆盖服务器返回的
  字段，其余保留。主动清空并保存会写 `auto_fetch=false`，停止后续自动读取。

### 2.8 个人主页与违规自查

- `real_profile` 先读 `api/v70400/user/info`（旧 `v70100` 仅在 404 时回退），再读学期汇总；任一半失败都会
  返回 `errors`，不会把失败伪装成 0。
- `real_violations` 只读 `api/v78/cheat/cheatlist`，不重试、不写任何文件。
  返回状态是显式的：`none_found`（明确返回空）、`violations`（有记录）、
  `unknown`（响应缺少 `self` 字段）、`failed`（网络/解密失败）。
- UI 只在点击「查询违规」时调用；`unknown` 和 `failed` 都按异常颜色显示，
  不会说“没有违规”。结果带 `checked_at`，超过 `STALE_AFTER_S` 标“已过期”。

---

## 3. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `10121 此账号已在其他设备登录` | 账号保护，另一台设备在线 | 原设备退出，或等约 1 小时；**不要反复试** |
| `10603` / `rate limited` | 点位接口频次限制 | 按 `uid\|unid` 记录 5 分钟冷却；精确距离缓存 → 同账号/跑区任意距离（24 h）；UI 标注“缓存” |
| `token有误或缓存中token不存在` / HTTP 401 | token 失效 | 重新登录，或在 UI 里更新 token；长期运行可开「token 保活」 |
| `fence error Unauthorized` | 同上 | 围栏会跳过，不影响离线功能 |
| `AMap key missing` | 没配高德 Key | `--amap-key` 或 `JHP_AMAP_KEY`（必须是**Web 服务**类型） |
| `路线没经过 N 个点位` | 画的线离打卡点超过 25 m | 加 `--snap-route`，或重画；确认无影响才用 `--allow-missed-points` |
| `账号处于跑步冻结期` | `freezeRunTime > 0` | 只提醒、不阻止；官方客户端此时不能开始跑步，建议等解冻 |
| `学校启用了人脸抽检` | `faceVerify` / `runFaceCheckConfigDTO.enable` | 工具无法完成人脸校验，会停止提交；不要伪造 `faceCheck=1` |
| `无法获取校区围栏，且本地没有已保存的校区坐标` | 围栏请求超时/解析失败，且从未保存过校区 | 重试；没有围栏的学校可先「设为校区」再提交 |
| `路线周长只有 Xm` | 路线太短，闭合后不足 80 m | 画大一点 |
| `路线太短：铺满 X km 要 N 圈（上限 30）` | 圈数太多会像“原地抖笔” | 画长一点，或减 `--km` |
| `路线有 N/M 个点在围栏外` | 画出校区了 | 只告警不拦截；但记录很可能无效 |
| `造数校验未通过: ['geometry ...']` | 数据自检失败 | 记下来报问题；不要带病提交 |
| `回执拦下: ...`（退出码 3） | 该时段已有 pending/unknown | 先用只读接口对账 |
| `每天最多 N 次`（退出码 4） | 每日上限 | 等明天，或 `--max-per-day` / `--no-limit` |
| `23:00–06:00`（退出码 4） | 深夜窗口 | `--allow-night` / `--no-limit` |
| `开始时间无效: 必须在 09:00–22:00` | 补跑窗口外 | 改时间；结束也不能超过 22:00 |
| `今天 09:00–22:00 已经放不下` | 今天剩余时间不够或还没到 09:00 | 改昨天/更早，或缩短距离 |
| `当日次数 ... 已达学校上限 ... 仅提醒` | 所选日期已达学校上限 | 只是提醒；要换一天也可以，不换也能提交 |
| `网络诊断` 显示 IP 城市不一致 | 出口 IP 与校区不同城（仅提醒，不挡提交） | 想更接近手机网络可换热点/同城代理；详情页会显示 IP 归属地 |
| `点位拉不全 / 每次都是同样几个点` | 旧版本把点位距离写死为 1 | UI 已固定按校区范围拉取；改目标距离不需要刷新。仍缺时等 5 分钟限流后点「刷新」 |
| `学校策略要求必经点位，不能跳过打卡点` | 学校有必经点 | 取消“跳过打卡点”，正常拉点位 |
| `bad args: ...` | 桥接调用参数不对 | 检查 `/bridge/<method>` 的 args |
| UI 502 / 打不开 | `run.py` 没启动或端口冲突 | 换端口：`--port 65521` |

---

## 4. 只读对账

提交结果不确定时，**只读**接口是安全的：

```powershell
# 最近记录（北京时间、公里、配速）
# UI：记录（只读）→ 最近记录

# 单条详情：轨迹 / 步频 / 海拔 / 爬升
# UI：点记录列表里的任意一条
```

命令行没有单独的只读入口，直接在 UI 里点即可；底层桥接：

```
POST /bridge/real_recent_records   {"args": {"limit": 20}}
POST /bridge/real_record_detail    {"args": {"rrid": 12345, "uuid": "..."}}
POST /bridge/real_weekly_summary   {"args": {"weeks": 4}}
```

---

## 5. 回归与自检

```powershell
cd 好想跑/app

# 一把梭：22 个模块
python -m replica.selfcheck

# 主测试套件
python -m pytest tests -q

# 协议/加密历史运行器（不进入 pytest）
python tests/legacy/legacy_bridge_suite.py
python tests/legacy/legacy_http_suite.py

# 私有采集/样本库（不属于产品）
python -m pytest ../private/research/tests -q

# 造数仿真
python tools/validate_sim.py

# 单模块
python -m replica.readonly
python -m replica.routes
python -m replica.points
```

期望输出（2026-10-25）：

```
28/28 模块通过（含 tokenkeeper）
236 passed（pytest tests/）
3 passed（private/research/tests）
38 passed（legacy_bridge_suite）
23 passed（legacy_http_suite）
validate_sim: PASS
```

---

## 6. 诊断报告

```python
from app.replica import diagnostics
print(diagnostics.collect())
```

输出是脱敏 JSON，可以直接贴出来。**不要**把 `data/` 目录、`accounts_emu.json`、
原始请求体发出去。
