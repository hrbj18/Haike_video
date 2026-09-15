# 当前项目状态

更新时间：2026-09-15

## 主线状态

- **统一队列**：SQLite 单通道，优先级/FIFO/幂等/恢复；完整视频父任务串行。双主持视频内部两个 RunningHub Plus 48GB 子任务先落账后并行。`review_ready` 不是批准，成片需人工观看。
- **有数字人 V1.8**：整数 PCM 样本 + 25FPS 清单是切割主合同，Whisper 只作诊断；默认 Plus 48GB 每角色一次；`exact_frame_clock` QA 阻断漂移。★ 供应商记录只存在于**当次任务**的 `phases` 里 → 新起同种任务会**重新付费生成数字人**；换镜后重出预览必须排 `full_preview`。
- **无数字人 V1.6**：`source_audio_only` 支持显式逐条定时字幕；响度新项目默认 -10、旧项目 -14 LUFS（可 -16~-8）。
- **多标题/新闻层 V1.2**：`story_headline_overrides` 与普通层共用 revision/CAS 与 FFmpeg 合同，保存只重做当前片段。
- **二次精剪 V7 + 推荐层 v2**：短语级语音单元（`material_interaction_units.py`，边界只落 VAD 间隙）；字幕按语音单元锚定；`pause_target_gap_seconds` 按相邻两句实际保留间隙（默认 0.30 秒）；`gap_policy=any` 为默认并可切回三重许可；导出无字幕成片 + SRT + `cut-list-v1`/FCP7/OTIO。推荐权重 40/25/20/15，同一主体降为选材要求，默认「优先时间长」。
- **素材证据层 V1**：包络与运动时间线各一次解码（原 27/12 次 spawn），签名只含素材身份；−40 dB 标定包络，与人工标注 IoU 0.881。
- **Cut V2 提速**：`interaction_concurrency.py` 有界并发内核（视觉 C=4 3.77×、ASR C=3 2.99×，受内核数上限约束）；付费调用仍串行。
- **音频软件级默认（09-15 定案）**：人声 +8 dB / BGM −14 dB / 成片 −10 LUFS。★ 响度有物理天花板 `I = min(目标, −2.0 − 波峰因数)`，抬人声增益改不动它；母版偏轻的期必须单独下调目标值，否则正式渲染直接 raise。
- **竖屏字幕与文字图层**：字幕底边必须 < 0.80（默认 64 号 / y 0.75）；顶部文字图层与署名牌已进模板，一层只有一种颜色，不自动换行、超框裁字，换文案必重测。
- **外站素材复刻**：工具链与坑位见 `skills/creative/material-remake-workflow.md`；预览 v004 43.30s。
- **四平台分发**：creatorhub（抖音/快手/视频号/小红书）是否真发出只以创作者后台为准；自身无封面能力，需后补封面脚本。

## 固定生产合同

- InfiniteTalk 精确帧模板 `config/runninghub/workflow-2094449979141218305.api.json`；新任务默认 Plus 48GB 每角色一次；双角色前冻结 5 元预算。
- 未知 RunningHub 响应进 `ambiguous` 并停止，禁止重复提交。
- 响度验收 `|I−target| ≤ 1.5` 且 `TP ≤ −1.0`；正式渲染不达标直接 raise。

## 当前阶段

GitHub `dev` 已纳入 `ddfbd16`（2026-09-15，含素材证据层 V1、二次精剪 v7、云 ASR/TTS、复刻链路）；`main`/`v0.1.1` 仍为 `50352aaa`；私有数据不进 Git。

## 最近验证

- 全量 `tests/backlot tests/lib tests/tools tests/contracts tests/unit` 通过；`scripts/audit_context_handoff.py` 全 OK。
- 二次精剪 v7 三版对比：`>0.30 秒` 空隙由 25/18/24 处降到 0/1/0 处；成片 108.425 / 108.242 / 55.495 秒。
- 整批优化套用 8 期已交付；该批工具链留在本地 `.backlot/9.14-remake-8/`，不入库。
- ★ `final_review.audio_spotcheck` 在归一化前取值会稳定误报，以 `loudness.integrated_lufs`/`true_peak_dbtp` 为准。

## 下一步

1. 人工观看本轮各版本预览，含 `gap_policy=any` 切口的听辨，只到待审，不发布。
2. 产品决策：排序「优先时间长」与首次切片「单互动 ≤180 秒」冲突时跳过还是放宽上限。
3. `dev` 经独立副本及 Linux 预发布验收后才可合入 `main`。
