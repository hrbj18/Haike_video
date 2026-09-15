# 代码地图

更新时间：2026-09-15

## 启动与前端

- 启动：`启动工作台.bat`；安全重载：`更新并重启工作台.bat`；无沙箱启动器 `scripts/launch_backlot.py`
- 后台服务与 API：`backlot/server.py`；项目库 `backlot/ui/index.html`、`library.js`、`board.css`
- 自动生产中心 `backlot/ui/automation.*`（`/automation`）；片段工作台 `backlot/ui/workbench.*`（共享 `backlot.theme` 明暗偏好）
- 通用配音中心：`backlot/audio_center.py`、`ui/audio_center.*`；统一队列 `production_queue*.py`、`__main__.py`
- 多标题/新闻层/纯音乐/语义双层：`text_overlay_composition.py`、`workbench.py`、`tools/video/video_compose.py`、`CinematicRenderer.tsx`
- V1.3 一键预览：`review_preview_pipeline.py`、`tools/video/hyperframes_compose.py`

## 素材理解与切割

- 本地导入与长视频索引：`media_index.py`、`server.py`、`workbench.py`、`ui/workbench.*`
- 素材视觉理解 V2（身份由 `vision_runtime_identity()` 冻结）：`ai_vision.py`、`material_vision_eval.py`
- 素材证据层 V1：`material_evidence.py`、`material_audio_envelope.py`、`material_motion_timeline.py`、`material_pause_evidence.py`（各一次解码，签名只含素材身份）
- 户外互动首次切片：`material_interactions.py`、`material_interaction_{review,edit,render,candidates}.py`、`material_audio_evidence.py`
- 短语级语音单元：`material_interaction_units.py`（`build_spoken_units`/`speech_blocks`；边界只落 VAD 间隙）
- 二次精剪：`material_interaction_story.py`（`_anchor_edges` 锚定）、`material_interaction_second_pass*.py`（v7：`gap_policy`/`pause_survivor_seconds`/单元级字幕）；接线 `workbench.py`（含 `export_..._deliverables`）、`server.py`、`task_center.py`；验收 `scripts/accept_interaction_{second_pass,phrase_unit_v4}.py`
- 粗剪并发：唯一入口 `interaction_concurrency.py`（`run_bounded`/`resolve_limit`；回串行 `HAIKE_FORCE_SERIAL=1`）；基准台 `scripts/benchmark_interaction_concurrency.py`
- 剪辑决策导出：`material_interaction_export.py`（`build_cut_list`/`fcp7_xml`/`otio`/`srt`，schema `cut-list-v1`）
- 粗剪推荐排序 v2：`material_interaction_recommend.py`；读接口 `GET .../interactions?order_mode=&w=`；界面 `ui/workbench.js::renderMaterialInteractionRankLayout`

## 关键媒体模块

- RunningHub：`runninghub_avatar.py`、`avatar_cloud.py`、`avatar_audio_clock.py`、`runninghub_config.py`；模板 `config/runninghub/`
- 有数字人一键预览、OOM 恢复与安全点：`avatar_review_preview_pipeline.py`
- 配音：腾讯云 `tencent_asr.py`（长音轨分片+ASR 并发）、`tencent_config.py`、`lib/tencent_cloud.py`、`tools/audio/tencent_tts.py`；本地 `openmontage_tts_engine.py`、`openmontage_tts_server.py`、`voicebox_tts.py`（安装见 `scripts/*local_tts*`）
- 长视频导入与切割 `avatar_import.py`；片段/素材/字幕/小标题/回退/归一化 `workbench.py`
- 自动视觉导演 `visual_director.py`、`ai_text.py`；Pexels `tools/video/stock_sources/pexels.py`
- HyperFrames `tools/video/hyperframes_compose.py`；最终合成 `tools/video/video_compose.py`、`tools/audio/audio_mixer.py`
- 跨平台库：`lib/ffmpeg_locator.py`（ffmpeg/ffprobe 定位，不依赖 PATH）、`lib/subprocess_window.py`（子进程隐藏窗口）；Remotion `src/components/NewsAnchor.tsx` 与 `styles/dark-tech-news.yaml`

## 外站素材复刻

- 工作流 `skills/creative/material-remake-workflow.md`；样例 `projects/apple-fold-duo-remake-1/`＋`.backlot/9.12-apple-fold/`
- `remake_material_screen.py`（`screen|pick|verify|audit`）、`remake_reference_transcript.py`（腾讯云 ASR，三态防重复计费）、`remake_build_project.py`、`remake_retime_shots.py`（换镜零付费）、`remake_subtitle_safe_area.py`（字幕安全区）

## 每日科技快报

- 采集/调度/供应商门/预算账本 `daily_automation.py`；候选池 V2 `copy_skill_hotspot_feed.py`；选题证据门 `news_selection_v2.py`；脚本/冷审 `daily_script_v2.py`；恢复账本 `daily_text_resilience.py`
- 配音/切割/失败槽恢复/合成/QA `daily_pipeline.py`；CLI `daily_cli select-v2|script-v2`，包装器 `scripts/run_daily_automation.py`；API `/api/daily-automation/*`
- 调度 `scheduler_runtime_status()`；台词增益 `narration_preferences.py`；配置 `config/daily_tech_brief.json`；运行状态 `.backlot/daily-runs/<日期>/daily_run.json`

## 测试入口

- 每日自动化：`test_daily_*.py`、`test_news_selection_v2.py`
- 工作台与声音：`test_workbench.py`、`test_server.py`、`test_narration_preferences.py`、`test_audio_center.py`
- 素材理解与证据：`test_media_index.py`、`test_ai_vision.py`、`test_material_evidence*.py`、`test_material_{audio_envelope,motion_timeline,pause_evidence}.py`
- 二次精剪与 Cut V2：`test_material_interaction_{units,story,recommend,second_pass*,review,interactions,export}.py`、`test_interaction_concurrency.py`、`test_tencent_asr_long_audio.py`
- 一键审核预览：`test_review_preview_pipeline.py`、`test_review_preview_server.py`；RunningHub `tests/tools/test_runninghub_avatar.py`；上下文包 `tests/unit/test_context_handoff.py`

定位时先用 `rg` 搜索上述入口。
