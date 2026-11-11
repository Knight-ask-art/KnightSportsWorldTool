# Route Memory — P0 合成验证报告

- 日期：2026-11-12
- 脚本：`tools/route_memory_spike.py --fixture synthetic`
- 目的：在不使用真实轨迹的前提下，验证单圈提取、聚类、方向不变、
  真实围栏/点位适配与审计前适配。

## 1. 聚类阶段（抽象 1.4 km 场景）

| 模板 | closed | lap_m | samples | medoid_m |
|---|---|---|---|---|
| 学习路线 1 | True | 1462.3 | 8 | 4.2 |
| 学习路线 2 | False | 2084.5 | 4 | 4.9 |
| 学习路线 3 | True | 414.5 | 3 | 3.8 |
| 学习路线 4 | True | 1137.5 | 3 | 5.7 |

> 该阶段离真实校区很远，不参与围栏判断；只验证几何。

## 2. 真实校区阶段

使用 `campus_fence_47203.json` 与 `campus_points_47203.json`。

- top1：学习路线 1，246.4 m/圈 × 11 圈，漏点 0
- top1 围栏外点数：0

## 3. 断言

| 断言 | 结果 | 实际 | 期望 |
|---|---|---|---|
| seen/readable/skipped | PASS | [24, 18, 6] | (24, 18, 6) |
| four templates | PASS | 4 | 4 |
| samples 8/4/3/3 | PASS | [8, 4, 3, 3] | [8, 4, 3, 3] |
| 400 m lap 380-430 | PASS | 414.5 | 380-430 |
| A medoid distance < 40 m | PASS | 4.2 | <40 |
| template line equals a real input lap | PASS | 1 | >=1 |
| direction invariant shape | PASS | 5.532 | <10 m |
| direction invariant lap | PASS | [1462.3, 1460.8] | within 2% |
| stable template ids | PASS | ["lr_335de380", "lr_335de380"] | same |
| 2.5/3.0 both suggest the A template | PASS | [["lr_335de380", "lr_e1b2f282", "lr_1b268849"], ["lr_e1b2f282", "lr_1b268849", "lr_335de380"]] | contains A id |
| campus summary ok | PASS | "ok" | ok |
| campus candidate exists | PASS | 1 | >=1 |
| top candidate >=4/5 points | PASS | 5 | >=4 |
| top candidate snapped 5/5 | PASS | 0 | 0 |
| top candidate fence 0 outside | PASS | 0 | 0 |
| apply ok | PASS | true | True |
| apply waypoints <=400 | PASS | 130 | <=400 |
| apply laps 1-30 | PASS | 11 | 1-30 |
| apply missed 0 | PASS | 0 | 0 |
| learning time <300 ms | PASS | 261.6 | <300 ms |

## 4. 性能

- 学习 24 条（18 有效）耗时：261.6 ms（门限 300 ms）

## 5. 真实数据复验

- **未验证**。本机备份 token 返回 `Unauthorized token有误或缓存中token不存在`，
  且没有 GPX/FIT/TCX 轨迹；`--account` 模式未运行或失败。
- 不能把本报告的合成结果当成真实数据结论。

## 6. 结论

20/20 项断言通过。

真实记录验证仍阻塞；产品内用弱数据门槛兜底。
