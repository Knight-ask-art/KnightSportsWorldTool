# 好想跑 · 应用代码

纯 Python 的本地工作台：本地 HTTP 服务 + pywebview 窗口 + `replica` 包。
产品入口、协议实现、领域计算、适配器和兼容层都在这里。

## 分层

```
replica/
├─ transport/     # 协议、加密、HTTP 客户端、高德/极验/OBS
├─ domain/        # 纯计算：坐标、轨迹、身份、规则、时间与脱敏
├─ application/   # 用例编排：登录、保活、点位、策略、闸门、查询
├─ adapters/      # 入口适配：bridge API、HTTP 服务、CLI
├─ compat/        # 旧 71 API 的 mock 服务（兼容用，不是核心业务）
├─ diagnostics/   # 脱敏诊断包 + 模块自测
├─ pipeline.py    # 兼容入口：python -m replica.pipeline
└─ selfcheck.py   # 兼容入口：python -m replica.selfcheck
```

依赖方向：`adapters → application → domain`；`application` 可以用
`transport`，`domain` 不反向依赖上层。`compat` 只被 `adapters` 引用。

## 入口

| 命令 | 作用 |
|---|---|
| `python app.py` | 桌面窗口（pywebview） |
| `python run.py --port 65520` | 浏览器模式 |
| `python -m replica.pipeline --help` | 命令行全链路 |
| `python -m replica.selfcheck` | 离线模块自测 |
| `python tools/validate_sim.py` | 造数/校验器回归 |

## 测试与构建

```powershell
cd 好想跑/app
python -m pytest tests -q
python -m replica.selfcheck
python tests/legacy/legacy_bridge_suite.py      # 非 pytest 的历史套件
python tests/legacy/legacy_http_suite.py

powershell -ExecutionPolicy Bypass -File .\build.ps1
# 或直接调用 packaging/build.ps1
```

## 目录

| 路径 | 说明 |
|---|---|
| `frontend/` | 单页 UI（HTML/CSS/JS + Leaflet 本地资源） |
| `tests/` | pytest 套件；`tests/legacy/` 为历史 `main()` 运行器 |
| `tools/` | 活跃工具；`tools/legacy/` 为 unidbg 时代脚本 |
| `fixtures/` | 静态样例（不含账号/token） |
| `packaging/` | PyInstaller spec、构建脚本、图标、用户说明 |
| `design/` | 设计源文件，不参与打包 |
| `data/` `backup/` `dist/` | 运行时/构建产物，已被 `.gitignore` 排除 |

## 数据与凭据

- 运行时目录由 `JHP_DATA_DIR` 控制，默认 `app/data/`。
- 密码不落盘；token 只保存在本机 `data/accounts_emu.json`。
- 不要把 `data/`、`backup/`、`dist/` 或任何 `.pem` 提交到版本库。
- 采集与样本库不在本目录，见仓库根 `private/`。
