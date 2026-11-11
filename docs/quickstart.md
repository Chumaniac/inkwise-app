# 从私有仓库开始本机使用

交付范围是GitHub私有仓库及可复现的本机使用，不要求租赁或部署服务器。既有公开页面保持合成演示。本机操作以下均从仓库根目录执行，支持Python 3.12+；推荐macOS或Linux，向量扩展和文件锁尚未验证Windows原生环境。

## 1. 安装固定依赖

```sh
git clone git@github.com:Chumaniac/inkwise-k12.git
cd inkwise-k12
uv sync --frozen --extra dev --extra postgres --extra retrieval --extra kafka --extra language --extra data
uv run inkwise teaching-check
```

只看展示时，可直接打开既有公开演示，或在 `demo/dist` 启动本地静态服务。真实后台用下方流程。

## 2. 生成独立练习环境

```sh
uv run inkwise setup-local
```

自动创建 `data/private/local-demo`：SQLite数据库、13套草案、独立教师/教研/管理员访问文件、`.env` 与无密钥身份清单 `setup.json`。配置及访问文件权限为0600，目录为0700；该目录被Git忽略。已有目录会被拒绝覆盖。需要另一个练习环境时用 `--directory data/private/另一个名称`，并在后续命令中使用对应 `.env`。

此步骤不启动服务、不下载模型、不消费API、不把候选词库发布为学校资料。初始化中的 `local-demo` 是本机练习租户，班级为 `practice`。

## 3. 准备本机模型

安装并启动Ollama后：

```sh
ollama pull deepseek-r1:7b
ollama pull bge-m3
ollama create inkwise-deepseek-local -f deploy/Modelfile.deepseek-local
uv run inkwise --env-file data/private/local-demo/.env doctor --connect
```

已有模型和成功结果直接复用。网络下载与本机运行有时间、磁盘及内存成本；不会调用收费云API。`doctor --connect` 只查询模型目录，不生成文本，不把配置或凭据打印出来。

已安装工作区不要重复拉取或覆盖模型；本机新评分、教师复核和导出的实际配置、校验值及边界见[本机验收记录](local-model-acceptance.md)。

7B蒸馏模型是本地起点，长上下文会增加内存需求。默认单并发、每任务12次调用、600秒任务上限，适合先测试短文。完整15000字符任务要启用Kafka分块，并按实际块数提高调用预算；不是调整一个超时参数即可保证性能。1.5秒仅为受理目标。

## 4. 可选：macOS原生OCR

```sh
mkdir -p artifacts
xcrun swiftc native/vision-ocr.swift -o artifacts/inkwise-vision
```

在私有 `data/private/local-demo/.env` 中将 `INKWISE_OCR_BACKEND` 改为 `vision`。默认路径为 `artifacts/inkwise-vision`；所有命令从仓库根目录执行。Linux可配置现有HTTP OCR接口，契约见[运行手册](operations.md)。不配置OCR时仍可输入或粘贴文本，界面会明确报OCR未配置；不返回假识别结果。

## 5. 一个命令启动和停止

```sh
uv run inkwise --env-file data/private/local-demo/.env local
```

打开 `http://127.0.0.1:8766/workspace/`，从本机私有文件读取 `teacher-access.txt` 并输入工作台。不要把访问文件提交Git或粘贴到聊天。创建作业选择班级 `practice`、匹配的年级/语言/体裁/量规。可使用 [原创练习](../teaching/examples.json) 的题目和正文。

流程：建立作业 → 可选上传图片并OCR → 核对正文 → 提交批改 → 查看引用与争议 → 教师逐维修改 → 确认终稿/打印。公开教学资料尚未成为学校批准量规，因此仍需教师判断。

`Ctrl+C` 同时停止API与worker，保留数据库及成功结果；下次运行同一命令恢复。端口占用时启动失败，避免误连另一个服务，可用 `local --port 8877`。

也可分别使用 `serve`、`worker` 命令。在批改进行中停止会保留任务状态；未知模型请求必须按原有回执核查流程处理，不能靠删除缓存重试。

## 6. 启用可选完整检索

最小模式使用经过hash验证的本地量规；界面报告会标记外部参考未取回并要求教师复核。它不假称已命中双路RAG。

要运行真实Neo4j/ChromaDB/PostgreSQL服务，仓库保留了[容器配置](../deploy/compose.yml)及[完整操作手册](operations.md)。本轮不启动新的服务器或公网端口。配置真实服务地址及授权知识包后，按 `knowledge-export → knowledge-index → 评估签核 → knowledge-activate` 流程建立索引；不能将本包原创例文静默变成评分金标准。

6000词候选索引可独立构建：

```sh
uv run python scripts/build_candidate_index.py
```

它按批次缓存并复用成功向量，输出候选专用sqlite-vec索引和回执；不改运行时词库指针。正式词库审批、增量重建与知识版本回滚见[教学资料说明](../teaching/README.md)。

## 常见问题

|现象|处理|
|---|---|
|`LOCAL_WORKSPACE_ALREADY_EXISTS`|保留现有数据，直接使用该目录的 `.env`；另建环境须给新目录|
|`LOCAL_PORT_UNAVAILABLE`|检查本机已有服务或选择另一个端口，不自动停止其他服务|
|模型目录检查失败|启动Ollama并确认已创建 `inkwise-deepseek-local`；检查端口与模型名|
|OCR未配置|使用文本输入，或完成原生编译/配置HTTP服务|
|双路检索降级提示|核对Neo4j、ChromaDB、嵌入模型与已审核知识包；本地草案仍可用于人工练习|
|`LEXICON_CANDIDATE_NOT_APPROVED`|完成真实词条审核后导入独立批准版本，不将候选状态当完成签核|
|长文预算不足|按真实分块数量与全篇评分预算配置，并启动chunk-worker；不要无限重试|
|首次模型评分结构失败或超时|保留成功回执；查看工作台错误码，按恢复流程处理|

停止本机服务不会删除数据。需要清除作业时使用有权限的擦除接口，并按原有流程清理已导出的版本与备份。

0.4的OCR、参考库、校准、自演进和混合路由配置见[完整功能手册](complete-workflows.md)。模型、OCR权重与公开评测数据均不随Git仓库提交。
