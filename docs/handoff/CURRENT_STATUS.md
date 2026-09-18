# 当前项目状态

更新时间：2026-09-18

## 主线状态

- **统一队列**：SQLite 单通道，优先级/FIFO/幂等/恢复；全片父任务串行。双主持子任务先落账后并行。`review_ready` 不是批准，成片需人工观看。
- **有数字人 V1.8**：整数 PCM 样本 + 25FPS 清单是切割主合同，Whisper 只作诊断；`exact_frame_clock` QA 阻断漂移。★ 供应商记录只在**当次任务** `phases` 里 → 新起同种任务会**重新付费生成数字人**；换镜后重出预览须排 `full_preview`。
- **无数字人 V1.6**：`source_audio_only` 支持显式逐条定时字幕；新项目响度 −9、旧项目 −14 LUFS。
- **多标题/新闻层 V1.2**：`story_headline_overrides` 与普通层共用 revision/CAS 与 FFmpeg 合同，保存只重做当前片段。
- **二次精剪 V7 + 推荐层 v2**：短语级语音单元（边界只落 VAD 间隙）；字幕按语音单元锚定；`pause_target_gap_seconds` 按相邻两句实际间隙（默认 0.30 秒）；`gap_policy=any` 默认；导出无字幕成片 + SRT + `cut-list-v1`/FCP7/OTIO。推荐权重 40/25/20/15，同一主体降为选材要求，默认「优先时间长」。
- **素材证据层 V1**：包络与运动时间线各一次解码（原 27/12 spawn）；−40 dB 标定，与人工标注 IoU 0.881。
- **Cut V2 提速**：`interaction_concurrency.py` 有界并发（视觉 C=4 3.77×、ASR C=3 2.99×）；付费仍串行。
- **音频软件级默认（09-16）**：人声 +8 dB / BGM −6 dB / 成片 −9 LUFS；口播走 `highpass→EQ→volume(增益+10)→alimiter` 处理链，去削顶并把波峰因数 13.2 压到 9.4。真源头＝`backlot/{narration,music,output_loudness}_preferences.py::DEFAULT_*`。★ 响度天花板 `I = min(目标, −2.0 − 波峰因数)`；母版偏轻的期单独下调目标值。★ 抖音端削低电平内容（P5/P25 −4.3~4.4 dB）⇒ BGM 验收只看包络 P5/P25。
- **竖屏字幕与文字图层**：底边须 < 0.80（默认 64 号 / y 0.75）；一层只一种颜色，不自动换行、超框裁字，换文案必重测。★ 断行不得切 ASCII 字母数字串（`1000` 曾被切成 `100`+`0`）；行首整串可超预算，上限 2× 预算。
- **跨仓研究包消费（09-16）**：只读 CopySkill 的 `episode-research-pack-v1`（`current.json` → `pack_path`，永不扫 `.staging`）。四处**越合同约束已拆**（分层见 `DECISIONS.md`）。真实生产形态现可达编辑门 exit 3；默认适配器恒产 `nodes: []` ⇒ `argument_map_ready` 需注入真实 `research_builder`。研究包**不进生产队列**；出成片必须显式提交队列。
- **外站素材复刻契约化（09-17）**：`schemas/remake-spec-v1.json` ＋离线口径层 `backlot/remake_project.py`；素材选择权归项目（`docs/MATERIAL_SELECTION_POLICY_V1_ZH-CN.md`）。重建不再把音频默认打回、不再绑错旧素材、不再把无数字人期推回数字人上下文。
- **四平台分发**：creatorhub（抖音/快手/视频号/小红书）是否真发出以创作者后台为准；无封面能力，需后补封面脚本。

## 固定生产合同

- InfiniteTalk 精确帧模板 `config/runninghub/workflow-2094449979141218305.api.json`；新任务默认 Plus 48GB 每角色一次；双角色前冻结 5 元预算。★ 48GB 实测耗时 `t = 19s + 39s × 窗口数`，去 block swap／改量化对耗时是噪声 ⇒ 显存升级不等于提速。
- 未知 RunningHub 响应进 `ambiguous` 并停止，禁止重复提交。
- 响度验收 `|I−target| ≤ 1.5` 且 `TP ≤ −1.0`；正式渲染不达标直接 raise。

## 当前阶段

`dev`（`7ac0109`）之后本地还有一批未提交改动：音频处理链与响度默认、跨仓研究包消费、复刻 spec 契约化、字幕断行与切镜闪白；`main`/`v0.1.1` 仍为 `50352aaa`；私有数据不进 Git。

## 最近验证

- 全量 `tests/backlot tests/lib tests/tools tests/contracts tests/unit` 通过；`scripts/audit_context_handoff.py` 全 OK。
- 二次精剪 v7 三版对比：`>0.30 秒` 空隙 25/18/24 处 → 0/1/0 处；成片 108.425/108.242/55.495 秒。
- 跨仓通道实测（09-16）：真实交付 r1 被本仓只读收编，`content_sha256=5c68f6dc…`、validation `pass`。
- 整批优化套用 8 期已交付；工具链留本地 `.backlot/9.14-remake-8/`，不入库。
- ★ `final_review.audio_spotcheck` 归一化前取值会稳定误报，以 `loudness.integrated_lufs`/`true_peak_dbtp` 为准。

## 下一步

1. 人工观看本轮各版本预览，含 `gap_policy=any` 切口听辨，只到待审，不发布。
2. 产品决策：排序「优先时间长」与首次切片「单互动 ≤180 秒」冲突时跳过或放宽上限。
3. `dev` 经独立副本及 Linux 预发布验收后才可合入 `main`。
