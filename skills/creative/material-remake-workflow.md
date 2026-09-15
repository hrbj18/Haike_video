# 外站素材复刻工作流（Material Remake Workflow）

> 触发场景：拿到一批**别人拍的**短视频素材（抖音/B 站/YouTube 下载），要把它重剪成
> 自己的原创视频——文案自己改写、画面用别人的好素材、配音与数字人用自己的。
> 核心约束通常是「画面上不能出现真人脸」「不能把原博主的烧入字幕带进成片」。
>
> 本工作流在 `2026-09-12 苹果折叠屏复刻` 上端到端跑通，工具脚本在 `scripts/`。

## 0. 一句话结论

**素材要双筛（人脸 + 烧入字幕），时长要量不要估，画面绑定用「清洗母版 + 源区间」而不是切片文件，
母版落地后必须在真正生效的窗口上再筛一次，换镜后重出预览只排 `full_preview`。**

---

## 1. 八个阶段

```
① 素材双筛        ② 参考视频付费转写     ③ 改写文案（自写）
  人脸 + 字幕带       腾讯云 ASR              不搬运原文
        │                    │                    │
        └────────────────────┴────────────────────┘
                             ▼
④ 量真实时长 ← 先用配音引擎试配一次，量出每句秒数（★ 不要用字数估算）
                             ▼
⑤ 规格书 → 建项目（清洗母版 + 源区间 + full_bleed 场景）
                             ▼
⑥ 数字人 + 配音（统一队列）
                             ▼
⑦ 合成预览 → ⑧ 输出质检（audit：生效窗口无人脸 / 无字幕 + 响度）
                             ▼
              ⑨ 局部修镜（remake_retime_shots.py）→ 排 full_preview 重出
```

### ① 素材双筛 —— `scripts/remake_material_screen.py`

```bash
# 整目录双筛：字幕带几何 + 人脸命中（主阈值 + 小脸阈值）
python scripts/remake_material_screen.py screen --video-dir <原片目录> --out <报告目录>

# 挑选镜头：带时间码的精细抽帧表
python scripts/remake_material_screen.py pick --video-dir <原片目录> --id <aweme_id> \
       --times 0.5,3.0,5.5,8.0

# 命中复核：把检测框画出来，人工判定真假
python scripts/remake_material_screen.py verify --video-dir <原片目录> \
       --id <aweme_id> --times 14.5

# ★ 最终门禁：在「清洗母版 + 真正生效的源窗口」上重跑人脸 + 字幕带
python scripts/remake_material_screen.py audit --project projects/<id>
```

判据（缺一不可）：

| 判据 | 看什么 | 典型陷阱 |
|---|---|---|
| 无人脸 | **必须看带框复核图**，不能只看比例 | 圆形镜头模组、铰链 V 形结构会误报；**iOS 的 FaceTime / 照片 / 相机图标**会被稳定误报；背景小人群、被 AI 打码的脸会漏检 |
| 无烧入字幕 | 全片每行边缘密度的**时序均值**高峰带 | 手机 UI 素材单帧检测必然误报（iOS 桌面全是小字） |
| 时长够用 | 逐条看总时长与可用窗口 | 7.8 秒的素材撑不起 4 秒镜头 + 前后衔接 |

> ★★ **整片筛「无字幕」会漏掉「短时字幕」。** 本次 `M3` 的烧入字幕「经过精细调校」
> 只在 **19.5–24.0 s** 出现（占 29 s 片长的 16%），整片 band 检测按「时序均值 ×
> 出现率」判定，直接被均值抹平、报告写「无字幕带」；而这块素材恰好被用在了
> T006 的 22.0 s 处 → 字幕进了成片。
> **所以：整片筛用于「不可用素材排除」，最终必须再由 `audit` 在真实生效窗口内复检。**

### ①b 命中复核不能只在缩小图上做

`verify` 会把帧缩到 640 px 再检测，**与原生分辨率的检测结果不一致**
（本次 M4@22.17 在成片里报了 0.151 的大框，640 px 下却一个框都没有）。
判定时以**原生分辨率的 `audit` 数字**为准，`verify` 只用来「看清是什么东西」。


### ①c ★ 还要单独筛「角落水印」——字幕带检测抓不到它

`screen` 的烧入字幕判据是「**整行**边缘密度的时序均值高峰带」。角落的**设备水印**
（本期 M3 的右下角「影石Insta360 Luna Ultra」+ 红点）只占约 15% 行宽，
撑不起整行的活跃比例 → **完全不会被报出来**，但它同样会跟着进成片。

所以除了 `screen`，还要**单独裁顶带 / 底带目视**（本项目 `.backlot/9.13-pengcheng-n90/`
的 `_bottom_check.py` / `_top_check.py`：整片 0.5s 步长抽帧，只留底部 20% / 顶部 15%
拼长条）。这一遍同时能看出三件事：

