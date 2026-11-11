# 04 · 关键决策与证据

> 原则：**没有源码 / issue / 实测出处的结论不写进代码**。
> 下面的每一条都标明依据；找不到依据的，进 §5「未验证清单」，不装作已确认。

---

## 1. 架构与协议

| 决策 | 依据 |
|---|---|
| 用**纯 Python** 复刻 NetSecKit，退役 unidbg | `netsec.py` + `nskcodec.py` 与上游 golden vector 对拍；同一条提交链在纯 Python 下跑通 |
| `JHP_CRYPTO=unidbg` 只作回退 | 旧 native 路径保留，不再维护 |
| 单文件协议栈：登录/极验/信封/签名都在 `replica/` | 不依赖模拟器、抓包重放或浏览器 |
| 提交体严格对齐上游 43 字段 / 31 签名字段 | `submit.rs` 与 `crypto.UPLOAD_SIGN_FIELDS` 逐字段对拍 |
| 删掉自造的 `segmentJson` / `prizeName` / `recordUrl` | 详见 `docs/api.md` §5.1：`prizeName`/`recordUrl` 只在详情页回显时 set，提交路径不设置；`segmentJson` 在 `UploadFormatEntity.setSegmentJson()` 里算完 `useMobilityTools`/`status` 后置空再上传 |
| `roomId` 只在 `sportType=3` 时发 | 上游条件分支 |

## 2. 坐标、单位与字段

| 决策 | 依据 |
|---|---|
| 内部统一 **GCJ-02** | 围栏接口只返回 `glat/glon`；地图（高德）也用 GCJ |
| 轨迹点 `lat/lon`：7.2/7.3 用 **BD-09**，7.4.0 为 **-1 占位**；`fivePoint` 仍用 BD-09 | `submit.rs` / App 的 `lat/lon` 语义；`gLat/gLng` 才是 GCJ。7.4.0 `MyLocation` 构造器写死 `lat/lng=-1`，真实坐标只在 `gLat/gLng` |
| OBS 默认不转 BD | 生成器内部已是 GCJ；只有喂真 BD 坐标才开 `JHP_OBS_CONVERT_BD=1` |
| 距离统一按**米**处理，只转一次 | 提交体 `totalDis` 是米；列表接口也是米 |
| 配速统一用 `时间 ÷ 距离` 算；轨迹 `speed` 是**配速 min/km**，`avgSpeed` / `bdS` 是 m/s | 官方 7.2/7.3 记录 667/667 点满足 `avgSpeed = 16.6667 / speed`（中位误差 0.0025）；7.4.0 `MyLocation` 三个字段都是 `double`；提交体 `speed = round(pace,2)*1000` 与 `UploadFormatEntity.setSpeed` 一致 |
| `address` = **起点逆地理编码得到的城市**，取不到写空串；不是学校名 | 官方 `OutdoorRunViewModel.saveStartPointToDb()` → `MapLogKt.getCity(gLat,gLng)`；上游 issue #16 的 `大连市` 默认值只是反面证据 |
| `policy` / `selDistance` 取自服务器 | `runModePolicy` 的 `policy`、`runRuleModel.minDistance`；不硬编码 |

## 3. 路线与围栏

| 决策 | 依据 |
|---|---|
| 田径场模式**拟合真实打卡点**，逆时针 | 上游 PR #44；拟合失败直接中止，不猜 |
| 自定义路线先拉点位，再按 **25 m** 校验命中 | 官方 `RunRanLocationManager` 的通过半径 |
| `--snap-route` 默认不自动开，但提示明确 | 插入点位会产生绕行，必须让人自己选 |
| 围栏越界**只告警不拦截** | 画到外面是用户的选择，但不能让他看不见 |
| 围栏边界上的点算**界内** | 围栏边是合法位置；射线法单独判断会误报 |
| 退化路线直接拒绝（周长 < 80 m、圈数 > 30） | 2 点路线会被折返铺满，几何量出来只有 1.5 km |
| 采样后按质心缩放，让轨迹长度等于目标 | 弦长 ≠ 弧长；直角路线会短 4%，属于数据自相矛盾 |
| 周汇总在**本地聚合** | APK 只有 `recordssummary/all`（学期），没有「周」端点 |

## 4. 安全与状态

| 决策 | 依据 |
|---|---|
| **提交永不重试** | 超时=未知；重试可能重复入库 |
| 回执先写后发（write-ahead） | 上游 `submission_guard.rs` + `docs/violation-checks.md` |
| 每日 2 次 + 23:00–06:00 闸门 | 上游 PR #80；可用 `--no-limit` 关 |
| 补跑只允许 **今天..3 天前**、**09:00–22:00**，结束也 ≤ 22:00 | 上游 `clamp(0,3)` + 服务器 `runModePolicy.validTime`（06:30–22:00）；取更窄区间避免边界 |
| 每日上限按**所选日期**统计，服务器记录优先；本地回执只作闸门/诊断 | 学校 `max_per_day` 是按跑步日算的；只看本机回执会漏掉官方 App/其它设备，且不能证明服务器次数 |
| 次数读不到时模型显示 **warn**，不静默当作 0 | “0 次”和“查不到”是两件事；静默会误导补跑决策 |
| 超过学校每日上限只给 **warn**，不 hard-fail、不拒绝提交 | 学校/服务器才决定多出来的记录算不算；工具只负责提醒 |
| 本地计数文件按日期存 map（旧单日格式自动迁移） | 旧实现 `load(昨天)` 会重置文件，补跑时会抹掉今天计数 |
| 会话/回执/计数落盘用原子替换 | 服务是多线程的，半截文件会表现成「token 突然没了」 |
| 会话 token 用 DPAPI 封存 | `accounts_emu.json` 只留 `token_enc`；旧明文在启动时迁移 | 复制 `data/`、备份或换 Windows 用户都无法解密冒充 |
| 单实例命名互斥量 | 第二个实例聚焦已有窗口并退出 | 两个窗口共用 `data/` 会互相覆盖 session/校区 |
| 点位 10603 冷却按 `uid\|unid` 隔离 | A 账号被限流不能锁住 B 账号；`force` 只绕过缓存，不绕过冷却 | 双账号是常态，全局冷却会误伤另一个校区 |
| **UI 点位**按最终提交「目标距离」拉取：绘制过程中不请求，路线定稿（关画笔/切页/导入/载入/拖动结束/手输）后按精确距离请求一次，仍受 5 分钟预算；**提交复用地图上的点位** | 实测每次请求随机抽 5 个点；逐 10 m 重拉会耗尽 3 次/5 分钟额度，提交时再抽会换一套（2026-10-30 修正） |
| 提交结果用**常驻卡**，结果未知时锁提交 | 180 s 超时后 toast 已消失，按钮却恢复可点；只读回执核对完成才解锁 | 重复提交和“提示已消失”都是操作事故 |
| 路线撤销用 **50 个完整快照**（含闭合状态） | `pop()` 只能撤销最后一个点，删中间点后越撤越乱；支持 Ctrl+Z / Ctrl+Shift+Z | 快照能精确恢复任意一次编辑 |
| 检查表只有**带目标**的行显示可点击 | 没有修复入口的冻结期/人脸抽检不再伪装成按钮；网络出口已移出检查表，只在诊断卡里手动查 | 视觉 affordance 必须兑现；提醒项不能变成提交闸门 |
| 冷却期间**刷新按钮置灰并每秒 tick** | 秒数只藏在 title 里时用户会反复点；按钮文本直接显示「冷却 Ns」 | 状态要可见、可等待 |
| 目标距离默认 **0**，手绘后自动填充；手动输入优先 | 默认 2.5 km 会让用户在没画线时以为目标已确定；手绘长度才是这次要提交的真实距离 | 删除最后一点或清空路线回到 0，手动值保留 |
| 点位与目标距离不一致要**显式提示** | 5 分钟预算/10603 可能让点位停留在旧距离；静默沿用会让界面和提交语义不一致 | chip、点位徽标、检查表、提交 note 四处同时标注，检查项可一键刷新 |
| `fivePointJson[].position` 用数组下标；10s 窗 `queueNum` 每 3 个窗（30 s）加 1、末窗按实际时长缩放 | 官方 7.4.0 `SpeedPerTenSec.queueNum`；真实 4 km 记录的窗口队列是 `0,0,0,1,1,1,…`，点队列也是 30 s 一批（旧实现每窗 +1 会把回放批次拉快 3 倍） |
| 回执核对只读、匹配到才写 accepted | 响应丢失时不能重发；只读查询能确认是否入库 |
| 网络诊断只在用户点击时查公网 IP，且**不进入检查表** | 自动查 IP 会泄露网络位置；把出口城市当成提交闸门会让省级 NAT 用户无法提交 |
| 设备指纹按 `data` 目录生成一次，登录/提交/OBS 共用 | 编译期常量会让所有用户同一台设备；每次启动随机会让账号天天换机 |
| 旧数据升级冻结原来的提交指纹，不静默轮换 | 旧账号已经用旧指纹提交过；静默换机本身就是新的风控信号 |
| 更换设备指纹必须显式 rotate，并先备份旧指纹 | 可回滚；避免误操作把真实账号变成“新手机” |
| GUI 更换入口放在“高级”并双重确认，后端再校验确认文字 | 误点会像换机；后端还会先退本机登录、备份旧指纹 |
| 新安装默认 Android；iOS 保留为可选 | 2026-11-12 起新账号统一 Android 起步；iOS 仍可通过 `JHP_IOS_CHANCE`、rotate 或自定义设备选择 |
| iOS 登录和提交都使用 iOS 头 | 保证“同一台设备”；上游 `submit.rs` 固定 Android 头，反被其自身审计（`review/ANTI-FLAG-AUDIT.md` A1）判为「同一条 token 上平台漂移」的硬矛盾。全链同平台的选择已在真机验证（rrid `1344757460`：`status=0` / `isCheat=0` / `policy=0` / `stepFreqStatus=0`） |
| iOS 使用 `arm64e`、按机型配物理/逻辑像素，不使用参考模拟器的 `x86_64 / 1080x1920` | 模拟器组合是明显的自动化信号；真实 iPhone 不会用 x86_64 |
| Android 逻辑像素使用 dp，不再与物理像素相同 | 早期 `1080x2400 / 1080x2400` 是明显的合成指纹；按机型表一起生成 |
| iOS 头使用独立的 typed JSON 序列化 | `uid/studentId` 是数字、`isRoot` 是布尔；套用 Android 的 Java HashMap 字符串化会改变 headerSign |
| 请求带平台对应的 User-Agent（仅线上传输，不参与 headerSign） | 不设置时 urllib 会暴露 `Python-urllib/x.y`，比设备字段更显眼 |
| UA 全链唯一来源，OBS 直传也用同一个 | 官方 Android dex 里没有自定义 UA（`SWCampus` 命中 0 次），故 Android 用 `okhttp/4.12.0`；iOS 用 NSURLSession 默认形态 `SWCampus/7.4.0 (iPhone; iOS x; Scale/3.00)`，版本与签名头 `appVersion` 同源。OBS 是独立域名，漏设 UA 会让同一次运行出现两种客户端（上游同样有此漏洞，antiban 审计点名） |
| iOS 的 `appInstallTime`/`DeviceId` 缺失时用兜底值 | 发 `0` / 空串是任何真机都不会报的值；参考实现按 iOS 3 天回退，DeviceId 进程序内固定生成（不逐请求漂移） |
| 跳过打卡点仅在学校无点位且策略无必经点时可用 | 有必经点却跳过点位会生成永远不完整的记录，后端直接拒绝 |
| 路线库存 `data/routes.json`，不再只靠 localStorage | localStorage 按 origin 隔离，换端口/换 exe 会丢路线 |
| 学期字段做保守归一化，缺失显示未知 | 不同学校/版本字段不一致；把缺失当 0 会误导达标判断 |
| 学校规则读取用 `runModePolicy` 的 `operateType=1`；提交继续用 `operateType=0` | 真实返回里只有学期视图带 `runRuleModel`，开始跑步视图只带 `policy`；提交记录实测 `policy=0` 与开始跑步视图一致 |
| 记录详情 `jsonFromObs=1` 时回读 OBS 对象 | 数据库行的 `allLocJson` 为空，完整轨迹在日期式 `run_data/{YYYYMMDDHH}/{uuid}.json`（旧 rrid 式仅作回退）；不回读会显示“0 点” |
| 打卡点命中半径由后端下发（`hit_radius_m`），前端不再写死 | 协议常量是 `RunSeqLocationManager` 的 `distance <= 25.0`；检查表曾用 40 m，会显示“全过”而服务端判定漏点，一次提交就白跑 |
| 补跑开始时间保留秒和毫秒，不落在整分 | 官方开始时间来自 `System.currentTimeMillis()`；整分会把每个点 `flag`、10 s 窗边界、`gainTime` 全部对齐到同一网格 |
| 步频窗 `maxDiff/minDiff/avgDiff` 保持 `0/1000/0`，`validDis/validTime` 保持与总量相等 | **反编译源码证实这是官方行为**：`StepManager` 的 `mDiff` 只有 `onDiffChanged` 会更新，而 `AccelerometerListener` 从不调用它；`RunLocationManager2` 明确 `setValidTime(mTotalTime)` / `setValidDis(mTotalDistance)`。上游审计 A2/A3 的“完美值”指控不成立，改了反而更像伪造 |

