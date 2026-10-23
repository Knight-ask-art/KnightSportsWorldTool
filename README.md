# 好想跑

本地跑步数据工作台：登录自己的账号 → 读取校区围栏与打卡点 → 画路线 / 造数 →
本地校验 → 提交 → 只读回查。桌面端窗口 + 本地 HTTP 服务，不上传第三方服务器。

> 仅用于本人账号与已授权环境。不要用它处理他人账号或未授权的数据。

## 目录

```text
好想跑/
├─ README.md                  # 本文件：入口、边界、命令
├─ .gitignore                 # 运行数据/凭据/研究资料全部排除
├─ docs/                      # 使用、API、运维、决策、历史、架构
│  ├─ architecture.md         # 分层规则与迁移映射
│  ├─ handoff.md              # 会话交接（历史记录）
│  ├─ 01-usage.md
│  ├─ 02-api.md
│  ├─ 03-operations.md
│  ├─ 04-decisions.md
│  └─ 05-history.md
├─ jhp_phase1/                # 应用源码与构建根
│  ├─ app.py                  # 桌面入口
│  ├─ run.py                  # 浏览器入口
│  ├─ build.ps1               # 兼容包装 → packaging/build.ps1
│  ├─ pyproject.toml          # 依赖与 pytest/ruff 配置
│  ├─ README.md               # 代码结构与分层说明
│  ├─ replica/                # 分层包（transport/domain/application/...）
│  ├─ frontend/               # 单页 UI + Leaflet
│  ├─ tests/                  # pytest；legacy/ 为历史运行器
│  ├─ tools/                  # validate_sim.py；legacy/ 为归档脚本
│  ├─ fixtures/               # 静态样例
│  ├─ packaging/              # PyInstaller + 图标 + 用户说明
│  ├─ design/                 # 设计源文件
│  ├─ data/  backup/  dist/   # 运行时/构建产物（忽略）
├─ private/                   # 不公开：采集与样本库（不打包、不进 UI/API）
│  ├─ README.md
│  ├─ research/               # collection.py / corpus.py + 私有测试
│  └─ data/                   # 采集产物（忽略）
├─ archive/                   # 历史文档（忽略；含旧凭据，勿外发）
├─ apk_work/                  # 逆向证据与反编译产物（忽略）
└─ platform-tools/            # adb 等外部工具（忽略）
```

## 快速开始

```powershell
cd 好想跑\jhp_phase1

# 1) 安装依赖（已有环境可跳过）
python -m pip install -e .

# 2) 桌面窗口
python app.py

# 3) 浏览器模式
python run.py --port 65520
```

首次登录在「账号」页完成；密码只在内存中，token 存在本机 `data/`。

## 测试

```powershell
cd 好想跑\jhp_phase1
python -m pytest tests -q            # 主测试套件
python -m replica.selfcheck          # 22 个模块离线自测
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
cd 好想跑\jhp_phase1
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

- 使用说明：[`docs/01-usage.md`](docs/01-usage.md)
- Bridge API：[`docs/02-api.md`](docs/02-api.md)
- 运行与排错：[`docs/03-operations.md`](docs/03-operations.md)
- 架构与分层：[`docs/architecture.md`](docs/architecture.md)
- 决策记录：[`docs/04-decisions.md`](docs/04-decisions.md)
