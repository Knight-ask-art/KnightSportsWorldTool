# 极验 v4：实现、观测与失效应对

> 结论先行：这套东西**不会**「永不失效」。GeeTest 的全部商业模式就是让客户端复刻变得困难。
> 合理目标是 **一年坏 1–2 次，每次两小时内恢复**。
> 恢复靠的不是重写，而是：先分清坏在哪一层，再加一条不依赖我们代码的兜底。

---

## 1. 代码结构

| 文件 | 职责 | 什么时候需要动它 |
|---|---|---|
| `replica/transport/gt4_image.py` | PNG → 缺口 x（灰度 → 5×5 高斯 → Canny → ZNCC） | GeeTest 改图、改缩放 |
| `replica/transport/gt4_payload.py` | 缺口 → `w` 明文；PoW、动态键、AES | **上游改字段（最常见）** |
| `replica/transport/gt4_rsa.py` | `w` 尾部的 RSA；可从线上 JS 刷新模数 | GeeTest 换密钥 |
| `replica/transport/gt4.py` | 编排、错误分类、落盘、canary | 协议流程本身变了 |

拆开的理由很实际：上游改字段的频率远高于改图像。字段变的时候不需要重测 CV，
CV 变的时候也不可能悄悄改到 payload。

### `w` 里最容易丢的两块

```json
{"setLeft":N,"passtime":N,"userresponse":N,"device_id":"","lot_number":"…",
 "pow_msg":"…","pow_sign":"…","geetest":"captcha","lang":"zh","ep":"123",
 "biht":"1426265548",
 "gee_guard":{"roe":{…}},
 "YciC":"P3Vn",                              ← 常量，漏了就是低信任票
 "<lot[26:30]+lot[7:11]>":"<lot[6:14]>",      ← 每次挑战现场切，无法重放
 "em":{…}}
```

`gt4_payload.PAYLOAD_KEYS` 是字段基线。`build_payload` 每次都会校验键集和顺序，
少一个、多一个、换顺序都会抛 `PayloadError`。**宁可当场炸，也不要发一张低信任票出去。**

### 逐字节回归

`gt4_payload.GOLDEN_SHA256` 是拆分前 `build_w` 产出的明文哈希。任何重构只要改动
一个字节，`python -m replica.transport.gt4` 就会红。这条基准在拆分时当场抓出了
漏掉的 `YciC` 字段。

---

## 2. 三种失效，不要混为一谈

| 层 | 现象 | 判断依据 | 性质 |
|---|---|---|---|
| **A** | GeeTest `/verify` 不是 `success` | `probe` 返回 `stage=GeeRejected` | 解题器或网络坏了 |
| **B** | GeeTest `success`，`geevalidate` 回 `10003` | `probe` 通过，登录仍 10003 | `w` 字段/信任分不够 |
| **C** | `geevalidate` 10000，`login` 回 `10007` / `10121` | 前两步都过 | 账号/IP 风控，**与极验无关** |

当年第 44–49 轮的全部误判，就是把 B 当成 C：两个第三方解题器都缺同一个字段，
于是「交叉验证」实际是「同一个 bug 测了两遍」，最后得出「必须真机」的错误结论。

代码里的对应关系：

- `GeeUnavailable` —— 网络/HTTP，可重试
- `GeeSchemaChanged` —— `/load` 或 `/verify` 结构变了
- `GeeRejected` —— GeeTest 明确拒绝，重试同一个 id 没意义
- `GeeImageError` —— 本地 CV 失败
- `RiskControlError` —— `geevalidate` 10007，重试同样没意义

---

## 3. 观测：坏了先留证据

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `JHP_GT4_DUMP` | `fail` | `off` / `fail` / `always`，控制落盘时机 |
| `JHP_GT4_DUMP_DIR` | `<data>/diagnostics/gt4` | 落盘位置 |
| `JHP_GT4_RSA` | 关闭 | 线上 JS 里**没有**模数；设 `on` 才会去抓，纯属给未来留的钩子 |

### 失败时落什么

```text
data/diagnostics/gt4/<YYYYmmdd-HHMMSS>/
  attempt.json   static_path / captcha_mode / check_device / load_keys
                 gap / bg_width / payload（完整明文）/ verify 原始响应
  bg.png         背景图
  slice.png      拼图块
```

`attempt.json` 里的 `pass_token` / `captcha_output` 会被截断成 8 个字符——
排障不需要完整凭证，泄漏了却是真凭证。

**`static_path` 是判断「服务端升级了」最直接的信号。** 它从 `v1.9.8-0748f2` 变成
别的值，就说明 GeeTest 发了新版本；`check_device` 从 `null` 变成 `true` 也同理。

### canary：只验极验，不碰账号

```powershell
cd app
python -m replica.transport.gt4 --canary
python -m replica.transport.gt4 --canary --json
```

它只跑 `/load → 图像 → PoW → w → /verify`，**不会**调用 `geevalidate`，也不会
触发登录风控。

- canary 失败 → A 层，解题器/网络
- canary 通过但登录 10003 → B 层，字段/信任分
- 两步都通过但登录 10007 → C 层，账号/IP

