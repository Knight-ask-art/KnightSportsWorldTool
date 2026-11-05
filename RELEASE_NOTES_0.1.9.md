# 好想跑 v0.1.9 发布说明

- **版本号**：v0.1.9
- **发布日期**：2026-11-05
- **渠道**：stable
- **上一版本**：v0.1.8（保留为回滚版本）

> 这是一次修复版本。重点不是新功能，而是结束 v0.1.7 / v0.1.8 留下的两个环境问题：
> 旧版共享设备指纹，以及 Android/iOS 版本头落后于官方 7.4.0。
> 相关说明和道歉：https://github.com/Knight-ask-art/KnightSportsWorldTool/issues/9

## 本版修复

### 1. 旧版共享设备指纹

- 旧版 `legacy-default` 指纹会让老用户共用同一个 `DeviceId`，并带 `x86_64`、`isRoot=1`、机型/系统/像素不自洽等特征；服务端会看成「单设备多账号 + 模拟器/root」。
- v0.1.9 启动时检测到这类指纹，会一次性生成唯一、自洽的新指纹。
- 迁移前自动备份为 `device_profile.backup-*.json`；迁移后强制退出一次本机登录，避免旧 token 和新设备混用。
- 唯一但字段不自洽的旧指纹，只修 CPU/root/机型/系统/像素，尽量保留原 `DeviceId`。
- 共享旧常量、`x86_64`、`isRoot=1`、机型系统像素对不上时，不再允许保存或发送。

### 2. Android / iOS 协议对齐

- Android `appVersion` 从 `7.3.11` 对齐到 `7.4.0`。
- iOS `appVersion` 和 UA 从 `7.3.40` 对齐到 `7.4.0`（App Store 2026-10-26 版本）。
- 修正 Android 登录前 `tokenSign` 应为空。
- 修正 iOS 布尔头在 HTTP 线上被 Python 写成 `False` 的问题（签名仍是原生 `false`）。
- 只改设备身份但不重新登录的路径会被拦下。

### 3. 测试与自检

- `pytest app/tests -q`：665 passed
- `python -m replica.selfcheck`：28/28 模块通过
- 冻结包 `好想跑.exe --check` 通过
- 冻结包 HTTP 冒烟通过
- zip 不含 `data/`、`replica/*.py`、`app.py`

## 升级注意

- 首次升级到 v0.1.9 会退出一次登录，并提示「旧版共享设备指纹已修复」。这是预期行为。
- `data/` 不会删除；回执、限额、点位缓存保留，旧指纹备份在 `data/device_profile.backup-*.json`。
- 重新登录后，账号页设备一行应显示「本机随机 / 旧版已修复 / 已修正」，不再显示「旧版冻结」。
- 不要反复更换设备指纹；迁移只做一次，频繁换机本身也是风险信号。
- 需要回退时，下载 `latest.json` 里 `rollback` 指向的 v0.1.8 包覆盖安装，`data/` 同样保留。

## 道歉

关于 v0.1.7 / v0.1.8 造成的异常和损失，我在 issue #9 里做了说明和道歉：

https://github.com/Knight-ask-art/KnightSportsWorldTool/issues/9

是我经验不足、验证不够，把 UI 改动和协议改动混在一起，也没有在发布前重新验证旧数据升级路径和 iOS 7.4.0。对不起。