| 要看的 | 本期实测 |
|---|---|
| 角落水印 | M3 右下角常驻设备水印 → 母版 `crop_bottom_ratio=0.12` 机械裁掉 |
| 顶部烧入字幕 | 另一条素材顶部两行描边字幕（0.137–0.186 / 0.202–0.250，100% 覆盖）→ **整条弃用** |
| 背景真人 | M1 19.0–22.6s 车旁站着真人、M3 2.2–4.0s 展厅顾客 —— **小脸检测只报了 M3 的 3.5s，M1 那处完全漏检** |

> ★ 结论：**「有人」这件事不能只信人脸检测。** 侧脸、背影、只露手脚的人体
> 在 YuNet 的大小两个阈值下都可能 0 命中，但目视一看就在那里。
> 顶带/底带长条是发现它们的唯一廉价手段。

### ①d ★ 建项目脚本里落一个「干净窗口白名单」断言

把每条素材复核后的可用区间写进建项目脚本的 `SAFE_WINDOWS`，任何镜头只要
`[in, out]` 不完全落在某个窗口内就直接 **拒绝建项目**。本期就靠它拦下一次
「M2 终点 21.11 越过了声明的 21.10」的越界。

好处是：**排除区间来自目视复核，不来自记忆**；后续任何改镜都不会悄悄用回禁区。


### ② 参考视频付费转写 —— `scripts/remake_reference_transcript.py`

```bash
python scripts/remake_reference_transcript.py \
  --video-dir <原片目录> --id <aweme_id> --out .backlot/<run>/asr
```

- Provider：**腾讯云录音文件识别（16k_zh）**，逐条 RPO 三态状态文件
  （`submitting → accepted → completed`）。`submitting` / `ambiguous` = 受理不明，
  **禁止自动重提**，否则重复计费。
- ★ ASR 对专名误识极重：`iPhone Duo` → `IPhone two` / `IPhone9` / `iPhou Dou`，
  `折叠屏` → `中泽屏`。**改写时型号/数字/比例必须人工还原或直接规避**，
  不要直接搬转写原文。
- 长视频（>60 min）另有分片并发路径，见 `haike-video-long-material-cutting` 技能。

### ③ 改写文案

借鉴**选题与信息结构**，不搬原文。本次做法：把两条参考转写按主题切成
「钩子 / 内屏 / 外屏 / 折痕 / 手感 / 相机 / 收尾」七段，每段一句，全部重写。

### ④ ★ 量真实时长，不要用字数估算

**这是本工作流最容易踩的坑。** 字/秒 估算（本项目口径 4.2 字/秒）与实际云端 TTS
偏差极大——本次 258 字估出 59.5 秒，腾讯云实际只输出 **41.6 秒**
（约 6.7 字/秒）。差 43%。

后果：数字人管线以**音频时长为唯一主时钟**，落地时会按真实音频重算每个场景时长
（`_build_timeline_update` → `_apply_timeline_update`），并按比例缩放视觉区间。
你的源区间裁剪点会被整体缩短。

**正确顺序**：先用配音引擎对每段台词试配一次，量出每段真实秒数，再据此设计分镜。

> ★★ **9.13 改进：把「量出来的秒数」直接当成段时长写进规格书。**
> 上一期是「按字数估时长 → 建项目 → 管线按音频重算」，缩放比 0.726，
> 源区间被整体掐尾。本期改成：试配量出 T001=4.40 / T002=7.64 / T003=7.21 /
> T004=6.65 / T005=8.11 / T006=6.12 / T007=5.11（合计 45.24s），
> **让每个 section 的 shots 时长之和恰好等于该秒数**。
> 结果：管线跑完音频驱动重算后，7 段的实际 wav 时长与试配**逐段偏差 0.00s**，
> 缩放比 = 1.0 → **已复核过的源窗口原样保留**，换镜/复检的成本直接归零。
> 试配脚本见 `.backlot/9.13-pengcheng-n90/measure_narration.py`
> （同一 profile → 同一 `speech_rate` → 结果可复现）。
>
> 顺带一个便宜的**读音探针**：同一句话换写法各合成一次比时长。
> 本期实测 `小米 HAD` 1.231s vs `小米智驾` 0.939s → 差 0.29s ≈ 一个音节，
> 说明 TTS 把全大写 `HAD` 当**英文单词 had** 读了，于是文案改用「小米智驾」。
> 字母+数字混排（`N90` / `L9` / `M9`）读法正常，不必规避。

### ⑤ 规格书 → 建项目 —— `scripts/remake_build_project.py`

输入 `remake-spec.json`（素材清单 + 分镜 + 配音/音乐设置），产出可直接渲染的项目：

```bash
python scripts/remake_build_project.py --spec <remake-spec.json>
```

关键设计：

