# GitHub 稳定版状态

更新时间：2026-09-01

## 当前发布

- 仓库：`https://github.com/hrbj18/Haike_video`
- 稳定分支：`main`
- 集成分支：`dev`
- 版本：`v0.1.0`
- 产品源码基线：`b29f0f4472a252cdb5bb71bfa197b0d7160f6107`
- 状态：已完成精简源码、品牌、部署和交接治理验收；正式视频仍只到 `review_ready`，不会自动发布。

`VERSION`、`CHANGELOG.md` 与本文件共同定义 GitHub 稳定版。远端提交 SHA 以 `git rev-parse origin/main` 的实时结果为准；上面的产品源码基线用于区分后续纯文档/治理提交。

## 已发布能力边界

- 标题或脚本进入可编辑草案，再生成无数字人审核预览。
- 双主持数字人路线具备精确帧音频、前置付费确认、自动切割、安全恢复和审核预览。
- Remotion、HyperFrames、Pexels、本地 TTS/ASR 的可复现源码与配置模板已纳入仓库。
- 密钥、私有音色、人物参考图、音乐、项目媒体、模型缓存和付费产物不在 GitHub 中。

本机历史工作区中存在的后续素材理解、自动选材等未发布开发，不属于 `v0.1.0`，不能仅凭本机演示宣称 GitHub 稳定版已经具备。

## 接手与发布

- 新同事从仓库根目录 [START_HERE.md](../../START_HERE.md) 开始。
- 安装按 [Windows 部署指南](../DEPLOYMENT_WINDOWS_ZH-CN.md)。
- 开发与发布按 [Git 分支与版本管理](../GIT_WORKFLOW_ZH-CN.md)。
- 智能体先读 `AGENT_GUIDE.md`，再按 `docs/handoff/README.md` 路由。

任何新稳定版都必须同步更新本文件、`VERSION` 和 `CHANGELOG.md`，并通过上下文审计后再合入 `main`。
