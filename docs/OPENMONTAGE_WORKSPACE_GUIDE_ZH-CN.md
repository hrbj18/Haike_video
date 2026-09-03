# OpenMontage 统一工作区指南

克隆后的仓库根目录是唯一项目根目录。旧外部工作区不再作为新项目的输入源，也不再被任何新脚本引用；本文中的所有相对路径都从仓库根目录解析。

## 三种文件的边界

| 类型 | 位置 | Git 策略 |
|---|---|---|
| 工程代码 | `lib/`、`tools/`、`backlot/`、`remotion-composer/` | 提交 Git |
| 可复用项目源文件 | `content/episodes/`、`content/templates/`、`content/library/` | 文案、配置、清单提交；大媒体按需使用 Git LFS |
| 运行时产物 | `projects/`、`renders/`、`cache/`、`temp/`、项目 `media/` 和 `qa/` | 默认忽略，可重新生成 |

## 新建项目

```powershell
python scripts/new_project.py --id 004-ai-news --title "第四期科技新闻"
python -m lib.project_manifest content/episodes/004-ai-news/project.yaml
.
scripts\preflight.ps1 -Project 004-ai-news
```

项目源文件目录约定：

```text
content/episodes/<project-id>/
├─ project.yaml       # 项目契约
├─ docs/              # 脚本、发布文案、制作说明
├─ script/            # 可直接用于生成的纯净台词
├─ timeline/          # 音频与字幕的统一顺序/时间轴
├─ composition/       # Remotion / HyperFrames 源码
├─ media/             # 本机素材，默认不进普通 Git
└─ qa/                # 审查结果和截图，默认不进普通 Git
```

## 运行方式

```powershell
.\scripts\setup.ps1
.\scripts\start_voicebox.ps1
.\scripts\preflight.ps1 -Project 003-tech-chat
.\scripts\preflight.ps1 -Project 003-tech-chat -Providers
python tools/build_third_episode_doc.py --project 003-tech-chat
```

所有工具通过 `lib/workspace_paths.py` 解析路径，禁止在业务脚本中写死机器路径。

## 本地供应商

- Pexels：密钥放在 `.env.secrets.local` 的 `PEXELS_API_KEY`，预检会调用一次搜索接口确认鉴权和网络可用。
- 本地配音：由仓库内 `scripts/setup_local_tts.ps1` 安装独立 Qwen3-TTS 运行时，`scripts/start_local_tts.ps1` 负责后台启动；模型、克隆参考音频和历史位于被 Git 忽略的 `.backlot/tts`。
- 默认可使用 Qwen 中文预设；现有 `雅雅`、`檬檬` 可通过 `scripts/migrate_voicebox_tts.py` 一次性迁入，之后按 profile ID 区分角色，避免多角色项目串音。

## 凭证和素材

- API Key 只放 `.env.secrets.local` 或 `.env.local`，不提交 Git。
- Pexels 下载素材要保留来源 ID 和原始 URL。
- 数字人原视频、音频和成片不放进普通 Git；需要跨机器同步时使用 Git LFS 或外部素材仓库。
- 新项目只依赖 `content/episodes/<project-id>/project.yaml`，不依赖任何旧外部目录。
