# 50 · 打包与发布

> 证据等级：**A**（脚本 / 实际发布记录）+ **B**（Nuitka 官方文档）
> 最后更新：2026-11-11 · 对应版本 **0.2.2**

## 1. 版本号落点

发布前 `python scripts\release_check.py --source-only` 会逐项检查：

| 落点 | 文件 | 说明 |
|---|---|---|
| 1 | `app/replica/build_info.py` | `VERSION`，运行时唯一来源 |
| 2 | `app/pyproject.toml` | `version` |
| 3 | `app/packaging/installer.nsi` | `!define VERSION`，**必须 UTF-8 BOM** |
| 4 | `app/frontend/index.html` | 4 处显示版本 |
| 5 | `app/packaging/USAGE.txt` | 首行 + 本次更新 |
| 6 | `app/packaging/make_latest.py` | `DEFAULT_VERSION` + `RELEASE_NOTES['x.y.z']` |

另外还要有 `RELEASE_NOTES_<version>.md`。
漏一个，`release_check` 会 WARN/FAIL，不要跳过。

## 2. 构建

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2
# 只预检、不构建：
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2 -Plan
```

产物在 `app/dist/`：

```text
haoxiangpao_v0.2.2_win64.zip
haoxiangpao_v0.2.2_win64.sha256
haoxiangpao_v0.2.2_win64_setup.exe
haoxiangpao_v0.2.2_win64_setup.sha256
latest.json
好想跑_v0.2.2/           （onedir 目录，调试用）
```

`app/dist/latest.json` 会自动同步到 `github-release/latest.json`。

### Nuitka 取舍

- 默认 **standalone + onedir**，不是 onefile：
  - onefile 运行时会释放内容到临时目录，反而更容易抓；
  - onedir 启动快、更新器按目录覆盖更简单。
- `--lto=yes`、`--python-flag=no_docstrings`、`--python-flag=no_asserts`。
- 打包后扫描产物：出现 `replica/**/*.py`、`app.py`、`replica/**/*.pyc` 就构建失败。
- **Nuitka 不是加密**：内存 dump、API hook、字符串搜索仍可还原部分逻辑。
- PyInstaller 只作兼容回退，不用于正式发布。

> Nuitka 官方也说明：Windows 上默认设置编译出的二进制**经常被杀软误报**。
> 这是通用现象，不是本项目特有；不要把它当成“包坏了”。
> <https://nuitka.net/user-documentation/common-issue-solutions.html>（B 级）

### NSIS

- `installer.nsi` 必须 **UTF-8 BOM**，否则中文乱码/编译失败。
- 安装包跳过 `data/`，卸载默认保留。
- 资产名全 ASCII；中文名会被 GitHub 重写，导致 `latest.json` 404。

## 3. latest.json

结构见 [`../update.md`](../update.md) §结构。关键规则：

| 字段 | 规则 |
|---|---|
| `version` | 客户端只做版本比较，不猜文件名 |
| `min_version` | 低于它的版本要先升到 `0.1.4` |
| `assets.windows-x64` | 必须是 zip，`layout=onedir` |
| `assets.windows-x64-setup` | `format=installer`；缺失时回退 zip |
| `size` / `sha256` | 下载后必须校验；缺一不可 |
| `url` | 主地址，老客户端只认它 |
| `urls` | 镜像在前、官方直链最后；客户端测速后重试 |
| `rollback` | 上一稳定版，手动回滚用 |

- 只接受 HTTPS（`localhost` 除外）。
- 同版本重建会改 `sha256` / `size`，必须重新上传 `latest.json`。
- 不要把 token、账号、本地路径写进清单（公网可下载）。

## 4. 发布 SOP（v0.2.2 实际流程）

```powershell
# 0. 参考项目（协议改动时）
python scripts\check_references.py

# 0b. manifest 签名密钥（每台构建机一次；私钥在仓库外）
python scripts\manifest_sign.py keygen --apply

# 1. 源码预检
python scripts\release_check.py --source-only

# 2. 本地全量
cd app; python -m pytest tests -q; cd ..

# 3. 云 Linux（先提交，脚本拒绝脏工作区）
python scripts\cloud_test.py

