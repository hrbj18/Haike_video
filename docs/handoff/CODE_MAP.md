# 代码地图

更新时间：2026-09-08

## 启动与前端

- 启动：`启动工作台.bat`；安全重载：`更新并重启工作台.bat`
- 后台服务与 API：`backlot/server.py`
- 项目库：`backlot/ui/index.html`、`library.js`、`board.css`
- 自动生产中心：`backlot/ui/automation.*`，路由 `/automation`
- 片段工作台：`backlot/ui/workbench.html`、`workbench.js`、`workbench.css`；项目库与工作台共享 `backlot.theme` 浅色/深色偏好
- 多标题/新闻层/纯音乐：`text_overlay_composition.py`、`workbench.py`、`video_compose.py`、`server.py`、`ui/workbench.*`；Cybercab脚本见 `scripts/*cybercab*`
- 通用配音中心：`backlot/audio_center.py` 与 `backlot/ui/audio_center.*`
- V1.3 一键预览：`review_preview_pipeline.py`、`workbench.py`、`hyperframes_compose.py`、`server.py`、`workbench.js/.css`
- 统一队列：`production_queue*.py`、`server.py`、`__main__.py`
- 语义双层画面：`workbench.py`、`CinematicRenderer.tsx`、`video_compose.py`
- 本地导入与长视频索引：`backlot/media_index.py`、`backlot/server.py`、`backlot/workbench.py`、`backlot/ui/workbench.*`
- 素材视觉理解 V2：`media_index.py`、`ai_vision.py`、`material_vision_eval.py`；身份由 `vision_runtime_identity()` 冻结。
- 户外互动首次切片：`material_interactions.py`、`material_interaction_review.py`、`material_audio_evidence.py`、`material_interaction_edit.py`、`material_interaction_render.py`、`material_interaction_candidates.py`。二次精剪：`material_interaction_story.py`、`material_interaction_second_pass*.py`；接线在 `workbench.py`、`server.py`、`task_center.py`、`ui/workbench.*`；验收 `scripts/accept_interaction_second_pass.py`。

## 每日科技快报

- 配置、热度采集、调度、供应商门、预算与付费账本：`backlot/daily_automation.py`
- copy_skill 候选池 V2 与幂等快照：`backlot/copy_skill_hotspot_feed.py`
- V2 选题/聚类/证据门：`backlot/news_selection_v2.py`；脚本/冷审/`story_id`：`daily_script_v2.py`；恢复账本：`daily_text_resilience.py`
- 主模型/豆包配置与职责路由：`backlot/ai_text.py`；批准样稿：`backlot/golden_scripts/`
- OpenMontage 本地配音、RunningHub、切割、失败画面槽单独恢复、合成与 QA：`backlot/daily_pipeline.py`
- 命令：`daily_cli select-v2|script-v2 --target-date YYYY-MM-DD`；Windows 包装器：`scripts/run_daily_automation.py`
- 前端 API：`backlot/server.py` 中 `/api/daily-automation/*`
- 调度真实状态：`scheduler_runtime_status()`、`scheduler_effective_state()`；事务开关：`apply_config_with_scheduler()`
- 台词默认增益：`backlot/narration_preferences.py`；项目声音合同与统一混音：`backlot/workbench.py`
- 全局配置：`config/daily_tech_brief.json`
- 运行状态：`.backlot/daily-runs/<日期>/daily_run.json`
- V2产物、文本/付费账本与单实例锁：`.backlot/daily-runs/`

## 关键媒体模块

- RunningHub：`runninghub_avatar.py`、`avatar_cloud.py`、`avatar_audio_clock.py`、`runninghub_config.py`；生产模板见 `config/runninghub/`
- 有数字人一键预览、有限 OOM 恢复和安全点：`backlot/avatar_review_preview_pipeline.py`
- 本地 TTS：`openmontage_tts_engine.py`、`openmontage_tts_server.py`、`voicebox_tts.py`；安装/启动/迁移见 `scripts/*local_tts*`
- 长视频导入与切割：`backlot/avatar_import.py`
- 片段、素材、字幕、小标题、画面回退与成片归一化：`backlot/workbench.py`
- 自动视觉导演（候选过滤、项目内模型评分、确定性回退）：`backlot/visual_director.py` 与 `backlot/ai_text.py`
- Pexels 下载前候选与预览帧：`tools/video/stock_sources/pexels.py`
- HyperFrames：`tools/video/hyperframes_compose.py`；浏览器故障与布局故障分开报告
- 最终合成：`tools/video/video_compose.py`、`tools/audio/audio_mixer.py`

## 测试入口

- 每日自动化：`test_daily_automation.py`、`test_news_selection_v2.py`、`test_daily_script_v2.py`、`test_daily_text_resilience.py`、`test_daily_pipeline.py`
- copy_skill 热点只读接入：`tests/backlot/test_copy_skill_hotspot_feed.py`
- 工作台与声音：`tests/backlot/test_workbench.py`、`test_server.py`、`test_narration_preferences.py`
- 通用多标题同屏与新闻受管层：`tests/backlot/test_text_overlay_composition.py`、`test_review_preview_ui_contract.py`；真实渲染入口 `python scripts/accept_text_overlay_composition.py`
- 语义画面与素材理解：`test_media_index.py`、`test_workbench.py` 的布局/本地上传/恢复、`test_review_preview_ui_contract.py`、`test_ui_bug_bash.py`、`test_cinematic_remotion_adapter.py`
- 素材视觉理解 V2：`test_ai_vision.py`、`test_material_vision_eval.py`、`test_material_vision_ui_contract.py`、`test_media_index.py`
- 户外二次精剪：`test_material_interaction_story.py`、`test_material_interaction_second_pass*.py`、`test_material_interaction_review.py`
- 无人值守包装器：`tests/backlot/test_daily_scheduler_wrapper.py`
- 一键审核预览：`tests/backlot/test_review_preview_pipeline.py`、`test_review_preview_server.py`、`test_review_preview_ui_contract.py`；批量画面底层回归仍在 `test_workbench.py`
- RunningHub：`tests/tools/test_runninghub_avatar.py`
- 上下文包：`tests/unit/test_context_handoff.py`

定位时先用 `rg` 搜索上述入口；不默认读取整个 `projects/` 或旧文档。
