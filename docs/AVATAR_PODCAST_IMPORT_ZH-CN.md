# 双数字人播客素材导入与原声剪辑

这套链路把“数字人生成”和“节目剪辑”拆开：公司网站或未来 API 只负责生成带原声的视频；Haike Video 负责接收、核对、排列、合成、生成字幕和质量检查。

## 核心规则

- `turn_id` 和 `speaker_id` 是稳定主键。一个轮次不能重复、漏失或交换人物。
- 推荐每轮台词导出一个视频，例如 `T001_YAYA.mp4`。长视频按人物切分仅作为兼容模式。
- 数字人视频自带声音是唯一主音轨，`audio_mode` 固定为 `native_avatar_audio`。工作台会阻止重复生成 TTS。
- 脚本里的开始/结束时间只是生产估算。最终剪辑点、字幕和两分钟时长检查都基于实际原声母版。
- 不在后台静默下载 ASR 模型。机器没有可用的本地 faster-whisper 模型时，任务会明确失败并给出原因。

## 素材目录

按轮次导入时：

```text
projects/<project-id>/
├─ artifacts/
│  ├─ script.json
│  └─ avatar_source_package.json
├─ assets/incoming/avatar/
│  ├─ yaya/T001_YAYA.mp4
│  └─ mengmeng/T002_MENGMENG.mp4
└─ renders/avatar/
   ├─ avatar-dialogue-master.mp4
   ├─ avatar-dialogue-timeline.json
   ├─ avatar-dialogue-subtitles.srt
   └─ avatar-dialogue-qa.json
```

长视频兼容模式将每个人物的文件存入 `assets/incoming/avatar/longform/`。ASR 会先识别长视频，再把脚本文字映射到词级时间戳，并在相邻轮次之间寻找较安静的切点。

## 工作台操作

1. 打开项目，进入“数字人导入”。
2. 选择“按轮次逐条导入（推荐）”并初始化素材包。
3. 将包含 `T001`、`T002` 等编号的文件批量拖入，或在每条台词右侧单独上传。
4. 点击“检查全部原片”。系统检查真实文件、视频/音频流、时长、重复文件和编码可读性。
5. 点击“ASR 核对台词”。每条识别结果必须达到配置的覆盖率，整期平均相似度也必须过线。
6. 点击“合成原声母版”。输出采用 H.264/AAC、25fps、48kHz，并执行完整解码、两分钟上限和最终母版 ASR 检查。

命令行也可查看和执行相同步骤：

```powershell
.\.venv\Scripts\python.exe scripts\avatar_package.py 004-tech-brief status
.\.venv\Scripts\python.exe scripts\avatar_package.py 004-tech-brief validate
.\.venv\Scripts\python.exe scripts\avatar_package.py 004-tech-brief asr
.\.venv\Scripts\python.exe scripts\avatar_package.py 004-tech-brief assemble
```

## 数据合同

`schemas/artifacts/avatar_source_package.schema.json` 是手工上传与未来 API 的共同输出合同，记录：

- 提供方、导入模式和原声音频策略；
- 人物与轮次清单；
- 文件相对路径、SHA-256、媒体探测结果；
- 每轮 ASR 文本、相似度、覆盖率和长视频切点；
- 合成任务状态、输出路径和 QA 问题。

`schemas/artifacts/script.schema.json` 增加了可选的 `turn_id`、`speaker_id`、`speaker_name`、`expected_asset_filename` 和 `visual_contract`，不影响现有单人项目。

## 未来公司 API 接入边界

当前生产基线是 `ManualAvatarImportProvider`。公司 API 稳定后，只新增一个提供方适配器，把任务提交结果写成同一个 `avatar_source_package`；后面的媒体探测、ASR、合成和 QA 不需要改变。

接入前必须确定并冻结：

- 正式基础 URL、API 版本和认证方式；
- 创建任务、查询状态、取消任务和下载结果的请求/响应字段；
- 幂等键、超时、重试、速率限制和错误码；
- 结果文件有效期、签名 URL 刷新方式和版权/审计元数据；
- 人物模型 ID、声音策略，以及一轮台词对应一个任务的保证。

在这些合同未稳定前，不把临时网址硬编码进软件，也不让工作台依赖供应商字段。

## 当前质量边界

已自动化：文件安全、媒体探测、轮次完整性、重复文件提醒、ASR 文本核对、长视频对齐、安静点切分、原声母版、字幕、编码/解码、时长和最终 ASR。

仍需人工或后续视觉模型：新闻画面是否与台词语义一致、角标是否准确、数字人是否被遮挡、抖音安全区、素材版权，以及“媒体消息”是否被误写成“官方确认”。这些要求已能随轮次记录在 `visual_contract` 中，但尚未实现自动视觉语义判定。