- **清洗母版**：`crop=iw:trunc(ih*keep/2)*2:0:0` 机械裁掉底部字幕带（时间轴不变，
  所以 `source_in/out` 仍按原片秒数写），并 `-an` **剥掉原博主声音**。
- **不切成小文件**：把清洗母版整条登记为资产，用 `source_in_seconds` /
  `source_out_seconds` 引用区间。出处可追溯、切点不受四舍五入影响。
- **`layout_recipe: "full_bleed"`**：让本地素材铺满画布。
- 每段 `visual_timeline.blocks` 必须从 0 起**首尾相接**覆盖整段时长；
  带源区间的块，**显示帧数必须与源区间帧数逐帧一致**（不能自动变速/循环）。
  ★ 母版落地后再改区间时，`source_out` 要写成
  **`source_in + 显示时长`**，否则触发「源区间与显示区间必须按帧一致」。
- 项目类型必须是 `avatar-spokesperson`（有数字人的一键审核预览只接受这一种）。
- 脚本每个 section 必须同时有 `id` 与 **`turn_id`**（`Txxx`，唯一），
  否则预检报「Txxx 轮次编号缺失或重复」。
- 建项目脚本要**显式** `wb._atomic_write(artifacts/script.json, ...)`：
  `generate_scene_plan_from_script()` 在 `state["scenes"]` 已存在时提前返回，
  不写 script.json，预检就拿不到轮次编号。
- 改画面布局走 `update_scene_visual_composition()` 时必须带
  **`expected_revision`**（乐观锁），先读回当前 `composition.revision`。


### ⑥ 数字人 + 配音（付费，统一队列）

```bash
# 1) 预检（免费，先看拦不拦得住）
#    avatar_review_preview_preflight(project_dir, payload)
# 2) 提交到统一队列（唯一执行权威，不要直接调 runner）
curl -X POST http://127.0.0.1:4754/api/production-queue/jobs \
  -H "Content-Type: application/json" -d '{
    "project_id": "<id>", "kind": "avatar_review_preview", "priority": "normal",
    "idempotency_key": "<id>-arp-<date>-v1",
    "request": {"confirmed": true, "budget_limit_cny": 5.0,
                "allow_plus_on_oom": true, "visual": {"planning_mode": "rule_mix"}}
  }'
```

- 显式声明：**腾讯云云端配音（自定义音色）** + **RunningHub InfiniteTalk，
  Plus 48GB，workflow `2094449979141218305` / profile `infinitetalk_448x560_exact_clock_v2`**。
- 预算护栏：单次 ≤ ¥5，绝对上限 ¥8，单角色预留 ¥2.5；超限自动停。
- 幂等键 + 队列 job id + 支付账本（`budget.entries`）都要持久化。
- `visual.planning_mode` 选 `rule_mix`：本地素材已完整覆盖每段画面时，
  Pexels / HyperFrames 都不会被拉起（预检里的 `pexels_required` 为 false）。

### ⑦ 合成

预览与终片共用同一时间线、字幕、数字人几何、裁切与资产合同
（`review-preview-shared-v1`）。数字人版式用 `custom` + 布局模板 `pip_top_right`
（右上圆形解说员，geometry `x=0.675, y=0.04, width=0.29`）。
`avatar_review_preview` 管线里 `default_treatment` 是**硬编码 `custom`** 的，
所以每段场景的 `layout_template_id` 就是最终版式——要「右上角全程」，
必须确认 `presenter_layouts.default_template_id == "pip_top_right"`
（`_ensure_presenter_layout_state` 在**未设置**时默认它，被改过就要改回来）。

### ⑦a 音频权威下的画面块重排（帧口径，零付费）

⑥ 落地后，**场景时长归母版原声管**，规格书里的块时长全部作废，必须重排。2026-09-13
（小米澎程 N90）实测：

| 段 | 试听/`turns/*.wav` | 母版轮长 = 场景时长 | 差 |
|---|---|---|---|
| T001 | 4.396 | 4.920 | +0.52 |
| T002 | 7.641 | 8.440 | +0.80 |
| T003 | 7.209 | 7.960 | +0.75 |
| T004 | 7.646→6.646 | 7.440 | +0.79 |
| T005 | 8.114 | 8.880 | +0.77 |
| T006 | 6.124 | 6.920 | +0.80 |
| T007 | 5.108 | 5.640 | +0.53 |
| 合计 | **45.237** | **50.200** | **+4.96** |

> ★ **配 wav 与试听值逐句 0.00 s 一致**（说明试听法本身没错），多出来的 0.5~0.8 s/句
> 在**数字人母版**：`timing-manifest.json` 的轮段（= `scene.end - scene.start`）比 wav 长。
> 所以「按量出来的秒数写规格书」能保证**规格书内部自洽**，但**保证不了成片自洽**。
> 判据一律取母版：读回 `workbench.json` 的 `scenes[].{start,end}_seconds`。

