# 好想跑

本地跑步数据工作台：登录自己的账号 → 读取校区围栏与打卡点 → 画路线 / 造数 →
本地校验 → 提交 → 只读回查。桌面端窗口 + 本地 HTTP 服务，不上传第三方服务器。

> 仅用于本人账号与已授权环境。不要用它处理他人账号或未授权的数据。

## 目录

```text
好想跑/
├─ README.md                        # 本文件：入口、边界、命令
├─ .gitignore                       # 运行数据 / 凭据 / 研究资料排除规则
├─ app/                             # 桌面应用：源码、前端、测试、打包
│  ├─ app.py                        # 桌面窗口入口（pywebview）
│  ├─ run.py                        # 浏览器模式入口
│  ├─ build.ps1                     # 构建包装脚本 → packaging/build.ps1
│  ├─ pyproject.toml                # 依赖、pytest、ruff 配置
│  ├─ README.md                     # 代码结构与分层说明
│  ├─ replica/                      # 协议复刻与业务核心
│  │  ├─ transport/                 # 网络、加密、登录、OBS
│  │  ├─ domain/                    # 坐标、轨迹、规则、安全边界
│  │  ├─ application/               # 用例编排：预检、保活、限额、查询
│  │  ├─ adapters/                  # HTTP bridge、静态服务、CLI
│  │  ├─ compat/                    # 旧接口的兼容 mock
│  │  └─ diagnostics/               # 自检与脱敏诊断
│  ├─ frontend/                     # 单页界面（HTML/CSS/JS + Leaflet）
│  ├─ tests/                        # pytest；legacy/ 为历史运行器
│  ├─ tools/                        # 仿真校验；legacy/ 为归档脚本
│  ├─ packaging/                    # PyInstaller 配置、图标、用户说明
│  ├─ fixtures/                     # 测试样例数据
│  ├─ design/                       # 设计源文件
│  └─ data/  backup/  dist/         # 运行时 / 构建产物（不提交）
├─ docs/                            # 项目文档
│  ├─ usage.md                      # 安装、界面、命令行、环境变量
│  ├─ api.md                        # 接口与字段
│  ├─ operations.md                 # 文件位置、限额、排错
│  ├─ decisions.md                  # 设计与取舍
│  ├─ history.md                    # 历史与证据
│  ├─ architecture.md               # 分层规则与迁移映射
│  └─ handoff.md                    # 会话交接
├─ private/                         # 私有采集与样本库（不打包、不进 UI/API）
│  ├─ README.md
│  ├─ research/                     # collection.py / corpus.py + 私有测试
│  └─ data/                         # 采集产物（不提交）
├─ reverse-engineering/             # APK / 协议逆向证据（不提交，勿外发）
├─ archive/                         # 历史归档（不提交；含旧凭据）
└─ third_party/                     # 外部工具
   └─ platform-tools/               # adb / fastboot（不提交）
```

### 各目录做什么

| 目录 | 用途 | 是否进安装包 |
|---|---|---|
| `app/` | 唯一的产品代码根：桌面入口、核心逻辑、前端、测试、打包配置 | 是 |
| `docs/` | 使用、接口、运维、决策、历史与架构文档 | 否 |
| `private/` | 采集与样本库，只在本机研究使用，不注册接口 | 否 |
| `reverse-engineering/` | APK 逆向、协议分析、历史证据，体积大且含敏感信息 | 否 |
| `archive/` | 早期文档与备份，仅供追溯 | 否 |
| `third_party/` | 外部下载的工具（Android platform-tools） | 否 |

## 快速开始

```powershell
cd 好想跑\app

# 1) 安装依赖（已有环境可跳过）
python -m pip install -e .

# 2) 桌面窗口
python app.py

# 3) 浏览器模式
python run.py --port 65520
```

首次登录在「账号」页完成；保活用的密码只在内存中，token 存在本机 `data/`。
登录页可勾选「记住密码」：勾选后用 Windows DPAPI 加密保存到
`data/saved_login.json`，不勾选不保存，取消勾选立即删除。

设备指纹每个 `data` 目录首次运行生成一次，保存在 `data/device_profile.json`；
登录和提交共用同一份。旧数据升级会冻结原来的提交指纹；更换需显式运行
`python -m replica.application.device_profile --data-dir <data> --rotate`，
旧指纹会先备份到 `device_profile.backup-*.json`。`data/` 不参与打包。

## 测试

```powershell
cd 好想跑\app
python -m pytest tests -q            # 主测试套件
python -m replica.selfcheck          # 23 个模块离线自测
python tools\validate_sim.py         # 造数/校验器矩阵

# 历史 main() 套件（不进入 pytest）
python tests\legacy\legacy_bridge_suite.py
python tests\legacy\legacy_http_suite.py

# 私有研究区（不属于产品）
cd ..
python -m pytest private\research\tests -q
```

## 构建

```powershell
cd 好想跑\app
powershell -ExecutionPolicy Bypass -File .\build.ps1
# 输出 dist\好想跑_v<版本>\ + dist\好想跑_v<版本>_win64.zip
```

构建脚本会做冻结导入检查和 HTTP 冒烟；同版本重建会保留目标目录里的 `data/`，
但不会把 `data/` 打进压缩包。

## 三条边界

1. **采集 / 样本库不公开**：只在 `private/`，不注册 bridge、不出现在 UI、不打包。
2. **不重试提交**：网络超时视为未知状态，写前回执会锁住时段，宁可少跑一次。
3. **凭据不入库**：`data/`、`backup/`、`dist/`、`.pem`、历史凭据目录全部忽略。

## 文档

- 使用说明：[`docs/usage.md`](docs/usage.md)
- Bridge API：[`docs/api.md`](docs/api.md)
- 运行与排错：[`docs/operations.md`](docs/operations.md)
- 架构与分层：[`docs/architecture.md`](docs/architecture.md)
- 决策记录：[`docs/decisions.md`](docs/decisions.md)
