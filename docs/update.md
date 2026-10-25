# 自动更新元数据：latest.json

本轮只准备**元数据**，不实现下载/替换逻辑。目标是让以后的更新器只依赖一个
稳定的入口文件，而不是从网页或文件名猜版本。

## 生成

打包脚本在 zip 和 `.sha256` 之后自动生成：

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

生成器从 `replica/build_info.py` 读版本号，从 zip 现算 SHA-256 和字节数，
所以不会出现“版本号对了、哈希是旧的”这种情况。

## 结构

```json
{
  "schema": 1,
  "app": "haoxiangpao",
  "name": "好想跑",
  "channel": "stable",
  "version": "0.1.2",
  "min_version": "0.1.1",
  "released_at": "2026-10-25T12:34:56+08:00",
  "notes": ["..."],
  "assets": {
    "windows-x64": {
      "file": "好想跑_v0.1.2_win64.zip",
      "url": "%E5%A5%BD%E6%83%B3%E8%B7%91_v0.1.2_win64.zip",
      "size": 78081761,
      "sha256": "…64 位十六进制…",
      "format": "zip",
      "layout": "onedir",
      "entry": "好想跑.exe",
      "version": "0.1.2"
    }
  },
  "rollback": {
    "version": "0.1.1",
    "file": "好想跑_v0.1.1_win64.zip",
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
| `assets.windows-x64` | 当前平台包；`url` 可为相对路径 |
| `size` / `sha256` | 下载后必须校验；不一致就丢弃，不替换 |
| `layout` | `onedir`：解压后是 `好想跑.exe` + `_internal`，不能只替换 exe |
| `rollback` | 上一稳定版，用于手动或自动回滚 |
| `min_version` | 见上；`0.1.1` 是当前保留的回滚基线 |

## URL 约定

- 默认写**相对 URL**（文件名经过百分号编码）。客户端用 `latest.json` 的
  最终 URL 作为 base 解析即可；这样换域名、放本地目录、放对象存储都不用改清单。
- 用 `-DownloadBaseUrl` / `--base-url` 时写绝对 URL，适合 CDN 或固定下载页。
- `url` 是百分号编码后的；`file` 保留原始中文文件名，方便人工核对。
- 不要把 token、账号、`data/` 路径写进清单；它会被公开下载。

### GitHub Releases

把 `latest.json` 和 zip 作为**同一个 Release 的资产**上传，客户端请求：

```text
https://github.com/<owner>/<repo>/releases/latest/download/latest.json
```

相对 `url` 会解析成：

```text
https://github.com/<owner>/<repo>/releases/latest/download/好想跑_v0.1.2_win64.zip
```

这是 GitHub 的固定“最新版资产”路径，换版本不用改客户端。**不要**让客户端
从 `raw.githubusercontent.com/.../github-release/latest.json` 读取，除非 zip
也放在同一个 raw 目录；raw 路径不会自动指向 Release 资产。

## 发布顺序

1. 打包：`powershell -ExecutionPolicy Bypass -File packaging\build.ps1`
2. 上传 `dist/好想跑_v<version>_win64.zip` 和 `dist/latest.json` 到同一目录；
   如果用绝对 URL，上传后核对 `url` 是否可下载。
3. 保留上一版 zip，供 `rollback` 和手动回退使用。
4. 同版本重建会更新 `sha256`/`size`，必须重新上传 `latest.json`，否则客户端
   会按旧哈希拒绝新包。

## 未来的更新器流程（建议）

```text
GET latest.json (HTTPS)
  → 比较 version 与本地 build_info.version
  → 展示 notes，用户确认
  → 下载 assets.windows-x64.url 到临时目录
  → 校验 size + sha256
  → 关闭应用
  → 解压到 staging；保留现有 data/ 不动
  → 用 staging 替换程序目录（失败则回滚到旧目录）
  → 重启 好想跑.exe
```

更新器**不要**覆盖 `data/`；账号、回执、限额、设备指纹都在那里。当前
`migrate_data.ps1` 的“同版本重建保留 data/”规则同样适用于更新。

## 当前限制

- 只生成 Windows x64（`windows-x64`）资产；没有 macOS/Linux 包。
- 没有签名，只有 SHA-256；正式分发建议再加 HTTPS + 代码签名。
- 还没有应用内检查更新按钮；本轮只把元数据和构建标识准备好。
