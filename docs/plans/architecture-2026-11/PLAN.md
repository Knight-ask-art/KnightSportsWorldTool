# 架构护栏与 run-flow 拆分（2026-11-12）

目标：在不改协议、不改四语言 native 边界的前提下，降低 `bridge_api.py` 的维护成本，并把“层方向 / 无清晰 FFI / 蜜罐隔离”变成可执行测试；前端拆分留到 UI 大改时。

## 约束（不可退让）

- 四种语言保持进程隔离，不新增 DLL / `extern "C"` / `__declspec(dllexport)` / `#[no_mangle]` / cgo 导出。
- 跨语言只走 stdin/stdout 的不透明帧或命令行 JSON；不暴露可读的鉴权符号。
- Go 仍负责并发采集 + 置换，Rust 仍负责混合，C++ 仍负责 VEH 平坦化状态机，Python 只拿 3 秒 token。
- `license_decoy` 仍是被动蜜罐：不联网、不落盘、不参与登录/提交。
- 不改变 `real_submit_run` / `real_fetch_points` 等 bridge 方法的签名和返回 envelope。

## 任务

- [x] T1 基线：全量测试 + git 状态（976 passed，node --check 通过）
- [x] T2 新增 native 边界守卫测试（无清晰 FFI、蜜罐隔离）
- [x] T3 新增分层导入守卫测试（domain/transport/application/adapters）
- [x] T4 抽出 `bridge_common.py`（`_ok`/`_fail`/`_fail_submit`/格式化/点位归一化/合理性警告）
- [x] T5 抽出 `bridge_run.py` 的 `RunBridgeMixin`（fence/points/preview/snap/submit）
- [x] T5b 抽出 `bridge_agent.py` 的 `AgentBridgeMixin`（route-memory 进度/线程 + Agent 工具）
- [x] T6 LLM 共享契约去重（响应上限、状态分类、超时/温度归一化）
- [x] T7 更新架构文档与状态记录
- [x] T8 全量测试 + 前端语法检查 + 自检（985 passed，28/28，node --check 通过）
