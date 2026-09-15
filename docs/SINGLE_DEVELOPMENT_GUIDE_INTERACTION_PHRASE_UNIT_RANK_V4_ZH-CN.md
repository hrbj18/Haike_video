# 单次开发指导文档：户外互动「短语级语音单元 + 推荐权重排序」V4

- 状态：**开发中**（编写时间 2026-09-12）
- 上游文档：`docs/SINGLE_DEVELOPMENT_GUIDE_INTERACTION_PAUSE_RECOMMEND_V3_ZH-CN.md`（停顿压缩/粗剪推荐 V3）、
  `docs/SINGLE_DEVELOPMENT_GUIDE_MATERIAL_EVIDENCE_LAYER_V1_ZH-CN.md`（素材证据层 V1）
- 本文档的读者是**后续接手的人**：所有数字都必须能由文中的命令复现；所有"应该"都必须能落到一个文件、一条判据。

---

## 1. 本次开发目标

用户在真实使用中提出四条反馈，本次开发只解决这四条，不做发散：

| # | 用户原话（要点） | 本次要达成的可验证目标 |
|---|---|---|
| G1 | "先写评分，然后没有对应的素材……原素材一长，评分部分就很长，要拉大半天才能看到素材" | 粗剪候选改成**左侧素材、右侧评分**的双列布局；素材常驻可见，评分不再把素材挤到屏幕外 |
| G2 | "不知道这个素材编号是不是对应排名""评分可以选择权重……同一主体是选素材要求不计入综合分""可选优先时间长/时间短/综合分/情绪值""默认优先时间长" | 推荐层权重与排序模式**可配置**；默认权重序为 **时间长 > 老外 > 情绪 > 互动/歌舞**；**同一主体**降级为"素材要求"（不计入综合分）；默认排序 **优先时间长** |
| G3 | "掐头去尾太狠了，不需要压缩到一分钟以内……两分十六秒的素材应从『你好』开始、到『拜拜』结束" | 二次精剪的目标时长**按父内容比例**推导（不再强压 45–60 秒）；片头/片尾按**问候/告别词**确定性锚定 |
| G4 | "中间对话拼接还没有完成压缩" | 对话间隙压缩**真实生效**：可删停顿总量相对现状提升 ≥ 2 倍，且仍然满足"与 VAD 语音帧相交 0"的既有硬约束 |

**验收总目标（用户给定）**：新建项目 `长视频测试2`，导入
`C:\Users\Administrator\Downloads\直播回放-09月08日\直播回放-09月08日.mp4`，
完成长视频剪辑测试，默认排序使用"优先时间长"，并对**排名前 3 名**的素材直接做二次精剪。
"仅这次任务"意味着前 3 名是**验收动作**，不是产品默认行为。

---

## 2. 现状核查（全部为实测，不是推断）

以下每条都在本机跑过，命令与输出摘要保留在 §7。样本为
`projects/cut-v2-accept`（来源 `C:\Users\Administrator\Downloads\5分钟.mp4`）与
`projects/local-material-understanding-test`。

### 2.1 已冻结的二次精剪计划暴露了全部问题（`ISP-01488a7acc5a412a`）

```
version              = interaction-second-pass-plan-v5
source_duration      = 136.527 秒   （父候选 keep_ranges = 12.033—148.56）
body_source_duration =  60.630 秒   （被语义层保留下来的正文）
removed_source_seconds = 75.897 秒  （"内容删减"）
removed_by_pause_seconds = 1.678 秒 （"压缩停顿"，仅 3 刀）
output_duration      =  53.593 秒
```

**结论**：136.5 秒 → 53.6 秒不是"压缩"造成的，是**丢弃**造成的。

### 2.2 丢弃的两段原因不同，必须分别处理

| 区间 | 时长 | 为什么被丢 |
|---|---|---|
| 12.033—60.11 | **48.1 秒** | **没有任何语义组覆盖它**。ASR 分句 `U00001` 跨 `0.0—60.26`，而父候选允许区间是 `12.033—148.56`；`story_utterances()` 的 `_containing_range()` 要求分句**完整落在**允许区间内，于是 `U00001` 被整条剔除，这个区间**从未进入模型视野**，也**没有产生任何警告** |
| 60.11—87.81 | **27.7 秒** | 模型把 `U00002` 整条（27.4 秒）判为 `drop`，理由"信息密度相对较低" |

`U00001` 的文本里恰恰包含"姐姐喂 你好"，**用户要的"从『你好』开始"就藏在这条被静默剔除的分句里**。

### 2.3 根因：ASR 只有**60 秒级**分块，没有句子级时间

```
audio.provider = tencent-asr:ap-guangzhou:16k_zh:audio-16k-mono-mp3-v1
U00001 0.0—60.26    U00002 60.26—87.66   U00003 88.08—148.56
U00004 148.56—208.6 U00005 208.6—268.62  U00006 268.62—315.067
```

十条分数（60.26 / 27.4 / 60.48 / 60.04 / 60.02 / 46.45 秒）说明这是**定长分块**，不是句子。
于是模型能做的取舍粒度 = 27 秒或 60 秒；**任何 3–5 秒的精彩前置都构造不出来**（实测该计划
`hook_candidates = []`，"精彩前置"恒为 0 秒）。这正是"掐头去尾太狠"的机器原因。

> 该方向在项目内已被记录：`docs/handoff/INTERACTION_ROUGH_CUT_PLAN.md` §素材证据层 V1 结尾写明
> "提升方向是 **补 ASR 分句粒度**，不是放宽证据层"。

### 2.4 停顿压缩为什么几乎等于没做（实测栅格）

探测证据：`noise_db = -30`、`min_silence_seconds = 0.45`、`backend = envelope`、
`envelope_threshold_db = -40`（`material-pause-evidence-v2`）。在 `12.033—148.56` 上共 **17 条静音、合计 11.20 秒**。

用真实证据跑计划层裁剪（`_pause_trims`，只统计"未裁剪过的整段父范围"）：

| 探测下限 | guard | target gap | 刀数 | 删减总量 | 占父内容 |
|---|---|---|---|---|---|
| **0.45（现状）** | **0.12（现状）** | **0.20（现状）** | **6** | **2.84 s** | **2.1 %** |
| 0.45 | 0.06 | 0.12 | 14 | 5.72 s | 4.2 % |
| 0.35 | 0.06 | 0.12 | 21 | 6.43 s | 4.7 % |
| 0.30 | 0.06 | 0.12 | 23 | 6.90 s | 5.1 % |
| 0.30 | 0.05 | 0.10 | 28 | 8.19 s | 6.0 % |
| 0.25 | 0.05 | 0.10 | 29 | 8.25 s | 6.0 % |

机制：单刀可删量 = `静音长度 − 2×guard − target_gap`，且当
`可删量 ≤ MIN_PAUSE_REMOVAL_SECONDS(0.05) + target_gap` 时整刀放弃。
现状参数等效门槛是"静音 ≥ **0.69 秒**才动刀"，而这条素材的对话间隙绝大多数是 **0.45—0.60 秒**——
**全部落在门槛之下**。`material_pause_evidence.py` 的模块注释其实已经把话说透了：
"真正限制结果的是计划层的 guard / 呼吸空间 / 最短停顿规则"。

### 2.5 粗剪推荐层的评分构成与用户要求不符

`material_interaction_recommend.py`（v1）：
`WEIGHTS = {same_subject .25, high_emotion .30, foreign_speech .20, performance .25}`，
**四条标准全部进综合分**，其中"时长"被折叠进 `same_subject`（`0.25×一致性 + 0.55×时长分量 + 0.20×完整性`）。
用户的诉求是：**时长独立成一项、权重最高；"同一主体"只作选材要求，不进综合分。**
现状默认排序是"按四条标准排序 / 按原片时间顺序"两选一，**没有"优先时间长"这类模式**。

