# 好想跑 · 项目知识库

> 这是项目知识的**入口和索引层**，不是又一套手册。
> 每个专题卡片只回答「结论是什么、证据在哪、代码在哪、还有什么没验证」，
> 细节仍然回到 `docs/` 里的原始文档。

## 怎么用

**人**：先看 [`00-overview.md`](00-overview.md)，再按下面的目录跳转。

**AI / 新会话**：按 `README → 00-overview → 相关专题` 的顺序读；回答时优先引用
卡片里的 `file:line` 或原始文档章节，不要凭记忆补协议细节。

**命令行检索**（离线，不联网）：

```powershell
# 全库检索（默认同时搜 docs/kb 和 docs）
python scripts\kb_search.py policy

# 只看知识库，最多 10 条
python scripts\kb_search.py 步频 --scope kb --limit 10

# 结构化输出，方便其他脚本消费
python scripts\kb_search.py 回执 --json

# 校验 index.json 和文件是否一致
python scripts\kb_search.py --check
```

## 目录

| 卡片 | 回答什么问题 |
|---|---|
| [00-overview.md](00-overview.md) | 项目是做什么的、当前版本、技术栈、目录、常用命令 |
| [10-protocol.md](10-protocol.md) | 登录/极验、点位/围栏、提交体、OBS、轨迹字段、接口回退 |
| [20-risk-rules.md](20-risk-rules.md) | `policy` / `isCheat` / `stepFreqStatus` 到底是什么意思，学校规则和清算边界 |
| [30-data-device.md](30-data-device.md) | `data/` 里有什么、设备指纹、DPAPI、迁移和备份 |
| [40-frontend.md](40-frontend.md) | 前端结构、公告模块、皮肤、动效约束、设计参考 |
| [50-build-release.md](50-build-release.md) | Nuitka/NSIS、版本落点、latest.json、完整发布 SOP、回滚 |
| [60-ops.md](60-ops.md) | 三道闸门、保活、缓存、排错表、诊断包、只读对账 |
| [70-issues.md](70-issues.md) | 历史 issue 结论、常见提问的标准答法 |
| [90-sources.md](90-sources.md) | 本地权威文档 + 外部资料清单、访问日期、可信度 |
| [99-glossary.md](99-glossary.md) | 术语表：rrid、OBS、学分跑、围栏、步频、配速…… |

机器可读目录：[`index.json`](index.json)。

## 证据等级

| 等级 | 含义 | 例子 |
|---|---|---|
| **A** | 本仓库源码、测试或真机实测，能复现 | `app/replica/...`、`pytest`、抓包结论 |
| **B** | 上游参考项目或官方文档，有明确出处 | Inverx / Demacia、极验官方文档、Nuitka 文档 |
| **C** | 社区、学校通知、应用商店等二手信息 | 各校体育部规则、商店版本号、知乎/36氪报道 |
| **D** | 推测、待验证 | “可能从某版本开始变严”这类判断 |

**规则**：卡片里的结论必须带等级。C/D 不能写成确定事实；涉及提交链的改动，
至少要有 A 或 B 级证据。

## 维护规则

1. 每次发布、协议改动、学校规则变化，更新对应卡片和 `index.json` 的 `updated`。
2. 新增卡片必须登记到 `index.json`，否则 `--check` 和测试会失败。
3. **不写凭据**：token、密码、完整 DeviceId、cookie、坐标、账号、本地绝对路径。
4. 拿不准的写进「未验证」，不要为了好看补结论。
5. 提交前跑：

```powershell
python scripts\kb_search.py --check
cd app; python -m pytest tests\test_kb.py -q
```

## 和现有文档的关系

- `docs/kb/` 是**索引 + 结论层**，适合快速定位和给 AI 读。
- `docs/api.md`、`usage.md`、`operations.md`、`packaging.md`、`update.md`、
  `geetest.md`、`decisions.md` 仍然是**权威细节**；冲突时以它们和源码为准，
  并回头修卡片。
- `docs/reference-projects.md` 跟踪协议参考项目，
  `docs/design-references.md` 跟踪前端视觉参考，两者不要混。
