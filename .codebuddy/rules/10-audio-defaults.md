# 音频默认值与验收（改音频前必读）

适用：任何改动 Haike_video / OpenMontage 音频默认值、混音配比、成片响度的任务。
最后修订 2026-09-16（依据：抖音端实测，见 `docs/handoff/PRODUCT_RULES.md`）。

## 1. 软件级默认（唯一真值）

人声 **+8 dB** / BGM **−6.0 dB** / 成片 **−9.0 LUFS**（逐期可按母版峰值单独下调目标，不回溯改历史项目）。

| 项 | 真源头（改这里） | 落盘 |
|---|---|---|
| 人声 | `backlot/narration_preferences.py::DEFAULT_NARRATION_GAIN_DB` | `.backlot/narration_preferences.json` |
| BGM | `backlot/music_preferences.py::DEFAULT_PLAYBACK_GAIN_DB` | `.backlot/music_preferences.json` |
| 响度 | `backlot/output_loudness_preferences.py::DEFAULT_OUTPUT_TARGET_LUFS` | `.backlot/output_loudness_preferences.json` |

`read_*` 每次读盘、无缓存 ⇒ 改完不用重启工作台。**已有项目的 `artifacts/workbench.json` 已落库，不受默认值影响**，要改必须逐项目改。

## 2. ★ 默认值有多个副本，只改一处＝没改

改 BGM 默认时，这 6 处都在历史上各存过一份旧值（09-16 已统一到读软件级默认）：

1. `backlot/music_preferences.py::DEFAULT_PLAYBACK_GAIN_DB`
2. `.backlot/music_preferences.json`
3. 批次脚本 `pp8.py::AUDIO_MUSIC_DB`
4. 批次脚本 `audio8.py::TARGET_MUSIC_DB`
5. `themes.py::_bgm(gain=)` —— **13 处 `_bgm()` 调用全不传 gain ⇒ 它就是每份 spec 的真源头**
6. `scripts/remake_build_project.py` 的 `music.get(..., 兜底)` —— **「重建后声音又变小」的病根**（spec 里存旧值，一重建就打回）
7. `scripts/remake_build_project.py` 的 `spec.get("narration_gain_db") or 0.0` —— **人声侧同款病根**（2026-09-16 修）：所有 spec 都不写该键 ⇒ 每次重建把口播增益写成 **0 dB**。实测 `matext2-remake-1` 重建后 `narration_policy.playback_gain_db = 0.0`（同期其余期为 +8.0）。现改为兜底 `DEFAULT_NARRATION_GAIN_DB`，并用 `clamp_narration_gain_db` 收敛。
   它是**静默**的：不报错、预览能过，只有上手机听才发现口播发闷 —— 与 BGM 那条一样属于「重建后声音变了」家族。

改完必须**回读校验**：四处默认读取一致 + 存量 `remake-spec.json` / `workbench.json` 刷齐 + 幂等复跑 0 项待改。
现成工具：`.backlot/9.14-remake-8/_diag/setbgmdflt8.py`（预览 / `--apply`，会断言人声与响度未被动）。
**旧批次目录也要顺手刷**：`.backlot/9.14-remake/{dji,tesla,matext2}/remake-spec.json` 长期停在 −16.0，
一旦被 rebuild 引用就会把 BGM 打回旧增益 ⇒ 跑 `--extra-root .backlot/9.14-remake`（可重复）一并刷齐。
`.backlot/9.12-apple-fold`、`.backlot/9.13-pengcheng-n90` 同样是历史值，已交付不再回炉，仅作参考。

## 3. ★ 验收判据：只看音频包络 P5/P25

BGM 只在**说话间隙**露头 ⇒ 只要看包络分位（P5/P25）。
**不得用整片 200–800 Hz 的 LUFS 当判据** —— 该带由人声主导，抬 BGM 几乎不动它，正确改动会被误判 FAIL（已踩）。
合格线：P5 ≥ **+2.0 dB**、P25 ≥ **+2.0 dB**、全频 ΔI ≤ +2.0 dB、`TP ≤ −1.0`、`|I − target| ≤ 1.5`。
现成工具：`.backlot/9.14-remake-8/_diag/bgmcheck8.py`（逐期对比新旧备份分带 + 包络）。

## 4. ★ 响度有物理天花板，抬人声改不动它

收尾 `loudnorm linear=true` 是单增益 ⇒ `I = min(目标, −2.0 − 波峰因数)`。
抬人声 1 dB ⇒ I 与 TP 同时抬 1 dB、波峰因数不变 ⇒ **抬不动**。
- 母版偏轻的期（实测 檬檬音色：I≈−20.7 / TP≈−5.8）必须 **单独下调该期 `target_lufs`**（用 `set_lufs8.py <key> <目标>`），否则正式渲染 raise「未达到发布容差」。
- **抬高 BGM 会推高波峰因数 ⇒ 天花板下降**。贴边的期（如 gpu）必须同步下调目标并**带上 `music` 步一起重跑**（只跑 preview,approve,final 会 422）。
- 改 `target_lufs` 会把 `music_policy.sample` 打回 stale ⇒ 必须重跑 `music`。

## 5. ★ 平台端事实（实测，不要再靠推测）

- 抖音回放**不做大幅整体衰减**（实测全频仅 −1.1 dB）⇒「声音被整体调小」不成立；**下调 `target_lufs` 只会让整条更小，不会让 BGM 变清楚**。
- 抖音**专门削低电平内容**：P5 −4.4 / P25 −4.3 dB，而那正是 BGM 唯一的露头处。
- 手机单扬声器 **<300 Hz 衰减 15~20 dB** ⇒ BGM 低频在手机上直接消失。
- ⇒ **抬 BGM 是唯一有效杠杆**；不要靠降响度「避开压制」。

拿平台实证的手法（**三步必须都做**）：
1. CreatorHub `POST /api/share-download`（body 带 `share_text` + `account_id`）把作品取回本地；
2. 用 `_diag/band2.py` 分带 + 包络（全频 / <200 / 200–800 / 800–3k / >3k + P5/P25/P50）；
3. **必须同时用 `-c:a aac -b:a 53k` 重编本地原片当对照组**，否则分不清编码损失与平台处理。

## 6. 改完的连带动作

- 正式成片不达标**直接 raise**（预览只告警）；改任何声音设置后必须重做第一段声音样板并确认。
- 只换音频/换镜时走 `full_preview`（零付费），**不要**起 `avatar_review_preview`（重扣 RunningHub 费）。
- 动了 `docs/handoff/*` 后运行 `python scripts/audit_context_handoff.py`（每份文件有非空白字符预算，超了要压回）。
