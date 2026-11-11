# 墨评部署与运行

本仓库的真实后台与公开静态演示分开。后台支持受控本机/服务器部署；Sites 公开演示不直接承载 Python、Neo4j、Kafka 或模型权重。当前用户选择只交付GitHub仓库，不部署服务器。本机使用优先按[快速开始](quickstart.md)完成初始化与启动，不将本机端口转发至公网。

## 本机准备

```sh
uv sync --frozen --extra postgres --extra retrieval --extra kafka --extra data
uv run inkwise init-db
uv run inkwise seed-demo --tenant my-school
uv run inkwise provision --tenant my-school --classroom class-7a --role teacher --output data/private/teacher-access.txt
uv run inkwise provision --tenant my-school --classroom class-7a --role reviewer --output data/private/reviewer-access.txt
```

访问凭据只写入指定的 0600 文件，不打印、不提交。教研审核者必须与提交候选者是不同身份。`seed-demo` 安装的是合成量规，正式教学前须替换为学校批准的版本；没有方言量规则拒绝自动评分。

正式量规使用 `inkwise import-rubric --tenant ... --file PRIVATE_RULE_JSON --submitted-by USER_ID --approved-by REVIEWER_ID --source "课程文件及页码"` 导入。两位身份必须属于同校且不同，审核者须具有教研/管理员角色；规则文件须显式标为非合成、通过权重及无环依赖校验。此命令记录已在线下取得的学校批准，不代表命令本身获得了教师签核。新版本不覆盖旧版本；再按下文建立对应图谱与案例索引。

默认任务库为 SQLite；配置 `INKWISE_DATABASE_URL` 可使用 PostgreSQL。首次部署显式运行 `init-db`，服务启动不自动迁移。0.2 是第一版数据库 schema；未来升级须提供迁移并先备份，不能把 `create_all` 当升级工具。

## 本地模型

```sh
ollama pull deepseek-r1:7b
ollama pull bge-m3
ollama create inkwise-deepseek-local -f deploy/Modelfile.deepseek-local
```

蒸馏 7B 模型与完整云端 DeepSeek 不等价。`Modelfile` 为长输入声明 65536 上下文；其长文吞吐、显存和评分质量仍需目标机器实测。一次本地短文真实验证使用了原始 `deepseek-r1:7b`，不是该长上下文配置的压测。模型 tag 可能变化，应保存 Ollama `/api/tags` 返回的 digest 并与知识清单一起冻结。

创建项目私有 `.env`（不提交），只在其中填写配置值：

```dotenv
INKWISE_MODEL_BASE_URL=http://127.0.0.1:11434/v1
INKWISE_MODEL_NAME=inkwise-deepseek-local
INKWISE_MODEL_STRUCTURED_OUTPUT=json_schema
INKWISE_MODEL_TIMEOUT_SECONDS=120
INKWISE_MODEL_MAX_OUTPUT_TOKENS=4096
INKWISE_MODEL_CONCURRENCY=1
INKWISE_MODEL_CALLS_PER_JOB=12
INKWISE_JOB_TIMEOUT_SECONDS=600
INKWISE_EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1
INKWISE_EMBEDDING_MODEL=bge-m3
```

模型网关不继承环境 HTTP 代理，避免学生数据被意外转发。云服务应配置明确 HTTPS 地址、模型 ID 与私有密钥，使用该服务实际支持的 JSON 输出模式；不得把本地 `json_schema` 设置照搬至不支持它的供应商。

## OCR

macOS 原生 OCR：

```sh
mkdir -p artifacts
xcrun swiftc native/vision-ocr.swift -o artifacts/inkwise-vision
```

私有配置设 `INKWISE_OCR_BACKEND=vision`。此适配器关闭语言纠正，保留字符框、候选和识别置信；结果始终需教师确认。它已通过合成印刷文字验证，未证明学生手写识别率。

Linux 或独立 OCR 服务设 `INKWISE_OCR_BACKEND=http` 与 `INKWISE_OCR_BASE_URL`。服务需要实现 `POST /recognize`，请求 `imageBase64` 与 `languageCorrection=false`，返回 `engine/revision/lines/requiresReview`；每行包含 `text/confidence/box/alternatives`，box 为左上角坐标系下的归一化 x/y/width/height。PaddleOCR 服务部署仍取决于目标平台和模型版本，当前仓库没有把原生 OCR 冒称为 PaddleOCR 手写验收。

## 启动 API 与任务执行器

分别启动：

```sh
uv run inkwise --env-file .env serve
uv run inkwise --env-file .env worker
```

