# 客户端加固：四语言边界与分布式校验

> 状态：Phase 1 / Phase 2 / Phase 3 / Phase 4 已实现（2026-11-12）。实现由
> Claude Code 按阶段提交，本文是验收口径。
> 结论先行：客户端加固只提高逆向成本，最终边界仍是服务端。不要把“不可逆向”
> 写进需求，也不要用它替代服务端风控。

## 1. 目标

在 Python + Rust + Go + C++ 四语言并存的离线桌面程序里，做到：

1. **没有清晰的 FFI 导出面**：不出现 `CheckLicense()`、`VerifyKey()`、
   `DecryptPayload()` 这类可读符号，也不把业务函数以 `extern "C"` 暴露给
   Python/Go。
2. **跨语言只传不透明字节**：帧 = `magic | version | opcode | length | ciphertext`，
   `opcode` 是数字；`ciphertext` 由会话密钥加密并带 HMAC。禁止 JSON 键名、
   明文字段名、可读参数结构出现在敏感边界。
3. **逻辑碎片化**：一次校验/派生必须经过至少三层不同语言，单层被补丁后
   结果仍然错误。每层只持有片段，不持有完整密钥或完整状态。
4. **短时凭证**：Python 最终拿到的凭证只在数秒内有效，过期即失效；不落盘、
   不进日志、不进诊断包。
5. **蜜罐**：外层保留一段“看起来像授权校验”的诱饵逻辑；被patch后不报错，
   而是进入假成功/延迟失败路径。蜜罐不能影响正常用户。

## 2. 现状与边界

| 语言 | 角色 | 现有边界 | 备注 |
|---|---|---|---|
| Python | UI 桥、编排、文件 I/O、DPAPI | 无自有 FFI | Nuitka 编译为 C；常量仍可能泄漏 |
| Rust | 协议原语（信封/签名/上传体/OBS） | 独立进程 + 行 JSON | `app/replica/transport/rustcore.py` |
| Go | 本地策略（一号一设备） | 独立进程 + CLI JSON | `go/hxp-guard` |
| C++ | 待新增：状态机/完整性/算法 | 无 | 目标：独立进程，二进制帧 |

**不要新增 DLL 形态的 C++ 模块。** 进程隔离优先；如果性能证明必须同进程，
只允许一个按 ordinal 导入的入口，且入参/返回值只能是密文字节。

## 3. 目标架构

```
JS 前端（公开）
   │ localhost HTTP
Python / Nuitka（编排，不持有完整密钥）
   │ 二进制帧（加密+HMAC）
C++ hxp-shield（平坦化状态机、VEH 异常推进、短时 token）
   │ 二进制帧
Go hxp-guard（5 路并发采集 + 切片置换 + 片段）
   │ 二进制帧
Rust hxp-core（位运算/多项式混合，不落盘、不联网）
```

一次“设备凭证”派生：

1. Go 用 5 个 goroutine 并发采集机器指纹片段，按 nonce 做 Fisher-Yates
   置换，拼成 `blob_go`。
2. Rust 对 `blob_go` 做高维位运算/多项式混合，返回 `blob_rust`。
3. C++ 用 VEH 推进的平坦化状态机把两个片段揉成 32 字节 token，
   `expiry = now + 3000ms`。
4. Python 只在内存里持有 `token + expiry`，用于同一个 `Popen` 上的立即
   `check`；check 返回后帧缓冲和 token 视图清零，不缓存到磁盘，过期或
   校验失败就重新派生。

任何一层缺失：开发环境回退到“跳过加固”（`JHP_SHIELD=0` 或二进制不存在）；
冻结构建必须置 `JHP_SHIELD=1`，缺层直接拒绝登录/提交，错误文案保持笼统。

## 4. 分阶段交付

### Phase 0 · 先拔 Python 明文常量（最高优先级）

- `app/replica/domain/crypto.py` 的 `RUNSCHOOL_SECRET`、`TOKEN_SIGN_SALT`、
  `RUNEC_KEY`、`APP_ID`、上传 URL 不得进入发布编译单元。
- 回退实现移到 dev-only 模块，Nuitka 用 `--nofollow-import-to` 排除；
  frozen 下缺少 native 直接报错，不静默回退。
- `app/packaging/build.ps1` 增加构建门禁：发布 exe 中命中上述字符串即失败。
- 验收：对 `dist/好想跑_v*/好想跑.exe` 做字节扫描，0 命中；源码测试仍通过。

### Phase 1 · C++ `hxp-shield`（已实现）

- 新增 `cpp/hxp-shield`，产出 `hxp-shield.exe`，**不导出任何符号**。
- 只认数字 opcode：`0x01 seal`、`0x02 check`、`0x03 ping`、`0x7f selftest`。
- 帧格式固定、长度前缀、最大 4 KiB；非法帧直接退出，不打印内部路径。
- 令牌 = HMAC-SHA256(机器密钥, nonce|blob_go|blob_rust|expiry)，TTL 3s。
- 状态机用表驱动 + VEH `RaiseException` 推进；加入不透明谓词，禁止
  编译器把状态机折叠回直筒代码。
