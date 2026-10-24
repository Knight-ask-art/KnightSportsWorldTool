# iOS 设备指纹：研究记录与接入状态

> 状态：**已接入 master 的随机池，但 iOS 提交链仍未真实验证。**
> 分支：`experiment/ios-device-profile`（历史原型，已合入主线）
> 结论先行：**目前没有证据表明 iOS 比 Android 更稳；参考实现的提交端固定是 Android。**
> 因此 iOS 只作为随机选项，默认占比 30%（`JHP_IOS_CHANCE`），登录和提交都使用
> iOS 头以保证“同一台设备”，但提交链必须实测后才能称为稳定。

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
appVersion=7.3.40
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

## 3. 还没验证的未知项

如果要把 iOS 做成真正的提交链，至少有以下未知：

1. `save/record` 是否接受 iOS 头（`osType=1`、`appVersion=7.3.40`、没有 `appUpdateTime`、`CustomDeviceId` 带 `_iOS_sportsWorld_campus`）？
2. iOS 提交是否仍然需要 `runes/runef`？上游只在 Android 提交路径里加。
3. 服务端是否把 token 绑定到平台/设备？同一个账号从 Android 切到 iOS 是否直接 401/10121/10007？
4. `checkGeeUse` / `geevalidate` 是否接受 iOS 头？上游登录链用 iOS 头，理论上可以，但没有我们的 Python 实现验证过。
5. `User-Agent` 和 HTTP `appVersion` 是否必须与 iOS 头一致？我们的 Android 链目前不发 UA，也过了；iOS 可能不同。
6. 提交后的回读/OBS 是否还能用同一套 header？`get_one_record` 和 OBS 签名是平台无关的，但 header 会变成 iOS。
7. `deviceName` 到底发具体机型（`iPhone 15`）还是统一的 `iPhone`？参考 builder 使用
   `identity.device_name`，其默认值是 `iPhone`；我们选择具体机型是为了和像素/系统
   成对，但如果服务端只认 `iPhone`，需要改回。
8. iOS 的 `headerSign` JSON 是否真的是插入顺序 + 原生类型？这是从参考 builder 的
   字段顺序推断的；没有真机抓包，iOS 登录可能直接 `10003`。

---

## 4. 主线已经做了什么

已经接入 master，但提交链仍标注未验证：

- `app/replica/application/device_profile.py`
  - 新安装从 Android / iOS 随机池抽取，`JHP_IOS_CHANCE` 默认 30；
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

5. **结论**
   - 当前状态是“iOS 已进入随机池，但提交链未验证”，不是“iOS 已稳定”。
   - 只有“登录 + 提交 + 回读 + OBS”全部通过，才把 iOS 提交标为已验证，才考虑
     提高 `JHP_IOS_CHANCE`。
   - 失败时用 `--rotate --platform android` 回到 Android；不要在主账号上反复试。
   - 即使通过，也不建议把已有 Android 账号切到 iOS；设备/平台变更本身就是信号。

---

## 6. 风险

- **平台切换信号**：同一账号从 Android 9/x86_64 切到 iOS 18/arm64，比单纯换 DeviceId 更明显。
- **提交端未验证**：上游自己都不走 iOS 提交，说明这条路没有现成证据。
- **`runes/runef` 未知**：iOS 提交是否需要，没有公开证据。
- **服务端可能软拒**：`200 + 0 bytes` 会被我们标成 unknown 并锁时段，不能重试。
- **回滚成本**：需要保留 Android profile 和回执，失败后恢复并重新登录。

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
