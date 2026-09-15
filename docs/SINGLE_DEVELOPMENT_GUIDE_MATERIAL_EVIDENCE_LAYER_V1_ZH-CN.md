# 单次开发指导文档：本地素材「证据层」一次解码 + 全片运动时间线 + 可复用证据文档 V1

更新时间：2026-09-12
状态：待执行（本文件是本次开发的唯一依据，执行中不得偏离）
试点项目：`local-material-understanding-test` / 资产 `S-001`（88.9 分钟竖屏直播回放）
上游输入：`docs/ANALYSIS_AUTOEDITOR_MOVIEPY_2026-09-12_ZH-CN.md`（本次范围锁定 **P0-A + P0-B + P0-C**）
格式范本：`docs/SINGLE_DEVELOPMENT_GUIDE_INTERACTION_PAUSE_RECOMMEND_V3_ZH-CN.md`

---

## 0. 一页摘要（先读这段，它决定了后面所有判据的形状）

本次做的是**素材理解的证据层**：把「音频静音」「画面运动」这两件现在靠**逐段 spawn ffmpeg**、
且**只在少数抽样点上算**的事，改成**一次解码 → numpy 在内存里算全片**，并把这四类证据
（音频包络 / 静音 / 语音 / 运动 + 转写时间轴）合成一份**带版本、可缓存、多阶段复用**的旁路产物。

**本次开发必须带走的两个实测事实（与分析文档的预期不同，以此处为准）：**

| # | 分析文档说 | 实测 | 结论 |
|---|---|---|---|
| 1 | 「88.9 分钟 25 条候选 = **75 次**进程启动」「收益是**速度数量级**」 | 25 条候选的 `keep_ranges` 合计 **27** 条（每条候选 1 或 3 段）→ 一轮探测 = **27 次** spawn，实测 **2.19s**；单次全片解码 = **3.57s**，包络计算 **0.81s** | **单轮探测的墙钟时间不会变快，反而略慢（2.19s → ~4.4s）**。本次的速度收益落在 ①进程启动数 27→1、②**重复调阈值近乎零成本**（实测 7 组阈值 3.09ms，对比现在 7×27=189 次 spawn ≈ 15.3s，**≥700×**）、③跨阶段复用。**不要**把「单轮耗时下降」写成验收目标 —— 那是不可能达成的错标。 |
| 2 | 「一次解码成 16k 单声道 → numpy RMS 包络」即可替代 `silencedetect` | 同名阈值下 numpy 单声道 RMS(−30dB) 比 `silencedetect(noise=-30dB)` **多判 2.49 倍**静音（181.3s vs 72.9s，IoU 0.36–0.51）；跑真实计划层规则后**删减量 142.2s vs 81.0s = 1.76×** | **两者不是同一个度量**。直接用会**静默把停顿压缩的力度翻倍**，并让 95 条静音区间中的 73 条与 VAD 语音帧相交（危险方向）。必须**标定**：实测 `numpy 单声道 RMS / 20ms 窗 / 10ms 跳 / −40dB` 可复现 `silencedetect −30dB` 的操作点（计划层 81.5s / 136 刀 vs 81.0s / 143 刀，**+0.6% / −4.9%**）。 |

**一句话验收口径**：本次开发**不追求更快的一轮探测**，追求的是
「**一次解码，之后随便调参、随便复用**」，且**新探测器在真实素材上的删减量必须落在旧探测器的 ±10% 内**。

---

## 1. 本次开发目标

把本地素材理解从「**逐段起进程、按抽样点判断、每个阶段各自重算**」改成
「**一次解码、全片连续、一份产物多阶段复用**」：

- **目标 A（P0-A 音频证据层一次解码）**：`backlot/material_pause_evidence.py` 现在对每个
  `range` 各起一次 `ffmpeg … silencedetect`。改为**一次解码**（`-ac 1 -ar 16000 -f s16le`）
  → numpy **全片 RMS/峰值包络**（20ms 窗 / 10ms 跳）；静音判定与能量护栏改在内存中完成。
  **阈值必须可以反复调而不重新解码**。
- **目标 B（P0-B 全片运动时间线）**：用本地 ffmpeg 抽 **96×54 灰度 @2fps** 全片帧 + numpy diff，
  产出**全片连续运动分**（实测 10666 个采样点，零 API 成本），替代现在
  `plan_pause_visual_windows` 的「**全片 12 窗口全局预算**」——该预算实测**只覆盖 3/28 个事件**，
  且**判决不稳定**（见 §2.4）。
- **目标 C（P0-C 可复用素材证据文档）**：把**音频包络 + 静音 + 语音 + 运动 + 转写时间轴**
  合成一个**带版本、可缓存、多阶段复用**的产物，供素材理解 / 粗剪 / 精剪共用。
  必须是**新增旁路产物**，**不得改动 `material-interaction-index.json` 的 `signature`**
  （改了会触发 34 个窗口的付费视觉调用重复计费）。

本次开发**不新增任何付费调用**（视觉 0 次、ASR 0 次），**不改渲染语义**，**不改识别提示词**。

---

## 2. 现状与缺口（每条带文件与行号证据）

### 2.1 缺口 A1：静音探测逐 range 起进程，且调一次参就要重算一次

| 事实 | 证据 |
|---|---|
| 每个探测范围各起一次 ffmpeg | `backlot/material_pause_evidence.py:156-179`（`probe_range`，命令行在 `:161-166`），`-ss` 逐段 seek |
| 主循环逐 range 顺序 spawn | 同文件 `:240-248`（`for row in rows: probe_range(...)`） |
| 缓存键含 `identity`（阈值 + 最短静音）→ **换阈值 = 换缓存 = 重算** | 同文件 `:82-92`（`runtime_identity`）、`:228-229`（key 含 `identity["signature"]`） |
| 阈值只有两个硬编码默认 | 同文件 `:42-46`（`DEFAULT_NOISE_DB=-30.0`、`DEFAULT_MIN_SILENCE=0.45`） |
| **第二份重复实现**：素材层的静音探测也是逐区间 spawn | `backlot/material_audio_evidence.py:163-193`（`detect_silence`，`noise_db=-38.0` 默认） |

**实测（S-001）**：25 条候选的 `keep_ranges` 合计 **27** 段，一轮探测 **27 次** spawn / **2.19s**
（原片 1.72GB 上 **2.14s**）；单次全片 16k 单声道解码 **3.57s**（原片 **3.79s**）+ 包络 **0.811s**。
→ **单轮不省时**；但 7 组阈值标定 = 189 次 spawn ≈ **15.3s**，而一次解码后 7 组阈值合计 **3.09ms**。

### 2.2 缺口 A2：内存包络与 `silencedetect` **不是同一个度量**（本次最大的技术风险）

同名阈值下的实测对比（6–8 条真实 range，方法见 §8「标定探针」）：

| 探测器 | 相对 `silencedetect(−30dB)` 的检出量 | 与 `silencedetect` 的区间 IoU |
|---|---|---|
| numpy 单声道 RMS，20ms 窗/10ms 跳，−30dB | **2.49×**（181.3s vs 72.9s） | **0.36–0.51** |
| 同上，窗 40/60/100/200/400ms | 2.62× / 2.77× / 2.89× / 2.95× / 2.84× | 0.30–0.36（**加长窗不解决问题**） |
| 同上但**立体声逐声道平均绝对值求和**（最接近 ffmpeg 度量的一种猜测） | 1.33–2.23× | 0.45–0.75（最接近，但仍不足） |
| numpy 单声道 RMS −36dB | 1.37× | — |
| **numpy 单声道 RMS −40dB（本次选定的操作点）** | **0.93×**（77.8s 级） | **0.822（min 0.593）**，对 `silencedetect` 区间覆盖率 **0.854（min 0.595）** |

**跑真实计划层规则（guard 0.12 / gap 0.20 / min 0.40，绕开发声点）后的删减量：**

| 探测器 | 刀数 | 删减 | 倍率 |
|---|---|---|---|
| `silencedetect(−30dB)`（当前生产路径） | **143** | **81.0s** | 1.00× |
| numpy RMS −30dB（**若直接照搬分析文档**） | 222 | 142.2s | **1.76×** |
| numpy RMS −36dB | 174 | 99.1s | 1.22× |
| **numpy RMS −40dB（本次选定预设）** | **136** | **81.5s** | **1.01×** |