- 构建：`cpp/hxp-shield/build.ps1` 调用 vcvars64，`/O2 /GL /GR- /Gy`，
  不生成 PDB，链接 `/RELEASE`。
- 验收：`hxp-shield.exe --selftest` 退出 0；篡改 token 后 check 返回 0；
  用 `dumpbin /exports` 确认无导出。

### Phase 2 · Go/Rust 片段 + Python 编排（已实现）

- Go `hxp-guard` 增加数字 op 的二进制帧模式；`attest` 用 5 个 goroutine
  采集片段并按 nonce 置换。CLI 接口为
  `hxp-guard op --state <file> --code 17`，**nonce 从 stdin 读一行 hex**，
  不放进 argv（Windows 会向同用户进程公开命令行）。`install_id` 只读，
  MachineGuid 可用时 attest 不写 `data/`。
- Rust `hxp-core` 增加 `shield_mix`；只接收 hex 片段，返回 hex 片段，
  并要求 32 B blob + 16 B nonce。
- Python 新增 `app/replica/application/shield.py`：找二进制、组帧、编排四层、
  即时 `seal → check`。C++ 的 `seal` 响应会回到 Python，但只用于同一个
  `Popen` 上的立即 `check`；帧缓冲与 token 视图在 `finally` 清零，不缓存、
  不落盘、不进日志、不进诊断包。
- **`seal` 与 `check` 必须由同一个 `hxp-shield.exe` 进程完成**。`check` 只
  接受本进程实例签发的 token；跨进程拿 token 去校验一定失败。Python 因此
  在一次 `Popen` 里先发 `seal`，立刻发 `check`，通过后马上关 stdin 并销毁
  token。
- 登录入口（`real_login`、`real_login_ticket`、`real_gt4_manual_finish`）
  在设备自检之后、网络登录之前取凭证；frozen 下失败即拒绝，文案统一为
  “设备校验未通过，请重启软件后再试”。`tokenkeeper.relogin_real` 与
  `cli.do_login` 走同一道门。
- 验收：源码环境无二进制可正常登录；放齐三层后能派生并校验；伪 token 被拒；
  `pytest` 全绿。

### Phase 3 · 蜜罐与完整性（已实现）

- `app/replica/application/license_decoy.py`：变量名不混淆的假鉴权，
  硬编码 `LICENSE_SECRET = 'HXP-PRO-2026'`；`activate_license()` 只做字符串
  比较，命中后返回假的 `{'ok': True, 'plan': 'pro', 'expires_in': 3}`，
  3 秒内 `license_status()` 报 `active`，之后报 `expired`。这是**被动诱饵**，
  不是真实授权：不联网、不写文件、不记录 key、不导入登录/提交模块。
  正常 UI 没有任何入口；只有反向工程者显式调用 bridge 的
  `real_decoy_activate` / `real_decoy_status` 才会碰到。没有死循环、删除
  文件或弹窗行为，3 秒后停在 `expired`。
- `app/replica/application/integrity.py`：frozen 构建下读取
  `<app_root>/runtime/integrity.json`，对 `runtime/rust/hxp-core.exe`、
  `runtime/go/hxp-guard.exe`、`runtime/cpp/hxp-shield.exe` 计算 SHA-256，
  返回 `{'ok', 'checked', 'missing', 'mismatch'}`。manifest 缺失或格式错
  返回 `ok=False, reason='manifest'`。源码 checkout 没有 manifest，返回
  `skipped`。
- `packaging/build.ps1` 在复制三层 native 之后计算三个 exe 的 SHA-256，
  以 UTF-8 无 BOM 写入 `runtime/integrity.json`；写失败直接 `throw`。
- **mismatch 拦登录，其余只提示**：`shield.require()` 在任何一层派生之前
  调用 `integrity.mismatch_message()`。只有“文件存在但哈希不同”会返回
  `REPAIR_MESSAGE`（「安装文件校验未通过，请重新安装软件后再试」）并拒绝
  登录；`missing`、manifest 缺失/格式错、源码 checkout 都不拦。杀毒隔离
  和半拷贝是常态，不能把可恢复的安装问题变成硬锁死；缺层本身仍由 Phase 2
  的 frozen 规则处理。
- `cpp/hxp-shield --selfcheck`：两条保守检查——调试器
  （`IsDebuggerPresent` / `CheckRemoteDebuggerPresent`）和自身 PE（入口点
  所在节可执行且不可写、section table 与 `SizeOfImage` 一致、入口页不是
  `PAGE_EXECUTE_READWRITE`）。退出码 0/1，stderr 只有
  `selfcheck: debugger|image`，不打印路径、模块名或地址。结果由
  `shield.selfcheck()` 拼进 `real_integrity_status`，并作为
  `integrity.json` 进入诊断包；**永不阻塞登录**——开发机常挂调试器，部分
  终端安全软件也会以调试方式探测进程。
