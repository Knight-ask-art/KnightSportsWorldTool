# 01 · 使用手册

## 1. 环境

| 项 | 要求 |
|---|---|
| 系统 | Windows 10/11（路径里的中文字符不影响，命令里加引号） |
| Python | 3.11（3.10+ 应该也行，代码用了 `X | None` 语法） |
| 三方包 | `requests`、`cryptography`（`pip install requests cryptography`） |
| 可选 | `pywebview`（只有桌面壳 `app.py` 需要；`run.py` 不需要） |
| 网络 | 直连 `run.gxapp.iydsj.com`；高德吸附需要「Web 服务」类型 Key |

```powershell
cd 好想跑\jhp_phase1
python -c "import requests, cryptography; print('ok')"
```

---

## 2. 启动

```powershell
# 浏览器 UI（推荐）
python run.py --port 65520

# 不自动开浏览器
python run.py --port 65520 --no-browser

# 桌面壳（需要 pywebview）
python app.py

# 指定数据目录（默认 jhp_phase1/data）
python run.py --port 65520 --store D:\somewhere\data
```

只监听 `127.0.0.1`，不对外网开放。

---

## 3. UI 面板（六页）

新 UI 是唯一入口：`frontend/index.html`，由 `python run.py` 或 `app.py` 打开。
启动后先显示**登录页**：没有本地会话时必须登录，成功后才进入六页；已有有效会话
会自动进入；点「退出登录」会回到登录页。登录成功会自动开启 probe-only token 保活
（密码不落盘）。没有真实数据时显示空状态，不生成演示记录。

| 页面 | 作用 | 关键点 |
|---|---|---|
| **总览** | 本周/近 4 周、本学期进度、未来 14 天计划、最近记录 | 来自 `real_weekly_summary` / `real_semester` / `real_plan` |
| **账号** | 登录/退出、token 校验、设备与会话、保活 | token 存 `data/accounts_emu.json`；密码不落盘 |
| **跑步** | 地图优先；围栏/点位、四种路线来源、模型、日期/开始时间、网络自检、提交、日志 | 点位按目标距离拉取；自由跑可选；提交前只列真实检查项 |
| **路线库** | 本机 `data/routes.json`、导入/导出 GPX | 不再只靠 `localStorage`；服务端不存路线 |
| **记录** | 最近记录、详情轨迹地图、提交回执、CSV/GPX 导出 | 回执只读核对；状态来自 `get_one_record` 回读 |
| **设置** | 11 项学校要求、代理/高德 Key、诊断包 | 登录后优先从服务器自动读取；未返回项留空，不猜规则 |

### 画路线

1. 登录后打开跑步页；围栏和点位自动加载（有缓存）。点「刷新」强制拉取。
2. 选「手绘」，在地图上**单击加点**；右键删点；第一个落点就是起点。
3. 闭合开关默认开：3/4/5 个点都会首尾相连成圈；关闭时保留开放折线。
4. 也可以选「自动」（按当前点位绕圈）、「田径场」（200/300/400m）或
   「导入」（GPX / GeoJSON / JSON / CSV，本地解析）。
5. 需要贴真实道路时点「吸附」（要 `JHP_AMAP_KEY` 或 `--amap-key`）。
6. 提交前检查 token、点位、打卡点覆盖、围栏和校规；未经过点位时后端可自动插入
   （`snap_route`）。

### 学校要求自动读取

登录后如果本地还没有保存学校要求，工具会自动调用服务器的
`runModePolicy`，把 `runRuleModel` 里有明确单位的字段填进「设置 → 学校要求」
并保存：

| 服务器字段 | 表单字段 | 换算 |
|---|---|---|
| `minDistance` | 最低距离 | 米 → km |
| `minRunTime` / `maxRunTime` | 最短/最长时长 | 秒 → 分钟 |
| `speedTop` / `speedBottom` | 配速下限/上限 | 分钟/km（原值） |
| `stepTop` / `stepBottom` | 步频下限/上限 | spm（原值） |
| `validTime[0]` | 有效时间窗 | `HH:MM` |

