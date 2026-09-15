# 更新日志

本项目从 `v0.1.0` 起采用语义化版本。尚未发布的变化只写入“未发布”，不能当作 GitHub 稳定版能力。

## 未发布

- 新增**素材证据层 V1**：`backlot/material_evidence.py` 统一承载音频包络、运动时间线与停顿证据，全片各一次解码（此前 27/12 次 spawn 降为各 1 次），签名只含素材身份，索引签名不变。S-001 用 −40 dB 标定包络，与人工标注 IoU 0.881。
- 新增**短语级语音单元与二次精剪 V7**（`backlot/material_interaction_units.py`）：边界只落在 VAD 间隙；字幕改按语音单元锚定（改前 5 句只出 1 句且错位）；`pause_target_gap_seconds` 改为按相邻两句**实际**保留间隙判定（默认 0.30 秒）；`gap_policy=any` 允许连环境音一起压，可一键切回三重许可。验收对比 R0007/R0024/R0027：`>0.30 秒` 空隙由 25/18/24 处降为 0/1/0 处。
- 新增**剪辑决策导出**（`backlot/material_interaction_export.py`）：导出无字幕成片 + SRT + `cut-list-v1`/FCP7/OTIO，只读，不覆盖已审看预览。
- 新增**粗剪推荐层 v2**（`backlot/material_interaction_recommend.py`）：权重 40/25/20/15，同一主体降为选材要求不计综合分，默认「优先时间长」；界面每条排名左切片右评分，二次精剪嵌在素材行内折叠。
- 新增**有界并发内核与零付费基准台**（`backlot/interaction_concurrency.py`、`scripts/benchmark_interaction_concurrency.py`）：视觉 C=4 提速 3.77×、ASR C=3 提速 2.99×，均受内核数上限约束；付费调用仍串行，基准台不产生任何付费请求。
- 新增**腾讯云 ASR 长音轨支持**（`backlot/tencent_asr.py`、`backlot/tencent_config.py`、`lib/tencent_cloud.py`）：60 秒级分块转写、分片提交与合并，配音中心「API 管理」可校验并保存 SecretId/SecretKey，错误一律返回中文可操作提示，任何路径都不回显密钥。此前长素材（60 分钟以上）转写结果会被截断。
- 新增**外站素材复刻链路**（`scripts/remake_*.py`、`skills/creative/material-remake-workflow.md`）：批量人脸筛素材 → 参考视频时间码转写 → 分镜表与机读脚本契约 → 合成项目与重排镜头。★ 短时长烧入字幕会漏过整片筛，须换镜 + `audit` 复检；竖屏字幕默认底边 0.888 低于平台 0.80 安全线，改为 64 号 / y 0.75。
- 新增 Remotion 新闻播报组件 `NewsAnchor`（`remotion-composer/src/components/NewsAnchor.tsx`）与 `styles/dark-tech-news.yaml` 风格模板。
- 修复 **ffmpeg/ffprobe 定位**（`lib/ffmpeg_locator.py`）：不再依赖 PATH，按候选顺序解析并校验可执行性，此前新机器上常因 PATH 缺失静默失败。
- 修复 **Windows 控制台弹窗**（`lib/subprocess_window.py`）：服务以无控制台方式启动时，每个 ffmpeg/npx 子进程都会新建窗口；现在统一以隐藏窗口参数派生。
- 修复腾讯云配音的音质回退：合成请求改为 `Codec=wav`。此前固定请求 mp3，而 24 kHz 下腾讯只返回 32 kbps 的 MPEG-2 Layer III，频谱 8.3 kHz 以上被完全削掉，试听与控制台试听差异明显；改走无损 PCM 后带宽恢复到约 11–13 kHz。预览与正式配音共用同一路径，一次改动同时生效。
  注意：豆包接口不提供 wav（仅 mp3 / ogg_opus / pcm），故豆包链路保持原样。
- 修复配音中心腾讯云音色的语速控件：此前后端只允许豆包音色保存语速，腾讯云音色拖动滑块必然保存失败，试听与正式配音恒为默认 1.25×（1.25 实际映射腾讯 `Speed=1`，出的是 1.2×）。
  现在腾讯云音色语速可保存，且界面只提供腾讯云真正支持的固定档位（0.60 / 0.80 / 1.00 / 1.20 / 1.50 / 1.70 / 2.00×）；1.10× 等中间值会取最近档位，不再显示无法合成数值。
- 配音中心的「云端音色管理」不再只支持豆包：新增「配音服务」下拉，可选豆包或腾讯云，填入显示名称与音色 ID 即可添加并试听。
  腾讯云侧接受任意纯数字 `VoiceType`（如 502003、601010），语速下拉只给腾讯支持档位；豆包侧行为与以前完全一致（含 `S_` 前缀自动用 ICL 资源）。
  旧配置文件里只有豆包的记录（无服务字段）继续按豆包加载，不会丢失。

## v0.1.1 - 2026-09-01

- 新增可单独发送给同事/Codex 的核心密钥安全配置指令。
- 新增隐藏输入的一键配置脚本，一次写入 GPT 中转站、豆包文本与 RunningHub 本地凭据，并提供不回显值的离线状态检查。
- 补齐 `OPENAI_TEXT_MODEL` 环境变量模板，明确豆包文本密钥和豆包语音密钥不能混用。

## v0.1.0 - 2026-09-01

- 建立 Haike Video 私有稳定源码快照和 Windows 可复现安装流程。
- 提供无数字人与双主持数字人的一键审核预览、安全恢复、字幕、背景音乐和确定性合成能力。
- 完成产品命名迁移，保留 AGPL 许可证、上游来源和第三方声明。
- 建立根目录接手入口、`main/dev/codex/*` 分支合同、版本文件、发布状态和机器审计门。

完整能力边界与验证证据见 [发布状态](docs/handoff/RELEASE_STATUS.md)。
