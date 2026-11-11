# Route Memory — 执行状态

> 最后更新：2026-11-12
> 计划分支：`plan/route-memory`
> 规格：`docs/plans/route-memory/SPEC.md`

## 任务与分支

| 任务 | 分支 | 工作树 | 状态 |
|---|---|---|---|
| T1 轨迹几何 | `task/rm-core` | `_worktrees/route-memory/t1` | ✅ 已合并 `b0e4e93` |
| T2 路线记忆核心 | `task/rm-memory` | `_worktrees/route-memory/t2` | ✅ 已合并 `b50c543`（复审 PASS） |
| T3 路线记忆 Bridge | `task/rm-bridge` | `_worktrees/route-memory/t3` | ✅ 已合并 `d595ff6`（复审 PASS） |
| T4 路线库前端 | `task/rm-frontend` | `_worktrees/route-memory/t4` | ✅ 已合并 `22e8ebd`（复审 HIGH/MEDIUM 已修） |
| T5 Agent 后端 | `task/rm-agent` | `_worktrees/route-memory/t5` | ✅ 已合并 `1ebd245`（复审 PASS） |
| T6 验收 | `task/rm-acceptance` | 待建 | ⏳ 等 T9 |
| T7 LLM 设置后端 | `task/llm-backend` | `_worktrees/route-memory/t7` | ✅ 已合并（presets 含 default_model） |
| T8 LLM 设置前端 | `task/llm-frontend` | `_worktrees/route-memory/t8` | ✅ 已合并 |
| T9 Agent 页面 | `task/rm-agent-page` | `_worktrees/route-memory/t9` | 🟡 进行中 |
| T0 P0 验证 | `task/rm-p0` | 待建 | ⏳ 等 T9 后一起做 |

## 已确认的决定

- 路线学习是确定性几何：单圈提取 → 128 点重采样 → 增量 k-medoids → medoid 真实轨迹。
- 相似度：闭环用循环对齐 + 方向不变的最小均值距离；开放路线用对称 Chamfer。
- 不引入 GIS 大件；`numpy` 只用于热路径。
- v1 不做地图匹配；历史轨迹本身就是真实路径。
- 原始轨迹不落盘；只存简化模板，按 `uid:unid` 隔离。
- LLM 只选候选/解释，不生成坐标；key 用 DPAPI 封存；prompt 只含统计量。
- Agent 页面：聊天式 + 候选卡 + 预览/审计 + 确认闸门；**永不提交**。
- P0 真实数据验证被 token 失效阻塞；用真实围栏/点位 + 合成噪声轨迹做基准，产品内加弱数据门槛。

## 下一步

1. T9 Agent 页面完成 → 独立复审 → 合并。
2. T0 合成基准（真实围栏/点位 + 合成噪声轨迹）与 T6 验收。
3. 验收：全量测试 + `node --check` + “学习 → 推荐 → 预览 → 审计 → 确认 → 不提交”端到端 + 手动 UI 走查。
4. T0 合成轨迹要保证采样间距 ≥ 15 m 并做 3 点平滑，
   否则独立噪声会把路径长度抬高 10–100%，圈长断言失真。

## 验收命令

```powershell
cd app
python -m pytest tests -q
node --check frontend/js/app.js
```