- 验收：正常安装与 `pytest` 都不触发蜜罐；只有显式调用 bridge 才会看到
  “假成功 → 3 秒过期”；哈希不符时登录被拒并给出重装提示，删除文件时诊断
  标红、frozen 下由缺层规则拒绝。任何分支都不删除文件、不动 `data/`。

### Phase 4 · 前端 A+B（已实现）

- **A · 决策下沉**：`real_submit_run` 的失败 envelope 带
  `data.failure_kind`：
  - `fail`：请求没有发出，或服务端明确拒绝；修正后可以重试；
  - `unknown`：请求可能已经到达服务端（或已预留回执）；提交按钮保持锁定，
    先去「记录」页只读核对。
  分类由 `bridge_api._fail_submit` 在产生失败的分支决定，不再依赖 HTTP
  状态码（bridge 失败全是 500）或中文消息前缀。前端 `submitFailureKind`
  优先读该字段，只有旧版 sidecar 缺少它时才回退到前缀规则。未预期的
  `real_submit_run` 异常由 `http_server` 标成 `unknown`，不会误开重试。
- **B · 构建期压缩**：`scripts/minify_frontend.py` 只处理打包 staged 副本的
  `frontend/js/*.js`，调用 `npx --yes terser@5.36.0`，启用 `--compress
  passes=2` 与 `--mangle`，**不生成 source map**，也**不加 `--toplevel`**，
  因此 `Announcements`、`HXPCharts` 等跨文件全局名仍然存在。`vendor/` 与
  `theme-boot.js` 不动；每个输出先过 `node --check`，全部通过才原子替换。
  - 接入 `packaging/build.ps1` 的 `1b/5`，位置在 Frozen HTTP smoke test
    之前：压缩后的登录页必须还能被真实进程加载，否则构建直接失败。
  - 源码树保持不变；开发时仍然是“改完刷新”，不需要 `npm install`。
- **边界**：A+B 只降低“顺手读前端就能绕过 UI”的收益，不是安全边界。
  真正的校验仍在 Python / 服务端；不要用 TypeScript、WASM 或 JS 加密壳
  替代后端判定。

## 5. 明确不做

- 不做全 exe 的 VMProtect/Themida 加壳（误报、兼容性、维护成本）。
- 不把 token/密码/DPAPI 明文交给 native。
- 不用客户端校验替代服务端判定。
- 不做“检测到调试器就删除文件/死循环”这类破坏性行为。
- 不做自毁：自检结果只影响登录这一特权动作和诊断信息，不删除程序、
  `data/`、记录或路线库。能改二进制的人会先复制一份目录再 NOP 掉检查，
  自毁只会伤到误报的正常用户。
- 不用调试器检测当登录门：开发机、部分终端安全软件和崩溃分析都会让
  `IsDebuggerPresent` 为真，拦它等于随机锁死用户。
- 不把 C++ 做成 `ctypes` DLL；如果将来必须，只允许单 ordinal 入口。

## 6. 验收命令

```powershell
# C++
cpp\hxp-shield\build.ps1
cpp\hxp-shield\hxp-shield.exe --selftest
cpp\hxp-shield\hxp-shield.exe --frame-test
cpp\hxp-shield\hxp-shield.exe --selfcheck

# Go
cd go\hxp-guard; go test ./...; go build -trimpath -ldflags="-s -w" -o hxp-guard.exe ./cmd/hxp-guard

# Rust
cd rust\hxp-core; cargo test; cargo build --release --locked

# Python
cd app; python -m pytest tests -q
python -m pytest tests/test_architecture_imports.py tests/test_native_boundaries.py -q
node --check frontend/js/app.js
python app.py --check

# 前端打包压缩（对 staged 副本执行；源码树不变）
python scripts\minify_frontend.py app\build_stage\好想跑\frontend

# 发布包字节扫描
python scripts\scan_release_secrets.py dist\好想跑_v0.2.2\好想跑.exe
```

## 7. 审查要求

- 每阶段先由 Claude Code 实现，再由 Claude Code 只读审查；审查必须给
  `file:line`、复现命令、实际输出。
- 任何“为了让测试过而放宽校验”的改动直接 BLOCK。
- 任何在日志/诊断包/异常信息里出现 token、nonce、密钥、完整 DeviceId 的
  改动直接 BLOCK。

## 8. 已知限制：排错旁路

- `JHP_SHIELD=0` / `off` / `false` / `no` 会直接跳过四层派生；
  `JHP_SHIELD_PATH` 可以把 shield 指向任意可执行文件。两者在 frozen 构建里
  **依然生效**，这是排错用的旁路，不是安全边界。
- `JHP_SHIELD=1` 目前不是“强制”开关：代码只把 `0/off/false/no` 当作关闭。
  发布包靠“三层二进制齐全”来保证执行，而不是靠这个变量。
- 本地用户可以删除 `data/account_binding.json`、替换 native 产物或调试本机
  进程。客户端加固只提高逆向与改包成本，最终判定仍在服务端。