### 2.6 界面的实际断点

`renderMaterialInteractions()` 先渲染**左侧单个原片播放器**，再渲染**右侧**（`.interaction-events`）
一长串事件卡；每张卡把评分、证据、边界依据、待核验、区间编辑、8 个操作按钮、对白证据 **全部纵向堆叠**。
素材因此被顶到屏幕之外；评分与素材编号之间没有视觉绑定，用户无法判断"编号 ↔ 排名"。

---

## 3. 架构思路（本次的关键设计决策）

### 决策 1：新增"语音单元"层，让**所有切点都落在 VAD 间隙里**

新增 `backlot/material_interaction_units.py`。输入是**已付费**的三样东西——
ASR 分句、VAD 语音区间（`speech_ranges`）、父候选允许范围；输出是**短语级语音单元**：

- 单元边界 = **VAD 语音块之间的真实间隙**（不是字符比例估算出来的时间点）；
- 因此"一个单元的起止"在结构上**不可能切开任何一个词**，这是硬保证而不是概率；
- 单元文本 = 把 ASR 短语流**单调、按字符数比例**投影到该分句覆盖的语音块上（与
  `_utterance_phrase_windows` 同一套估算口径，只是把落点吸附到 VAD 间隙）；
- 分句与语音块严格保持原序，**不做任何跨分句搬运**。

**为什么这是关键**：单元层同时解决 G3 与 G4。
① 模型现在取舍的是 2—6 秒的单元，不可能再"一刀砍掉 60 秒"；
② 3—5 秒的精彩前置第一次变得可构造（`hook_target_seconds = [3, 5]` 是既有合同）；
③ "从『你好』开始、到『拜拜』结束"变成"找到第一个/最后一个命中词表的单元"，可确定性实现；
④ 停顿压缩的落点与单元间隙天然对齐，不再被 60 秒块边界掩盖。

### 决策 2：单元层是**派生视图**，不进入任何付费签名

单元由"分句 + 语音区间 + 允许范围"三者纯函数派生，**不新增任何模型调用，也不改变索引签名**。
这与 V3 的"推荐层是零成本旁路"、素材证据层 V1 的"签名只含身份"是同一条纪律。
`story_utterances()` 在没有语音证据时**原样退回**旧行为（分块级），并写 `degradations`。

### 决策 3：计划版本升 v6，旧计划必须继续可读（N7）

`build_second_pass_plan` 冻结 `spoken_units`；新增
`UNIT_AWARE_VERSIONS = {VERSION}`，只有 v6 才被要求复现单元派生。
`validate_second_pass_plan` 的"重新派生并逐字段比对"逻辑只对 v6 增加 `spoken_units` 比较项。
**v1—v5 的磁盘计划保持逐字节可读** —— 历史计划是证据，不是草稿。

### 决策 4：目标时长改为**按父内容比例**（用户明确要求）

新增选项 `duration_policy`：`proportional`（默认）/ `absolute`。

- `proportional`：`target_min = 0.70 × 父内容`、`target_max = 1.00 × 父内容`（各自夹到 `[15, 1800]`）；
- `absolute`：沿用用户显式填的秒数（旧行为，可复现旧结果）。

同时改写提示词中"尽量进入目标区间"的措辞为 **"保留完整叙事链；只有明显重复、跑题、与互动无关时才整组删除；不得为了凑时长删除有信息的对话"**。
136.5 秒的素材在 proportional 下的目标区间是 **95.6—136.5 秒**，与用户"从你好到拜拜、只做微调"的预期一致。

### 决策 5：首尾锚定是**确定性规则**，不交给模型

`normalize_story_analysis()` 在依赖闭包之前增加一步 `_anchor_edges()`：
在**语音单元**上从前往后找第一个命中问候词表的单元，把片头锚定到它；从后往前找第一个命中告别词表的单元，
把片尾锚定到它；两者之间的单元强制 `keep`，区间外的强制 `drop`（`reason` 写明"'你好'等明确开场词"）。
找不到命中时**退回**现有的"丢弃首尾 greeting/farewell 类型组"行为，并写 `warnings`。
词表是显式常量（`GREETING_LEXICON` / `FAREWELL_LEXICON`），可测试、可审计、不依赖模型版本。

### 决策 6：停顿压缩用**预设**联动探测与计划两层

单改一层是无效的（§2.4 已证明）。新增 `pause_preset`：

| 预设 | probe `min_silence` | `pause_min_seconds` | `pause_guard_seconds` | `pause_target_gap_seconds` |
|---|---|---|---|---|
| `conservative`（= 现状，保留用于复现） | 0.45 | 0.40 | 0.12 | 0.20 |
| `standard` | 0.35 | 0.30 | 0.08 | 0.15 |
| **`tight`（新默认）** | **0.30** | **0.25** | **0.06** | **0.12** |

显式传入 `pause_min_seconds` / `pause_guard_seconds` / `pause_target_gap_seconds`
时以显式值为准（预设只提供缺省），因此旧调用与旧测试不被静默改写。
三重许可（不与 VAD 相交 + 静音探测确认 + 单条足够长）**一条都不放松**；
"删减量提升"来自把固定开销从 0.44 秒压到 0.24 秒，不是来自放宽安全门槛。

### 决策 7：推荐权重"四改四 + 一降级"

- 计分因子（进综合分）：`duration`、`foreign_speech`、`high_emotion`、`performance`，
  默认权重 **0.40 / 0.25 / 0.20 / 0.15**（严格满足"时间长 > 老外 > 情绪 > 互动唱歌跳舞"）；
- `same_subject` 移入 `requirements.same_subject = {ok, score, threshold, label, evidence}`，
  **不再进入 `recommendation_score`**，只作为"选材要求"供筛选与展示；
- 排序模式 `ORDER_MODES`：`duration_desc`（**默认**，优先时间长）、`duration_asc`（优先时间短）、
  `score_desc`（优先综合分）、`emotion_desc`（优先情绪值）；
- 推荐模块版本升 `material-interaction-recommend-v2`；`workbench._interaction_recommendations()`
  增加缓存版本校验，**旧缓存自动失效并在本地免费重算**（不触发任何付费调用，索引签名不变）。

### 决策 8：排序权威在服务端，界面不复制一套规则

`rank_events()` 是唯一定序实现；读接口接受 `order_mode` / `weights` 覆盖参数，
**覆盖请求不写缓存**（缓存只存默认视图）。界面改权重/排序 → 一次本地 GET → 重绘。
不把排序规则在 JS 里再实现一遍，避免"两套规则漂移"。

### 决策 9：界面是**一条排名一行，左＝该条切片、右＝该条评分**，原素材不参与排名

「左边是对应剪辑素材，右边是对应评分」里的**"对应"是关键**：左边要显示的是**这一条排名自己的
裁切片段**，不是原片。所以：

- 排名区是**单列**，从第 1 名往下排；**每一条卡片内部**才是左右两栏：
  左＝该条的切片播放器，右＝该条的四项评分 + 选材要求 + 理由；
- 左侧那份视频**优先播已出的首次完整切片**（`interaction-candidates/IEP-*/preview.mp4`），
  还没出片时播**原片的对应区间**（`…review.mp4#t=start,end` 媒体片段；媒体路由支持
  HTTP Range 206，浏览器按需取流），并在徽标上写明用的是哪一种、区间是多少；
