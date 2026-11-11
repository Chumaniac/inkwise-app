# 0.4 功能与验收操作手册

日期：2026-10-12。交付目标为私有 GitHub 仓库；不部署服务器，不改变既有公开合成演示。所有真实评分仍须教师确认。

## 安装与配置

```sh
uv sync --frozen --extra dev --extra postgres --extra retrieval --extra kafka --extra language
uv run inkwise teaching-check
uv run inkwise setup-local
uv run inkwise --env-file data/private/local-demo/.env doctor --connect
uv run inkwise --env-file data/private/local-demo/.env local
```

`setup-local` 拒绝覆盖旧目录，为独立演示学校准备13套草案、三种角色凭据、随机评测盐、隔离校准目录和本机模型配置。访问文件只保存在本地；不要把其中内容复制到工单或仓库。完整可选参数见根目录 `.env.example`。

本机模型使用 `deploy/Modelfile.deepseek-local`，固定 `num_ctx=65536`。路由在首次任务中记录 Ollama digest，并在新请求前再次核对。已有任务的路由配置不允许静默改变；更换模型须新建独立任务，未知请求先用既有回执核查流程处理。旧版本未记录路由却已有推理回执的任务返回 `LEGACY_ROUTE_RECONCILIATION_REQUIRED`，不会假定其免费或尚未调用。

## 1. 课程参考与教学资料

- `teaching/reference-pack.json`：30张中、英、混写参考卡，区分1–9年级和高中扩展；每卡含来源、适用范围和原创建议。它们可以用于解释课程原则，不能冒充已打分学生例文。
- 来源包括教育部2022语文/英语课标与2020高中通知、英国DfE写作与KS2教师评估框架。中国课标只保留链接及自主改写原则；未获开放许可的全文与教材没有复制进仓库。
- 原6000对codespell候选词保留独立CC BY-SA许可。另有34对原创中文固定词语辨析候选，位于 `teaching/lexicons/zh-candidates.json`。全部仍为候选，不自动替换OCR或自动扣分。
- 工作台“查看候选知识 → 课程参考资料”支持年级、语言与关键词筛选。停用来源、修改正文或版本后，旧索引命中会被即时过滤。

```sh
uv run inkwise --env-file <学校私有配置> reference-index --tenant <学校代号>
```

正式案例仍走“教师终稿 → 单独授权脱敏 → 独立教研审核 → 留出评估 → 发布”原有流程。上述资料并不等于5000条学校审核词条，也不等于真实教师评分金标准。

检索复现：先运行 `scripts/validate_reference_library.py`，再运行 `scripts/validate_reference_retrieval.py`；使用隔离Neo4j/Chroma和本机BGE-M3。成功向量有缓存。15条原创相关性题目见 `teaching/reference-evaluation.json`，只衡量工程参考检索。

## 2. 对齐评测与置信校准

`calibration-fit` 将训练、校准、测试分区隔离，检查学习者跨分区泄漏，使用单调映射校准分数与“落入给定误差容限”的概率，输出MAE、五档QWK、教师间一致性、ECE、Brier及可靠性分桶。`evaluate-suite` 另支持OCR CER/WER、Recall/nDCG/MRR、必用规则覆盖、反馈有用性/错误建议率和延迟分位数。

```sh
uv run python scripts/prepare_public_ratings.py
uv run inkwise calibration-fit --source artifacts/public-evaluation/study-input.json --output artifacts/public-evaluation/profile.json
uv run inkwise evaluate-suite --source <有来源的评测输入.json> --output <汇总.json>
uv run inkwise --env-file <学校私有配置> calibration-publish --tenant <学校代号> --profile <审核后的校准文件.json> --reviewer <真实审核者ID>
```

