# 40 · 前端与公告

> 证据等级：**A**（源码 / 浏览器实测）
> 最后更新：2026-11-12 · 对应版本 **0.2.2**

## 1. 结构

```text
app/frontend/
├─ index.html          单页骨架 + 七页 nav
├─ css/
│  ├─ app.css          主样式、布局、组件
│  ├─ skins.css        五套皮肤（data-skin）
│  ├─ editorial.css    paper=编辑杂志风的全部覆盖（最后加载）
│  ├─ fonts.css        本地字体，不发网络请求
│  └─ splash.css       启动页
├─ js/
│  ├─ app.js           业务逻辑、页面渲染、bridge 调用
│  ├─ announcements.js 公告通用渲染
│  ├─ charts.js        图表
│  └─ splash.js        启动动画
├─ vendor/             leaflet 等本地依赖
├─ assets/             图片（含 QQ 群二维码）
└─ fonts/              本地字体文件
```

- **无构建步骤**：没有 npm、没有打包器、没有框架。
- **运行时不联网**：字体、地图库、图片全部本地；只有 bridge 调本机 Python。
- **地图水印**：`#map` / `#routeMap` / `#rdMap` 上叠加 `.map-watermark`，
  文本取 `LIVE.session.username`（登录手机号），无会话时隐藏；z-index 250
  位于瓦片之上、路线/标记之下，`pointer-events:none` 不挡拖动和缩放。
- 主题在首屏前由 `/theme-boot.js` 注入（服务端从 `data/ui_state.json` 生成），
  避免先闪一下纸感皮肤。

## 2. 七个页面

| `data-page` | 页面 | 主要做什么 |
|---|---|---|
| `overview` | 总览 | 学期进度、最近记录、快捷入口 |
| `accounts` | 账号 | 登录态、校区、保活、设备指纹 |
| `run` | 跑步 | 点位、路线、节奏、提交 |
| `routes` | 路线 | 路线库、GPX、记录生成 |
| `records` | 记录 | 列表、详情、轨迹、只读对账 |
| `profile` | 我的 | 个人主页、违规自查 |
| `settings` | 设置 | 学校规则、外观、公告、关于、更新 |

## 3. UI ↔ Python 桥

- 统一走 `POST /bridge/<method>`，实现在
  `app/replica/adapters/bridge_api.py`。
- 前端 `api(method, params)` 封装超时和错误；返回统一 `{code, msg, data}`。
- 敏感数据不落 localStorage；只有皮肤偏好 `hxp_skin` 存在 localStorage，
  其它状态（公告已读、引导、免责声明）都在 `data/ui_state.json`。

## 4. 公告模块

**目标**：换文案、换二维码、加第二条公告只改一个 catalog，不碰 `index.html`
和 `app.js`。

| 部分 | 文件 | 职责 |
|---|---|---|
| 数据 | `app/replica/application/announcements.py` | `ANNOUNCEMENTS` 元组、`current()`、`by_id()` |
| 桥 | `bridge_api.py::real_get_announcements` | 原样透传，不丢字段 |
| 渲染 | `app/frontend/js/announcements.js` | 弹窗、图片、暗号、已读 |
| 入口 | 设置 → 关于「加入 QQ 群」 | `data-announcement-id` 打开指定公告 |
| 已读 | `data/ui_state.json` 的 `announcement_seen` | `{id: version}` |

### 字段

必填：`id`、`version`、`title`。
可选：`kicker`、`body`、`note`、`image`、`image_alt`、`meta`、`copy`、
`copy_label`、`dismiss`、`ok`、`enabled`。

当前 QQ 群公告：

- `id`: `qq-group-2026-11`
- `version`: **2**（改内容必须 +1，已读用户才会重新看到）
- `note`: `加群暗号：皮卡丘`
- `image`: `assets/qq-group.jpg`
- 正文**不写群号**，让用户直接扫码；图片右上角自带的群号是图片内容

### 加一条公告

1. 在 `ANNOUNCEMENTS` 追加 dict，`id` 唯一；
2. 图片放 `app/frontend/assets/`，catalog 里写 `assets/<file>`；
3. 需要固定入口就在 `index.html` 加 `data-announcement-id` 按钮；
4. 更新 `docs/kb/40-frontend.md` 和测试；
5. 跑 `pytest tests/test_v017_security.py tests/test_frontend_login.py`。

### 约束

- 文案用 `textContent`，不插远端 HTML。
- 图片只接受 `assets/<file>`，拒绝外部 URL。
- 公告不能和新手引导、免责声明叠层；`app.js` 有顺序控制。
- 已读按 `(id, version)` 比较：`seen < version` 才弹。

### 设备指纹面板

