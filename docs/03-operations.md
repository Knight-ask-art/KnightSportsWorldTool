# 03 · 运行与排错

## 1. 本地文件

默认根目录：`jhp_phase1/data/`（可用 `JHP_DATA_DIR` 改）。

| 路径 | 内容 | 能不能外发 |
|---|---|---|
| `accounts_emu.json` | UI/CLI 保存的会话（uid、token、校区、中心点、keepalive 状态） | **不能** |
| `devices/<uid>.json` | 设备指纹（DeviceId、型号、系统版本…） | **不能** |
| `limits/<uid>.json` | 按日期存的提交计数（北京时间） | 可以 |
| `submission_receipts/<uid>__<unid>.json` | 提交回执（状态、rrid、时间窗） | 可以（不含 token/坐标） |
| `points_cache.json` | 打卡点缓存（300 秒，按目标距离分键） | 可以 |
| `routes.json` | 路线库（手绘/导入/记录生成） | 可以 |
| `students.json` / `sport.json` / `account.json` | 旧 mock / 本地账号密码 | **不能** |
| `../campus_fence_47203.json` | 这个校区围栏的样例（10 点） | 可以 |

> `diagnostics` 模块生成的是**脱敏**报告：不含 token、密码、uid、DeviceId、坐标、IP、
> 原始请求体、校区数字 ID、本地路径。要贴日志/提 issue 时用它。

> **token 保活**：`accounts_emu.json` 的 `keepalive` 里只存间隔、上次检查/成功时间、
> 失败次数、过期标记等；**不存密码**。自动重登的密码只在进程内存里，重启即失效。
> `disabled: true` 的会话不会被探测或重登——这是停用旧账号的硬开关。

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

### 2.5 点位距离与自由跑

- 点位接口末尾是**本次目标距离（米）**，缓存也按距离分开。改距离后点
  「刷新」会按新距离重新拉；不点刷新则沿用当前缓存，避免撞 5 分钟限流。
- **自由跑**只在“学校确实没有打卡点”时使用。勾选后提交不拉点位，但仍检查
  围栏/距离/配速/时间窗；学校策略若有必经点，后端会明确拒绝。

### 2.6 路线库

- 路线存在 `data/routes.json`，不再只靠浏览器 `localStorage`；换端口/换 exe
  不会丢。导入导出是 GPX/GeoJSON/JSON/CSV，纯本地解析。

> 采集与样本库不在产品内：实现与数据都放在 `private/`，默认写入
> `private/data/corpus/`，不参与打包、不注册 bridge、不在界面暴露。

### 2.7 学校要求自动读取

- 登录后若 `data/school_rules.json` 还没有任何规则，UI 自动调用
  `real_fetch_school_rules`，读取 `runModePolicy.runRuleModel` 并保存。
- 只映射有源码证据的字段：`minDistance`（米→km）、`minRunTime/maxRunTime`
  （秒→分钟）、`speedTop/speedBottom`（配速）、`stepTop/stepBottom`（步频）、
  `validTime[0]`（时间窗）。
- `dayGoal`、`weekCountGoal`、`minSteps` 等没有可靠换算的字段只在提示里展示，
  不写入规则；服务器返回多个 `validTime` 时只填第一个并提示。
- 已有规则不会被自动覆盖；「从服务器读取」会先确认，再只覆盖服务器返回的
  字段，其余保留。主动清空并保存会写 `auto_fetch=false`，停止后续自动读取。

---

## 3. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `10121 此账号已在其他设备登录` | 账号保护，另一台设备在线 | 原设备退出，或等约 1 小时；**不要反复试** |
| `10603` / `rate limited` | 点位接口频次限制 | 用缓存；等 5 分钟；别连续拉 |
| `token有误或缓存中token不存在` / HTTP 401 | token 失效 | 重新登录，或在 UI 里更新 token；长期运行可开「token 保活」 |
| `fence error Unauthorized` | 同上 | 围栏会跳过，不影响离线功能 |
| `AMap key missing` | 没配高德 Key | `--amap-key` 或 `JHP_AMAP_KEY`（必须是**Web 服务**类型） |
| `路线没经过 N 个点位` | 画的线离打卡点超过 40 m | 加 `--snap-route`，或重画；确认无影响才用 `--allow-missed-points` |
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
| `网络自检` 显示 IP 城市不一致 | 出口 IP 与校区不同城 | 换手机热点/同城代理；详情页会显示 IP 归属地 |
| `点位拉不全 / 每次都是同样几个点` | 旧版本把点位距离写死为 1 | v0.1.1 已按目标距离拉取；改距离后点「刷新」 |
| `学校策略要求必经点位，不能使用自由跑` | 学校有必经点 | 关闭自由跑，正常拉点位 |
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
cd 好想跑

# 一把梭：23 个模块
python -m jhp_phase1.replica.selfcheck

# 端到端（假客户端；不联网、不提交）
python jhp_phase1/tests/test_pipeline.py

# 协议/加密
python jhp_phase1/tests/test_phase1.py
python jhp_phase1/tests/test_phase2.py
python jhp_phase1/tests/test_netsec.py

# 造数仿真
python jhp_phase1/tools/validate_sim.py

# 单模块
python -m jhp_phase1.replica.readonly
python -m jhp_phase1.replica.routes
python -m jhp_phase1.replica.points
```

期望输出（2026-10-22）：

```
23/23 模块通过（含 tokenkeeper）
43 passed（pytest）
44 passed（pipeline）
38 passed（phase1）
23 passed（phase2）
validate_sim: PASS
```

---

## 6. 诊断报告

```python
from jhp_phase1.replica import diagnostics
print(diagnostics.collect())
```

输出是脱敏 JSON，可以直接贴出来。**不要**把 `data/` 目录、`accounts_emu.json`、
原始请求体发出去。
