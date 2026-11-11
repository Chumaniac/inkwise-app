# 墨评浅色设计系统 · 0.5.0

本次保留墨评的布局与功能，只统一视觉、组件状态和可访问性。教师工作台、仓库内合成演示及其文档页采用同一套浅色令牌。没有部署服务器，也没有发布在线演示更新。

## 参考与边界

参考 Genkoy `codex/full-remote-integration-20261009` 工作区的 `apps/web/src/app/globals.css`、共享按钮及页面布局，核对时提交为 `5b65639df`，该样式文件无未提交变更；文件 SHA-256 为 `caabcfd6a486d2fd97bae8da5701803deb284a7e3ccf305a0d8e06f013d0fa82`。复制的是获授权的视觉规范与公开字体资产，没有迁移 Genkoy 业务模块或修改其代码。

工作台仍为顶部身份区、左侧作业列表、右侧内容区；报告保留原稿/确认文字与评分/教师复核双栏。既有1100px、700px响应式边界不变。演示仍为三层级入口中的三个原有视图，保留212/178/72px侧栏及550px手机顶部导航模式；双栏工作区、知识库与方案内容均未删除。原工作台100个DOM ID和演示27个DOM ID全部保留，新增的主内容与弹窗标题ID只用于可访问性。

## 唯一令牌源

`web/theme.css` 是唯一手工维护的令牌文件，`web/workspace.css` 与 `demo/dist/styles.css` 分别描述原有页面布局。演示目录可独立托管，所以其主题、图标与字体是生成副本：

```sh
uv run python scripts/sync_demo_theme.py
uv run python scripts/sync_demo_theme.py --check
```

CI运行只读同步检查，拒绝两套主题漂移。不要直接修改 `demo/dist/theme.css` 或其中的生成资产，不另建暗色变量覆盖当前主题。

|用途|令牌与值|
|---|---|
|应用背景|`--surface-app: #f8f9fa`|
|卡片/弹窗|`--surface-card: #ffffff`|
|弱强调|`--surface-muted: #f4f4f5`|
|正文/辅助字|`--text-primary: #18181b` / `--text-secondary: #62626b`|
|主要操作|`--brand: #111111`；hover `#27272a`；active `#000000`|
|分割线/控件边界|`--border: #e4e4e7` / `--border-strong: #71717a`|
|状态|沿用Genkoy成功、警告、错误、信息语义色，仅用于小面积状态信息|
|基础圆角|6/8/12/16px；主面板12px|
|控件高度|44px；原有正文内链接不强制扩成块|
|间距|4/8/12/16/20/24/32/40px|
|阴影|普通面板 `0 1px 3px rgba(0,0,0,.06)`；浮层 `0 12px 36px rgba(0,0,0,.12)`|

强色只用于文字、图标、小面积主按钮及必要状态。顶栏、侧栏、卡片、表单和通知保持浅色；系统深色偏好不会自动翻转主题。没有加入新的图片、渐变背景或动画依赖。

## 字体与资源

与Genkoy一致使用Inter Variable，中文由系统中文字体回退；代码区使用JetBrains Mono Variable。字体在同源静态目录自托管，使用 `font-display: swap`，只预加载48,256字节的Inter Latin变量字体；40,404字节的JetBrains Mono仅在实际显示代码时按需使用。不下载中文字体、不调用Google Fonts或其他字体服务。两个字体及其来源、版本和SIL OFL许可位于 `web/assets/fonts/`，不改变项目专有源码许可。

主题CSS约4KB，工作台CSS约13KB，演示CSS约21KB（源文件、未压缩）。这不是首屏耗时的测量值；没有用包体或字体大小推断CPU、内存或真实网络体验。布局用CSS完成，没有加入React、Tailwind运行时、测量循环或新的业务依赖。

## 组件与可访问性

按钮区分主次、hover、pressed、disabled、busy和键盘focus；加载指示绝对定位，不改变按钮宽度。输入框边界与装饰性卡片分割线分开，原稿预览预留稳定区域，放大仍显示原图。表单、提示、标签、报告引用与打印报告使用一致灰阶；候选和成功/警告状态始终有文字说明。

新增跳到主内容入口、原稿弹窗可读标题、键盘Tab循环和关闭后焦点返回、可键盘滚动的原图/文档区域、图标按钮名称、当前作业状态语义、表单名称及合适的自动更正设置。`prefers-reduced-motion`关闭过渡和旋转，平滑滚动也尊重该偏好。未增加自动提交、自动评分或新的权限行为。

静态令牌对比度计算：正文/白底17.72:1，辅助字/白底6.04:1，辅助字/弱灰底5.49:1，白字/主按钮18.88:1，输入边界/白底4.83:1，警告文字/浅警告底4.84:1，成功文字/浅成功底5.21:1，错误文字/浅错误底5.91:1。卡片细分割线属于装饰，不拿它充当需要3:1对比的输入边界。

## 预览

[教师工作台](previews/light-workspace.png) · [合成演示](previews/light-demo.png)。截图只含合成数据，展示本机代码，不表示托管站点已更新。

## 验证

- `tests/design_acceptance.cjs`：9个视图，320/390/768/1024/1440px，无页面级横向溢出；200%根字号模拟、系统深色偏好保持浅色、动效减弱、跳转链接、弹窗焦点循环/Escape/返回通过。
- 使用已有axe-core，对上述视图执行WCAG A/AA标签检查，违规项0、待人工确认项0；这仅是自动检查范围，不是完整WCAG认证或真实屏幕阅读器用户研究。
- 工作台原有真实Chrome流程通过，包括分页、候选修订、教师编辑、导出、OCR多页/替换/复用、权限和退出清理。
- 新增演示浏览器回归覆盖三视图导航、键盘快捷键、自定义内容不伪造AI评分、教师确认/导出、仅合成案例回流与撤回。
- 改造前后截图与原始结果保存在忽略的 `artifacts/redesign/`，仅使用合成数据。教师工作台与演示截图逐页视觉复核。

本机运行设计检查需先启动既有 `tests/serve_browser_fixture.py` 和 `demo/dist` 回环静态预览，再提供已安装Playwright和axe的模块位置：

```sh
PLAYWRIGHT_MODULE=<已安装的playwright模块> AXE_SOURCE=<已安装的axe.min.js> node tests/design_acceptance.cjs
PLAYWRIGHT_MODULE=<已安装的playwright模块> node tests/browser_acceptance.cjs
PLAYWRIGHT_MODULE=<已安装的playwright模块> node tests/demo_browser_acceptance.cjs
```

设计检查遵循 [Web Interface Guidelines固定版本](https://github.com/vercel-labs/web-interface-guidelines/blob/e3d624baaf29dc1fc645aff3e38f03e564d2d6b1/command.md) 与 [WCAG 2.2参考](https://www.w3.org/WAI/WCAG22/quickref/)。未来变更先更新令牌，再验证代表页面与原操作路径；发现视觉或功能回归时回退对应样式提交，不更改业务数据或重做成功生成资产。