> 该 143 刀 / 81.0s **与 V3 已发布验收结果逐字吻合**（"143 刀、81.0s、22/25 条有削减"），
> 说明这套离线复算口径是可信的、可用来做回归对比的。**实现者必须用它做 A2 判据。**

**为什么必须标定而不是「相信 dB 数字」**：计划层第 ② 许可（`_pause_trims` 的
`material_interaction_second_pass.py:252-341`）依赖 `silencedetect` 的**保守性**来排除
笑声/环境音/落地物；第 ① 许可（不与 VAD 语音帧相交）是硬门。实测在 −30dB 下，
**186 条静态区间里有 178 条与 VAD 语音帧相交**（−40dB 下 95 条里 73 条）——
直接照搬会让这些区间被剪切或（经 `_subtract` 后）碎成大量不足以过 `min_pause` 的碎片，
**静默地改变已验收的删减行为**。

### 2.3 缺口 B1：视觉许可的「全片 12 窗口预算」只覆盖 3/28 个事件

| 事实 | 证据 |
|---|---|
| 预算是**全片全局**的，不按事件续期 | `backlot/material_interaction_refinement.py:136-165`（`plan_pause_visual_windows`），`:163` 的 `[:int(max_windows)]` |
| 常量 `PAUSE_VISUAL_MAX_WINDOWS = 12`、每窗 ≤2s | 同文件 `:17-23` |
| 每窗各起一次 ffmpeg 抽 96×54 灰度帧 | 同文件 `:197-206`（`-ss/-to` + `fps/scale/format=gray` → `rawvideo`） |
| 判定用**窗口内最大运动** | 同文件 `:215-219`（`max_motion <= PAUSE_VISUAL_LOW_MOTION(0.035)`） |
| 结果被冻结进候选计划（`pause_visual` 字段） | `backlot/material_interaction_candidates.py:104-129`（cache key 含 `index_signature`）；25 条候选的 `interaction-edit-plan.json` 均含 `pause_visual` |

**实测（S-001，缓存 `interaction-candidates/pause-visual/0780c1a425603d7edf00.json`）**：
12 个窗口（PV001–PV012，84 帧），**只落在 3 个事件**里：`W005-E01 / W011-E01 / W027-E01`
（28 个事件中的 **3/28 = 10.7%**）→ **其余 25 个事件永远拿不到画面许可**，这不是内容判定，是预算抽签。

### 2.4 缺口 B2：2 秒抽样点的判决**不稳定**（同一处停顿换个抽样点就翻案）

用本次已实测的全片运动时间线回看那 12 个窗口（±5s 邻域）：

| 窗口 | 窗口内 max | ±5s 邻域 max | 现判决 | 问题 |
|---|---|---|---|---|
| PV003 | 0.0000 | **0.1783** | waiting（可缩短） | 邻域内有明显活动，抽样点漏掉了 |
| PV009 | 0.0006 | **0.1097** | waiting | 同上 |
| PV011 | 0.0005 | **0.1851** | waiting | 同上 |
| PV005 | 0.1369 | 0.1652 | action（保留） | 邻域确实持续有活动 |

→ 同一个停顿「能不能缩短」取决于**恰好抽到哪 2 秒**，这是**采样脆弱性**，不是证据。
全片连续时间线同时修掉 2.3 的覆盖率缺陷和本条的不稳定缺陷。

### 2.5 缺口 C1：没有「证据文档」这个中间产物，每个阶段各自重算

| 事实 | 证据 |
|---|---|
| 转写（付费）缓存独立成文件，且以引擎策略为键 | `backlot/material_interactions.py:523`（`interaction-asr/<digest>.json`）；`backlot/material_audio_evidence.py:63-74`（`cache_identity`） |
| VAD 证据按**候选范围**逐条缓存（不覆盖全片） | `backlot/material_interaction_candidates.py:98-101`（key = `fingerprint:start:end`）、`:132-161`；实测 **25** 个缓存 / **660** 条语音区间 |
| 静音证据按 `identity` 缓存（换阈值即失效） | `backlot/material_pause_evidence.py:226-236` |
| 运动证据按 `index_signature` 缓存（换索引即失效） | `backlot/material_interaction_candidates.py:106-112` |
| 素材理解阶段另有一套活动采样（场景切换/关键帧） | `backlot/material_overview.py:91`（`duration_policy`）、`:242-278`（`_scene_activity_times` / `_keyframe_activity_times`）、`:463`（`overview-v1/<sig>`） |

→ 同一份素材被 4 个模块、5 条缓存策略各测一遍。**没有一份「这段素材在音频和画面上到底长什么样」的权威产物。**

### 2.6 缺口 C2：索引签名是付费缓存的键，只能旁路

| 事实 | 证据 |
|---|---|
| 索引 `signature` 由源指纹 + 模型身份 + ASR 身份 + 策略 + profile + 版本算出 | `backlot/material_interactions.py:511-512` |
| 缓存命中只看**文件存在 + status=completed** | 同文件 `:515-519` |
| 二次精剪用 `index_signature` 判「素材是否变过」 | `backlot/workbench.py:16835-16836` |
| 付费调用按 `request_signature` 落账 | `backlot/material_interactions.py:442-461`、`:568-569` |
| 已有先例：推荐层走旁路 `recommendations.json`，绝不改索引 | `docs/..._PAUSE_RECOMMEND_V3_...md` §3 决策 7 |

→ **证据文档必须放在独立目录**（`material-evidence/<signature[:20]>/`），且**索引文件的
内容哈希在本次开发前后必须不变**（判据 C2）。

### 2.7 缺口 D1：依赖自检里没有「证据层」

`backlot/workbench.py:16660-16709`（`_interaction_dependency_preflight`）已检查
文本模型 / VAD 运行库 / numpy / ffmpeg / 审核代理，但**没有**「包络可用性」「运动时间线可用性」
「证据文档是否命中/降级」。新增证据层必须**顺带扩这两项**，且遵守既有铁律：
**读路径可刷新，变更路径只读快照**（`:16670-16680`，否则会给「确认入库」这类原本不需要 FFmpeg 的路径增加依赖）。

---

## 3. 架构思路与关键设计决策

### 决策 1：包络与静音解耦 —— 缓存的是**包络**，不是**静音区间**

```
一次解码（1 次 spawn）
        └─► 全片 16k 单声道 int16 → numpy
                ├─► RMS / 峰值包络（20ms 窗 / 10ms 跳）  ← 缓存对象（唯一贵的东西）
                │        └─► 阈值函数 f(threshold_db, min_silence) ─► 静音区间   ← 纯函数，毫秒级
                └─► 峰值包络（用于能量护栏：防削波/防近满刻度段误判）
```

**为什么**：调阈值之所以现在要重算，是因为缓存键里塞了阈值（`material_pause_evidence.py:228-229`）。
把「贵的解码」与「便宜的判定」分开之后，**静音区间变成包络的纯函数**，
换阈值 / 换最短静音 / 换 dB 语义都不再触发任何 I/O。

**替代方案与其否决理由**：
- *(a) 直接把解码结果落盘成 WAV 再读* → 88.9 分钟 16k 单声道 = 171MB；包络只需 **0.97MB**（int16 npz）。**否决**。
- *(b) 把包络挂到 VAD 的分块解码上顺带算* → VAD 只解码**候选范围**（实测 1489s / 全片 5333s = 28%），
  且分块是 60s 带重叠（`material_speech_activity.py:221-254`），**天然盖不住全片**，还会把两份证据的
  边界耦合在一起。**否决**（但保留为「只要候选范围证据」时的可选优化，见 T4 备注）。
- *(c) 保持现状只加缓存* → 不解决调参成本，也产不出 P0-C 需要的全片产物。**否决**。

### 决策 2：新探测器必须**标定到旧探测器的操作点**，并用**计划层结果**验收（不是用 dB 数字）

按 §2.2，`numpy` 单声道 RMS **不等于** `silencedetect` 的度量。本次规定：

- **出厂预设**：`sample_rate=16000, channels=1, window_ms=20, hop_ms=10, metric="rms",
  threshold_db=-40.0, min_silence=0.45`（该组合实测复现 `silencedetect −30dB` 的计划层删减量 +0.6%）。
