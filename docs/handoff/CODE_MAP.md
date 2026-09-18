# 代码地图

更新时间：2026-09-18

## 启动与前端

- 启动 `启动工作台.bat`；安全重载 `更新并重启工作台.bat`；无沙箱启动器 `scripts/launch_backlot.py`
- 服务与 API `backlot/server.py`；项目库 `ui/index.html`、`library.js`
- 自动生产中心 `ui/automation.*`（`/automation`）；片段工作台 `ui/workbench.*`；配音中心 `backlot/audio_center.py`、`ui/audio_center.*`；统一队列 `production_queue*.py`、`__main__.py`
- 多标题/新闻层/纯音乐/语义双层 `text_overlay_composition.py`、`workbench.py`、`tools/video/video_compose.py`、`CinematicRenderer.tsx`

## 素材理解与切割

- 本地导入、长视频索引与视觉理解 V2（身份由 `vision_runtime_identity()` 冻结）`media_index.py`、`workbench.py`、`ui/workbench.*`、`ai_vision.py`、`material_vision_eval.py`
- 素材证据层 V1 `material_evidence.py`、`material_audio_envelope.py`、`material_motion_timeline.py`、`material_pause_evidence.py`（各一次解码，签名只含素材身份）
- 户外互动首次切片 `material_interactions.py`、`material_interaction_{review,edit,render,candidates}.py`、`material_audio_evidence.py`
- 短语级语音单元 `material_interaction_units.py`（边界只落 VAD 间隙，`_hard_split` 不切 ASCII 串）
- 二次精剪 `material_interaction_story.py`（`_anchor_edges` 锚定）、`material_interaction_second_pass*.py`（v7：`gap_policy`/`pause_survivor_seconds`/单元级字幕）；接线 `workbench.py`、`server.py`、`task_center.py`
- 粗剪并发 `interaction_concurrency.py`（回串行 `HAIKE_FORCE_SERIAL=1`）、基准台 `scripts/benchmark_interaction_concurrency.py`
- 剪辑决策导出 `material_interaction_export.py`（`cut-list-v1`/FCP7/OTIO/SRT）
- 推荐排序 v2 `material_interaction_recommend.py`；读接口 `GET .../interactions?order_mode=&w=`；界面 `ui/workbench.js::renderMaterialInteractionRankLayout`

## 关键媒体模块

- RunningHub `runninghub_avatar.py`、`avatar_cloud.py`、`avatar_audio_clock.py`、`runninghub_config.py`；模板 `config/runninghub/`；48GB 派生与付费探针 `scripts/build_runninghub_infinitetalk_48g_workflow.py`、`scripts/probe_infinitetalk_48g_variants.py`
- 有数字人一键预览、OOM 恢复与安全点 `avatar_review_preview_pipeline.py`
- 配音：腾讯云 `tencent_asr.py`（长音轨分片+ASR 并发）、`tencent_config.py`、`lib/tencent_cloud.py`；本地 `openmontage_tts_{engine,server}.py`、`voicebox_tts.py`
- 长视频导入与切割 `avatar_import.py`；视觉导演 `visual_director.py`、`ai_text.py`；Pexels `tools/video/stock_sources/pexels.py`
- HyperFrames 与一键预览 `tools/video/hyperframes_compose.py`、`review_preview_pipeline.py`；最终合成 `tools/video/video_compose.py`、`tools/audio/audio_mixer.py`
- 声音默认与口播链 `narration/music/output_loudness_preferences.py`、`workbench.py::NARRATION_PROCESSING_CHAIN`
- 跨平台库 `lib/ffmpeg_locator.py`（不依赖 PATH）、`lib/subprocess_window.py`（隐藏窗口）；Remotion `NewsAnchor.tsx`、`dark-tech-news.yaml`

## 外站素材复刻

- 工作流 `skills/creative/material-remake-workflow.md`；政策 `docs/MATERIAL_SELECTION_POLICY_V1_ZH-CN.md`；样例 `projects/apple-fold-duo-remake-1/`
- `remake_material_screen.py`（`screen|pick|verify|audit`）、`remake_reference_transcript.py`（腾讯云 ASR，三态防重复计费）、`remake_build_project.py`、`remake_retime_shots.py`（换镜零付费）、`remake_subtitle_safe_area.py`（字幕安全区）
- 机读契约 `schemas/remake-spec-v1.json`；离线口径层 `remake_project.py`（规格书→`script.json`/视觉时间线纯函数）

## 跨仓研究包与编辑层

- 权威校验 `copy_skill_research_pack.py`；冻结快照 `research_pack_snapshot.py`；账本 intake `research_pack_intake.py`；CLI `scripts/remake_editorial_cli.py`（fail-closed，退出码 2/3/4）；子命令 `python -m backlot research-pack`
- 编辑层 `remake_intake.py`、`remake_editorial.py`、`remake_editorial_verdict.py`

## 每日科技快报

- 采集/调度/供应商门/预算账本 `daily_automation.py`；候选池 `copy_skill_hotspot_feed.py`；选题证据门 `news_selection_v2.py`；脚本/冷审 `daily_script_v2.py`；恢复 `daily_text_resilience.py`
- 配音/切割/失败槽恢复/合成/QA `daily_pipeline.py`；CLI `daily_cli select-v2|script-v2`、`scripts/run_daily_automation.py`；API `/api/daily-automation/*`；状态 `.backlot/daily-runs/<日期>/daily_run.json`

## 测试入口

- 每日自动化 `test_daily_*.py`、`test_news_selection_v2.py`；工作台与声音 `test_{workbench,server,narration_preferences,audio_center}.py`
- 素材理解与证据 `test_{media_index,ai_vision}.py`、`test_material_evidence*.py`、`test_material_{audio_envelope,motion_timeline,pause_evidence}.py`
- 二次精剪与 Cut V2 `test_material_interaction_*.py`、`test_interaction_concurrency.py`、`test_tencent_asr_long_audio.py`
- 复刻与研究包 `test_remake_{intake,project,editorial,editorial_cli,editorial_verdict}.py`、`test_research_pack_{cross_repo,intake}.py`、`test_copy_skill_research_pack.py`
- 上下文包 `tests/unit/test_context_handoff.py`；RunningHub `tests/tools/test_runninghub_avatar.py`