不猜的字段：`dayGoal`（每日目标，不是单次上限）、`weekCountGoal`、
`minSteps` 等，只在提示里显示，不写进规则。服务器返回多个时段时只填第一个，
并在提示里说明。已经保存过规则时不会自动覆盖；设置页的「从服务器读取」可以
手动更新（确认后只覆盖服务器返回的字段，其余保留）。如果主动把所有项清空并
保存，会关闭模型并停止后续自动读取；需要恢复时点「从服务器读取」。

### 补跑日期与开始时间

「跑步 → 参数」里的 **提交日期 / 开始时间** 控制记录落在哪一天：

- 点开日期按钮是一个小的日历，直接选**具体日期**（北京时间日历日，
  不是 72 小时）；只能选最近 3 天（含今天），范围外的日期是灰的。
- 开始时间必须在 **09:00–22:00**；留空则在该时段内随机。
- 整段跑步都要在 22:00 前结束；当天的记录也不能排到“现在”之后，
  否则模型和提交都会拒绝并说明原因。
- 打开页面时如果今天已经放不下这次跑步（例如还没到 09:00），
  日期默认落到昨天，避免一进来就是一条必然失败的记录。
- 学校要求里的 **每日上限** 按**所选日期**统计：选 10 月 20 日就查 20 日
  已跑几次。次数优先取服务器记录；服务器读不到时模型会明确显示
  “无法确认”，而不是假装 0 次。
- 超过学校上限时**只提醒**（模型 `warn` + 琥珀色感叹号），不会因此拒绝
  提交；是否还有效由学校/服务器决定。

> 服务器是否接受补跑的历史日期没有公开证据（上游 README 写“前 3 天”，
> 但仓库把 `#64 时间限制根因未确定` 标为未解决）。工具只负责把时间写对，
> 不能保证服务器一定收。详见 `docs/04-decisions.md`。

> 围栏和检查只是**提示**；越界或漏点的记录很可能被判无效。

---

### 提交回执与只读核对

「记录」页顶部是**提交回执**：`待确认 / 结果未知 / 已受理 / 已拒绝`。
提交超时后不要重复点提交；点「只读核对未决」，它会用最近记录按开始时间
（±3 分钟）和距离（±120m 或 8%）匹配，匹配到才把回执改成「已受理」。
**核对不会重发、不会补传 OBS**；没匹配到就保持未决。

### 网络自检

「跑步 → 提交」里的「网络自检」会显式查询一次公网 IP 归属地，显示
校区城市、出口 IP 城市、是否同城和警告。它只在你点击时联网；不点就不会查。
江门校区用广州出口 IP 是最常见的暴露点，建议真实提交前先看一眼。

### 自由跑（学校没有打卡点）

「跑步 → 路线」里的 **自由跑** 勾选后，提交时不再拉打卡点，仍会检查围栏、
距离、配速、时间窗和学校策略。学校策略里若有必经点，后端会明确拒绝，
不会假装成功。只在学校确实没有打卡点时使用。

### 路线库 / GPX

- 路线从 `localStorage` 迁到本机 `data/routes.json`；换端口或用 exe 都不会丢。
- 「路线库」支持导入 GPX/GeoJSON/JSON/CSV，选中路线或当前路线可导出 GPX。

> 采集与样本库属于内部研究能力，已移出产品界面与 bridge API，
> 见 `private/README.md`；公开版本不提供该功能。

### 学期进度与 14 天计划

总览的「本学期」显示服务器返回的已完成/目标/剩余天数和完成度；读不到时
显示失败而不是 0。「未来 14 天」来自 `real_plan`，点「填」会把距离/配速/
步频填进跑步参数；已完成的日期会标灰。

### 记录详情轨迹

「记录」右侧详情里有一张小地图：轨迹、起点/终点、打卡点绿/红。可导出
GPX 或把这条轨迹存进路线库。地图瓦片和主地图一样按 z18 封顶放大。

---

## 4. 命令行

入口两种写法，效果一样：

```powershell
# 在仓库根目录（好想跑）
python -m jhp_phase1.replica.pipeline --plan 1

# 在 jhp_phase1 目录里
python -m replica.pipeline --plan 1
```

### 4.1 账号