---

## 5. 未验证清单（重要）

这些**没有**在真实链路上确认过，代码里按最保守的方式处理，但不要当成事实：

| 项 | 现状 | 影响 |
|---|---|---|
| 最后一次完整提交回归 | **没做**（token 失效 + 不允许拿账号测试） | 提交链路的最后一步仍是历史上跑通过的状态 |
| 服务器是否接受补跑的历史日期 | **未验证**；上游 README 写“前 3 天”，但 `#64 时间限制根因未确定` | 可能被 `11016` 或其它规则拒；工具只保证时间字段自洽 |
| 补跑记录的服务器审查口径 | 未知 | 回溯性审查可能对历史日期更敏感；不要把它当作“安全补签” |
| 记录详情字段全集 | 按 APK 前端代码对齐（`run_data.allLocJson` 等） | 真实返回不同时，`fields` 会列出实际键名 |
| 详情里 `speed` 的单位 | 已确认是**平均配速 min/km**（10-24 记录 6.08 = 24′31″/4.029 km） | 只原样透出；`speedTop/Bottom` 也是配速区间 |
| 记录列表的排序方向 | 不假设 | 代码会检测「新→旧 / 旧→新」两种 |
| 周汇总的窗口边界 | 北京时间周一~周日；`truncated` 标明页数用尽 | 记录很多时早期周只是下限 |
| `allLocJson` 发完整轨迹 | 上游发空串，我们发完整轨迹（**主动偏离**） | 若服务器校验轨迹长度/形状，这是差异点 |
| 高德「Web 服务」Key | 未配置时吸附按钮直接报错 | 不影响画线和提交 |
| 点位 `/distance/<米>` | 2026-10-30 实测：1000/1060/2500 m 各返回 5 个点，成员与 `isFixed` 均不同（池子 ≥7） | 每次请求随机抽 5 个，不是稳定校区清单；提交必须复用地图上的那套 |
| OBS 10 秒窗新语义 | 按 PR #106 真机语义实现，未看真实回放页 | 若回放仍异常，优先对比 `id/queueNum/末窗` |
| 跳过打卡点无点位记录 | 后端会拒绝策略有必经点的学校；未真机提交 | 服务器是否接受普通计分跑的无点位记录未知 |
| 学期字段单位 | 里程字段按“>=1000 视为米”归一化；时间戳兼容秒/毫秒 | 极端学校字段若已用米但数值 <1000，可能显示偏小 |
| 学校规则自动读取 | 登录/设置页只读 `runModePolicy.runRuleModel`；按 APK 源码单位映射距离/配速/时长/步频/时间窗 | 真实字段名与多时段还没 token 回归；`dayGoal`/`weekCountGoal`/`minSteps` 不猜 |
| 每日次数上限 | `runRuleModel` 没有每日次数；`CampusConfigModel.maleDayUpper` 是每日里程，不是次数 | “学校允许每日次数”仍需手动填；不把里程当次数 |

> 主动偏离只有这一条（`allLocJson`）。它写在 `PORT_FROM_NEKO.md` 里，
> 如果将来提交被莫名拒绝，**第一个要回滚的就是它**。

---

## 6. 证据在哪

| 想查什么 | 去哪 |
|---|---|
| 每一轮改了什么、为什么 | `reverse-engineering/unidbg/PORT_FROM_NEKO.md`（§1–§30） |
| 接口/字段的原始出处 | 同上的「核实清单」与各节「出处」表 |
| unidbg / Frida 时代的推导 | `archive/unidbg-era/` |
| 算法题式的手工推导 | `archive/reference/algorithms/` |
| 旧协议、探针、版本差异 | `archive/reference/` |
| 第三方 7.2.70 分析 | `reverse-engineering/coaixy_ref/`（保留原位） |

---

## 7. 工程边界（2026-10-24）

| 决策 | 内容 | 原因 |
|---|---|---|
| 采集 / 样本库私有化 | 实现移至 `private/research/`；数据默认写 `private/data/corpus/`；不进 UI、不进 bridge、不进打包 | 属于内部研究能力，不构成产品功能，且样本含真实轨迹 |
| 公共包导入名 | `replica` 为唯一 canonical 名称；`app.replica` 只是仓库根目录下的命名空间路径 | 避免同一进程加载两份模块与全局状态 |
| 旧 71 API / mock | 隔离到 `compat`，默认不暴露、不删除 | 可能仍被旧脚本使用，先冻结再评估 |
| 恢复点 | 仓库根 `好想跑/` 初始化 Git；`data/`、`backup/`、`dist/`、`private/data/`、`archive/`、`reverse-engineering/` 全部忽略 | 保证可回滚，同时不把凭据和运行数据入库 |

---

## 8. 发布形态（2026-10-26）

**正式发布统一使用 NSIS 安装包。** 下面几条是约束，不是建议。

