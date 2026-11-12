# 04 · 开发与发布工作流

> 本文件是「好想跑」的定制工作流，不是通用模板。原则只有三条：
> **Windows 专属的留本地，能上云的都上云，每个结论都要有命令和输出。**

## 1. 三条跑道

| 跑道 | 在哪跑 | 适合什么 | 不适合什么 |
|---|---|---|---|
| Windows 本地 | 开发机 | UI、pywebview、DPAPI、Nuitka、NSIS、最终安装包 | 长时间批量任务 |
| InstaBox Linux | `agent-browser-yrbo` | pytest、ruff、Docker、批量下载/转码、浏览器自动化、长时间 Agent | Windows 打包、真实 `data/`、Windows 注册表/DPAPI 测试 |
| InstaBox Android | `b` | adb、APK 安装/截图、x86_64 App 动态测试 | ARM-only `.so`、Google 服务、重 3D |

云机器的通路：本地 OpenCode 的 `instabox` MCP（`https://api.instabox.sh/mcp`），
key 在用户环境变量 `INSTABOX_API_KEY`。没有 MCP 时用 REST：

```powershell
$h = @{ 'X-API-Key' = $env:INSTABOX_API_KEY }
Invoke-RestMethod https://api.instabox.sh/v1/machines -Headers $h
```

## 2. 标准流水线

### 2.0 参考项目

协议/真实性相关改动前，先看两个上游有没有新东西：

```powershell
python scripts\check_references.py
```

返回 `NO CHANGES` 就继续；有变化时按 `docs/reference-projects.md` 的映射
在 InstaBox 上拉取 diff，不要复制代码，只吸收字段、阈值和结论。

### 2.1 接单与复现

1. 从 issue 或对话里写清楚：现象、期望、复现命令、影响版本。
2. 先写一个会失败的测试（`app/tests/`），再改代码。
3. 判断跑道：
   - UI / pywebview / updater / DPAPI / NSIS → Windows 本地；
   - 纯 Python 逻辑、协议、时间窗、Docker、批量任务 → InstaBox Linux；
   - APK / adb → InstaBox Android。
4. 高风险改动（时间窗、提交链、更新器、权限/凭据）默认走 **10 轮审查**，
   每轮一个范围，审查者必须给 `file:line`、复现命令、实际输出。

### 2.2 实现

- 分支从本地 `master` 切：`fix/<issue>-<slug>`。
- 不把 `app/data/`、`app/dist/`、`private/`、`reverse-engineering/` 带进提交。
- 版本号改动一次改全：`app/replica/build_info.py`、`app/frontend/index.html`、
  `app/packaging/installer.nsi`、`app/packaging/USAGE.txt`、`app/pyproject.toml`、
  `app/packaging/make_latest.py`（`DEFAULT_VERSION` + `RELEASE_NOTES`）。
  用 `python scripts/release_check.py --source-only` 检查漏改。

### 2.3 验证

**Windows 本地（必须）**

```powershell
cd app
python -m pytest tests -q          # 695 passed（v0.2.2 实测）
python -m replica.selfcheck        # 28/28
node --check frontend/js/app.js
node --check frontend/js/charts.js
python app.py --check              # exit 0
```

**InstaBox Linux（跨平台回归）**

```powershell
python scripts\cloud_test.py
# 670 passed, 19 skipped（DPAPI / 注册表 / updater 自动跳过）
```

脚本只上传 `git archive HEAD` 里的 tracked files，`data/`、`dist/`、`.git`
不会离开本机；首次运行会在云机器建一个 Python 3.11 venv 并装
pytest / requests / pycryptodome / numpy / opencv-python-headless。
**先提交再跑**：工作区脏时脚本默认拒绝，因为未提交的测试不会被上传；
确实只想验证 HEAD 时加 `--allow-dirty`。

**Android（按需）**

```powershell
# 查看设备
curl -s -X POST https://api.instabox.sh/v1/machines/b/exec `
  -H "X-API-Key: $env:INSTABOX_API_KEY" -H "Content-Type: application/json" `
  -d '{"cmd":"adb devices"}'
```

### 2.4 审查

- 普通改动：1 轮自审 + 1 轮独立审查。
- 高风险改动：10 轮，每轮限定范围，避免“泛泛而谈”。
- 审查者只读，不改代码；修复后重跑同一轮的复现命令。
- 审查产物写到 `output/reviews/<version>/round-N.md`（`output/` 不入库）。

