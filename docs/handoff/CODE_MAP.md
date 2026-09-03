# 代码地图

更新时间：2026-09-01

## 启动与前端

- 一键启动：`启动工作台.bat`
- 后台服务与 API：`backlot/server.py`
- 项目库：`backlot/ui/index.html`、`library.js`、`board.css`
- 自动生产中心：`backlot/ui/automation.html`、`automation.js`、`automation.css`，页面路由 `/automation`
- 片段工作台：`backlot/ui/workbench.html`、`workbench.js`、`workbench.css`；项目库与工作台共享 `backlot.theme` 浅色/深色偏好
- 通用配音中心：`backlot/audio_center.py` 与 `backlot/ui/audio_center.*`
- V1.3 一键预览：`review_preview_pipeline.py`、`workbench.py`、`tools/video/hyperframes_compose.py`、`server.py`、`workbench.js/.css`
- 语义双层画面 V1：`workbench.py` 的 `visual_composition`/统一物化、`CinematicRenderer.tsx` 的 `focus_card`、`video_compose.py`
- 本地导入与长视频索引：`backlot/media_index.py`、`backlot/server.py`、`backlot/workbench.py`、`backlot/ui/workbench.*`
- 素材视觉理解 V2：`media_index.py`、`ai_vision.py`、`material_vision_eval.py`；`vision_runtime_identity()` 冻结模型、提示词、Schema 与图片合同的缓存身份；指导文档 `docs/SINGLE_DEVELOPMENT_GUIDE_MATERIAL_VISION_V2_ZH-CN.md`

## 每日科技快报

- 配置、热度采集、调度、供应商门、预算与付费账本：`backlot/daily_automation.py`
- copy_skill 候选池 V2 与幂等快照：`backlot/copy_skill_hotspot_feed.py`
- V2 正式选题、聚类、证据门、H/C/U 与资源规划：`backlot/news_selection_v2.py`
- V2 正式脚本、冷审与 `story_id` 合同：`backlot/daily_script_v2.py`
- 文本恢复、尝试指纹与幂等账本：`backlot/daily_text_resilience.py`
- 主模型/豆包配置与职责路由：`backlot/ai_text.py`；批准样稿：`backlot/golden_scripts/`
- OpenMontage 本地配音、RunningHub、切割、失败画面槽单独恢复、合成与 QA：`backlot/daily_pipeline.py`
- 媒体预检、配音启动和瞬时故障恢复：`backlot/daily_pipeline.py`、`scripts/run_daily_automation.py`
- 命令入口：`backlot/daily_cli.py`
- V2 命令：`daily_cli select-v2|script-v2 --target-date YYYY-MM-DD`
- Windows 稳定包装器：`scripts/run_daily_automation.py`
- 前端 API：`backlot/server.py` 中 `/api/daily-automation/*`
- 调度真实状态：`scheduler_runtime_status()`、`scheduler_effective_state()`；事务开关：`apply_config_with_scheduler()`
- 台词默认增益：`backlot/narration_preferences.py`；项目声音合同与统一混音：`backlot/workbench.py`
- 全局配置：`config/daily_tech_brief.json`
- 运行状态：`.backlot/daily-runs/<日期>/daily_run.json`
- V2 独立产物：`.backlot/daily-runs/<日期>/topic_selection_v2.json`、`news_research_v2.json`、`news_selection_v2_run.json`
- 文本恢复账本：`.backlot/daily-runs/<日期>/daily_text_attempts.json`
- 付费操作账本：`.backlot/daily-runs/<日期>/paid_operations.json`
- 单实例锁：`.backlot/daily-runs/.daily-production.lock`

## 关键媒体模块

- RunningHub：`runninghub_avatar.py`、`avatar_cloud.py`、`avatar_audio_clock.py`、`runninghub_config.py`；生产模板见 `config/runninghub/`
- 有数字人一键预览、有限 OOM 恢复和安全点：`backlot/avatar_review_preview_pipeline.py`
- 内置 Qwen3-TTS 引擎、串行任务与音色目录：`tools/audio/openmontage_tts_engine.py`
- 本地 HTTP 服务：`tools/audio/openmontage_tts_server.py`
- 历史调用兼容层：`tools/audio/voicebox_tts.py`
- TTS 安装/启动/迁移：`scripts/setup_local_tts.ps1`、`start_local_tts.ps1`、`migrate_voicebox_tts.py`、`local_tts_profiles.py`
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
- 语义双层画面与素材理解：`tests/backlot/test_media_index.py`、`test_workbench.py` 中 `visual_composition`/本地上传/恢复用例、`test_review_preview_ui_contract.py`、`test_ui_bug_bash.py`、`tests/tools/test_cinematic_remotion_adapter.py`
- 素材视觉理解 V2：`test_ai_vision.py`、`test_material_vision_eval.py`、`test_material_vision_ui_contract.py`、`test_media_index.py`
- 无人值守包装器：`tests/backlot/test_daily_scheduler_wrapper.py`
- 一键审核预览：`tests/backlot/test_review_preview_pipeline.py`、`test_review_preview_server.py`、`test_review_preview_ui_contract.py`；批量画面底层回归仍在 `test_workbench.py`
- RunningHub：`tests/tools/test_runninghub_avatar.py`
- 上下文包：`tests/unit/test_context_handoff.py`

定位问题时先使用 `rg` 搜索上述入口；不要默认读取整个 `projects/`、媒体文件或所有旧指导文档。
