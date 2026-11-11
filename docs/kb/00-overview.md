# 00 · 项目概览

> 证据等级：**A**（源码 / 测试 / 本仓库文档）
> 最后更新：2026-11-11 · 对应版本 **0.2.2**

## 一句话

「好想跑」是一个**本地 Windows 工作台**：复刻「运动世界校园」7.4.0 的跑步提交链路
（登录 → 拉点位/围栏 → 画路线 → 造数 → 签名 → 提交 → OBS 回放 → 只读查询），
界面在本机 `127.0.0.1`，数据留在 `data/`，不依赖模拟器和抓包重放。

## 当前状态

| 项目 | 值 |
|---|---|
| 版本 | `0.2.2`（2026-11-10 发布） |
| Release | <https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/tag/v0.2.2> |
| 回滚版本 | `0.1.9` |
| 本地测试 | 695 passed |
| 云 Linux | 676 passed / 19 skipped（Windows-only 跳过） |
| 发布通道 | `stable`，Windows x64，安装版 + 便携版 |
| 官方 App 基线 | 7.4.0（商店页 2026-10-29 更新，C 级） |

版本号和发布说明的落点见 [`50-build-release.md`](50-build-release.md)。

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 语言 | Python `>=3.11` | `app/pyproject.toml` |
| 桌面壳 | pywebview + EdgeChromium | 前端是本地静态页，不是 Electron |
| 前端 | 原生 HTML/CSS/JS | 无构建步骤、无框架、无运行时 CDN |
| 加密 | pycryptodome + 自研 NetSecKit 复刻 | 签名/信封在 `replica/transport/`、`replica/domain/crypto.py` |
| 打包 | Nuitka standalone（onedir）+ NSIS | 不用 onefile；PyInstaller 只作回退 |
| 测试 | pytest | 本地全量 + InstaBox Linux 交叉验证 |

## 目录地图

```text
好想跑/
├─ app/                     应用本体
│  ├─ app.py / run.py       桌面入口 / 浏览器入口
│  ├─ replica/
│  │  ├─ transport/         信封、HTTP、极验、OBS、unidbg sidecar
│  │  ├─ domain/            坐标、轨迹、路线、时间、签名原语
│  │  ├─ application/       登录、保活、点位、规则、限额、回执、只读
│  │  ├─ adapters/          bridge API、HTTP 服务、CLI
│  │  └─ compat/            旧 71 API 的本地 mock
│  ├─ frontend/             index.html + js/ + css/ + assets/
│  ├─ packaging/            build.ps1、installer.nsi、make_latest.py
│  ├─ tests/                pytest
│  └─ data/                 运行时数据（不入库、不打包）
├─ docs/                    手册 + 本知识库
├─ scripts/                 发布检查、云测试、参考项目检查、KB 检索
├─ github-release/          发布分支用的 README + latest.json
├─ reverse-engineering/     逆向资料（大部分不入库）
├─ archive/                 封存文档
└─ output/                  构建日志、审查记录（gitignored）
```

分层依赖：`adapters → application → domain`，`application → transport → domain`。
细节见 [`../architecture.md`](../architecture.md)。

## 常用命令

```powershell
# 开发模式（浏览器）
cd app; python run.py --port 65520

# 桌面窗口
cd app; python app.py

# 离线自检 / 全量测试
cd app; python -m replica.selfcheck
cd app; python -m pytest tests -q

# 只造数、不提交
cd app; python -m replica.pipeline --token X --uid 1 --km 2.0 --dry-run --no-preflight --no-history

# 发布前预检（不打包）
python scripts\release_check.py --source-only

# 云 Linux 测试
python scripts\cloud_test.py

# 知识库检索 / 校验
python scripts\kb_search.py policy
python scripts\kb_search.py --check
```

## 阅读顺序

1. **新人**：本页 → [`60-ops.md`](60-ops.md) → [`10-protocol.md`](10-protocol.md)
2. **改协议**：[`10-protocol.md`](10-protocol.md) → [`20-risk-rules.md`](20-risk-rules.md)
   → `docs/api.md` 对应章节 → `docs/decisions.md`
3. **改前端**：[`40-frontend.md`](40-frontend.md) → `docs/design-references.md`
4. **发版**：[`50-build-release.md`](50-build-release.md) → `docs/workflow.md`
5. **回 issue**：[`70-issues.md`](70-issues.md) → 对应 Release Notes

## 三条硬边界

1. **不重试提交**：超时 = 未知状态，回执锁住时段，宁可少跑一次。
2. **本地闸门**：默认每天最多 2 次、23:00–06:00 拒绝提交（可关，但不建议）。
3. **凭据只留本机**：`app/data/` 含 token / 设备指纹 / 回执 / 坐标，不提交、不外发。

## 相关

- 术语：[`99-glossary.md`](99-glossary.md)
- 外部资料与可信度：[`90-sources.md`](90-sources.md)
- 原始手册：[`../README.md`](../README.md)