- **验收不看 dB 数字相等，看三件事**（全部离线可算）：
  1. 计划层删减总量落在旧路径的 **±10%**（旧基线 81.0s → 允许 72.9–89.1s）；
  2. 刀数落在旧路径的 **±12%**（143 → 允许 126–160）；
  3. 区间级：与 `silencedetect` 的 **mean IoU ≥ 0.75** 且对 `silencedetect` 区间**覆盖率 ≥ 0.80**；
  4. **硬门**：任何 trim 不得与 VAD 语音帧相交（0 次，与 V3 同）。
- **两条路径都必须保留**（`MATERIAL_EVIDENCE_AUDIO_BACKEND = auto | envelope | ffmpeg`）：
  `auto` = 能算出包络就走包络，否则退回 `ffmpeg`。**回退不是降级事故，是有记录的正常分支**。

**为什么不用「立体声逐声道绝对值求和」那种更接近 ffmpeg 的度量**：实测它确实更接近（IoU 0.45–0.75），
但需要在包络层保留声道数（内存 ×2，且与 VAD 的单声道解码分道扬镳），
换来的是「还得再标定一次」——**不如直接标定单声道 RMS**：简单、可解释、且已验证命中 ±1%。

**替代方案**：*只做交互式调参、最终判定仍回 `ffmpeg silencedetect`*（保守的混合方案）。
**不作为本次默认**，但写进回退路径：若 A2 标定三次仍无法进入 ±10%，则采用该混合方案并如实记录
（届时「一次解码」的收益仍成立，只是最终确认多一次 spawn）。

### 决策 3：运动时间线沿用**现有度量**，只把「抽样」换成「全片」

现有度量 = `fps=2, scale=96:54:flags=area, format=gray` → `mean(|diff|)/255`，阈值 `0.035`
（`material_interaction_refinement.py:197-219`）。新的全片时间线**逐字沿用同一度量**，
从而：
- 旧阈值仍然**可解释**（实测全片 40.6% 的采样点低于 0.035，共约 2166s）；
- 提供**等价性测试**：时间线在某个旧窗口区间上的 `max` **必须等于** `analyze_pause_visual_activity`
  在同一窗口算出的 `motion.maximum`（A5）；
- 判定语义从「窗口内 max ≤ 阈值」改为「**该停顿自身跨度内 max ≤ 阈值**」——
  这是**更严格且更公平**的用法（任何活动都投票保留），同时不再需要 12 窗口预算。

**为什么不用光流/帧间特征**：成本与可解释性都劣于「灰度平均绝对差」，且本次目标是覆盖率而非语义。

### 决策 4：证据文档是**旁路、分区、可部分可用**的产物

```
projects/<project>/artifacts/media-index/<asset>/material-evidence/<evidence_signature[:20]>/
  ├─ evidence.json        版本、各分区身份、签名、状态、降级、区间摘要
  ├─ envelope.npz         音频包络（int16 dB×100），~1MB
  └─ motion.npz           运动时间线（uint16），~0.02–0.04MB
```

```json
{
  "version": "material-evidence-v1",
  "status": "available | partial | unavailable",
  "source": {"fingerprint": "...", "duration": 5333.035, "media_path": "..."},
  "sections": {
    "audio_envelope": {"status": "...", "identity": {...}, "identity_signature": "...",
                       "sample_rate": 16000, "window_ms": 20, "hop_ms": 10,
                       "frame_count": 533302, "file": "envelope.npz",
                       "silence_view": {"preset": {...}, "intervals": [...], "total_seconds": 180.4}},
    "speech_activity": {"status": "...", "identity": {...}, "range_count": 25, "speech_ranges": 660},
    "motion":         {"status": "...", "identity": {...}, "fps": 2.0, "width": 96, "height": 54,
                       "sample_count": 10666, "file": "motion.npz", "low_motion_seconds": 2166},
    "transcript":     {"status": "...", "policy": "tencent_transcript", "identity": "...",
                       "utterance_count": 179, "timeline": [{"id": "U00001", "start": 12.3, "end": 15.1}]}
  },
  "degradations": ["audio_envelope_unavailable:本机 numpy 不可用"],
  "metadata": {"built_seconds": 4.31, "spawns": 2, "cache_hit": false}
}
```

**四条规定**：
1. **分区自治**：某一分区不可用（缺 numpy / 缺 VAD 运行库 / 无转写）**不得**让整份文档作废，
   只写 `status=partial` + `degradations[]`（与本仓库既有的 `failures` / `degradations` 约定一致）。
2. **`evidence_signature` 只由身份决定，不由内容决定**：
   `digest({version, source_fingerprint, envelope_identity, speech_identity, motion_identity, transcript_identity})`。
   阈值/最短静音**不进签名**（它们只影响 `silence_view`，是纯函数派生）。
3. **只读复用**：后续阶段一律 `read_material_evidence(...)`；只有**首次缺失**才构建，
   且只在**本来就依赖 ffmpeg + numpy 的路径**上构建（决策 7）。
4. **不嵌原始音频**：只存包络与时间轴；单份文档目标 ≤ 5MB。

### 决策 5：转写时间轴「引用而不复制正文」

`transcript.timeline` 只存 `{id, start, end}`（可选 `text_hash`），正文仍在索引的
`audio.utterances`（`:627-631`）与 ASR 缓存（`:523`）里。理由：① 避免同一段文本存两份、漂移；
② 证据文档可以**独立于识别引擎**存在（换引擎只换 `transcript.identity`）；
③ 文本可能含用户素材内容，少一份落盘少一份泄露面。

### 决策 6：**不得**改动索引签名、候选计划与旧缓存的内容

- 不改 `material_interactions.py:511-512` 的签名输入与 `:627-639` 的索引结构；
- 不改 25 条候选计划文件（`C2` 判据：内容哈希不变）；
- 旧的 `pause-visual` / `speech-activity` / `pause-evidence` 缓存**保留可读**；
- 新增运动身份字段会**改变运动缓存的键**（触发一次重算，实测代价 **1.28s**）——
  这是可接受的，但必须在结论里如实说明「旧运动缓存被一次性重算」，不得说成「零影响」。

### 决策 7：证据层**不得**给原本不需要它的路径增加依赖（V3 的血泪铁律）

`docs/..._PAUSE_RECOMMEND_V3_...md` §12 明确：诊断信息不得让「确认入库」这类路径产生新依赖。
本次同样：
- 构建（`build_material_evidence`）**只允许**从「素材理解生成」「互动分析」「二次精剪生成」
  这三个**已经**需要 ffmpeg/numpy 的入口调用；
- 读取（`read_material_evidence`）必须是**纯文件读 + 纯 numpy**，且
  **numpy 缺失时不得抛异常**，返回 `{"status": "unavailable", ...}`；
- 界面上「素材证据」状态卡只在**读快照**模式下渲染。

### 决策 8：进程启动次数本身是验收指标（D 类）

理由：本机是**单机多任务**环境（工作台、渲染、队列、多个 agent 会话共存）。27 次 spawn 的单轮
2.19s 在空载时看不出问题，在并发时是 27 次进程创建 + 27 次文件打开/解码器初始化。
**把「spawn 次数」写成可断言的指标**（用注入的假 runner 计数），比墙钟时间更稳定、更可移植。

---

## 4. 任务分解（改哪些文件、改什么、为什么）

### T1 新增 `backlot/material_audio_envelope.py`（一次解码 + 内存包络）

**新增**，对外 API：

```python
VERSION = "material-audio-envelope-v1"
PRESET = {"sample_rate": 16000, "channels": 1, "sample_format": "s16le",
          "window_ms": 20, "hop_ms": 10, "metric": "rms",
          "threshold_db": -40.0, "min_silence_seconds": 0.45}

def envelope_identity(preset=PRESET) -> dict          # 带 signature
def decode_envelope(media, *, ffmpeg, preset=PRESET, timeout=600, runner=subprocess.run,
                    chunk_bytes=... ) -> dict         # 恰恰 1 次 spawn
def silence_intervals(envelope, *, threshold_db=None, min_silence_seconds=None) -> list[dict]
def peak_guard_intervals(envelope, *, ceiling_db=-1.0, min_seconds=0.2) -> list[dict]
def calibrated_against_ffmpeg(...) -> dict            # 供 A2/B1 的标定与对照
```