渲染端取源（源码 `backlot/workbench.py:9090-9112`）：

```python
block_duration = end_seconds - start_seconds
command.extend(["-ss", source_in_seconds, "-t", block_duration, "-i", source])
```

> ★★ **生效窗口 ≡ `[source_in, source_in + 块时长]`**。已用「预览帧 → 素材帧」反解实证：
> 匹配点斜率 1.0，且「按源窗拉伸铺满」假设被明确否掉
> （T001/VB-002 实测 13.75/14.30/15.15 vs `-ss` 预测 13.80/14.40/15.20，
> 拉伸假设预测 13.71/14.25/14.96 —— 越走越偏）。**别把源窗当成会缩放的东西。**

写盘前必须满足的校验器约束（`_validated_visual_timeline`，帧 = `floor(t*fps + .5)`）：

| 约束 | 阈值 | 违反后果 |
|---|---|---|
| `abs(显示帧数 − 源帧数)` | ≤ 1 帧 | 「源区间与显示区间必须按帧一致，不能自动变速或循环」 |
| `source_out ≤ 素材时长 + 1/fps` | 硬 | 「源出点超过素材实际时长」 |
| 末块 `end` vs 场景时长 | ≤ 0.012 s | 「视觉时间线必须覆盖本段完整时长」 |
| 相邻块首尾 | ≤ 0.012 s | 「区间重叠 / 存在空白」 |
| 每块时长 | ≥ `min(0.4, 场景时长)` | 「第 n 个画面区间太短」 |

**重排算法（`scene_scale` 那种等比缩放不够用，要按块算容量）**：

1. 场景时长不动（音频权威）；块边界只是**视觉切点**，可在段内自由挪。
2. 每块容量 `cap = min(素材片长, 安全窗上界) − source_in`（安全窗见 ①c/①d）。
3. 每块先取 `min(想要时长, cap)`；差额按同段内有富裕（`cap − 时长`）的块吸收。
4. **整段容量 < 场景时长**时：把越界那块的 `source_in` 前移（容量会变大），
   或换一段素材；不要靠放宽 `source_out` 硬塞（校验器会拒）。
5. 全程**按帧算**再回写秒（3 位小数）：`floor(t*30+.5)` 反推能命中的秒值。

写盘走服务端权威端点，别直接改 `workbench.json`（常驻进程会覆盖）：

```bash
# payload={"blocks":[{id,start_seconds,end_seconds,asset_id,source_mode,
#                     source_in_seconds,source_out_seconds,label}, ...]}
curl -X PUT --noproxy '*' \
  http://127.0.0.1:4754/api/project/<id>/workbench/scenes/<scene_id>/visual-timeline \
  -H "Content-Type: application/json" -d @blocks.json
```

> ★ 先用同一套帧口径在本地干跑一遍再写盘，能一次过。
> 本次 18 块、7 段全部一次 HTTP 200 通过，`stale_source_out` 与 `not_within_verified` 均为空。
>
> ★★ **`artifacts/scene_plan.json` 是「脚本确认时」的快照**：
> `generate_scene_plan_from_script()` 在 `state["scenes"]` 已存在时**提前返回**，
> 所以重定时后它不会自己更新，必须手工对齐 `scenes[].{start,end}_seconds`
> 与 `total_duration_seconds`（schema 别动，它被 Remotion 分支读取）。

### ⑦b 局部修镜 + 重出预览（零付费）

```bash
# 1) 只换「用哪段素材的哪一秒」，不动时长/配音/数字人
python scripts/remake_retime_shots.py --project projects/<id> --patch shots-patch.json
python scripts/remake_retime_shots.py --project projects/<id> --patch shots-patch.json --apply

# 2) 复检生效窗口
python scripts/remake_material_screen.py audit --project projects/<id>

# 3) 重出预览 —— 排 full_preview（本地合成，零付费）
curl -X POST http://127.0.0.1:4754/api/production-queue/jobs -H "Content-Type: application/json" \
  -d '{"project_id":"<id>","kind":"full_preview","priority":"normal",
       "idempotency_key":"<id>-fp-<date>-v1","request":{"confirmed":true}}'
```

