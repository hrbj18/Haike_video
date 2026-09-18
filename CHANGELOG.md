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
- **口播处理链**（`backlot/workbench.py`）：人声增益不再只做裸 `volume=NdB`。旧实现把台词推到 +0.6~+2.9 dBFS 削顶（gpu / microduck 期实测），既失真又听不清；现在固定叠加 `highpass 90` + `equalizer 3k +3dB` + `volume(项目增益 + 10dB)` + `alimiter 0.85`，波峰因数 13.2 → 9.4，可在不牺牲 LRA 的前提下把可达响度提高约 1.8 dB。施加上限由实测决定：驱动 +22 dB 时 TP 越 −1.0 容差。
- **成片响度不再在混音前的中间态否决**（`workbench.py::_generate_project_video_render`）：此前正式渲染在施加人声/BGM 之前就按发布容差 raise，一个本来能被混音救回的中间电平会掐死整条链（gpu 期实测中间态只到 −12.4 就中止，声音设置根本没机会应用）。现在只有「直接音轨模式」的正式渲染才在该点把关，其余交给混音之后那次权威归一化。
- 修订**软件级音频默认**为 人声 **+8 dB** / BGM **−6 dB** / 成片 **−9 LUFS**（此前 BGM −14、成片 −10）。依据是把已发布成片从抖音取回分带实测：平台不做大幅整体衰减（全频仅 −1.1 dB），但专门削低电平内容（P5 −4.4 / P25 −4.3 dB），而 P5/P25 正是 BGM 唯一露头处；叠加手机单喇叭 <300 Hz 衰减 15~20 dB ⇒ 抬 BGM 是唯一有效杠杆。验收判据随之改为音频包络 P5/P25，不得再用整片 200–800 Hz 的 LUFS。
- 修复**重建项目把音频默认打回**的两处漏洞（`scripts/remake_build_project.py`）：BGM 缺键时兜底软件级默认而不是 −16 dB；`narration_gain_db` 缺键时兜底 +8 dB 而不是 0 dB。后者的原写法还会在 `np_` 未定义处抛 `NameError`，且崩在场景与母版已改完之后，留下"半重建"项目。
- 修复**重建后全片预览必然 422**（`scripts/remake_build_project.py`）：重建会改变音频混音签名，但脚本直接写 `state.json` 不走 API，服务端不会自动把第一段声音样板置 stale。现在重建末尾显式置 stale，下游重新生成样板。
- 修复**重建后画面绑回旧素材**（`scripts/remake_build_project.py`）：脚本原先只增不删资产，整期换素材后旧资产仍留在表里并与新资产共用 `S1 ` 名字前缀，转场重排会绑错素材（powerbank / ram / deepseek 期实测）。现在会清理残留资产（移入项目内回收目录而非删除）并刷新未变 aweme_id 的资产显示名。
- 修复**无数字人期被推回"有数字人"上下文**（`scripts/remake_build_project.py`）：原实现无条件写入 `avatar.default_treatment="custom"`，使 `_scene_presenter()` 不再回退 `hidden` ⇒ animated-explainer 期每次重建都走数字人分支。现按 `pipeline_type == "avatar-spokesperson"` 才写该键。
- 新增**素材选择政策 V1**（`docs/MATERIAL_SELECTION_POLICY_V1_ZH-CN.md`）：素材选择权归本项目而非 copyskill，必须基于多宫格帧大图先看图再选区段，禁止"整条源当一整块素材"。附 shengteng 水印/烧入字幕漏筛等四期事故复盘。
- 新增**`remake-spec-v1` 机读契约**（`schemas/remake-spec-v1.json`）与离线校验层 `backlot/remake_project.py`（规格书 → `script.json` 与逐段视觉时间线的纯函数适配，帧口径与 `workbench.py::_validated_visual_timeline` 一致，供离线校验与后续编排复用，带单元测试）。
- 新增**跨仓研究包只读消费链路**：`backlot/copy_skill_research_pack.py`（权威校验：current → READY → manifest → 七语义文件、hash、路径安全、外键、时间码、权限门）、`research_pack_snapshot.py`（冻结 editorial snapshot 纯投影）、`research_pack_intake.py`（账本 + 快照，永不扫 `.staging`）、编辑层 `remake_intake.py` / `remake_editorial.py` / `remake_editorial_verdict.py`；CLI `scripts/remake_editorial_cli.py`（fail-closed，退出码 2/3/4）。新增 `python -m backlot research-pack <path> [--batch]` 子命令，进程内执行：不进生产队列、不触网、不花钱。
- 修复**素材筛选中 `%` 让整条源被跳过**（`scripts/remake_material_screen.py`）：抽帧把整条输出路径交给 ffmpeg，image2 muxer 会扫描路径里所有 `%`，词干里的 `80%` 被当成非法序列占位符 ⇒ 报 "Cannot write more than one file"，整条源无结果（musk 期实测）。目录名现在会先净化。
- 修复**素材筛选一坏俱坏与结果丢失**（`scripts/remake_material_screen.py`）：单条源抽帧失败原先抛 `SystemExit` 直接终止整批；`--json` 传带目录的相对路径时会在最后一行 `FileNotFoundError`，前面整轮抽帧/人脸/字幕带检测全部白跑。现在失败可捕获并跳过该条，输出路径按调用方 cwd 解析并自动建目录。
- 新增**切镜闪白**（`workbench.py::_directive_filter_chain`）：`flash_white` 进入外科手术式组件白名单，以 start 为中心做整帧白场。它不套用通用 `duration = max(0.5, …)`，否则 0.15 秒会被抬到 0.5 秒。
- 修复**字幕把一个词切成两半**（`workbench.py`、`material_interaction_units.py`）：行宽是算术，按"第 N 个字符"切的硬上限会把 `1000` 切成 `100`+`0`、`UnifoLM-WLA-1.0` 切开，成片里出现以孤立字符开头的字幕。现在 ASCII 字母/数字串（含 `Model 3`、`RTX 5090`、`7:14`、`48%`）视为不可断整体，硬切落在串内时整体后移。
- 新增 **RunningHub Plus 48GB 工作流构建与付费 A/B 探针**（`scripts/build_runninghub_infinitetalk_48g_workflow.py`、`scripts/probe_infinitetalk_48g_variants.py`）：从冻结 24GB 图派生 48GB 版本并逐变体付费对比。实测耗时模型 `t = 19s + 39s × 窗口数`（启动只占一分钟渲染的约 2%），去掉 block swap、改 load_device 或量化对耗时均为噪声 ⇒ 显存升级不等于提速。

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