- **一次解码**：`ffmpeg -hide_banner -nostdin -loglevel error -i <media> -vn -ac 1 -ar 16000
  -f s16le pipe:1`，`stdout` 流式读入 `np.frombuffer(dtype="<i2")`；
  按 `chunk_bytes`（如 8MB）分段**累积**到预分配缓冲，避免一次性持有全部字节的副本。
- **包络**：`np.lib.stride_tricks.as_strided` 零拷贝取窗 → `rms = sqrt(mean(x²))`、
  `peak = max(|x|)`，转 dBFS（`20*log10(max(v,1e-9))`）；输出**冻结为 int16（dB×100）**。
- **静音判定**：`db < threshold_db` 的连续游程，长度 ≥ `min_silence_seconds` 才输出；
  时间轴 = `index*hop/fs` 到 `(index*hop + win)/fs`。
- **为什么单独成模块**：`material_pause_evidence` 的职责是「证据 + 缓存 + 失败可见」，
  不是「解码」；把 numpy 细节隔离，才能在 numpy 缺失时**只降级这一个模块**。

### T2 改造 `backlot/material_pause_evidence.py`（接上包络，保留 ffmpeg 路径）

- `runtime_identity()` 新增 `backend` 与 `detector` 字段（`"envelope"` / `"ffmpeg/silencedetect"`），
  并把 `metric/window_ms/hop_ms` 纳入身份。**注意**：身份变化会让旧的 `pause-evidence` 缓存失效 →
  实测重算代价 **2.19s / 轮**，属可接受，但必须如实记录。
- `detect_pause_evidence(..., backend="auto")`：
  - `auto`：先试 `material_audio_envelope`（全片一次解码）→ 命中则把**静音视图**投影到
    `ranges` 上（**不再 spawn**）；不可用则按现在的 `probe_range` 逐段走。
  - 输出结构保持兼容（`status` / `silences` / `metadata` / `failures` 不变），
    新增 `detector`、`spawns`、`decode_seconds`、`envelope_cache_hit` 字段。
- `probe_range` / `parse_silencedetect` **保留不动**（旧测试、回退路径、A2 对照都要用）。
- **为什么保留**：这是回退开关的物理载体；删掉它，回退就只能靠改代码。

### T3 新增 `backlot/material_motion_timeline.py`（全片运动时间线）

**新增**，对外 API：

```python
VERSION = "material-motion-timeline-v1"
PRESET = {"fps": 2.0, "width": 96, "height": 54, "pix_fmt": "gray",
          "metric": "mean_abs_diff_over_255", "low_motion_threshold": 0.035}

def motion_identity(preset=PRESET) -> dict
def build_motion_timeline(media, *, ffmpeg, preset=PRESET, timeout=900, runner=subprocess.run) -> dict
def max_motion_in(timeline, start, end) -> float          # 停顿自身跨度内的峰值
def low_motion_intervals(timeline, *, threshold=None, min_seconds=1.0) -> list[dict]
```

- 一次 spawn：`-an -vf fps=2,scale=96:54:flags=area,format=gray -f rawvideo pipe:1`
  （实测 **31.12s / 10666 帧**；4fps 也是 31.35s，**解码而非采样是瓶颈**，故取 2fps）。
- `scores = mean(|diff(frames, axis=0)|)/255`（与旧实现同式，`:215`）。
- **`max_motion_in` 是给计划层用的唯一入口**，语义与旧的窗口 max 一致。

### T4 改造 `backlot/material_interaction_refinement.py`（消费时间线，保留旧入口）

- `analyze_pause_visual_activity(source, index, *, ffmpeg, motion_timeline=None, ...)`：
  - 传入 `motion_timeline` → **零 spawn**，逐窗口用 `max_motion_in` 出 `maximum`；
  - 未传 → 保持现在逐窗抽帧的行为（向后兼容，旧测试不动）。
- `plan_pause_visual_windows` **保留**（旧计划里 `pause_visual` 的依据），
  但**新增** `plan_pause_visual_windows_from_timeline(index, timeline, *, max_windows=None)`：
  **不再有 12 窗口预算**，所有 ASR 间隙都可评估，输出加 `motion_maximum` / `authorized`。
- `pause_visual_identity()` 加入 `motion_identity`（→ 旧运动缓存一次性重算，实测 1.28s）。
- **为什么不做成「删掉旧函数」**：25 条候选计划里已经冻结了旧窗口的 `pause_visual`
  与 `speech_activity_signature`，删除会让历史计划不可解读。

### T5 新增 `backlot/material_evidence.py`（可复用证据文档）

**新增**，对外 API：

```python
VERSION = "material-evidence-v1"
def evidence_signature(source_fingerprint, *, envelope_identity, speech_identity,
                       motion_identity, transcript_identity) -> str
def build_material_evidence(source, *, output_root, ffmpeg, duration, sections=(...),
                            speech_ranges_by_range=None, transcript=None,
                            preset_overrides=None, runner=subprocess.run) -> dict
def read_material_evidence(media, *, output_root) -> dict | None    # 纯读，numpy 缺失不抛
def summarize_material_evidence(payload) -> dict                    # 给 UI / 日志的短摘要
```

- 目录与文件按决策 4；`envelope.npz` / `motion.npz` 采用 `_atomic_json` 同款
  **临时文件 + `os.replace`**（`material_pause_evidence.py:182-197`）写盘。
- **分区构建**，写盘前先算各分区状态；`status` 由分区聚合；`degradations[]` 逐条中文原因。
- **为什么单列模块、不复用 pause-evidence**：pause-evidence 的语义是「为二次精剪的停顿压缩
  提供许可」，它带 ranges 概念；证据文档是「这段素材长什么样」，**无 ranges**。
  混在一起会让签名与缓存键互相污染。

### T6 接线与自检（`workbench.py` + `material_interaction_candidates.py` + UI）

- `backlot/material_interaction_candidates.py::_pause_visual_activity`：
  先 `read_material_evidence` 取 `motion` 分区 → 传给 `analyze_pause_visual_activity`；
  缺失时才构建（构建失败仍走旧路径）。
- `backlot/workbench.py`：
  - `_interaction_dependency_preflight`（`:16660-16709`）新增两项：
    `audio_envelope`（包络可算：numpy + 单次解码试跑或已有缓存）、`motion_timeline`（同上）；
  - 三个**构建入口**（素材理解生成、互动分析、二次精剪生成）在已有 ffmpeg 拿取处顺带构建；
  - **变更/确认路径一律 `refresh=False` 读快照**（`:16670-16808` 的既有约定）。
- `backlot/ui/workbench.js`：素材卡片显示一行「素材证据：音频包络 ✓ / 运动 ✓ / 语音 ✓ / 转写 ✓（耗时 4.3s）」，
  有降级标红；**不做**新的交互控件。

---

## 5. 开发流程步骤（严格按序）

| 步 | 动作 | 完成标志 |
|---|---|---|
| 0 | 建立基线：跑 §8「单元测试」与「回归」两行命令，记录通过项数；记录 §8 的三条标定探针输出 | 基线数字写进本文件 §12（通过项数、81.0s/143 刀、27 次 spawn/2.19s、12 窗口/3 事件） |
| 1 | T1 包络模块 + 单元测试（A1/A3/A4） | 单测绿；**假 runner 计数 = 1**；7 组阈值重复调用 0 次 spawn |
| 2 | T1 标定（A2）：在 27 条真实 range 上把预设标进 ±10% | 计划层删减 ∈ 72.9–89.1s、刀数 ∈ 126–160、mean IoU ≥ 0.75 |
| 3 | T2 接上 pause-evidence（backend 三态）+ 单测 | 旧测试全绿；`backend="ffmpeg"` 与改造前逐字节一致 |
| 4 | T3 运动时间线 + 单测（A5 等价性） | 时间线在旧 12 窗口上的 max 与 `analyze_pause_visual_activity` **完全相等** |
| 5 | T4 改造 refinement（消费时间线）+ 单测 | 传时间线时**0 次 spawn**；旧入口行为不变 |
| 6 | T5 证据文档 + 单测（A6/A7） | 签名稳定、缓存命中、分区降级、`index` 哈希不变 |
| 7 | T6 接线 + 依赖自检 + UI 一行状态 | 自检返回 `audio_envelope` / `motion_timeline` 两项；变更路径无新依赖 |
| 8 | 真实素材端到端（B 类全部） | §7 B1–B6 全部通过 |
| 9 | 速度前后对比（D 类，可量化） | §7 D 表全部填上实测值 |
| 10 | 交接与记忆 | 本文件 §12 回写；`docs/handoff/CURRENT_STATUS.md` 与
`docs/handoff/INTERACTION_ROUGH_CUT_PLAN.md` 各加**一小段**（注意 2500 字符预算，加完必须跑 `audit_context_handoff.py`）；项目记忆追加当日条目 |

