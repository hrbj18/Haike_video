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

## 每日科技快报

- 配置、热度采集、调度、媒体放行纯函数、供应商资格门、幂等预算及付费操作账本：`backlot/daily_automation.py`
- copy_skill 候选池 V2 与幂等快照：`backlot/copy_skill_hotspot_feed.py`
- 独立新闻素材选择 V2（聚类、E/R 门、H/C/U、资源规划）：`backlot/news_selection_v2.py`
- V2 正式选题、证据门与降级：`backlot/news_selection_v2.py`
- V2 正式脚本、冷审与 `story_id` 合同：`backlot/daily_script_v2.py`
- 文本韧性控制器（Top 3组合、attempt/progress 指纹、重复拒稿抑制、新鲜写稿/换组合升级、最佳稿与幂等账本）：`backlot/daily_text_resilience.py`
- 主模型/豆包独立安全配置、每日快报职责路由与连接测试：`backlot/ai_text.py`；用户批准样稿：`backlot/golden_scripts/`
- OpenMontage 本地配音、RunningHub、切割、失败画面槽单独恢复、合成与 QA：`backlot/daily_pipeline.py`
- 无付费媒体预检、本地配音隐藏启动与阶段瞬时故障重试：`backlot/daily_pipeline.py`；Windows包装器级同日期重试：`scripts/run_daily_automation.py`
- 命令入口：`backlot/daily_cli.py`
- V2 命令：`python -m backlot.daily_cli select-v2 --target-date YYYY-MM-DD`
- V2 测试稿：`python -m backlot.daily_cli script-v2 --target-date YYYY-MM-DD`
- Windows 稳定包装器：`scripts/run_daily_automation.py`
- 前端 API：`backlot/server.py` 中 `/api/daily-automation/*`
- 调度真实状态：`scheduler_runtime_status()`、`scheduler_effective_state()`；事务开关：`apply_config_with_scheduler()`
- 人物台词默认增益与原子持久化：`backlot/narration_preferences.py`；项目声音合同、`audio_mix_signature`、试听失效和统一混音顺序：`backlot/workbench.py`
- 全局配置：`config/daily_tech_brief.json`
- 运行状态：`.backlot/daily-runs/<日期>/daily_run.json`
- V2 独立产物：`.backlot/daily-runs/<日期>/topic_selection_v2.json`、`news_research_v2.json`、`news_selection_v2_run.json`
- 文本恢复账本：`.backlot/daily-runs/<日期>/daily_text_attempts.json`
- 付费操作账本：`.backlot/daily-runs/<日期>/paid_operations.json`
- 单实例锁：`.backlot/daily-runs/.daily-production.lock`

## 关键媒体模块

- RunningHub：`tools/avatar/runninghub_avatar.py`、`backlot/avatar_cloud.py`；最终 WAV 精确帧时钟：`backlot/avatar_audio_clock.py`；安全配置持久化：`backlot/runninghub_config.py`；生产模板：`config/runninghub/workflow-2094449979141218305.api.json`；短样本付费验收：`scripts/accept_runninghub_exact_clock.py`
- 有数字人一键预览父任务、有限 OOM 恢复、尾静音规范化与安全点继续：`backlot/avatar_review_preview_pipeline.py`；测试入口：`tests/backlot/test_avatar_review_preview_pipeline.py`、`test_review_preview_server.py`、`test_review_preview_ui_contract.py`
- 内置 Qwen3-TTS 引擎、串行任务与音色目录：`tools/audio/openmontage_tts_engine.py`
- 本地 HTTP 服务：`tools/audio/openmontage_tts_server.py`
- 历史调用兼容层：`tools/audio/voicebox_tts.py`
- 安装/启动/旧音色迁移/私有音色包：`scripts/setup_local_tts.ps1`、`scripts/start_local_tts.ps1`、`scripts/migrate_voicebox_tts.py`、`scripts/local_tts_profiles.py`
- 长视频导入与切割：`backlot/avatar_import.py`
- 片段、素材、字幕、按 `story_id` 复用的小标题层、HyperFrames失败回退、`contextual_broll`比例修复和成片 -14 LUFS 归一化：`backlot/workbench.py`
- 自动视觉导演（候选过滤、项目内模型评分、确定性回退）：`backlot/visual_director.py` 与 `backlot/ai_text.py`
- Pexels 下载前候选与预览帧：`tools/video/stock_sources/pexels.py`
- HyperFrames：`tools/video/hyperframes_compose.py`；横屏流程图为结论条预留安全区，浏览器启动故障与布局故障分开报告；每日新闻场景由 `headline_policy` 禁止重复烘焙右上角标题
- 最终合成：`tools/video/video_compose.py`、`tools/audio/audio_mixer.py`

## 测试入口

- 每日自动化：`tests/backlot/test_daily_automation.py`、`test_news_selection_v2.py`、`test_daily_script_v2.py`、`test_daily_text_resilience.py`、`test_daily_pipeline.py`、`test_daily_automation_workbench.py`
- copy_skill 热点只读接入：`tests/backlot/test_copy_skill_hotspot_feed.py`
- 工作台与声音：`tests/backlot/test_workbench.py`、`test_server.py`、`test_narration_preferences.py`
- 无人值守包装器：`tests/backlot/test_daily_scheduler_wrapper.py`
- 一键审核预览：`tests/backlot/test_review_preview_pipeline.py`、`test_review_preview_server.py`、`test_review_preview_ui_contract.py`；批量画面底层回归仍在 `test_workbench.py`
- RunningHub：`tests/tools/test_runninghub_avatar.py`
- 上下文包：`tests/unit/test_context_handoff.py`

定位问题时先使用 `rg` 搜索上述入口；不要默认读取整个 `projects/`、媒体文件或所有旧指导文档。
