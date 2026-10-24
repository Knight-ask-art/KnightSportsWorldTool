# 好想跑 · 文档

纯协议复刻「运动世界校园」的跑步提交链路：登录 → 拉点位/围栏 → 画路线 → 造数 → 签名 → 提交 → OBS 回放 → 只读查询。
不依赖模拟器，不依赖抓包重放。

> 代码在 `app/`，文档在 `docs/`，过时文档在 `archive/`。
> 技术演进主日志（最权威、按轮次记录）是 `reverse-engineering/unidbg/PORT_FROM_NEKO.md`。

---

## 5 分钟上手

```powershell
cd 好想跑\app

# 1. 打开 Web UI（默认只监听 127.0.0.1）
python run.py --port 65520

# 2. 只造数、不提交（不需要账号，先看数据长什么样）
python -m replica.pipeline --token X --uid 1 --km 2.0 --dry-run --no-preflight --no-history

# 3. 跑一遍离线回归（不联网、不提交）
python -m replica.selfcheck
```

> 在 `app/` 里用 `python -m replica.pipeline`；在仓库根目录用
> `python -m app.replica.pipeline`。两种都行，别在 `app/` 里跑后者。
> `app.replica` 只是仓库根目录下的命名空间路径；新代码统一从 `replica` 导入。

启动后先显示**登录页**；没有有效本地会话时必须登录后才能进入工作台。
进入后的顺序：**总览 → 点位/围栏 → 画路线 → 节奏 → 提交 → 记录**。
围栏和点位都按 token 实时拉；登录成功会自动开启 probe-only token 保活（保活密码只在内存）。登录页可选「记住密码」，勾选后用 Windows DPAPI 加密保存在 `data/saved_login.json`，不勾选不保存。

---

## 文档地图

| 文档 | 回答什么问题 |
|---|---|
| [usage.md](usage.md) | 怎么装、怎么跑、UI 每个按钮干什么、命令行全部参数、环境变量 |
| [api.md](api.md) | 调了哪些接口、请求/返回长什么样、字段单位、OBS 十个键、轨迹怎么解 |
| [operations.md](operations.md) | 本地文件都在哪、每日上限/深夜闸门/回执怎么工作、报错怎么查 |
| [decisions.md](decisions.md) | 为什么这么设计、哪些结论有源码/实测出处、哪些**还没验证** |
| [history.md](history.md) | 这一路踩过什么坑、旧文档为什么被封存、去哪找证据 |

会话交接（按时间写，不是手册）：[`handoff.md`](handoff.md)
技术主日志（证据最全）：[`../reverse-engineering/unidbg/PORT_FROM_NEKO.md`](../reverse-engineering/unidbg/PORT_FROM_NEKO.md)
封存文档索引：[`../archive/README.md`](../archive/README.md)

---

## 当前状态（2026-10-24）

**离线回归**

```
23/23 模块自测     PASS
pytest（tests/）   159 passed
私有 corpus tests   3 passed
legacy_bridge_suite  38 checks passed
legacy_http_suite    23 checks passed
validate_sim      1/2/3/5 km PASS
```

**真实链路**

| 能力 | 状态 |
|---|---|
| 登录（手机号+密码，本地过极验） | 代码完成；线上最后一步卡在 `10121 账号已在其他设备登录` |
| 拉点位 / 围栏 | 线上拉通过 |
| 提交 `save/record` | 历史实测被受理；最后一次回归**未做** |
| OBS 轨迹双 key 上传 + 回读 | 本地模拟通过，线上待回归 |
| 只读查询（记录/详情/周汇总/违规/学期/排行） | 代码完成，未用真实 token 验证 |
| token 保活 | 定时 `user/info` 探测；失效标记；可选内存自动重登；`disabled` 旧账号不探测 |
| 补跑（今天..3 天前，09:00–22:00） | 代码完成；服务器是否接受历史日期**未验证** |
| 每日次数提醒 | 按所选日期查服务器记录；超学校上限时只给 `warn` 提醒，不阻止提交 |
| v0.1.1 稳定包 | UI 点位按校区固定范围拉取（提交按目标距离补点）、回执只读核对、网络自检、记录轨迹地图、日历次数、学期进度、路线库落盘、14 天计划、诊断包、学校规则自动读取、个人主页/违规自查 |
| 设备指纹 | 每个 `data` 目录随机一次（Android / iOS，`JHP_IOS_CHANCE` 默认 30）；登录/保活/提交/OBS/注销共用；旧数据冻结原指纹；更换需显式 rotate 并备份；iOS 提交链未实测 |

> 最后一条是硬限制：没有可用 token 时，所有真实接口都会明确报错，
> 不会伪装成空数据。详见 [operations.md](operations.md#3-排错表)。

---

## 三条安全边界

1. **不重试提交**。网络超时=未知状态，回执会锁住该时段，宁可少跑一次也不重复入库。
2. **本地闸门**：默认每天最多 2 次、23:00–06:00 拒绝提交（`--no-limit` 可关）。
3. **凭据只留在本机**。`app/data/` 和 `archive/unidbg-era/SESSION_MUMU_TOKEN_20261012.md`
   含账号/令牌痕迹，不要提交、不要外发。

---

## 约定

- 本文档里的路径默认相对 `好想跑/`。
- 距离单位：接口层是**米**，展示层是**公里**；时间戳是**毫秒**；配速是**分钟/公里**。
- 坐标：内部统一 **GCJ-02**；报文里 `lat/lon` 是 **BD-09**；`gLat/gLng` 是 GCJ-02。

---

## 品牌与命名

| 东西 | 名字 |
|---|---|
| 品牌 / 产品 | **好想跑** |
| 根目录 | `好想跑/` |
| exe | `好想跑.exe`（`build.ps1` 输出到 `dist/好想跑_v<版本>/`，不覆盖旧 `dist/好想跑/` 的 live data） |
| 窗口标题 | 好想跑 |
| 网页标题 | 好想跑 · 工作台 |
| Python 包 | `app`（内部名，保持不变） |

旧名「聚合跑」只保留在 `archive/` 里的历史文档；旧 `dist/聚合跑工具/` 已删除。
