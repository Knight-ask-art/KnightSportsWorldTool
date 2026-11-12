# 路线记忆（Route Memory）实现规格 v1

> 状态：计划稿，供多个 Claude Code worker 并行实现。
> 仓库：`好想跑`（Python 3.11 / 纯本地 / Nuitka standalone）。
> 本文件是接口契约。worker 不得擅自改接口；需要变更先写进 `QUESTIONS.md`。

---

## 0. 目标与非目标

### 目标

1. 从用户**自己的历史跑步记录**里学习常跑路线。
2. 把学到的路线作为模板，按当前目标距离和当前打卡点重新生成一条可提交路线。
3. 在 UI 里以“从你跑过的 N 条记录学到 3 条路线”呈现，不叫“AI 画线”。
4. 全部本机处理，不上传轨迹，不把坐标发给任何 LLM。
5. 设置页提供 LLM 协议、Base URL、模型、API Key、温度和超时配置；Key 用 DPAPI 封存，默认关闭。
6. 提供一个专门的 Agent 页面：用户说目标，Agent 编排“学习 → 推荐 → 适配 → 预览 → 审计”，停在用户确认，永不自动提交。

### 非目标（v1 明确不做）

- 不做地图匹配（HMM / Valhalla / OSMnx / FMM）。
- 不引入 `geopandas` / `shapely` / `movingpandas` / `scikit-mobility` / `osmnx`。
- 不做 LLM 生成坐标。
- 不自动提交；Agent 必须停在用户确认。
- 不学习其他账号、其他校区、工具自己伪造的记录（用本地回执 uuid 排除）。
- 不改提交链、不改协议字段、不改 `data/` 里的账号/凭据格式。

---

## 1. 现有代码事实（worker 必须先读）

| 能力 | 位置 |
|---|---|
| 记录列表 | `replica/application/readonly.py::recent_records` |
| 单条详情 + OBS 回退 | `replica/application/readonly.py::record_detail` |
| 轨迹解析（GCJ） | `replica/application/readonly.py::parse_track` |
| 路线几何 | `replica/domain/routes.py` |
| 打卡点命中 | `replica/domain/routes.py::missed_points`（25 m） |
| 插入打卡点 | `replica/domain/routes.py::snap_through` |
| 围栏判断 | `replica/domain/routes.py::outside_fences` |
| 闭环重复/裁剪 | `replica/domain/rundata.py::build_loop` / `_trim_polyline` |
| 路线库 | `replica/adapters/bridge_api.py::real_get_routes` / `real_save_routes` |
| 前端路线库 | `app/frontend/js/app.js`（`ROUTE_KEY` / `persistRoutes` / `autoGenerate`） |
| Bridge 返回风格 | `_ok({...})` / `_fail('...')`，`code=200` |

约束：`domain` 不反向依赖 `application` / `adapters`；`application` 可以依赖 `domain`。

---

## 2. 数据流

```text
历史记录列表
  → 过滤（同账号/同校区/质量合格/非工具生成）
  → 逐条详情（OBS 回退）
  → parse_track → 清洗 → 单圈提取 → 简化
  → 增量聚类（k-medoids）
  → 模板（medoid 真实轨迹）
  → data/route_memory/<uid>_<unid>.json

当前请求（目标 km + 当前点位 + 围栏）
  → 模板打分
  → top 3
  → 旋转起点 + 插入漏点位
  → 返回 waypoints（交给现有 preview_route）
  → 现有检查表 + audit
```

---

## 3. 模块与接口（硬契约）

### 3.1 `app/replica/domain/trajectory.py`

纯函数，只依赖标准库 + `math`；性能关键路径可使用项目已有的 `numpy`（不得引入 scipy / shapely / pandas）。不读写文件、不联网。

```python
from __future__ import annotations
from typing import Sequence

Point = tuple[float, float]

def haversine_m(a: Point, b: Point) -> float:
    """两点球面距离，米。"""

def polyline_length_m(points: Sequence[Point]) -> float:
    """折线累计长度，米。少于 2 点返回 0。"""

def simplify(points: Sequence[Point], tolerance_m: float = 2.5,
             max_points: int = 400) -> list[Point]:
    """Douglas-Peucker；结果不超过 max_points（超过时按步长抽稀）。"""

def resample(points: Sequence[Point], n: int = 128) -> list[Point]:
    """按弧长等距重采样；输入少于 2 点原样返回。"""

def is_closed(points: Sequence[Point], tolerance_m: float = 30.0,
              ratio: float = 0.03) -> bool:
    """首尾距离 <= max(tolerance_m, ratio * length) 视为闭环。"""

def extract_loop(points: Sequence[Point], min_lap_m: float = 500.0
                 ) -> list[Point] | None:
    """从闭环轨迹中提取单圈。

    从累计长度 40% 之后开始，找第一个回到起点 tolerance 内的点；
    该段累计长度 >= min_lap_m 时返回 start..i；否则返回 None。
    输入不是闭环时返回 None。
    """

def canonical_loop(points: Sequence[Point]) -> list[Point]:
    """闭环规范化：起点旋到纬度最大（并列取经度最小）的点；
    再按有向面积统一为顺时针；返回首尾不重复的 list。"""

def route_distance(a: Sequence[Point], b: Sequence[Point],
                   closed: bool = False) -> float:
    """两条轨迹的形状距离，米。

    closed=True：重采样 128 点，正反方向 + 所有循环偏移取最小；
    closed=False：重采样 128 点，对称 Chamfer 距离。
    """

def medoid_index(tracks: Sequence[Sequence[Point]], closed: bool = False) -> int:
    """返回到其他轨迹平均 route_distance 最小的下标。空输入返回 -1。"""

def point_to_polyline_m(point: Point, line: Sequence[Point]) -> float:
    """点到折线各段的最小距离，米。"""
```

