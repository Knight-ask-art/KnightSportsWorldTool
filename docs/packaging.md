# 打包与源码保护

> 结论先行：Nuitka 能显著提高反编译成本，但**不能保证绝对不可逆**。
> 真正的边界是服务端校验和不下发敏感密钥；客户端加固只是提高门槛。

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
dist/好想跑_v<version>_win64.zip
dist/好想跑_v<version>_win64.sha256
dist/latest.json
```

## 数据目录

- 打包产物**绝不包含** `data/`。
- 同版本重建：先把目标目录的 `data/` 移到备份，替换程序后再放回。
- 新版本：从最新的 `dist/好想跑_v*/data` 迁移，先备份再复制。
- `data/` 里有 token、回执、限额、设备指纹；更新器不得覆盖。

## 构建后验证

```powershell
# 1) 冻结导入检查（退出码 0）
dist\好想跑_v0.1.2\好想跑.exe --check

# 2) 无窗口 HTTP 冒烟：登录页应包含 id="loginGate"
dist\好想跑_v0.1.2\好想跑.exe --no-window --port 8765

# 3) 源码泄漏
Get-ChildItem dist\好想跑_v0.1.2 -Recurse -Include *.py,*.pyc |
  Where-Object { $_.FullName -match '\\replica\\' -or $_.Name -eq 'app.py' }

# 4) 哈希
Get-FileHash dist\好想跑_v0.1.2_win64.zip -Algorithm SHA256
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