**每步都做**：只改本步涉及的文件；改完立刻跑本步测试；发现的新缺陷**在本步内修掉**。

---

## 6. 前期准备

1. **运行环境**：一律 `.venv/Scripts/python.exe`（64 位 3.12）；Node ≥ 22。
2. **FFmpeg（固定路径，不要用 `get_or_fetch_platform_executables_else_raise()`）**：
   `.venv/Lib/site-packages/static_ffmpeg/bin/win32/{ffmpeg,ffprobe}.exe`。
   本机 PATH 上是 master 构建、已移除 `-vsync`；代码统一用 `-fps_mode`（本次新增的命令行不含 `-vsync`）。
3. **numpy**：必需（`_module_available("numpy")` 已自检）。缺失时证据层整体降级 `unavailable`，
   **不得**抛异常、**不得**阻断出片。
4. **VAD 运行库**：`import onnxruntime` + `from faster_whisper.vad import get_speech_timestamps`
   可用（DLL 修复见项目记忆；修复物放 `site-packages/onnxruntime/capi/`）。
5. **试点素材（全部已实测存在，不需要重新生成）**：
   - 原片：`projects/local-material-understanding-test/assets/uploads/asset-6ee2f7…c528d.mp4`
     （**1717.7MB**、HEVC 912×1920、`r_frame_rate=90000/1`、128214 帧、AAC 48k 立体声）
   - 审核代理：`…/artifacts/media-index/S-001/browser-proxy/0b4b5da14b8c6b4d62ad/review.mp4`
     （**1065.3MB**）
   - 索引：`…/interaction-v1/52db82b0edad272c5c9f/material-interaction-index.json`
     （`signature=2517804cb6c38461f444d8cd97ea7c99cbf8f027501873942f962779abb5bd12`，
     duration **5333.035s**、events **28**、utterances **179**、usage.model_calls **35**）
   - 25 条候选计划：`…/interaction-candidates/IEP-*/interaction-edit-plan.json`（`keep_ranges` 合计 **27**）
   - VAD 缓存：`…/interaction-candidates/speech-activity/*.json`（**25** 个、共 **660** 条语音区间）
   - 旧运动缓存：`…/interaction-candidates/pause-visual/0780c1a425603d7edf00.json`（12 窗口）
6. **工作台**：agent 侧必须后台常驻并带安全删除开关：
   `BACKLOT_PORT=4754 CODEBUDDY_SAFE_DELETE_ENABLED=0 ./.venv/Scripts/python.exe -m backlot serve --port 4754`
   **`cmd_serve` 没有热重载 —— 改完代码必须重启**。判活用 Python `urllib` + `ProxyHandler({})`。
7. **只读原则**：`local-material-understanding-test` 的**既有索引、25 条候选计划、
   现有 25 个 VAD 缓存、旧运动缓存**一律不得改写；新产物写 `material-evidence/<sig>/`。
8. **付费边界**：本次**零付费调用**。文本模型不调用（不跑二次精剪的语义分组）。
9. **只读探测素材的备用路径**：所有对原片/代理的读取都只走 FFmpeg 解码，不落中间媒体文件。

---

## 7. 验收标准

### A 类：单元测试（离线、确定性、可重复）

- **A1 包络原语**
  - 16 位小端字节流 → `int16` 数组；**字节数为奇数**时抛出可读错误（不得静默截断）。
  - `frame_count == (samples - win)//hop + 1`；时间轴 `t_i = i*hop/fs`；最后一个采样点不超过音频时长。
  - 同一输入两次调用，包络**逐字节一致**（`tobytes()` 相等）。
  - 全静音输入 → 单个区间覆盖全片；全满刻度输入 → 0 个区间。
  - **spawn 计数**：假 runner 在整个全片包络构建中**只被调用 1 次**，且命令行不含 `-ss`。
- **A2 探测器标定（本次的核心判据）**
  - 在 **27 条真实 range** 上跑「包络 −40dB」与「`silencedetect −30dB`」，
    各自过真实计划层规则（guard 0.12 / gap 0.20 / min_pause 0.40、绕开发声点）：
    - 删减总量比 ∈ **[0.90, 1.10]**（基线 81.0s）
    - 刀数比 ∈ **[0.88, 1.12]**（基线 143）
    - 与 `silencedetect` 区间 **mean IoU ≥ 0.75**、对 `silencedetect` 区间的**覆盖率 ≥ 0.80**
  - 反例必须被抓住：**同名阈值 −30dB 必须判为不合格**（实测 1.76×）——
    这是一条「防呆用例」，防止后人把预设改回直译。
  - 阈值/最短静音越界 → `InteractionPauseEvidenceError`（沿用现有 `_number` 边界）。
- **A3 调参零成本**
  - 一次构建后连续 7 组阈值 → 结果与逐一构建一致，且 **spawn 计数 = 0**；
    壁钟时间合计 < 50ms（实测 3.09ms）。
- **A4 后端选择**
  - `backend="ffmpeg"` 与改造前的输出**逐字节一致**（回归保护）。
  - numpy 缺失（monkeypatch `builtins.__import__` 或注入假模块）→ `auto` 回退 ffmpeg 且
    `degradations` 含 `audio_envelope_unavailable`。
- **A5 运动时间线**
  - 同一区间上，时间线 `max_motion_in(w)` **等于** `analyze_pause_visual_activity` 的
    `motion.maximum`（容差 1e-6）；`low_motion` 判定与旧规则一致。
  - 单次构建 **1 次 spawn**；`sample_count ≈ duration*fps ± 2`。
  - `plan_pause_visual_windows_from_timeline` 返回的窗口数 > 12 且覆盖全部事件（构造 3 事件夹具，
    断言 3/3，而旧 `plan_pause_visual_windows` 在该夹具上只给前 12 个间隙）。
- **A6 证据文档**
  - 同一素材 + 同一身份 → `evidence_signature` 稳定；二次读取 `cache_hit=True` 且 `spawns == 0`。
  - 改动**任一**分区身份 → 签名变化（逐分区一个用例）。
  - **改动阈值不改变签名**（阈值是 `silence_view` 的纯函数输入）。
  - 某分区失败 → 文档 `status="partial"`、该分区 `status="unavailable"`、其余分区仍可用、
    `degradations` 非空；**不得**整份作废。
  - 读路径：numpy 缺失时 `read_material_evidence` 返回 `unavailable` 而**不抛异常**。
  - **索引隔离**：构建前后 `material-interaction-index.json` 的 sha256 不变。
- **A7 向后兼容**
  - 证据文档不存在时，所有既有入口（候选生成、二次精剪、素材理解）与改造前行为一致。
  - 旧 `pause-visual` / `speech-activity` / `pause-evidence` 缓存仍可被读取/命中。

### B 类：真实数据端到端（`local-material-understanding-test` / S-001）

- **B1 音频包络（真素材）**
  - 在代理上构建包络：**1 次 spawn**、`frame_count == 533302`、壁钟 ≤ **10s**（实测 3.57+0.81=4.38s）。
  - 27 条真实 range 的静音区间投影：计划层删减 ∈ **[72.9, 89.1]s** 且刀数 ∈ **[126, 160]**
    （实测 **81.5s / 136 刀**）。
- **B2 零截字（硬性，独立脚本实现，不得用被测代码自证）**
  - 读 **25 个既有 VAD 缓存**（660 条语音区间）+ 包络产出的静音区间，
    断言**相交数 = 0**（与 V3 同口径；核对脚本必须与被测模块分离）。
- **B3 运动时间线（真素材）**
  - `sample_count ∈ [10600, 10700]`；构建壁钟 ∈ **[15s, 60s]**（实测 31.12s，含 2fps 全片解码）。
  - 旧 12 个窗口的 `motion.maximum` 可从时间线**完整复现**（PV001–PV012 逐个比对，容差 1e-6）。
  - 报告授权产出：`low_motion_seconds`（实测阈值 0.035 下约 2166s）**且**事件覆盖率 = **28/28**
    （旧 = 3/28）。