| 参数 | 默认 | 说明 |
|---|---|---|
| `--user` | — | 手机号，走纯协议登录 |
| `--pass` | — | 密码（会出现在进程命令行里，介意就用 `--token`） |
| `--captcha-id` | — | 极验的 captchaId，通常不用手动给 |
| `--token` | — | 已有 token，跳过登录 |
| `--uid` | — | 配合 `--token` 必填 |
| `--unid` | `47203` | 学校/校区 ID |
| `--device-id` | 自动 | 设备指纹；不填会从 `data/devices/<uid>.json` 复用 |
| `--device-model` | `NCO-AL00` | 设备型号 |
| `--os-version` | `12` | Android 版本 |

### 4.1.1 token 保活

官方接口里**没有**文档化的 refresh-token 端点。因此保活分两层：

1. **定时探测**：每 N 小时用当前 token 请求一次最轻的 `GET api/v70100/user/info`；
   成功就更新 `last_ok_at`，401/业务拒绝就把 `token_expired` 标成 true。
   这能证明 token 还活着，也能提前发现失效，但**不能保证**延长服务端固定 TTL。
2. **自动重登（可选）**：探测到失效时，用当次运行传入的手机号/密码重新登录，
   换新 token。密码只在内存，**不写入 `accounts_emu.json`**；进程重启后需重新输入。

```powershell
# 只探测一次（适合计划任务）
python -m replica.pipeline --keepalive-once

# 前台保活：每 6 小时探测一次
python -m replica.pipeline --keepalive --keepalive-interval 21600

# 失效时自动重登（密码只在内存，会出现在进程命令行里，介意就别用）
python -m replica.pipeline --keepalive --auto-relogin --user <手机号> --pass <密码>
```

UI 的「账号」页有「token 保活」卡片：选间隔、开启/停止、立即检查；
勾「失效时自动重登」会使用卡片上方的手机号/密码，密码不落盘。
被标记为 `disabled` 的会话（例如用户明确停用的旧账号）**不会**被探测或重登。

### 4.2 跑步

| 参数 | 默认 | 说明 |
|---|---|---|
| `--km` | `2.5` | 目标距离（公里），会被拉伸到恰好这个值 |
| `--pace` | `6.0` | 配速，分钟/公里 |
| `--cadence` | `172` | 步频，步/分 |
| `--weight` | `68` | 体重 kg，用于热量 |
| `--altitude` | `25` | 校区海拔基准（米）。上游默认 82 是作者校区的值 |
| `--days-ago N` | — | 补跑日期：`0`=今天、`1`=昨天，最多 `3`；北京时间日历日 |
| `--time HH:MM` | — | 开始时间，必须在 `09:00–22:00`；不给则在该窗口内随机 |
| `--policy` | `1` | **回退值**：正常以服务器 `runModePolicy` 为准，取不到时才用它 |

> 不带 `--days-ago` 也不带 `--time` 时保持旧行为：记录以当前时间倒推，
> 不套用 09:00–22:00 窗口。给任意一个参数即启用补跑窗口。

### 4.3 路线

| 参数 | 默认 | 说明 |
|---|---|---|
| `--no-points` | 关 | 不拉校园点位，直接本地环形路线 |
| `--force-points` | 关 | 忽略点位缓存强拉 |
| `--custom START END` | — | 高德自定义路线（两个地址） |
| `--custom-mode` | `loop` | `single` / `loop`（去程+回程） |
| `--amap-key` | 环境变量 | 高德「Web 服务」Key |
| `--route-file PATH` | — | JSON / GeoJSON / GPX / CSV |
| `--route-frame` | `auto` | `auto` 时 GPX 按 WGS84、其余按 GCJ |
| `--route-close` | 关 | 首尾相连 |
| `--smooth N` | `10` | 每段插值点数，0=不插值 |
| `--snap-route` | 关 | 没经过的点位自动插入（会绕行） |
| `--allow-missed-points` | 关 | 漏点位也提交（记录很可能不达标） |
| `--stadium [PERIMETER]` | 自动拟合 | 田径场模式：不写=按打卡点自动拟合；`--stadium`=400 m；也可指定周长 |
| `--bearing` | `0` | 田径场朝向（度，0=长轴朝北） |

### 4.4 行为

