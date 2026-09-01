# Haike Video｜海客视频工厂

Haike Video 是一个本地优先的自动化视频生产工作台。用户只需提供标题或脚本，即可经过可编辑脚本草案、本地配音、画面规划、数字人、字幕、背景音乐与确定性合成，得到可人工观看的审核预览。

系统终点为 `review_ready`，不会自动批准或发布正式视频。

## 核心工作流

- **无数字人预览：** 脚本 → 逐句配音 → 画面规划 → 素材下载 → 字幕 → 全片预览。
- **有数字人预览：** 脚本 → 精确帧配音 → 付费前置确认 → 双主持数字人 → 自动切割 → 主体画面 → 字幕 → 全片预览。
- **安全恢复：** 已完成的配音与付费数字人结果会被保留，从安全点继续不会重复提交成功任务。
- **双渲染路线：** 结构化 React 场景使用 Remotion，HTML/GSAP 动态设计使用 HyperFrames。

## Windows 环境要求

- Windows 10/11 x64
- Git for Windows
- 64 位 Python 3.12
- Node.js 22 或更高版本
- 首次下载依赖、模型以及调用云端服务时需要联网
- 基础环境建议预留 30 GB；持续生产视频建议预留 50–100 GB

## 一键安装

```powershell
git clone https://github.com/hrbj18/Haike_video.git
Set-Location Haike_video
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1
```

安装器会创建 Python 环境、安装 Remotion 锁定依赖、准备本地 ASR/TTS，并在需要时从 `.env.example` 创建 `.env.local`。

HyperFrames 首次使用时通过 npm 获取，部署后运行：

```powershell
npx hyperframes doctor
```

## 本地配置

真实密钥只能写入 `.env.local` 或 `.env.secrets.local`。

- 无数字人路线通常需要 Pexels 和选定的文本模型。
- 有数字人路线还需要 RunningHub 密钥/工作流、主持人参考图和对应音色。
- 私有克隆音色、人物图、音乐、历史项目、模型缓存和付费产物不会通过 GitHub 分发。

## 启动

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\preflight.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_backlot.ps1
```

也可以双击 `启动工作台.bat`。默认工作台地址为 `http://127.0.0.1:4754/`。

## 新电脑验收

```powershell
.\.venv\Scripts\python.exe scripts\audit_context_handoff.py
.\.venv\Scripts\python.exe -m pytest -q --import-mode=importlib --basetemp=.p tests\backlot tests\contracts tests\lib tests\tools tests\unit
Set-Location remotion-composer
npm ci --no-audit --no-fund
Set-Location ..
npx hyperframes doctor
```

上述测试不会提交 RunningHub 付费任务，也不会发布视频。真实供应商验收必须使用短样本、明确预算，并在提交前由用户确认。

## 文档入口

- [Windows 完整部署指南](docs/DEPLOYMENT_WINDOWS_ZH-CN.md)
- [智能体任务路由](AGENT_GUIDE.md)
- [当前交接状态](docs/handoff/CURRENT_STATUS.md)
- [供应商配置](docs/PROVIDERS.md)
- [许可证](LICENSE)与[来源声明](UPSTREAM.md)

## 许可证

项目按 GNU AGPLv3 分发。许可证、修改与来源声明分别保存在 `LICENSE`、`UPSTREAM.md` 和 `THIRD_PARTY_NOTICES.md`。
