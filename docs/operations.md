# 03 · 运行与排错

## 1. 本地文件

默认根目录：`app/data/`（可用 `JHP_DATA_DIR` 改）。

| 路径 | 内容 | 能不能外发 |
|---|---|---|
| `accounts_emu.json` | UI/CLI 保存的会话（uid、token、校区、中心点、keepalive 状态） | **不能** |
| `device_profile.json` | 本机设备指纹（平台、DeviceId、机型、系统版本、安装/更新时间）；登录和提交共用 | **不能** |
| `device_profile.backup-*.json` | 更换设备指纹前的旧指纹备份（回滚用） | **不能** |
| `devices/<uid>.json` | CLI 手动 `--device-id` 的本次运行覆盖记录（审计用；不自动生效） | **不能** |
| `limits/<uid>.json` | 按日期存的提交计数（北京时间） | 可以 |
| `submission_receipts/<uid>__<unid>.json` | 提交回执（状态、rrid、时间窗） | 可以（不含 token/坐标） |
| `points_cache.json` | 打卡点缓存（300 秒，按目标距离分键） | 可以 |
| `routes.json` | 路线库（手绘/导入/记录生成） | 可以 |
| `updates/` | 自动更新下载的安装包（已校验 sha256，等待手动解压） | 可以 |
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
> 都读同一份，不会逐请求漂移。新安装从 Android / iOS 随机池里抽一份；iOS 占比由
> `JHP_IOS_CHANCE` 控制（默认 30，`0` 关闭）。iOS 登录头已接入，但提交链没有真实
> 跑通验证，生产上要保守就设 `JHP_IOS_CHANCE=0`。旧数据首次升级时冻结原来的提交
> 指纹（`source=legacy-*`），不会静默换机；要更换必须显式执行
> `python -m replica.application.device_profile --data-dir <data> --rotate`
> （可选 `--platform android|ios|random`），或在账号页「高级：更换设备指纹」选择
> 平台后输入“更换”二次确认；两种方式都会先备份旧指纹并退出本机登录。旧指纹备份到
> `device_profile.backup-*.json`。回滚时把备份文件复制回 `device_profile.json` 并
> 重启程序。**升级/迁移时复制整个 `data/` 目录（含 `device_profile.json`）；只复制
> `accounts_emu.json` 会被当成旧数据并冻结旧指纹。**
> **`data/` 不参与打包；把整个 `data/` 复制给别人等于复制同一台设备。**
>
> **迁移前先自查**：`python -m replica.application.device_profile --data-dir <data>
> --audit`。新版随机池保证机型 / 系统 / 物理像素 / 逻辑像素 / CPU / root 自洽；
> 旧版冻结指纹可能故意不符合新表（例如早期把逻辑像素写成了物理像素），命令会列出，
> 但不会自动改。早期生成器造成的像素错误可用 `--repair-pixels` 只修这两个字段
> （保留 DeviceId，先备份）；其他字段不匹配必须显式 rotate。任何字段修改都会改变
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

### 2.5 点位与自由跑

- **UI 里的点位按校区固定范围拉取**（默认 2500 m），与今天的「目标距离」无关；
  改目标距离不会重拉、不会让已加载的点位失效。这样同一次登录看到的点位是稳定的。
- 后端/CLI 仍保留 `points_path(distance_m)` 能力：提交时按目标距离向服务器补点，
  再把路线吸附到缺失点位，避免生成“少一个点”的不完整记录。缓存按距离分键。
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

- `real_profile` 先读 `api/v70100/user/info`，再读学期汇总；任一半失败都会
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
| `点位拉不全 / 每次都是同样几个点` | 旧版本把点位距离写死为 1 | UI 已固定按校区范围拉取；改目标距离不需要刷新。仍缺时等 5 分钟限流后点「刷新」 |
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
23/23 模块通过（含 tokenkeeper）
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
