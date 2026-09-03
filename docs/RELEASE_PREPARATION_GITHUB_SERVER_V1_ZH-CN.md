# GitHub 与服务器发布准备 V1

更新时间：2026-09-03

## 本轮目的

将本机已验收的三类改动整理为一个可追溯的 GitHub `dev` 候选发布：

1. 本地素材导入、批量视觉理解与人工采用编排；
2. 豆包云端配音、单音色语速和自定义音色 ID；
3. 暖纸张前端、项目库、固定导航和片段工作台升级。

本文件不授权提交、推送、服务器重启或付费生成。正式发布必须另行确认。

## 发布边界

只允许按路径白名单暂存，不可使用 `git add .`。

| 类别 | 可纳入 |
| --- | --- |
| 运行时 | `backlot/`、`lib/`、`tools/`、`schemas/`、`scripts/` |
| 前端 | `backlot/ui/`、`remotion-composer/src/`、构建配置与受控 demo props、对应 `package*.json`、`styles/` |
| 公开配置 | `config/` 中未被 `.gitignore` 排除的模板、`requirements*.txt`、`.env.example`、`config.yaml` |
| 测试与文档 | `tests/`、当前交接/部署/运行说明、`README*`、许可证/贡献说明、`.github/` |

明确排除：`.env*` 实值、`.backlot/`、`projects/`、模型、人物图、克隆音色、音乐、视频、渲染、付费任务记录、调试日志、原型工程、本机 Agent/IDE 目录、本地 Skill，以及历史开发指导、设计 QA、单次生产计划和验收证据。

## 整理步骤

1. 运行 `python scripts/audit_github_release.py`；审计器只读取 Git 候选文件，不会修改 Git 状态。
2. 对 `review` 列表逐项作出“纳入”或“排除”决定；任何未知路径都不得自动暂存。
3. 对计划纳入路径做高置信密钥扫描。发现密钥匹配即停止；不要在终端输出密钥原文。
4. 运行无付费回归：配音中心、素材编排、工作台 UI、数字人精确帧合同和发布部署测试。
5. 由人工以命名路径 `git add <path...>` 暂存；先执行 `git diff --cached --check` 与 `git diff --cached --name-only`，确认白名单后才允许提交到 `dev`。
6. 推送 `dev` 并记录精确提交 SHA。服务器仅拉取该 SHA，保留现有容器回滚点；先健康检查、再验证豆包音色可见性。任何 RunningHub 短样本仍需费用/预算确认。

## 验收标准

- 审计器无高置信密钥命中，所有 `review` 路径均有明确处置；
- 暂存区不含 `.env`、`.backlot`、`projects`、媒体、模型或私有资产；
- 无付费回归通过，且 `git diff --cached --check` 无输出；
- GitHub `dev` 提交可从干净目录安装并启动；
- 服务器升级后 `/api/health` 为 `ok`，配音中心出现豆包云端音色；
- 有数字人服务器短样本仅在当前预算确认后执行，并停在人工审核状态，不自动发布。
