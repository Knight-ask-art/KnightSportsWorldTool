# 06 · 前端视觉与动效参考

> 这份清单只服务于 **UI / 视觉 / 动效**，和协议参考
> [`reference-projects.md`](reference-projects.md) 分开记。
> 它们不是依赖，运行时不联网、不挂 CDN；要不要引入、引入哪一段，按下面的
> 工程约束逐项判断。

## 1. 清单

| 编号 | 资源 | 地址 | 能拿来做什么 | 对「好想跑」怎么用 |
|---|---|---|---|---|
| 01 | **Anime.js** | <https://animejs.com/> · <https://github.com/juliangarnier/anime> | 轻量 JS 动画引擎，路径动画、时间线、滚动动效、数字滚动 | 只在确实需要时间线/路径动画时考虑；必须把单文件 vendor 进 `app/frontend/vendor/`，不引 CDN |
| 02 | **Motion Sites** | <https://motionsites.ai/> | AI 网页灵感合集，热门案例、section 结构和提示词 | 看首屏节奏、分屏方式、滚动叙事；只借结构，不照抄品牌和文案 |
| 03 | **Showreel Design** | <https://showreel.design/> | 全球设计工作室作品合集，快速提取视觉风格 | 提取配色、字体气质、留白比例，落成 CSS 变量和设计 token |
| 04 | **React Bits** | <https://reactbits.dev/> | 高质量 React 动效组件，可预览后复制源码 | 项目不是 React，**不能直接安装**；只参考效果，用原生 CSS/JS 重写 |
| 05 | **Aceternity UI** | <https://ui.aceternity.com/> | 高级 UI 模块：首屏、卡片、视差、背景光效 | 同样只参考交互结构；不引入 React/Tailwind 运行时 |
| 06 | **Uiverse** | <https://uiverse.io/> | 大量免费 UI 小组件：按钮、输入框、加载动画、开关 | 最贴近现有技术栈，HTML/CSS 片段可以改用；逐个核对作者与许可 |

## 2. 用在哪些页面

| 页面 | 适合借什么 | 不适合做什么 |
|---|---|---|
| 启动页 / 登录页 | 纸墨质感、克制的入场、印章式强调 | 大面积 3D、视频背景、持续粒子 |
| 工作台（总览 / 路线 / 节奏） | 数字滚动、进度条、卡片层级、状态过渡 | 为了动效牺牲可读性；遮挡表单 |
| 记录详情 | 地图与海拔图的入场、数值切换 | 地图上叠加炫光或自动旋转 |
| 公告 / 新手引导 | 轻量淡入、步骤切换 | 与免责声明/引导叠层；自动轮播 |

现有视觉基线：纸感底色、墨色正文、印章红强调，字体和纹理都走「手账 / 档案」
路线。参考项目只用来补细节，不整体换风格。

## 3. 工程约束

1. **技术栈是原生 JS + CSS**，没有构建步骤。React / Vue / Tailwind 组件不能
   直接落地，只能拆成 HTML 结构 + CSS + 少量原生 JS。
2. **运行时不联网**。任何库都要放到 `app/frontend/vendor/` 并随包分发，同时
   保留 LICENSE；不要在 `index.html` 里引 CDN。
3. **尊重 `prefers-reduced-motion`**。项目已经支持「减少动态效果」，新动效必须
   走同一开关，不能绕过。
4. **低配 Windows 优先**。避免 WebGL、常驻 `requestAnimationFrame`、大图序列帧；
   动画只改 `transform` / `opacity`，不动布局属性。
5. **许可**。Anime.js 是 MIT；Uiverse 每个组件由作者各自授权；React Bits /
   Aceternity UI 的代码和模板许可不同。复制前记录来源和许可，付费模板不整段搬。
6. **不碰协议**。这些参考只影响展示层，不改提交体、签名、设备指纹或服务端交互。

## 4. 采用记录

真正用到某一段时，在这里补一行，方便以后回查来源和许可：

| 日期 | 来源 | 用在 | 改动文件 | 许可 / 备注 |
|---|---|---|---|---|
| 2026-11-11 | 编辑杂志设计系统（本项目自定） | paper 皮肤整体 | `app/frontend/css/editorial.css` | 暖米纸 `#f9f8f6`、墨黑 `#1c1c1c`、石墨 `#6b7280`、陈玫 `#816d70`；无圆角/无阴影，标题衬线、正文无衬线 |
| 2026-11-11 | Uiverse `cssbuttons-io/brown-otter-21` | 主按钮（`.btn.primary`、登录按钮） | `editorial.css` + `app.js` 的 `decoratePrimaryButtons()` | MIT；黑框直角、左侧短线、上下 key；hover 反色且 key 收起 |
| 2026-11-11 | Uiverse `Cornerstone-04/bitter-impala-54` | 收起后的登录按钮 | `editorial.css` 的 `.login-card.collapsed .login-tape` | MIT；两段边框从两侧滑入并长成完整框 |
| 2026-11-11 | Uiverse `alexruix/tame-fly-42` | 登录/解极验等待动画 | `app.css` 的 `.login-loader` | MIT；弹跳球踩阶梯，球改为墨黑 `var(--ink)`，阶梯用 `var(--rule)` |

## 5. 什么时候看

- 新增页面、改首屏、改弹窗动效之前；
- 做视觉翻新或皮肤时（现有 4 套皮肤见 `decisions.md`）；
- 用户反馈「动画卡 / 看着乱」时，先对照第 3 节排查。

这份清单没有自动检查脚本；协议参考的自动检查仍然只跑
`python scripts\check_references.py`。
