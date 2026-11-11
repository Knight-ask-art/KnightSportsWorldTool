# 99 · 术语表

> 最后更新：2026-11-11

| 术语 | 含义 |
|---|---|
| **运动世界校园** | 本项目复刻的官方 App，包名 `com.zjwh.android_wh_physicalfitness` |
| **好想跑** | 本项目：本地 Windows 工作台，协议复刻 + 造数 + 提交 + 只读回查 |
| **学分跑** | 学校计入学分的跑步模式，服务器 `policy=1` |
| **policy** | 服务器下发的规则集编号；**不是风险标记** |
| **runRuleModel** | 学期视图返回的学校规则模型（距离、时段、配速、步频、围栏等） |
| **rrid** | 服务器为一条跑步记录生成的 ID |
| **uuid** | 本次跑步的对象 ID，OBS key 和部分接口会用到 |
| **unid** | 学校/校区 ID |
| **OBS** | 对象存储（华为云 `iydsj-hbase-hot`）；轨迹和分段数据放这里 |
| **run_data** | OBS 对象，内部是 `allLocJson` |
| **allLocJson** | 完整轨迹点的 JSON 字符串，可能再套一层 |
| **fivePointJson** | 打卡点数组；`position` 是真机数组下标 |
| **segment_json** | 分段分析；提交体里会被置空，OBS 里是另一个对象 |
| **speed_json / step_freq_json** | 10 秒窗的配速/步频序列，`queueNum` 每 30 秒加 1 |
| **laps_json** | 圈段数据，`avgPace` 单位是分钟/公里 |
| **isCheat** | 服务端反作弊命中标记，`1` = 异常 |
| **stepFreqStatus** | 步频异常标记，非 0 = 异常 |
| **normalRun** | 列表里的“是否正常跑”，`false` = 有问题 |
| **reasonList** | 详情里的原因列表，`complete=false` = 有未完成项 |
| **围栏 / geoFence** | 校区可跑步区域；为空是合法配置 |
| **打卡点 / fivePoint** | 跑步必须经过的点位，随机抽取 |
| **跑区 / runArea** | 一个学校下的多个可跑区域，按 `campusName`/`schoolName` 匹配 |
| **步频** | 每分钟步数；平台会检查异常波动 |
| **配速** | 分钟/公里；提交体 `speed` 是 ×1000 的整数 |
| **极验 / GeeTest v4** | 登录前的滑块验证；本地纯算 + 手动兜底 |
| **NetSecKit** | 官方 App 的网络加密/签名组件，本项目在 `transport/` 复刻 |
| **headerSign** | 提交/请求头签名 |
| **UPLOAD_SIGN_FIELDS** | 参与提交体签名的 31 个字段清单 |
| **DPAPI** | Windows 数据保护 API；token 和“记住密码”用它加密 |
| **设备指纹** | `device_profile.json`，登录/提交/OBS 共用；每个 data 目录一份 |
| **回执 / receipt** | 提交前 write-ahead 的状态文件，防止重复提交 |
| **pending / unknown** | 回执状态：未知，不能重提，只能只读对账 |
| **10603** | 点位接口频次限制错误码 |
| **10121** | 账号在其它设备登录 |
| **10175** | 密码错误 |
| **bridge** | 前端 → Python 的本地 JSON 接口 `/bridge/<method>` |
| **skin** | 皮肤：`paper` / `poster` / `minimal` / `archive` |
| **announcement_seen** | `data/ui_state.json` 里的公告已读映射 `{id: version}` |
| **latest.json** | 自动更新清单，客户端按版本/大小/SHA-256 校验 |
| **rollback** | 清单里的上一稳定版，手动回滚用 |
| **onedir / onefile** | Nuitka 两种产物形态；本项目用 onedir |
| **SmartScreen** | Windows 的未知发布者提示；没有代码签名时会出现 |
