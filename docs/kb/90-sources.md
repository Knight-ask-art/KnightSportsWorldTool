# 90 · 资料与证据

> 最后更新：2026-11-11
> 访问日期均为 **2026-11-11**（外部链接可能变动，引用前先重开确认）

## 1. 本地权威来源（优先）

| 文档 | 内容 | 等级 |
|---|---|---|
| [`../api.md`](../api.md) | 端点、提交体、OBS、轨迹字段、错误码 | A |
| [`../geetest.md`](../geetest.md) | 极验 v4 结构、失效分类、canary、硬编码清单 | A |
| [`../operations.md`](../operations.md) | 本地文件、闸门、回执、排错、只读对账 | A |
| [`../packaging.md`](../packaging.md) | Nuitka、NSIS、源码泄漏门禁 | A |
| [`../update.md`](../update.md) | latest.json 结构、URL 约定、更新器流程 | A |
| [`../decisions.md`](../decisions.md) | 关键决策、证据、未验证清单 | A |
| [`../workflow.md`](../workflow.md) | 测试/云测试/发布 SOP、当前基线 | A |
| [`../reference-projects.md`](../reference-projects.md) | Inverx / Demacia 跟踪清单 | B |
| [`../design-references.md`](../design-references.md) | 前端视觉/动效参考 | C |
| [`../../RELEASE_NOTES_0.2.2.md`](../../RELEASE_NOTES_0.2.2.md) | v0.2.2 变更与证据边界 | A |
| `app/tests/` | 回归测试，结论可复现 | A |

源码本身永远优先于文档；文档和源码冲突时，以源码 + 测试为准，并回头修文档。

## 2. 上游参考项目

| 项目 | 地址 | 用途 | 许可 |
|---|---|---|---|
| Inverx | <https://github.com/Inverx/com.zjwh.android_wh_physicalfitness> | Python 协议实现、NetSecKit、极验、OBS、签名 | MIT |
| Demacia | <https://github.com/nekohy/DemaciaSportsWorldTool> | 轨迹真实性、19 项自检、海拔结论 | CC BY-NC 4.0，**只读结论，不复制代码** |

跟踪方式和基线见 [`../reference-projects.md`](../reference-projects.md)；
自动检查 `python scripts\check_references.py`。

## 3. 官方/商店信息（C 级）

| 来源 | 能证明什么 | 链接 |
|---|---|---|
| 小米应用商店 | 运动世界 7.4.0，更新时间 2026-10-29 | <https://app.xiaomi.com/details?id=com.zjwh.android_wh_physicalfitness> |
| Apple App Store | 运动世界校园版存在，版本需页面确认 | <https://apps.apple.com/us/app/id1099174257> |
| 应用下载站（ddooo） | 第三方页面写 7.4.01（2026-11-06），**未在官方商店确认** | <https://www.ddooo.com/softdown/247591.htm> |

> 商店页只用于“当前大概是什么版本”，不能证明接口结构。接口仍以抓包/dex 为准。

## 4. 学校规则与社区信息（C 级，因校而异）

| 来源 | 说法 | 怎么用 |
|---|---|---|
| 湖北职业技术学院使用说明 | 系统检测步频，波动异常成绩无效 | 佐证“步频是平台检查项” |
| 安徽某校 2019 使用指南 | 配速 2'11"–10'/km、必须经过绿色/蓝色点 | 只能当历史口径；规则会变 |
| 知乎《从软件“运动世界校园”的分析出发…》 | 管理后台可看配速、步频、跑步模式、作弊原因/详情 | 佐证“服务端有回溯判定” |
| 36氪《校园代跑，跑出一条隐秘的产业链》 | 不同设备数据会自然错开、平台会看数据重合 | 佐证“设备一致性/数据分布”重要 |
| 百度聚合页（2026） | 配速 3–10 分/km、步频 8–40 不通过 | 与其它来源冲突，只能当 C |
| 旧版应用说明（goapk） | 步频合理区间 60–300 | 与上一条冲突，说明“平台常量”说法不可靠 |

链接：

- <https://gt.hbzy.edu.cn/info/1045/1607.htm>
- <https://www.ahdy.edu.cn/tyjlb/2019/1109/c1331a25761/page.htm>
- <https://zhuanlan.zhihu.com/p/50415843>
- <https://m.36kr.com/p/3441097236092288>
- <https://kuku.baidu.com/landing/tscp_doc/bffb01efeef4e61310257cf7b5f9ded3>
- <https://www.goapk.com/anzhuo/18506.html>

**结论**：学校规则必须以服务器 `runRuleModel` 为准；社区数字只做提示文案，
不能写死成平台规则。

## 5. 技术文档（B 级）

| 来源 | 能证明什么 | 链接 |
|---|---|---|
| Nuitka Common Issue Solutions | Windows 二进制被杀软误报是常见现象 | <https://nuitka.net/user-documentation/common-issue-solutions.html> |
| Nuitka User Manual | standalone / onefile 的差异和限制 | <https://nuitka.net/user-documentation/user-manual.html> |
| GeeTest v4 FAQ | 极验如何识别协议破解/模拟器 | <https://docs.geetest.com/gt4/faq/other> |
| GeeTest Glossary | `w`、二次校验、验证参数的生命周期 | <https://docs.geetest.com/gt4/help/glossary> |
| GeeTest 官网 | 风控产品定位 | <https://www.geetest.com/> |
| Uiverse `cssbuttons-io/brown-otter-21` | 主按钮的黑框/短线/key 交互，MIT | <https://uiverse.io/cssbuttons-io/brown-otter-21> |

## 6. 历史/社区实现（C 级，不要照抄）

| 来源 | 说明 |
|---|---|
| Desgard 2016《玩转运动世界校园》 <https://www.desgard.com/python/2016/11/02/Running-Or-Safe.html> | 早期接口破解思路；协议早已变化，只作历史 |
| alwayslone/Sports-World-Campus <https://github.com/alwayslone/Sports-World-Campus> | 社区自动化实践；不复制代码，只看问题清单 |
| CSDN 极验 v4 逆向文章 <https://blog.csdn.net/weixin_30591519/article/details/156308525> | 第三方分析，结论需用官方文档/本地实测复核 |

## 7. 引用规则

1. 协议/提交链结论优先用 A 级；没有 A 就用 B，并标注“参考实现”。
2. C 级只能写“社区口径/学校通知”，不能写成平台事实。
3. D 级必须写进「未验证」，不能出现在发布说明的确定语气里。
4. 外部链接加访问日期；版本号变化时重新核对。
5. 任何来源都不复制代码到 `app/`，除非许可明确且保留版权声明。
