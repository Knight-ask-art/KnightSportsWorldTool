# iOS 设备指纹：研究记录与接入状态

> 状态：**iOS 全链已真机验证（2026-10-25，rrid `1344757460`）。**
> 当前 App Store 版本为 **7.4.0（2026-10-26）**；文中 7.3.40 是验证时的历史版本，
> 线上 `appVersion`/UA 已跟随到 7.4.0。
> 分支：`experiment/ios-device-profile`（历史原型，已合入主线）
> 结论先行：**iOS 提交链可用**——登录、策略、点位、提交、OBS 全部走 iOS 头，
> 服务端返回 `status=0` / `isCheat=0` / `policy=0` / `stepFreqStatus=0`。
> 参考实现（`YanamiNeko/NekoSportsWorldTool`）在提交时**强制改回 Android 头**，
> 其衍生分支 `czcdr/NekoSportsWorldTool-antiban` 的 `review/ANTI-FLAG-AUDIT.md`
> 把这一行为列为 A1（high）：同一条 token 上登录是 iOS、提交是 Android，
> 真机不可能中途换平台。我们选择整条链同平台，并把这条选择做了实测。
> iOS 仍只是可选选项（2026-11-12 起默认 Android；`JHP_IOS_CHANCE` 默认 0），因为「换平台」本身是信号，
> 不是因为没有证据。

---

## 1. 为什么会有这个分支

用户提出“如果能上苹果设备是最稳的”。为了回答这个问题，查了上游和多个分支的代码：

- `YanamiNeko/NekoSportsWorldTool`
  - `src/crypto/header.rs`
  - `src/api/login.rs`
  - `src/api/client.rs`
  - `src/api/submit.rs`
  - `src/ui/device.rs`
- `huang3337/NekoSportsWorldTool-Plus`
- `ooo12061018/NekoSportsWorldTool`
- `fhvbd/NekoSportsWorldTool-SimLocation`
- `Funrestan/FunSportWorld`（Python 实现）
- `react-native-device-info` 的 `getInstanceId`

---

## 2. 已知事实（有出处）

### 2.1 iOS header 的形状

上游 `src/crypto/header.rs` 的 iOS 头是：

```text
osType=1
DeviceId=<UUID 大写>
deviceName=iPhone
CustomDeviceId=<DeviceId>_iOS_sportsWorld_campus
osVersion=18.x / 26.x
[IDFA]
logicPixel=360x640
physicPixel=1080x1920
cpuModel=x86_64
appVersion=7.4.0
isRoot=false
appInstallTime=<持久化>
nonce
[uid]
[token]
timeStamp
studentId
[tokenSign]
```

没有 `appUpdateTime`，也没有 Android 那组 `IMEI / androidId / wifiMac / blMac`。

### 2.2 登录 body 仍然是 Android 形态

上游 `src/api/login.rs` 的登录 body 固定：

```json
{
  "device_model": "<identity.device_name>",
  "os_version": "<identity.os_version>",
  "mac_address": "<identity.mac_address>",
  "imei": "",
  "loginType": 0,
  "username": "...",
  "password": "...",
  "uuid": "...",
  "osType": "0"
}
```

也就是说：**iOS 只换请求头，登录 body 的 `osType` 仍然是 `"0"`。**

### 2.3 提交端固定 Android

这是最关键的一条。上游 `src/api/submit.rs` 的 `send_record()` 里：

```rust
let android_identity = HeaderIdentity {
    platform: "android".into(),
    device_id: device_id.clone(),
    os_version: "14".into(),
    device_name: "22081212C".into(),
    ..client.identity.clone()
};
let (hp, hp_extra) =
    build_android_header(&android_identity, p.uid, &client_token(client), None);
...
req = req
    .set("User-Agent", UA_ANDROID)
    .set("appVersion", "7.3.40")
    .set("headerSign", &header_env.json)
    .set("runes", &runes)
    .set("runef", &runef);
```

即使登录 identity 是 iOS，提交仍然：

- 用 `build_android_header`；
- `os_version=14`；
- `device_name=22081212C`；
- `User-Agent=UA_ANDROID`；
- 带 `runes/runef`。

所以上游的 iOS 只是“登录/读取用 iOS 头”，并不是“登录和提交同一台 iPhone”。这正好和我们的目标相反。

### 2.4 Python 实现也是 Android

`Funrestan/FunSportWorld` 的 `funsport/crypto/header.py` 只有 Android 头；`submit` 也走 Android。没有 iOS 提交的现成实现。