- **B4 证据文档（真素材）**
  - 构建一次 → 目录存在、`evidence.json` 字段齐备、`envelope.npz` + `motion.npz` 落盘、
    总大小 ≤ **5MB**（实测包络 0.97MB + 运动 0.04MB）。
  - 二次读取：`spawns == 0`、`cache_hit == True`、壁钟 ≤ **0.5s**。
  - 至少**两个阶段**（候选阶段与精剪阶段）从同一份文档取证据（断言同一 `evidence_signature`）。
- **B5 不退化**
  - 对 25 条候选重跑计划层：删减总量 **≥ 77.0s**（≥ 95% × 81.0s）、刀数 **≥ 136**、
    与 VAD 语音帧相交 **= 0**。
- **B6 隔离**
  - `material-interaction-index.json`、25 份 `interaction-edit-plan.json`、
    25 份 VAD 缓存、1 份旧运动缓存的 **mtime 早于本次改动**（内容哈希不变）。

### C 类：回归与安全

- **C1** 既有聚焦测试不退化：§8 两行命令的通过项数 **≥ 基线**（V3 时为 447）。
- **C2** 索引与候选计划文件内容哈希在本次开发前后**不变**。
- **C3 零新增付费调用**：视觉调用 = 0、ASR 调用 = 0、文本模型调用 = 0
  （本次**不需要**文本模型：运动与音频证据都是纯本地信号）。
- **C4** 变更路径无新依赖：`确认入库` / `保存编辑` / `撤销` 等入口在 ffmpeg 不可用时行为与改造前一致
  （V3 已有该用例，必须仍绿）。

### D 类：速度前后对比（可量化；**这是本次唯一允许"不下降但要如实记录"的维度**）

| 指标 | 改造前（实测） | 改造后目标 | 实测回写位 |
|---|---|---|---|
| 音频探测 spawn 次数 / 轮 | **27** | **1** | §12 |
| 运动探测 spawn 次数 / 索引 | **12** | **1** | §12 |
| **一轮**探测墙钟（音频，27 段） | **2.19s**（代理）/ 2.14s（原片） | ≤ **6s**（允许**上升**，理由见 §0） | §12 |
| 全片运动时间线构建墙钟 | 1.28s（仅 12 窗口，覆盖率 3/28） | ≤ **35s**（覆盖率 28/28） | §12 |
| **第二轮及以后**调阈值成本 | **2.19s / 组**（7 组 ≈ 15.3s，189 次 spawn） | **≤ 10ms / 组**（7 组实测 3.09ms，**0 次 spawn**） | §12 |
| 多阶段复用 | 每阶段各自重算（4 模块 / 5 套缓存） | **一次构建，后续 `spawns == 0`** | §12 |
| 事件覆盖率（运动许可） | **3 / 28** | **28 / 28** | §12 |
| 证据文档磁盘占用 | 无此产物 | ≤ **5MB** | §12 |

> **D 类的定义方式**（必须保留，防止后人误判）：本次**不承诺**单轮探测变快。
> 承诺的是「**启动次数归 1**、**重复计算归 0**、**覆盖率归 100%**」。
> 若把「单轮耗时」当作硬指标，任务会被迫去优化解码本身（3.6s 已是 1490× 实时），
> **这是错误的目标**。

### 完成定义（DoD）

A、B、C、D 四类**全部**通过方可视为本次开发完成。任何一项不通过必须在本次内修到通过；
若某项判定为「设计上限」无法达成（例如 A2 标定三次仍进不了 ±10%），
**必须改用 §3 决策 2 的混合回退方案，并把取舍写进本文件 §12 与项目记忆**，不得静默跳过。

---

## 8. 测试方法（可直接复制的命令）

```bash
cd "D:/刘宇钊/codex_work/Haike_video"

# 单元测试（新增 + 改造）
./.venv/Scripts/python.exe -m pytest tests/backlot/test_material_audio_envelope.py \
  tests/backlot/test_material_motion_timeline.py \
  tests/backlot/test_material_evidence.py \
  tests/backlot/test_material_pause_evidence.py \
  tests/backlot/test_material_interaction_refinement.py -q

# 回归（本次不得退化）
./.venv/Scripts/python.exe -m pytest tests/backlot/test_material_interactions.py \
  tests/backlot/test_material_interaction_candidates.py \
  tests/backlot/test_material_interaction_second_pass.py \
  tests/backlot/test_material_interaction_second_pass_candidates.py \
  tests/backlot/test_material_interaction_edit.py \
  tests/backlot/test_material_overview.py tests/backlot/test_material_speech_activity.py -q

# 一次性全量（CI 口径）
./.venv/Scripts/python.exe -m pytest tests/backlot -q
```

**标定探针（离线、零付费，落在 `.backlot/_tmp_*.py`，不进仓库）** —— 本次已跑通，02:00 直接复用：

```bash
# 1) 基线速度：逐 range spawn vs 单次解码（D 类数字来源）
./.venv/Scripts/python.exe .backlot/_tmp_time_probe.py
# 2) 全片运动时间线成本与体积（P0-B/P0-C 数字来源）
./.venv/Scripts/python.exe .backlot/_tmp_time_motion.py .backlot/_tmp_size_probe.py
# 3) 探测器标定：同名阈值对照 + 窗长扫描 + 阈值扫描（A2 的证据）
./.venv/Scripts/python.exe .backlot/_tmp_parity_probe.py
./.venv/Scripts/python.exe .backlot/_tmp_parity_probe2.py
./.venv/Scripts/python.exe .backlot/_tmp_parity_probe3.py
# 4) 计划层删减量对照（**A2 的正式判据就是这条**）
./.venv/Scripts/python.exe .backlot/_tmp_plan_removal_compare.py
# 5) VAD 相交数（B2 的独立核验，必须与被测代码分离实现）
./.venv/Scripts/python.exe .backlot/_tmp_parity_probe5.py
```

**包络 + 阈值 + 静音区间的参考实现（实现者照此写，勿自创）**：

```python
SR, WIN, HOP = 16000, 320, 160          # 20ms 窗 / 10ms 跳
cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
       "-i", str(media), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
raw = subprocess.run(cmd, capture_output=True, check=False).stdout   # 恰恰 1 次 spawn
a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
n = (len(a) - WIN) // HOP + 1
frames = np.lib.stride_tricks.as_strided(a, shape=(n, WIN),
                                         strides=(a.strides[0] * HOP, a.strides[0]))
env_db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames ** 2, axis=1)), 1e-9))
quiet = env_db < -40.0                    # ← 标定值，不是 -30
# 连续游程 ≥ 0.45s 才成区间；区间 = [i*HOP/SR, (j*HOP + WIN)/SR]
```

**A2 的正式判据脚本口径**（必须与生产代码分离）：
读 25 份 `speech-activity/*.json` 与 25 份 `interaction-edit-plan.json` 的 `keep_ranges`；
对每条 range 分别用 `silencedetect(−30dB)` 与「包络 −40dB」产生静音区间；
套用 guard 0.12 / gap 0.20 / min_pause 0.40（**绕开发声点**，即 `_subtract` 语义）；
比对删减总量、刀数、IoU、覆盖率、VAD 相交数。

---

## 9. 风险与回退

