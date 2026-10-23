# 05 · 时间线与存档

> 这一页只讲「怎么走到今天」和「旧资料去哪了」。
> 具体接口/参数看 [02-api.md](02-api.md)，决策依据看 [04-decisions.md](04-decisions.md)。

---

## 1. 五个阶段

| 阶段 | 时间 | 干了什么 | 留在哪 |
|---|---|---|---|
| ① 摸协议 | 2026-09 上旬 | 拆 APK、只读探针、7.2.70/7.3.70 差异 | `archive/reference/`、`apk_work/coaixy_ref/` |
| ② unidbg | 2026-09 中 – 10 上 | 跑 native 库当 oracle，啃响应解密 | `archive/unidbg-era/` |
| ③ Frida + 真机 | 2026-10 中 | 证明密钥是会话绑定；真机拿 token、真实围栏 | `archive/unidbg-era/` |
| ④ 纯 Python | 2026-10 中 | `netsec.py` 复刻 NetSecKit，unidbg 退役 | `PORT_FROM_NEKO.md` §0–§9 |
| ⑤ 做稳 + 补齐 | 2026-10 下 | 字段对拍、数据真实性、PR 吸收、路线/围栏、只读接口、审计 | `PORT_FROM_NEKO.md` §10–§30 |

> ④ 是分水岭：从「模拟器里跑官方 so」变成「纯协议、单进程、可回归」。
> 这也是为什么大量 unidbg 文档被封存——它们不再是运行前提。

---

## 2. 踩过的坑（按教训记）

| 坑 | 教训 |
|---|---|
| `address` 填了学校名 | 服务器要的是**城市**；这是上游 issue #16 的根因 |
| 提交体里塞了自造字段 | 宁可少发，不要发明；每个字段都要有出处 |
| 两条路线「看着一样长」，几何差 16% | 自称的距离必须等于坐标能量出来的距离 |
| 2 点路线被折返铺满 2 km | 退化路线要拒绝，不要硬凑 |
| 假设列表一定「新→旧」 | 排序方向要检测，不能假设 |
| `{"status": false}` 被当成功 | 失败就是失败，不能伪装成空数据 |
| JSON 落盘写到一半 | 多线程下会「token 突然没了」；必须原子写 |
| 越界没人提示 | 看不见的约束等于没有约束 |

---

## 3. 旧文档封存去向

2026-10-22 做了一次统一整理：**手册和日志分开，过时的进 `archive/`**。

| 原路径 | 新路径 | 为什么 |
|---|---|---|
| `交接文档.md` | `archive/2026-09-07-交接文档.md` | 响应解密部分已被 `netsec.py` 取代 |
| `apk_work/unidbg/STATUS.md` | `archive/unidbg-era/STATUS.md` | unidbg 时代日志 |
| `apk_work/unidbg/FRIDA_NEXT.md` | `archive/unidbg-era/FRIDA_NEXT.md` | Frida 验证线已结束 |
| `apk_work/unidbg/SESSION_MUMU_TOKEN_20261012.md` | `archive/unidbg-era/` | 真机会话；**含明文账号密码，勿外发** |
| `apk_work/unidbg/TOMORROW.md` | `archive/unidbg-era/` | 过期待办 |
| `apk_work/unidbg/AES_DEC_CONFIRMED.md` | `archive/unidbg-era/` | 结论已在 `nskcodec.py` + 测试里 |
| `jhp_phase1/NEW_API_LIST.md` | `archive/reference/NEW_API_LIST.md` | 00201.0.0 的桥接清单，版本过时 |
| `jhp_phase1/WIRE_PROTOCOL.md` | `archive/reference/WIRE_PROTOCOL.md` | 7.2.70 请求链笔记 |
| `apk_work/PROBE_FINDINGS.md` | `archive/reference/` | 早期探针结果 |
| `apk_work/PROTO_DIFF_7.3.70.md` | `archive/reference/` | 版本差异表 |
| `apk_work/算法题_*.md` | `archive/reference/algorithms/` | 算法推导过程（16 篇） |

**没有搬走的**：

- `apk_work/coaixy_ref/`：20 MB 反编译源码树，文档与代码绑在一起；
- `apk_work/unidbg/` 下的 Java/Python 脚本、日志、APK：实验证据，不是文档。

完整说明见 [`../archive/README.md`](../archive/README.md)。

---

## 4. 现在以什么为准

| 想知道 | 看 |
|---|---|
| 怎么用 | `docs/01-usage.md` |
| 接口/字段 | `docs/02-api.md` |
| 出错了怎么办 | `docs/03-operations.md` |
| 为什么这么定 | `docs/04-decisions.md` + `PORT_FROM_NEKO.md` |
| 上一会话停在哪 | `jhp_phase1/HANDOFF.md` |
| 旧资料 | `archive/`（只读） |

---

## 5. 还没做完的

1. **最后一次真机回归**：token 失效 + 不允许拿账号测试，卡在这里；
2. **UI 重做**：新 UI 已正式启用（`frontend/index.html`），旧 UI/旧 mockup/旧 dist 已删除；
3. **重新打包 exe**：`build.ps1` 现在输出 `dist/好想跑_v<版本>/`，并保留同版本目标里的 `data/`；旧 `dist/好想跑/` 不再被删除。

> 这些事不影响本地自测和只读查询。