补充要求：

- `simplify` 必须保留首尾点。
- `resample` 必须保留首尾点，n >= 2。
- `route_distance` 对空/单点输入返回 `float('inf')`，不要抛异常。
- 所有函数对 `lat/lng` 非法值（NaN/None/0,0）要跳过或返回安全值，不能崩溃。
- 不修改输入 list。

### 3.2 `app/replica/application/route_memory.py`

只做纯逻辑 + 本地文件；网络请求留在 bridge。依赖 `domain.trajectory` 和 `domain.routes`。

```python
SCHEMA_VERSION = 1
THRESHOLD_M = 50.0
MIN_LAP_M = 200.0
MIN_TRACK_POINTS = 20
MIN_DISTANCE_M = 500.0
MAX_DISTANCE_M = 20000.0

def memory_path(store_dir: str, uid, unid) -> str
def load(store_dir: str, uid, unid) -> dict
def save(store_dir: str, uid, unid, doc: dict) -> None
def clear(store_dir: str, uid, unid) -> None

def track_from_detail(detail: dict) -> list[Point]:
    """从 record_detail 返回体取 track，转成 [(lat,lng)]，丢弃非法点。"""

def learn_from_tracks(tracks: list[dict], owner: str,
                      campus: dict | None = None,
                      threshold_m: float = THRESHOLD_M) -> dict:
    """输入清洗后的 track dict，返回 memory doc（不写盘）。

    track dict:
      {
        "rrid": str, "line": [(lat,lng), ...],
        "distance_m": float, "duration_s": int, "start_ms": int,
        "is_cheat": int, "complete": bool, "source": "server"|"tool"
      }
    """

def summarize(doc: dict) -> dict:
    """给 UI/Agent 的摘要：模板数、样本数、最近学习时间、可读/跳过统计。"""

def suggest(doc: dict, km: float, points: list[dict],
            fence: list[list[Point]] | None = None,
            center: Point | None = None) -> list[dict]:
    """返回 top 3 候选，按分数降序。没有候选返回 []。"""

def apply_template(doc: dict, template_id: str, km: float,
                   points: list[dict] | None = None) -> dict:
    """返回 {"ok": True, "waypoints": [...], "closed": bool, ...}
    或 {"ok": False, "error": "..."}。"""
```

`learn_from_tracks` 规则：

1. 过滤：`source != 'tool'`、`is_cheat == 0`、`complete is True`、点数 >= 20、距离 0.5–20 km；
   单点前后跳变都超过 1000 m 的“飞点”整条剔除。
2. `line` 先 `simplify`，再判断闭环；闭环先 `extract_loop` 取候选段，
   再用“锚定周期性”把它裁成恰好一圈（首尾回到起点才计数），
   最后 `canonical_loop`。200 m / 400 m 小圈同样适用。
3. 闭环按 `route_distance(closed=True)` 增量聚类；开放路线按 `closed=False`。
4. 新轨迹与最近 medoid 距离 <= threshold 则并入，否则新建簇。
5. 每簇重算 medoid；模板 `line` 用 medoid 的简化轨迹（闭环先 canonical）。
6. 模板 `lap_m = polyline_length_m(line)`；闭环要把首尾闭合段算进去
   （`line + [line[0]]`）；`avg_distance_m` 取簇内原始距离均值。
7. `id` 用 `lr_` + sha1(line 的 6 位小数串)[:8]；同一输入必须稳定。
8. 输出 doc 必须可 JSON 序列化；不能包含 tuple（转 list）。
9. 空输入返回合法 doc：`templates: []`，`records_seen: 0`。

`suggest` 规则：

1. 对每个模板：
   - 单模板 `samples < 3` 或 `medoid_distance_m > 60` 直接淘汰（低置信不推荐）。
   - 闭环：`laps = ceil(target_m / lap_m)`；`laps > 30` 淘汰；`lap_m > target_m * 1.5` 淘汰。
   - 开放：`lap_m > target_m * 1.5` 淘汰；距离拟合按 `build_open` 的
     “够长就截断、否则来回折返”计算，不硬编码 2 倍。
   - 围栏：`fence` 非空时，模板所有点必须 `outside_fences` 为空；否则淘汰。
   - 点位：调用 `routes.missed_points`；`isMustPoint` 漏一个淘汰；普通点计入覆盖分。
   - 起点距离：模板首点到 `center`（或第一个点位）的距离；越近越好。
   - 新鲜度/频次：`last_seen_ms`、`samples`。
   - 绕行：`snap_through` 插入漏点后长度 - 原长度；计入扣分。
2. 分数归一化到 0–1；返回字段见 §3.3。
3. `reasons` 用中文短句，必须基于实际数据，不要编。
4. 返回的 `line` 是**已适配当前请求的 waypoints**，不是模板原线；闭环要旋转起点、插入漏点，长度不要求精确等于目标（交给 preview 的 `build_loop`）。
5. 最多返回 3 条。

`apply_template`：

