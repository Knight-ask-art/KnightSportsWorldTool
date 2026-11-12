# 架构护栏与 run-flow 拆分 — 状态

> 日期：2026-11-12
> 分支：`task/arch-guardrails`
> 合并状态：`master` 当前在 `好想跑-user-review` 工作树检出且有未提交的用户改动，
> 本轮未强行 merge；待该工作树提交/暂存后再合并。
> 计划：`PLAN.md`

## 已完成

| 项 | 结果 |
|---|---|
| T1 基线 | `976 passed`；`node --check` 通过 |
| T2 native 边界守卫 | 新增 `app/tests/test_native_boundaries.py`（6 项） |
| T3 分层导入守卫 | 新增 `app/tests/test_architecture_imports.py`（2 项） |
| T4 bridge helper | 新增 `adapters/bridge_common.py`，`bridge_api` 重新导出 |
| T5 run-flow | 新增 `adapters/bridge_run.py` 的 `RunBridgeMixin`；`bridge_api` 保持同名方法 |
| T5b Agent/route-memory | 新增 `adapters/bridge_agent.py` 的 `AgentBridgeMixin` |
| T6 LLM 共享契约 | 上限、状态分类、超时/温度归一化收口到 `llm_settings` |
| T7 文档 | 更新 `docs/architecture.md`、`docs/hardening.md` |

## 结构结果

- `app/replica/adapters/bridge_api.py`：**5,549 → 3,498 行**（facade + 非 run-flow 方法）
- `app/replica/adapters/bridge_run.py`：1,068 行（fence / points / preview / snap / submit）
- `app/replica/adapters/bridge_agent.py`：886 行（route-memory 进度/线程 + Agent 工具）
- `app/replica/adapters/bridge_common.py`：179 行（响应/点位/JSON helper）
- 所有 bridge 方法签名和返回 envelope 未变。
- AST 对比：HEAD 的 231 个 `ReplicaApi` 方法全部保留；`real_submit_run`
  仅把 `AMAP_KEY` 读取改为 `self._amap_key()`，另移除一个未使用的 `as e`
  绑定，其余方法体逐 AST 等价。

## 安全边界证据

`test_native_boundaries.py` 静态锁定：

- C++ `main.cpp`：无 `extern "C"` / `__declspec(dllexport)` / `LoadLibrary`；
  保留 `AddVectoredExceptionHandler`、`RaiseException`、`g_states`、`kTtlMs = 3000`。
- Rust `hxp-core`：仍是 `[[bin]]`，无 `cdylib`/`staticlib`/`dylib`、无
  `#[no_mangle]`/`extern "C"`。
- Go `hxp-guard`：无 cgo/`import "C"`/`//export`；`attest.go` 仍有 5 个采集
  func、`go func(` 并发和 nonce 置换。
- Python `shield.py` / `rustcore.py` / `account_guard.py`：只用 `subprocess`，
  不加载 native DLL。
- `license_decoy.py`：只导入 `time`，无文件/网络/子进程；只被
  `bridge_api` 的方法内懒加载，正常登录/提交不引用。

`test_architecture_imports.py` 锁定：

- `domain` / `transport` 不反向依赖 `application` / `adapters`；
  `application` 不依赖 `adapters`。
- 仅允许登记的惰性例外：`domain.crypto -> transport.rustcore`、
  `domain.rundata -> transport.rustcore/obs`。
- 新增文件级 import 环直接失败（当前唯一允许的环是
  `domain.rundata <-> transport.obs`，反向边只在 `obs.self_test` 内）。

## 验证

```powershell
cd 好想跑/app
python -m pytest tests -q
# 985 passed

python -m pytest tests/test_architecture_imports.py tests/test_native_boundaries.py -q
# 8 passed

python -m replica.selfcheck
# 28/28 模块通过
```

`node --check frontend/js/app.js` 通过。

## 未做 / 下一阶段

- 前端 `app.js` 仍为单文件；按 section 拆分留到 UI 大改时做。
- `domain -> transport` 的 native 加速调用尚未收口成端口；目前由守卫测试
  登记为惰性例外。
- `bridge_api` 仍有 session / settings / update / diagnostics 等长方法；
  可按同样 mixin 方式继续拆，但本轮优先保证 run-flow 和 Agent 的行为不变。
- 真实第三方 LLM / 真实记录 P0 未在本轮验证。
