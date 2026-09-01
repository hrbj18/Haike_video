# Haike Video Git 分支与版本管理

更新时间：2026-09-01

## 分支职责

- `main`：唯一稳定发布分支，只接受通过发布门的合并。
- `dev`：日常集成分支，功能分支先在这里完成组合回归。
- `codex/<任务名>`：一个具体开发任务一个分支；需要隔离时使用独立 worktree。
- `upstream/main`：上游参考，不是 Haike Video 的发布分支，禁止向上游推送私有开发结果。

## 日常开发

```powershell
git fetch origin
git switch dev
git pull --ff-only origin dev
git switch -c codex/<任务名>
```

开发结束后只暂存本任务文件：

```powershell
git status --short
git add <明确文件1> <明确文件2>
git diff --cached --name-only
git commit -m "feat: <简洁说明>"
git push -u origin codex/<任务名>
```

禁止使用 `git add .`。脏工作树中不允许用 reset/checkout 覆盖不明变更。

## 合并与发布

1. 功能分支通过聚焦测试和交接审计后，提交 PR 到 `dev`。
2. `dev` 完成组合回归；付费或外部验收必须单独授权。
3. 准备稳定版时更新 `VERSION`、`CHANGELOG.md` 和 `docs/handoff/RELEASE_STATUS.md`。
4. 从 `dev` 提交 PR 到 `main`，核对明确的发布文件清单、密钥扫描和部署文档。
5. 合并后创建与 `VERSION` 一致的标签，例如 `v0.1.0`；标签只指向 `main` 的已验收提交。
6. 将 `dev` 快进到新的 `main` 后再开始下一轮。

## 版本规则

使用 `MAJOR.MINOR.PATCH`：

- `MAJOR`：不兼容的数据、工作流或部署合同变化。
- `MINOR`：向后兼容的新能力或重要工作流扩展。
- `PATCH`：向后兼容的缺陷、稳定性、文档或安装修复。

未合入 `main` 的功能统一放在 `CHANGELOG.md` 的“未发布”，不得写进稳定版能力清单。

## 发布前门禁

- `python scripts/audit_context_handoff.py`
- 与改动对应的自动测试；稳定版再运行公开发布套件。
- `git diff --check`
- 明确暂存文件清单，不含 `.env*` 实值、`.backlot/`、`projects/`、媒体、模型、私有音色、人物图或付费产物。
- `README.md`、`START_HERE.md`、`VERSION`、`CHANGELOG.md` 和发布状态相互一致。

详细跨电脑安装见 [Windows 部署指南](DEPLOYMENT_WINDOWS_ZH-CN.md)。