1. 找到模板；找不到返回 `ok=False`。
2. 闭环：`canonical_loop` 后旋转到离 `center`/首点位最近的起点；若有 `points`，用 `snap_through` 插入。
3. 开放：保持方向；`snap_through` 插入。
4. 返回 `waypoints`、`closed`、`lap_m`、`laps`、`target_m`、`missed`、`fence_ok`。
5. 不生成完整 run data，不写盘。

### 3.3 候选返回结构

```python
{
    "template_id": "lr_a1b2c3d4",
    "name": "学习路线 1",
    "score": 0.87,
    "coverage": 1.0,
    "reasons": ["围栏内", "5/5 点位", "2 圈 + 160 m", "最近 12 天跑过 3 次"],
    "line": [[22.4101, 112.9402], ...],
    "closed": True,
    "lap_m": 1420.5,
    "laps": 2,
    "samples": 8,
    "target_m": 2500.0,
    "missed": [],
    "fence_ok": True,
    "detour_m": 0.0,
}
```

---

## 4. 存储 schema

`data/route_memory/<uid>_<unid>.json`：

```json
{
  "version": 1,
  "owner": "uid:unid",
  "campus": {"center": [22.41, 112.94], "fence_hash": "..."},
  "updated_at_ms": 1792584000000,
  "records_seen": 42,
  "records_readable": 35,
  "records_skipped": 7,
  "templates": [
    {
      "id": "lr_a1b2c3d4",
      "name": "学习路线 1",
      "line": [[22.4101, 112.9402]],
      "closed": true,
      "lap_m": 1420.5,
      "samples": 8,
      "source_rrids": ["1344757460"],
      "first_seen_ms": 1790000000000,
      "last_seen_ms": 1792500000000,
      "avg_distance_m": 2860.0,
      "avg_pace": 6.1,
      "quality": {"ok": 8, "cheat": 0},
      "medoid_distance_m": 18.2
    }
  ]
}
```

- 文件名中的 `uid/unid` 必须替换掉 `/ \ : * ? " < > |` 和空白，防止路径注入。
- `load` 遇到损坏/旧版本返回空 doc，不抛异常。
- `save` 用原子替换（临时文件 + `os.replace`）。
- 不把原始轨迹写进 doc；`source_rrids` 只留 id。

---

## 5. Bridge API（`replica/adapters/bridge_api.py`）

新增方法，全部走现有 `_ok` / `_fail`：

| 方法 | 行为 |
|---|---|
| `real_route_memory_status(self)` | 返回 `summarize(doc)` + `templates` 摘要（不含完整 line）+ `progress` |
| `real_route_memory_learn(self, limit=50, force=False, wait=True)` | `wait=True` 同步返回摘要；`wait=False` 立即返回 `{started: true, progress}`，后台线程拉取/学习，前端轮询 `status` |
| `real_route_memory_suggest(self, km=2.5, points=None, fence=None, run_area_id=None)` | 读 doc，调用 `suggest`，返回 `candidates` |
| `real_route_memory_apply(self, template_id, km=2.5, points=None)` | 调用 `apply_template` |
| `real_route_memory_clear(self)` | 删除当前账号缓存 |
| `real_route_memory_save(self, template_id)` | 把模板复制进 `routes.json`，`type='learned'` |

要求：

- 账号来自 `self._real_account()`；无 token 返回 `_fail('未配置 token...')`。
- `learn` 必须先调 `recent_records`，再逐条 `record_detail`；详情失败跳过并计数，不能整批失败。
- `limit` 最大 100；详情请求之间至少 0.15 s，避免打爆。
- `learn` 进度写入内存 `self._rm_progress`（并镜像到 `data/route_memory/progress.json`）：
  `{running, phase, done, total, error, started_at_ms, finished_at_ms}`；
  `phase` ∈ `listing|detail|learning|saving|done|error`。
- `wait=False` 时后台线程必须自己捕获异常并写 `phase='error'`，不能让 UI 永久转圈。
- 工具自己生成的记录：检查 `data/submission_receipts/` 里是否有对应 uuid；有则 `source='tool'` 并跳过。
- `fence` 入参兼容：
  - `[[{lat,lon}, ...], ...]`
  - `[[[lat,lng], ...], ...]`
  - 统一转成 `list[list[tuple]]` 再传给 `suggest`。
- `points` 入参兼容 `lat/lon` 和 `lat/lng`；`isMustPoint` 原样保留。
- `real_route_memory_suggest` 的返回：
  ```python
  _ok({"candidates": [...], "templates": 3, "target_m": 2500})
  ```
- `learn` 返回：
  ```python
  _ok({"summary": summarize(doc), "templates": [...], "skipped": [...]})
  ```

---

## 6. 前端

### 6.1 路线库

在“路线库”页新增“学习路线”分组：

- 未学习：显示按钮“从跑步记录学习路线” + 说明“只读本机记录，不上传”。
- 学习中：进度条 + “已读取 18/42 条”。
- 已学习：卡片列表，每张显示名称、圈长、样本数、最近使用；操作“预览 / 存为路线”。
- 失败/0 条：显示原因和建议（记录太少 / 无轨迹 / 不在当前校区）。

### 6.2 自动模式

`autoGenerate()` 改为：

1. 先调 `real_route_memory_suggest`。
2. 有候选：弹出候选卡（最多 3 条），显示理由；选中后把 `line` 写入当前路线并走现有 `redraw()` / `scheduleRouteFinalize()`。
3. 无候选：保留当前最近邻逻辑，但提示“还没有学到路线，已用临时自动路线”。
4. 新增“反向跑”按钮（闭环时可用）。

要求：

