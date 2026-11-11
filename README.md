# 墨评 · 独立用户版

把作文原文、评分依据、教师复核与报告放在同一个本地工作区。

分数绑定身份、作业、已确认原文、量规版本和模型版本；缺少原文依据或必要规则会阻断评分。教师终评与案例授权是独立操作。

这是包含完整可构建源码的社区用户仓库，不依赖原私有仓库。旧仓库的提交历史、用户数据、模型缓存和内部验收记录不在这里。

## 开始使用

前提：Python 3.12、uv；本机评分需另装 Ollama 与指定模型。macOS 原生 OCR 构建需要 Xcode Command Line Tools。

```sh
git clone https://github.com/Chumaniac/inkwise-app.git
cd inkwise-app
uv sync --frozen --no-dev --python 3.12
.venv/bin/python scripts/start_local.py --no-open --start-model
```

浏览器只连接本机回环地址。桌面版下载与系统支持以 [Releases](https://github.com/Chumaniac/inkwise-app/releases) 的实际资产为准；没有对应资产时从源码安装。桌面构建方式见 [构建说明](docs/build.md)。

## 用一次完整流程

新作业/上传原稿 → 确认文字 → 真实评分 → 教师逐项复核 → 导出绑定原文版本的报告

1. 先按模型配置说明准备本机服务。首次启动会建立私人练习工作区与访问文件；把 teacher-access.txt 中的凭据填入登录框，关闭时清除会话。
2. 选择练习量规，粘贴合成作文或上传获授权原稿。检查 OCR 不确定行，确认文字后再提交评分。模型错误会原样转成可处理失败，不补造分数。
3. 教师逐项核对引用依据、维度分数与评语，明确保存复核。导出报告时检查作业/文字/规则版本；旧成功报告继续保留。

```sh
.venv/bin/inkwise doctor
.venv/bin/inkwise teaching-check --directory teaching --json
.venv/bin/python scripts/start_local.py --upgrade-check --no-open
```

## 保存与边界

macOS：~/Library/Application Support/InkwiseK12/workspace；其他系统：~/.local/share/inkwise-k12/workspace。首次建立的 teacher-access.txt 是私人访问文件，不能上传或分享。

练习包内的13套量规为合成草案，不是学校批准标准。模型文件不随源码/桌面包再分发。可选向量检索、Kafka 长任务和学校实际评阅需要单独配置与验收；默认本地规则回退会要求教师复核。禁止把真实学生文本、图片、访问文件或原始服务响应提交到仓库。

- [完整使用说明](docs/user-guide.md)
- [隐私、备份与恢复](docs/privacy.md)
- [排错与环境检查](docs/troubleshooting.md)
- [已验证范围](docs/verification.md)
- [参考项目与许可](docs/references.md)

许可证见 [LICENSE](LICENSE)，第三方资料见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
