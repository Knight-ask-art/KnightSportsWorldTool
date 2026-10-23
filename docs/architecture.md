# 架构与目录说明

> 2026-10-24 结构重构后的现状。目标不是套模板，而是把「协议/计算/用例/入口/兼容」
> 分开，让改动落在单一层里。
>
> 同日工作区目录更名：`jhp_phase1/` → `app/`，`apk_work/` → `reverse-engineering/`，
> `platform-tools/` → `third_party/platform-tools/`。

---

## 1. 分层

```text
adapters  ──▶ application ──▶ domain
   │              │
   │              └──▶ transport ──▶ domain（只做数据转换）
   └──▶ compat（旧 71 API 的 mock，仅本地）
```

| 层 | 目录 | 职责 | 不该做的事 |
|---|---|---|---|
| transport | `replica/transport/` | 信封/加密、HTTP 客户端、高德、极验、OBS、unidbg sidecar | 决定业务规则、读写会话状态 |
| domain | `replica/domain/` | 坐标、轨迹生成、路线几何、田径场、时间/脱敏、签名原语 | 发起网络请求、读写用户数据目录 |
| application | `replica/application/` | 登录、保活、点位、学校策略、限额/回执、只读查询 | 解析命令行、直接返回 HTTP |
| adapters | `replica/adapters/` | bridge API、静态文件/HTTP 服务、CLI | 写业务算法 |
| compat | `replica/compat/` | 旧 71 API 的 mock 学校/后端 | 被 domain/application 反向依赖 |
| diagnostics | `replica/diagnostics/` | 脱敏诊断包、模块自测 | 参与提交链路 |

已知例外：`domain/rundata.py` 的校验函数在运行时惰性导入
`transport/obs.build_laps()` 做圈段交叉校验；这是单向的、仅校验用途。
若要彻底消除，可把 `build_laps` 下沉到 domain，当前不做。

---

## 2. 模块归属

| 目录 | 模块 |
|---|---|
| `transport/` | `netsec` `nskpure` `nskcodec` `crypto`* `realclient` `sidecar` `amap` `gt4` `obs` |
| `domain/` | `coords` `crypto`* `health` `safety` `rundata` `routes` `stadium` |
| `application/` | `login_flow` `tokenkeeper` `points` `policy` `riskcheck` `qualification` `identity` `variation` `limits` `guard` `readonly` |
| `adapters/` | `bridge_api` `http_server` `cli` |
| `compat/mocks/` | `mock_iydsj` `mock_65530` |
| `diagnostics/` | `collector` `selfcheck` |

\* `crypto` 是签名/加密原语，放在 domain；其中 `ensure_keypair()` 只被 mock
服务用于生成本地测试密钥，不影响主链路。

---

## 3. 入口与兼容

| 入口 | 实现 | 说明 |
|---|---|---|
| `python app.py` | `replica.adapters.http_server` | 桌面窗口 |
| `python run.py` | 同上 | 浏览器模式 |
| `python -m replica.pipeline` | `replica/adapters/cli.py` | 由 `replica/pipeline.py` 薄包装转发 |
| `python -m replica.selfcheck` | `replica/diagnostics/selfcheck.py` | 由 `replica/selfcheck.py` 薄包装转发 |
| `POST /bridge/<method>` | `replica/adapters/bridge_api.py` | 本地 UI 的 JSON 接口 |

`replica` 是唯一 canonical 导入名（工作目录为 `app/`）。从仓库根目录执行
`python -m app.replica.*` 也能运行，但那是命名空间路径，不应在代码里使用；
测试、工具和应用统一从 `replica` 导入，避免同一模块被加载两次。

---

## 4. 数据目录

| 路径 | 内容 | 入库/打包 |
|---|---|---|
| `app/data/` | 会话、设备指纹、限额、回执、点位缓存、路线库 | 否 |
| `app/backup/` | 本机数据备份 | 否 |
| `app/dist/` | 构建产物 | 否 |
| `private/data/` | 采集样本（含轨迹） | 否 |

`JHP_DATA_DIR` 覆盖应用数据目录；`JHP_CORPUS_DIR` 覆盖私有样本目录。

---

## 5. 迁移映射（2026-10-24）

| 旧位置 | 新位置 |
|---|---|
| `replica/realclient.py` `netsec.py` `nskpure.py` `nskcodec.py` `crypto.py` `sidecar.py` `amap.py` `gt4.py` `obs.py` | `replica/transport/` |
| `replica/coords.py` `crypto.py` `health.py` `safety.py` `rundata.py` `routes.py` `stadium.py` | `replica/domain/` |
| `replica/login_flow.py` `tokenkeeper.py` `points.py` `policy.py` `riskcheck.py` `qualification.py` `identity.py` `variation.py` `limits.py` `guard.py` `readonly.py` | `replica/application/` |
| `replica/server.py` | `replica/adapters/http_server.py` |
| `replica/replica_api.py` | `replica/adapters/bridge_api.py` |
| `replica/pipeline.py` | `replica/adapters/cli.py` + 薄包装 |
| `replica/selfcheck.py` | `replica/diagnostics/selfcheck.py` + 薄包装 |
| `replica/diagnostics.py` | `replica/diagnostics/collector.py` |
| `replica/mock_65530.py` `mock_iydsj.py` | `replica/compat/mocks/` |
| `campus_fence_47203.json` | `fixtures/` |
| `build.ps1` `build.spec` `EXE_README.txt` `assets/logo.ico` | `packaging/` |
| `assets/logo-source.png` | `design/` |
| `tools/*`（unidbg 探针） | `tools/legacy/` |
| `tests/test_phase1.py` `test_phase2.py` `test_realclient_smoke.py` | `tests/legacy/legacy_*` |
| `HANDOFF.md` | `docs/handoff.md` |
| `replica/corpus.py` | `private/research/`（不公开） |

---

## 6. 验证基线

```powershell
cd 好想跑/app
python -m pytest tests -q                 # 87 passed
python -m replica.selfcheck               # 22/22 modules
python tests\legacy\legacy_bridge_suite.py  # 38 checks
python tests\legacy\legacy_http_suite.py    # 23 checks
python tools\validate_sim.py              # 1/2/3/5 km + validator
python app.py --check                     # exit 0
```

真实登录 / 真实提交 / OBS 回放仍未在本轮验证，状态见 `decisions.md`。