| 决策 | 内容 | 原因 |
|---|---|---|
| 正式发布出 **NSIS 安装包** | `packaging/installer.nsi` 产 `haoxiangpao_v<version>_win64_setup.exe`；zip 保留为兼容资产 | 安装/卸载/开始菜单/卸载项一次到位；升级能真正删掉旧文件（zip 只是覆盖，旧 `.pyd` 会一直留着）；LZMA 比 zip 小 |
| **按用户安装**到 `%LOCALAPPDATA%\Programs\好想跑` | `RequestExecutionLevel user`，不请求管理员 | 程序把 `data/` 写在 exe 旁边，装进 `Program Files` 写不进去，登录状态保存会失败 |
| 升级 / 卸载**永不碰 `data/`** | `installer.nsi` 逐项删除并跳过 `data`；卸载默认保留，静默卸载也保留 | 里面有 token、提交回执、设备指纹；误删等于丢登录态和防重复提交的闸门 |
| 清单同时保留 zip 与安装包 | `windows-x64`（zip）+ `windows-x64-setup`（exe） | 0.1.2 客户端只认 `windows-x64` 且只支持 zip；直接换成 exe 会让那一批人更新报「不是有效 zip」 |
| 安装版走安装包、便携版走 zip | 更新器用 `Software\好想跑` 注册表判断形态 | 两条轨道各自原地升级，谁都不用迁移数据目录 |
| 资产名全 ASCII | `haoxiangpao_v0.1.3_win64_setup.exe` | GitHub 会把中文资产名重写成 `_v...`，清单里的 URL 会 404 |
| 升级包必须带 `sha256` | zip 与安装包缺哈希时更新器都直接拒绝 | 两者都会被自动执行/覆盖，没有哈希等于闭眼执行 |
| NSIS 不装进系统 | 用官方免安装 zip，解压到 `app\build_tools\nsis`，用完删除 | 安装包只有 1.5MB，但没必要常驻；构建机保持干净 |

细节见 `docs/packaging.md`（构建、路径、BOM 要求）和 `docs/update.md`（清单、双轨）。

---

## 9. 极验的可维护性（2026-10-26）

极验不会「一劳永逸」。目标是**一年坏 1–2 次、每次两小时内恢复**，所以这轮改的不是
算法，是「坏了能不能快速定位、用户能不能自己绕过去」。细节见 `docs/geetest.md`。

| 决策 | 内容 | 原因 |
|---|---|---|
| 拆成 **图像 / 字段 / RSA / 编排** 四块 | `gt4_image.py`、`gt4_payload.py`、`gt4_rsa.py`、`gt4.py` | 上游改字段的频率远高于改图像；拆开后改字段不用重测 CV，改 CV 也碰不到 payload |
| **黄金向量**逐字节回归 | `gt4_payload.GOLDEN_SHA256` 固定拆分前的明文哈希 | 拆分当场就抓出漏掉的 `YciC` 字段——这种错在线上表现为 10003，极难反推 |
| **字段基线**校验 | `PAYLOAD_KEYS` + `validate_payload`，少/多/错序都抛 `PayloadError` | 宁可在本地炸，也不要发一张低信任票出去 |
| 错误按 **A/B/C** 分层 | `GeeRejected` / `GeeSchemaChanged` / `GeeUnavailable` / `RiskControlError` | 当年把「票被 GeeTest 接受」当成「票被业务服务器接受」，49 轮里有 13 轮浪费在这上面 |
| 失败**落盘**且脱敏 | `data/diagnostics/gt4/<时间戳>/`，含 `static_path`、`w` 明文、两张图 | 没有 ground truth 才需要 49 轮；有的话是一个 diff |
| canary **不碰账号** | `python -m replica.transport.gt4 --canary` 只跑 `/load → /verify` | 能干净区分「解题器坏了」和「账号被风控了」 |
| 重试收敛到 **2 个 id × 1 次** | `solve_gt4(attempts=1)`，`GeeRejected` 不重试 | 旧逻辑最多解 18 次；同一 IP 连续失败本身就触发风控 |
| 手动兜底**下沉到协议层** | 前端只画图 + 给偏移量，`real_gt4_manual_start/finish` 走和解题器**同一份** `build_payload` | 官方 `gt4.js` 在本应用的 Chromium/WebView2 里渲染不出来（实测 0 canvas / 0 img），点开是空框；协议层兜底不依赖第三方 JS，也不会变成第二套 `w` 实现 |
| 手动兜底的**纵向位置仍需匹配** | `locate_gap` 一并返回 `y`，前端把滑块画在缺口那一行 | 首版把滑块画在 `top:0`，横坐标再准也永远对不上；`y` 本来就在 `minMaxLoc` 里，之前被丢掉了 |
| `/verify` 的 **`data.result=fail`** 算拒绝 | `submit_distance` 先看 `data.result`，再找 `seccode` | 答错时极验返回的是 `status:"success"` + `result:"fail"`；旧代码会报「成功但 seccode 不是对象」，把「答错」误诊成「协议改了」 |
| RSA 模数**留了刷新钩子** | `gt4_rsa.refresh` 默认关闭，`JHP_GT4_RSA=on` 才去抓 | 实测线上 `gcaptcha4.js` 里没有公钥；默认开着只会每次登录白下 728KB |
| 缩放 / 时序**去硬编码** | `userresponse` 按 `bg_width` 换算；`passtime` 按距离生成 | 图从 340 变 600（retina）时，旧公式会算错但不会报错 |

**没做的**：没有引入 Playwright，没有把 unidbg 请回来，也没有为了兜底去打包第二个
Chromium。前者是 150MB 只为一条路径，后者解决不了 `w` 字段问题——这两条在
`docs/geetest.md` §7 都有说明。

**手动兜底的边界**：它兜的是「答案不对」（CV 认错缺口、或认对了但被判低信任），
不兜「协议变了」——两条路共用 `build_payload`，字段集一变会一起失败、一起落盘。
这是刻意的：兜底路径如果自带第二套协议实现，它自己就会腐烂，而且坏了没人知道。

---

## 10. 轨迹点类型是一个封闭集合（2026-10-27）

「灰线」不是渲染 bug。官方详情页把每个点的 `type` 直接交给地图 SDK：

```java
// mvi/run/mode/RunHistoryDetailPImpl.java:382
if (p.getType() != -1) {
    list.add(new LineBean(latlng, p.getType(), p.getSpeed(), p.getBdD()));
}
// mvi/sport/BaseDueRunPImpl.java:1171（实时绘线同理）
```

值是客户端**产出**的全集，不是随意填的：

| 值 | 来源 | 含义 |
|---|---|---|
| `0` | `passPointAndBackType()` 默认返回（`BaseRunPImpl:1351`、`BaseDueRunPImpl:1307`） | 普通点 |
| `1` / `2` | 真的进入必经点范围时 `isFixed == 1 ? 2 : 1`（`RunRandomPImpl:358`） | 打卡 |
| `5` | `initPoint.setType(5)` | 起点 |
| `6` | `location.setType(6)` | 终点 |
| `-1` | `myLocation.setType(-1)` | 无效，详情页丢弃并**断开线** |

**渲染器认识的值和客户端产出的值不是同一个集合。** 这一点之前只能靠上游 PR #55 的
注释推断，2026-10-27 在公开仓库 `Coaixy/android_wh_physicalfitness_analyse`
（对应下面这份反编译快照）里拿到了实际画线代码 `GaoDeRunPointControl.addLine()`：

```java
// com/zjwh/map/gaode/GaoDeRunPointControl.java
if (this.mCachePoint < 1 && lineBean2.getType() == lineBean.getType()
        && Math.abs(lineBean.getBdD() - lineBean2.getBdD()) <= 10.0f) {
    this.mCachePoint++;                    // 合并顶点，不上色
} else {
    int i = (lineBean2.getType() == 3 || lineBean2.getType() == 4) ? -4868683
          : (lineBean2.getType() == 10 || lineBean2.getType() == 7) ? SupportMenu.CATEGORY_MASK
          : -16727669;
    ...colorValues(arrayList2)...
}
```

| 颜色 | 常量 | RGB | 触发条件 |
|---|---|---|---|
| 灰 | `-4868683` | `#B5B5B5` | `type == 3` 或 `4` |
| 红 | `0xFFFF0000` | `#FF0000` | `type == 7` 或 `10` |
| 绿 | `-16727669` | `#00C18B` | **其它所有值**（`-1` 已被跳过） |

所以「灰线」的确切条件是 `type ∈ {3, 4}`，不是「未知值兜底」——这一点我此前说错过，
现在按源码更正。上游工具在默认模式下生成 39% 的 `type=3`，正好命中灰色分支，
夹在 `type=0` 的绿色中间就是「一段绿一段灰」（上游 issue #103 / PR #55）。

**`bdD` 也真的参与绘制。** 上面那条 `Math.abs(bdD1 - bdD2) <= 10°` 决定相邻点是否
合并成同一个顶点。我们原来所有点都写 `0.0`，等于恒等于「方向没变」，每个同类型段都会
被合并掉一个顶点。现在按相邻点算真实方位角（`Utils.initPoint(..., bdDirection, ...)`
→ `MyLocation.bdD` → `LineBean.bdD`）。

我们原来的 `rundata.py` 还有两个越界值：后 1/3 以 6% 概率撒 `[1, 1, 2, 8]`。`8` 不在
这个客户端的取值域里；`1/2` 则是在**没真的经过必经点**的地方断言「打过卡」。
现在改成：只有 `fivePoint.isPass` 为真、且最近轨迹点确实在那个点位上的，才写 `1/2`，
并加了一条生成期断言（`legal_types = {0, 1, 2, 5, 6}`），越界直接抛错。

我们自己看不见灰线，是因为 `readonly.parse_track()` 把这些字段丢了，前端只画单色线——
所以这条只能靠 payload 断言守，不能靠看界面发现。

**证据的边界**：颜色表和 `bdD` 合并规则来自 2026-05 的公开反编译快照
（`GaoDeRunPointControl.java`，classes8.dex）。手上这份 596 MB 的当前 APK 被网易易盾
加固，`classes.dex` 只有 30 个壳类，无法静态复核；如果哪天当前版本的配色变了，
以 `static_path` 式的实测为准，不要拿这段当永久真理。