- 不破坏 `route` / `pushRouteSnapshot` / `scheduleRouteFinalize` 的现有语义。
- 所有新文案中文；不使用“AI 生成”。
- 调用失败不能把页面卡死；toast + 回退。
- `node --check frontend/js/app.js` 必须通过。

---

## 7. Agent（第二阶段）

### 7.1 目标

- 给用户一个**专门的 Agent 页面**，不是藏在设置里的开关。
- 用户可以用自然语言说“帮我跑 2.5 公里”“换一条”“反向跑”“重新学习”，Agent 负责编排：
  收集目标 → 读学习路线 → 推荐 → 适配 → 预览 → 审计 → 等用户确认。
- Agent 只做编排和解释；路线几何、打卡点、围栏、距离全部走本地确定性代码。
- **Agent 永不提交**。页面上的“确认”只是把结果交给现有「跑步」页，最终提交仍由用户在原有确认/提交闸门点击。

### 7.2 状态机

```text
idle
  → collect     解析目标距离；读取当前点位/围栏/校区
  → learn       没有模板或用户要求重新学习时，只读拉取历史记录
  → suggest     模板打分，返回最多 3 条候选
  → adapt       选中模板后旋转起点、插入漏点位
  → preview     调用现有 preview_route 生成完整跑步模型
  → audit       调用现有 audit_run 做本地检查
  → wait_confirm
  → confirmed   只把预览参数交给前端，不提交
```

异常/回退：

```text
任何阶段失败 → error（带原因和下一步建议）
用户取消     → cancelled
学习结果弱   → wait_confirm 前直接 error/fallback，提示“记录还不够，先用自动/手绘”
```

### 7.3 后端模块 `app/replica/application/agent.py`

纯逻辑 + 线程编排；不直接发 HTTP，不直接 submit。依赖 `route_memory`、`routes`、`audit` 和 bridge 注入的工具函数。

```python
STATES = ('idle', 'collect', 'learn', 'suggest', 'adapt',
          'preview', 'audit', 'wait_confirm', 'confirmed',
          'cancelled', 'error')

class AgentError(RuntimeError):
    pass

class AgentSession:
    def __init__(self, tools: dict, store_dir: str, owner: str): ...
    def start(self, text: str = '', km: float | None = None,
              points: list[dict] | None = None,
              fence: list[list[tuple]] | None = None,
              run_area_id=None) -> dict: ...
    def status(self) -> dict: ...
    def message(self, text: str) -> dict: ...
    def confirm(self) -> dict: ...
    def cancel(self) -> dict: ...
```

- `start` 立即返回 `status()`，耗时步骤在后台线程执行；前端轮询。
- `status()` 返回：
  ```python
  {
    "session_id": "ag_...",
    "state": "wait_confirm",
    "goal": {"km": 2.5, "points": 5, "run_area_id": 47203},
    "log": [{"at_ms": ..., "level": "info", "text": "...",
             "tool": "route_memory_suggest", "data": {...}}, ...],
    "candidates": [...],       # 同 §3.3；选中前都带 line
    "selected": "lr_a1b2c3d4",
    "preview": {...},          # 现有 preview_route 的返回
    "audit": {...},            # 现有 audit_run 的返回
    "llm": {"used": false, "model": "", "error": ""},
    "stats": {
      "tool_calls": 8, "tool_retries": 0,
      "cache_hits": 2, "cache_misses": 1,
      "llm_calls": 0, "llm_cache_hits": 1, "llm_cache_misses": 0,
      "candidates_tried": 1
    },
    "error": "",
    "updated_at_ms": ...
  }
  ```
- `log` 只保留最近 200 条；`data` 字段不得含 token、uid、完整坐标。
- 工具返回必须过结构校验：`status/learn` 缺 `summary`、`suggest` 缺候选列表、`apply` 缺有效控制点、`audit` 缺布尔 `summary.ok`，一律进入 `error`；**缺审计 summary 不得当作通过**。
- 瞬时错误（超时/网络/429/5xx/繁忙）自动重试 1 次；token/401/403 不重试。
- 审计失败时自动尝试下一条候选，最多 3 条；全部失败仍停在 `wait_confirm` 并保持 `audit.summary.ok=false`，确认按钮不可用。
- 显式「用第 N 条」是用户决定，不参与自动轮换；审计失败时保留该候选，并在 `wait_confirm` 日志/提示中写明“未自动替换”。
- `suggest/apply` 使用进程内 TTL 缓存；缓存键包含记忆文件内容哈希，`learn/clear` 主动失效。`fence=None` 时围栏结果另缓存 60 秒，避免重复联网。
- 同一时间只允许一个运行中的会话；`start` 会取消旧线程（设置 `cancel_event`）。
- `confirm()` 只在 `wait_confirm` 状态有效；返回：
  ```python
  {"preview_args": {...}, "route": [[lat, lng], ...], "km": 2.5}
  ```
  不调用 submit，不写回执。

### 7.4 意图解析（确定性，不依赖 LLM）

`message(text)` 先走规则：

| 输入 | 动作 |
|---|---|
| 含 `2.5 km` / `2.5公里` / `跑三公里` / `3km路线` | 设置目标距离，重新 `suggest` |
| `换一条` / `下一条` | 候选列表轮换，重新 `adapt/preview/audit` |
| `反向` / `反着跑` | 闭环候选反向，重新预览 |
| `重新学习` / `再学一次` | 强制 `learn(force=True)` |
| `确认` / `就这条` | 等价 `confirm()` |
| `取消` / `算了` | `cancel()` |
| `跑3公里，用第2条` | 距离优先；新候选生成后优先第 2 条 |
| `换一条3公里` / `反向3公里` | 距离优先，重新推荐；动作在新候选上重新计算 |
| `不要取消` / `别再反向` | 否定词不触发对应动作 |
| 其他 | 记一条“没听懂”的提示，保持当前状态，不报错 |

