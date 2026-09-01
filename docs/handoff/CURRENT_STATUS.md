# 当前项目状态

更新时间：2026-09-01

## 主线状态

有数字人口播一键审核预览 V1.4 已通过本机真实短样本验收：4 句、双主持、两位 Standard 24GB 数字人、精确帧音频、自动切割、主体画面、字幕与全片预览完整到达 `completed/review_ready`。最终预览为 H.264/AAC、1080×1920、30 FPS、约 16.5 秒；任务计数 7/7、失败 0。一次明确终态 OOM 只重试失败角色，已成功角色和配音均被复用；总费用低于 0.50 元，未使用 Plus。

供应商原片只在严格尾静音区缺少少量帧。系统仅在输入签名、V2 清单 SHA、连续 PTS、尾部 PCM 静音和语音终点全部通过时做本地末帧规范化；保留供应商原片和来源记录，不产生新上传或付费提交。4/4 切点自动批准，Whisper 仅诊断，不设置二次人工切点门。

无数字人 V1.3 已真实到达 `preview_ready`。批量补画面、逐句草案编辑、声音设置与失败槽幂等恢复均已接入一键路线。

## 固定生产合同

- InfiniteTalk 精确帧 API 模板：`config/runninghub/workflow-2094449979141218305.api.json`。
- 默认机型为 Standard 24GB。Plus 不是全局默认；只有单次父任务已在唯一前置确认中明确授权、同一角色前两次均为明确终态 OOM 且预算再次通过时，才允许第 3 次使用一次 Plus。
- 未知 RunningHub 响应进入 `ambiguous` 并停止，禁止重复提交。
- `review_ready` 不是批准或发布；成片仍需人工观看。

## 当前阶段

产品品牌和发布仓库统一为 **Haike Video / 海客视频工厂**。权威私有仓库为 `https://github.com/hrbj18/Haike_video`，发布分支为 `main`；本机正式干净副本位于 `D:\codex_work\Haike_video`。旧开发目录仅作为本机历史工作台保留，不再作为跨电脑部署入口。

GitHub 首页、README、代码标识、公开配置、测试和部署说明均使用 Haike Video 命名。AGPL 许可证要求的来源与版权信息只保留在 `LICENSE`、`UPSTREAM.md` 和 `THIRD_PARTY_NOTICES.md`，不作为产品品牌展示。密钥、私有音色、人物图、音乐、模型、缓存、项目媒体和付费产物均未进入 Git。

新电脑按 `docs/DEPLOYMENT_WINDOWS_ZH-CN.md` 安装 Python 3.12、Node.js 22、核心/ASR/Remotion/本地 TTS 依赖，再运行预检、上下文审计和测试。静态通过不代表云端账号或私有素材已验收；迁移本地配置后仍需单独授权短样本真实验证。

## 最近验证

- 品牌迁移专项：Python、JSON、YAML 和 PowerShell 静态解析通过；配置、本地 TTS 与一键流程专项 150 passed、2 skipped。
- 精简发布快照正式套件基线：1780 passed, 24 skipped, 1 subtest passed；品牌迁移后的完整套件继续保持同一功能边界，资源受限的 FFmpeg 编码项另行单独复核。
- Python Launcher 不可用时，安装器可回退 PATH 中的 64 位 Python 3.12；全新 `.venv` 无联网烟测通过。PowerShell 两个安装脚本语法通过。
- Remotion package/lock 一致，`npm ci --dry-run` 解析 260 个包通过。
- 发布候选 1726 个文件、约 27.7 MiB；没有大于 1 MiB 的文件、真实环境文件、高置信密钥、本机路径、私有媒体或付费产物。
- 完成态幂等恢复没有新增 worker、operation 或费用。

## 下一步

1. 在另一台 Windows 电脑从 `https://github.com/hrbj18/Haike_video` 克隆，并按 `docs/DEPLOYMENT_WINDOWS_ZH-CN.md` 完成干净安装与无付费回归。
2. 私下迁移音色、人物图和本地环境配置；不要经 GitHub 同步。
3. 真实 Pexels/RunningHub 验证仍需单独授权短样本与预算；未知供应商状态不得重提。