---

## 11. 配速偏差用相关噪声，不用固定正弦（2026-10-27）

旧实现把配速写成两个写死的正弦：`0.025·sin(2π·3t) + 0.015·sin(2π·7t)`。
问题不在幅度，在**频率固定**：无论跑 15 分钟还是 45 分钟，频谱上永远是 3 个和 7 个
周期各一个尖峰。实测占频谱 18.9% / 10.7%，而相邻频点只有 5% 上下——这比「速度恒定」
更隐蔽，但同样是确定性指纹，而且换时长也甩不掉。

改成 OU 过程：24 s 相关时间、2% 稳态标准差；我们按 10 s 分桶，所以
`phi = exp(-10/24) ≈ 0.66`。这个 24 s 取自上游 PR #134 的配速目标相关时间，
但只借了这一个数字，没有移植它的运动模型。

同 seed 复测：3 周期 18.9% → 6.0%，7 周期 10.7% → 3.6%，与相邻频点（4 周期 5.5%、
6 周期 5.7%）齐平；主导成分变成 1 周期，也就是起跑加速 + 6% 疲劳衰减，这是真实跑步
本来就有的形状。总距离、总时长、总步数仍由 `d_scale` 归一，精确对齐不变
（0.1.4 起改为按公里分段归一，移除 `d_scale`，见 §15）。

**没做的**：PR #134 在 GPS 抖动之后校验观测点不越出围栏，这一条没吸收。8 个
`generate_run_data` 调用点里只有提交链路拿得到围栏多边形，其余是离线生成和自检；
只在一处加检查会给人「已经防住」的错觉。要做就得先把多边形贯通到生成器，
属于另一轮。

---

## 12. 提交前用服务端规则刷新本地闸门（2026-10-27）

`useMobilityTools` 不是服务端算的，是**客户端算的**。
`UploadFormatEntity.setSegmentJson()` 遍历每段 `AnalysisStateDB`，未达标比例
≥ `unqualifiedRate` 且 `isOpenTimeInterval` 为真时置 1、同时 `setStatus(1)`，
然后把 `segmentJson` 清空再上传——服务端拿不到明细，只能信这个字段，或者自己按
`speedPerTenSec` / `stepsPerTenSec` 重算。

我们原来把这个字段写死 `0`，而配速/步频的区间闸门依赖 `data/school_rules.json`。
那份文件只在用户**第一次打开「设置」页**时从服务器读一次（`app.js:954`），之后
不再刷新。后果有二：学校改了区间，闸门还在查旧数字；装好就没进过设置页，闸门
根本不存在，记录会一边声称 `useMobilityTools=0`、一边发出大量超区间窗口。

实测（学校区间取 5.5–8.0 分/公里，10 秒窗口）：

| 配速 | 超出区间的窗口 |
|---|---|
| 4.5 分/公里 | 100% |
| 5.5 分/公里 | 47% |
| 6.0 分/公里 | 0% |
| 8.0 分/公里 | 48% |
| 9.0 分/公里 | 100% |

现在提交前用**这次提交已经取到的** `runModePolicy` 刷新（不额外发请求）：
只覆盖服务器真正下发的字段，保留手填的 `max_per_day`，也尊重显式关闭的
`auto_fetch`。刷新后会写回 `school_rules.json`，下次打开界面也是新的。

**读不到规则时不硬拦**：学校不下发 `runRuleModel` 时，服务端自己也没有
`unqualifiedRate` 可以拿来算，硬拦只会误伤正常提交。这时把「本次未校验配速/步频」
放进提交结果的提醒里，用户看得见。

---

## 13. `user/info` 跟到 v70400，旧路径只做 404 回退（2026-10-27）

7.4.0（versionCode 408）的 dex 里只有 `api/v70400/user/info`；7.2.70 到 7.3.70
都是 `v70100`。这是三个版本之间**唯一一条变动的接口**——`save/record`、
`originalSign`、`runModePolicy`、`facecheck/save`、`records`、`get_one_record`、
`geevalidate` 全部逐字节相同，提交字段族（`allLocJson` / `fivePoint` /
`speedPerTenSec` / `stepsPerTenSec` / `useMobilityTools` / `unqualifiedRate`）
计数一致。

`user/info` 是判断 token 还活着的唯一探针，所以「路径没了」绝不能被读成「token
死了」：只在 HTTP 404/405 时回退 `v70100`。401 是服务端对 **token** 的判决，
原样返回；5xx 是服务端故障，换路径也救不回来，回退只会让每条失败探测发两次请求。

**证据边界**：7.4.0 是字符串层证据（易盾仍加固，dex 头里只有 1,061 条字符串、
30 个类），不是类级反编译；中文全部丢失，所以只能比较路由与英文/字段名。取包
校验信息与差异表在 `archive/reference/PROTO_DIFF_7.4.0.md`（`archive/` 被
`.gitignore` 忽略，不进版本库）。

---

## 14. 镜像按实测速度排序，不按 ping（2026-10-27）

`latest.json` 的资产现在带 `urls`（镜像在前、官方直链在最后），`url` 留给老客户端。

选择不能靠 ping：代理普遍屏蔽 ICMP，ping 通也不代表 HTTP 可用。也不能靠 HEAD：
不少 CDN 对 HEAD 走缓存，几毫秒就返回 200，GET 正文照样慢。

下载前对每个镜像发 `GET` + `Range: bytes=0-65535`，收到第一块就断开，用
`ttfb + 64KB / 首块速度` 排序。整轮探测 6 秒预算（连接 4 s / 读取 6 s），没答完的排到后面，全挂就
按清单顺序来。首块还要校验文件头（zip 是 `PK\x03\x04`，exe 是 `MZ`）——被劫持
的代理常返回一个 200 的 HTML 页面，等 sha256 失败要多下 76 MB 才知道。

选中只是**首选**：按顺序逐个下载，`size` / `sha256` 对不上就换下一个。所以坏镜像
最多浪费一次流量，装不进坏包。

这条在 0.1.4 生效。0.1.3 的二进制不含它——构建时 HEAD 还是 `e8ffc6f`，
包里的构建标识也如实写着这个提交。

---

## 15. 生成器按账号真实跑步重标定，并允许编辑每公里用时（2026-10-28）

只读拉取账号下 25 条 `state=ok` 记录的 OBS 对象，用「2026-09/10 人工跑 7 条」定标。
2025-12~2026-04 的 9 条是另一套特征（dt 4.6-7.7 s、CV 0.06、海拔恒 10 m、步频多为
115），像另一台设备或另一种记录方式，不参与定标。

| 指标 | 真实（9-10 月） | 旧生成器 | 现在 |
| --- | --- | --- | --- |
| 采样间隔 | 2.2-3.4 s | 固定 5.0 s | 2.2-3.8 s |
| 10 s 窗口距离 CV | 0.19-0.52 | 0.03-0.07 | 0.18-0.40（随配速增长） |
| 步频窗口 CV | 0.18-0.36 | 0.06-0.11 | 0.19-0.35 |
| 海拔跨度 | 19-23 m | 1 m | 4 km: 19-24 m；1.3 km: 11-14 m |
| 停顿窗口 | 0 或约 6% | 0 | 0 或 1-8% |
| 每公里步频 | 各公里不同 | 恒 150.0 | 138-164（含配速联动） |
| 默认步频 | 102-154 | 172 | 150 |

窗口 CV 按 `0.20*(pace/6)^1.5` 放大：同一条 4 km 记录，6'05" 时 0.19、7'25" 时
0.43、8'57" 时 0.52。固定定位误差在更慢的窗口里占比更大，这与实测吻合。

> 上表的采样间隔是 7.2/7.3 账号样本的定标。§19 起目标切到 7.4.0，生成器
> 默认采样改为 5 s（7.4.0 抓包 97/103 个间隔为 5 s），其余窗口/步频/海拔指标不变。

每公里配速不再由全局平滑曲线决定：

* 生成时按 `ceil(km)` 分公里，每公里独立抽一个约 ±6%（夹到 ±12%）的用时，
  强制相邻至少差 3%，再消除同秒；最后按比例缩放，合计严格等于 km × 配速；
* 每个公里内部单独归一化，保证「这一公里正好这么多米、这么多秒」。停顿由本公里
  吸收，不会改变该公里的用时；
* 海拔按距离定标：4 km 约 19-24 m、1.3 km 约 11-14 m。形状 = 3 个不同周期的
  正弦 + 慢漂移 + 0-2 段短坡（桥/台阶）+ 25 s 相关的气压抖动；粗糙项在缩放之后
  叠加，避免波形相消时把噪声放大成 ±20 m；
* 每公里步频不再恒定：给每公里一个 ±2.5% 的步频档位，步幅按 `speed^0.8`
  跟随配速（快公里步幅变长，步频只按 `speed^0.2` 小幅上升）。旧模型里步数严格
  正比于距离，laps 的 avgCadence 每一公里都会除回 150.0；
* `generate_run_data(km_times=...)` 接收 UI 回传的整表，生成器按表执行；
* `obs._cumulative` 改用 `locPoints[].totalDis`，不再按时间线性摊距离，否则
  `laps_json` 会把每公里配速重新抹平。

