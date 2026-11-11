# 第三方资料与运行时

本仓库自身代码按根目录 LICENSE 授权；第三方资料保留各自许可，不能由根许可证覆盖。

## 独立用户版的运行包

源代码仓库本身不包含依赖可执行文件或用户数据。构建器在组装实际运行包时附带相关许可：

- CPython 3.12.9 与所链接组件的上游许可和元数据保留在 desktop/runtime-licenses/python，组装时复制进运行包。Python 依赖按锁文件安装，保留各自 dist-info 中的许可证和元数据。

- teaching/third_party/codespell 派生词表及其元数据仍为 CC BY-SA 3.0；web/assets/fonts 字体保留各自 OFL。根 MIT 不替代这些许可。

模型、数据集、Intiface 应用不在运行包中。借鉴来源和未复制的实现见 docs/references.md。

默认新工作区模型 Qwen2.5-7B-Instruct 的官方权重按 Apache-2.0 提供，准备工具只在本机按固定哈希下载，代码/桌面包不包含权重。原 DeepSeek 配置与旧模型保留。见 docs/local-model-setup.md。
