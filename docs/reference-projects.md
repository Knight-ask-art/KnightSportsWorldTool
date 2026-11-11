# 05 · 参考项目跟踪

> 这两个仓库是「好想跑」的上游参考实现，不是依赖，也不复制代码。
> 每次协议/真实性相关改动前，先看它们有没有新版本、新目录、新结论。
>
> 前端视觉和动效参考是另一份清单：[`design-references.md`](design-references.md)。
> 本文件只跟踪协议实现，不混入 UI 参考。

## 1. 跟踪清单

| 仓库 | 语言 / 许可 | 为什么跟踪 | 重点看什么 |
|---|---|---|---|
| [Inverx/com.zjwh.android_wh_physicalfitness](https://github.com/Inverx/com.zjwh.android_wh_physicalfitness) | Python / MIT | 运动世界校园纯 Python 协议实现，含 NetSecKit 4.2.4、GeeTest v4、OBS、31 字段签名和 5 阶段时序 | 新版本目录（7.3.92 → 7.4.x）、`core/` 加密、`policy/` 规则、`running/` OBS/签名、`telemetry/` 埋点、`login/` 极验 |
| [nekohy/DemaciaSportsWorldTool](https://github.com/nekohy/DemaciaSportsWorldTool) | Rust / CC BY-NC 4.0 | 轨迹生成与真实性自检的工程化实现，含 19 项自检和大量实测结论 | `docs/REALISM_FINDINGS_zh.md`、`src/track/realism.rs`、`src/track/altitude.rs`、`src/track/generator.rs`、`Cargo.toml` |

**许可边界**：repo 1 是 MIT，可在保留版权声明的前提下参考；repo 2 是
CC BY-NC 4.0，**只读结论，不复制代码**。两边的协议字段、阈值和实测数值可以
用来写我们自己的测试和实现。

## 2. 当前基线（2026-11-09）

| 项目 | 最新提交 | 说明 |
|---|---|---|
| Inverx | `412a2e28`（2026-10-28） | 已归档 7.3.92 模块化套件、7.3.40 独立脚本，并在提交里加入 7.3.93 / 7.4.0 脱壳资料 |
| Demacia | `98f58e50`（2026-10-26） | v0.5.5 之后补了默认值和「里程不足提前拦」；真实性结论在 `REALISM_FINDINGS_zh.md` |

## 3. 变更时怎么做

1. 先跑检查：

```powershell
python scripts\check_references.py
```

2. 有变化时，在 InstaBox 上拉一份，不占本地磁盘：

```bash
# 在 agent-browser-yrbo 上
mkdir -p /data/refs
git clone --depth 50 https://github.com/Inverx/com.zjwh.android_wh_physicalfitness.git /data/refs/inverx
git clone --depth 50 https://github.com/nekohy/DemaciaSportsWorldTool.git /data/refs/demacia
```

3. 只看 diff 和关键文件，不整仓复制：

```bash
cd /data/refs/inverx && git log --oneline -10
git diff HEAD~5 -- "协议/7.3.92/core" "协议/7.3.92/running"
cd /data/refs/demacia && git diff HEAD~5 -- src/track docs/REALISM_FINDINGS_zh.md
```

4. 映射到我们的模块：
   - 加密/签名 → `app/replica/transport/netsec.py`、`nskcodec.py`、`nskpure.py`
   - 规则/围栏/时段 → `app/replica/application/policy.py`、`domain/safety.py`
   - 轨迹/真实性 → `app/replica/domain/rundata.py`、`health.py`
   - 提交/OBS → `app/replica/transport/obs.py`、`application/qualification.py`
5. 每个确认的差异补一条回归测试，再改实现；不要只改代码不留证据。

## 4. 什么时候看

- 每次协议相关 issue / 提交链改动前；
- 每次发版前（`release.ps1` 预检时顺手跑一次）；
- 官方 App 版本号变化时（当前我们按 7.4.0，repo 1 主套件还是 7.3.92）。

## 5. 已知差异（待逐项核对）

| 主题 | 参考项目 | 我们 | 处理 |
|---|---|---|---|
| App 版本 | repo 1 主套件 7.3.92；提交里有 7.4.0 脱壳资料 | 7.4.0 | 优先看 7.4.0 资料，别直接套 7.3.92 |
| 海拔 | repo 2 实测海拔带 9.2–12.4 m，反对固定跨度 | 4 km 约 19–23 m，按距离增长；预览图已改为自适应 | 保留现有生成器；用 repo 2 的结论做回归，不改成固定 9–12 m |
| 采样 | repo 2 记录 5 s 为主，偶发 1–2 s | 4.2–6.0 s，中位数 5 s | 已接近；如需更真，补 1–2 s 加密采样 |
| 真实性自检 | repo 2 有 19 项提交前互校 | 我们有 qualification 模型 + 预览 | 对照 19 项补缺口，不复制代码 |
| `stepDistance` | repo 2 指出官方点是累计步幅距离，跑 2 km 不能记 0 | v0.2.1 已改为累计：按 10 s 步数窗积分 × 全局步幅估计，末点≈`juli` | **已完成**；回归见 `test_run_realism.py::test_step_distance_is_cumulative_not_zero` |
| 点字段 | repo 2 的 `wire.rs` 输出 30 键，含 `accuracy` / `hasAltitude` / `steps` | 7.4.0 `MyLocation` 有 30 个 DB 字段，但 `im/mp/ra` 只有 `@Column`、没有 `@SerializedName`，Gson 实际序列化 **27 键**；10-26 抓包 103/103 点也是 27 键 | **不采纳**：加这 3 个键会变成 30 键，和 7.4.0 真机载荷不一致。repo 2 把 DB 字段当成了线上字段 |
| 冻结/人脸 | repo 2 把 `freezeRunTime > 0` 作为硬拒绝，`faceCheck` 跟随 `faceVerify` | 冻结是强提醒不硬拦；`faceCheck=0` 且阻止人脸学校 | 行为差异是有意的；发版说明里写清楚 |
| 10s/60s 窗口 | repo 2 要求提交体与 OBS 用同一套窗口、边界线性插值 | 提交体 `speed_json`/`step_freq_json` 与 OBS `speed_json`/`step_freq_json` 已读同一份数组 | **已完成**；OBS 距离精度从 2 位统一到 4 位，逐项相等有回归 |
| 遥测/广告 | repo 1 有 startup/pv、datacollection/pv、adgain/adxvip | 未实现 | 先确认是否影响提交链，再决定 |
| 室内锻炼 | repo 1 有 16 字段签名 | 未实现 | 记录为未来能力，不在当前版本范围 |

### 5.1 待核对任务

- [x] `stepDistance` 累计值（repo 2 的 K/L 项）
- [x] `accuracy` / `hasAltitude` / `steps`：已用 7.4.0 dex + 27 键抓包核对，不采纳
- [x] 提交体与 OBS 的 10 s 窗逐窗对拍（含 4 位精度与窗时长合计）
- [ ] 把 repo 2 的 19 项自检映射到我们的 `qualification` / `preview`
- [ ] repo 1 的 7.4.0 脱壳资料里是否有新 endpoint / 签名字段

## 6. 自动检查

`scripts/check_references.py` 通过 GitHub API 比较：

- 每个仓库的最新 commit；
- 最新 release（如果有）；
- 每个重点路径的最后一次 commit。

状态默认写到 `output/reference-state.json`（已忽略，不提交）。第一次运行建立基线，
之后运行只打印变化；`--update` 更新基线，`--fail-on-change` 用于发版前门禁。