证据边界：这是保真度改造，不是「降检测」。10/23 通过的那条工具记录窗口 CV 0.026、
海拔 1 m，服务端照样 `isCheat=0`；目前唯一与标记完全分离的变量仍是 OBS 对象是否
存在。`qualification.py` 的软阈值同时按真实数据重标（配速波动 ≤65%、停顿 ≤15%、
爬升 ≤70 m），否则它会给自己的生成记录、也给账号里的真实记录报「注意」。

UI 侧在参数面板新增「每公里用时」卡片：预览时由 `preview_route` 返回 `splits`，
可直接改成 `6:03` 或 `6.05`（分钟），改完总配速跟着重算；卡片底部显示合计
距离 / 用时 / 平均配速。提交时把整表作为 `km_times` 传回，记录里的 laps 就是
这张表；不填时每公里独立随机，缩放后合计等于预计用时。

---

## 16. 0.1.4 发布：v0.1.3 作为回滚（2026-10-28）

0.1.4 的生成器改造见 §15。这版不删 v0.1.3：Release 保留，`latest.json` 新增
`rollback` 条目，指向 v0.1.3 的 zip，并列出 gh-proxy / ghfast / gh.llkk / 直连
四个地址。

回滚资产必须有自己的下载前缀。把 `haoxiangpao_v0.1.3_win64.zip` 拼到 v0.1.4 的
base 上会得到 `.../download/v0.1.4/haoxiangpao_v0.1.3_win64.zip`，是 404；所以
`make_latest.py` 增加 `--rollback-base-url` 与 `--rollback-mirror-base-url`，
默认仍沿用主 base（相对路径场景不受影响）。

发布资产：setup 56,035,019 B / `fff74439…a831`；zip 79,796,101 B / `556e459b…501c`；
v0.1.3 回滚 zip 79,749,401 B / `cb026f6b…24e39c`。装包实测：全新静默安装、
0.1.3→0.1.4 覆盖升级、静默卸载三条路径都保留 `data\`，`--check` 均返回 0。

---

## 17. 0.1.5 收尾：官方每公里表单位、无围栏/无点位校区（2026-10-29）

**官方 `laps_json.avgPace` 是分钟/公里，不是秒/公里。** 反编译的
`RunLapsFullAdapter.formatPace(double)` 先执行 `(int)(pace * 60)` 再格式化成
`i'rr"`；官方 `PluginableLocationManager.handleLapsData()` 也写
`setAvgPace(d / 60.0)`，其中 `d` 是秒。旧实现写秒，导致 6'00" 的公里在手机上
显示成 360'00"（用户看到的“200 多分钟”）。`obs.build_laps()` 已改为分钟，
两侧单位口径：`km_splits[].pace` / `preview_series` 仍是秒/公里（工具内部 UI），
OBS `laps_json` 是分钟/公里（官方 App 读取）。

**空围栏是合法配置。** 官方 `LocationGeoFenceHelper.isInArea()` 在
`geoFences` 为空时默认 `true`。旧实现把“围栏为空”当失败并阻止提交，导致
没有配置围栏的学校完全无法使用。现在的顺序是：围栏中心 → 本机保存的校区
坐标 → 手绘路线第一个点；三者都没有才停止。跑步页新增「设为校区」，把当前
地图中心写入本机会话。

**跑区先按账号校区匹配，匹配不到再用 `selectedPosition=0`。** 官方
`SportTypeViewModel.selectedPosition` 是静态 int，默认 0；只有
`RunningAreaDialog` 用户点击后才改变。`RunAreaModels.getSelect()` 在 1.3.0
反编译源码里只有定义、没有调用，所以工具不使用 `select`。

服务端返回的 `runAreaModels` 是当前账号的列表，但一个学生可能属于多个校区；
为了 A 账号（广软）和 B 账号（广州）各自自动落到自己的跑区，工具先用当前
账号的 `campusName`/`schoolName` 匹配 `runAreaModels[].name`，匹配不到才回退
官方默认 0。这是工具侧兼容层，不改变官方“按 position 取”的协议；显式下拉
选择仍然优先。多跑区时跑步页下拉框对应官方对话框，`valid == false` 的跑区
显示为灰色不可选；默认/匹配到的跑区无效时提交停止并提示重新选择。

拿到跑区后，用它的 `fenceIds` 过滤 `geoFences`，并保留该跑区拥有的**全部**
围栏，不再按名称/距离只挑一条；`runArea.id` 作为 `runAreaId` 发给打卡点接口。
没有 `runAreaModels`（或显式 `position == -1`）时使用全部围栏，任意一条包含
轨迹点就算在区域内（官方 `LocationGeoFenceHelper.isInArea` 的 OR 语义）。

`geoFence` 明确返回空列表时视为“该跑区没有围栏”；`geoFence` 为 `null` 或
缺少该段时不直接判“无围栏”，而是回退一次 `getGeoFenceForRun`（来源标记
`legacy-fallback`）。这是 2026-10-29 实测的冻结账号：`runModePolicy` 的
`geoFence`、`runAreaModels` 都是 `null`，但 `getGeoFenceForRun` 仍返回学校
围栏，且围栏中心与该账号缓存打卡点一致。`runModePolicy` 本身取不到时直接
停止提交，不复用旧围栏。

**打卡点按目标距离拉取，但提交复用已显示的那套。** 接口路径包含目标米数；
2026-10-30 实测 1000/1060/2500 m 每次各返回 5 个随机点（成员与 `isFixed`
都不同）。UI 画线时 3 s 防抖、变化 ≥200 m、最多 1 次/5 分钟；手输仍 900 ms
防抖。提交直接带地图上的点位；缓存按距离分键，10603 时允许同账号/跑区
任意距离回退（24 h）并标注来源。

**空打卡点默认阻止。** 反编译只看到 `requestFivePoint` 成功回调
`addAll(pointsResModels)`，没有“空列表自动转自由跑”的分支；官方真正的空点位
上传是 `RunFreePImpl`（serviceType=3 / sportType=4 / empty fivePointJson），
和普通计分跑不是同一条路径。工具现在 `parse_points(allow_empty=False)`：
预览可以显示“无打卡点（需确认）”，但提交默认停止，只有操作员显式勾选
“跳过打卡点（无点位学校，非官方自由跑）”才允许继续。空响应不写 300 s 缓存，
避免一次异常空响应当成学校配置。等有真实抓包证明某学校空点位可提交后，再改
默认行为并补回归测试。

**必经点以 `isFixed` 为准，命中半径 25 m。** 官方 `RunRanLocationManager`、
`RunSeqLocationManager`、`FivePointPlugin` 都是 `distance <= 25.0` 才算通过；
`isFixed == 1` 才是必经点（随机跑据此返回 type 2），本账号缓存里没有
`isMustPoint` 字段。工具现在同时保留 `isFixed` 原值、由它推导必经点，并把
轨迹/吸附/校验的命中半径统一为 25 m（吸附余量 20 m）。

**顺序跑按点位编号排序。** 官方 `SportRecord.getFivePoint()` 是
`orderBy("position", false)`，`RunSeqLocationManager` 每次取 `get(0)`；
服务端下发的数组可能无序（实测 `点位2/3/1/7/4`）。`policy == 1` 时工具按
`position`/`pointName` 数字排序，`fivePoint.position` 同步，避免按最近邻
走错顺序。

**冻结只提醒，人脸硬拦截。** `RandomConfigBean.freezeRunTime > 0` 时官方
“开始跑步”按钮是禁用的；工具按用户要求只记录警告并继续提交。
`RunRuleModel.faceVerify` / `runFaceCheckConfigDTO.enable` 为 true 时，工具
无法完成人脸校验，直接停止提交，不伪造 `faceCheck=1`。

**距离决定候选，但每次请求随机抽 5 个点（2026-10-30 实测）。** 同一账号、
同一锚点分别请求 `1000 / 1060 / 2500` m，三次各返回 5 个点，成员不同、
`isFixed=1` 的点也不同（池子至少 7 个）。所以：

- 画线自动改目标距离时**不逐 10 m 重拉**：3 s 防抖、变化 ≥200 m、最多
  1 次/5 分钟；手输/快捷/计划填入仍走 900 ms 防抖。
- **提交复用地图上显示的那套点**（`points`），不再按当前 km 重新抽；
  `points_distance_m` 只作为 UI 没有点位时的查询兜底。
- 10603 时按「精确距离任意锚点 → 同账号/跑区任意距离（24 h）」回退，结果
  标记 `stale` 并写明来源距离；UI 与提交 note 都会标注。

**跑区可在跑步页切换，并记住每个账号的选择。** 官方 `RunningAreaDialog` 会在
多跑区时让用户选，并拒绝点击 `valid == false` 的跑区。工具把已取到的
`runAreaModels` 渲染成下拉框（少于 2 个跑区时隐藏），无效项显示为灰色不可选；
选择后带 `run_area_id` 重拉围栏/点位，提交也带同一个 id；id 无效时退回默认跑区
（index 0）并提示。缺 `valid` 字段视为“未知”，只有显式 `false` 才无效。
选择按 `uid:unid` 存进 `ui_state.json`，下次登录/重启由后端重新校验后恢复。

**「设为校区」给无围栏或多围栏学校。** 单围栏时用服务器围栏中心；没有围栏
或检测到多个围栏、且本地没有可判定中心时，按钮才显示。多围栏时手动中心
必须落在某条围栏内才会采用，避免把另一校区坐标当成当前校区。

