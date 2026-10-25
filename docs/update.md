# 自动更新：latest.json

客户端只读一个稳定入口，不从网页或文件名猜版本。清单里同时列出**安装包**和
**zip**，客户端按自己的安装形态选一个：

| 当前形态 | 用哪个资产 | 更新方式 |
|---|---|---|
| NSIS 安装版（注册表有 `Software\好想跑`） | `windows-x64-setup` | 下载后 `/S` 静默安装 |
| 便携版（解压即用） | `windows-x64` | 解压覆盖，保留 `data/` |

两条轨道互不干扰，谁都不用搬 `data/`。`windows-x64` **必须一直是 zip**：
0.1.2 客户端只认这个键，也只认 zip，换成 exe 会让那一批人更新直接报错。

## 生成

打包脚本在 zip / 安装包和 `.sha256` 之后自动生成：

```text
app/dist/latest.json
github-release/latest.json   # 发布目录里的副本（build.ps1 自动同步）
```

也可以单独生成（不重新打包）：

```powershell
cd app
python packaging\make_latest.py --dist dist
# 指定下载前缀（写到 CDN / 对象存储时）
python packaging\make_latest.py --dist dist --base-url https://example.com/hxp
# 或直接写到 GitHub Release 发布目录
python packaging\make_latest.py --dist dist --out ..\github-release\latest.json
```

生成器从 `replica/build_info.py` 读版本号，从资产现算 SHA-256 和字节数，
所以不会出现「版本号对了、哈希是旧的」。安装包不存在时只写 zip，并在 stderr
提示，不会伪造一个下载项。

## 结构

```json
{
  "schema": 1,
  "app": "haoxiangpao",
  "name": "好想跑",
  "channel": "stable",
  "version": "0.1.3",
  "min_version": "0.1.1",
  "released_at": "2026-10-25T12:34:56+08:00",
  "notes": ["..."],
  "assets": {
    "windows-x64": {
      "file": "haoxiangpao_v0.1.3_win64.zip",
      "url": "haoxiangpao_v0.1.3_win64.zip",
      "size": 78081761,
      "sha256": "…64 位十六进制…",
      "format": "zip",
      "layout": "onedir",
      "entry": "好想跑.exe",
      "version": "0.1.3"
    },
    "windows-x64-setup": {
      "file": "haoxiangpao_v0.1.3_win64_setup.exe",
      "url": "haoxiangpao_v0.1.3_win64_setup.exe",
      "size": 51234567,
      "sha256": "…64 位十六进制…",
      "format": "installer",
      "layout": "installer",
      "entry": "好想跑.exe",
      "version": "0.1.3"
    }
  },
  "rollback": {
    "version": "0.1.2",
    "file": "haoxiangpao_v0.1.2_win64.zip",
    "url": "…",
    "size": 78079627,
    "sha256": "…",
    "format": "zip",
    "layout": "onedir",
    "entry": "好想跑.exe"
  },
  "generated_by": "packaging/make_latest.py"
}
```

| 字段 | 用途 |
|---|---|
| `schema` | 清单格式版本；不兼容变更时加一 |
| `version` | 最新版本，客户端只做**版本比较**，不猜文件名 |
| `min_version` | 支持从哪个版本直接升级；更老的版本应先升级到 `0.1.1` |
| `released_at` | 北京时间 ISO-8601，仅展示/排序 |
| `notes` | 更新说明，客户端可直接显示 |
| `assets.windows-x64` | 便携版资产，**必须是 zip** |
| `assets.windows-x64-setup` | 安装版资产，`format=installer`；没有这个键时安装版回退到 zip |
| `format` | `zip` 或 `installer`，决定更新器走哪条路径 |
| `layout` | `onedir`（解压即用）或 `installer`（NSIS） |
| `size` / `sha256` | 下载后必须校验；**安装包没有 sha256 会被直接拒绝**（下载完就要执行它） |
| `rollback` | 上一稳定版，用于手动或自动回滚 |

## URL 约定

- 默认写**相对 URL**（文件名经过百分号编码）。客户端用 `latest.json` 的最终 URL
  作为 base 解析；这样换域名、放本地目录、放对象存储都不用改清单。
- 用 `-DownloadBaseUrl` / `--base-url` 时写绝对 URL，适合 CDN 或固定下载页。
- 资产名全部 ASCII，所以 `url` 通常和 `file` 一样，不需要百分号编码。
  **不要**再用中文资产名：GitHub 会把 `好想跑_v0.1.2_win64.zip` 重写成
  `_v0.1.2_win64.zip`，清单里的链接就 404 了。
- 不要把 token、账号、`data/` 路径写进清单；它会被公开下载。

### GitHub Releases

把 `latest.json`、zip、安装包作为**同一个 Release 的资产**上传，客户端请求：

```text
https://github.com/<owner>/<repo>/releases/latest/download/latest.json
```

相对 `url` 会解析成：

```text
https://github.com/<owner>/<repo>/releases/latest/download/haoxiangpao_v0.1.3_win64_setup.exe
```

这是 GitHub 的固定「最新版资产」路径，换版本不用改客户端。**不要**让客户端
从 `raw.githubusercontent.com/.../github-release/latest.json` 读取，除非资产也
放在同一个 raw 目录；raw 路径不会自动指向 Release 资产。

国内网络走 `https://gh.llkk.cc/https://github.com/...` 镜像；清单本身保留
jsDelivr 作为 raw 的回退。

## 发布顺序

1. 打包：`powershell -ExecutionPolicy Bypass -File packaging\build.ps1`
   （需要 `makensis`，见 `docs/packaging.md`）
2. 上传这五个文件到同一个 Release：
   - `haoxiangpao_v<version>_win64_setup.exe`
   - `haoxiangpao_v<version>_win64_setup.sha256`
   - `haoxiangpao_v<version>_win64.zip`
   - `haoxiangpao_v<version>_win64.zip.sha256`
   - `latest.json`
3. 保留上一版资产，供 `rollback` 和手动回退使用。
4. 同版本重建会更新 `sha256`/`size`，必须重新上传 `latest.json`，否则客户端
   会按旧哈希拒绝新包。

## 更新器流程

两种形态共用前四步，之后分叉：

```text
GET latest.json (HTTPS，失败回退 jsDelivr)
  → 比较 version 与 build_info.VERSION
  → 展示 notes，用户确认
  → 下载到 data/updates，校验 size + sha256
  ├─ 安装版（format=installer）
  │    → 等待本进程退出
  │    → setup.exe /S
  │    → 启动新的 好想跑.exe
  └─ 便携版（format=zip）
       → 解压到 data/updates/staging（跳过 data/）
       → robocopy 覆盖程序目录（/XD data）
       → 启动新的 好想跑.exe
```

更新器**不覆盖** `data/`：账号、回执、限额、设备指纹都在那里。安装包的
`installer.nsi` 同样跳过 `data/`，卸载时默认保留。

## 当前限制

- 只生成 Windows x64 资产；没有 macOS/Linux 包。
- 没有代码签名，只有 SHA-256；SmartScreen 仍会提示「未知发布者」，需要用户点
  「更多信息 → 仍要运行」。
- 没有增量更新：每次都是完整包（zip 约 76MB；安装包用 LZMA 固实压缩，预计更小，首次构建后以实测为准）。
- 回滚是手动的：下载 `rollback` 资产覆盖，客户端不会自动降级。