- **原素材回看**移到排名区**上方的一个折叠块**（`原片回看（与排名无关，仅用于核对边界）`），
  不再是排名左侧的常驻列 —— 用户明确要求"原素材不要放在排名的左边"；
- 卡片头不再需要"点了把素材塞进共享播放器"这种间接绑定：切片就在卡片里，绑定关系是**结构性的**。

---

## 4. 任务分解（改哪些文件、改什么）

### T1 语音单元层（新增，核心）

`backlot/material_interaction_units.py`（新文件）

```
VERSION = "material-interaction-units-v1"
MIN_UNIT_SECONDS = 0.35     # 短于它的语音块并入相邻单元，避免 0.16 秒的假组
MERGE_GAP_SECONDS = 0.22    # 小于它的两个语音块视为"连续说话"，先合并
SNAP_TOLERANCE_SECONDS = 1.5

split_phrases(text, *, max_chars)            # 从 second_pass 迁出，行为逐字节不变
phrase_windows(utterance, *, max_chars)      # 同上（_utterance_phrase_windows 的实现）
speech_blocks(speech_ranges, allowed, *, merge_gap)   # VAD 语音块（合法切点之间的极大段）
build_spoken_units(utterances, speech_ranges, *, allowed, max_chars) -> (units, degradations)
```

- 单元 id 用 `P0001` 起始（与 ASR 的 `U00001` 不冲突）。
- 无文本的单元（分句数少于语音块数时）**强制 `keep`**，不允许模型删除它看不见文本的东西。
- `speech_ranges` 为空 → 返回旧行为（分块单元）并把原因写进 `degradations`。
- 纯函数：同样的输入必须给出同样的输出（可测）。

### T2 二次精剪计划层（改造）

- `backlot/material_interaction_story.py`
  - `build_story_context(index, parent_plan, options, *, units=None)`：`units` 非空时，
    上下文里的 `utterances` 换成单元，并增加 `unit_source = "vad_aligned"` 与 `unit_count`；
  - `PROMPT_VERSION` → `interaction-story-prompt-v3`：
    说明"每条 supplied item 是一个 2—6 秒的语音单元"、删掉"为凑目标时长而删"的措辞、
    要求 3—5 秒精彩前置、要求首尾必须是明确的开场/告别词；
  - `normalize_story_analysis()` 增加 `_anchor_edges()`（决策 5）。
- `backlot/material_interaction_second_pass.py`
  - `VERSION` → `interaction-second-pass-plan-v6`，新增 `UNIT_AWARE_VERSIONS`；
  - `normalize_options()` 增加 `duration_policy`；`build_second_pass_plan()` 冻结 `spoken_units`
    并据 `duration_policy` 推导 `target_min/max_seconds`；
  - 计划层不对单元做任何再切分（单元只是"取舍原子"与"锚定引导"）。
- `backlot/material_interaction_second_pass_candidates.py`
  - `_resolve_story()` 调用 `build_spoken_units()` 并把单元交给 story 层；
    缓存请求签名**包含单元签名**（否则换算法会命中旧分析）。

### T3 停顿压缩预设

- `material_interaction_second_pass.py`：`PAUSE_PRESETS` 常量 + `normalize_options` 支持 `pause_preset`；
- `material_pause_evidence.py` / `workbench._interaction_pause_evidence()`：透传 `min_silence`；
- `workbench.py`：由选项推导 `min_silence` 后调探测（探测身份签名含 `min_silence`，两套证据可共存不冲突）；
- UI：等待段处理旁增加"停顿压缩强度"选择（保守/标准/紧凑）。

### T4 推荐层后端

- `material_interaction_recommend.py`：
  - `VERSION = "material-interaction-recommend-v2"`；
  - `WEIGHTS = {duration .40, foreign_speech .25, high_emotion .20, performance .15}`；
  - `_event_factors()` 拆出 `duration` 因子，`same_subject` 改产出 `requirements`；
  - 新增 `ORDER_MODES` / `rank_events()` / `resolve_order_mode()`；`build_recommendations(..., sort_mode=...)`；
  - 理由文本增加"【时间长度】"一条，共 4 条理由 + 1 条要求理由。
- `workbench.py`：`_interaction_recommendations(project_dir, asset_id, index, *, weights=None, sort_mode=None)`
  增加版本校验与覆盖路径；`read_asset_material_interactions(..., preferences=...)` 透传。
- `server.py`：`GET .../media-index/interactions` 接受可选 `order_mode`、`w`（逗号分隔四项权重）查询参数。

### T5 推荐层界面

- `backlot/ui/workbench.js`
  - 新增 `materialInteractionRanking`（服务端返回的排序视图）与
    `materialInteractionRankPreferences`（权重/排序/要求筛选，localStorage 持久化）；
  - 新增 `renderInteractionRankPanel()`：左素材 / 右评分双列；
  - `renderMaterialInteractions()` 用新面板替换旧的 `.interaction-events` 列表，
    审核目录保留在下方（不再与推荐混排）。
- `backlot/ui/workbench.css`：`.interaction-rank-layout`、`.interaction-rank-card`、
  `.interaction-rank-badge`、`.interaction-score-bar`、`.interaction-weight-panel` 等（明暗主题都走既有变量）。

### T6 验收脚本

`scripts/accept_interaction_phrase_unit_v4.py`（新文件，可重复运行的验收台）：
新建项目 → 导入长素材 → 预检 → 跑互动分析 → 读默认排序（优先时间长）→ 依次对前 3 名
生成首次完整切片 + 二次精剪 → 落 `docs/` 机读报告。**不使用未文档化的旁路调用**。

### T7 测试

- 新增 `tests/backlot/test_material_interaction_units.py`；
- 更新 `test_material_interaction_recommend.py`（新因子集/要求/排序模式）；
- 更新 `test_material_interaction_story.py`（单元上下文 + 首尾锚定）；
- 更新 `test_material_interaction_second_pass.py`（v6、`duration_policy`、`pause_preset`）。

---

## 5. 开发流程步骤（严格按序）

1. 记录**改动前基线**：`pytest tests/backlot -q` 结果 + `git status --short`（保护用户已有改动）。
2. 写本指导文档（本文）并冻结验收标准，**先写标准再写代码**。
3. T1 单元层 + 单元测试（纯函数，可离线验证）。
4. T3 停顿预设 + 参数测量（用真实证据复算 §2.4 栅格，确认提升幅度）。
5. T2 计划层 v6 + 首尾锚定 + 单元上下文（含 N7 兼容回归）。
6. T4 推荐层 v2 + 排序模式。
7. T5 界面双列与权重面板。
8. 全量回归（`tests/backlot` + `tests/unit` 中与上下文包相关的用例）。
9. 启动工作台（`4754`），按 T6 跑 `长视频测试2` 端到端验收。
10. 回填本文 §12 执行结果，更新 `docs/handoff/` 中受影响的最小文件并跑 `scripts/audit_context_handoff.py`。

---

## 6. 前期准备