**围栏读取异常一律停止提交。** 只有 `FenceNotConfigured`（学校确实没围栏）
才允许用已保存坐标或路线首点兜底。网络/解析等一般异常曾经只加一条备注继续，
但此时 `fence` 是空的，后面的区域校验整段被跳过，可能提交一条围栏外轨迹。
现在一般异常直接失败，不把路线首点写成校区，也不调用 `_save_center`。

**`runModePolicy` 只取一个完整回复。** 官方 `RunPolicyViewModel` 缓存的是
单次响应；旧实现按 key 合并 mode 1/2，可能拼出“mode 1 的跑区 + mode 2 的
围栏”。现在优先采用同时带 `runAreaModels` 和 `geoFence` 的那一条，两个
模式都不完整时选围栏那条，不再跨回复拼字段。

---

## 18. 0.1.8：网络诊断移出检查表、服务器时钟、官方 address（2026-11-04）

**网络出口只是提醒，不能挡提交。** 旧实现把 `LIVE.preflight` 当成检查表的一行：
没有自检、`fetch_ip_info` 被拦截、或者出口城市不同，都会让 `done !==
checks.length`，提交按钮和 dock 一直是未就绪。运营商省级 NAT 会让大量正常
用户永远“不同城”，而官方客户端根本不看电脑的出口 IP。现在：

- `refreshChecks()`、`done === checks.length`、dock、提交按钮都不再引用网络项；
- 「跑步 → 提交」保留独立卡片「网络诊断（仅提醒）」，只在用户点击时联网；
- `ip_city_matches` 显式三态：`true` / `false` / `null`（无法确认），三者都
  不改变提交状态；
- 时间窗、开始时间等真实检查仍然保留；网络失败不再被叫成“自检失败”。

**时钟偏差由软件吸收，不让用户改系统设置。** 旧提示“请校准系统时间”会把
一个系统设置问题推给用户，实际还会造成新的 issue：用户不知道怎么改、改了
别的软件又出问题。服务端校验信封 `timeStamp`，本机快几分钟时生成的记录还会
落在“未来”。现在：

- `RealClient.server_time()` 读 `GET api/v3/getserver/gmttime`；
- 响应体为空/被拦截时回退到同一响应的 HTTP `Date` 头，仍是服务器时钟；
- 用请求往返中点估算偏移（`server - (t0+t1)/2`），避免把单程网络延迟算进去；
- `replica.domain.clock` 保存偏移，并以 `time.monotonic()` 锚定：即使用户中途
  改了系统时间，进程内的时间轴也不会跳；
- `safety.now_ms()`、`netsec.now_ms()`、`nskpure.now_ms()`、请求头
  `timeStamp`、`tokenSign`，以及 `runes` 的兜底全部改走这个时钟；
- 偏移缓存到 `data/clock.json`，12 小时内复用；每次提交造数前强制重测一次；
- 系统时间**从不修改**，提示改成“已自动改用服务器时间……不需要修改系统设置”。

读不到 `gmttime` 时按本机时钟继续，提交结果里明确标注“未能校准”，不阻断
提交。官方 App 的冷启动和结算前也都会调这个端点，Inverx 7.3.92 的
`running/client.py` 同样用它当 `now`，所以这不是自造行为。

**`address` 按官方链路取起点城市。** 旧实现用“校区城市表”，广工这类名字里
没有城市的学校会得到 `None`，最后退化成“学校名 + 校区名”提交。反编译
`OutdoorRunViewModel.saveStartPointToDb()` 显示官方是：

```
起点 MyLocation.gLat/gLng
  -> MapLogKt.getCity(context, gLat, gLng, callback)
  -> AMap RegeocodeAddress
  -> SportRecordManager.setAddress(city)   // null -> ""
```

现在 `riskcheck.address_for_start()` 按同一顺序：AMap 逆地理（直辖市回退
`province`）→ 校区/学校名中的城市 → 空串。提交和 CLI 都用轨迹首点的
`gLat/gLng`，不再把学校名当城市。没有高德 Key 时只会回退到名字里的城市，
识别不到就提交空串；这比伪造一个城市更接近官方行为。

**记录详情跟到 7.4.0。** `readonly.DETAIL_PATHS` 现在是
`v70320 → v70260 → v61200`。7.4.0 的 dex 字符串池仍是 `v70320`
（`PROTO_DIFF_7.4.0`，7.3.70 起未变），7.2.x 仍答 `v70260`；`v61200` 是
更早的 APK 路径，放最后。三个都失败才报错，错误信息带三边原因。

**Inverx 的两项只读接口暂不接入。** `api/v1/barrierFree/getBarrierFree`
（discovery 主机）和 `api/creditscore/v70390/popup` 都是客户端 UI/遥测：
前者是安卓无障碍服务黑名单，后者是信用分弹窗。它们不参与
`save/record` 的字段或签名，读它们不会让记录更容易通过，反而多一个跨主机
请求面；真正与提交相关的 `api/v78/cheat/cheatlist` 已经以只读方式接入。
因此 v0.1.8 明确暂缓，不假装“吸收了”。

---

## 19. 0.1.8 切到 7.4.0：27 字段、配速、30 s 分段（2026-11-04）

用户确认当前官方 App 是 **7.4.0**。7.2/7.3 的 22 字段、字符串数值、`lat/lng=0`
和 2 s 采样只保留为历史对照，不再作为 v0.1.8 的目标形状。

| 项 | 7.4.0 目标 | 依据 |
|---|---|---|
| 轨迹点字段 | **27 个**，没有 `steps` | dex `MyLocation` 30 个字段去掉 DB-only 的 `im/mp/ra`；Demacia 10-26 抓包 103 点 × 27 字段 |
| 字段类型 | `double/float/int/long`，不是字符串 | 7.4.0 `MyLocation` / `AnalysisStateDB` 声明 |
| `lat/lng` | `-1.0` 占位，真实坐标在 `gLat/gLng` | `MyLocation` 构造器写死 `lat/lng=-1`；Demacia 10-26 抓包 103/103 点 |
| OBS 对象 | **11 个键**，含 `extension_json`（空串）；`segment_json` 也是空串；**只写日期式 key** | Demacia 10-26 抓包 11 字段、1 条 PUT；少一个键或写两条 key 本身就是差异 |
| `speed` | **配速 min/km** | 官方 7.2/7.3 记录 667/667 点满足 `avgSpeed = 16.6667 / speed`；`avgSpeed`/`bdS` 是 m/s |
| `gainTime` | 墙钟字符串 | 官方记录 `%Y-%m-%d %H:%M:%S`；`totalTime` 是活跃秒，两者本来就不是同一个时钟 |
| `id` / `queueNum` | `id=1..N`；点队列每 30 s 加 1 | 官方 4 km 记录 `queueNum` 在 0/15/30/45… 点跳变；窗口队列 `0,0,0,1,1,1…` |
| 起终点 | index 0 `type=5`，末尾 `type=6`，异常点可为 `type=-1`，`locType=0` | 官方记录首点全零累计 + `type=5`，末点 `type=6` |
| 分段 | `AnalysisStateDB` 11 键、30 s、`avgSpeed` 配速 | 官方 48 段记录逐段验证 `distance/(endTime-startTime)` 与 `avgSpeed` 一致 |
| `geoFencesJson` | 真实围栏数组 `{id,name,points:[{lat,lng,glat,glng}]}` | `GeoFencesBean` / `GeoFencesPointBean`；官方记录围栏点 `lat/lng=0`、`glat/glng` 有值 |
| `isCheat` | 非 0 参与状态判定 | 10-13 记录 `isCheat=1`、列表 `status=2`、详情多一条“运动数据有问题” |
| `count` | 53–79，均值 ~67 | Demacia 7.4.0 真机统计：整数 53–79，均值 67.6，相邻差中位数 2 |
| `state` / `bdG` | `1` / `1` | 7.4.0 抓包默认；不再随机 1/2/3 |
| 采样 | **5 s 为主 + 抖动** | 7.4.0 抓包 97/103 个间隔是 5 s；2 s 是本账号 7.2/7.3 的设备行为 |

**不采纳的两项**：Inverx 的 `fivePoint.isPass=true` 硬编码（我们按 25 m 实际
通过判定）和 `maxRunTime/minSteps` 猜测（没有官方依据，保持不猜）。

**分段仍在线上发空串。** `ObsClient` 的 `segment_json` 和
`UploadFormatEntity.setSegmentJson()` 在 7.4.0 里最终都置空（Demacia 抓包的
OBS `segment_json` 解码也是空串）。上面的 30 s 分段只用于本工具的内部预览/
自检，不改变线上报文；不要因为它“看起来像”就塞进 OBS。

**证据边界**：本账号没有 7.4.0 官方新记录，上述 7.4.0 结论来自
“Demacia 7.4.0 抓包 + 7.4.0 dex + 本账号 7.2/7.3 官方记录”三份材料。
5 s 采样、`count/state` 的具体分布、`fivePoint.lat/lon` 是否在真机 7.4.0 上
始终非零，仍属于**有依据的假设**，不是本账号实测。`geoFencesJson` 的
`freedomShowFence` 保持 `false`；没有跑区时 `runAreaId` 保持 `-1`
（有服务端跑区时仍沿用官方 `getRunAreaId()` 的值）。计分 `runModePolicy`
的围栏/跑区仍要等 06:30–22:00 才能只读确认。

## 20. 免责声明强制确认与防倒卖提示（2026-11-04）