浏览器访问 `http://127.0.0.1:8766/workspace/`，在本机读取教师访问文件并登录；不要把凭据贴入聊天。默认只监听回环。外网运行需要已授权的服务器、HTTPS、数据库备份、反向代理限流以及学校数据处理流程。

作业过程：创建 → 上传可选原图 → OCR → 核对正文 → 固定量规并入队 → 双路检索 → 五维评分 → 反思/有限修订 → 教师确认。正文或规则变化使用新版本；已批准报告不能直接覆盖正文。

教师可逐维编辑、选择或排除评语与引用，最终打印报告只采用这些已确认内容。旧版审核记录如果没有逐维发布标记，只显示分数和教师总评，不自动提升模型草稿为教师评语。未保存的表单修改会触发离开提示；确认后的原文仍可只读查看。

## 易错词索引与 OCR 联动

准备私有 JSON 文件，结构为 `{"provenance":"授权来源说明","entries":[{"incorrect":"teh","correct":"the","language":"en","explanation":"按原稿核对，不替学生改错"}]}`。来源、词条正确性与使用授权须由学校审核，不能把合成条目数量当真实词库规模。

```sh
uv run inkwise --env-file .env lexicon-build --tenant my-school --source PRIVATE_WORDS_JSON --model-revision ACTUAL_EMBEDDING_REVISION --submitted-by USER_ID --approved-by REVIEWER_ID
```

两位同校身份必须不同，审核者具有教研/管理员角色。设置 `INKWISE_LEXICON_MODEL_REVISION` 为相同嵌入版本后，OCR 会对最多 8 条低置信行检索本校词库，给出至多每行 3 个候选；原始转写不变，仍需教师对照原图。方言任务不套用普通拼写纠正。

新版本从统一源构建，不覆盖旧索引；相同模型下未变词条向量会复用，发布指针有并发检查。初版上限为 10000 对/10 MB 源文件，更大规模需流式构建。已完成 5001 条**合成向量容量**检查和 2 条真实 BGE-M3/OCR 联动检查，现已另行准备6000对固定来源CC BY-SA候选，并用本机BGE-M3建立隔离候选索引；尚未标为学校正式审核词库。见[教学资料包](../teaching/README.md)。

## 数据库与容器

`deploy/compose.integration.yml` **仅用于隔离合成测试**，数据库免密码且端口只绑定回环，禁止拿它作为生产配置。

`deploy/compose.yml` 是真实部署起点：数据库不公开端口，应用非 root，设置内存限制。将 `deploy/.env.example` 复制为 `deploy/.env`，在本机填写凭据与模型配置，权限设 0600。密码用于连接 URL 时应使用 URL 安全字符或对连接 URL 正确编码。

```sh
docker compose --env-file deploy/.env -f deploy/compose.yml build
docker compose --env-file deploy/.env -f deploy/compose.yml up -d postgres neo4j chroma
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm api /app/.venv/bin/inkwise init-db
docker compose --env-file deploy/.env -f deploy/compose.yml up -d api worker
```

容器中 `127.0.0.1` 指容器自身。模型/OCR/嵌入服务地址必须按部署网络改为可访问地址；不自动把宿主机服务改成公网监听。健康响应的 `modelConfigured` 只说明配置存在，不代表供应商已经通过验收。

## Kafka 长文

配置 `INKWISE_KAFKA_ENABLED=true`、私有 Kafka 地址与 topic。另启 `inkwise chunk-worker`。任务按有界正文 core 和上下文区间拆分，outbox 只发送 chunkId；成功分块有持久记录，重复消息不会重复调用模型。整体内容/结构仍由全篇评分检查。

15000 字符按约 1000 字符分块，调用预算须覆盖分块数加五维评分和反思；默认 12 次预算会明确拒绝不足的长文配置。最多允许配置 60 次，但必须先确定费用预算。Kafka 本身不会让全链路变成 1.5 秒。

Broker 应在私网，并根据部署环境启用 TLS/SASL/ACL；本仓库的隔离测试采用回环 PLAINTEXT，不能据此宣称生产 Kafka 已加固。

## 未知模型请求恢复

超时不等于服务方没有处理。普通重试不会再次发送 `started/unknown` 请求。管理员需核实供应商状态，优先恢复已完成结果：

```sh
uv run inkwise --env-file .env reconcile-call --receipt-id RECEIPT_ID --action recover --result-file PRIVATE_RESULT_JSON --actor OPERATOR_ID --reason "供应商结果已核对" --provider-checked
```

只有确认需要重发时选择 `--action retry`。该操作记录处理人、原因和原 attempt，不清零任务调用预算；之后再在工作台请求重试。已成功评分维度和片段会复用。格式失败可有限重试，引用失败最多一次基于不可变原文片段的修复。

