# 好想跑 v0.2.1 发布说明

- **版本号**：v0.2.1
- **发布日期**：2026-11-10
- **渠道**：stable
- **上一版本**：v0.2.0
- **回滚版本**：v0.1.9

> 这次只修数据形状和预览，不新增协议能力。

## 1. `stepDistance` 改为累计步幅距离

旧版把每个点的 `stepDistance` 都写成 `0.0`。官方 `MyLocation.stepDistance`
是**累计步幅距离**：客户端用相邻两点的差值得到某个 10 秒窗口走了多少米。
跑 4 km 而计步器一米的步幅距离都没记，是最容易被服务端对拍出来的破绽之一。

v0.2.1 改为：

- 用 10 秒 `stepsNum` 窗口在活跃时钟上积分，得到每个点的累计步数；
- 乘以全程步幅估计 `juli / total_steps`；
- 末点与 `juli` 对齐，逐点单调不减，中间按步数分布自然漂移。

回归：`test_run_realism.py::test_step_distance_is_cumulative_not_zero`，
以及 `application.audit` 新增的 `points.step-distance` 检查。

## 2. OBS 与提交体的 10 秒窗口逐项对齐

提交体的 `speedPerTenSec` / `stepsPerTenSec` 和 OBS 的 `speed_json` /
`step_freq_json` 现在读同一份窗口数组；OBS 的距离精度从 2 位统一到 4 位，
不再出现“同一条记录两个载荷差几毫米”的情况。OBS 也不再按 220 spm 截断
步数窗，240 spm 的高步频记录同样逐项相等。

回归：`test_run_realism.py::test_obs_and_submit_read_the_same_windows_and_points`。

## 3. 海拔预览图自适应（issue #12）

海拔预览图原来强制 Y 轴从 0 开始，几十米的海拔变化被压成一条平线。现在按本次
数据的最小值/最大值自动缩放，并保留 `minSpan` 防止纯平数据把噪声放大。

## 4. QQ 群公告

启动后弹一次 QQ 群公告，直接扫码进群。公告内容是
数据驱动的，放在 `app/replica/application/announcements.py`；以后改文案、换
二维码图片或加第二条公告，只改这个文件和 `app/frontend/assets/`，不需要动
`index.html` / `app.js`。已读状态按公告 id 存在 `data/ui_state.json` 的
`announcement_seen` 里；设置 → 关于里随时可以重新打开。

## 5. 没有采纳的参考项

参考项目 `nekohy/DemaciaSportsWorldTool` 的 `wire.rs` 会输出 `accuracy` /
`hasAltitude` / `steps`，共 30 键。但 7.4.0 dex 的 `MyLocation` 里这三个字段
并不在 Gson 序列化结果中（`im/mp/ra` 只有 `@Column`，没有 `@SerializedName`），
10-26 抓包的 103 个点也都是 27 键。v0.2.1 保持 27 键，不凭 7.3.x 的形状加字段。

## 6. 证据边界

- `stepDistance` 的语义来自参考项目对官方客户端的反编译结论；7.4.0 dex 的
  `MyLocation` 确认该字段存在且是 `double`；
- 27 键来自 7.4.0 FridaDump 的 `MyLocation`（`im/mp/ra` 只有 `@Column`）与
  10-26 真机抓包；
- 以上只证明“形状与已知证据一致”，不证明服务端一定接受。

## 7. 回滚

回滚版本是 v0.1.9：下载 v0.1.9 的安装包覆盖安装即可，`data/` 会保留。
发布 tag 在独立的 release 分支上，只带 `README.md`、`assets/`、`latest.json`，
不含源码；回滚指二进制回滚，不是从 tag 重建。