> ★★ **`full_preview` 会被「音量样板未确认」挡下（HTTP 422）**，报错原文
> 「声音设置已修改：请先生成并确认第一段音量样板，再生成全片」。
> 判据在 `_require_approved_music_sample()`：只要 `music_policy.enabled` 为真
> （选了 BGM）且 `music_policy.sample.status != "approved"` 就会拦。
> `avatar_review_preview` 能过是因为管线自己带内部能力令牌 + 前授权签名，
> **裸的 `full_preview` 没有**。解法是先跑一次本地样板（零付费）：
>
> ```bash
> # 生成（本地 ffmpeg 合成第一段人声+BGM，不联网、不扣费）
> curl -X POST http://127.0.0.1:4754/api/project/<id>/workbench/music-sample/jobs \
>   -H "Content-Type: application/json" -d '{"confirmed":true}'
> # 等 music_policy.sample.status 变成 ready，再确认
> curl -X POST http://127.0.0.1:4754/api/project/<id>/workbench/music-sample/approve \
>   -H "Content-Type: application/json" -d '{"confirmed":true}'
> ```
>
> 样板记录的 `policy_signature` 就是当前**混音指纹**
> （`_audio_mix_signature` = sha256(narration_gain_db + music_policy 签名 + 输出响度策略)）。
> 只改字幕/画面不会动它 → **确认一次即可长期复用**；一旦改了 BGM 或人声音量，
> 它会失配并要求重新生成确认（这是有意设计，别绕过）。

> ★★★ **改完画面千万不要为了重出预览而新起一条 `avatar_review_preview` 任务。**
> `_avatar_records()` 只从**当前任务自己的** `phases` 里读供应商记录，
> 新任务的 phases 是空的 → 7 轮数字人**全部重新提交 RunningHub 并重新扣费**
> （本次实测：为了一次本地换镜，多付了 ¥2.5 预留 / **¥0.989** 结算；
> 两次队列任务各自独立结算 **¥0.892 + ¥0.989 = ¥1.881**）。
> 三态保护只在**同一任务内**的重试/续跑生效，跨任务无效。
>
> 正确的判断链：`_mark_render_needs_refresh()` 会把 `preview_render.status`
> 置为 **`needs_refresh`**，而队列 worker 的 `_run_full_preview` 在
> status 不是 `completed`/`preview_ready` 时**直接调 `generate_full_preview_render()`**
> （纯本地 ffmpeg）→ 所以换镜后重出预览一律走 `full_preview`，零付费。


### ⑧ 输出质检

- `ffprobe`：时长、分辨率、帧率、音视频流。
- 抽帧目视：**全程无真人脸、无原素材烧入字幕**。
- 响度：看 `loudness.integrated_lufs` / `true_peak_dbtp`；
  ⚠️ 不要看 `final_review.audio_spotcheck`（归一化前测，稳定误报 clipping）。
  v002 实测 **−12.17 LUFS / −1.88 dBTP / LRA 1.4**（`target_lufs=-10`，`acceptance_enforced=false`
  → 差 2.17 dB 只记 `warning`，不阻断；短视频平台自己会归一化，可接受）。

#### audit 的窗口口径（2026-09-13 修）

`remake_material_screen.py audit` 原来用 `eff = 显示时长 × scene_scale` 推生效窗口，
`scene_scale` 取自 `timing-manifest.json` 的 `母版轮长 / 场景时长`。

> ★★ **这个模型只对「音频把场景改短」的上一期成立**（7fps 那种掐尾）。
> 本期母版比规格书**长**（+4.96 s），`scale > 1` 会让 `eff` 跑到 `source_out`
> 之外，而 `within_verified` 又恰好是拿 `source_out` 比的 → **两边同时失真**。
> 更糟的是它只覆盖 `显示时长 × scale`，**尾部 4%~6% 根本没进检测**。
>
> 已改成按真实窗口检测：`eff = 显示时长`（与渲染端一致，见 ⑦a），
> 并新增 `stale_source_out`（源出点与显示区间按帧不一致）与
> `not_within_verified`（生效窗口越出素材片长）两个独立字段。
> 本期复检：18 窗口、**50.2 s 全覆盖**、两个字段均为空。

**命中要区分真假**（本期 18 个窗口报了 1 处人脸 + 14 处字幕带，全部是误报）：

| 命中 | 真因 | 判据 |
|---|---|---|
| 人脸 | 驾驶位第一视角下**自己的大腿/短裤**肤色块 | `verify` 画框后目视：没有眼鼻嘴 |
| 字幕带 | 车身腰线 / 仪表台接缝 / 路面地平线 / 车标字母 | 窗口对准已知无字幕的镜头也会报 |

> ★ 别只看 `face_hits` 计数，也别只看 `band_hits` 计数——**两个都要 `verify` 抽帧目视**。
> 想省事就一次生成全片联系表（1 s 步长）通读，比逐个窗口跑 verify 快得多。

### ⑧b 竖屏字幕安全区（发布前必做）

**默认字幕样式对 9:16 短视频是不安全的**：内置 `subtitle-default` 是
`font_size=42` / `position.y=0.89`，实测文字底边落在 **0.8880**，即距画面底仅 215px；
而抖音等平台的底部作者/描述/话题块保守覆盖到 **0.80**（1536px）→
**旧默认比安全线还低 168px，上传必被遮挡**。这不是审美问题，是会被平台 UI 吃掉。