| 项 | 要求 | 说明 |
|---|---|---|
| 工作台 | `BACKLOT_PORT=4754 CODEBUDDY_SAFE_DELETE_ENABLED=0 ./.venv/Scripts/python.exe -m backlot serve --port 4754` | 漏掉第二个变量会在删除路径抛 `SystemExit` 杀掉进程；**改完代码必须重启**（无热重载） |
| FFmpeg | static-ffmpeg 的 `win32/ffmpeg.exe` | PATH 上的 master 版可能缺滤镜；本机已验证 `.venv/Lib/site-packages/static_ffmpeg/bin/win32/` 可用 |
| VAD | `faster_whisper.vad`（Silero） | 二次精剪依赖它；缺失要在依赖自检里报，不得到渲染才炸 |
| 云端凭据 | 腾讯云 ASR（长素材） + GPT 中转文本模型 | ASR 为**付费**：88.9 分钟素材按 600 秒分片 ≈ 9 片；文本模型每次二次精剪**最多 1 次** |
| 素材 | `C:\Users\Administrator\Downloads\直播回放-09月08日\直播回放-09月08日.mp4`（1.72 GB） | 时长 > 60 分钟，禁止整片上传；走分片 ASR |
| 预算 | 单次任务不做整片数字人；本任务不含 RunningHub 付费 | 视觉窗口上限由预检返回，提交前显式记录 |

---

## 7. 测试方法

```bash
export PATH="/usr/bin:/bin:$PATH"
cd "D:/刘宇钊/codex_work/Haike_video"

# 定向测试（串行，避免模块间污染）
./.venv/Scripts/python.exe -m pytest tests/backlot/test_material_interaction_units.py \
    tests/backlot/test_material_interaction_recommend.py \
    tests/backlot/test_material_interaction_story.py \
    tests/backlot/test_material_interaction_second_pass.py -q

# 全量回归
./.venv/Scripts/python.exe -m pytest tests/backlot -q

# 上下文包自检（改了 docs/handoff/ 之后必须跑）
./.venv/Scripts/python.exe scripts/audit_context_handoff.py
```

冻结计划只读复算（不写盘、不付费）：

```bash
./.venv/Scripts/python.exe -c "p='.backlot/_probe_second_pass.py'; exec(compile(open(p,encoding='utf-8').read(),'x','exec'), {'__name__':'__main__','__file__':p})"
```

> 注意：本机 Bash 工具下的 heredoc 会改写 `\s`、`\d` 等转义，**含正则的脚本一律先写文件再执行**；
> 执行脚本用 `exec(compile(...), {'__name__':'__main__','__file__':p})`（stdin 下没有 `__file__`）。

---

## 8. 验收标准

### A 类：单元测试（离线、确定性、可重复）

| 编号 | 判据 |
|---|---|
| A1 | `build_spoken_units()` 对同一输入两次调用逐字段一致（确定性） |
| A2 | **每一个单元的起止都不落在任何 VAD 语音区间内部**（允许误差 0；这是本层最强的安全断言） |
| A3 | 分句与语音块顺序严格保持：任意两个单元的文本拼接顺序 = 原分句文本顺序 |
| A4 | `speech_ranges` 为空 / 不合法 → 退回分块单元且 `degradations` 非空、不抛异常 |
| A5 | 单元数在真实样本上 **≥ 25**（对照：现状只有 2 个可取舍对象） |
| A6 | `normalize_options` 的 `duration_policy=proportional` 用父内容推出 `target_min/max`；`absolute` 保持旧值 |
| A7 | `pause_preset` 三档映射正确；显式 `guard/gap/min` 优先于预设 |
| A8 | 推荐层默认权重和为 1、序列为 `duration > foreign_speech > high_emotion > performance` |
| A9 | `same_subject` **不在** `recommendation_score` 的任何一项里（把它的值改成 0/1，综合分不变） |
| A10 | 四种排序模式的第 1 名符合定义（构造的夹具各自命中） |
| A11 | 首尾锚定：夹具里第一个含"你好"的单元成为片头、最后一个含"拜拜"的单元成为片尾；无命中时退回旧行为并给警告 |
| A12 | v1—v5 冻结计划仍可读、可重排、`validate_second_pass_plan` 通过（N7） |

### B 类：真实数据端到端（离线复算，零付费）

样本：`projects/cut-v2-accept`（`ISP-01488a7acc5a412a` 的父候选 `IEP-d796e02b01309214`，136.527 秒）。
复算脚本：`.backlot/_probe_v6_plan.py`（只读，不改项目，不触发任何付费调用）。

| 编号 | 判据 |
|---|---|
| B1 | **停顿压缩容量**：把父范围当作一个连续片段（任何选段可播放的上限），`pause_preset=tight` 的删减总量 **≥ 6.0 秒**、刀数 **≥ 18**，且显著高于 `conservative`（现状 2.84 秒 / 6 刀） |
| B2 | 全部刀口与 VAD 语音帧**相交 0 次**（沿用既有校验，不是新放宽的判据） |
| B3 | 以 `duration_policy=proportional` 生成的计划：`target_min ≥ 95`、`target_max ≥ 136`（按父内容 136.5 秒推导） |
| B4 | 单元层在该样本上产出 **≥ 25 个单元**；**第一个含"你好"的单元起点落在 12.0—62.0 秒内**，且最后一个含"拜拜"的单元即片尾 |
| B5 | **真实分组形态**（每 5 个单元一组，模拟模型被要求的"1—8 个单元一组"）：计划里 `removed_by_pause_seconds > 0`、`output_duration ≥ 100 秒`、`target_duration_status == within_target`、`content_qa == passed`（对照现状 53.6 秒） |
| B6 | `spoken_units` 已冻结进计划、`unit_source == "vad_aligned"`；重排（`_rebuild`）后逐字段一致 |

> B1 与 B5 是**两个不同的量**，不能混为一谈：B1 量的是这一层自身的容量；B5 量的是
> 真实分组下最终成片有多长。极端分组（每个单元一组）下"停顿压缩"会变小，因为单元之间的
> 间隙已经作为**整组切除**被删掉了 —— 输出仍然变短，只是记在"内容删减"而不是"压缩停顿"上。
> 两个数字都必须如实记录。

### C 类：长视频测试2 端到端（用户指定，付费）

素材 `C:\Users\Administrator\Downloads\直播回放-09月08日\直播回放-09月08日.mp4`
（**5333.03 秒 / HEVC 912×1920 / 1.72 GB**），项目 `long-video-test-2`（标题「长视频测试2」），
实现脚本 `scripts/accept_interaction_phrase_unit_v4.py`（`create|analyse|cut` 三段可续跑）。
机读报告：`.backlot/accept_phrase_unit_v4.json`。

| 编号 | 判据 | 实测 | 判定 |
|---|---|---|---|
| C1 | 项目建成、素材导入成功、时长与源文件一致 | `S-001`，5333.03 秒（流式上传 1638 MB，6.4 秒） | ✅ |
| C2 | 互动分析完成、事件数 > 0、无 `ambiguous` | `completed`，**27 条事件 / 27 条人工目录**，0 降级 | ✅ |
| C3 | 默认排序 = 优先时间长，排名 1..N 连续 | `order_mode=duration_desc`（优先时间长），权重 `40/25/20/15`，阈值 0.5，排名 1..27 | ✅ |
| C4 | 排名前 3 名各出一次完整切片 + 一条二次精剪 | 前 3 名中 **#1 因 202.0 秒 > 首次切片 180 秒合同被跳过并记录**；实做 #2/#3/#4 共 **3 条** | ⚠️ 见下 |
| C5 | 模型主动删除的语义组占比 ≤ 25 % | **0 % / 4.7 % / 4.6 %**（对照改前 55.6 %） | ✅ |
| C6 | 三条 `qa`/`content_qa` 通过、预览存在且非 0 字节 | 三条 `qa=passed`、`content_qa=passed`；预览 118.167 / 112.708 / 77.716 秒，H.264+AAC 608×1280，20–28 MB | ✅ |
| C7 | 付费调用与预检上限一致、无重复提交 | 预检 34 窗口 / 上限 35；视觉 34、ASR 一套分片、文本模型 **4 次**（其中 1 次因模型返回引用了未暴露的画面 id 而失败，重试 1 次，失败已落盘不自动重投） | ✅ |

