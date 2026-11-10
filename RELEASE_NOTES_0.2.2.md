# 好想跑 v0.2.2 发布说明

- **版本号**：v0.2.2
- **发布日期**：2026-11-10
- **渠道**：stable
- **上一版本**：v0.2.1
- **回滚版本**：v0.1.9

> 这是一个小补丁：修正记录详情里的 `policy=1` 误报，并给 QQ 群公告补上暗号。

## 1. `policy=1` 不再是“风险标记”

issue #14 里，11/05 之后的记录在详情页被标成「风险标记 policy=1」，但记录本身是
“已通过”，平台检查也全部通过。

原因是：提交体里的 `policy` 是服务器下发的**规则集编号**（1 = 学分跑规则），
详情接口会把它原样回显。旧版前端把任何非零 `policy` 都当成反作弊标记，
所以从服务器开始回显这个字段起，正常的学分跑记录也被画成红色。

v0.2.2 改为：

- 「风险标记」只读 `isCheat` 和 `stepFreqStatus`，以及 `status` / `normalRun` 等真实异常信号；
- `policy` 单独显示为「规则集 policy=1」，并标注“服务器下发的跑步规则集编号，不是风险标记”；
- 列表行的状态判断本来就没有用 `policy`，这次只修详情页。

> `policy=1` 本身不代表被清算。服务端的反作弊是回溯性的，真正要看的还是
> `isCheat`、`stepFreqStatus`、`status`、`normalRun` 和记录详情里的原因列表。

## 2. QQ 群公告加暗号

公告里补了一行「加群暗号：皮卡丘」。公告仍然是数据驱动的：

- 文案/暗号：`app/replica/application/announcements.py`
- 二维码图片：`app/frontend/assets/qq-group.jpg`
- 已读状态：`data/ui_state.json` 的 `announcement_seen`

不改群号，不要求手动输入，直接扫码进群。

## 3. 证据边界

- `policy` 的语义来自提交体 `build_upload_entity` 的规则集字段和详情接口回显；
- 这次只修 UI 展示，不改变提交体、签名或服务端判定；
- 是否被标记仍以服务端返回的 `isCheat` / `stepFreqStatus` / `status` 为准。
