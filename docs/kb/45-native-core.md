# 45 · Rust 原生核心

> 结论先行：Rust 不是「不可逆向」，它只是把核心常量、签名和信封算法从
> 可读的 Python 字节码里搬到 native 代码，提高静态分析和补丁的成本。
> 真正的边界仍然是服务端校验；客户端加固只提高门槛。

## 1. 边界

| 层 | 语言 | 负责 |
|---|---|---|
| UI / 桥 / 本地文件 / 业务规则 | Python | 页面、登录流程、记录、规则、打包 |
| 协议核心 | Rust | NetSecKit 请求/响应信封、Android/iOS tokenSign、runschool 签名、GeeTest v4 `w` payload、上传体 `UploadFormatEntity`、OBS 轨迹对象、设备指纹生成/校验、跑量配速/10 s 窗口/30 s 分段核心 |

Rust 侧不碰网络、不读 `data/`、不存 token；它只接收最小参数并返回结果。
这样即使 native 层被单独调用，也拿不到完整账号或本地文件。

## 2. 进程协议

打包产物：`runtime/rust/hxp-core.exe`（Nuitka 产物同级的 `runtime/rust/`）。

Python 通过 stdin/stdout 一行一个 JSON 调用：

```json
{"id":1,"op":"build_envelope","args":{"plain":"{}","order":"insert","four":"86123.456789","ts_ms":1700000000000,"platform":2}}
{"id":1,"ok":true,"data":{"json":"{...}","d":"...","h":"...","k":"...","container":"...","req_key":"...","ts_ms":1700000000000,"key_data":["nhang.school","5K0E8400-E29","597DEA1AFB49","86123.456789"]}}
```

可用 op：

| op | 作用 |
|---|---|
| `build_envelope` | 生成 `insert` / `observed` 两种外层顺序的请求信封 |
| `decrypt_response` | 两层响应解密 + RSA 签名校验（fail-closed） |
| `four_from_ms` | `keyDataFour` 的 `%.6f` 尾 12 位 |
| `token_sign` / `token_sign_ios` | 两种平台的 tokenSign |
| `official_sign` / `runschool_sign` | 通用签名 |
| `gt4_payload` / `gt4_w` | GeeTest v4 `w` 明文与 `hex(AES)||hex(RSA)` |
| `gt4_pow_msg` / `gt4_str16` / `gt4_passtime` / `gt4_userresponse` | PoW、AES key、拖动时间、缺口坐标 |
| `upload_entity` | 伪造 run + 提交上下文 → 官方上传体（字段顺序、嵌套 JSON、speed 两次取整） |
| `obs_object` | OBS 轨迹对象（11 个 gzip 字段、27 键点列、30 秒窗口、分段表） |
| `device_validate` / `device_generate` | 设备指纹 schema 校验 / Android/iOS 相干生成 |
| `run_core` | 逐秒配速曲线、10 s 窗口、30 s 分段表；返回采样时刻与累计距离 |
| `self_test` | 上游 golden vectors + 信封/GT4 payload/上传体 round-trip |
| `ping` | 启动探活 |

## 3. 启用与回退

- 默认：找到 `hxp-core.exe` 就用，找不到回退 Python（源码运行不受影响）。
- `JHP_RUST_CORE=0`：强制 Python（排错用）。
- `JHP_RUST_CORE=1`：要求 native，缺失时直接报错；`app.py` 在 frozen 构建里
  默认设置它，发布包不会静默退回 Python。
- `JHP_RUST_CORE_PATH`：指定二进制路径。
- 测试/调试替换了 `netsec.rsa_recover_digest` 时，`decrypt_response` 自动留在
  Python 路径（没有服务器私钥，native 无法复现 mock）。

## 4. 构建

```powershell
cd rust\hxp-core
cargo test
cargo build --release --locked
```

`packaging/build.ps1` 会自动执行 `cargo build --release --locked`，并把
`hxp-core.exe` 复制到 `dist/好想跑_v*/runtime/rust/`。打包机需要 Rust
toolchain（1.80+）；源码运行和纯 Python 测试不需要。

## 5. 文件

| 文件 | 作用 |
|---|---|
| `rust/hxp-core/src/netsec.rs` | fold32 / PAES key / AES-CBC / RSA / 信封 / 解密 |
| `rust/hxp-core/src/gt4.rs` | GeeTest v4 payload / PoW / AES / RSA `w` |
| `rust/hxp-core/src/rundata.rs` | 上传体字段映射 / 提交窗口 id |
| `rust/hxp-core/src/obs.rs` | OBS 轨迹对象 / 分段 / 30 秒窗口 / gzip |
| `rust/hxp-core/src/device.rs` | 设备指纹生成 / 校验 / 机型池 |
| `rust/hxp-core/src/runcore.rs` | 跑量核心：配速曲线 / 窗口 / 分段 |
| `rust/hxp-core/src/rng.rs` | SplitMix64，Python 播种 |
| `rust/hxp-core/src/pyval.rs` | CPython 口径的 int/float/round |
| `rust/hxp-core/src/sign.rs` | tokenSign / official / runschool |
| `rust/hxp-core/src/main.rs` | 行式 JSON sidecar |
| `rust/hxp-core/tests/fixtures/upload_entity.json` | 上传体 golden fixture（Python 与 Rust 共用） |
| `app/replica/transport/rustcore.py` | 进程管理、超时、回退 |
| `app/replica/transport/netsec.py` | 优先 native，失败回退 Python |
| `app/replica/transport/gt4_payload.py` | GT4 payload 优先 native，失败回退 Python |
| `app/replica/domain/rundata.py` | 上传体优先 native；跑量核心优先 native，`_pace_core_py` 保留 Python 参考实现 |
| `app/replica/transport/obs.py` | OBS 对象优先 native，`_build_obs_object` 保留 Python 参考实现 |
| `app/replica/application/device_profile.py` | 生成/校验优先 native，`_validate_profile` 保留 Python 参考实现 |
| `app/replica/domain/crypto.py` / `transport/ios_headers.py` | 签名优先 native |