三条成片（同一父范围口径）：

| 排名 | 素材 | 首次切片 | 二次精剪 | 父 → 成片 | 压缩停顿 | 目标 | 语音单元 |
|---|---|---|---|---|---|---|---|
| #2 | W007-E01（R0007） | `IEP-db22c092c8ccddd7` 149.637 s | `ISP-630092c9adefd6dc` | 149.637 → **118.153 s** | **10.184 s / 30 刀** | `within_target` | 51 |
| #3 | W022-E04（R0024） | `IEP-2c533ef365c8dc51` 135.998 s | `ISP-33f5aeb714c0e361` | 135.998 → **112.696 s** | 1.412 s / 6 刀 | `within_target` | 33 |
| #4 | W027-E01（R0027） | `IEP-5aaf931f2e9a4ebf` 137.735 s | `ISP-8af02752bff68fbd` | 137.735 → **77.716 s** | 1.784 s / 9 刀 | `outside_target` | 29 |

**对照改前**：同一条 `R0007` 通道，v5 计划是 136.527 → **53.593 秒**（内容删减 75.897 秒）；
v6 现在是 149.637 → **118.153 秒**（语义删除 9.485 秒 = 6.3 %，压缩停顿 10.184 秒）。

**C4 为什么是 ⚠️ 而不是 ✅**：`#1 W002-E02` 的互动范围是 **202.0 秒**，超过首次完整切片
"单个互动 ≤ 180 秒"的既有合同，服务端按规则拒绝。脚本**没有**偷偷换一条更短的素材充数，
而是把跳过原因写进报告并按排名顺序继续，最终产出 **3 条**（#2/#3/#4）。
这是排序（按时长优先）与切片合同（≤180 秒）之间的真实冲突，**不是本次改动引入的**，
也不应由本次改动悄悄放宽 —— 记在这里供产品决策。

**注意 `removed_source_seconds` 不能当"激进删减"读**：#4 的 40.5 % 里，模型只删了 1 个组（6.3 秒），
其余 55.8 秒是**父范围内本来就没有语音**（机器狗穿过人群步行、只有 50.1 秒对白）。
所以 C5 判的是**模型主动删除的语义组**，无语音区间单独报告。

### D 类：界面（结构断言 + 人工可见）

| 编号 | 判据 |
|---|---|
| D1 | 排名区是**单列**（`.interaction-rank-layout` 无左素材列）；每条卡片内部是 `切片 | 评分` 两栏，**不存在** `.interaction-material-column` / `.interaction-score-column` |
| D2 | 每张评分卡**同时**渲染排名徽标与素材编号（`interaction-rank-badge` + `素材编号 <event_id>`） |
| D3 | 排序选择器默认值 = `duration_desc`（优先时间长） |
| D4 | 权重面板四滑杆默认 40/25/20/15，改动后会带上 `order_mode`/`w` 参数重新取数 |
| D5 | 同一主体在卡片上以"素材要求 ✓/✗"呈现，且不出现"同一主体 xx 分"这类综合分项 |
| D6 | 每条排名的左侧切片**指向该条自己**：已出片用 `IEP-*/preview.mp4`，未出片用 `#t=start,end`；徽标写明来源 |
| D7 | 原素材只出现在排名区**上方的折叠块**「原片回看（与排名无关）」里，**不在**任何排名卡片的左栏 |

### 完成定义（DoD）

1. A 类全绿；2. B 类 6 条全过；3. C 类 7 条全过；4. D 类 5 条结构断言全过；
5. `tests/backlot` 全量回归 0 失败；6. `scripts/audit_context_handoff.py` 通过；
7. §12 回填真实数字，且**没有把没验的东西写成已验证**。

---

## 9. 风险与回退

| 风险 | 影响 | 回退 |
|---|---|---|
| 单元文本投影把短语配错位置（±1–2 秒） | 字幕仍是旧口径（`subtitle_cues` 继续用原始分句），**不受影响**；只是取舍与锚定的边界有偏差 | `UNITS_ENABLED` 开关：等价于 `speech_ranges` 为空 → 退回分块级 |
| 模型不接受"单元"这个概念，输出乱组 | `normalize_story_analysis` 的既有校验会拒绝（"不存在/连续/同一分句不能属于多个组"） | 提示词版本回退到 v2，计划版本仍可升 v6（单元只用于锚定） |
| `pause_preset=tight` 听感过紧 | 主观 | 预设可在界面切回 `standard`/`conservative`；`conservative` 与现状逐字节等价 |
| 目标时长比例放宽后成片变长 | 与用户诉求一致 | `duration_policy=absolute` + 手填秒数 |
| 推荐缓存 v1 → v2 失效 | 本地免费重算一次 | 无需回退；不触发付费 |
| 长素材分析耗时长/中断 | 验收无法完成 | 任务可续跑（既有恢复合同）；`_tmp` 级进度落 `.backlot/` |

---

## 10. 明确不在本次范围

- 不放松"三重许可"的任何一条（不与 VAD 相交 / 静音确认 / 单位时长下限）。
- 不做逐词强制对齐（不引入新的付费 ASR 或强制对齐模型）。
- 不改 `pause_visual`（局部画面门）与素材证据层的标定。
- 不做"自动发布/自动入库"；二次精剪终点仍是待审预览。
- 不改数字人/RunningHub 链路，本任务不产生该类付费。
- 不为 Linux 预发布背书。

---

## 11. 附：本次改动文件清单

**新增**

- `backlot/material_interaction_units.py`
- `tests/backlot/test_material_interaction_units.py`
- `scripts/accept_interaction_phrase_unit_v4.py`
- `docs/SINGLE_DEVELOPMENT_GUIDE_INTERACTION_PHRASE_UNIT_RANK_V4_ZH-CN.md`（本文）

**修改**

- `backlot/material_interaction_story.py`（单元上下文、提示词 v3、首尾锚定）
- `backlot/material_interaction_second_pass.py`（v6、`duration_policy`、`pause_preset`、`spoken_units`）
- `backlot/material_interaction_second_pass_candidates.py`（构建单元并纳入分析缓存签名）
- `backlot/material_interaction_recommend.py`（v2：四因子 + 要求 + 排序模式）
- `backlot/workbench.py`（探测下限透传、推荐层覆盖参数、载荷）
- `backlot/server.py`（读接口的 `order_mode` / `w` 查询参数）
- `backlot/ui/workbench.js`、`backlot/ui/workbench.css`（双列布局与权重面板）
- `tests/backlot/test_material_interaction_{recommend,story,second_pass}.py`
- `docs/handoff/{CURRENT_STATUS,CODE_MAP,INTERACTION_ROUGH_CUT_PLAN}.md`

---

## 12. 执行结果

> 本节只写跑过的数字。

### 12.1 现状核查阶段的实测（改动前基线，可复现）

`.backlot/_probe_second_pass.py` / `_probe_index2.py` / `_measure_pauses.py`（只读）：

- 冻结计划 `ISP-01488a7acc5a412a`（v5）：父 136.527 秒 → 输出 53.593 秒，
  `removed_source_seconds = 75.897`、`removed_by_pause_seconds = 1.678`（3 刀）。
- 该素材 ASR 只有 6 条分句，长度 **60.26 / 27.40 / 60.48 / 60.04 / 60.02 / 46.45 秒**。
- 语义层只有 2 个组：`G001`（U00002，27.4 秒，被 drop）、`G002`（U00003，60.6 秒，keep）；
  `U00001`（0—60.26 秒）因**不完整落在**允许区间 `[12.033, 148.56]` 被整条剔除 →
  前 48.1 秒从未进入模型视野且没有警告。
