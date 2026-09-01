# Haike Video 同事接手入口

更新时间：2026-09-01

如果你刚拿到仓库，只读这一页即可找到正确入口。不要从聊天记录、旧任务文档或本机历史目录猜项目状态。

## 先确认你要做什么

| 目标 | 第一份文档 | 然后读取 |
| --- | --- | --- |
| 新电脑安装 | [Windows 部署指南](docs/DEPLOYMENT_WINDOWS_ZH-CN.md) | [发布状态](docs/handoff/RELEASE_STATUS.md) |
| 接手开发 | [Git 分支与版本规则](docs/GIT_WORKFLOW_ZH-CN.md) | [当前状态](docs/handoff/CURRENT_STATUS.md) |
| Codex/智能体继续开发 | [AGENT_GUIDE.md](AGENT_GUIDE.md) | `docs/handoff/README.md` 路由的至多一个专题 |
| 了解稳定版能力 | [发布状态](docs/handoff/RELEASE_STATUS.md) | [CHANGELOG.md](CHANGELOG.md) |
| 配置密钥或供应商 | [Windows 部署指南](docs/DEPLOYMENT_WINDOWS_ZH-CN.md) | `.env.example` |

## 五分钟接手检查

```powershell
git remote -v
git branch --show-current
git status --short
Get-Content VERSION
Get-Content docs\handoff\RELEASE_STATUS.md
```

- `origin` 必须指向 `https://github.com/hrbj18/Haike_video.git`。
- 普通开发从 `dev` 创建 `codex/<任务名>`，不要直接在 `main` 开发。
- `main` 只保存已验收稳定版；`VERSION`、`CHANGELOG.md` 与发布状态必须同步更新。
- 工作区不干净时先确认变更所有者，不要覆盖、重置或批量暂存别人的文件。
- 禁止提交密钥、Cookie、`.env.local`、`.env.secrets.local`、私有音色、人物图、音乐、项目媒体、模型缓存和付费产物。

## 安装与启动

```powershell
git clone https://github.com/hrbj18/Haike_video.git
Set-Location Haike_video
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_backlot.ps1
```

真实 Pexels、RunningHub 或其他付费/外部验收不属于安装步骤，必须另行获得授权和预算。

## 开发完成的最低门槛

1. 只暂存本任务允许的明确文件，禁止 `git add .`。
2. 运行相关测试、`python scripts/audit_context_handoff.py` 和 `git diff --check`。
3. 功能分支先合入 `dev`；稳定验收后再由 `dev` 合入 `main`。
4. 发布时更新 `VERSION`、`CHANGELOG.md`、`docs/handoff/RELEASE_STATUS.md` 并创建同名 `vX.Y.Z` 标签。

遇到文档冲突时，GitHub 已发布范围以 `main`、`VERSION` 和 `RELEASE_STATUS.md` 为准；本机正在开发但尚未发布的内容以当前任务的 `CURRENT_STATUS.md` 和可执行测试为准。