工具：`scripts/remake_subtitle_safe_area.py`（`probe` / `apply` / `measure`）

```bash
# 1) 试算：在合成画布上量候选 (字号 × 落点) 的真实像素框
python scripts/remake_subtitle_safe_area.py probe --project projects/<id> \
  --font-sizes 42 60 64 68 --ys 0.89 0.80 0.75

# 2) 写入（走工作台 API，落到 subtitle-default 模板，apply_scope=all）
python scripts/remake_subtitle_safe_area.py apply --project projects/<id> \
  --font-size 64 --y 0.75 --queue-preview

# 3) 复核成片
python scripts/remake_subtitle_safe_area.py measure --project projects/<id>
```

本次实测结论（1080×1920，19 条字幕，最长 18 字）：

| 配置 | 18 字行宽 | 文字高 | 底边 | 距底 | 距 0.80 安全线 |
|---|---|---|---|---|---|
| 旧 42 / y0.89 | 549（0.508） | 31 | 0.8880 | 215px | **−168px（侵入）** |
| 新 64 / y0.75 | 838（0.776） | 47 | 0.7464 | 487px | **+103px** |

> ★ **别按 1.0 em 估中文字宽**：libass + Microsoft YaHei 实测 CJK 字宽只有
> **≈0.727 em**（42 号 18 字仅占画布宽 0.508）。按 1.0 em 估会误判「不能再放大」，
> 白白放弃 40%+ 的字号空间。**一律以 `probe` 实测像素为准。**
>
> ★ **`WrapStyle: 2` = 不自动换行**：超宽不会折行，直接从画布边缘裁掉。
> 所以硬约束是「最长句字数 × 字宽 ≤ 画布宽」。本次 18 字在 80 号时已达 1044px（0.967）
> → `probe` 会标「★溢出画布」；68 号 890px（0.824）是可用上限。
>
> ★ **量成片时必须把检测窗口对准真实字幕带**：旧落点在 y≈1674、新落点在 y≈1387。
> 窗口开太大（如 1200–1850）会把画面本身的高亮（机身、手）当成字幕，
> 测出「宽 767」这种偏大的假值——同一个 18 字句在干净背景下其实是 549px。
> 十字参考：`measure --band 1330 1580`（新落点）/ `--band 1600 1780`（旧落点）。

---

## 2. 硬规则速查

| 规则 | 为什么 |
|---|---|
| 画面绑定「清洗母版 + 源区间」，不切小文件 | 时间轴不变、出处可追溯、切点不漂 |
| 清洗母版一律 `-an` | 复刻片必须用自己的配音，原博主声音不能进成片 |
| 人脸结论必须过一遍带框复核 | 检测两侧都有误差，比例数字不能直接采信 |
| 字幕用「位置稳定性」判定，不用单帧检测 | 产品 UI 素材会疯狂误报 |
| 时长先量后估 | 字数推算偏差可达 40%+，会连带把视觉区间缩放掉 |
| 生产走统一队列，不直调 runner | 队列是跨项目执行权威，保证幂等与恢复 |
| **换镜后重出预览只排 `full_preview`** | 新起 `avatar_review_preview` 会重新付费生成数字人 |
| **裸 `full_preview` 前先确认音量样板** | 否则 422：`music_policy.sample` 未 approved 会被 `_require_approved_music_sample` 拦 |
| **竖屏字幕底边必须 < 0.80** | 平台底部作者/描述块会吃掉字幕；内置默认 0.89 实测低 168px |
| **中文字宽按实测 ≈0.727 em，不按 1.0 em** | 按 1.0 em 估会误判「不能再放大」，白丢 40% 字号空间 |
| 付费前声明 provider / 实例 / 预算 | 不允许静默换供应商或模型 |
| 数字人母版落地后，**必须复核生效窗口** | 场景时长被音频重算，块会**等比变长或变短**；变长会把生效窗口顶出片长/安全窗（见 ⑦a） |
| **段时长 = 配音试配量出的真实秒数** | 让建项目时的缩放比压到 1.0，已复核的源窗口原样保留（本期逐段偏差 0.00s）。**但它只管到建项目为止**——成片时长归母版 |
| **成片场景时长看母版，不看试听值** | 母版每轮比 wav 长 0.5~0.8 s（本期 7 段共 +4.96 s）→ ⑥ 之后必须按帧重排画面块 |
| **生效窗口 ≡ `[source_in, source_in + 块时长]`** | 渲染端 `-ss source_in -t (end-start)`；源窗不会缩放（已用帧反解实证） |
| **`source_out` 必须写成 `source_in + 显示时长`** | 校验器按帧比对 `abs(显示帧数−源帧数) ≤ 1`，且 `source_out ≤ 素材时长+1/fps`，超了直接拒 |
| **重排块边界走 `PUT .../visual-timeline`** | 服务端权威端点；直接改 `workbench.json` 会被常驻进程覆盖 |
| **`scene_plan.json` 不会自动跟随重定时** | `generate_scene_plan_from_script()` 在 `scenes` 已存在时提前返回，要手工对齐（schema 别动） |
| **`audit` 的人脸/字幕带命中要 `verify` 画框定性** | 本期 1 人脸 + 14 字幕带全是误报（大腿肤色块 / 车身腰线 / 仪表台接缝） |
| **BGM −16 dB 的参照物是「人声 −7.5 dB」** | 母带 −9.24 LUFS 的热母带按 −16 dB 播放 ≈ −25.2 LUFS，人声 −17.7 LUFS；短片段跑 `loudnorm` 数字不可信（4.4 s 测出 +6 LU 的假差值） |
| **顶带 / 底带必须单独目视** | `screen` 的字幕带判据按整行边缘密度算，**角落水印与背景真人都会漏** |
| **「有人」不能只信人脸检测** | 侧脸/背影/只露手脚在大小两个阈值下都可能 0 命中（M1 19.0–22.6s 实测） |
| 建项目脚本要落 **`SAFE_WINDOWS` 白名单断言** | 排除区间来自目视复核而非记忆；越界直接拒绝建项目 |
| **`full_bleed` = `scale=increase,crop=W:H`（cover + 居中裁切）** | 16:9 母版铺满 9:16 只保留源 x **0.361–0.639**；主体不在中间就会被切掉，「左边有没有人」要在裁切后看 |
| **新项目的 `subtitle_styles` 是 init 时的旧默认（42 / y0.89）** | 上一期项目里改过的样式**不会**被新项目继承，每次都要重设 |
| 生产队列请求**不接受未知字段** | 多一个 `_comment` 就 400：`统一队列请求包含不受支持字段` |

