# Route Memory — 慢速审查清单

> 目的：每个任务先过审查门，再合并；不追求并行速度。
> 规则：**一次只合并一个任务；合并前必须有审查记录和测试证据。**

## 0. 每个任务的固定流程

```text
worker 完成
  → 计划者读完整 diff（不是只看摘要）
  → 计划者补边界测试 / 修契约偏差
  → 独立 Claude Code reviewer 只读审查（plan mode）
  → 计划者逐条回应 findings
  → 跑 focused tests + 全量 tests + node --check
  → 合并到 plan 分支
  → 更新 STATUS.md
```

任何一步失败，停在原地，不带病进入下一任务。

## 1. 代码审查门（人工）

- [ ] 公共函数签名与 SPEC 完全一致，没有“顺手改接口”。
- [ ] 没有新增第三方依赖；`pyproject.toml` 未变。
- [ ] `domain` 不反向依赖 `application` / `adapters`。
- [ ] 没有读写 `app/data/`、`private/`、`third_party/`。
- [ ] 没有把原始轨迹、token、uid、昵称写进日志或缓存。
- [ ] 错误路径有中文提示，不吞异常、不白屏。
- [ ] 输入校验：NaN、None、0/0、超范围、空列表、损坏 JSON。
- [ ] 原子写盘；并发学习/状态读取有锁。
- [ ] 前端没有硬编码第二份协议列表；没有 `AI 生成` 文案。
- [ ] Agent 页面代码里没有 `real_submit_run`。

## 2. 几何边界门（T1/T2 必过）

- [ ] 闭环首尾段参与距离/点位/围栏计算（`line + [line[0]]`）。
- [ ] 1 圈、2 圈、3 圈、5 圈同一路线归同一模板。
- [ ] 正跑/反跑归同一模板。
- [ ] 200 m / 400 m 跑道能学出模板（`MIN_LAP_M=200`）。
- [ ] 起点抖动 ±80 m 不影响聚类。
- [ ] GPS 噪声 σ=8 m 不影响 medoid 真实性。
- [ ] 飞点/超短/超长/作弊/工具记录被过滤且计数正确。
- [ ] 打卡点位于闭环首尾段附近时，`missed_points` 不误报。
- [ ] `snap_through` 插入后 5/5，且不把路线拉出围栏。
- [ ] 多围栏（任一围栏内）判断正确。
- [ ] `medoid` 是真实输入轨迹，不是平均线。
- [ ] 模板 id 对同一输入稳定；方向反转 id 不变。

## 3. 存储与账号门（T2/T3 必过）

- [ ] `data/route_memory/<uid>_<unid>.json` 路径已消毒。
- [ ] 损坏 JSON / 旧版本 / 未知字段 → 空 doc，不抛异常。
- [ ] 保存是临时文件 + `os.replace`。
- [ ] A 账号模板不出现在 B 账号。
- [ ] 原始轨迹不落盘；只留简化模板和 `source_rrids`。
- [ ] 学习进度 `wait=False` 能轮询；异常写 `phase='error'`。
- [ ] 第二次学习不会并发起两个线程。
- [ ] 详情 OBS 404 只跳过该条，不整批失败。
- [ ] 工具生成的记录按回执 uuid 排除。

## 4. LLM 门（T7/T8 必过）

- [ ] `api_key` 永不回传前端；`load()` 只有 `has_key`。
- [ ] DPAPI 不可用时保存 key 明确失败，不写明文。
- [ ] 协议 7 种；Base URL 只允许 http/https。
- [ ] 401/403/404/429/5xx/超时分类正确。
- [ ] 错误信息做 key 替换，不泄露。
- [ ] `build_choice_prompt` 不含经纬度、token、uid、unid、校区名、昵称。
- [ ] 测试连接走 `JHP_PROXY`，不落盘。
- [ ] 未启用/未配置时，所有路线功能照常工作。

## 5. Agent 门（T5/T9 必过）

- [ ] 状态机只允许合法跳转；非法 confirm 返回错误，不抛异常。
- [ ] `confirm()` 只返回 `preview_args`，不调用 submit、不写回执。
- [ ] Agent 页面 `#agentConfirm` 默认 disabled，审计失败保持 disabled。
- [ ] 确认后跳到跑步页，停在预览态；最终提交仍由用户点击。
- [ ] 中文距离解析（2.5 / 三公里 / 五公里 / 半）正确。
- [ ] 换一条 / 反向 / 重新学习 / 取消 都能回到可用状态。
- [ ] 无 LLM、token 失效、0 模板、弱数据都有明确提示。
- [ ] 日志脱敏：不含 token/uid/坐标/prompt 原文。
- [ ] 轮询停止：离开页面不再请求。

## 6. 端到端合成验收（T6 必做）

```text
合成 20 条轨迹（4 族 + 6 条脏数据）
  → learn
  → status = ok
  → suggest 2.5 km
  → apply top1
  → preview_route
  → audit_run
  → agent confirm
  → 断言：未提交、无回执、点位 5/5、围栏 0 外、距离由 preview 负责
```

## 7. 命令证据

每个任务至少保存：

```powershell
cd app
python -m pytest tests/test_<task>.py -q
python -m pytest tests -q
node --check frontend/js/app.js
```

审查报告写到 `docs/plans/route-memory/reviews/<task>.md`，包含：
- 审查时间、commit
- 发现列表（file:line + 后果 + 修复）
- 测试命令与输出
- PASS / BLOCK