### 2.5 设备身份要持久化

上游 `src/ui/device.rs` 和 `FunSportWorld/funsport/config.py` 都是：

- 首次生成一次；
- 写 `identity.json`；
- 之后复用；
- 明确警告不要频繁删/换。

`react-native-device-info` 的 `getInstanceId` 也是“随机 UUID 存 SharedPreferences，按安装稳定”。

---

## 3. 未验证项（2026-10-29 更新）

1. ✅ `save/record` 接受 iOS 头（`osType=1`、`appVersion=7.3.40`、无 `appUpdateTime`、
   `CustomDeviceId` 带 `_iOS_sportsWorld_campus`）——rrid `1344757460` 已入账。
2. ✅ iOS 提交带 `runes/runef` 被接受；没有出现「非法 runes」类错误。
3. ❓ 服务端是否把 token 绑定到平台/设备：已有 iOS 会话可用，但**没有**做「同一账号
   Android→iOS 切换」的实验；按设备指纹纪律也不建议做。
4. ✅ `checkGeeUse` / `geevalidate` / `login` 全部走 iOS 头，Python 实现已跑通。
5. ⚠️ `User-Agent` 不参与 headerSign，服务端不校验也能过；但**它不是无所谓**：
   同一次运行里如果 API 用平台 UA、OBS 直传用 `Python-urllib`，就出现两种客户端。
   现在全链（含 OBS PUT/GET）统一取 `realclient.user_agent(acct)`。
6. ✅ 提交后 `get_one_record` 回读与 OBS 轨迹读取都在 iOS 头下通过（466 点）。
7. ❓ `deviceName` 发具体机型还是统一 `iPhone`。两家参考实现都用 `iPhone`
   （理由是 iOS 的 `UIDevice.current.model` 真机恒为 `iPhone`）；
   但我们的真实 iOS 提交用 `iPhone 15 Pro Max` 被服务端接受且四项风险全 0。
   既然已经用它建立了设备画像，**不改现存 profile**——中途改字段等于换机。
8. ✅ iOS `headerSign` 的插入顺序 + 原生类型（`uid/studentId` 数字、`isRoot` 布尔）
   已被真实登录/提交验证。

---

## 3b. 2026-10-29 外部复核：`czcdr/NekoSportsWorldTool-antiban`

这个衍生分支带一份逐条标注证据等级的审计（`review/ANTI-FLAG-AUDIT.md`），
其中与 iOS 直接相关的三条：

| 编号 | 结论 | 对本项目的处置 |
|---|---|---|
| A1 | 上游「iOS 登录 + Android 提交」= 同一 token 平台漂移，high | 我们本来就是全链 iOS，实测通过；不动 |
| — | `ua_for()` 必须全链唯一来源：上游登录/点位写死 iOS UA、OBS 直传漏设（发出 `ureq/x`）、提交另拼一套，一次运行四种 UA | 已修：`obs.put_object`/`get_object_bytes` 接受并传入平台 UA |
| — | iOS 头写死 `360x640 / 1080x1920 / x86_64` 是模拟器签名；`logicPixel × scale = physicPixel` 必须整数倍 | 我们早早改用真实机型像素对（全部 2x/3x），并加了 `Scale` 派生 |

另外该分支确认：官方 Android 7.4.0 dex 里 `SWCampus` 命中 0 次，只有 `okhttp/4.12.0`
这类默认值——所以 Android UA 保持 `okhttp/4.12.0`，iOS 才用 `SWCampus/...`。

---

## 4. 主线已经做了什么

已接入 master 并完成真机验证：

- `app/replica/application/device_profile.py`
  - 新安装默认 Android，`JHP_IOS_CHANCE` 默认 0；
  - iOS 机型、系统、物理/逻辑像素、`arm64e`、`isRoot=0` 按同一张表生成；
  - 80% 的 iOS profile 使用全零 IDFA（ATT 拒绝跟踪的常见状态）；
  - `audit_profile()` 检查机型/系统/像素/CPU/root 是否自洽。
- `app/replica/transport/ios_headers.py`
  - `build_ios_headers(acct, ts, nonce, idfa='')` 按参考字段顺序生成；
  - `ios_header_json()` 保留 `uid/studentId` 数字、`isRoot` 布尔和插入顺序；
  - `RealClient` 按 `Account.platform` 选择 Android 23 头或 iOS 头。
- `app/replica/transport/realclient.py`
  - 登录、提交、读取、注销都按平台走同一套 header；
  - 线上额外带平台对应的 User-Agent，但不参与 headerSign。
