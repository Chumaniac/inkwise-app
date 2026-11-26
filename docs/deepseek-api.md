# DeepSeek API 配置

本机可移除 Ollama，评分继续使用现有云端路由。此配置模板保持云端关闭、密钥为空、费用上限为零；填写模板不代表已通过真实模型质量验收。

已有工作区修改 `~/Library/Application Support/InkwiseK12/workspace/.env` 中对应键，保留数据库、规则、访问文件和其他设置。先停止工作区并备份配置，不要把密钥放入仓库、聊天或网页前端。

```dotenv
INKWISE_MODEL_BASE_URL=""
INKWISE_MODEL_NAME=""
INKWISE_MODEL_REVISION=""
INKWISE_MODEL_API_KEY=""
INKWISE_CLOUD_ENABLED=false
INKWISE_CLOUD_MODEL_BASE_URL="https://api.deepseek.com"
INKWISE_CLOUD_MODEL_NAME="deepseek-flash"
INKWISE_CLOUD_MODEL_REVISION="DeepSeek-V4.1-Flash-1017"
INKWISE_CLOUD_MODEL_API_KEY=""
INKWISE_CLOUD_CONTEXT_TOKENS=65536
INKWISE_CLOUD_STRUCTURED_OUTPUT="json_object"
INKWISE_CLOUD_INPUT_PRICE_MICROUSD_PER_MILLION=300000
INKWISE_CLOUD_OUTPUT_PRICE_MICROUSD_PER_MILLION=1200000
INKWISE_MAX_CLOUD_COST_MICROUSD=0
INKWISE_EMBEDDING_BASE_URL=""
INKWISE_EMBEDDING_MODEL=""
INKWISE_EMBEDDING_REVISION=""
INKWISE_LEXICON_MODEL_REVISION=""
```

管理员之后填写密钥、核对当前价格，并设置明确的单作业费用上限，才将 `CLOUD_ENABLED` 改为 `true`。`MAX_CLOUD_COST_MICROUSD` 的单位为百万分之一美元，1000000 表示 1 美元。程序按最大上下文、输出和调用次数保守估算费用，可能高于实际账单；不能为通过检查直接放大额度。前端每份新作业还必须选择云端路由、同意发送确认后的原文并给出预算。在 DeepSeek 准备页点击“重新检查并打开工作区”可读取新保存的密钥、云端开关及预算。修改工作区位置或服务地址时需重开应用。

上述价格使用 2026-11-25 查到的 Flash 高峰未命中缓存价格作为保守上界；启用前重新核对 [官方模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)。模型修订为运营方记录的版本名，不能保证远端别名的权重永久不变。DeepSeek 请求明确关闭 thinking，输出仍经过既有 JSON、原文引用及权限检查。参见 [官方接口](https://api-docs.deepseek.com/api/create-chat-completion/)。

移除 BGE-M3 后，当前未配置的向量检索沿用本地量规快照降级；不会把既有向量改成其他维度，也不会将案例或 OCR 材料自动发到云端。已保存作业、评分和成功调用回执不重新生成。密钥或预算未配置期间不能进行新评分，应用会显示配置指引。
