# OpenMontage Windows 完整部署指南

本指南面向从 `https://github.com/hrbj18/haike` 获取代码的新电脑。仓库只包含可复现的源码、公开配置模板和测试；密钥、私有音色、人物参考图、模型、缓存、项目媒体与付费产物不会进入 Git。

## 1. 已验证环境

- Windows 10/11 x64，PowerShell 5.1 或更高版本
- Git for Windows
- 64 位 Python 3.12（当前锁定版本）
- Node.js 22 或更高版本，附带 npm
- 建议至少预留 30 GB 磁盘空间给 Python、Node、Qwen3-TTS、Whisper 与媒体缓存
- NVIDIA GPU 可显著加快本地 TTS/ASR，但不是启动工作台的硬条件

FFmpeg/ffprobe 由固定版本的 `static-ffmpeg` Python 包提供；如果系统 PATH 已有完整的一对，也可直接使用。

### 1.1 FFmpeg 必须支持 `-filter_complex_script`（数字人合成）

数字人母版的滤镜图很长，为避免撞上 Windows 命令行长度上限，合成时是**写进临时文件**再交给 FFmpeg 的：

```
ffmpeg -y -i … -filter_complex_script <临时文件> -map [vout] -map [aout] …
```

**问题**：并非所有构建都实现了这个选项。本机实测（2026-09-12）：

| 二进制 | `-filter_complex_script` |
|---|---|
| `static-ffmpeg`（项目自带，`.venv/Lib/site-packages/static_ffmpeg/bin/win32/`） | **支持** |
| master 构建 `N-126495-g3a165c77dc-20260910`（当时在 PATH 上） | **不支持**，报 `Unrecognized option 'filter_complex_script'.` |

一旦选到不支持的构建，合成会失败，且**发生在付费的数字人生成之后**。

**现在的行为**：`backlot/avatar_import.py::_graph_script_ffmpeg()` 不再按 PATH 顺序取第一个，
而是**按能力挑选** —— 逐个探测已知候选（`FFMPEG_BINARY`/PATH → VideoCompose 运行时 → `static-ffmpeg`），
取第一个真正认这个选项的。都不支持时抛出明确中文错误，指出需要改用项目自带的 `static-ffmpeg`。

**排查**：看到 `Unrecognized option 'filter_complex_script'` 或
`本机没有支持 -filter_complex_script 的 FFmpeg` 时，检查 PATH 上是不是混进了别的 FFmpeg：

```bash
./.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'.'); from lib.ffmpeg_locator import supports_option, resolve_ffmpeg_with_option as r; import shutil; print('PATH:', shutil.which('ffmpeg'), supports_option(shutil.which('ffmpeg'), '-filter_complex_script')); print('选中:', r('-filter_complex_script')[0])"
```

注意 FFmpeg 报错时**会把选项的前导短横线去掉**（打印 `'filter_complex_script'` 而不是 `'-filter_complex_script'`），
写解析脚本时按无短横线的形式匹配。

## 2. 克隆与一键安装

这是私有仓库。先用有访问权限的 GitHub 账号完成认证：Git for Windows 可在首次克隆时通过 Git Credential Manager 打开浏览器登录；已安装 GitHub CLI 时也可运行 `gh auth login --web`。不要把个人访问令牌写进命令、脚本或文档。

```powershell
git clone https://github.com/hrbj18/haike.git
Set-Location haike
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

默认安装流程会：

1. 创建项目 `.venv`，安装 `requirements.txt` 与 `requirements-dev.txt`；
2. 安装 `requirements-asr.txt`，提供 faster-whisper 诊断；
3. 用 `npm ci` 安装锁定的 Remotion 依赖；
4. 在 `.backlot\tts-runtime` 创建独立 Qwen3-TTS 环境；
5. 创建本地工作目录，并在不存在时从 `.env.example` 生成 `.env.local`。

仅在明确不需要对应能力时使用 `-SkipDev`、`-SkipAsr`、`-SkipNode` 或 `-SkipLocalTts`。跳过开发依赖后不能运行发布验收测试；`-SkipInstall` 只适合已有完整依赖的维护场景。

## 3. 本地配置与密钥

把真实值写入 `.env.local` 或 `.env.secrets.local`；两者都被 Git 忽略。不要修改 `.env.example` 填入真实值。

基础无数字人预览通常需要：

- `PEXELS_API_KEY`：下载实拍素材；
- 可用的文本模型配置：仅 AI 导演、生成/整理脚本时需要；
- OpenMontage 本地 TTS：安装后可使用 Serena、Vivian、Dylan 公开预设。

有数字人预览还需要：

- `RUNNINGHUB_API_KEY`；
- `RUNNINGHUB_WORKFLOW_ID` 与 `RUNNINGHUB_WORKFLOW_PROFILE`；
- 两位主持人的 4:5 参考图；
- 两个可用音色。雅雅/檬檬是私有克隆音色时，应单独迁移，不得提交仓库。

仓库包含生产 API 模板 `config/runninghub/workflow-2094449979141218305.api.json`。使用同一 RunningHub 账号可配置现有工作流 ID；更换账号时先在 RunningHub 导入模板并发布，再把新 ID 写入本地密钥文件或工作台配置。Plus 48GB 不是全局默认，只能在单次父任务前置确认中显式授权。

## 4. 迁移私有音色与人物图

旧电脑导出克隆音色：

```powershell
$voicePack = Join-Path $env:USERPROFILE 'OpenMontage-private\voices.zip'
.\.venv\Scripts\python.exe scripts\local_tts_profiles.py export --output $voicePack --profile-id <PROFILE_ID>
```

新电脑导入：

```powershell
$voicePack = Join-Path $env:USERPROFILE 'OpenMontage-private\voices.zip'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_local_tts.ps1 -ImportProfilePack $voicePack
```

人物参考图通过工作台“数字人角色”配置重新导入。音色包、人物图、背景音乐和已有项目应使用私有文件传输，不要经 GitHub 同步。

## 5. 启动与预检

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_backlot.ps1
```

也可以双击 `启动工作台.bat`。默认地址：

- 工作台：`http://127.0.0.1:4754/`
- 工作台健康检查：`http://127.0.0.1:4754/api/health`
- 本地 TTS：`http://127.0.0.1:17494/health`

需要真实校验 Pexels 与本地 TTS 时运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1 -Providers
```

## 6. 新电脑验收

```powershell
.\.venv\Scripts\python.exe scripts\audit_context_handoff.py
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib --basetemp=.p tests\backlot tests\contracts tests\lib tests\tools tests\unit
Set-Location remotion-composer
npm ci --no-audit --no-fund
Set-Location ..
```

`--import-mode=importlib` 用于避免测试目录遮蔽仓库根模块，短 `--basetemp=.p` 用于规避 Windows 深层临时路径限制；`.p` 已被 Git 忽略。

测试不会自动调用 RunningHub 或发布视频。真实付费验收必须新建短项目、设置预算并由用户在最前面的确认框授权；未知供应商状态不得重提。

## 7. Git 不同步的内容

`.backlot/`、`projects/`、`renders/`、`cache/`、`temp/`、`artifacts/`、`exports/`、私有角色图片、音乐、模型缓存、pytest 临时目录、审核证据和个人黄金样稿都只留在本机。换电脑前如需保留，应另行加密备份。

## 8. 更新代码

```powershell
git pull --ff-only origin main
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

安装器会保留已有 `.env.local`、私有音色和项目目录。更新后先运行预检和测试，再恢复付费生产。