中文数字要支持：一/二/三/四/五/六/七/八/九/十，以及 `半`（0.5）。
解析失败时不要猜，直接问“想跑多少公里？”。

### 7.5 Bridge API

| 方法 | 行为 |
|---|---|
| `real_agent_start(self, text='', km=None, points=None, fence=None, run_area_id=None)` | 创建/重置会话并启动；立即返回 `_ok(status)` |
| `real_agent_status(self)` | 返回当前 `status()` |
| `real_agent_message(self, text)` | 处理一条用户消息；返回 `_ok(status)` |
| `real_agent_confirm(self)` | 返回 `_ok({preview_args, route, km})`；不提交 |
| `real_agent_cancel(self)` | 取消并返回 `_ok(status)` |

- 没有会话时 `status` 返回 `state='idle'`，不报错。
- 账号/token 失效时 `start` 仍可进入 `collect`，但在 `learn` 阶段返回明确错误：
  “token 失效，请重新登录”；不得伪造模板。
- 工具注入：
  ```python
  tools = {
    "route_memory_status": ...,
    "route_memory_learn": ...,
    "route_memory_suggest": ...,
    "route_memory_apply": ...,
    "preview_route": ...,
    "audit_run": ...,
    "snap_missing_points": ...,
  }
  ```
  由 bridge 构造，Agent 模块不 import bridge（避免循环依赖）。

### 7.6 Agent 页面（`#page-agent`）

导航：

- 侧边栏「工作台」分组新增 `data-page="agent"`，label `Agent`，放在「跑步」之后。
- `PAGES` 增加 `'agent'`；`go('agent')` 时初始化页面（只初始化一次）。

页面结构：

```text
#page-agent
  header: Agent 跑步规划 + 状态 chip (#agentStateChip)
  #agentGoalBar: 目标距离 | 点位数 | 校区 | LLM 状态
  #agentTranscript: 消息流（用户/Agent/工具卡片），自动滚到底
  #agentCandidates: 候选卡（最多 3 张，含理由、圈长、样本数、点位覆盖）
  #agentPreview: 预览摘要（距离、圈数、配速、点位、围栏、审计）
  #agentComposer: 输入框 #agentInput + 发送 #agentSend + 快捷 chip
  #agentConfirm: “确认，去跑步页预览”按钮（默认 disabled）
  #agentCancel: “取消”
```

交互要求：

- 进入页面自动 `real_agent_status`；没有会话显示引导语和 3 个快捷入口：
  “帮我跑 2.5 公里”、“用我跑过的路线”、“先学习我的路线”。
- 发送消息后立即显示用户气泡，再轮询 `real_agent_status`（600 ms，直到
  `wait_confirm/confirmed/error/cancelled`）。
- 工具卡片显示中文动作：`正在读取跑步记录…`、`正在学习路线…`、`正在推荐…`、
  `正在生成预览…`、`正在检查…`；不要显示原始 JSON。
- 候选卡按钮：“用这条” → `real_agent_message('用第 N 条')`；
  “换一条” → `real_agent_message('换一条')`。
- 预览成功后显示 `#agentConfirm` 可用；审计不通过时显示原因并保持 disabled。
- 点击确认：
  1. 调用 `real_agent_confirm`；
  2. 把 `preview_args` 直接应用到跑步页：`route`、`#inKm`、`#inPace`、
     `#inCadence`、闭合状态；
  3. `go('run')` 并切到路线面板，停在预览态；
  4. 最终提交仍由用户在跑步页点击；Agent 代码里没有 `real_submit_run`。
- 取消：`real_agent_cancel`，清空候选和预览，回到 idle 引导。
- LLM 未配置时，顶部显示“未配置模型，Agent 使用本地规则”，并给一个
  `data-goto="settings"` 的链接；不阻塞任何流程。

### 7.7 日志与隐私

- 每次 Agent 工具调用写 `data/agent_runs/<session_id>.jsonl`：
  `{at_ms, state, tool, ok, detail, prompt_sha256?}`。
- 不记录 token、uid、账号昵称、完整坐标、原始 prompt。
- 页面上的消息流可以显示候选统计，但不显示原始坐标列表。

### 7.8 测试

| 文件 | 内容 |
|---|---|
| `tests/test_agent_route_memory.py` | 状态机合法/非法跳转、mock 工具、无模板回退、token 失效、取消、confirm 不调用 submit、日志脱敏、中文距离解析 |
| `tests/test_frontend_agent_page.py` | 导航项、页面 id、控件 id、确认按钮默认 disabled、页面代码不调用 `real_submit`、文案存在 |

### 7.9 验收

- 从 Agent 页输入“帮我跑 2.5 公里”，最多 3 次点击内看到候选、预览、审计结果。
- 确认后跳到跑步页，路线/距离/配速已填好，页面停在预览态，未提交。
- Agent 页任何路径都不产生提交回执。
- 无 LLM、token 失效、0 模板三种情况都有明确中文提示，不白屏。

---

## 8. 测试与验收

### 必跑

```powershell
cd app
python -m pytest tests -q
node --check frontend/js/app.js
```

