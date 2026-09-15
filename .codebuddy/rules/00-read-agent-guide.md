---
alwaysApply: true
---

# WorkBuddy 侧的第一动作

本项目自带一套 agent 契约，WorkBuddy 也必须遵守。

**动手前先完整读 `AGENT_GUIDE.md`**（OpenMontage Agent Fast Router，短文件）——
它规定了上下文路由（该读哪些 `docs/handoff/*`）与请求路由（该不该改代码）。
它明确要求**不要默认扫描整个仓库**，只读回答当前问题所需的路径。

## 工具名对照（本环境 vs Codex / CLI）

| AGENT_GUIDE.md 里的说法 | 本环境实际使用 |
|---|---|
| `rg` / `rg --files` | Grep / Glob 工具 |
| `apply_patch` | Edit / Write 工具 |
| `python …` / `pytest …` | 写全路径 `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe` |
| bash 命令 | Bash 工具，每条命令先 `export PATH="/usr/bin:/bin:$PATH"` |

其余约定一律照 `AGENT_GUIDE.md` 执行，不得放宽：
付费 provider 必须显式声明、生产队列是唯一执行权威、正式视频不自动发布、
最多两轮独立 review、重试时保留已完成的付费产物。