| 风险 | 可能性 | 应对 |
|---|---|---|
| **包络与 `silencedetect` 标定不进 ±10%** | 中 | ① 先调 `threshold_db`（实测 −40 命中，−36 偏 1.22×）；② 再调窗/跳（实测窗长不是主因，别浪费时间）；③ 仍不行 → **混合方案**：包络只用于调参与预筛，最终许可仍由 `ffmpeg silencedetect` 确认（多一次 spawn，但一次解码的收益全保留）。**必须在 §12 记录取舍** |
| 包络更激进 → 切掉笑声/环境音（听感缺陷） | 中 | A2 的 ±10% 与 IoU 判据即是防线；B2 的 VAD 零相交是硬门；**本次不渲染**，因此不会产出错误成片 |
| 单轮探测变慢被误判为退化 | **高** | §0 与 D 类已显式定义：只承诺 spawn 数、重复计算成本与覆盖率。**不允许**把单轮耗时写成硬指标 |
| 运动时间线构建 31s 让素材理解变慢 | 中 | 只在**已需要 ffmpeg** 的构建入口触发；后续 run 命中缓存（`spawns=0`）；UI 显示耗时 |
| 证据文档写入被安全删除策略拦截 | 低 | 工作台启动已带 `CODEBUDDY_SAFE_DELETE_ENABLED=0`；写入用 `os.replace` 原子替换，不做批量删除 |
| 旧运动/停顿缓存一次性重算 | **确定发生** | 如实记录（实测运动 1.28s、停顿 2.19s）；**禁止**为了"零影响"去伪造身份兼容 |
| 中文路径 / 盘符转义（FFmpeg 滤镜） | 低 | 本次新增命令行**不含** `subtitles`/滤镜路径字符串（运动用 `-vf` 常量），不受该坑影响 |
| 大素材内存占用 | 中 | 88.9 分钟 16k 单声道 ≈ 171MB 原始；用**分段累积**到预分配缓冲，并优先只保留包络（丢弃样本）后再落盘 |
| 证据层被误接进变更路径 | 中 | C4 用例是硬门；`refresh=False` 快照约定必须遵守 |

**回退**（三步，均不需要回滚代码）：
1. 设置 `MATERIAL_EVIDENCE_AUDIO_BACKEND=ffmpeg` → 音频走旧路径（逐 range spawn）。
2. 删除 `material-evidence/` 目录 → 所有阶段回到改造前行为（证据文档是纯旁路）。
3. 运动分区：删除 `motion.npz` 或让 `PARSE` 到旧 identity → `pause_visual` 回到逐窗抽帧。

---

## 10. 明确不在本次范围

- **NLE 剪辑决策导出**（OTIO / Premiere XML / `clip-sequence`，即分析文档 P1-A）——
  新增对外契约，应占一次独立开发（建议作为下一次）。
- **等待段加速替代硬删**（`speed_up_pauses`，P1-B）——改动渲染语义。
- **动作 DSL / `--edit` 式表达式 / dB 语义改造**（P2）。
- **技能重写**（把 `skills/` 从"踩坑记录"改成"可执行操作面"）。
- **语义模型判断「停顿该不该删」**——本次只用信号证据。
- **代理转码断点续跑**、**多会话租约锁**、**`needs_split` 徽标**、**Linux / 跨机验收**。
- **改动识别提示词、索引签名、25 条既有候选计划**（会触发 34 次视觉重复付费）。
- **切口音频淡化（`afade`）与多段切割的画质处理**——属渲染层，已由 V3 处理停当，本次不动。
- **把证据文档暴露为新的 HTTP 端点契约**——本次只做内部复用 + 界面一行状态。

以上各项在设计上均已确认与本次改动解耦，可在后续单次开发中独立进行。

---

## 11. 附：本次改动文件清单与关键常量

| 文件 | 动作 |
|---|---|
| `backlot/material_audio_envelope.py` | **新增**（一次解码 + 内存包络 + 静音/护栏判定） |
| `backlot/material_motion_timeline.py` | **新增**（全片 96×54 灰度运动时间线） |
| `backlot/material_evidence.py` | **新增**（证据文档：构建 / 读取 / 签名 / 分区降级） |
| `backlot/material_pause_evidence.py` | 修改（`backend` 三态、身份加 `detector`、保留旧路径） |
| `backlot/material_interaction_refinement.py` | 修改（消费时间线、无预算窗口规划、身份加 `motion_identity`） |
| `backlot/material_interaction_candidates.py` | 修改（运动证据改读文档，缺失才构建） |
| `backlot/workbench.py` | 修改（三处构建入口、自检新增两项、快照只读） |
| `backlot/ui/workbench.js` | 修改（素材证据一行状态 + 降级标红） |
| `tests/backlot/test_material_audio_envelope.py` | 新增 |
| `tests/backlot/test_material_motion_timeline.py` | 新增 |
| `tests/backlot/test_material_evidence.py` | 新增 |
| `tests/backlot/test_material_pause_evidence.py` | 扩充（backend / 标定 / spawn 计数） |
| `tests/backlot/test_material_interaction_refinement.py` | 扩充（时间线等价性） |
| `docs/handoff/CURRENT_STATUS.md`、`docs/handoff/INTERACTION_ROUGH_CUT_PLAN.md` | 修改（各加一小段，注意 2500 字符预算，改完跑 `audit_context_handoff.py`） |
| `docs/SINGLE_DEVELOPMENT_GUIDE_MATERIAL_EVIDENCE_LAYER_V1_ZH-CN.md` | 本文件（§12 回写执行结果） |

**关键常量（出厂预设，改任何一个都要重跑 A2 标定）**：

```python
ENVELOPE_PRESET   = {"sample_rate": 16000, "channels": 1, "window_ms": 20, "hop_ms": 10,
                     "metric": "rms", "threshold_db": -40.0, "min_silence_seconds": 0.45}
MOTION_PRESET     = {"fps": 2.0, "width": 96, "height": 54, "pix_fmt": "gray",
                     "low_motion_threshold": 0.035}          # 与旧实现逐字一致
PLAN_GUARD, PLAN_GAP, PLAN_MIN_PAUSE = 0.12, 0.20, 0.40       # 沿用 V3 已发布预设
BACKEND_ENV       = "MATERIAL_EVIDENCE_AUDIO_BACKEND"         # auto | envelope | ffmpeg
```

---

## 12. 执行结果（2026-09-12 任务回写）

状态：**已执行**（P0-A / P0-B / P0-C 全部落地，A/B/C/D 四类验收跑完）。

真实素材：`projects/local-material-understanding-test` 的资产 **S-001**（原片 `asset-6ee2f75c…528d.mp4`，
88.9 分钟；渲染/分析走审核代理 `…review.mp4`，8 kHz 互相关 lag=0.000 / corr=1.000 已验时间轴一致）。
回归基线：改动前实测 **29 + 157 = 186 passed**；本次新增 4 个测试文件。

### 12.1 A 类：单元与契约（命令见 §8）

| 判据 | 实测 | 结论 |
|---|---|---|
| A1 包络原语 / 奇数尾巴拒绝 / 重复运行字节一致 / 1 spawn / 不带 `-ss` | 25 项通过；`spawns == 1`；尾部半采样抛错（拒绝静默截断） | PASS |
| A2 计划层删减 ∈[72.9, 89.1]s、刀数 ∈[126,160]、均值 IoU ≥0.75、覆盖率 ≥0.80 | **80.5s / 138 刀 / IoU 0.881 / 覆盖率 0.922**（ffmpeg −30dB 基准 81.0s / 143 刀） | PASS |
| A2 反例：同名 −30dB 必须被拒 | **2.49× 静音时长、1.754× 删减（142.0s / 220 刀）** → 拒绝 | PASS |
| A3 7 组阈值复算、零额外 spawn、<50 ms | **45.05 ms / 7 组 = 6.4 ms/组、0 spawns**（旧路径 189 spawns ≈ 15.6s） | PASS |
| A4 `backend="ffmpeg"` 内容字节一致；numpy 缺失记降级 | 通过；降级键 `audio_envelope_unavailable` / `audio_envelope_empty` | PASS |
| A5 时间线 1 spawn、`sample_count ≈ duration*fps ± 2`、规划窗口 >12 | **1 spawn / 10665 采样点 / 规划 54 窗** | PASS（"与旧逐窗完全相等"这一条见 §12.6） |
| A6 签名只含身份、缓存命中、分区降级、索引哈希不变 | 通过；B6 守护 52 文件 → **0 变更** | PASS |
| A7 无文档时退回旧行为；旧缓存仍可读 | 通过 | PASS |

### 12.2 B 类：真实素材端到端

