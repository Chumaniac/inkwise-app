# 准备本机评分服务

新工作区默认使用 `inkwise-qwen-local`：Qwen2.5-7B-Instruct Q4_K_M，32768 上下文，4096 输出上限。模型输出始终经过结构、原文引用和规则校验，教师保留终评权。一次软件闭环通过不能当成真实教学准确率。已有 DeepSeek 工作区不会被默认重写；可在首次创建时用 `--model-profile deepseek-r1-7b` 保留该配置。

## 常规准备

安装并打开 [官方 Ollama](https://ollama.com/download)，使用本机服务，无需云端密钥。

```sh
ollama pull qwen2.5:7b
ollama pull bge-m3
ollama create inkwise-qwen-local -f deploy/Modelfile.qwen-local
```

## 下载网络受限时

保留网络和重定向防护。使用官方 GGUF 文件源、固定 SHA-256 校验与 Ollama 的 split-GGUF 导入，不把原始模型或旧报告删除。需要约4.7GB下载，以及提供方导入所需额外空间。

源码目录：

```sh
.venv/bin/python scripts/prepare_local_model.py --download --import-model
```

已安装的桌面包（不需要额外安装 Python）：

```sh
inkwise_runtime="$HOME/Applications/墨评.app/Contents/Resources/runtime"
"$inkwise_runtime/python/bin/python3" "$inkwise_runtime/app/scripts/prepare_local_model.py" --download --import-model
```

准备工具只下载固定官方文件，保留部分下载以便续传；先校验哈希，再通过既有本机 Ollama 导入。不会覆盖已有同名模型、访问文件、评分配置或数据库。导入 split GGUF 需要 Ollama 0.35+。如使用自定义评分服务，继续核对原服务配置，不要把不同模型混用到同一名字。

文件来源：[Qwen 官方 GGUF](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF)，模型为 Apache-2.0；[官方模型卡](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)。分片大小与校验值固定在准备脚本中。模型权重不随代码仓库或桌面包再分发。

## 检查与使用

准备后在启动页点击重新检查，或运行 `inkwise doctor --connect`。此检查只查询目录，不执行评分。登录时可选择私人工作区的 teacher-access.txt 填入，只保存在当前页；关闭或退出会清除。正式使用先核对量规，再确认原文，完成教师复核后导出。

模型评语默认不发布。教师应逐项核对后采纳或改写，不准确、重复或无实际帮助的评语可直接不选；原模型建议留在私有审阅记录中，学生报告使用教师选择。