### 新增测试

| 文件 | 内容 |
|---|---|
| `tests/test_trajectory.py` | 闭环、单圈、重采样、方向不变、相似度、medoid |
| `tests/test_route_memory.py` | 过滤、聚类、medoid、schema、损坏恢复、账号隔离、suggest 打分、apply |
| `tests/test_route_memory_bridge.py` | mock 记录/详情、无 token、OBS 失败跳过、fence 兼容 |
| `tests/test_frontend_route_memory.py` | 静态断言新 UI 文案/调用存在（不跑浏览器） |
| `tests/test_llm_settings.py` | 协议预设、存取、损坏恢复、非法 URL/协议、key 不回传、prompt 脱敏 |
| `tests/test_llm_client.py` | 各协议请求体、401/403/429/超时、错误信息不含 key |
| `tests/test_frontend_llm_settings.py` | 设置页控件 id、协议选项、文案静态断言 |
| `tests/test_agent_route_memory.py` | Agent 状态机、mock 工具、回退、取消、confirm 不提交、日志脱敏、中文距离 |
| `tests/test_frontend_agent_page.py` | Agent 导航/页面/控件、确认按钮默认 disabled、不调用 `real_submit` |

### 验收指标

- 10 条同环线 + 5 条折返 → 2 个模板。
- 2.5 km 和 3.0 km 同路线 → 同一模板。
- 正跑/反跑 → 同一模板。
- 当前 5 个点位 → top 1 snap 后 5/5，必经点 100%。
- 目标距离误差交给 preview；`apply_template` 返回的 waypoints 不超 400 点。
- 50 条学习 < 60 s（mock 网络时 < 2 s）。
- 无新增第三方依赖。

---

## 9. 工作纪律

- 每个 worker 只改自己 TASK 里列出的文件；跨文件改动先写 `QUESTIONS.md`。
- 不提交（不 `git commit`）；由计划者统一审查/提交。
- 不读写 `app/data/`、`private/`、`reverse-engineering/`、`third_party/`。
- 不联网；测试全部 mock。
- 代码风格跟随现有：`from __future__ import annotations`、4 空格、行宽 100、中文 docstring 可以。
- 完成后报告：改动文件、关键实现、测试命令与输出、未决问题。

---

## 10. 任务分解

| 任务 | 负责文件 | 依赖 |
|---|---|---|
| T0 P0 验证 | `tools/route_memory_spike.py` + `docs/plans/route-memory/P0-REPORT.md` | T1 |
| T1 轨迹几何 | `domain/trajectory.py` + `tests/test_trajectory.py` | 无 |
| T2 路线记忆核心 | `application/route_memory.py` + `tests/test_route_memory.py` | T1 |
| T3 路线记忆 Bridge | `adapters/bridge_api.py` 路线记忆方法 + `tests/test_route_memory_bridge.py` | T2 |
| T4 路线库前端 | `frontend/js/app.js`、`frontend/index.html`、`frontend/css/app.css` + `tests/test_frontend_route_memory.py` | T3 契约 |
| T5 Agent 后端 | `application/agent.py`（新建）+ bridge 工具 + `tests/test_agent_route_memory.py` | T3 |
| T6 验收 | 集成测试 + 文档 + 手工验收清单 | T1–T5, T9 |
| T7 LLM 设置后端 | `application/llm_settings.py`、`application/llm_client.py` + `tests/test_llm_settings.py`、`tests/test_llm_client.py` | 无 |
| T8 LLM 设置前端 | `frontend/js/app.js`、`frontend/index.html`、`frontend/css/app.css` + `tests/test_frontend_llm_settings.py` | T7 契约 |
| T9 Agent 页面 | `frontend/js/app.js`、`frontend/index.html`、`frontend/css/app.css` + `tests/test_frontend_agent_page.py` | T5, T4 |

### 并行波次（计划者调度）

```text
Wave 1: T1（几何）  ‖  T7（LLM 后端）  ‖  T8（LLM 前端，按契约写）
Wave 2: T0（P0 验证，用 T1）  ‖  T2（记忆核心，用 T1）  ‖  T3（bridge，按契约写）
Wave 3: T4（路线库前端）  ‖  T5（Agent 后端）
Wave 4: T9（Agent 页面，等 T4 合并后）
Wave 5: T6（验收、文档、手工清单）
```

- T7/T8 先合，避免和 T3/T4/T9 抢 `bridge_api.py` / `app.js`。
- T2 与 T3 可以同时开发：T3 按本文件的函数签名写，合并 T2 后再跑测试。
- T4 与 T5 无文件冲突，可并行；T9 必须等 T4 合并，避免同一前端文件冲突。
- 同一文件同一时间只允许一个 worker 改；冲突由计划者裁决。

---

## 11. LLM 设置与常见协议（T7 / T8）

### 11.1 目标

- 设置页可以配置 LLM 的协议、服务地址、模型、API Key、温度和超时。
- LLM 只做两件事：**在已有候选路线里选一条**、**用中文解释理由**。
- LLM 不生成坐标，不参与几何计算，不接触原始轨迹。
- 默认关闭。关闭或未配置时，路线学习、推荐、预览、审计全部照常工作。

### 11.2 协议预设（硬契约）

