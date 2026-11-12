# 打包与源码保护

> 结论先行：Nuitka 能显著提高反编译成本，但**不能保证绝对不可逆**。
> 真正的边界是服务端校验和不下发敏感密钥；客户端加固只是提高门槛。

## 原生核心：Rust sidecar

- 签名与 NetSecKit 信封在 `rust/hxp-core`，打包时先跑
  `cargo build --release --locked`（需要 Rust toolchain）。
- 产物复制到 `dist/好想跑_v*/runtime/rust/hxp-core.exe`，由
  `app/replica/transport/rustcore.py` 以行式 JSON 调用。
- 找不到二进制或 sidecar 崩溃时回退 Python，源码运行/纯 Python 测试不受影响；
  想强制检查原生路径可以用 `JHP_RUST_CORE=1`。
- `app.py` 在 Nuitka 构建里会自动把 `JHP_RUST_CORE` 置为 `1`：发布包丢了
  `runtime/rust/hxp-core.exe` 就在第一次加密调用时报错，不会静默退回 Python。
  显式设 `0` 仍可用于排错。
- Rust 提升的是静态分析和补丁成本，不是「不可逆向」；它不替代服务端校验。

## 原生核心：Go sidecar

- `go/hxp-guard` 负责本机的一号一设备绑定；打包时执行
  `go build -trimpath -ldflags="-s -w"`，复制到
  `dist/好想跑_v*/runtime/go/hxp-guard.exe`。
- Python 侧 `app/replica/application/account_guard.py` 以 CLI/JSON 调用；
  二进制缺失时源码运行放行，正式包由构建脚本保证存在。
- 状态文件 `data/account_binding.json` 只存账号哈希和脱敏提示，带 HMAC；
  不是强 DRM，规则与边界见 `docs/kb/46-account-guard.md`。

## 原生核心：C++ shield sidecar

- `cpp/hxp-shield` 负责短时设备凭证（TTL 3 s）。打包时调用
  `cpp/hxp-shield/build.ps1`（MSVC x64，`/O2 /GL`，无 PDB），再把
  `hxp-shield.exe` 复制到 `dist/好想跑_v*/runtime/cpp/hxp-shield.exe`。
- 复制完成后立即执行一次 `hxp-shield.exe --selftest`；失败即构建失败，
  不会把验签不了的凭证层发出去。
- 同一阶段还要 smoke test 随包的 Go/Rust 层：用固定 16 B nonce 走
  `hxp-guard.exe op --state <tmp> --code 17`（stdin 传 nonce），向
  `hxp-core.exe` 发 `{"op":"self_test"}`；任一没有返回 `"ok":true`
  即 `throw`。这样坏副产物不会拖到用户第一次登录才暴露。
- 只复制 exe：`.obj` / `.pdb` 不进入 `runtime/cpp/`，也不进入 zip/安装包。
- Python 侧 `app/replica/application/shield.py` 在同一个进程里发 `seal`
  再发 `check`；跨进程 token 会被 `check` 拒绝。
- frozen 构建缺这一层时登录直接失败（`shield.enabled()` 在 frozen 下恒为真，
  `JHP_SHIELD=0` 不再生效）；源码运行缺二进制则跳过，保证 `pytest` 不依赖
  native 产物。

## 默认后端：Nuitka standalone

`packaging/build.ps1` 默认使用 Nuitka 4.x，把 Python 模块编译成 C 再链接，
输出 **onedir**（`好想跑.exe` + 依赖 DLL/数据），不是 onefile：

- 不把源码解压到临时目录；onefile 运行时会释放内容，反而更容易抓。
- `--lto=yes`：链接期优化，去掉部分符号/中间层。
- `--python-flag=no_docstrings`：不打包 docstring。
- `--python-flag=no_asserts`：移除 assert，减少内部逻辑暴露。
- `--enable-plugin=pywebview`：带入 EdgeChromium / pythonnet 运行时。
- `--include-data-dir=frontend=frontend`：前端静态文件放在 exe 同级。
- `--include-package-data=webview/clr_loader/pythonnet/cv2`：运行时 DLL、配置。
- `--nofollow-import-to=pytest/tests/tools`：开发代码不进包。
- `--windows-console-mode=disable`：GUI 构建不弹控制台。

### 源码泄漏门禁

打包完成后脚本会扫描产物；只要出现以下任一情况就**构建失败**：

- `replica/**/*.py`
- `app.py`
- `replica/**/*.pyc`

依赖包的数据文件（例如 `cv2/config.py`）允许存在，它们不是本项目算法。
前端 `frontend/` 本来就是明文静态资源，不属于 Python 源码保护范围。

### 必须知道的限制

- Nuitka 不是加密。攻击者仍可通过内存 dump、API hook、动态调试、字符串搜索
  还原部分逻辑；C 反编译也比 Python 难，但不是不可能。
- 不要只靠客户端做授权/次数限制；服务端必须能独立判定。
- 不要在客户端硬编码长期密钥、后台账号、私有 API key。打包只能防止“顺手拿走”，
  不能防止有动机的逆向者。
- PyInstaller 只作为兼容回退（`-Backend pyinstaller`），**不要用于正式发布**：
  `python -m pyinstaller` 的产物可以直接解包出 `.pyc`。
- 代码签名、HTTPS、哈希校验、服务端风控比“藏代码”更重要。

## 构建

```powershell
cd app
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

要求：

- Python 3.11 x64
- Nuitka >= 4.0（`python -m pip install nuitka`）
- Visual Studio 2022 Build Tools（MSVC 14.3）或 Nuitka 的 MinGW 回退
- 首次编译较慢；LTO 会进一步增加时间

回退到 PyInstaller：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Backend pyinstaller
```