## 知识审核、DVC 与回滚

教师先确认报告，再独立授权提交脱敏正文与脱敏案例评语。原报告总评不自动复制；逐维反馈需单独勾选授权，并重新核对引用仍匹配脱敏正文。第二位教研审核者能查看正文、评语、引用与来源，再决定批准。旧候选缺少显式脱敏回流标记时不导出，需按新流程重新审核。公开演示中的确认不能进入此数据库。

```sh
uv run inkwise --env-file .env knowledge-export --tenant my-school --release kb-2026-10-10
uv run inkwise --env-file .env knowledge-index --bundle knowledge/artifacts/my-school/kb-2026-10-10/bundle.json
uv run dvc add knowledge/artifacts/my-school/kb-2026-10-10/bundle.json
```

Git 只追踪 `.dvc` 指针与非敏感清单，JSON 内容默认忽略。DVC remote 必须是学校批准的加密存储，凭据放私有配置，不写入 Git。代码不自动设置外部 remote，也不自动上传学生数据。

`inkwise evaluate --labels PRIVATE_LABELS_JSON --output PRIVATE_METRICS_JSON` 计算固定尺度 QWK、MAE、相邻一致率、分组统计和学生级 bootstrap；会拒绝学生跨训练/测试分区泄漏。指标没有变化样本时不伪造 QWK=1。

教研签核文件必须包含以下字段，才能 `knowledge-activate --bundle ... --evaluation ...`：

```json
{
  "bundleHash": "实际知识包SHA256",
  "tenantId": "my-school",
  "release": "kb-2026-10-10",
  "decision": "approved",
  "reviewer": "本校仍有效的教研或管理员身份ID",
  "modelRevision": "实际被评价模型的版本或digest",
  "labelFileHash": "实际评估标签文件的64位小写SHA256",
  "metrics": {"overall": {"count": 100, "qwk": 0.8, "mae": 0.2, "adjacentAgreement": 0.95}}
}
```

示例中的指标仅说明格式，必须替换为 `evaluate` 的实际输出；qwk在不可定义时为null，不能伪造1。审核者必须是同校有效教研/管理员，且不能自批本人提交的案例。校验验证结构、身份与绑定关系，不会把人工填写的文件当作独立证明，也不会自动判断教学效果已达标。

每个知识包限制8MiB、最多10000个候选，超限明确拒绝并要求拆分发布；导出按批读取，索引最多保留16条嵌入输入。大规模语料应采用分区版本，不能依赖增加内存无限扩容。

发布、规则导入和回滚共用学校级文件锁。激活先保存恢复记录，在数据库事务中留下提交标记，再原子切换规则清单；数据库与文件系统不是同一个事务。中途退出后运行：

```sh
uv run inkwise --env-file .env knowledge-recover --tenant my-school
```

恢复依据数据库提交标记完成新版本切换，或还原原版本；下一次发布、导入或回滚也会先执行该恢复。规则文件hash不匹配则停止，不猜测版本。`knowledge-rollback --tenant ... --release ...` 只允许回滚当前正在使用的该版本，防止旧回滚指令覆盖较新的发布。

校准函数 `propose_weights` 的固定步长求解器仅接受有限的0–5正则参数；其输出仍只是供教研审核的候选权重。

## 隐私与清除

- 教师凭据只保留在浏览器内存，退出时清空正文、原图对象 URL、候选和处理记录；不同作业不会沿用上一份原图或评语。
- 撤回候选有版本检查；检索时再次核对数据库授权，旧索引不能使撤回失效。
- 学校管理员可调用 `POST /api/v1/jobs/{id}/erasures` 清除实时数据库正文、原图、评分、OCR、模型结果及候选内容，保留最小审计。
- 如果已经导出知识包，接口会明确返回 `exportedStorageCleanupRequired=true`。需要由学校运维清理历史包、DVC 缓存/remote 与备份，并按保留政策完成记录；实时清除不冒称完成所有备份副本删除。
- 当前不具备学校法律/采购签核，不得直接导入真实学生数据进行无人监管试验。

## 验证命令

```sh
uv sync --frozen --extra dev --extra postgres --extra retrieval --extra kafka --extra data
uv run pytest -q
node tests/test_demo.mjs
docker compose -f deploy/compose.integration.yml --profile kafka up -d --wait --wait-timeout 120
uv run python scripts/wait_test_services.py
INKWISE_RUN_INTEGRATION=1 INKWISE_RUN_KAFKA=1 INKWISE_RUN_LOCAL=1 uv run pytest -q
node tests/test_workspace.mjs
docker compose -f deploy/compose.integration.yml --profile kafka down --volumes
```

