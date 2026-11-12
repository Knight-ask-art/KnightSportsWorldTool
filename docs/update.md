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
# 使用未加密 Ed25519 PEM 私钥签名（Slice 1 原语；不传 --sign-key 时保持无签名输出）
python packaging\make_latest.py --dist dist --sign-key path\to\release.pem --key-id release --require-signature
```

`--sign-key` 读取未加密的 Ed25519 PEM 私钥在写盘前为清单签名，`--key-id` 默认
`release`；传入 `--require-signature` 但未提供 `--sign-key` 时直接以退出码 `2`
中止，避免误发未签名清单。私钥不入库、不打印到终端或日志。

生成器从 `replica/build_info.py` 读版本号，从资产现算 SHA-256 和字节数，
所以不会出现「版本号对了、哈希是旧的」。安装包不存在时只写 zip，并在 stderr
提示，不会伪造一个下载项。

回滚资产需要自己的下载前缀：`--base-url` 是 v0.1.5 时，把 v0.1.4 的文件名拼
上去会得到 `.../download/v0.1.5/haoxiangpao_v0.1.4_win64.zip`，是 404。显式
给回滚前缀（镜像同理）：

```powershell
python packaging\make_latest.py --dist dist --rollback-version 0.1.4 `
  --base-url https://gh-proxy.com/https://github.com/<owner>/<repo>/releases/download/v0.1.5 `
  --rollback-base-url https://gh-proxy.com/https://github.com/<owner>/<repo>/releases/download/v0.1.4
```

`--rollback-base-url` 省略时沿用 `--base-url`；`--rollback-mirror-base-url`
可以重复，写进回滚项的 `urls[]`。

## 结构

```json
{
  "schema": 1,
  "app": "haoxiangpao",
  "name": "好想跑",
  "channel": "stable",
  "version": "0.1.5",
  "min_version": "0.1.4",
  "released_at": "2026-10-25T12:34:56+08:00",
  "notes": ["..."],
  "assets": {
    "windows-x64": {
      "file": "haoxiangpao_v0.1.5_win64.zip",
      "url": "haoxiangpao_v0.1.5_win64.zip",
      "size": 78081761,
      "sha256": "…64 位十六进制…",
      "format": "zip",
      "layout": "onedir",
      "entry": "好想跑.exe",
      "version": "0.1.5"
    },
    "windows-x64-setup": {
      "file": "haoxiangpao_v0.1.5_win64_setup.exe",
      "url": "https://gh.llkk.cc/https://github.com/…/haoxiangpao_v0.1.5_win64_setup.exe",
      "urls": [
        "https://gh.llkk.cc/https://github.com/…/haoxiangpao_v0.1.5_win64_setup.exe",
        "https://gh-proxy.com/https://github.com/…/haoxiangpao_v0.1.5_win64_setup.exe",
        "https://ghfast.top/https://github.com/…/haoxiangpao_v0.1.5_win64_setup.exe",
        "https://github.com/…/haoxiangpao_v0.1.5_win64_setup.exe"
      ],
      "size": 51234567,
      "sha256": "…64 位十六进制…",
      "format": "installer",
      "layout": "installer",
      "entry": "好想跑.exe",
      "version": "0.1.5"
    }
  },
  "rollback": {
    "version": "0.1.4",
    "file": "haoxiangpao_v0.1.4_win64.zip",
    "url": "…",
    "size": 78079627,
    "sha256": "…",
    "format": "zip",
    "layout": "onedir",
    "entry": "好想跑.exe"
  },
  "generated_by": "packaging/make_latest.py",
  "signature": {
    "alg": "ed25519",
    "key_id": "release",
    "value": "<base64>"
  }
}
```

| 字段 | 用途 |
|---|---|
| `schema` | 清单格式版本；不兼容变更时加一 |
| `version` | 最新版本，客户端只做**版本比较**，不猜文件名 |
| `min_version` | 支持从哪个版本直接升级；更老的版本应先升级到 `0.1.4` |
| `released_at` | 北京时间 ISO-8601，仅展示/排序 |
| `notes` | 更新说明，客户端可直接显示 |
| `assets.windows-x64` | 便携版资产，**必须是 zip** |
| `assets.windows-x64-setup` | 安装版资产，`format=installer`；没有这个键时安装版回退到 zip |
| `format` | `zip` 或 `installer`，决定更新器走哪条路径 |
| `layout` | `onedir`（解压即用）或 `installer`（NSIS） |
| `size` / `sha256` | 下载后必须校验；**zip 和安装包缺少 sha256 都会被拒绝**（两者都会被自动执行/覆盖） |
| `url` | 主下载地址，老客户端只认这一个 |
| `urls` | 同一文件的全部渠道（官方直链在前、镜像按优先级在后）；客户端从上次成功节点开始轮换，失败依次重试。老客户端忽略该字段 |
| `rollback` | 上一稳定版，用于手动或自动回滚 |
| `signature` | 可选的 Ed25519 清单签名（`alg`/`key_id`/`value`）；老客户端忽略该字段。规范载荷为移除顶层 `signature` 后按 `sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False` 序列化的 UTF-8 JSON，并以 `key_id + "\0" + payload` 参与签名；源码中 `PUBLIC_KEYS` 默认为空，客户端验签接入留待后续步骤 |

## URL 约定

- **只接受 https**；`http://localhost` / `127.0.0.1` / `::1` 仅用于本地测试。
  明文 http 的资产或镜像会被更新器直接丢弃/拒绝，避免降级替换。