- `app/tests/test_ios_headers.py` + `app/tests/test_device_profile.py`
  - 校验字段顺序、类型、`tokenSign` 向量；
  - 校验 iOS 登录/提交使用同一台设备；
  - 校验随机池开关和旋转平台。

运行：

```powershell
cd app
python -m pytest tests/test_ios_headers.py tests/test_device_profile.py -q
```

---

## 5. 下一步实验方案（需要单独账号 + 真实网络批准）

> 不要用主账号。不要在主 data 目录上做。不要和 Android 提交混在一起。

1. **准备**
   - 新建一个独立 data 目录（例如 `app/data-ios-exp/`）。
   - 用一个测试账号（或明确允许实验的账号）。
   - 生成 iOS profile：
     - 用 `python -m replica.application.device_profile --data-dir <data>
       --rotate --platform ios`，或让 `JHP_IOS_CHANCE=100` 新建；
     - `DeviceId`：UUID 大写
     - `deviceName`：具体 iPhone 机型（如 `iPhone 15`），与系统/像素同一行
     - `osVersion=18.1` 或 `26.5.2`
     - `cpuModel=arm64e`
     - `appInstallTime`：2–60 天前
     - `IDFA`：全零（未授权跟踪）或 UUID
     - 像素/CPU/isRoot 按机型表
   - 旧 Android profile 先备份。

2. **只读探测**
   - 用 iOS 头请求 `api/v70100/user/info`。
   - 如果 401/10003/10007：停止，记录原始状态码/长度，不连续重试。
   - 如果 200：进入登录。

3. **登录**
   - `checkGeeUse` → GT4 → `geevalidate` → `login`，全部用 iOS 头。
   - 登录 body 保持上游形态：`osType="0"`。
   - 如果 10121：先在手机 App 退出；不要反复试。
   - 如果 10007/10003：停止，回滚 Android profile。

4. **一条小提交**
   - 1.0–2.5 km，单条。
   - 先用 iOS 头 + `runes/runef`（照 Android 提交的额外头）。
   - 如果服务端返回明确“缺少/非法 runes/runef”类错误，才允许去掉它们重试一次；否则不要猜。
   - 验收：`error=10000 + rrid`、`get_one_record` 回读、OBS 轨迹。
   - 任何一步失败：停止，回滚 Android profile，不提交第二条。

5. **结论（已执行，2026-10-25）**
   - 登录 + 提交 + 回读 + OBS 全通过：rrid `1344757460`，4.13 km / 2329 s /
     9'40"/km / 117 spm / 466 点，`status=0`、`isCheat=0`、`policy=0`、
     `stepFreqStatus=0`、`complete=true`。
   - 因此 iOS 已标为已验证；`JHP_IOS_CHANCE` 只是分布开关。
   - 仍然不建议把已有 Android 账号切到 iOS：设备/平台变更本身就是信号。

---

## 6. 风险

- **平台切换信号**：同一账号从 Android 9/x86_64 切到 iOS 18/arm64，比单纯换 DeviceId 更明显。
- **提交端已验证**：rrid `1344757460` 证明服务端接受 iOS 头；但这是单账号单条记录，
  不等于「iOS 比 Android 更稳」。
- **`runes/runef` 已验证可用**：iOS 提交带上它们没有被拒。
- **服务端可能软拒**：`200 + 0 bytes` 会被我们标成 unknown 并锁时段，不能重试。
- **回滚成本**：需要保留 Android profile 和回执，失败后恢复并重新登录。
- **`deviceName` 语义仍无抓包**：参考实现发 `iPhone`，我们发现存机型，两者都被接受。

---

## 7. 参考链接

- https://github.com/YanamiNeko/NekoSportsWorldTool/blob/main/src/crypto/header.rs
- https://github.com/YanamiNeko/NekoSportsWorldTool/blob/main/src/api/login.rs
- https://github.com/YanamiNeko/NekoSportsWorldTool/blob/main/src/api/client.rs
- https://github.com/YanamiNeko/NekoSportsWorldTool/blob/main/src/api/submit.rs
- https://github.com/YanamiNeko/NekoSportsWorldTool/blob/main/src/ui/device.rs
- https://github.com/Funrestan/FunSportWorld/blob/main/funsport/config.py
- https://github.com/Funrestan/FunSportWorld/blob/main/funsport/crypto/header.py
- https://github.com/react-native-device-info/react-native-device-info#getinstanceid
