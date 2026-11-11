# 30 · 数据目录与设备指纹

> 证据等级：**A**（源码 / `docs/operations.md`）
> 最后更新：2026-11-11 · 对应版本 **0.2.2**

## 1. `data/` 里有什么

默认根目录 `app/data/`，可用 `JHP_DATA_DIR` 覆盖。

| 文件 | 内容 | 能否外发 |
|---|---|---|
| `accounts_emu.json` | 会话（uid、校区、中心点、keepalive）；token 由 DPAPI 封存为 `token_enc` | **不能** |
| `device_profile.json` | 设备指纹（平台、DeviceId、机型、系统、安装/更新时间） | **不能** |
| `device_profile.backup-*.json` | 换指纹前的备份 | **不能** |
| `device_migration.json` | 一次性迁移通知（脱敏 DeviceId） | 可以 |
| `devices/<uid>.json` | CLI `--device-id` 的本次覆盖记录（审计） | **不能** |
| `limits/<uid>.json` | 按日期存的提交计数（北京时间） | 可以 |
| `submission_receipts/<uid>__<unid>.json` | 提交回执（状态、rrid、时间窗） | 可以（不含 token/坐标） |
| `points_cache.json` | 点位缓存（300 秒；10603 回退可读 24 h） | 可以 |
| `points_ratelimit.json` | 点位接口 10603 冷却截止时间 | 可以 |
| `routes.json` | 路线库（手绘/导入/记录生成） | 可以 |
| `updates/` | 已校验 sha256 的更新包 | 可以 |
| `diagnostics/gt4/<时间戳>/` | 极验失败现场（含 `w` 明文） | **不能** |
| `cache/gt4/rsa-<版本>.json` | RSA 模数缓存，仅 `JHP_GT4_RSA=on` 时生成 | 可以 |
| `saved_login.json` | “记住密码”的 DPAPI 密文 | **不能** |
| `students.json` / `sport.json` / `account.json` | 旧 mock / 本地账号 | **不能** |

诊断包是**脱敏**的：不含 token、密码、uid、DeviceId、坐标、IP、原始请求体、
校区数字 ID、本地路径。要贴日志/提 issue 时用它。

## 2. 设备指纹

- 每个 `data` 目录只生成一次，登录、保活、提交、OBS、注销共用同一份，不会逐请求漂移。
- 新安装默认 Android；iOS 占比由 `JHP_IOS_CHANCE` 控制（默认 0，`100` 强制 iOS）。
- iOS 7.3.40 已真机验证；当前商店 7.4.0，**iOS 提交/OBS 未重新验证**。
  默认已是 Android；需要 iOS 时才开。
- 设备指纹不合法（共享常量、`x86_64`、`isRoot=1`、机型/系统/像素不一致）时
  拒绝保存/提交。
- 已有 Android 账号不要随手切 iOS；跨平台切换比同平台换机型更显眼。

### 旧版共享指纹迁移

触发条件：`source=legacy-default`，或 DeviceId 等于旧共享常量
（`afa505…4311`，带 `x86_64` / `isRoot=1`）。

处理：

1. 生成唯一、自洽的新指纹；
2. 先备份为 `device_profile.backup-*.json`；
3. 强制退出本机登录，旧 token 不与新设备混用；
4. 重新登录后全链路使用新指纹。

唯一但字段不自洽的旧指纹，只修 CPU/root/机型/像素，保留 DeviceId。

手动命令：

```powershell
cd app
python -m replica.application.device_profile --data-dir data --audit
python -m replica.application.device_profile --data-dir data --migrate
python -m replica.application.device_profile --data-dir data --repair-pixels
python -m replica.application.device_profile --data-dir data --rotate --platform android
```

任何字段修改都会改变指纹，必须由操作者确认；修改前自动备份。

## 3. 在 UI 里改设备

入口统一在 **设置 → 设备指纹**（账号页只保留一个跳转按钮）：

- 只读区：设备、脱敏 DeviceId、平台、屏幕、缩放、来源、环境自检、UA。
- **高级功能：更换设备指纹**：随机 / Android / iOS，输入“更换”二次确认。
- **高级功能：自定义设备**：平台可选 Android / iOS；机型、系统版本、物理/逻辑
  分辨率、CPU 都自己填写，保存前按平台规则校验。

约束：

- 自定义机型可以自由输入；`audit_profile` 对 `source=custom` 检查竖屏、比例、
  iOS 2x/3x 和 CPU 家族，不再要求机型在示例表内。
- 自定义 DeviceId 仍拒绝旧版共享常量。
- 保存前先校验，校验通过才退出登录；保存后旧指纹备份、强制重新登录。
- 自定义来源记为 `source=custom`，UI 显示“自定义”。

## 4. DPAPI 与密码

- 会话 token 用 Windows DPAPI 加密后存 `token_enc`，不落明文。
- “记住密码”单独存 `data/saved_login.json`，同样走 DPAPI；取消勾选立即删除。
- 自动重登的密码只在进程内存里，重启即失效。
- `disabled: true` 的会话不会被探测或重登（停用旧账号的硬开关）。

## 5. 迁移与备份

- **升级/迁移复制整个 `data/`**，含 `device_profile.json`。
  只复制 `accounts_emu.json` 会被当成旧数据并触发迁移。
- 回滚指纹：把 `device_profile.backup-*.json` 复制回 `device_profile.json`，重启。
- 把整个 `data/` 复制给别人 = 复制同一台设备，绝对不要。
- `data/` 不参与打包；安装包和更新器都跳过它。

## 6. 安全边界

1. 不在 issue / 日志 / 截图里贴 token、cookie、DeviceId、坐标、手机号。
2. 诊断包只用于排错，确认脱敏后再发。
3. `output/`、`private/`、`app/data/` 都不进 git；发布分支只有 README + latest.json。
4. 设备指纹、账号、回执不要跨机器拷贝。

## 7. 常见坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 换电脑后要求重新登录 | 只复制了 `accounts_emu.json`，触发指纹迁移 | 复制整个 `data/` |
| 提交被拒“设备不一致” | 指纹字段不自洽或共享常量 | `--audit` 后 `--migrate` |
| 登录后 token 立刻失效 | 旧 token 与新指纹混用 | 重新登录，让登录/提交共用新指纹 |
| 更新后数据没了 | 把 `data/` 放在程序目录又被覆盖 | 确认更新器/安装包跳过 `data/`；从备份恢复 |
| 诊断包不敢发 | 怕含凭据 | 用 `diagnostics` 模块生成脱敏包 |
