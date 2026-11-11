# 60 · 运行与排错

> 证据等级：**A**（源码 / `docs/operations.md`）
> 最后更新：2026-11-11 · 对应版本 **0.2.2**

## 1. 三道本地闸门

| 闸门 | 默认 | 开关 | 退出码 |
|---|---|---|---|
| 每日上限 | 每天 2 次 | `--max-per-day N`（0=不限） | 4 |
| 深夜 | 23:00–06:00 拒绝 | `--allow-night` / `--no-limit` | 4 |
| 回执 | 提交前 write-ahead | 不能关 | 3（拦下） |

- 计数按**北京时间零点**重置；补跑按记录开始日期占用额度。
- 学校自己的每日上限是**提醒**，不是工具硬闸门；两者不要混。
- 深夜窗口和每日上限都是工具自己的保护，不是服务器规则。

## 2. 回执（write-ahead）

提交前先写 `data/submission_receipts/<uid>__<unid>.json`，四种状态：

| 状态 | 含义 | 后续 |
|---|---|---|
| `pending` | 请求已发出或进程崩了 | **未知，不能重提** |
| `accepted` | 服务器返回 10000 + rrid | 完成 |
| `rejected` | 明确拒绝 | 可以手动重试 |
| `unknown` | 传输/解析失败 | 只能只读对账 |

- **提交永不自动重试**。
- `pending` 锁住该时段，避免“响应丢了但服务器其实入库了”。
- 同一 `data/` 下多进程用文件锁串行化。
- 回执不含 token、密码、坐标。

遇到「回执拦下」：先看记录列表/详情，确认上次成没成，**不要删回执重提**。

## 3. 补跑与时间窗

- 允许最近 3 天（含今天，北京时间）。
- 开始时间 09:00–22:00，**结束也要 ≤ 22:00**。
- 今天的记录不能排到“现在”之后。
- 越界直接拒绝（退出码 1），不会悄悄改时间。

## 4. token 保活

- 定时 `user/info` 探测；失效标记。
- 可选内存自动重登，密码只在内存，重启失效。
- `disabled: true` 的会话不探测、不重登。
- 登录页「记住密码」用 DPAPI 存 `data/saved_login.json`，取消勾选立即删除。

## 5. 点位、围栏与缓存

- 点位按距离随机抽 5 个；同一校区不同目标距离成员可能不同。
- 缓存 300 秒；10603 冷却 5 分钟，跨重启保留。
- 提交复用地图上显示的那套点，避免记录走另一批点。
- 没有围栏是合法配置；官方在 `geoFences` 为空时直接 `isInArea=true`。
- 没有打卡点默认停止，必须显式勾选「跳过打卡点」才继续。
- 多围栏按跑区名匹配；匹配不到才回退 `selectedPosition=0`。
- 围栏优先信 policy；`null`/缺段才回退 `getGeoFenceForRun`。

## 6. 排错表（高频）

| 现象 | 原因 | 处理 |
|---|---|---|
| `10121 此账号已在其他设备登录` | 账号保护 | 原设备退出，或等约 1 小时；**不要反复试** |
| `10175` 密码错误 | 密码不对 | 重新输入；不要连续试 |
| `10603` / rate limited | 点位频次限制 | 等 5 分钟冷却；缓存可读 24 h |
| token 失效 / HTTP 401 | token 过期 | 重新登录；长期运行开保活 |
| `fence error Unauthorized` | token 问题 | 围栏跳过，不影响离线功能 |
| `AMap key missing` | 没配高德 Key | `--amap-key` 或 `JHP_AMAP_KEY`（Web 服务类型） |
| `路线没经过 N 个点位` | 线离点超过 25 m | `--snap-route` 或重画 |
| 跑步冻结期 | `freezeRunTime > 0` | 只提醒；建议等解冻 |
| 人脸抽检 | `faceVerify` / `runFaceCheckConfigDTO.enable` | 停止提交；不要伪造 `faceCheck=1` |
| `路线周长只有 Xm` | 路线太短 | 画大一点（闭合至少 80 m） |
| `路线太短：铺满 X km 要 N 圈（上限 30）` | 圈数太多 | 画长一点或减距离 |
| 点在围栏外 | 画出校区 | 只告警；记录很可能无效 |
| `回执拦下`（退出码 3） | 该时段已有 pending/unknown | 先只读对账 |
| 每日上限 / 深夜（退出码 4） | 本地闸门 | 等明天或显式关闸 |
| `开始时间无效` | 补跑窗口外 | 改时间；结束也要 ≤22:00 |
| 当日次数达学校上限 | 模型提醒 | 仅提醒，不阻止；是否算数由学校定 |
| IP 城市不一致 | 出口 IP 与校区不同城 | 仅提醒；可换热点/同城网络 |
| UI 502 / 打不开 | `run.py` 没启动或端口冲突 | 换端口 `--port 65521` |

完整表见 [`../operations.md`](../operations.md) §3。

## 7. 只读对账

提交结果不确定时，只读接口是安全的：

```text
POST /bridge/real_recent_records   {"args": {"limit": 20}}
POST /bridge/real_record_detail    {"args": {"rrid": 12345, "uuid": "..."}}
POST /bridge/real_weekly_summary   {"args": {"weeks": 4}}
```

UI「记录 → 提交回执」的只读核对按开始时间（±3 分钟）和距离（±120 m 或 8%）
匹配；匹配到才把回执改成 `accepted`，没匹配到保持未决。**不重发、不补传 OBS**。

## 8. 诊断包

- `replica.diagnostics` 生成脱敏报告：不含 token、密码、uid、DeviceId、坐标、
  IP、原始请求体、校区数字 ID、本地路径。
- 贴日志/提 issue 优先用诊断包，不要直接贴 `data/`。
- 极验失败现场含 `w` 明文，**不能外发**。

## 9. 自检与回归

```powershell
cd app
python -m replica.selfcheck
python -m pytest tests -q
```

- `selfcheck`：模块级离线自检。
- pytest：全量回归；Windows-only 用例在 Linux 自动 skip。
- 发布前还要跑 `scripts\cloud_test.py` 和 `scripts\release_check.py --source-only`。

## 10. 数据恢复

| 场景 | 做法 |
|---|---|
| 升级后登录失效 | 正常；重新登录，指纹不变 |
| 换机 | 复制整个 `data/`（含 `device_profile.json`） |
| 指纹要回滚 | 用 `device_profile.backup-*.json` 覆盖后重启 |
| 回执 `pending` 不确定 | 只读对账；不要删回执重提 |
| 路线库丢了 | 检查 `data/routes.json`；不要再依赖 localStorage |
| 更新包坏了 | 删 `data/updates/` 重新下载，校验 sha256 |