## 6. 测试

- Rust：`cargo test`（33 个单测：fold32、PAES、信封顺序、签名向量、GT4 golden digest、上传体 fixture、OBS gzip/窗口/分段、设备指纹 schema/机型相干、跑量核心闭合、CPython 取整）
- Python：`tests/test_rust_core.py`；没有二进制时自动 skip
- 现有 `tests/test_netsec.py` / `test_ios_headers.py` / `test_gt4_flow.py` / `test_gt4_split.py` / `test_record_obs_and_rules.py` / `test_device_profile.py` / `test_rundata_*` 在二进制存在时会走 native

## 7. 下一步

按收益排序，后续可迁入 native 的模块：

1. `generate_route` 的几何/海拔/checkpoint 吸附（把 `generate_run_data` 的 RNG 接口继续下沉）
2. `_km_pace_factors` / `_distinct_paces` / `_pause_windows` 等分段决策

跑量核心只迁了逐秒配速曲线、10 s 窗口和 30 s 分段：分段秒数、路线几何、
海拔、retiming、`locPoints`/`fivePoint` 仍在 Python，由 `getrandbits(64)` 播种，
Rust 用自己的 PRNG，不要求与 CPython 的随机流逐位一致。设备指纹只迁了生成和
schema 校验：文件 I/O、legacy 迁移、`audit_profile`/修复和 UI catalog 仍在
Python。GT4 已迁完字段构造、PoW、AES/RSA；`gt4_image.py` 的 Canny/ZNCC 留在
Python，因为调参频繁、需要能直接看中间图。上传体只迁字段映射；OBS 只接管
`wire_points` 路径，旧的 `conv_point` 兜底留在 Python。每迁一块都保留 Python
回退和 golden vector，避免把「防逆向」变成「防自己排错」。

## 8. 设备凭证分层（Phase 2）

一次登录前的派生链，任何一层失败都拒绝登录（仅 frozen；源码环境缺二进制跳过）。
`JHP_SHIELD=0` 与 `JHP_SHIELD_PATH` 只在源码 checkout 生效，frozen 构建忽略
两者——否则一个环境变量或一个 stub 路径就能跳过整条链：

```
Go hxp-guard      op --code 17  -> 32B blob_go   （stdin 读 16B hex nonce；5 goroutine + nonce 置换）
Rust hxp-core     shield_mix    -> 32B blob_rust （FNV-1a + 多项式 + SHA-256）
C++ hxp-shield    op 0x01 seal  -> token + expiry（TTL 3s）
C++ hxp-shield    op 0x02 check -> 1/0            （同一进程）
Python shield.py  仅在内存持有 token 到同进程 check 返回，帧缓冲在 finally 清零
```

| 文件 | 作用 |
|---|---|
| `go/hxp-guard/guard/attest.go` | 5 路并发采集、Fisher-Yates 置换、len\|fragment 哈希、nonce keystream XOR |
| `go/hxp-guard/cmd/hxp-guard/main.go` | 数字 op CLI：`op --code 17`，nonce 从 stdin 读一行 hex，只输出 `ok`/`blob` |
| `rust/hxp-core/src/shield.rs` | `pub fn mix(blob, nonce) -> [u8; 32]` |
| `rust/hxp-core/src/main.rs` | 行 JSON op `shield_mix`，blob/nonce 只收 hex，要求 32B/16B |
| `cpp/hxp-shield/main.cpp` | `seal` / `check` / `ping` / `selftest`，进程内 token ring |
| `app/replica/application/shield.py` | 找二进制、组帧、四层编排、统一拒绝文案、帧缓冲清零 |
| `app/replica/application/account_guard.py` | `attest(nonce, store_dir)` 通过 stdin 调用 Go op |
| `app/tests/test_shield.py` | 帧格式、source skip、frozen 缺层、check=0、真实假子进程帧/超时、成功 |
| `app/tests/test_shield_gate.py` | 三个登录入口与保活的 403 门禁 |

### opcode / 接口