Claude Code 审查（默认只读、只给 Read/Glob/Grep + git 工具）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
  -Scope "issue #12 海拔图缩放与 charts.js" -Round 1

# 需要审查者自己跑测试时再加 -WithTests（会慢一些）
powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
  -Scope "提交链时间窗" -Round 2 -WithTests

# 只打印命令，不调用模型
powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
  -Scope "release scripts" -Plan

# 可选：给本轮设美元上限（默认 0 = 不设上限）
# 上限太小会在读完文件前中止，脚本会明确报 Exceeded USD budget
powershell -ExecutionPolicy Bypass -File scripts\claude_review.ps1 `
  -Scope "提交链时间窗" -Round 3 -MaxBudgetUsd 5
```

Claude Code 也已接入同一个 `instabox` MCP，可以直接让 Claude Code 管理云机器：

```powershell
claude mcp list | Select-String instabox
```

### 2.5 打包（仅 Windows）

推荐用包装脚本，它会先做版本/BOM/工作区预检，再把四个镜像数组完整传进
`build.ps1`（旧写法用 `-File` 传数组时，最后一个 URL 会被当成下一个参数）。
版本号改完要先提交，脚本默认拒绝在脏工作区上打正式包：

```powershell
# 0) 每台构建机一次：生成 manifest 签名私钥（仓库外），把公钥写回源码
python scripts\manifest_sign.py keygen --apply

# 先看参数，不编译
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2 -Plan

# 预检 + 本地测试 + 编译 + 深度校验
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2
```

打包时 `build.ps1` 会计算三层 native 的 SHA-256、写 `version=2` 的
`runtime/integrity.json`、用构建机私钥签名，再用编译进程序的公钥验一遍；
任一步失败直接终止。私钥丢了就换新公钥重新发版，旧包无法升级到新的
manifest 体系，所以 `%USERPROFILE%\.hxp\manifest.key` 要备份。

等价的手工命令：

```powershell
cd app
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
  -RollbackVersion 0.1.9 -NoDataMigration `
  -DownloadBaseUrl "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2" `
  -MirrorBaseUrl @("https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2",
                   "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2",
                   "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2") `
  -RollbackBaseUrl "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9" `
  -RollbackMirrorBaseUrl @("https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9",
                           "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9",
                           "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9")
```

打包后必须核对：

1. `dist/好想跑_v<version>/好想跑.exe --check` 退出码 0；
2. `--no-window --port <port>` 登录页 200；
3. zip 221 项左右，不含 `data/`、`*.py`、`*.pyc`；
4. `installer.nsi` 前三字节是 `EF BB BF`；
5. `latest.json` 的 zip / setup / rollback 各 4 个 URL；
6. `dist/好想跑_v<version>/runtime/integrity.json` 有 `sig` 且能被
   `MANIFEST_PUBKEY` 验过（`release_check --deep` 已包含这一项）；
   对不上就说明用错了密钥或包是 `-Unsigned` 打的。

### 2.6 发布

1. 更新 `RELEASE_NOTES_<version>.md`（测试数、产物哈希、已知限制）。
2. 同步 `github-release/README.md` 的版本、大小和下载链接。
3. 重新生成清单（4 个镜像）：

```powershell
cd app
python packaging\make_latest.py --dist dist --version 0.2.2 `
  --base-url "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2" `
  --mirror-base-url "https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2" `
  --mirror-base-url "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2" `
  --mirror-base-url "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.2" `
  --rollback-version 0.1.9 `
  --rollback-base-url "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9" `
  --rollback-mirror-base-url "https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9" `
  --rollback-mirror-base-url "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9" `
  --rollback-mirror-base-url "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.1.9"