- 停顿探测栅格（同一父范围，未裁剪过的连续片段）：

| 探测下限 | guard | gap | 刀数 | 删减 |
|---|---|---|---|---|
| 0.45（现状） | 0.12 | 0.20 | 6 | 2.84 s |
| 0.45 | 0.06 | 0.12 | 14 | 5.72 s |
| 0.35 | 0.06 | 0.12 | 21 | 6.43 s |
| **0.30** | **0.06** | **0.12** | **23** | **6.90 s** |
| 0.30 | 0.05 | 0.10 | 28 | 8.19 s |

### 12.2 验收结果

**A 类（单元测试）**：全绿。新增 `tests/backlot/test_material_interaction_units.py`（21 项）、
`test_material_interaction_recommend.py` 重写（24 项）、`test_material_interaction_story.py` 增至 13 项、
`test_workbench.py` 增 6 项（排序视图、陈旧缓存重建、幂等键含派生版本、界面结构契约）。
其中 A2（**单元边界不落在任何 VAD 语音区间内部**）与 A3（单元文本是原转写的**顺序子序列**）
在真实样本与合成夹具上都断言为 0 违规；**锚定词表**另有专门用例覆盖"你好聪明"这类假命中。

**B 类（离线复算，`.backlot/_probe_v6_plan.py`，零付费）**：

| 编号 | 实测 | 判定 |
|---|---|---|
| B1 停顿容量（父范围当一个连续片段） | conservative **6 刀 / 2.84 s** → tight **23 刀 / 6.90 s**（2.37×） | ✅ |
| B2 刀口 ∩ VAD 语音 | **0** | ✅ |
| B3 比例目标窗口 | `95.569 — 136.527`（父 136.527 × 0.70/1.00） | ✅ |
| B4 单元层 | **38 个单元**；第一个含"你好" = `P0003 @ 17.521s`（文本"姐姐喂 你好 嗯"）；最后一个含"拜拜" = `P0038 @ 142.345s` | ✅ |
| B5 真实分组（每 5 单元一组） | 停顿压缩 **6.544 s / 19 刀**，输出 **110.88 s**（对照 v5 的 53.593 s），`within_target`，`content_qa=passed`，字幕 41 句 | ✅ |
| B6 单元冻结 | `unit_source=vad_aligned`、`spoken_units` 38 条随计划冻结、`validate_second_pass_plan` 通过 | ✅ |

**最坏分组（每个单元一组）**：`removed_by_pause = 1.073 s / 2 刀`、`removed_source_seconds = 21.723 s`、
输出 **103.392 s**。→ 输出照样短，只是删减记在"内容删减"而不是"压缩停顿"上；
这正是 §8 B1 与 B5 必须分开报的原因。

**D 类（界面结构断言，人工可见）**：

| 编号 | 实测 |
|---|---|
| D1 | 排名区单列；卡片内部 `minmax(180px,260px) | 1fr` 两栏；`interaction-material-column` / `interaction-score-column` 在 js 与 css 中**均已不存在** |
| D2 | 每卡首行同时给出 `第 N 名` 徽标与 `素材编号 <event_id>` |
| D3 | 排序选择器默认 `duration_desc`（优先时间长） |
| D4 | 权重面板默认 40/25/20/15；改动后带 `order_mode` + `w` 重新取数（实测 `duration_asc` → #1 变 7.9 秒） |
| D5 | 同一主体以"选材要求 ✓/✗"呈现，`reasons` 不含 `【选材要求】` |
| D6 | 每卡左栏是该条自己的切片：已出片 → `对应切片 · 完整切片 IEP-*`；未出片 → `对应切片 · 原片区间` + `#t=start,end` |
| D7 | 原素材只在排名上方的折叠块「原片回看（与排名无关，仅用于核对边界）」里 |

**真实接口验证**（`projects/cut-v2-accept` 与 `long-video-test-2`，免费）：

- v1 缓存被自动重建成 `material-interaction-recommend-v2`；默认 `duration_desc`（优先时间长）
  → cut-v2-accept 的 #1 是 W001-E01（136.0 s）。
- 长视频实测 `?order_mode=duration_asc`（优先时间短）→ #1 变成 7.9 s 的 W020-E01
  （默认 `duration_desc` 下 #1 是 202.0 s 的 W002-E02）→ 四种排序确实在真实数据上生效；
  `?w=duration:0,high_emotion:1,…` 也按预期重排且**不写缓存**。
- 界面结构断言直接跑在服务端返回的 bundle 上（`GET /ui/workbench.js`）：两个列容器、
  排名徽标 + 素材编号、默认 `duration_desc`、权重 40/25/20/15、选材要求徽标、
  目标时长策略与停顿强度两个新开关，全部命中（同一组断言也在 `test_workbench.py` 里固化）。

**回归**：`tests/backlot` 全量 **1523 passed / 2 skipped / 0 failed**（约 190 s，须带
`CODEBUDDY_SAFE_DELETE_ENABLED=0`，否则会在 teardown 清理临时目录时被沙箱批量删除保护
**静默打断**，表现为跑到 ~42% 就没了）。

### 12.3 实施中发现并修掉的新缺陷

1. **长短句同粒度的第二处后果**：`story_utterances()` 的 `_containing_range()` 让跨界分句
   **整条消失且无警告**。单元层用"语音块与允许区间求交"取代它，
   并把 `spoken_units_partial_text`（有语音无文本的单元）写成显式降级项，强制保留。
2. **比例策略会覆盖人工秒数**：`duration_policy` 起初无条件生效，导致显式传
   `target_min/max` 的旧调用被改写（`test_over_maximum_plan_keeps_preview_but_blocks_approval`
   直接翻车）。改为"raw 里出现显式秒数即推断为 `absolute`"。
3. **`rank_events()` 排序了副本却返回原顺序**：`payload["events"]` 用的是未排序的 `rows`，
   于是 `rank` 来自一个排序、位置来自另一个。改为返回 `ranked`。
4. **v6 计划的分组文本会全部为空**：`workbench._public_interaction_second_pass_candidates` 只从
   `plan["utterances"]` 建查找表，而 v6 的组引用 `P…`。改为两个 id 空间合并查找。
5. **字幕归属需要 id 桥接**：`subtitle_cues` 按 `utterance_id` 取原始分句，若直接喂 `P…` 会归零。
   新增 `_caption_utterance_ids()`，字幕仍按**原始 ASR 分句**生成（不拿单元文本当字幕，
   否则 6 秒单元会变成一整行长句）。
6. **探测下限没跟预设联动**：只改计划层参数时 `min_silence` 还停在 0.45，压缩量上不去。
   现在 `workbench` 用 `pause_probe_min_silence(options)` 传参，且 `min_silence` 进探测身份签名
   （两套证据共存，不互相覆盖）。
7. **版本门控漏项**：`SUBTITLE_SENTENCE_AWARE_VERSIONS` 等集合原本引用 `VERSION`，
   升到 v6 后会**把 v5 排除在外、悄悄放宽校验**。改为逐版本显式列出。
8. ★★ **首尾锚定的词表把"你好聪明"当成了开场词**（真机验收抓到）：旧实现是**子串匹配**，
   于是 G003 的"你好聪明 你好聪明"被判为片头锚点，**前 32 秒被整段裁掉** ——
   比不锚定更糟。修法：按 **ASR 自己的空格/标点切出短语**，只有"整词相等、或仅多一个字、
   或只多重复字（拜拜拜拜）、或只多语气词（呀/啊/啦…）"才算命中；同时补 `你们好/大家好/各位好/姐姐好`。
   实测：长视频那条从 92.838 秒回到 **118.153 秒**。