| 层 | 接口 | 输入 | 输出 |
|---|---|---|---|
| Go | `op --code 17` | stdin 一行 16 B nonce（hex） | `{"ok":true,"blob":"<64 hex>"}` |
| Rust | `{"op":"shield_mix","args":{"blob":"<hex>","nonce":"<hex>"}}` | 32 B + 16 B | 32 B hex 字符串 |
| C++ | `0x01 seal` | 16 + 32 + 32 B | 32 B token + 8 B expiry_ms |
| C++ | `0x02 check` | 32 B token + 8 B expiry_ms | `0x01` 有效 / `0x00` 无效 |
| C++ | `0x03 ping` / `0x7f selftest` | 空 | `0x01` |

帧头固定 `<IHHI`：magic `0x53505848`、version `1`、op、len（≤ 4096）。
Python 严格校验 magic/version/op/len；任何帧错、超时、token 过期或 `check`
返回 0 都折叠成同一条用户文案，不暴露层名、路径或原因。

### 约束

- `seal` 与 `check` 必须是**同一个进程实例**：token 只在本进程内存 ring 里，
  换进程一定 `check = 0`。`seal` 的响应会回到 Python，但只用于同一进程上的
  立即 `check`；帧缓冲在 `finally` 清零。
- token / nonce / blob 不写 `data/`、不进日志、不进诊断包、不进异常文本。
  nonce 通过 Go 的 stdin 传入，不出现在 argv。
- Go attest 的 `install_id` 采集是只读的：文件存在才读，否则退回
  `g.DeviceID`；MachineGuid 可用时登录不会写 `data/install_id`。只有
  `guard.New` 在完全拿不到系统机器 ID 时才会创建回退文件。
- Python 帧 I/O 超时 0.5 s × 4 次读 = 最多 2 s，小于 C++ 的 3 s TTL；
  子进程回收预算 0.75 s。
- frozen 构建缺任一层直接拒绝登录；源码 checkout 缺二进制跳过，保证
  `pytest` 不依赖 native 产物。

## 9. 原生完整性清单（Phase 3）

打包产物在 `runtime/` 下多一个 `integrity.json`：

```json
{"version":2,"files":{"hxp-core.exe":"<sha256>","hxp-guard.exe":"<sha256>","hxp-shield.exe":"<sha256>"},"sig":"<ed25519 hex>"}
```

| 项 | 位置 / 行为 |
|---|---|
| 生成 | `packaging/build.ps1` 在复制三层 native 之后计算三个 exe 的 SHA-256，写 `version=2` 的 manifest，再调 `scripts/manifest_sign.py sign` 追加 `sig`，最后用编译进 `integrity.py` 的公钥 `verify`；任一步失败直接 `throw` |
| 签名 | Ed25519 over `{"version","files"}` 的 canonical JSON（sorted keys、紧凑分隔符），不覆盖 `sig` 本身；私钥在 `JHP_MANIFEST_KEY` 或 `%USERPROFILE%\.hxp\manifest.key`，仓库里只有公钥常量 |
| 读取 | `app/replica/application/integrity.py` 的 `check_native()`；frozen 下 manifest 路径为 `<app_root>/runtime/integrity.json` |
| 校验对象 | `runtime/rust/hxp-core.exe`、`runtime/go/hxp-guard.exe`、`runtime/cpp/hxp-shield.exe` |
| 返回 | `{'ok', 'checked', 'missing', 'mismatch'}`；manifest 缺失/格式错/版本不符为 `reason='manifest'`，签名错误为 `reason='signature'`；源码 checkout 为 `skipped` |
| 暴露方式 | bridge `real_integrity_status()`，以及诊断包里的 `integrity.json`；前端目前没有入口 |
| 边界 | `mismatch`、`reason='manifest'`、`reason='signature'` 都拦登录（`shield.require` 在派生前调用 `integrity.tamper_message()`，文案「安装文件校验未通过，请重新安装软件后再试」）；`missing` 不拦。不删文件、不读 `data/`、不联网、不自动修复 |
| 自检 | `hxp-shield.exe --selfcheck` 走独立进程，检查调试器与自身 PE（入口节可执行且不可写、section table 与 `SizeOfImage` 一致、入口页不是 RWX）；`shield.selfcheck()` 返回 `{'ok', 'skipped'}` 或 `{'ok': False, 'reason': 'debugger'|'image'|'unavailable'}`，只进诊断，不拦登录 |

`runtime/cpp/` 与 `runtime/rust/`、`runtime/go/` 一样只放一个 exe：

```
runtime/
  integrity.json
  rust/hxp-core.exe
  go/hxp-guard.exe
  cpp/hxp-shield.exe
```

`hxp-shield.exe` 仍然是独立进程（二进制帧、无导出符号），不是 DLL/ctypes
模块；`runtime/cpp/` 只决定打包位置，不改变 Phase 1/2 的进程边界。完整性
只拦“签名/manifest 不对”和“文件在但字节变了”，蜜罐只做观察；两者都只是
减速带，最终信任边界仍在服务端。

同阶段的被动蜜罐 `app/replica/application/license_decoy.py` 不参与上述
链路：它只在 bridge 被显式调用时返回 3 秒的假 `pro` 状态，随后 `expired`，
不阻塞任何正常功能。