| id | 显示名 | 默认 Base URL | 认证 | 请求 |
|---|---|---|---|---|
| `openai` | OpenAI 兼容 | `https://api.openai.com/v1` | `Authorization: Bearer <key>` | `POST /chat/completions` |
| `deepseek` | DeepSeek | `https://api.deepseek.com/v1` | Bearer | 同 OpenAI |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | Bearer | 同 OpenAI |
| `anthropic` | Anthropic | `https://api.anthropic.com` | `x-api-key` + `anthropic-version: 2023-06-01` | `POST /v1/messages` |
| `gemini` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta` | `x-goog-api-key` | `POST /models/<model>:generateContent` |
| `ollama` | Ollama 本地 | `http://127.0.0.1:11434/v1` | 无 | 同 OpenAI |
| `custom` | 自定义（OpenAI 兼容） | 空 | Bearer（有 key 才发） | 同 OpenAI |

- 协议列表必须由后端 `PROTOCOLS` 单一来源提供，前端只渲染，不硬编码第二份。
- 新增协议只加后端配置 + 前端无需改代码。
- `custom` 允许任意 `http/https` Base URL；其余协议允许用户改 Base URL（代理/镜像），但仍按该协议的请求格式发送。

### 11.3 存储与密钥

`data/llm_settings.json`：

```json
{
  "version": 1,
  "enabled": false,
  "protocol": "openai",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "api_key": "<DPAPI base64>",
  "has_key": true,
  "temperature": 0.2,
  "timeout_s": 30,
  "updated_at_ms": 1792584000000,
  "last_test": {"ok": true, "at_ms": 1792584000000, "latency_ms": 812, "error": ""}
}
```

硬要求：

- `api_key` 用 `replica.application.credentials.seal(key, entropy=b'haoxiangpao.llm.v1')` 封存；**绝不写明文**。
- `load()` 返回的设置**永远不含 `api_key`**，只含 `has_key`。
- `save()` 收到空 key 表示“保持原 key 不变”；收到 `clear_key=true` 才删除。
- 非 Windows（DPAPI 不可用）保存 key 返回明确错误；不写明文降级。无 key 的其余字段仍可保存。
- 原子替换写盘（临时文件 + `os.replace`）。
- 损坏/旧版本/未知协议 → 返回默认设置，不抛异常。
- 日志、错误信息、测试输出不得出现 key 或 key 片段。

### 11.4 后端模块

`app/replica/application/llm_settings.py`：

```python
SCHEMA_VERSION = 1
PROTOCOLS: dict[str, dict]          # id -> {label, base_url, auth, kind}
DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 5
MAX_TIMEOUT_S = 120

def presets() -> list[dict]:
    """[{id,label,base_url,needs_key}]，给前端渲染协议下拉。"""

def load(store_dir: str) -> dict:
    """含 has_key，不含 api_key；损坏返回默认值。"""

def save(store_dir: str, payload: dict) -> dict:
    """校验并保存；返回 load() 的结果。空 key = 不修改。"""

def clear(store_dir: str) -> bool:
    """删除设置文件和 key。"""

def effective(store_dir: str) -> dict | None:
    """给调用方：返回含明文 api_key 的运行时设置；未启用/无 key 返回 None。"""

def build_choice_prompt(candidates: list[dict], target_m: float) -> list[dict]:
    """构造 messages；只含候选序号、圈长(取整)、样本数、点位覆盖、围栏、绕行。"""

def parse_choice(text: str, candidates: list[dict]) -> str | None:
    """从模型回复里解析 template_id；解析失败返回 None。"""
```

`app/replica/application/llm_client.py`：

```python
def chat(settings: dict, messages: list[dict], proxies: dict | None = None) -> dict:
    """返回 {ok, text, error, latency_ms, status}；超时 30s；响应上限 256 KB。"""

def test_connection(settings: dict, proxies: dict | None = None) -> dict:
    """发一条 “只回复 OK” 的最小请求，返回 {ok, latency_ms, model, error}。"""
```

- 只依赖 `requests` 和标准库。
- 不在此模块读写 `data/`，不打印请求头。
- 错误分类：`timeout` / `auth`(401/403) / `not_found`(404) / `rate_limit`(429) / `server`(5xx) / `network`。
- 错误文本可以带 HTTP 状态和 provider 返回的 `error.message`，但必须先做 key 替换（`***`）。

### 11.5 脱敏与隐私（硬门禁）

- `build_choice_prompt` 的输入只允许：候选序号、`lap_m` 取整、`laps`、`samples`、`missed` 数量、`fence_ok`、`detour_m` 取整、`score`、`coverage`、目标距离取整。
- 禁止出现：任何经纬度、完整 `line`、`template_id` 以外的 id、uid/unid、token、设备指纹、校区名、账号昵称。
- 测试必须用正则断言 prompt 序列化后不含 `\d{2,3}\.\d{4,}`、`token`、`uid`、`unid`。
- LLM 返回只用于“选第几条 + 解释”；最终 `line` 必须来自本地候选。

### 11.6 Bridge API

| 方法 | 行为 |
|---|---|
| `real_llm_settings(self)` | 返回 `{settings, protocols}`；settings 不含 key |
| `real_llm_settings_save(self, payload)` | 校验 + 保存；返回同 `real_llm_settings` |
| `real_llm_settings_clear(self)` | 删除设置 |
| `real_llm_test(self)` | 用已保存设置发最小请求；返回延迟/模型/错误分类 |

- 无 key 且协议 `ollama`/`custom` 允许测试；其余协议无 key 返回 `_fail('未配置 API Key')`。
- 测试连接走当前 `netProxy` 内存设置（与高德/网络设置共用），不落盘。
- 任何返回值不得包含 key；`real_llm_settings` 的 `has_key` 只表示“已保存”。