9. ★ **`trim_head`/`trim_tail` 的旧语义会强制删掉模型主张保留的开场组**（13 秒的
   `greeting` 组即使模型写了保留理由也被 `drop`）—— 这正是用户说的"掐头去尾太狠"。
   现在这两个开关只表示"允许按明确开场/告别词锚定"，找不到词就不动。
10. ★ **画面证据校验把"被截断"当成"编造"**：`visual_evidence.frames` 全局上限 12 条，
    而事件的 highlights 各自带着自己的 frame_id；模型引用了后者 → 被判定"引用了不存在的
    画面证据" → **整次付费分析作废**（3 条里废掉 1 条）。修法：校验集合改成
    "模型实际看到的全部 id"（frames ∪ pause_windows ∪ events[].evidence_frame_ids ∪
    events[].highlights[].frame_id）；编造的 id 仍然被拒（测试保留）。
11. ★ **幂等键不含派生版本 → 改了算法还会返回旧计划**：二次精剪任务签名只含
    `asset_id + request`，已完成的任务会被直接返回。结果是：同一条素材在修好锚定后
    **仍然返回旧的（错误锚定的）计划**，另外两条才被重建。修法：签名升 v2，
    并入 `story/units/plan` 三个版本号（`derivation` 字段）。
12. **公开载荷缺 `removed_by_pause_seconds`**：计划里一直有，投影时漏了 → 界面
    "压缩停顿 X 秒"恒显示 0.0，验收脚本直接 KeyError。已补（并补 `played_source_seconds`）。
13. **两条既有字幕渲染用例的预期需要跟着新契约更新**（不是缺陷，是默认值改变的必然结果）：
    `test_subtitles_are_burned_and_the_file_matches_the_cues` 与
    `test_subtitling_can_be_turned_off_without_a_degradation` 原本断言"开场白被丢弃 → 2 条字幕"，
    而新契约下 `大家好。` 是**片头锚定词**所在组，会被保留 → 3 条。
    改的是**预期**并写明理由，**没有**为了让用例变绿去回退默认值。
14. ★★ **界面第一版做错了方向，用户当场否掉**：我把"左素材 / 右评分"读成"左列常驻原片播放器、
    右列评分列表"，于是用户看到的仍然是**一个原素材 + 一串与它无关的评分卡**，
    而且原素材还占了排名左侧。正确读法是"**每条排名自己左切片、右评分**"：
    排名区单列从上到下，卡片内部才分左右，左栏播**这一条自己的切片**（已出片用 `IEP-*/preview.mp4`，
    未出片用 `#t=start,end`），原素材移到排名上方一个折叠块。
    教训：**用户说"左边是对应剪辑素材"时，"对应"是判据，不是修辞**；
    界面对不上先回头读原话，别急着加可视性。

### 12.4 本次未做（与原计划一致）

- 没有放松三重许可的任何一条（B2 的 0 相交仍是结构性保证）。
- 没有引入词级强制对齐（不新增付费 ASR/对齐模型）。
- 没有改 `pause_visual` 局部画面门与素材证据层的标定。
- 没有自动发布/自动入库；二次精剪终点仍是待审预览。
- 没有为 Linux 预发布背书。

---

## 13. 第二轮（2026-09-14）：间隙、字幕、交付物

用户在同一条链路上又提了五条反馈。本节记录这五条的目标、实测、改动与验收。

### 13.1 五条反馈与实测现状

| # | 用户原话（要点） | 实测现状 |
|---|---|---|
| G1 | "二次精剪不应该分开的，可以在每个素材下面有一个点击展开的下拉部分" | 二次精剪候选当时排在页面**最底部**，与它所属的素材行相隔一整块候选列表 |
| G2 | "字幕和实际会话还是没有一一对应上，字幕会慢不少，话都说完了字幕才出来" | 字幕时间来自**整条 ASR 分块**（60 秒宽）按字数比例估算：夹具实测 5 句只产出 **1 句**，且落在错误位置（应在 0.15—4.05 秒的两句，被算成 0—2.30 秒的一句「我很好」） |
| G3 | "长篇分析…不需要列怎么长的字幕出来给人看" | 每张二次精剪卡片把**每个语义组的逐句对白**平铺出来（截图里整屏都是"1012.63秒 你们好/1015.26秒 姐姐 姐姐…"） |
| G4 | "前后对话间隙压缩还是几乎看不出来…希望压缩到固定的 0.3 秒" | v6 成片实测：65 处段间空隙里 **25 处 >0.3 秒**（合计 16.70 秒），最大 1.64 秒；根因见 13.2 |
| G5 | "应该提供没有字幕的二次精剪视频和一份携带字幕时间的字幕文件" | 只有烧入字幕的预览；`export` 只导出剪辑清单（cut-list/FCP7/OTIO/SRT），**没有一条"拿得走"的干净成片 |

### 13.2 G4 的根因（两段实测，不是推断）

**第一段：计划层的几何让 0.3 秒根本不可达。** 旧公式"每刀固定开销 = `2×guard + gap`"，
把 `pause_target_gap_seconds` 当成"中间保留的呼吸"，两侧 guard 再各自叠加 → 想留 0.3 秒
就要 `2×0.06+0.3 = 0.42` 秒。**v7 把语义改成「相邻两句之间实际保留的间隙」**：
`keep = max(guard, gap/2)`，guard 只作下限不再叠加。

**第二段：探针看不见真实间隙。** 该素材（R0007，父 149.6 秒）有 **68 处 VAD 间隙、合计 50.8 秒**；
在标定的 −40 dB 下探针只看到 **23.4 秒**，因为 `silence_intervals` 要求**连续**静音帧，
街头素材的多秒间隙里往往夹着一两帧噪声（车、路人、电机），一帧就把整段静音打断。
按"固定 0.30 秒"复算，源侧可删量从 **21.5 秒**（不桥接）升到 **24.5 秒**（桥接 0.10 秒）。

**第三段（决定默认策略的一条）**：v7 用「只压真静音」跑完三条素材后，成片里仍有 10 处
>0.55 秒的空隙（合计 9.09 秒），它们的**95 分位电平是 −17.9…−29 dB**，而这段素材的
**语音帧中位电平是 −22.3 dB** —— 也就是说这些间隙不是死气，而是**电机声、车流和人群**，
比一半的对话还响。**把 0.3 秒贯彻到底就必须连环境音一起切。**

### 13.3 决策

1. **G1**：二次精剪不再单独成块，改成每条素材行下的**两层折叠详情**
   （`第一次完整切片：详情与操作` / `二次精剪（N 条，点击展开）`）；
   候选的"切片详情"筛选一并移进排名工具条。页面底部原来的两块列表整体删除。
   ★ 一个素材会有多版方案（每改一次算法/选项就留一版），所以行内**只铺最新一版**，
   更早的收进「更早的方案（N 条）」——否则一个素材下面挂四张卡，又回到用户抱怨的混乱。
2. **G2**：计划升 **v7**，字幕的锚点从"整条 ASR 分块"换成**语音单元**（2—6 秒、VAD 对齐）。
   v5/v6 计划仍按旧口径派生（`UNIT_CAPTION_AWARE_VERSIONS` 门控），历史计划逐字节可读。
3. **G3**：逐句对白收进 `interaction-story-transcript` 折叠块，语义分组整块收进
   `interaction-story-wrap`；证据都在，默认不再铺满屏幕。