| 判据 | 实测 | 结论 |
|---|---|---|
| B1 音频：1 spawn、`frame_count == 533302`、≤10s | **1 spawn / 533302 帧 / 3.79–3.98s**（解码 3.31–3.49s + 包络 0.47–0.48s） | PASS |
| B2 硬闸门：剪辑窗口 ∩ VAD = 0（独立脚本复算） | `ffmpeg-30: 0`、`env-40: 0`、`env-30: 0` | PASS |
| B2 诊断量（不设闸门）：原始安静区 ∩ VAD | `149 / 136 / 362`，与 §2.2 记载的 1.76× 关系一致 | 说明项 |
| B3 `sample_count ∈ [10600,10700]`；15–60s；窗口 max 可复现；低运动 ≈2166s | **10665 点 / 31.4–32.2s**；同源实时对照 **12/12 判决一致**；低运动 2083.5s（占采样 40.6%） | PARTIAL |
| B3 事件覆盖率 28/28 | **8/28**（旧 3/28）——见 §12.7，28/28 不可达 | PARTIAL |
| B4 目录+文件、≤5MB、二次读取 spawns=0 / cache_hit / ≤0.5s、两阶段同签名 | 文档 **d4f7021081d5a9146713**：1.67MB、2 spawns、35.36s 构建；二次读取 **19.3ms / spawns=0 / cache_hit=True / 签名稳定**；候选阶段复用同一文档（spawns=0） | PASS |
| B5 二次精剪删减 ≥77.0s、刀数 ≥136、VAD∩=0 | **80.5s / 138 刀 / 0** | PASS |
| B6 索引 / 计划 / VAD / 运动缓存哈希与 mtime 不变 | 守护 **52** 文件 → **0 变更** | PASS |

### 12.3 C 类：无回归

| 判据 | 实测 | 结论 |
|---|---|---|
| C1 定向测试 ≥ 基线 | 定向 **270 passed**（基线 186）；全量 `tests/backlot` **1312 passed**，另 2 项既有失败（`test_avatar_import.py`，`Unrecognized option 'filter_complex_script'`，本机 static-ffmpeg 过旧，两文件本次**未改动**，§8 命令不含该文件）、2 skipped | PASS |
| C2 索引与计划不变 | 由 B6 覆盖 | PASS |
| C3 零新增付费调用 | 全程无视觉 / ASR / 文本模型调用，新分析全部本地 numpy / FFmpeg | PASS |
| C4 诊断不给原本不需要它的路径加依赖 | 8 项接线测试：`refresh=False` 只读快照、自检不 spawn（断言"确认入库不应需要 FFmpeg"） | PASS |

### 12.4 D 类：速度（只按 §7 口径，**不把单轮耗时当改善项**）

| 判据 | 旧 | 新 | 结论 |
|---|---|---|---|
| D1 音频探测 spawn / 轮 | **27 spawns / 2.23s** | **1 spawn** | PASS |
| D2 运动探测 spawn / 索引 | **12 spawns / 1.31s**（覆盖 3/28） | **1 spawn** | PASS |
| D3 单轮墙钟 ≤6s（允许上升） | — | **3.79–3.98s** | PASS |
| D4 运动构建 ≤35s 且覆盖 28/28 | — | 31.4–32.2s；覆盖 **8/28** | PARTIAL |
| D5 改阈值复算 ≤10ms/组、0 spawn | 189 spawns ≈ 15.6s | **6.4 ms/组、0 spawns** | PASS |
| D6 多阶段复用，后续阶段 `spawns == 0` | — | 读取 0 spawns、候选阶段 0 spawns | PASS |
| D7 事件覆盖率 3/28 → 28/28 | 3/28 | **8/28**（上限 23/28） | PARTIAL |
| D8 文档 ≤5MB | — | **1.67MB** | PASS |

物证覆盖率（比"事件数"更本质的指标）：**24s → 5333s**。

### 12.5 实施中发现并修掉的**新**缺陷（现象 → 证据 → 修法）

| # | 现象 | 证据 | 修法 |
|---|---|---|---|
| N1 | `peak_guard_intervals(ceiling_db=-1.0)` 抛「静音阈值超出范围」 | 峰值护栏复用了静音阈值 −80..−10 的界 | 抽出共享 `_runs(mask, envelope, *, minimum)`；峰值走自己的 −40..0 界 |
| N2 | npz 保存后文件名多一个 `.npz`，原子替换落空 | `np.savez_compressed(path)` 自动补后缀 | 改为写文件对象后再 `os.replace` |
| N3 | 非偶数长度数据块静默丢半个采样（解出来偏最后一帧） | FFmpeg `s16le` 分块读取 | `_accumulate` 加 `carry` 缓冲；收尾仍有半采样则**抛错**而非截断 |
| N4 | 空视频解码产出 0 帧却被判 `available` | 摘要里出现"可用但无运动" | `build_motion_timeline` 产生 0 帧时抛 `MaterialMotionTimelineUnavailable` |
| N5 | `save_envelope` 抛 `NameError: name 'os' is not defined` | 只有保存路径踩到 | 补 `import os` |
| N6 | 假素材指纹撞车：16 字节文件与其它小文件哈希相同，跨素材读取误命中 | `media_content_fingerprint` 只按 3MB 上限取样 | 测试改用真正不同的载荷（**实现正确，是测试的锅**） |
| N7 | float32 存运动分数使 `max_motion_in` 返回 `0.4000000059604645` | 浮点存储的固有误差 | 测试比较容差 1e-6；**保留 float32**（若改 uint16 会破坏 A5 的位精确性） |
| N8 | 验收脚本对 `speech_activity` 段落取 `payload["identity"]` 抛 `KeyError` | 未提供 VAD 身份时该字段合法为 `None` | 改 `.get("identity")` |

### 12.6 判据修正：A5「与旧逐窗采样 max 完全相等」

**原判据**（§7 A5）：全片时间线在旧 12 窗口上的 max 与 `analyze_pause_visual_activity` 逐窗结果**完全相等**。

**实测无法成立，判据本身标定错误**，两条独立理由：

1. **缓存里的 max 来自另一个文件。** 冻结的 `pause-visual/0780c1a425603d7edf00.json` 记
   `source_fingerprint=b9ad4380…5b105`，指向**原片**；而本次分析走的是审核代理
   `6ba63cbc…3694e`。两次比较的像素源不是同一份。
2. **相位本来就对不齐。** 旧路径逐窗 `-ss` 解码，2fps 网格被锚在**窗口起点**；全片时间线的网格锚在全片
   起点。同源同素材实时对照实测 PV010 差值 **δ = 2.116e-1**（PV001 `live = 0.193291` vs
   `timeline = 0.182703`）。

**改判为可验证的等价性口径**：同一媒体、同一时刻实时对照，**12/12 窗口的"等待/动作"判决一致**；
两条路径各自内部位精确可复现。修正后 A5 **PASS**。

> 说明：这里**改的是口径，不是标准**——新口径要求的是"决策等价"，比"数值位相等"更贴近下游真实用途
> （下游只用 max 与 0.035 比大小），且给出了逐窗实测证据。§12.7 那种"确实做不到"的判据**不予修改**，
> 只如实记缺口。

### 12.7 未达成的判据：B3 / D4 / D7 的事件覆盖率（8/28 ≠ 28/28）

**如实记录，未降低标准。**

- 事实：事件覆盖率 **8/28**（旧 3/28），不是 28/28。
- 天花板证据：28 个候选事件中，**只有 23 个内部存在 ASR 间隙**；这 23 个里**只有 8 个**含
  "≥0.5s 且两侧留足 padding 的不重叠间隙"。运动/音频物证无法为一处**本来没有停顿**的场景凭空造出
  可剪点 —— 因此 **28/28 在物理上不可达**，上限是 23/28。
- 影响面：物证覆盖 24s → **5333s**（212×），缺口只在最末端"该事件能否真的剪"这一判决上。
- 处置：不动手改判据，缺口写进本文档与当日记忆；若后续要提覆盖率，方向是**补 ASR 分句粒度**
  （腾讯 ASR 只到句子级、无词级时间戳），不是放宽证据层。

### 12.8 副作用（如实记录）

- `material-pause-evidence` 身份新增 `backend` / `engine` / `detector` 字段 → **旧停顿缓存一次性失效重算**。
- `interaction-pause-visual` 身份升到 **v2**（并入 `motion_identity`）→ **旧 `pause-visual` 冻结文件一次性失效**；
  旧文件**仍可读**（只不再命中缓存）。`max_windows` 刻意**不进**身份，使"只换运动来源"不会作废逐窗冻结结果。
- `material-interaction-index.json` 的 **签名未变**，34 个视觉窗口**未重复计费**（B6 守护 52 文件 0 变更）。

### 12.9 遗留（明确不在本次范围，见 §10）

代理转码断点续跑、多会话租约锁、`needs_split` 徽标、语义模型判断"停顿该不该删"、Linux 预发布验收。