- 默认写**相对 URL**（文件名经过百分号编码）。客户端用 `latest.json` 的最终 URL
  作为 base 解析；这样换域名、放本地目录、放对象存储都不用改清单。
- 用 `-DownloadBaseUrl` / `--base-url` 时写绝对 URL，适合 CDN 或固定下载页。
- 资产名全部 ASCII，所以 `url` 通常和 `file` 一样，不需要百分号编码。
  **不要**再用中文资产名：GitHub 会把 `好想跑_v0.1.2_win64.zip` 重写成
  `_v0.1.2_win64.zip`，清单里的链接就 404 了。
- 不要把 token、账号、`data/` 路径写进清单；它会被公开下载。

### 多镜像

`--base-url` 写主地址（清单里的 `url`），`--mirror-base-url` 可重复，写进 `urls`。
顺序是「官方直链在前、镜像按优先级在后」；客户端从 `last_good_index` 开始轮换，
不会因为官方直链排在第一位就每次都先打它。

客户端 0.1.4 起，在用户点「下载更新」时对每个 `urls` 发一个
`GET` + `Range: bytes=0-65535`，收到第一块就断开，用
`ttfb + 64KB / 首块速度` 排序；6 秒预算内没答完的排到后面，全部失败就按清单
顺序来。选中的地址只是首选，下载后照常校验 `size` 和 `sha256`，不对就换下一个
——坏镜像最多浪费一次流量，装不进坏包。

0.1.3 及更老的客户端只读 `url`，不会看 `urls`；所以 `url` 必须是一个能用的地址，
不能写相对路径指望客户端自己拼镜像。

### 镜像注册表与发布前体检

- `app/packaging/update_mirrors.json` 是**候选镜像表**，不保证每个镜像长期可用；官方地址始终排在第一位。
- 发布流程：**打包 → 体检 → 过滤 → 签名生成**（先用 `packaging/check_update_mirrors.py --manifest ... --write-registry ...` 探测真实资产并筛出健康镜像，再用 `make_latest.py --mirror-registry ... --require-mirrors N` 重新生成最终清单）。
- 所有镜像 URL 都写入签名保护的 `latest.json`（`assets.*.urls[]` 与 `rollback.urls[]`），且每次下载仍校验资产的 `size` 与 `sha256`。
- 失效镜像、返回非预期魔数（如 HTML 拦截页）的节点以及明文 `http://` / `file://` 模板一律拒绝或剔除，绝不随正式清单发布。
- 客户端策略：优先探测上次成功节点 + 官方地址 + 前几个高优先级镜像，其余按需轮换重试。

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

国内网络走 `https://gh-proxy.com/https://github.com/...`，备选 `https://ghfast.top/`；
`gh.llkk.cc` 时好时坏，排最后。清单本身保留 jsDelivr 作为 raw 的回退。

## Go launcher 静默更新

- 启动顺序：launcher 先读 `data/updates/download.state.json`；只有 `phase=ready`、
  状态带有效 `sha256`/`size` 且包版本高于 `build_info.json` 时，才把自身复制到
  `%TEMP%\hxp-apply-<pid>.exe`，由 helper 等待 launcher 退出后替换。
- helper 解压到 `data/updates/staging-<version>`，保留 `data/`，按
  `app.new` → `app.old` → `app` 的顺序交换；启动新程序并等待
  `JHP_BOOT_OK_PATH` 写入匹配版本。成功删除 `.old`，失败时把新目录移回
  `.new`、恢复 `.old` 并重新启动旧程序。交换期间不会先删除任何一份程序或用户数据。
- 下载 worker 在 app 启动后派生：多镜像轮换、Range 断点续传、指数退避；
  父进程退出即停，状态与 `.part` 保留。状态文件只保存 `urls_hash` 与索引，
  不保存完整 URL 或 query token。
- 同一版本 boot-ok 失败后保持 `rolled_back`，worker 不会把保留的包重新提升为
  `ready`；只有清单版本或 `sha256` 变化才重置，避免“应用→失败→再应用”死循环。
- `--check` / `--dry-run` 只读：不恢复目录、不替换、不派生 worker。launcher 与
  apply helper 共用 `Local\Haoxiangpao.Launcher.v1` 互斥量，双击两次不会并发恢复/交换。
- 未经过签名清单写入的 `sha256`/`size` 的状态一律拒绝替换；本地状态损坏时
  helper 会先启动旧程序，绝不把安装目录留在“无程序可启动”的状态。

## 发布顺序

发布构建（正式签名 + 镜像体检）：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
  -DownloadBaseUrl https://github.com/<owner>/<repo>/releases/download/v<version> `
  -RollbackBaseUrl https://github.com/<owner>/<repo>/releases/download/v<rollback> `
  -SignKey D:\keys\hxp-release-ed25519.pem `
  -RequireSignature -MinHealthyMirrors 5
```

`build.ps1` 会：编译并注入公钥到 `hxp-launcher.exe` → 写入 `build_info.json`
→ 打包 zip/安装包 → 生成候选 `latest.json` → 探测镜像并只保留健康节点 →
用同一把私钥签最终清单。健康节点少于 `-MinHealthyMirrors` 时构建直接失败，
不会把死链发出去。没有 `-SignKey` 的构建仍可产出未签名包，但 launcher 不会
启用更新（fail-open）。

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