Copy-Item dist\latest.json ..\github-release\latest.json -Force
```

4. 发布分支是**独立的 release 线**（`origin/main` 只有 README / assets /
   latest.json，与源码 `master` 没有共同祖先）。把 `github-release/` 的内容
   提交到 `main`，打 tag `v<version>`，再推 `main` 和 tag。
   **tag 不带源码**，所以「回滚」指下载 `latest.json` 里 `rollback` 指向的
   旧二进制覆盖安装，不是从 tag 重建源码。
5. 上传 5 个资产到同一个 Release：
   `setup.exe`、`setup.sha256`、`zip`、`zip.sha256`、`latest.json`。
6. 重新下载 Release 资产，核对 SHA256；确认 raw `main/latest.json` 和 jsDelivr
   都是新版本。
7. 不自动评论 issue #9；其他 issue 是否回复由操作者决定。

## 3. 测试矩阵

| 环境 | 命令 | 期望 |
|---|---|---|
| Windows 本地 | `pytest tests -q` | 694 passed |
| InstaBox Linux | `python scripts/cloud_test.py` | 675 passed, 19 skipped |
| Windows 冻结包 | `好想跑.exe --check` | exit 0 |
| Windows HTTP | `--no-window --port` + 登录页 | 200 |
| 自检 | `python -m replica.selfcheck` | 28/28 |
| JS | `node --check` | 无输出 |

Windows-only 测试用 `@pytest.mark.windows_only` 标记；非 Windows 由
`app/tests/conftest.py` 统一 skip，不要靠删测试或 `--ignore` 整文件。

## 4. 资源与安全

- 云机器只放**代码和公开产物**；`data/`（token、设备指纹、回执、坐标）、
  `private/`、`reverse-engineering/`、`archive/` 一律不上传。
- `git archive HEAD` 是默认上传方式；不要 `scp -r` 整个工作区。
- 云机器空闲 30 分钟自动暂停；不用时 `suspend`，下次用会自动唤醒。
- 旧版本产物确认在 GitHub Release（核对 SHA256）后删本地；不在 Release 的
  旧版本先传到云机器 `/data/archive/haoxiangpao/` 再删。

## 5. 当前基线（2026-11-10）

- 产品版本：`0.2.2`（2026-11-10 已发布）；回滚版本 `0.1.9`。
- Release：https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/tag/v0.2.2
- 本地：695 passed；云 Linux：676 passed + 19 skipped。
- 云机器：`agent-browser-yrbo`（Linux，OpenCode/Claude/Codex/Docker）、
  `b`（Android 14，adb 在线）。
- 参考项目：`docs/reference-projects.md`；检查脚本 `scripts/check_references.py`，
  基线状态在 `output/reference-state.json`（不提交）。
- 已知坑：`installer.nsi` 必须 UTF-8 BOM；`origin/main` 是发布孤儿分支；
  `pyproject.toml` 版本容易漏改；GitHub 直链 HEAD 偶尔 504，实际 GET 可成功。

## 6. 工作流自检记录（v0.2.2）

v0.2.2 是小补丁，重点验证 issue #14 的 `policy` 误报和公告暗号：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 参考项目 | `python scripts\check_references.py` | `NO CHANGES` |
| 版本/BOM 预检 | `python scripts\release_check.py --source-only` | PASS，0 warning |
| 本地测试 | `cd app; python -m pytest tests -q` | 695 passed |
| 云 Linux | `python scripts\cloud_test.py` | 676 passed, 19 skipped |
| 浏览器实测 | 公告弹窗 | 显示「加群暗号：皮卡丘」，正文无群号 |
| 打包 | `scripts\release.ps1 -Version 0.2.2` | 5/5，深哈希通过 |
| 发布 | `gh release create v0.2.2 --latest` | 5 资产，latest，SHA256 复核通过 |
| jsDelivr | `purge.jsdelivr.net` 后复查 | `latest.json` 为 0.2.2 |

上一轮 v0.2.1 的完整流水线记录保留在 `output/reviews/0.2.1/` 和
`output/build-0.2.1.log`。

### v0.2.1（2026-11-10）

这一轮用 v0.2.1 的源码把整条流水线走了一遍，最后完成打包和发布：

| 步骤 | 命令 | 结果 |
|---|---|---|
| 参考项目 | `python scripts\check_references.py` | `NO CHANGES` |
| 版本/BOM 预检 | `python scripts\release_check.py --source-only` | PASS，0 warning |
| 本地测试 | `cd app; python -m pytest tests -q` | 694 passed |
| 云 Linux | `python scripts\cloud_test.py` | 675 passed, 19 skipped |
| 打包 | `scripts\release.ps1 -Version 0.2.2` | 5/5，深哈希通过 |
| 发布 | `gh release create v0.2.1 --latest` | 5 资产，latest，SHA256 复核通过 |
| Claude 审查 | `scripts\claude_review.ps1 -Round N -WithTests` | round 1 发现阻塞 → 修复 → round 2/3/5 PASS（`output/reviews/0.2.1/`） |

发现并修掉的工作流问题：

1. **OBS 窗口距离精度 2 位、提交体 4 位**：同一条记录两个载荷差几毫米。
   OBS 统一到 4 位，并加逐项回归。
2. **Claude 审查的美元上限默认值太小**：`--max-budget-usd 2` 在读完文件前
   就中止，输出文件只有一行错误。默认改为 0（不限），需要限额时显式传
   `-MaxBudgetUsd`，命中上限会给出明确报错。
3. **审查范围太宽会跑很久**：提示词现在要求先 `git diff --stat HEAD~2..HEAD`，
   最多读 15 个文件，不递归扫 `output/`、`archive/`、`reverse-engineering/`。
4. **云测试必须先提交**：`git archive HEAD` 不会带未提交的测试；脚本默认拒绝
   脏工作区，`--allow-dirty` 只用于明确要验证 HEAD 的场合。
5. **版本预检漏掉发布说明和更新器文案**：`release_check.py` 现在会检查
   `RELEASE_NOTES_<version>.md` 是否存在，以及 `make_latest.py` 里有没有
   对应的 `RELEASE_NOTES['<version>']` 和 `DEFAULT_VERSION`。
6. **OBS 的 220 spm 上限会截断步数窗**：UI 允许 260 spm，旧上限让 OBS 与
   提交体在 240 spm 时差 351 步。删除上限，窗口步数完全以 `rundata` 为准，
   并加 240 spm 回归。
7. **回滚说明与 release tag 不一致**：说明里的“保留为回滚版本”容易读成
   v0.2.0；实际 `latest.json` 回滚到 v0.1.9。文档已明确 tag 在独立发布分支、
   不含源码，回滚指旧二进制覆盖安装。
8. **公告写死在 `index.html` / `app.js`**：改为 `announcements.py` 数据目录 +
   `js/announcements.js` 通用渲染。以后换文案、换二维码、加第二条公告只改
   catalog；已读状态按 id 存 `announcement_seen`。

## 7. 公告怎么发

公告内容不再写死在 `index.html` / `app.js`，只改一个目录：

| 要改什么 | 改哪里 |
|---|---|
| 文案、二维码、按钮文字、显示顺序 | `app/replica/application/announcements.py` 的 `ANNOUNCEMENTS` |
| 二维码/配图 | 放到 `app/frontend/assets/`，在 catalog 里写 `assets/<文件名>` |
| 同一条公告改完想再弹一次 | 把该条的 `version` 加 1 |
| 新增一条公告 | 在 `ANNOUNCEMENTS` 里追加一个 dict，`id` 唯一 |
| 暂时下线 | 该条 `enabled` 设为 `False` |

已读状态按 id 存在 `data/ui_state.json`：

```json
{"announcement_seen": {"qq-group-2026-11": 1}}
```

前端 `app/frontend/js/announcements.js` 只负责通用渲染、图片、复制按钮和
“下次不再自动显示”，正常加公告不需要改它。设置 → 关于里的「加入 QQ 群」
按钮用 `data-announcement-id` 打开指定公告。

验证：

```powershell
cd app
python -m pytest tests\test_v017_security.py tests\test_frontend_login.py -q
```

## 8. 知识库怎么维护

知识库入口是 [`kb/README.md`](kb/README.md)，机器目录是
[`kb/index.json`](kb/index.json)。

| 动作 | 命令 / 位置 |
|---|---|
| 检索 | `python scripts\kb_search.py <关键词>`（`--scope kb` 只看知识库） |
| 校验目录和文件一致 | `python scripts\kb_search.py --check` |
| 列卡片 | `python scripts\kb_search.py --list` |
| 测试 | `cd app; python -m pytest tests\test_kb.py -q` |

新增或改卡片时：

1. 在 `docs/kb/` 改对应 `.md`，标好证据等级（A/B/C/D）；
2. 同步 `index.json` 的 `summary` / `tags` / `updated`；
3. 发版时把 `index.json` 的 `version` 改成新版本；
4. 跑 `--check` 和 `test_kb.py`。

**不写凭据**：token、密码、完整 DeviceId、cookie、坐标、账号、本地绝对路径。
C/D 级信息只能写成“社区口径/待验证”，不能写成平台事实。


