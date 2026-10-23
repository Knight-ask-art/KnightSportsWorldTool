# 私有研究区（PRIVATE — 不对外）

本目录**不属于产品功能**，不参与 `build.spec` 打包，不注册到 HTTP bridge，
不出现在前端界面，也不进入公开文档的 API 清单。

| 目录 | 内容 | 说明 |
|---|---|---|
| `research/` | 只读采集与样本库实现 | 面向研究/回归验证，显式调用 |
| `data/` | 采集产物（`*.jsonl`） | 含轨迹坐标，**禁止外发、禁止入库** |

## 边界

- 公共代码（`app/replica`、`frontend`）**不得** import `private.*`。
- 私有代码**可以** import 公共包（只读复用 `readonly` 等能力）。
- `private/data/` 已被 `.gitignore` 排除；样本默认写入
  `private/data/corpus/<uid>.jsonl`，可用 `JHP_CORPUS_DIR` 覆盖。

## 运行私有测试

```powershell
cd 好想跑
python -m pytest private/research/tests -q
```

## 合规约束

- 只读采集，不提交、不修改服务端数据。
- 只在明确授权与用户许可下对指定账号执行。
- 采集结果含真实轨迹与时间，按敏感数据处理。