### 11.7 前端（设置页）

在「网络与吸附」之后新增 `#llmSection`：

- 开关：`#llmEnabled`（默认关）。
- 协议：`#llmProtocol`（后端 presets 渲染）；切换时若 Base URL/模型为空或仍是上一个预设的默认值，自动填新预设默认值。
- Base URL：`#llmBase`（`type=url`，允许 `http://127.0.0.1`）。
- 模型：`#llmModel`，placeholder 根据协议变化（如 `gpt-4o-mini` / `deepseek-chat` / `claude-sonnet-4-5` / `gemini-2.0-flash` / `qwen2.5:7b`）。
- API Key：`#llmKey`，`type=password`，placeholder：“已保存；留空=不修改”。
- 温度 `#llmTemp`（0–1，步长 0.1，默认 0.2）、超时 `#llmTimeout`（5–120 秒，默认 30）。
- 按钮：`#llmSave` 保存、`#llmTest` 测试连接、`#llmClearKey` 清除 Key。
- 状态：`#llmStatus` 显示“未配置 / 已配置 / 连接正常 812 ms / 连接失败：401”。
- 文案必须说明：**Key 用 Windows DPAPI 封存；只在本机；路线坐标不会发给模型，只发候选统计。**
- 所有控件禁用态、加载态、失败 toast 要完整；不能因为 LLM 未配置阻塞页面。

### 11.8 Agent 使用规则

- 有 LLM：把 `build_choice_prompt` 的结果发给模型，解析 `template_id`；失败则回退到确定性 top 1。
- 无 LLM：直接 top 1，不提示“AI 未配置”。
- 选路调用固定 `temperature=0`，让同一 prompt 的决策可复现。
- 本地缓存 `data/agent_llm_choices.json`：键是 model + base_url + messages 的 SHA-256，值是 `template_id`；默认 30 天、最多 200 条。缓存不写 prompt 原文、坐标、uid/unid 或 key。
- 缓存命中时仍要校验 `template_id` 在本次候选列表中；不在则视为 miss。
- 模型选中的候选若比本地第 1 名低超过 0.15 分，回退到第 1 名并记录原因，避免“模型选了明显更差的路线”。
- LLM 解释只作为 `reasons` 的补充，不得覆盖本地计算出的硬指标（围栏、点位、距离）。
- 每次选择写 `data/agent_runs/<timestamp>.jsonl`：记录 `template_id`、模型名、延迟、prompt 的 sha256；**不记录 prompt 原文**。

---

## 12. P0 验证结论与降级策略

### 12.1 已尝试的真实数据验证（2026-11-12）

- 备份账号 `15663170 / 47203` 的会话仍在，但只读请求返回：
  `api/v70230/runnings/records: error=Unauthorized token有误或缓存中token不存在`。
- 本机没有 GPX/FIT/TCX；备份里只有打卡点缓存、围栏、提交回执，没有轨迹。
- 结论：**当前无法用真实记录做 P0**；不能因此阻塞实现，也不能假装验证过。

### 12.2 替代验证（必须做）

1. **合成基准（离线，可重复）**
   `tools/route_memory_spike.py --fixture synthetic`
   - 使用真实校园中心、`points_cache.json` 里的真实打卡点、`campus_fence_47203.json` 围栏。
   - 生成 4 组路线族：操场闭环（8 条，正反跑、2–3 圈、GPS 噪声 σ≈8 m、起点抖动 80 m）、折返（4 条）、8 字（3 条）、400 m 标准跑道（3 条，4–6 圈）。
   - 混入 6 条脏数据：工具生成、`isCheat=1`、`complete=false`、点数不足、距离越界、飞点。
   - 断言：4 个模板；400 m 跑道模板圈长 380–430 m（含 GPS 噪声）；方向不变；2.5/3.0 km 同模板；top1 点位 ≥ 4/5，snap 后 5/5；围栏 100%；medoid 是真实轨迹。
2. **真实数据复验（等用户重新登录）**
   `tools/route_memory_spike.py --account <accounts_emu.json> --limit 20`
   - 只读；不打印 token/坐标；输出聚类报告到 `docs/plans/route-memory/P0-REPORT.md`。
   - 若 token 仍失效，明确写“未验证”，不得编造。
3. **产品内运行时门槛（真实数据的第一道闸）**
   - `learn` 结束后计算：可用记录数、模板数、每簇样本数、`medoid_distance_m`。
   - 任一不满足 → `status='weak'`，UI 显示“记录还不够，先不提供学习路线”，并保留自动/手绘。
   - 门槛：可用记录 ≥ 5；模板 ≥ 1；单模板样本 ≥ 3（1–2 条只能标记“低置信”，默认不推荐）；`medoid_distance_m ≤ 60 m`。
4. **单路线降级**
   - 只有 1 个稳定模板时，允许显示，但标注“只学到 1 条路线”，不假装有多个选择。
5. **失败回退**
   - 学习失败、0 模板、token 失效、OBS 404 → 都回退到现有自动/手绘，并显示具体原因。

### 12.3 P0 通过标准

- 合成基准全部断言通过。
- 聚类结果不依赖随机种子；同一输入重复运行模板 id 一致。
- 单条 50 条记录的学习耗时（mock 网络）< 2 s；纯几何部分 < 300 ms。
- 若真实数据复验后来可用，再补一份真实报告；在此之前文档明确标注“合成基准通过，真实数据待复验”。
