# 客户端加固：四语言边界与分布式校验

> 状态：设计稿（2026-11-12）。实现由 Claude Code 按阶段提交，本文是验收口径。
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
4. Python 只在内存里持有 `token + expiry`，登录/提交前现取现用；
   过期或校验失败就重新派生，不缓存到磁盘。

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

### Phase 1 · C++ `hxp-shield`

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

### Phase 2 · Go/Rust 片段 + Python 编排

- Go `hxp-guard` 增加数字 op 的二进制帧模式；`attest` 用 5 个 goroutine
  采集片段并按 nonce 置换。
- Rust `hxp-core` 增加 `shield_mix`；只接收 hex 片段，返回 hex 片段。
- Python 新增 `app/replica/application/shield.py`：找二进制、组帧、加密、
  3 秒缓存；不写日志、不进诊断包。
- 登录入口（`real_login`、`real_login_ticket`、`real_gt4_manual_finish`）
  在设备自检之后、网络登录之前取凭证；frozen 下失败即拒绝，文案统一为
  “设备校验未通过，请重启软件后再试”。
- 验收：源码环境无二进制可正常登录；放齐三层后能派生并校验；伪 token 被拒；
  `pytest` 全绿。

### Phase 3 · 蜜罐与完整性

- `app/replica/application/license_decoy.py`：变量名不混淆的假鉴权，
  硬编码 key；被 patch/缺失时进入“假成功 → 延迟失败”，不阻塞正常用户。
- 启动时校验 `hxp-core.exe`、`hxp-guard.exe`、`hxp-shield.exe` 的哈希；
  失败只降级提示，不直接崩溃。
- 蜜罐不能死循环、不能破坏数据、不能影响正常登录；只针对被篡改的安装。
- 验收：正常安装不触发；手动改 decoy 后触发一次假成功并给出笼统错误；
  不影响 `pytest`。

## 5. 明确不做

- 不做全 exe 的 VMProtect/Themida 加壳（误报、兼容性、维护成本）。
- 不把 token/密码/DPAPI 明文交给 native。
- 不用客户端校验替代服务端判定。
- 不做“检测到调试器就删除文件/死循环”这类破坏性行为。
- 不把 C++ 做成 `ctypes` DLL；如果将来必须，只允许单 ordinal 入口。

## 6. 验收命令

```powershell
# C++
cpp\hxp-shield\build.ps1
cpp\hxp-shield\hxp-shield.exe --selftest

# Go
cd go\hxp-guard; go test ./...; go build -trimpath -ldflags="-s -w" -o hxp-guard.exe ./cmd/hxp-guard

# Rust
cd rust\hxp-core; cargo test; cargo build --release --locked

# Python
cd app; python -m pytest tests -q
node --check frontend/js/app.js
python app.py --check

# 发布包字节扫描
python scripts\scan_release_secrets.py dist\好想跑_v0.2.2\好想跑.exe
```

## 7. 审查要求

- 每阶段先由 Claude Code 实现，再由 Claude Code 只读审查；审查必须给
  `file:line`、复现命令、实际输出。
- 任何“为了让测试过而放宽校验”的改动直接 BLOCK。
- 任何在日志/诊断包/异常信息里出现 token、nonce、密钥、完整 DeviceId 的
  改动直接 BLOCK。
