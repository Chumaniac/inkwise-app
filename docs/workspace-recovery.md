# 停服工作区备份与新目录恢复

管理员在本机使用三个运维命令，不向教师网页暴露整校备份接口。仅支持已停止、没有非空WAL/journal的本地SQLite；不停止服务、不初始化原数据库、不恢复到唯一原件，也不启动评分/OCR/Worker。

先正常退出应用和所有写入者。进入包含私人工作区、规则、参考包和备份目标的私有父目录；配置中的绝对路径及所有输入输出必须在该操作上下文内。示例使用 `setup-local` 生成的绝对路径配置：

```sh
cd /absolute/private-parent
inkwise --env-file /absolute/private-parent/workspace/local.env \
  workspace-backup --output new-backup --stopped
inkwise workspace-check --backup new-backup
inkwise workspace-restore --backup new-backup --output new-recovery
inkwise --env-file /absolute/private-parent/new-recovery/restore.env upgrade-check
```

生成的应用资料在 `new-recovery/workspace/`，独立恢复配置是 `restore.env`。先核对兼容检查、作业版本、原稿和规则；必要的显式升级仍按原 `upgrade-check` / `init-db --backup-file` 流程，不能因复制完成跳过兼容门禁。

## 覆盖与保全

备份包括完整 `data_dir`（数据库若位于其他路径则另行绑定）、当前本地规则目录、参考包，以及存在的本地校准资料。清单绑定相对路径、大小、权限和SHA-256，布局说明也纳入哈希；数据库保留原字节与tenant/classroom、版本、原文哈希、回执和原稿关联，不重算结果。复制结束前重新检查所有源组件，变化时保留不完整新副本并拒绝成功。

这不是外部PostgreSQL、Neo4j/Chroma、DVC远端、模型权重或供应商账单的恢复证明。位于 `data_dir` 内的私人配置和访问文件会随资料一同保留，但不会在日志或命令结果中打印；备份需像原件一样保密。外部依赖不自动下载、不自动连接。软件和模型版本仍需用原安装/制品核对。

## 恢复默认关闭外部副作用

恢复只写新目录。新 `restore.env` 重定向本地数据库、资料、规则、参考包和校准路径，默认关闭云模型预算、模型/嵌入端点、Kafka和自动演进。原私人配置仅作为资料保留，不自动执行或覆盖新配置；由管理员核对模型、盐、撤销状态和部署设置。不要把恢复点当作现在的授权状态，也不要不经原回执核对就继续未知的模型任务。

只有文件、布局、数据库兼容检查和新配置都成功后才写 `COMPLETE`。失败或中断保留 `INCOMPLETE`；检查拒绝把它当成功备份。不要删除唯一原件或覆盖旧成功备份，不要自动重试原评分/上传。

## 限额与私密性

10,000项、64层、单文件2GiB、总文件8GiB、清单4MiB、1MiB复制缓冲，开始前保留至少64MiB额外空间。文件树中的链接、特殊文件、越界、重复路径和资源超限拒绝；现存输出目录不覆盖。已停止是操作者确认，运行标记和变化检测不能证明所有进程停写。

这是未加密的本机副本：放在系统加密备份或独立私有加密介质，禁止提交Git/Issue或公开同步。POSIX输出0700/0600；Windows需要私有ACL。校验说明复制内容一致，不证明备份作者可信，不保证介质永不损坏；真实学校数据与完整生产恢复需另行授权和验收。