| 决策 | 理由 | 边界 |
|---|---|---|
| 登录前必须勾选《免责声明》 | 用户明确要求；未勾选不能登录 | 只在 UI 层强制，不改变服务端协议 |
| 接受状态写 `ui_state.json` | 随机端口会重置 localStorage；`data/` 才是持久源 | 记录版本号和接受时间，不写账号文件 |
| 旧会话先本地退出再要求勾选 | 升级后不能靠旧 token 绕过确认 | 只清本机 token；服务端注销走后台尽力而为 |
| 登录页/侧栏/关于常驻防倒卖提示 | 软件永久免费，付费渠道均为第三方倒卖 | 文案硬编码，不依赖远程配置 |

## 21. v0.1.8 P0–P2：审计、核验、真实性与离线工具（2026-11-04）

| 功能 | 决策 | 边界 |
|---|---|---|
| 形状审计 | `application.audit` 独立检查 27 字段、OBS 11 键、窗口/分段/圈守恒；生成数据离线，记录只读 | 只证明与已知证据一致，不证明服务端接受 |
| 错误解释 | `application.errors` 纯函数映射 11016/10003/10603/404/超时/空 200 | 未归类的错误明确输出 `unclassified`，不猜 |
| 只读核验 | `application.verification` 读详情并给 `ok/warn/issue/unknown` 时间线 | 绝不重发、绝不 PUT；只读失败也返回 `unknown` 而不是假成功 |
| OBS key 模式 | 默认 `single`（官方 7.4.0 抓包 1 条 PUT）；`legacy` 才写日期+rrid 两条 | 旧服务端兼容用，默认不增加指纹 |
| 暂停建模 | `pause_profile` 默认 `none`；`auto/manual` 只把暂停加到墙钟 `gainTime` 与 `stopTime`，`totalTime` 保持活跃秒 | 暂停期间窗口仍是活跃时钟；不是 7.4.0 真机逐点复现 |
| GPS 档案 | 默认 `urban`；`clean` 固定 60/5；`tunnel` 低卫星+高精度半径，保留点数以保证守恒 | 不是实际隧道轨迹，只是可复现的传感器质量档 |
| 坡度联动 | 只有提供海拔模板时才生效；按每 1% 坡度约 ±2.5% 速度重新分配每公里用时 | 没有模板不发明山丘；总时长保持不变 |
| 多围栏 | `points.match_fence()` 用锚点/路线首点匹配；匹配不到抛 `FenceAmbiguous`，不取第一个 | CLI 与 UI 共用；没有锚点时要求人工确认 |
| 预设/统计/FIT/诊断包/巡检 | 全部本地或只读；预设写 `store_dir`，统计只读，诊断包自动脱敏 | 巡检不提交，诊断包不含 token/密码/完整坐标/签名 URL |

## 22. 旧版共享设备指纹一次性迁移（2026-11-05）

| 决策 | 理由 | 边界 |
|---|---|---|
| 不再把 `LEGACY_DEVICE` 冻结为旧会话的指纹 | 该常量被所有旧安装共用，且 `x86_64`/`isRoot=1`/机型系统像素不自洽；服务端会看成「单设备多账号 + 模拟器/root」 | 旧值只保留用于识别和备份，不能保存、不能用于登录/提交 |
| 旧共享指纹自动迁移为唯一、自洽的新指纹 | 共享 DeviceId 无法通过继续提交来“养回来”；唯一设备身份是恢复环境可信度的前提 | 迁移前备份 `device_profile.backup-*.json`；`save_profile` 拒绝再次写入共享常量 |
| 迁移后强制退出本机登录 | 旧 token 是给旧设备签发的；换设备后继续用同一 token 会让服务端看到“同一 token 中途换机” | 只清本机 token，服务端注销尽力而为；重新登录后登录/提交/OBS 使用同一新指纹 |
| 字段不自洽但 DeviceId 唯一的旧指纹只修字段、保留 DeviceId | 换 DeviceId 的信号比修 CPU/root/像素更大；能保留主身份就保留 | 只有共享常量或 `legacy-default` 才生成新 DeviceId |
| 启动时检测并迁移，CLI/保活同样拒绝旧 token | GUI、CLI、保活共用一份 `data`；漏一条路径就会继续发旧环境 | 迁移通知写 `device_migration.json`，登录成功后清除 |

## 23. v0.2.1：`stepDistance` 改为累计步幅距离（2026-11-09）

| 决策 | 理由 | 边界 |
|---|---|---|
| 轨迹点 `stepDistance` 改为累计值 | 参考项目 `DemaciaSportsWorldTool` 的 L 项：官方 `MyLocation.stepDistance` 是累计步幅距离，客户端用相邻两点相减得到 10 s 窗口值；整条 4 km 记录全 0 是最容易被对拍的破绽 | 用 10 s `stepsNum` 窗口在活跃时钟上积分，再乘全程 `juli / total_steps` 的步幅估计；末点≈`juli`，逐点单调不减 |
| 不补 `accuracy` / `hasAltitude` / `steps` | 7.4.0 dex 的 `MyLocation` 确有 30 个字段，但 `im/mp/ra` 只有 `@Column`、没有 `@SerializedName`，Gson 实际只发 27 键；10-26 抓包 103/103 点也是 27 键。repo 2 的 `wire.rs` 把 DB 字段也算进了线上载荷 | 保持 27 键；如果未来拿到 7.4.x 真机载荷明确带这 3 键，再按证据加 |
| OBS 与提交体共用 10 s 窗口，距离精度统一 4 位 | 参考项目 K 项：两处各算一遍或精度不同，服务端把两个载荷对起来就能看见矛盾 | OBS `_obs_windows` 不再把 `distance` 降到 2 位；回归逐项比较提交体与 OBS 的 `distance` / `stepsNum` 和窗时长合计 |
| 点结构继续由 `application.audit` 自检 | 27 键、`stepDistance` 累计、窗口守恒都是离线可验证的形状约束 | 只证明与已知证据一致，不证明服务端接受 |

## 24. v0.2.1：公告改为数据驱动模块（2026-11-09）

| 决策 | 理由 | 边界 |
|---|---|---|
| 公告内容放 `application/announcements.py` | 以后换文案、换二维码、加第二条公告只改一个 catalog，不碰 `index.html` / `app.js` | 公告只是本地 UI，不影响协议、账号或提交 |
| 前端 `js/announcements.js` 只做通用渲染 | 图片、正文、可选群号/复制按钮、已读状态都可以由数据描述 | 不把远端 HTML 直接插入页面，文案用 `textContent`；图片只接受 `assets/<file>` |
| 已读状态按 id 存 `announcement_seen` | 同一条公告只弹一次；改内容时把 `version` 加 1 即可再次提示 | 存在 `data/ui_state.json`，不依赖随机端口上的 localStorage |
| 设置 → 关于保留入口 | 用户关掉后仍能主动打开，不靠“再弹一次” | 入口用 `data-announcement-id` 绑定，公告 id 变化时同步改按钮 |

## 25. v0.2.2：`policy` 是规则集，不是风险标记（2026-11-10）

| 决策 | 理由 | 边界 |
|---|---|---|
| 记录详情的 `policy` 单独显示为「规则集」 | 提交体的 `policy` 是服务器下发的规则集编号（1 = 学分跑），详情接口原样回显；旧前端把所有非零 `policy` 当风险标记，导致 11/05 后正常记录被标红（issue #14） | 只改 UI 展示，不改提交体、签名或服务端判定 |
| 风险标记只读 `isCheat` / `stepFreqStatus` / `status` / `normalRun` | 这些才是服务端的反作弊字段；`policy=1` 本身不代表被清算 | 服务端反作弊是回溯性的，最终以服务端记录和原因列表为准 |
| 详情页字段改名 `rdPolicy` → `rdPaceBand`，新增 `rdRulePolicy` | 旧 ID 把“配速区间”和“规则集”混在一起，后续维护容易再次误读 | 静态回归锁定：`if(+x.policy) risk.push` 不能重新出现 |

## 26. 设备设置集中到设置页，新增自定义设备（2026-11-11）

| 决策 | 理由 | 边界 |
|---|---|---|
| 设备指纹的查看、更换、自定义统一放「设置 → 设备指纹」 | 账号页只保留登录/会话信息，设备相关操作不再分散；高级操作都在同一个折叠区 | 账号页保留一个跳转按钮，不重复渲染设备控件 |
| 自定义设备开放机型/系统/双分辨率/CPU/DeviceId | 固定机型表选项太少，覆盖不了新机型；改为自由填写，`audit_profile` 对 `source=custom` 改用平台规则（竖屏、比例、iOS 2x/3x、CPU 家族）校验 | 仍拒绝旧版共享 DeviceId；不提供“跳过环境自检”开关 |
| 先校验候选指纹，再退出登录 | 输入错误（未知机型、系统不匹配、DeviceId 非法）不应该把用户踢下线 | 校验通过才 `real_logout(local_only=True)`，然后备份并写入 |
| 自定义指纹写入 `source=custom`，备份旧指纹 | 与 rotate 一样可回滚；UI 能区分“随机更换”和“手动定义” | 保存后强制重新登录，旧 token 不会被新设备复用 |
| `real_get_device_options` 只提供示例默认值 | 前端不复制常量；自定义表单的像素/CPU 默认值来自这里，但字段可编辑 | 返回值不含完整 DeviceId，只含脱敏摘要 |

## 27. 编辑杂志风：paper 皮肤重做（2026-11-11）

