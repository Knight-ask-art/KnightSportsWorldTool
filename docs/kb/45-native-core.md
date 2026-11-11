# 45 · Rust 原生核心

> 结论先行：Rust 不是「不可逆向」，它只是把核心常量、签名和信封算法从
> 可读的 Python 字节码里搬到 native 代码，提高静态分析和补丁的成本。
> 真正的边界仍然是服务端校验；客户端加固只提高门槛。

## 1. 边界

| 层 | 语言 | 负责 |
|---|---|---|
| UI / 桥 / 本地文件 / 业务规则 | Python | 页面、登录流程、记录、规则、打包 |
| 协议核心 | Rust | NetSecKit 请求/响应信封、Android/iOS tokenSign、runschool 签名、GeeTest v4 `w` payload、上传体 `UploadFormatEntity`、OBS 轨迹对象、设备指纹生成/校验 |

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
| `rust/hxp-core/src/pyval.rs` | CPython 口径的 int/float/round |
| `rust/hxp-core/src/sign.rs` | tokenSign / official / runschool |
| `rust/hxp-core/src/main.rs` | 行式 JSON sidecar |
| `rust/hxp-core/tests/fixtures/upload_entity.json` | 上传体 golden fixture（Python 与 Rust 共用） |
| `app/replica/transport/rustcore.py` | 进程管理、超时、回退 |
| `app/replica/transport/netsec.py` | 优先 native，失败回退 Python |
| `app/replica/transport/gt4_payload.py` | GT4 payload 优先 native，失败回退 Python |
| `app/replica/domain/rundata.py` | 上传体优先 native，`_build_upload_entity` 保留 Python 参考实现 |
| `app/replica/transport/obs.py` | OBS 对象优先 native，`_build_obs_object` 保留 Python 参考实现 |
| `app/replica/application/device_profile.py` | 生成/校验优先 native，`_validate_profile` 保留 Python 参考实现 |
| `app/replica/domain/crypto.py` / `transport/ios_headers.py` | 签名优先 native |

## 6. 测试

- Rust：`cargo test`（27 个单测：fold32、PAES、信封顺序、签名向量、GT4 golden digest、上传体 fixture、OBS gzip/窗口/分段、设备指纹 schema/机型相干、CPython 取整）
- Python：`tests/test_rust_core.py`；没有二进制时自动 skip
- 现有 `tests/test_netsec.py` / `test_ios_headers.py` / `test_gt4_flow.py` / `test_gt4_split.py` / `test_record_obs_and_rules.py` / `test_device_profile.py` 在二进制存在时会走 native

## 7. 下一步

按收益排序，后续可迁入 native 的模块：

1. `domain/rundata.py` 的生成器（配速/暂停/步频/GPS 抖动；随机过程需要先固定 RNG 语义）

设备指纹只迁了生成和 schema 校验：文件 I/O、legacy 迁移、`audit_profile`/修复
和 UI catalog 仍在 Python。GT4 已迁完字段构造、PoW、AES/RSA；`gt4_image.py`
的 Canny/ZNCC 留在 Python，因为调参频繁、需要能直接看中间图。上传体只迁字段
映射，轨迹生成仍在 Python；OBS 只接管 `wire_points` 路径，旧的 `conv_point`
兜底留在 Python。每迁一块都保留 Python 回退和 golden vector，避免把「防逆向」
变成「防自己排错」。