已实测的公开数据是Yang、Yap、Ali（2023）在Mendeley发布的82份成人EFL作文数字评分，CC BY 4.0：[数据与署名](https://data.mendeley.com/datasets/8fbzsg82p9/1)。只读取数字列，不导入作文、姓名或学校信息。40/20/22划分分别用于拟合、置信校准、测试。这是历史ChatGPT评分，不是本项目DeepSeek推理，更不是K12金标准。

该参考校准MAE下降，但QWK退化，发布门禁正确拒绝。不能据此开启学校校准。正式校准须绑定学校、量规hash、年级、语言、模型revision、pipeline及可选聚合版本，并由有效教研角色发布。失配、缺少模型版本或撤换聚合版本时 `confidence=null`，不会输出伪概率。校准分数仅为参考，不改教师最终成绩。

## 3. 多页与手写OCR

本机macOS可使用已有Vision程序；英文手写可选TrOCR主引擎或第二引擎。安装和准备过程不会自动发生在启动时：

```sh
uv sync --frozen --extra dev --extra language --extra ocr
uv run python scripts/prepare_ocr_model.py --download
```

该脚本固定微软TrOCR版本 `eaacaf452b06415df8f10bb6fad3a4c11e609406`，使用上游文件清单校验大小和Git/LFS摘要，保留已验证权重。实际推理强制离线、safetensors、关闭遥测；每个共享数据目录最多同时运行一个TrOCR进程。约1.33GB权重的运行内存高于文件大小，默认Docker镜像不捆绑Torch或权重；本轮手写验收在48GiB macOS本机完成。

配置 `OCR_BACKEND=vision`、`OCR_SECONDARY_BACKEND=trocr`，或对英文使用 `OCR_BACKEND=trocr`。默认最多6页、每页10MB、合计30MB，单页TrOCR最多60行；第二引擎只比较最多8行。支持灰度、对比度、可选小角度纠偏、原图坐标映射、页序调整、重复页拦截、缺页提示和放大原稿。单栏投影是TrOCR回退分行方法；分栏、文字重叠、删除线和涂改必须看原稿，不能称为已自动理解复杂版面。

识别分歧只展示备选，不改写主转写。源图摘要与缓存配置均检查；配置变更返回 `OCR_CACHED_CONFIGURATION_CHANGED`，保留成功转写，恢复对应配置或人工核对。按任务清除内容时清除原图、预处理图与OCR缓存。HTTP OCR和embedding仅允许本地地址/私有IP/`host.docker.internal`；云端评分授权不能顺带授权图片或词库文本外发。

公开手写复现：`scripts/prepare_handwriting_samples.py` 与 `scripts/validate_handwriting.py`。保留GNHK公开测试集中12个普通英文词裁片，不导入完整可识别页面；[原项目及许可](https://github.com/GoodNotes/GNHK-dataset)，镜像revision写入manifest。不是中文学生手写、整篇作文或校对用时研究；已有成功结果再次运行会复用。

## 4. 意图与教师反馈

创建任务可选评分、仅校对或按教师处理要求识别；作文内的指令不会改变意图。已完成报告可以直接解释量规、分数和原文依据，不增加模型调用。仅校对不产生分数，教师可编辑/排除建议，不能作为评分案例回流。

反馈池只取仍授权、独立审核、同学校/量规/年级的脱敏案例摘要。重复建议、同证据多维扣分嫌疑、反思分歧与明显分数变动转教师裁决。规则冲突和无效引用继续按既有门禁拦截。不可变原文片段与规则枚举用于定向修复；错误维度最多追加一次收窄schema的修复，仍计入任务总调用上限。

## 5. 多语言政策

安装 `language` extra后使用本机Lingua识别，返回Unicode字符偏移和不确定提示。字符提示不等于语言识别置信度，更不代表学生能力。中英混写的语法/词汇分项分别取证，按已声明比例加权并归入量规档位；允许的引语、专名、英美拼写和受保护词语不应被直接扣分。

`teaching/language-policies/` 提供四种候选模板。方言从教师显式政策选择，系统不猜测学生方言身份。正式量规没有已审核语言政策时拒绝评分；合成量规可用框架默认政策。

```sh
uv run inkwise --env-file <学校私有配置> attach-language-policy --tenant <学校代号> --rubric <量规ID> --file <已审核政策.json> --release <新版本> --submitted-by <提交者ID> --approved-by <独立审核者ID> --source <审核来源>
```

方言模板只示范字段和保护词，不是完整地区方言库；中英之外的分数归一化与方言教学效果仍无学校实测证据。

## 6. 周期提案、影子与班级A/B

`learning-cycle` 从明确授权的脱敏案例、教师分数和学习者代号产生有界、周期幂等的权重提案，不采集学生原文。训练/留出分组使用学校私有盐；没有固定模型版本、足够学习者或学校允许学习权重时，记录阻断理由。

```sh
uv run inkwise --env-file <学校私有配置> learning-cycle --tenant <学校代号>
# 只有明确启用 EVOLUTION_ENABLED 后才可启动；本轮没有常驻运行该服务。
uv run inkwise --env-file <学校私有配置> evolution-worker --tenant <学校代号>
```

工作台可查看提案；提交、独立批准、经审核的影子试验和发布分开。真实A/B按班级平衡分配，至少4班，任务还须有单独试验授权；影子模式使用配对结果。发布校验试验、提案、量规、学校、年级、班级与模型/pipeline一致。撤回案例或试验授权即时影响后续取用/统计；退化时暂停变体并回滚模型聚合指针，不改写既有成绩或学校原量规。这里的“真实A/B”指完整运行功能，实际学校试验尚未开展。

## 7. 混合路由、并发与长文

默认只允许本机。云端必须由操作者设置HTTPS模型端点、固定revision、价格、总上限并启用，再由任务选择hybrid、勾选云端授权、给出美元预算。云端长文优先和本机不可用时的预请求降级均受同一授权约束；请求一旦发出，不自动换模型重发。云端路径使用模拟供应商测试，本轮没有云端费用或真实学生文本外发。

预算按“最大调用数 × 最大上下文/输出token × 配置价格”给出保守上界，单位为micro-USD；这不是供应商账单。价格必须由操作者核对。每次请求完整载荷都检查上下文；缺少匹配tokenizer时采用UTF-8字节保守上界。可用 `routing` extra及校验和固定的tokenizer文件精确计数。跨进程文件锁限制共享数据目录/端点并发；分块回执带独立chunk范围，未知状态不会重发。

```sh
uv run python scripts/validate_long_text.py
uv run python scripts/validate_long_text.py --real-local
```

脚本只对独立合成英文长文运行，记录受理/处理延迟、Python协调进程峰值RSS、token用量、模型revision和总调用数。成功结果直接复用。已知零调用预检查失败可用 `--retry-preflight`；有剩余额度且无未知请求的已知引用失败可用 `--resume-known-failure`。这些选项不增加原12次预算，不允许无限重试。

本机15,000字符真实模型处理经两次受限修复后进入教师复核，累计处理330.36秒（不计开发修复间隔），共12次调用；不是一次无中断完成的冷启动基准，也不是p95。1.5秒完整批改目标未达到。主服务接受任务约6.3ms、确认入队27.6ms，仅为单样本；Ollama进程/整机CPU和峰值内存没有完整采样，不把协调进程RSS当成模型总内存。

## 本机验收入口

```sh
docker compose -f deploy/compose.integration.yml --profile kafka up -d --wait
INKWISE_RUN_LOCAL=1 INKWISE_RUN_INTEGRATION=1 INKWISE_RUN_KAFKA=1 uv run pytest -q
node tests/test_demo.mjs
node tests/test_workspace.mjs
docker compose -f deploy/compose.integration.yml --profile kafka down --volumes
```

真实Chrome脚本是 `tests/browser_acceptance.cjs`，配合 `INKWISE_SYNTHETIC_PREVIEW=1 uv run python tests/serve_browser_fixture.py`；需要本机Chrome、Playwright、Vision可执行程序；预览程序会准备两张合成测试页并复用已有文件，详情见脚本和交付证据。没有实际学校数据时不得把此结果写成教学签核。

## 0.4.1 教师日常操作补充

作业可按题目查找并翻页；候选也可翻页。候选待审核或被退回时，提交者可在候选卡片选择“修改候选”，重新核对脱敏内容和使用授权后保存，系统重新排入审核；已批准案例不能直接改写。

下载报告采用已保存内容。若尚有修改，先完成教师确认再下载；原稿查看和页码切换不会重置正在编辑的评分与评语。新建任务会清空上一次的正文、处理要求和云端授权。退出会清理当前页面内容；这不是跨刷新自动保存。

OCR失败且原文尚未确认时，可选中需要纠正的当前页，在上传用途里选择“替换当前页”，仅上传一张新扫描。成功的其他页继续复用缓存，替换前的图片保留在页码下拉列表中供核对；历史原稿不能作为当前页继续排序或替换。默认保留最多12张历史原稿，学校管理员擦除任务内容时同步清除。
