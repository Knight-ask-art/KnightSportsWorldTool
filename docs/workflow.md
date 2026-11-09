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
  `app/packaging/installer.nsi`、`app/packaging/USAGE.txt`、`app/pyproject.toml`。
  用 `python scripts/release_check.py` 检查漏改。

### 2.3 验证

**Windows 本地（必须）**

```powershell
cd app
python -m pytest tests -q          # 687 passed
python -m replica.selfcheck        # 28/28
node --check frontend/js/app.js
node --check frontend/js/charts.js
python app.py --check              # exit 0
```

**InstaBox Linux（跨平台回归）**

```powershell
python scripts\cloud_test.py
# 668 passed, 19 skipped（DPAPI / 注册表 / updater 自动跳过）
```

脚本只上传 `git archive HEAD` 里的 tracked files，`data/`、`dist/`、`.git`
不会离开本机；首次运行会在云机器建一个 Python 3.11 venv 并装
pytest / requests / pycryptodome / numpy / opencv-python-headless。

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
- 审查产物写到 `output/reviews/<version>/round-N.md`（`output/` 不入库）。
- 审查者只读，不改代码；修复后重跑同一轮的复现命令。

### 2.5 打包（仅 Windows）

```powershell
cd app
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
  -RollbackVersion 0.1.9 -NoDataMigration `
  -DownloadBaseUrl "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0" `
  -MirrorBaseUrl @("https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0",
                   "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0",
                   "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0") `
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
5. `latest.json` 的 zip / setup / rollback 各 4 个 URL。

### 2.6 发布

1. 更新 `RELEASE_NOTES_<version>.md`（测试数、产物哈希、已知限制）。
2. 同步 `github-release/README.md` 的版本、大小和下载链接。
3. 重新生成清单（4 个镜像）：

```powershell
cd app
python packaging\make_latest.py --dist dist --version 0.2.0 `
  --base-url "https://gh-proxy.com/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0" `
  --mirror-base-url "https://ghfast.top/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0" `
  --mirror-base-url "https://gh.llkk.cc/https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0" `
  --mirror-base-url "https://github.com/Knight-ask-art/KnightSportsWorldTool/releases/download/v0.2.0" `
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
5. 上传 5 个资产到同一个 Release：
   `setup.exe`、`setup.sha256`、`zip`、`zip.sha256`、`latest.json`。
6. 重新下载 Release 资产，核对 SHA256；确认 raw `main/latest.json` 和 jsDelivr
   都是新版本。
7. 不自动评论 issue #9；其他 issue 是否回复由操作者决定。

## 3. 测试矩阵

| 环境 | 命令 | 期望 |
|---|---|---|
| Windows 本地 | `pytest tests -q` | 687 passed |
| InstaBox Linux | `python scripts/cloud_test.py` | 668 passed, 19 skipped |
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

## 5. 当前基线（2026-11-09）

- 产品版本：`0.2.0`；回滚版本 `0.1.9`。
- 本地：687 passed；云 Linux：668 passed + 19 skipped。
- 云机器：`agent-browser-yrbo`（Linux，OpenCode/Claude/Codex/Docker）、
  `b`（Android 14，adb 在线）。
- 已知坑：`installer.nsi` 必须 UTF-8 BOM；`origin/main` 是发布孤儿分支；
  `pyproject.toml` 版本容易漏改；GitHub 直链 HEAD 偶尔 504，实际 GET 可成功。