设置页的「设备指纹」区块统一管理设备，不再放在账号页：

- 只读：设备、脱敏 DeviceId、平台、屏幕、缩放、来源、环境自检、UA。
- **高级功能：更换设备指纹**：随机 / Android / iOS，输入“更换”确认。
- **高级功能：自定义设备**：平台可选 Android / iOS；机型、系统、物理/逻辑分辨率、
  CPU 都是文本输入，`real_get_device_options` 只提供默认像素/CPU 建议。

自定义设备写入 `source=custom`，保存前先校验（竖屏、比例、iOS 2x/3x、CPU），
校验通过才退出登录；旧指纹备份，保存后必须重新登录。前端不复制机型常量。

## 5. 皮肤与动效

五套皮肤：`paper`（**编辑杂志**，默认）、`manuscript`（**手稿笔记**，保留原来的
楷体 + 荧光笔 + 手绘边框方向）、`poster`（赛场海报）、`minimal`（极简白）、
`archive`（复古档案）。

- 皮肤只改 CSS 变量和少量纹理，不改 DOM 结构。
- 选择存 localStorage，同时写 `data/ui_state.json`（服务端生成首屏样式）。
- 字体全部本地打包，离线可用。

### 编辑杂志风（paper）

`css/editorial.css` 在 `skins.css` 之后加载，只作用于 `html[data-skin="paper"]`：

- 暖米纸 `#f9f8f6`、墨黑 `#1c1c1c`、石墨 `#6b7280`、陈玫 `#816d70`（只用于选中/引文）。
- 标题衬线，正文/按钮无衬线；8px 基准、发丝线、无圆角、无阴影。
- 工作台主按钮：细线描边、hover 反色，保持安静；只有跑步面板的 `#submitRun` 用 `doniaskima/btn-23` 的黑底滚动文字。
- 登录页的登录按钮参考 Uiverse `brown-otter-21`：黑框、左侧短线、上下 key；hover 反色并收起 key。这套样式只给 `.login-submit`，不再用于工作台主按钮。
- 量化线场：品牌标记采样成 1–4px 四档竖线（`line-field.svg`），只放在总览标题右侧和「我的」头像区；缓慢漂移，减少动效时停止。
- 数字统一 `lining-nums tabular-nums`（避免 Georgia 旧式数字把 0 读成 o）；中文标签字距收回到 `.04em`。
- 收起后的登录按钮参考 `Cornerstone-04/bitter-impala-54`：两段边框滑入并长成完整框。
- 登录/解极验等待用 `alexruix/tame-fly-42` 的弹跳球 + 阶梯，球改为墨黑，阶梯用墨色半透明；等待时登录面板只显示该动画。
- 覆盖页面：概览、账号、跑步、路线库、设置、登录面板；开屏动画不改。
- 所有新动效在 `prefers-reduced-motion` 下关闭。

动效约束（详见 [`../design-references.md`](../design-references.md)）：

1. 尊重 `prefers-reduced-motion`；
2. 只动 `transform` / `opacity`，不动布局属性；
3. 不用 WebGL、常驻 `requestAnimationFrame`、大图序列帧；
4. 不引 CDN；需要库就 vendor 到 `app/frontend/vendor/` 并保留 LICENSE。

## 6. 文件导出与下载

- CSV（记录）、FIT、GPX、诊断包都由前端生成 Blob，再用 `<a download>` 交给浏览器/桌面壳保存。
- pywebview 默认 `ALLOW_DOWNLOADS = False`，会静默取消所有下载；`app/app.py` 必须在 `webview.start()` 前打开它，否则按钮看起来“没反应”。
- CSV 导出会额外请求 `real_recent_records(limit=200)`，覆盖比表格 20 条更多的本人记录；请求失败时退回当前表格数据。

## 7. 设计参考怎么用

[`../design-references.md`](../design-references.md) 里的 6 个站点只做参考：

- **Uiverse** 最贴近原生 HTML/CSS，可以直接改用组件；
- **Anime.js** 只在确实需要时间线/路径动画时 vendor；
- **React Bits / Aceternity UI** 是 React/Tailwind，不能直接装，只能参考效果；
- **Motion Sites / Showreel Design** 只借版式和风格，不抄品牌。

## 8. 测试

| 测试 | 覆盖 |
|---|---|
| `tests/test_frontend_login.py` | 登录门禁、静态资源外链、公告 catalog/暗号/群号 |
| `tests/test_v017_security.py` | `ui_state.announcement_seen`、公告桥 |
| `tests/test_theme_and_windows.py` | 皮肤首屏、窗口行为 |
| 浏览器实测 | 公告弹窗、已读落盘、不叠层 |