| 参数 | 默认 | 说明 |
|---|---|---|
| `--proxy` | — | 出网代理，让出口 IP 和校区同城 |
| `--dry-run` | 关 | 只造数、只打印，不提交 |
| `--no-preflight` | 关 | 跳过风控预检（离线调试用） |
| `--random` | 关 | 距离/配速/步频按区间抽样 |
| `--seed` | — | `--random` 的可复现种子 |
| `--km-range LO HI` | `2.0 3.0` | `--random` 的距离区间（配置选择，不是统计分布） |
| `--plan DAYS` | — | 只打印未来 N 天节奏计划，不联网 |
| `--no-history` | 关 | 不拉历史记录做同质化检查 |
| `--ignore-must-points` | 关 | 策略必经点解析失败也继续 |
| `--ignore-model` | 关 | 合格模型判定失败也继续 |
| `--max-per-day` | `2` | 每天最多提交几次（0=不限） |
| `--allow-night` | 关 | 允许 23:00–06:00 提交 |
| `--no-limit` | 关 | 关闭本地闸门（次数 + 深夜） |
| `--json` | 关 | 输出 JSON 报告 |

### 4.5 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 登录 / 参数 / 造数 / 路线校验失败 |
| 2 | 提交被服务器拒绝 |
| 3 | 回执拦下（时段重叠 / 上次结果未知），**没有提交** |
| 4 | 本地闸门拒绝（每日上限 / 深夜） |
| 5 | 学校要求模型不通过（距离 / 配速 / 时间窗 / 日期等硬性项；当日次数只提醒） |

---

## 5. 常用组合

> 以下命令在**仓库根目录**执行；如果你在 `jhp_phase1/` 里，把 `jhp_phase1.replica`
> 换成 `replica` 即可（两种写法等价）。

```powershell
# 离线看数据（不登录、不提交）
python -m jhp_phase1.replica.pipeline --token X --uid 1 --km 2.0 --dry-run --no-preflight --no-history

# 已有 token，画好的路线，自动插点位
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --route-file my.json --snap-route --km 2.5

# 田径场 400m，跑 3km
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --stadium 400 --km 3.0

# 高德自定义路线（需要 Key）
$env:JHP_AMAP_KEY="<amap-web-service-key>"
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --custom "五邑大学南门" "五邑大学北门" --km 2.5

# 随机档位 + 同城代理（真正提交）
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --random --seed 42 --proxy http://127.0.0.1:7890

# 补昨天 10:30 的记录（09:00–22:00）
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --days-ago 1 --time 10:30 --km 2.5

# 补 3 天前，开始时间在窗口内随机
python -m jhp_phase1.replica.pipeline --token <t> --uid <uid> --days-ago 3 --random

# 只看未来 14 天计划
python -m jhp_phase1.replica.pipeline --plan 14
```

---

## 6. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `JHP_DATA_DIR` | `jhp_phase1/data` | 回执、每日上限、设备指纹的根目录 |
| `JHP_POINTS_CACHE` | `jhp_phase1/data/points_cache.json` | 点位缓存文件 |
| `JHP_AMAP_KEY` | — | 高德 Web 服务 Key（吸附道路用） |
| `JHP_PROXY` | — | 同 `--proxy` |
| `JHP_CRYPTO` | `netsec` | `netsec`＝纯 Python；`unidbg`＝旧原生后端（回退用） |
| `JHP_HTTP_TIMEOUT` | `30` | 单请求超时（秒） |
| `JHP_NSK_PLATFORM` | `2` | 信封平台的平台号 |
| `JHP_OBS_CONVERT_BD=1` | 关 | OBS 点从 BD-09 转 GCJ（只有喂真 BD 坐标时才开） |
| `JHP_UNIDBG` / `JHP_JAVA` | — | 旧 unidbg 后端路径，已退役 |

---

## 7. 自测

```powershell
cd 好想跑

# 全部模块自测（离线）
python -m replica.selfcheck

# 流水线端到端（假客户端，不联网）
python jhp_phase1/tests/test_pipeline.py

# 其它
python jhp_phase1/tests/legacy/legacy_bridge_suite.py
python jhp_phase1/tests/legacy/legacy_http_suite.py
python jhp_phase1/tests/test_netsec.py
python jhp_phase1/tools/validate_sim.py
```

单个模块也能直接跑，例如：

```powershell
python -m jhp_phase1.replica.readonly
python -m jhp_phase1.replica.routes
```