---

## 3. 已知残留风险

- **★ 宽屏素材铺满竖屏只留中间 27.8% 的宽度。** 渲染端滤镜是
  `scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1`
  = **cover + 居中裁切**；16:9 母版（如 1280x632）铺满后只保留源 **x 0.361–0.639**。
  推论有两条，方向相反，都要用：
  - 好消息：画面**左右边缘**的人（M3 左侧 0–0.15、M1 车旁的人）天然被裁掉；
  - 坏消息：**主体不在中间就会被切掉**（M3 4.3s 的「SKYNOMAD」字标只剩「SKYNOM」），
    而且**纵向全保留** → 顶部/底部的烧入字幕与水印**不会**因为这层裁切而消失
    （这就是上一期 16:9 素材底部小字幕变成大白块的原因）。
  → 裁切几何可以直接按 `cover + center` 算，也可以先跑一帧 `full_bleed` 模拟图再定镜头。
- **★ 角落水印是「字幕带检测」的盲区。** 见 ①c。它不会被 `screen` 报出来，
  但如果它落在裁切后仍可见的区域（比如横向铺满的 16:9 母版底部），就一定会进成片。
  处理方式只有两种：清洗母版阶段机械裁掉，或换窗口。
- **★ 母版落地后的区间「缩短」不等于「安全」。** `_apply_timeline_update` 会等比缩放
  视觉块的 `start/end`，但**不动** `source_in/out`；渲染端按
  `-ss source_in -t block_duration` 取片 → 实际生效窗口 = **原窗口的前缀**，
  1× 速度、不循环、不漂移。
  单看这条推理，「无脸无字幕」是子集关系、应当继承——**但前提是原窗口真的验证过**。
  本次就是因为原窗口的验证不完整（整片筛漏掉短时字幕），前缀里照样带着烧入字幕。
  **结论：母版落地后必须跑 `audit`，不要相信推理。**
- **16:9 素材铺满 9:16 会放大残留瑕疵。** M3 是 1024×506，铺满 1080×1920 时
  垂直放大 **3.79×**，画面底部一条小字幕会变成一大块糊掉的白字。
  → 底部字幕要么在**清洗母版阶段裁掉**，要么换窗口，不能指望「铺满后会看不见」。
- 清洗母版的裁切比例要在**母版上**复核（用 `screen` 的底部活跃行），
  不能只按原片的 band 位置推算——原片 band 可能只是「其中一条」，另有更靠下的。
- 素材文件名可能含 **U+F8FF** 私有区字符（终端显示成 `_`）→ 一律按
  `aweme id` 匹配文件，不要手打文件名。
- **素材 id 与文件名的对应不要凭记忆。** 资产 id（`S-00x`）是按登记顺序生成的，
  和 `M1/M3/M4/M5` 的文件名前缀**不对应**；换镜前先从
  `artifacts/workbench.json` 的 `assets[].path` 读回真实映射。
- 含中文的路径下 `cv2.imread` / `imwrite` 会静默失败 → 用
  `np.fromfile + cv2.imdecode` / `cv2.imencode + tofile`。
