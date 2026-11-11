# 教学起始资料包

本包可直接用于本地练习环境、任务设计和人工审阅。它包含 **13 套量规草案、13 篇原创练习、6000 对外部英文易错词候选**。所有量规与练习均未获得学校签核；没有虚构教师评分，没有真实学生个人数据。

## 内容及适用范围

- `sources.json`：教育部2022语文/英语课标、DfE写作课程和KS2评价来源，注明地域、学段、使用方式与许可边界。教育部PDF在本次抓取中出现重定向限制，因此未声称完成逐页校勘；不把网上可读等同全文再分发许可。
- `rubrics/`：五维权重合计100，五个档位，字段可由现有量规引擎直接加载。1–2年级中文；3–4、5–6、7–9年级中、英、混合写作；10–12年级中、英、混合议论写作。高中部分为原创进阶草案，未宣称对齐中国高中课标。
- `examples.json`：原创合成任务、短文、修改提示。`scores=null`，没有冒充教师金标准或历年真题。既有RAG只接收独立审核后的案例，本包不会自动成为高置信评分锚点。
- `third_party/codespell/`：独立许可的6000对外部英语候选。固定上游commit、源文件和派生文件校验和、转换脚本、署名及CC BY-SA 3.0全文。数据许可独立于闭源应用许可。
- `manifest.json`：文件索引及校验和；`inkwise teaching-check` 验证来源引用、量规schema、候选状态及文件完整性。

量规描述、权重、0–4档划分为Inkwise原创提案，不是教育部或DfE公布的分值。中国与英国年级体系不机械等同。方言暂无经过学校审核的规则，继续保留拒绝自动评分的边界。

## 准备与练习

```sh
uv run inkwise teaching-check
uv run inkwise setup-local
uv run inkwise --env-file data/private/local-demo/.env doctor
```

初始化仅将13套草案装入独立的 `local-demo/practice` 练习环境，保留 `synthetic=true`。教师、教研、管理员访问文件分别保存在该私有目录，互不相同。不会启用外部易错词，也不会覆盖已有目录。

工作台创建作业时选择匹配的年级、语言、体裁和量规；可复制 `examples.json` 中的任务与正文用于练习。执行前按[本机快速开始](../docs/quickstart.md)准备本地模型并启动API与worker。

## 6000对真实来源候选的本地向量化

```sh
uv run python scripts/build_candidate_index.py
```

需已在本机Ollama安装 `bge-m3`。脚本只访问 `127.0.0.1:11434`，不提供云端地址选项；每16条持久化一次，可恢复复用成功批次。索引、缓存和回执位于忽略目录 `artifacts/teaching-index`。索引绑定源hash与实际模型digest，租户标识固定为 `candidate-only`，不会创建学校运行时的 `active.json`。不在Git中提交可重建的模型向量。

这是外部词对的真实向量索引；词条是否适合具体学段、OCR语境检索精度和教师采用率仍须另行评价。不是5000条真实学生错误，也不是5000条已审核知识。

## 正式采用

1. 教师针对教学任务、学段、语言变体和体裁修订草案；教研审核每个维度的权重、档位、例外与重复扣分风险。
2. 审核人取得独立校准集：按学生划分数据，双教师评分及争议仲裁。使用 `inkwise evaluate` 计算QWK、MAE及分组指标；本包不伪造金标准。
3. 保存学校采用版为新文件与新release，将已审核量规 `synthetic` 明确改为false；以现有 `import-rubric` 命令记录两位同校独立身份及原始依据。此操作由实际学校流程完成，初始化工具不会代办签核。
4. 对外部词库按年级、领域、专名、方言及上下文筛选；保留NOTICE和许可。另存批准版，显式设 `reviewStatus=approved`，记录来源与审核依据，再运行 `lexicon-build`。仅改变字段不能代替真实审核。
5. 经教师确认、独立授权及教研审核的真实或正式教研案例，使用原有knowledge导出、索引、评估、激活与回滚流程。未经审核的原创示例不自动注入评分锚点。

## 候选材料取舍

没有把许可不明确的中文纠错集、教材作文或学生手写样本批量搬入仓库；没有用自动制造错字补足词库数量。中文和方言词库继续使用现有学校审核入口。英国来源按OGL范围署名，不复制其中学生个人数据或未获授权第三方内容。

Contains public sector information licensed under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).

## 0.4新增资料

`reference-pack.json`含30张版本化中英/混写课程参考卡及5个官方来源入口；`reference-evaluation.json`含15条原创相关性标签，不冒称教师gold。`lexicons/zh-candidates.json`为34条原创中文固定词语候选，hash已纳入manifest。`language-policies/`为中、英、混写、方言的未批准配置模板。全部正式采用仍走学校审核流程，不能用候选状态绕过发布校验。公开评分、手写资料的许可、实测限制与复现步骤见[0.4手册](../docs/complete-workflows.md)。