4. **G4**：三个旋钮一起动 —— 几何（13.2 第一段）、探针（桥接 + 下限 0.20 秒）、
   **间隙压缩范围** `gap_policy`：`any`（默认，连环境音一起压，VAD 间隙直接作候选，
   不需要静音证据）/ `quiet`（旧三重许可，一键可切）。
   两者都**只**在 v7 生效，v1–v6 一律按 `quiet` 复现。
5. **G5**：新增交付物导出 —— 同一份计划把 `burn_subtitles` 关掉**再本地渲一次**
   （字幕决策本来就在渲染签名里，所以落在自己的目录，**绝不覆盖已审看的预览**）+ 输出时间轴的
   `.srt`；接口 `POST .../second-pass/{plan_id}/deliverables`，卡片上给两个下载入口。

### 13.4 任务分解

| 任务 | 文件 | 要点 |
|---|---|---|
| T1 几何 | `material_interaction_second_pass.py` | `_pause_keep_seconds` + `GAP_TARGET_VERSION=v7` + 逐版本门控 |
| T2 探针桥接 | `material_pause_evidence.py` | `bridge_seconds` 进身份签名；`bridge_intervals` |
| T3 间隙范围 | `material_interaction_second_pass.py` | `_candidate_pieces`（`quiet`/`any`）+ `gap_policy` 选项 |
| T4 字幕锚点 | `material_interaction_second_pass.py` | `subtitle_cues` 的 atom 源按版本切换 |
| T5 交付物 | `workbench.py` / `server.py` | `export_asset_material_interaction_second_pass_deliverables` + 新路由 |
| T6 界面 | `ui/workbench.js` / `.css` | 行内折叠详情、对白墙折叠、间隙范围开关、下载入口 |
| T7 测试与文档 | `tests/backlot/*`、`docs/handoff/*`、本文件 | 见 13.5 |

### 13.5 验收标准

| 编号 | 判据 |
|---|---|
| V1 | 二次精剪卡片只出现在它所属的素材行内部；页面底部**不再**有独立的候选/二次精剪列表 |
| V2 | 对白墙与语义分组**默认收起**（`<details>`），展开后内容完整 |
| V3 | v7 字幕锚定在语音单元上：夹具（60 秒分块 + 2 秒单元）给出 `0.15–2.15 你好 你好吗` 与 `2.45–4.05 我很好 谢谢你 再见`；同一计划按 v6 复算仍只有 1 句 |
| V4 | `pause_target_gap_seconds` **就是**成片里相邻两句之间保留的间隙：`pause_survivor_seconds()` 在默认预留下等于 **0.30**；guard 大于 target/2 时取 guard |
| V5 | v1–v6 冻结计划（含磁盘上的真实 v4/v5 计划）读取即通过，`pause_trims` 逐字节一致 |
| V6 | 默认 `gap_policy=any` 时，**不需要静音证据**也能压缩（无证据不报降级）；`quiet` 仍需要证据并报 `pause_compression_disabled` |
| V7 | 交付物导出产出「无字幕成片 + SRT」两个文件，且**不覆盖**审看过的带字幕预览 |
| V8 | 全量 `tests/backlot` 全绿；`scripts/audit_context_handoff.py` 全 OK |

### 13.6 执行结果

见 §13.7（完成后回填）。

### 13.7 实测结果（2026-09-14）

**G2 字幕锚定**（夹具实测，`test_v7_captions_are_anchored_on_the_spoken_unit_not_the_whole_asr_block`）：

| 版本 | 产出字幕 |
|---|---|
| v7（语音单元 2 秒） | `0.150–2.150 你好 你好吗` · `2.450–4.050 我很好 谢谢你 再见` |
| v6（ASR 分块 60 秒） | `0.000–2.300 我很好` ← **两句丢失、剩下的一句还落在错误时间** |

**G4 间隙压缩**（三条素材各跑三版，段间空隙在**输出时间轴**上由 VAD 语音段实测；
按"语音段 ∩ 实际播放区间"裁剪后再计空隙，避免把跨界段误算成一个大空隙）：

| 素材 | 版本 | 成片 | 空隙数 | 中位 | 最大 | **>0.30 秒** |
|---|---|---|---|---|---|---|
| R0007 | v6 | 118.153 s | 65 | 0.273 s | 1.636 s | 25 处 / 16.70 s |
| R0007 | v7 + `quiet` | 116.687 s | 65 | 0.276 s | 1.585 s | 26 处 / 15.25 s |
| R0007 | **v7 + `any`** | **108.425 s** | 65 | 0.273 s | **0.273 s** | **0 处 / 0.00 s** |
| R0024 | v6 | 112.696 s | 37 | 0.291 s | 0.989 s | 18 处 / 9.66 s |
| R0024 | v7 + `quiet` | 112.244 s | 37 | 0.284 s | 0.989 s | 16 处 / 8.59 s |
| R0024 | **v7 + `any`** | **108.242 s** | 37 | 0.273 s | 0.524 s | **1 处 / 0.52 s** |
| R0027 | v6 | 77.716 s | 37 | 0.495 s | 3.898 s | 24 处 / 34.49 s |
| R0027 | v7 + `quiet` | 76.357 s | 37 | 0.495 s | 3.898 s | 24 处 / 33.07 s |
| R0027 | **v7 + `any`** | **55.495 s** | 38 | 0.273 s | **0.273 s** | **0 处 / 0.00 s** |

**结论**：`gap_policy=any` 下**相邻对话间隙实测落在 0.273 秒**（目标 0.30 秒减去半个包络 hop 的
测量粒度），三条素材里只有 R0024 残留 **1 处 0.52 秒**（处于片段边界、可删量刚好不达阈值）——
**不能写成"处处 0.30"**。同一次分析下 `any` 相对 `quiet` 多压掉的量：
R0007 **20.2 s / 35 刀**、R0024 4.536 s / 14 刀、R0027 43.668 s / 26 刀；
成片分别从 116.687→**108.425 s**、112.244→**108.242 s**、76.357→**55.495 s**。
**模型主动删除的语义组没有因此变大**，多出来的全是间隙。

**G5 交付物**（真实调用一次）：

| 产物 | 路径 | 实测 |
|---|---|---|
| 无字幕成片 | `renders/ISP-3c32e8fc272e4b60/44d36e3b…/preview.mp4` | 108.425 s / 18.2 MB，**目录里没有 `subtitles.srt`** |
| 字幕文件 | `export/ISP-3c32e8fc272e4b60.srt` | 47 句、3.1 KB，`00:00:00,136 --> 00:00:01,067 哈 你们好` |
| 已审看的带字幕预览 | `renders/ISP-3c32e8fc272e4b60/cfaf83e8…/`（含 `subtitles.srt`） | **原样保留**，导出没有覆盖它 |

**回归**：`tests/backlot` 全量 **1528 passed / 2 skipped / 0 failed**（302 s）。
第一次全量跑时 `test_workbench.py::test_task_center_and_ppt_card_endpoints` 失败过一次
（PPT 卡片任务没出现在任务中心），**单跑与复跑全量均通过** → 判定为与本次改动无关的
时序 flake（任务中心的 worker 启动时机），**记录在案，没有改测试去掩盖它**。

> ⚠️ **`any` 的代价必须说清**：它连**环境音**一起切。这条素材里 10 处 / 9.09 秒的空隙
> 电平在 −18…−29 dB（电机、车流、人群），切完这些声音会明显变"跳"。
> 界面上保留了「只压真静音（三重许可）」一键切换；`PRODUCT_RULES.md` 已同步记录默认变更；
> 计划里每一刀的 `reason` 都标了「该段不是静音（连环境音一起压）」，可逐刀复核。