# 4. 构建（会跑 5/5 + 深哈希 + release_check --deep）
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2
```

然后：

```powershell
# 5. 把 README + latest.json 放进 release 分支的临时 worktree
git worktree add -b release/0.2.2 C:\Temp\hxp-release-022 origin/main
Copy-Item github-release\README.md C:\Temp\hxp-release-022\README.md -Force
Copy-Item github-release\latest.json C:\Temp\hxp-release-022\latest.json -Force
cd C:\Temp\hxp-release-022
git add README.md latest.json
git commit -m "release: publish 0.2.2 manifest (rollback 0.1.9)"
git tag -a v0.2.2 -m "好想跑 v0.2.2"
git push origin HEAD:main
git push origin v0.2.2

# 6. 发布（5 个资产）
gh release create v0.2.2 --latest --title "好想跑 v0.2.2" `
  --notes-file RELEASE_NOTES_0.2.2.md `
  app/dist/haoxiangpao_v0.2.2_win64_setup.exe `
  app/dist/haoxiangpao_v0.2.2_win64_setup.sha256 `
  app/dist/haoxiangpao_v0.2.2_win64.zip `
  app/dist/haoxiangpao_v0.2.2_win64.sha256 `
  app/dist/latest.json
```

### 发布后复核

1. `gh release view v0.2.2 --json assets`：5 个资产、`state=uploaded`、digest 正确。
2. `gh api repos/.../releases/latest`：tag 是 `v0.2.2`。
3. 下载资产重算 SHA256，和 `dist/*.sha256` 一致。
4. raw `main/latest.json` 是 0.2.2。
5. jsDelivr 若还是旧版，执行：

```text
https://purge.jsdelivr.net/gh/Knight-ask-art/KnightSportsWorldTool@main/latest.json
```

再查 `cdn.jsdelivr.net/.../latest.json`。

## 5. 回滚

- `latest.json.rollback` 指向 `0.1.9`。
- 客户端**不会自动降级**；手动下载 rollback 资产覆盖。
- 回滚不动 `data/`；若指纹也换了，用 `device_profile.backup-*.json` 恢复。

## 6. 失败清单

| 现象 | 原因 | 处理 |
|---|---|---|
| `release_check` 报版本不一致 | 6 个落点漏改 | 按 §1 全改 |
| 构建报 `manifest signing failed` | 构建机没有私钥 | `python scripts\manifest_sign.py keygen --apply`，或设 `JHP_MANIFEST_KEY` |
| 构建报 `does not verify against MANIFEST_PUBKEY` | 换了密钥但没更新常量 | 重新 `keygen --apply`，或把公钥改成与私钥匹配 |
| 登录、恢复会话或提交报“安装文件校验未通过” | 包是 `-Unsigned`、manifest/sig 被改过，或打包后替换过 sidecar | 用正式签名流程重打；不要发布 `-Unsigned` 包 |
| NSIS 中文乱码/编译失败 | 文件不是 UTF-8 BOM | 重新保存为 BOM |
| Release 链接 404 | 资产名带中文被 GitHub 重写 | 资产名只用 ASCII |
| 更新器拒绝新包 | 同版本重建后没换 `latest.json` | 重新上传清单 |
| raw 已更新、jsDelivr 仍旧 | CDN 缓存 | 用 purge 接口 |
| `git rev-parse v0.2.2^{commit}` 在 PowerShell 报错 | `^` 被解析 | 加引号：`git rev-parse "v0.2.2^{commit}"` |
| 杀软报毒 | Nuitka 通用误报 | 说明来源 + SHA256；不要声称“绝对安全” |
| 发布分支和源码分支混了 | `origin/main` 是独立 release 分支 | 源码在 `master`，发布只推 README/latest.json |

## 7. 发布纪律

- **未经明确授权不打包、不发布**。
- 发布前工作区必须干净；云测试基于 `git archive HEAD`。
- 每次发布保留上一版资产，供回滚。
- Release Notes、`latest.json.notes`、`USAGE.txt` 三处文案要一致。
- 发布后把版本、哈希、验证结果记到 `docs/workflow.md`。
