# Haike Video 轻量交接入口

更新时间：2026-08-21

## 读取规则

1. 每轮只强制读取仓库根目录 `AGENT_GUIDE.md`。
2. 涉及项目开发、诊断、状态或规划时，再读本文件和 `CURRENT_STATUS.md`。
3. 只按任务追加读取一个专题文件：产品边界读 `PRODUCT_RULES.md`；历史决策读 `DECISIONS.md`；定位代码读 `CODE_MAP.md`；每日快报读 `DAILY_TECH_BRIEF.md`；GitHub 与跨电脑安装读 `DEPLOYMENT.md`。
4. 不默认扫描 `projects/`、`.backlot/daily-runs/`、日志、媒体文件、旧任务指导文档或整个 Git 差异。

## 当前任务入口

当前优先级是完成私有 GitHub 首次发布和跨电脑部署交接；先读 `CURRENT_STATUS.md` 与 `DEPLOYMENT.md`。首次发布后再恢复视频生产。

## 更新规则

只在功能实现、运行状态、关键决策、阻塞或下一步发生实质变化时更新；普通问答不更新。更新时覆盖过时结论，不追加聊天流水账。完成改动后运行：

`python scripts/audit_context_handoff.py`

超出 `context-policy.json` 上限时必须先压缩，再结束任务。