本地真实模型检查为 `python scripts/validate_local_models.py`，只使用回环模型和原创合成作文，结果持久化在忽略目录中；已有成功结果不重复生成。它不属于学校金标准评估。具体本轮通过/失败和未测范围见 `delivery.md`。

## 0.4运行边界

详见[完整功能与验收操作手册](complete-workflows.md)。主任务与chunk worker必须共享数据目录和一致的路由配置；路由冻结和跨进程并发锁保存在该目录。OCR与embedding仅允许本地目标，云端评分须同时通过操作者开关、任务同意和费用上限。已有成功OCR缓存的配置变更会被明确拒绝，禁止通过删除成功缓存强行重跑。周期演进服务默认关闭，输出提案不自动发布学校量规。Docker默认包含语言识别，但不包含Torch或手写权重；手写模型在已验收本机单独准备。


## 2026-10-15 缓存容量与版本化导出

`inkwise storage-status` 只读磁盘余量及 OCR、模型路由缓存的汇总元数据；不输出作业标识、路径或正文。最多检查 100,000 个目录项，超限或目录异常时 `complete=false`，此时计数只是已检查部分，不能视为全量。命令不删除文件。

新增 OCR/路由处理前检查单任务缓存目录最多 128 个文件，默认预算 1 GiB（`INKWISE_CACHE_JOB_MAX_BYTES`）。磁盘默认保留 256 MiB 余量（`INKWISE_CACHE_MIN_FREE_BYTES`）；OCR 每页额外预留 96 MiB，路由计划预留 1 MiB。容量不足返回明确的存储错误，管理员核对空间后可按既有重试流程继续。此检查是开始新工作前的容量保护，不是操作系统磁盘配额；多个进程或外部程序仍可能改变可用空间。

已有成功缓存优先复用，容量不足也不重跑、不驱逐历史原稿或成功结果。缓存 JSON 读取上限 8 MiB，拒绝重复键、符号链接和读取期间发生变化的文件。全局保留周期仍由学校制定，删除仍使用已有的管理员授权擦除流程；未知模型调用仍需人工核查，不能因腾出空间就重发。

教师处理记录支持 `after` 游标与 `limit`（1–100），响应包含 `nextCursor`、`hasMore`；工作台可继续读取或从头查看，最多保留最近读取的 100 条，切换作业和退出清空。报告下载先按当前任务版本向服务端读取 `export-snapshot`，版本冲突时拒绝导出；文件标注学校、班级、任务版本、文字版本与 SHA-256、量规/模型/流程版本和核对时间。这是核对时的快照，之后有修改需重新下载。


### 当前源码：原稿图片预算

`INKWISE_IMAGE_STORE_MAX_BYTES`默认2GiB，`INKWISE_IMAGE_STORE_MAX_FILES`默认4096、最大4096，覆盖同一私有工作区的`images`原图及历史替换稿。现有每作业替换次数上限不变。每份规范化PNG按实际字节计入，开始解码前及写入前分别检查原有`INKWISE_CACHE_MIN_FREE_BYTES`余量。每次上传最多检查4096个平面原稿项和一个边界项，随后逐页累加，不递归扫描整个数据目录或额外复制原图。

API进程共享零内容的私有`.image-admission.lock`租约；忙时拒绝排队，进程退出由内核释放。租约文件不计为原图。链接、非普通文件或无法完整计量时拒绝新增，由管理员检查；既有超预算资料不会被删除。`storage-status`原有`files/bytes`仍表示缓存，新增`originalImages`只包含文件数、字节、预算、完整性和是否尚有预算。`hasCapacity`不保证下一份任意大小图片一定能写入。

上传容量不足返回507，安全存储不可用返回503，页面提供下一步。多页中途明确失败回滚本次文件；如果数据库提交回应未知，则保留可能已关联的完整新图，先刷新核对原作业，禁止为清理而删除可能成功的原件。崩溃或未知提交遗留的完整文件也计入预算，不自动清扫；管理员需按已批准的内容清除/保留流程处置，或显式调整预算。此准入不能阻止外部进程消耗磁盘。

原稿目录须属于当前服务账户且为0700；其数据父目录也须同账户所有、不可被其他账户写入。原稿仅接受同账户的单链接普通文件，允许0600或私有只读0400；租约须0600且为空。已有权限异常时拒绝新增，不自动chmod或删除原件。明确的507原图预算拒绝直接显示处置提示并保留输入；无法确认的503写入/数据库回应仍进入原核查流程。页面提示在旧提示被清除后仍能重新显示，退出时清除。
