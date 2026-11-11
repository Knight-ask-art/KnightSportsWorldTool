# 46 · 一号一设备绑定

> 结论先行：这是一个**本地防切换**规则，不是强 DRM。设备绑定文件可以删，
> 真正的账号风控仍应在服务端。它的价值是让“退出再登别人号”这件事变得麻烦，
> 并给用户一个明确解释。

## 1. 规则

- 第一个登录成功的账号拥有这台机器；
- 退出登录**不会**清除绑定；
- 同一台机器再登另一个账号 → 拒绝，提示
  `为防止恶意使用软件，软件请一号一设备绑定`；
- 同一账号可重复登录，换设备指纹也不影响（绑定的是机器，不是假指纹）。

## 2. 实现

| 层 | 位置 | 作用 |
|---|---|---|
| 策略引擎 | `go/hxp-guard` | 读写绑定、机器 ID、HMAC、CLI/JSON |
| Python 适配 | `app/replica/application/account_guard.py` | 找二进制、调用、失败策略 |
| 登录入口 | `app/replica/adapters/bridge_api.py` | `real_login` / `real_login_ticket` / `real_gt4_manual_finish` 登录前 `check` |
| 成功写入 | `bridge_api._persist_login` | 服务端登录成功后 `bind` |
| 自动重登 | `app/replica/application/tokenkeeper.py` | 重登前 `check`、成功后 `bind` |
| CLI | `app/replica/adapters/cli.py` | `do_login` 同样受约束 |

`check` 只读；`bind` 是真正的执行点，即使两个登录并发，第二个账号也拿不到
可用会话。

## 3. 数据

`data/account_binding.json`：

```json
{
  "version": 1,
  "device": "<机器 ID 的 SHA-256 前 128 位>",
  "account": "<账号规范化后的 SHA-256>",
  "hint": "138****1234",
  "bound_at": 1794420027,
  "mac": "<HMAC>"
}
```

- 原始手机号不落盘，只存哈希和脱敏提示；
- `mac` 用机器密钥（Windows `MachineGuid`，其它平台 `/etc/machine-id`）
  做 HMAC；同机改 JSON 会被识别为 `tampered`，直接拒绝；
- 没有系统机器 ID 时，在状态文件旁生成 `install_id` 作为回退密钥；
- 整个 `data/` 拷到另一台机器：设备 ID 不同，视为未绑定，允许重新绑定。

## 4. 失败策略

| 情况 | 行为 |
|---|---|
| 二进制不存在（源码运行） | 放行，`guard=off` |
| `JHP_ACCOUNT_GUARD=0` | 放行，排错用 |
| 同机不同账号 | 拒绝，403 + 固定文案 |
| 绑定文件损坏 / MAC 不符 | 拒绝，提示联系维护者 |
| 二进制存在但崩溃/超时 | 拒绝（fail closed） |
| 换机器 / 换安装 ID | 视为未绑定，允许重新绑定 |

支持重置：

```powershell
go\hxp-guard\hxp-guard.exe reset --state data\account_binding.json --yes
```

只能在用户明确要求解绑时使用；UI 不提供入口。

## 5. 边界

- 本地规则可被删除 `data/account_binding.json` 绕过；不要把它写成“无法破解”。
- 不采集硬件序列号、不联网、不上传设备信息；只在本机比较哈希。
- 诊断包不包含绑定文件（诊断包是白名单收集）。
- 如果将来服务端下发绑定关系，以服务端为准，本地只做缓存和提示。

## 6. 测试

- Go：`cd go/hxp-guard; go test ./...`（首次、重复、换账号、篡改、换机器、掩码）。
- Python：`tests/test_frontend_login.py`、`tests/test_device_profile.py`、
  `tests/test_gt4_flow.py` 覆盖登录入口与滑块路径。
- 手工：建临时目录，`bind` A 后 `check` B，退出码 3 且输出固定文案。
