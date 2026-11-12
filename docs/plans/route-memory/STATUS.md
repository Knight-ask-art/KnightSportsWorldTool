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
| T6 验收 | `task/rm-acceptance` | `plan/route-memory` | ✅ 完成（832 passed；P0 20/20；Agent e2e 通过） |
| T7 LLM 设置后端 | `task/llm-backend` | `_worktrees/route-memory/t7` | ✅ 已合并（presets 含 default_model） |
| T8 LLM 设置前端 | `task/llm-frontend` | `_worktrees/route-memory/t8` | ✅ 已合并 |
| T9 Agent 页面 | `task/rm-agent-page` | `_worktrees/route-memory/t9` | ✅ 已合并 `c1a504f`（复审 PASS） |
| T0 P0 验证 | `task/rm-p0` | `plan/route-memory` | ✅ 完成（`tools/route_memory_spike.py`，20/20） |
| T10 Agent 优化 | `task/agent-optimize` | 主工作树 | ✅ 完成（缓存 + 工具校验/重试 + 候选轮换；954 passed） |

## 已确认的决定

- 路线学习是确定性几何：单圈提取 → 128 点重采样 → 增量 k-medoids → medoid 真实轨迹。
- 相似度：闭环用循环对齐 + 方向不变的最小均值距离；开放路线用对称 Chamfer。
- 不引入 GIS 大件；`numpy` 只用于热路径。
- v1 不做地图匹配；历史轨迹本身就是真实路径。
- 原始轨迹不落盘；只存简化模板，按 `uid:unid` 隔离。
- LLM 只选候选/解释，不生成坐标；key 用 DPAPI 封存；prompt 只含统计量。
- Agent 页面：聊天式 + 候选卡 + 预览/审计 + 确认闸门；**永不提交**。
- P0 真实数据验证被 token 失效阻塞；用真实围栏/点位 + 合成噪声轨迹做基准，产品内加弱数据门槛。

## 2026-11-12 Agent 优化

- 意图路由：否定词不再误触发取消/反向；距离优先于“换一条/反向”；支持“跑3公里，用第2条”。
- 工具契约：`status/learn/suggest/apply/audit` 结构校验；审计缺 `summary.ok` 直接报错，不再默认通过。
- 可靠性：瞬时错误重试 1 次；审计失败自动轮换最多 3 条候选；显式「用第 N 条」不轮换，审计失败时保留并提示“未自动替换”。
- 缓存：suggest/apply 进程内 TTL 缓存（记忆文件 mtime+size+SHA-256 签名失效）；`fence=None` 围栏缓存 60 秒；LLM 选路持久缓存（30 天、最多 200 条、固定 temperature=0）。
- 意图：否定词不误触发；“算了，跑3公里”按新目标处理，“打算了解”不误判取消；支持 `3km.` / `500m.` 等句末标点。
- 可观测：`status.stats` 暴露工具重试、缓存命中/未命中、LLM 调用与候选尝试次数；Agent 页显示缓存命中，审计失败时显示 Agent 的最终说明。
- 测试：`python -m pytest app/tests -q` → **954 passed**；`node --check app/frontend/js/app.js` 通过。

## 下一步

1. 交付 `plan/route-memory`（含 SPEC/REVIEW/STATUS、P0 报告、验收记录、两组离线检查脚本）。
2. 用户重新登录后，跑 `tools/route_memory_spike.py --account <session.json>` 做真实记录复验。
3. 真实记录 P0 未完成前，不要把合成基准当成真实数据结论。

## 验收命令

```powershell
cd app
python -m pytest tests -q
node --check frontend/js/app.js
```