- 抽帧必须 JPEG（`-pix_fmt yuvj420p -q:v 3`），PNG 会显著拖慢。
- 换镜补丁 JSON 里以 `_` 开头的键会被 `remake_retime_shots.py` 跳过，
  可用来写 `_comment`。


---

## 4. 同批多期「优化套用」批处理（9.14 batch 实操定稿）

> 场景：一期（参考期）已定稿若干优化项，用户要求「复刻到同批其他期」。
> 参考实现与全部脚本在 `.backlot/9.14-remake-8/`（8 期实测交付）。

### 4.1 先审计，别急着改代码

把 5 类优化分成「配置缺失」与「代码已内置」两堆——**后者只要重出就生效**：

| 项 | 判据 | 处理 |
|---|---|---|
| 音频策略 | 读 `workbench.json` 的 narration/music/loudness 三处 | 缺就离线改（不必起服务） |
| 顶部钩子标题 | `text_overlay_composition` 图层是否为空 | 空就批量写图层 |
| 数字人署名牌 | 同上 | 空就批量写图层 |
| 字幕落点 / 行尾标点 / 切句 | **全局内置代码** | **不改代码，重出即生效** |

★ 旧成片 ass 里还残留行尾标点 ⇒ **别据此以为要改代码**，那是重出前的产物。

### 4.2 钩子字号：唯一正解是「整体等比缩放」

参考期的两层字号比例（例：46 : 56）就是**全批统一观感**的锚点：

```python
BASE_SIZE_1, BASE_SIZE_2 = 46, 56      # 从参考期抄
SCALE_MIN, SCALE_MAX = 1.0, 1.30       # 上限防长行变巨型
CONTENT_W  = LAYER_W * CANVAS_W - 2*8  # 层宽 508 ⇒ 492
RIGHT_LIMIT = 562.0 - 10               # 硬右界再留余量
# 对每期求「最大的不超框 k」，两层同乘 k
```

- ❌ **逐行各自取最大号**：短行会飙到 79 号（参考期 56）⇒ 8 期观感散架。
- ✅ 锁定比例整体缩放：实测各期 `k ∈ [1.06, 1.22]`，字号落在 49~68 的窄带内。
- ★ **文案写长了就换文案，不要靠调字号救**：超框（Δw 几十 px）说明这句话本身太长。

### 4.3 响度天花板：算清楚再定目标，别硬撑 −10

收尾 `loudnorm` 用 `linear=true`（**单增益**）⇒

```
输出 I = min(目标 LUFS, −2.0 − 混音波峰因数)      # −2.0 是 AAC 过冲余量，固定
```

⇒ **人声增益抬不动上限**：人声 +1 dB，混音 I 与 TP 同时 +1 dB，波峰因数不变。
⇒ **母版更轻的那几期（如"檬檬"音色母版 I≈−20.7 / TP≈−5.8）天花板 ≈ −12.1**，
   硬把 `target_lufs` 设 −10 ⇒ **正式成片直接 raise「未达到发布容差」**。
⇒ 正确做法：**把该期目标改到物理可达值**（例 `-12.0`），而不是继续加人声增益。

★ **改 `target_lufs` 会把 `music_policy.sample` 打回 stale** ⇒ 批量驱动时
**必须带上 `music` 步**；只跑 `preview,approve,final` 会 422
「声音设置已修改：请先生成并确认第一段音量样板，再生成全片」。

### 4.4 批量重出：服务生命周期绑进同一条长驻命令

驱动脚本自己 `subprocess.Popen` 起 `backlot serve` 当**子进程**（不要 detached），
再在同一个命令里触发任务并轮询到完成——拆成两条命令 ⇒ job 只写 `generating` 就随服务消失。
单期失败（music/preview 阶段）即 break 该期、继续下一期，**绝不拖垮整批**。

### 4.5 验收：期望墨迹法（不需要"没有图层的基线视频"）

1. 用 `build_text_overlay_assets` 把**期望图层**栅格化；
2. 取纯墨迹掩码 = 只保留与该层主色 Δ 相近（≤40）且 `alpha>200` 的像素；
3. 在成片若干帧的同位 ROI 里数命中率 ⇒ `min > 0.77` 即判定图层确实在且全程在。
4. 字幕底边取 ass 的 `\pos` 真值（不用亮度阈值）；行尾剥标点用「行尾字符集」直接判。
5. 响度读 `render_report.json` 的 `integrated_lufs` / `true_peak_dbtp`。

### 4.6 对齐用户说法的"位置一样"

当用户说「名字不一样但位置要一样」时，**验收要落到坐标数字上**：
比对各期的 `tag_y` / `bottom_ratio` / 字幕 `pos_y` **集合是否只有一个值**。
等宽文案（如两组 5 字名）在同一胶囊几何下会得到同一落点。
