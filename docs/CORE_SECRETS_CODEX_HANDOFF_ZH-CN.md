# Haike Video 核心密钥配置指令（供同事与 Codex 读取）

更新时间：2026-09-01

这份文档可以单独发给同事。它只说明配置方法，**不包含任何真实密钥**。真实密钥必须通过密码管理器、加密文件或可信私聊单独传递，不要粘贴到本文、GitHub Issue、提交记录或 Codex 对话中。

## 目标

一次性配置三组核心凭据：

| 用途 | 必填变量 | 说明 |
| --- | --- | --- |
| GPT 中转站 | `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_TEXT_MODEL` | 脚本生成、整理及 AI 导演文本路由；Base URL 由中转站提供 |
| 豆包文本模型 | `DOUBAO_API_KEY` | 火山方舟文本 API，默认端点和模型由脚本填写 |
| RunningHub | `RUNNINGHUB_API_KEY` | 数字人工作流；生产工作流 ID、模板和精确帧 Profile 由脚本填写 |

注意：`DOUBAO_API_KEY` 是豆包/火山方舟**文本模型**密钥；`DOUBAO_SPEECH_API_KEY` 是豆包语音 TTS 的另一组可选密钥，二者不能互换。项目内置本地配音不要求豆包语音密钥。

## 同事本人操作

先完成仓库安装并进入仓库根目录。运行：

```powershell
.\.venv\Scripts\python.exe scripts\configure_core_secrets.py
```

终端会依次要求输入三组密钥和 GPT 中转站地址。密钥使用隐藏输入，不会回显；已配置过的字段可以直接回车保留。脚本把结果写入 Git 已忽略的 `.env.secrets.local`，不会修改 `.env.example`，不会联网，也不会发起付费任务。

如果确实要启用豆包语音 TTS，再单独运行：

```powershell
.\.venv\Scripts\python.exe scripts\configure_core_secrets.py --with-doubao-speech
```

只检查是否齐全，不显示任何值：

```powershell
.\.venv\Scripts\python.exe scripts\configure_core_secrets.py --check
```

`--check` 显示“核心配置：就绪”表示三组核心凭据及 GPT 中转站地址已经写入。它只做本地存在性检查，不代表账号权限、余额或云端工作流已经通过真实验收。

## 可直接发给同事 Codex 的指令

复制下面整段给同事的 Codex：

> 这是 Haike Video 的本地密钥配置任务。先读取仓库根目录 `START_HERE.md`、`AGENT_GUIDE.md` 和 `docs/CORE_SECRETS_CODEX_HANDOFF_ZH-CN.md`，按项目路由读取部署交接。不要让我把任何密钥粘贴进聊天，也不要读取、打印、回显或提交 `.env.secrets.local` 的内容。请在仓库根目录运行 `.\.venv\Scripts\python.exe scripts\configure_core_secrets.py`，让我本人在终端的隐藏输入提示中填写 GPT 中转站、豆包文本和 RunningHub 三组密钥。完成后只运行同一脚本的 `--check` 做脱敏存在性检查；不要调用供应商、不要运行真实素材生成、不要触发 RunningHub 或任何付费请求。最终只报告哪些变量“已配置/未配置”、配置文件路径是否被 Git 忽略，以及是否还缺非密钥的私有音色或人物参考图；绝不能报告密钥值。

## 脚本自动写入的公开路由默认值

以下不是密钥，可以进入配置模板：

```dotenv
OPENAI_TEXT_MODEL=gpt-5.6-luna
DOUBAO_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_TEXT_MODEL=doubao-seed-2-1-pro-260628
RUNNINGHUB_WORKFLOW_ID=2094449979141218305
RUNNINGHUB_BASE_URL=https://www.runninghub.cn
RUNNINGHUB_WORKFLOW_TEMPLATE=config/runninghub/workflow-2094449979141218305.api.json
RUNNINGHUB_WORKFLOW_PROFILE=infinitetalk_448x560_exact_clock_v2
```

如中转站不支持默认的 `gpt-5.6-luna`，应把 `OPENAI_TEXT_MODEL` 改成该中转站实际返回的模型名；不要擅自更换供应商。RunningHub 的公开默认值对应当前已验收的精确帧工作流，更换 RunningHub 账号或重新发布工作流后，只更新本地 `RUNNINGHUB_WORKFLOW_ID`，不要把账号密钥写进仓库。

## 安全边界

- 不要把 `.env.secrets.local` 通过微信明文、邮件附件或 Git 传递；只传递本说明文档。
- 不要截图包含密钥的终端或工作台配置页。
- 不要使用 `Get-Content .env.secrets.local`、`type .env.secrets.local` 或类似命令展示文件。
- 不要把密钥作为命令行参数；命令历史和进程列表可能留痕。
- 密钥配置完成不等于真实服务验收。任何 Pexels、RunningHub、模型或付费测试仍需单独授权与预算。