输出：

```text
dist/好想跑_v<version>/好想跑.exe
dist/haoxiangpao_v<version>_win64.zip
dist/haoxiangpao_v<version>_win64.sha256
dist/haoxiangpao_v<version>_win64_setup.exe
dist/haoxiangpao_v<version>_win64_setup.sha256
dist/latest.json
```

## 安装包：NSIS（v0.1.3 起为发布标准）

**以后所有正式发布都出 NSIS 安装包。** zip 继续保留，但只作为兼容资产，不是主推形态。

选它的理由：

- 安装 / 卸载 / 开始菜单 / 卸载项一次到位，用户不用先想「解压到哪」。
- 升级时能真正删掉旧文件。zip 更新只是覆盖，Nuitka 换模块布局后旧 `.pyd` 会一直留着。
- LZMA 固实压缩，安装包比 zip 小。

### 三条不能破的约束

1. **按用户安装**：`%LOCALAPPDATA%\Programs\好想跑`，`RequestExecutionLevel user`。
   程序把 `data/` 写在 exe 旁边，装进 `Program Files` 会写不进去，登录状态都保存不了。
2. **升级只删程序文件，`data/` 永远不动**。`installer.nsi` 用 `FindFirst` 逐项删除并跳过
   `data`；卸载默认保留，静默卸载也保留。
3. **静默安装 `/S` 必须可用**。应用内更新就是靠它：下载 → 校验 → 等进程退出 → `/S` 安装 → 重启。

### 构建（不需要安装 NSIS）

NSIS 官方提供免安装 zip，用完删掉即可：

```powershell
# 1) https://nsis.sourceforge.io/Download 下载 nsis-3.13.zip
# 2) 解压到 app\build_tools\nsis（或任意目录）
# 3) 正常打包
powershell -ExecutionPolicy Bypass -File packaging\build.ps1

# 也可以显式指定
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Makensis D:\tools\nsis\makensis.exe
```

`build.ps1` 依次在这些位置找 `makensis.exe`：

- `app\build_tools\nsis\makensis.exe`（推荐，随时可删）
- `%ProgramFiles(x86)%\NSIS\makensis.exe`
- `%ProgramFiles%\NSIS\makensis.exe`
- `%LOCALAPPDATA%\Programs\NSIS\makensis.exe`

找不到会**直接构建失败**，不会静默产出一个没有安装包的版本。`-NoInstaller` 只用于本地粗测。

### installer.nsi 的两个硬要求

- **文件必须是 UTF-8 with BOM**。NSIS 只有看到 BOM 才按 UTF-8 解析脚本，否则「好想跑」
  会被当成 ANSI，装出来的路径全是乱码。改完确认文件头是 `EF BB BF`。
- `OutFile` 由 `build.ps1` 先指到 `%TEMP%`，编译完再移回 `dist`。`/D` 只传 ASCII 值，
  避免中文路径在命令行上被转码。

### 资产命名

`haoxiangpao_v<version>_win64_setup.exe`、`haoxiangpao_v<version>_win64.zip`，全 ASCII。
GitHub 会把中文资产名重写成 `_v0.1.2_win64.zip` 这种形式，清单里的 URL 就对不上了。

## 数据目录

- 打包产物**绝不包含** `data/`。
- 同版本重建：先把目标目录的 `data/` 移到备份，替换程序后再放回。
- 新版本：从最新的 `dist/好想跑_v*/data` 迁移，先备份再复制。
- `data/` 里有 token、回执、限额、设备指纹；更新器不得覆盖。
- 安装版升级：`installer.nsi` 逐项删除程序文件、跳过 `data`；卸载默认保留，静默卸载也保留。

## 构建后验证

```powershell
# 1) 冻结导入检查（退出码 0）
dist\好想跑_v0.1.3\好想跑.exe --check

# 2) 无窗口 HTTP 冒烟：登录页应包含 id="loginGate"
dist\好想跑_v0.1.3\好想跑.exe --no-window --port 8765

# 3) 源码泄漏
Get-ChildItem dist\好想跑_v0.1.3 -Recurse -Include *.py,*.pyc |
  Where-Object { $_.FullName -match '\\replica\\' -or $_.Name -eq 'app.py' }

# 4) 哈希
Get-FileHash dist\haoxiangpao_v0.1.3_win64.zip -Algorithm SHA256
Get-FileHash dist\haoxiangpao_v0.1.3_win64_setup.exe -Algorithm SHA256

# 5) 安装包冒烟：静默装一遍，确认 data 不被清空
dist\haoxiangpao_v0.1.3_win64_setup.exe /S
# 再装一次模拟升级，检查 %LOCALAPPDATA%\Programs\好想跑\data 还在
```

## 进一步加固（按性价比排序）

1. **代码签名**：购买 EV/OV 证书，给 exe 和安装器签名，减少 SmartScreen 拦截，
   也让篡改更容易被发现。
2. **HTTPS + SHA-256**：`latest.json` 和 zip 必须走 HTTPS；下载后校验
   `size`/`sha256`，不一致直接丢弃。
3. **服务端判定**：把关键规则、次数、资格、反作弊放服务端；客户端只做展示。
4. **最小化字符串**：错误信息、日志不要打印内部路径、密钥、算法细节。
5. **完整性自检**：启动时校验关键资源哈希，失败则降级/提示；只能提高门槛，
   不能阻止补丁。
6. **混淆/加壳**：PyArmor、VMProtect 等可再提高门槛，但会增加误报、兼容性和
   维护成本；先确保不泄漏密钥，再考虑。
