# Route Memory — T6 验收记录

- 日期：2026-11-12
- 计划分支：`plan/route-memory`
- 范围：T2 核心、T3 Bridge、T4 路线库、T5 Agent 后端、T9 Agent 页面、T7/T8 LLM 设置

## 1. 自动化

```powershell
cd app
python -m pytest tests -q
# 829 passed, 8 skipped

node --check frontend/js/app.js
# exit 0
```

跳过项均为 `tests/test_rust_core.py`：本机未构建 `hxp-core`。

## 2. 路线学习 P0（合成基准）

见 `docs/plans/route-memory/P0-REPORT.md`（由 `tools/route_memory_spike.py`
生成，20/20 断言通过）。

补充单测：`test_out_and_back_track_is_stored_as_open_half` 验证“跑到折返点再原路
返回”的记录会存成 open 的单程模板，而不是把整条往返当成一个闭合圈。

真实记录复验仍未完成：本机备份 token 返回 `Unauthorized token有误或缓存中token不存在`，
本机也没有 GPX/FIT/TCX 轨迹。报告必须明确写“未验证”，不得把合成结果当成真实数据结论。

## 3. Agent 端到端（离线，真实 Bridge）

```powershell
python tools/agent_e2e_check.py --json
```

实测结果：

```json
{
  "state": "wait_confirm",
  "candidates": 1,
  "selected": "lr_b7364844",
  "audit": {"ok": true, "passed": 32, "failed": 0, "warned": 0},
  "km": 2.5,
  "waypoints": 130,
  "closed": true,
  "checkpoints": 5,
  "submitted": false,
  "receipts": []
}
```

验证点：

- 从 5 条合成历史记录学到 1 个模板；
- Agent 自动选路 → 适配 → 本地预览 → 审计 32 项全过；
- `confirm` 只返回 `preview_args`，没有调用 `real_submit_run`；
- 临时 store 下没有生成任何 `submission_receipts`。

## 4. Agent 页面 DOM 冒烟（本地 HTTP + Chrome DevTools）

在 `plan` 工作树启动 `python run.py --no-browser --port 65521 --store <temp>`，
用 Chrome DevTools 打开 `#agent`，注入 stub bridge 后验证：

| 场景 | 结果 |
|---|---|
| 审计通过 + `wait_confirm` | 候选卡、预览、`本地检查通过` 正常显示，`#agentConfirm` 可用 |
| 审计失败 | `#agentConfirm` 保持 disabled，提示“本地检查未通过” |
| 点击确认 | 跳到 `#page-run`，`#inKm=2.5`、闭合开、`distanceManual=true`、路由已载入 |
| 提交路径 | `window.__submitCalled === false`，没有任何 submit 调用 |
| 桌面布局 | 1728×1064 双栏：消息流 + 候选/预览栏 |
| 窄屏 390×844 | 单栏，候选/预览在消息流下方，确认按钮可滚动到 |

截图（人工查看）：Agent 页候选卡 + 预览 + 确认按钮均可见，文案为“已推荐路线/已生成预览/已完成检查”。

## 5. 关键不提交证据

```powershell
rg "real_submit_run" app/frontend/js/app.js
rg "submit" app/replica/application/agent.py
```

- Agent 前端区块没有 `real_submit_run`；
- `agent.py` 没有任何 submit 工具或回执写入；
- `confirm()` 仅返回 `preview_args`，最终提交仍由跑步页用户点击。

## 6. 已知限制

- 真实记录 P0 仍被 token 失效阻塞（见 §2）。
- Agent 预览使用当天默认时间窗；凌晨无有效窗口时会显示“开始时间无效”，需在跑步页选日期。
- 前端自动化是 DOM 冒烟 + 静态断言，不是完整 E2E 浏览器回归。
- LLM 设置页只做了代码与静态测试，未接真实第三方模型做手动连通性测试。
