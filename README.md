<img src="demo/dist/assets/mark.svg" width="56" height="56" alt="" />

# 墨评 Inkwise

[![Build](https://github.com/Chumaniac/inkwise-app/actions/workflows/user-edition.yml/badge.svg?branch=main)](https://github.com/Chumaniac/inkwise-app/actions/workflows/user-edition.yml) [![License](https://img.shields.io/badge/license-MIT-555)](LICENSE)

逐段核对作文原文、评分依据与教师终评。

[在线试用](https://chumaniac.github.io/inkwise-app/) · [桌面下载](https://github.com/Chumaniac/inkwise-app/releases)

网页版使用合成作文。选择案例、确认原文、检查引用，再保存复核结果。自定义文本可以用于人工复核，网页不会生成模型分数。

评语卡上的「定位原文」会选中对应的原句。修改作文后，重新评阅才会显示与新原文匹配的依据。

## 核心功能

- 对照原文和量规查看每个维度的依据。
- 标记需要复核的段落，保留教师修改。
- 桌面版可连接本机评分服务，并导出绑定原文版本的报告。

## 本机快速开始

前提：Python 3.12、uv；本机评分需另装 Ollama 与指定模型。macOS 原生 OCR 构建需要 Xcode Command Line Tools。

先按[本机模型配置](docs/local-model-setup.md)准备 Ollama 与 `inkwise-qwen-local`。`--start-model` 只启动已安装的服务，不下载或创建评分模型。

```sh
git clone https://github.com/Chumaniac/inkwise-app.git
cd inkwise-app
uv sync --frozen --no-dev --python 3.12
.venv/bin/python scripts/start_local.py --no-open --start-model
```

浏览器只连接本机回环地址。桌面版下载与系统支持以 [Releases](https://github.com/Chumaniac/inkwise-app/releases) 的实际资产为准；没有对应资产时从源码安装。桌面构建方式见 [构建说明](docs/build.md)。

## 操作步骤

新作业/上传原稿 → 确认文字 → 真实评分 → 教师逐项复核 → 导出绑定原文版本的报告

1. 先按 [本机模型配置](docs/local-model-setup.md) 准备评分服务。首次启动会建立私人练习工作区与访问文件；选择 teacher-access.txt 填入登录框，或手动输入凭据，关闭时清除会话。
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

## 常见问题

**网页需要密钥吗？** 不需要。公开示例在浏览器内处理。本机评分的模型、工作区目录和访问方式见[配置说明](docs/local-model-setup.md)。

[参与贡献](CONTRIBUTING.md)

## 工作区与窄屏操作

窄屏评阅分为原文与报告两个视图。引用定位会回到原文；评阅完成后进入报告。教师工作台的班级列表按需展开，选中任务后收起，退出会清除展开状态。