UI 侧对应 `real_gt4_probe`。

---

## 4. 恢复路径

### 第一步：看 `static_path` 有没有变

变了就是 GeeTest 升级。先看 `attempt.json` 里的 `load_keys`，跟 `gt4.LOAD_FIELDS`
比一比，`/load` 是不是多了必填字段。

### 第二步：字段 diff（B 层）

拿一份最新的 `w` 明文（`attempt.json` 里的 `payload`），逐键对照上游
`YanamiNeko/NekoSportsWorldTool` 的 `src/api/gt4.rs`。

上游通常比我们快几天。改 `gt4_payload.py` 即可，`gt4_image.py` 不用重测。

改完先跑：

```powershell
python -m replica.transport.gt4        # 离线：黄金向量 + 字段基线
python -m replica.transport.gt4 --canary
```

### 第三步：手动兜底（用户侧不受影响）

登录失败且错误里带 `极验|gee|gt4|10003|滑块` 时，登录框会露出
**「验证码过不去？手动滑一下」**。

点开是一个模态窗口，里面加载的是 **GeeTest 官方 `gt4.js`**，不是我们的实现。
用户拖完，四个凭证回传给 `real_login_ticket`，走正常的 `geevalidate → login`。

这条路径的价值不在于「方便」，而在于**它不会随 GeeTest 改字段而失效**，
因为它跑的就是官方客户端代码。

### 第四步：跟上游

`gt4_payload.py` 和 `gt4_image.py` 分开之后，上游的字段改动可以逐条对照着搬，
不用每次重跑图像回归。

---

## 5. 硬编码清单

| 常量 | 位置 | 失效症状 | 现状 |
|---|---|---|---|
| `_RSA_LIMBS` | `gt4_rsa.py` | `/verify` 直接 `fail` | 硬编码 1024 位。2026-10-26 实测 `v1.9.8-0d53fd` 的 `gcaptcha4.js`（728KB）里没有 `RSA`、没有 256 位十六进制串，`gct4.*.js` 只有 3KB 加载器 —— 所以自动刷新默认关闭，真要轮换只能手工替换 |
| `1.0059466666666665` | `gt4_payload.py` | 缺口找对了但 `userresponse` 错 | 图宽落在 280–420px 区间时保持原式；超出这个区间（retina / 改过控件尺寸）才按 `bg_width` 换算 |
| `passtime: 1887` | `gt4_payload.py` | 长期固定值可能被判脚本 | 按滑动距离生成 0.9–3.4s 的合理值 |
| PoW 固定 `sha256` + 8 位 | `gt4_payload.py` | 服务端提高难度或换算法时直接失败 | 已改为按 `pow_detail` 的 `bits` / `hashfunc` 算；实测线上是 `bits=0 / md5` |
| `YciC` / `gee_guard` / `em` | `gt4_payload.py` | 低信任票 → 10003 | 字段基线校验，改动会抛 `PayloadError` |
| `CAPTCHA_ID_CANDIDATES` | `gt4.py` | `/load` 直接报错 | 两个 id 依次尝试 |

---

## 6. 重试策略（刻意保守）

```text
一个解题周期：2 个 captcha id，各 1 次
  ├─ GeeRejected    → 立刻换下一个 id，不重试
  ├─ GeeUnavailable → 同 id 重试（默认 attempts=1，也不重试）
  └─ 都失败          → 抛错，交给手动兜底
geevalidate 只给 1 次机会：10003 直接转手动，不再自动重解
```

同一 IP 连续失败十几次，本身就像攻击。旧版一次登录最多会解 18 次，现在是 2 次。
要临时放宽可以显式传 `max_captcha_rounds=2`，但默认不放宽。

---

## 7. 未验证 / 已知限制

- **手动兜底没有在真实网络下跑过。** `captcha_id` 可能绑定了域名，`http://127.0.0.1`
  这个 origin 是否被 GeeTest 接受，要等第一次真机验证。如果不接受，错误会显示在
  模态窗口里，不影响自动解题路径。
- **RSA 自动刷新默认关闭**：实测线上 `gcaptcha4.js` 里没有公钥，抓了也是白抓。代码留了 `JHP_GT4_RSA=on` 的钩子，等 GeeTest 哪天把密钥搬回 JS 再打开。
- **`pow_detail` 参数会变**：2026-10-26 线上是 `bits=0 / hashfunc=md5`。现在按服务器说的算，0 位就直接返回，不再硬套 sha256 + 8 位。
- 没有代码签名；安装包和 zip 只有 SHA-256。
- 只做滑块（`risk_type=slide`）。GeeTest 若切到点选/无感，`/load` 字段会变，
  `GeeSchemaChanged` 会立刻报出来。

---

## 8. 历史

完整过程在 `archive/unidbg-era/FRIDA_NEXT.md`：第 37 轮定位「登录前必须先过极验」，
第 39–40 轮用第三方工具跑通，第 44–49 轮把 10003 误判成环境问题。
结论写在这里，过程留在档案里，不用重走。