| 决策 | 理由 | 边界 |
|---|---|---|
| 保留内部 `paper` 键，外观改为“编辑杂志” | 老用户保存的 skin 就是 `paper`，换键会让所有人回到旧外观或触发迁移；重做同一个键可以无缝生效 | 其它三套皮肤保留，不改 token |
| 新增 `css/editorial.css`，在 `skins.css` 之后加载 | 只覆盖 paper，不动基础布局和另外三套皮肤；所有规则都带 `html[data-skin="paper"]` 前缀 | 只改视觉，不改 DOM 结构、路由或业务逻辑 |
| 暖米纸 `#f9f8f6` + 墨黑 `#1c1c1c` + 石墨 `#6b7280` + 陈玫 `#816d70` | 颜色只做一层暖底和一个 2% 面积的点缀；层次靠透明度和 1px 线 | 不再使用纸张噪点、荧光笔、硬阴影、圆角 |
| 标题衬线、正文无衬线；登录按钮用 Uiverse `brown-otter-21` 的黑框 key 交互 | 杂志感来自字号级差和留白；登录页保留一个标志性按钮，工作台主按钮改为安静的细线描边，跑步提交按钮单独用 `doniaskima/btn-23` 的滚动文字 | Uiverse 组件 MIT；`app.js` 只补 key / marquee 节点，不引入依赖 |
| 总览标题右侧和「我的」头像区加入量化线场（`line-field.svg`） | 把品牌标记采样成 1–4px 四档竖线，给空白区一个可识别的纹理，而不是把动效铺满全站 | 只出现两处；`prefers-reduced-motion` 下停止漂移；不透传到其它页面 |
| 新增 `manuscript` 皮肤，保留原来的「手稿 × Notion」方向 | 编辑杂志版占用了 `paper` 键，但不能因此丢掉 10 月定下的楷体 + 荧光笔 + 手绘边框；把它作为第五套皮肤放回外观页 | 旧 `paper` 用户不迁移；`manuscript` 直接复用 app.css 基线，不加 editorial 覆盖 |
| 只覆盖概览/账号/跑步/路线库/记录/我的/设置和登录面板 | 这轮覆盖用户点名的页面；公告和开屏留给后续视觉确认 | 开屏动画 `#bootSplash` 保持原样；`prefers-reduced-motion` 下所有新动效关闭 |
| 地图层叠加账号水印 | 截图/录屏能追溯到登录手机号；低透明度斜排重复，不遮挡底图 | 只覆盖 `#map` / `#routeMap` / `#rdMap`；`pointer-events:none`，z-index 250 位于瓦片之上、路线/标记之下；无会话或未登录时隐藏 |

## 28. 核心协议迁移 Rust（2026-11-11）

| 决策 | 理由 | 边界 |
|---|---|---|
| NetSecKit 信封、tokenSign 与 GeeTest v4 `w` 迁入 `rust/hxp-core`，Python 保留回退 | Python 字节码即使经过 Nuitka 也仍可被分析；把盐、PAES、RSA、动态 key 和信封顺序放进 native，提高静态分析与补丁成本 | Rust 不是不可逆向；服务端校验仍是最终边界，客户端只提高门槛 |
| 用常驻 sidecar（行式 JSON）而不是 PyO3/ctypes | 进程边界清楚，崩溃不会带崩 UI；打包只需复制一个 exe；不绑定 Python ABI | 每次调用有 IPC 开销；只传最小参数，不把 token 或 data/ 交给 native |
| `JHP_RUST_CORE` 自动探测，`0` 强制 Python，`1` 要求 native；Nuitka 构建默认置 `1` | 源码运行/CI 没有 Rust 也能跑；排错时可以一键回到 Python；发布包不会静默退回可读实现 | 打包脚本缺 cargo 直接失败；frozen 构建丢 exe 时第一次加密调用即报错 |
| GT4 只迁字段构造、PoW、AES/RSA，不迁图像识别 | `w` 的字节布局和动态 key 是最容易被逐字节抄走的部分；图像 Canny/ZNCC 调参频繁，留在 Python 更好排错 | `gt4_image.py` 继续纯 Python；`build_w` 的 modulus 由 Python 传入，native 不内置私钥/公钥选择逻辑 |
| 上传体字段映射迁入 native，轨迹生成留在 Python | 上传体是服务器直接评分的对象，字段顺序、嵌套 JSON 和 `speed` 两次取整错一处就静默返回 10003；生成器随机过程多，整体迁走会放大回归面 | `build_upload_entity` 只接收 run/ctx，不读本地文件；`_build_upload_entity` 保留 Python 参考实现，两边共用 fixture |
| OBS 轨迹对象迁入 native，旧点位兜底留在 Python | 回放页读的是 OBS 对象，27 键点列、30 秒窗口和分段表要与提交体交叉一致；`wire_points` 路径覆盖全部正常生成 | `conv_point`/`_cumulative` 的几何兜底只为手写测试载荷保留；native 只接 `wire_points` |
| 设备指纹生成与 schema 校验迁入 native，文件与迁移留在 Python | 机型/系统/分辨率必须成对，服务端按这套指纹聚账号；收紧 schema 能直接减少「看起来像模拟器」的档案 | I/O、legacy 迁移、`audit_profile` 和 UI catalog 仍在 Python；`JHP_IOS_CHANCE` 由 Python 决定后传入 |
| 跑量核心（配速曲线、10 s 窗口、30 s 分段）迁入 native，分段/几何留在 Python | 提交体与 OBS 都会按这些列交叉校验；核心列同源生成后，距离、步数、分段不再各算一遍 | Python 用 `getrandbits(64)` 播种，Rust 用自己的 PRNG；`_pace_core_py` 保留回退，随机流不要求与 CPython 一致 |
| 下一批只考虑 rundata 生成器、device_profile | 这两块体积大、状态多，需要先把随机语义和纯函数边界钉死再迁 | 按 `45-native-core.md` 的顺序推进，每块保留 golden vector |

## 29. 一号一设备绑定（2026-11-12）

| 决策 | 理由 | 边界 |
|---|---|---|
| 用 Go sidecar `go/hxp-guard` 管本地绑定，不塞进 Python | 绑定是跨账号的持久状态，独立进程便于单测、重置和后续替换；Python 只做编排 | 本地文件可删，属于防切换/降摩擦，不是强 DRM；服务端才是最终边界 |
| 第一个登录成功的账号拥有机器，退出不释放 | 用户描述的规则是“退出再登别人号”要阻止；退出即释放等于没有绑定 | 同账号重复登录、换设备指纹均放行 |
| 只存账号哈希 + 脱敏提示，机器 ID 用 `MachineGuid` 派生 | 不把手机号写进新文件；机器指纹变化不影响绑定 | 无系统机器 ID 时用 `data/install_id` 回退；拷到新机器视为未绑定 |
| 状态文件带 HMAC，同机篡改 fail closed | 直接改 JSON 换账号是最低成本的绕过；损坏/篡改时给笼统提示 | 不能防删除文件；重置入口只在 CLI，不在 UI |
| 登录前 `check`、成功后 `bind`，自动重登和 CLI 同样走 | 只在 UI 拦会被绕过；`bind` 是并发下的最终执行点 | 守卫二进制缺失时源码运行放行；frozen 构建由打包脚本保证存在 |
| 提示固定为「为防止恶意使用软件，软件请一号一设备绑定」 | 用户明确要求；文案不暴露绑定账号、设备 ID 或内部实现 | 具体绑定账号只在日志/诊断里脱敏显示，不进入错误弹窗 |

## 30. 四层设备凭证（Phase 2，2026-11-12）

| 决策 | 理由 | 边界 |
|---|---|---|
| C++ shield 做成独立进程，不做 DLL/ctypes | 进程边界让崩溃、调试器和补丁都留在 native 侧；Python 只通过二进制帧说话，没有可读导出面 | 每次登录多一次短命进程；帧格式错误直接 kill，不重试 |
| `seal` 与 `check` 必须在同一个 `hxp-shield.exe` 进程内完成 | token 的本体只在该进程的 ring 里；换进程 check 必然为 0，所以 dump 出来的 token 不能拿去别处验证 | 不允许缓存 token 到磁盘或跨登录复用；C++ 的 `seal` 响应会回到 Python，只在同一个 `Popen` 上立刻 `check`，帧缓冲和 token 视图在 `finally` 清零，不写日志、不落盘 |
| token TTL 固定 3 秒 | 登录/提交是即时动作，3 秒足够覆盖一次派生 + 校验，又短到无法成为长期凭证 | 过期即拒绝；不续期、不落盘；系统时钟大幅回拨也会失败（fail closed）。Python 的 4 次帧读预算合计 2 秒，小于 TTL |
| Go `attest` 只输出 32 字节片段，不返回五路原文 | 机器指纹片段本身可识别，不能让 Python、日志或诊断包拿到；置换 + SHA-256 + nonce keystream 后只剩不透明字节 | 任一路采集失败整体失败；CLI 只保留数字 op，不打印片段 |
| frozen 缺任一层直接拒绝登录，源码缺层跳过 | 发布包丢了 `hxp-guard` / `hxp-core` / `hxp-shield` 就是加固失效，不能静默降级；源码 checkout 和 CI 没有 native 产物时必须还能跑 `pytest` | 错误统一为「设备校验未通过，请重启软件后再试」，不暴露层名/路径/原因；`JHP_SHIELD=0` 只用于本地排错 |
| 登录、自动重登、CLI 三条入口共用 `shield.require()` | 只拦 UI 的校验等于没有校验；保活自动重登同样会拿到新 token | 不改协议字段、提交体或签名；校验发生在网络登录之前 |
