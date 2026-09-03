# GitHub 与跨电脑部署

更新时间：2026-09-01

## 权威入口

- 私有仓库：`https://github.com/hrbj18/Haike_video`
- 发布状态：`main` 是稳定版、`dev` 是集成版；`v0.1.1` 与两分支当前均位于 `50352aaa92e083605bf1a75062adbec71199ff6c`。该版本新增可单独发送的核心密钥交接文档和隐藏输入的一键配置脚本。
- 同事入口：GitHub 根目录 `START_HERE.md`；版本由 `VERSION`、`CHANGELOG.md` 和 `docs/handoff/RELEASE_STATUS.md` 共同定义。
- Windows 完整步骤：`docs/DEPLOYMENT_WINDOWS_ZH-CN.md`
- 环境变量模板：`.env.example`
- 一键安装：`scripts/setup.ps1`
- 启动与预检：`启动工作台.bat`、`scripts/preflight.ps1`

## 新电脑顺序

1. 安装 Git、64 位 Python 3.12、Node.js 22+。
2. 克隆 `main` 稳定版，在根目录先读 `START_HERE.md`，再运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`。
3. 只在 `.env.local` 或 `.env.secrets.local` 写密钥；不要把真实值写回模板。
4. 私下导入克隆音色、人物参考图和音乐。模型由安装器/官方加载器下载，不从 Git 获取。
5. 配置 Pexels；有数字人路线再配置 RunningHub key、已发布 workflow ID、两位角色图与音色。
6. 运行 `scripts/preflight.ps1`、上下文审计，以及 `tests/backlot tests/contracts tests/lib tests/tools tests/unit` 自动套件；pytest 必须带 `--import-mode=importlib --basetemp=.p`，全部通过后才进行短样本真实验收。不要把旧 `tests/qa` 脚本当作默认自动套件。

## Linux 轻量版服务器现状

- 2026-09-01 已将本机忽略的核心密钥配置安全同步到服务器仓库，并按变量名合并进 Docker 持久化 `.env.local`；未覆盖既有 Linux 路径、本地 Qwen3-TTS 或 faster-whisper 配置。
- `haike-video` 容器重启后，只读接口确认 GPT 中转站、豆包文本和 RunningHub 均已配置，RunningHub 配置问题数为 0；本地音频中心仍为 `available`，可见 3 个音色并保留默认音色。
- HyperFrames 已在 Linux 容器内完成 CPU 基线初始化：CLI `0.8.22`、Chrome Headless Shell、`unzip`、FFmpeg 和 FFprobe 均通过关键预检；容器 `/dev/shm` 已提升到 `512 MB`，npm 与浏览器缓存均挂载到 `/opt/Haike_video/cache` 持久化。项目运行时返回 `runtime_available=true`，本轮未执行视频渲染。
- 2026-09-02 已在广州 Linux 轻量服务器通过真实浏览器完成无数字人全流程：新建“键盘排序的秘密”项目、Luna 自动写稿、人工审核闸门确认、本地 Qwen3-TTS、Pexels/HyperFrames 配画面、字幕与 FFmpeg 全片合成，最终到达 `preview_ready`。父任务 `RPP-8b51022434c9` 为 16/16、失败 0，预览 `renders/previews/full-preview-v001.mp4`（H.264/AAC、1080×1920、30 FPS、127.5 秒）；没有自动批准或发布。
- 服务器本地 TTS 的生产默认模型已改为可配置的官方 `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`，通过 `HAIKE_VIDEO_TTS_CUSTOM_MODEL` 选择，并使用 `HF_ENDPOINT=https://hf-mirror.com` 完成模型缓存。1.7B 官方模型缓存仍保留，可通过配置切回；未把云 TTS 写死为唯一实现。0.6B 在该 CPU 服务器上的短句实测冷启动约 112 秒、热调用约 38 秒，完整 16 句流程约 90 分钟，因此“可运行”不等于“高并发实时”。
- 当前生产容器为 `haike-video`；切换前容器保留为停止状态的 `haike-video-pre-hyperframes-20260901-2302`，仅用于短期回滚，确认稳定后再单独决定是否清理。
- 本轮没有调用 Pexels、文本模型、RunningHub 或其他付费服务。一次性 SSH 公钥已从服务器撤销，本机临时私钥目录已删除。
- 服务器密钥文件权限为 `600` 且被 Git 忽略；后续更新不得把 `.env.local` 或 `.env.secrets.local` 纳入提交。

## 发布边界

Git 只保存源码、测试、公开配置模板、精确帧 RunningHub API 模板和交接文档。禁止提交 `.env*` 实值、`.backlot/`、`projects/`、模型、私有音色、人物图、音乐、渲染、付费任务产物、审计证据、pytest 临时目录或个人黄金样稿。

RunningHub Plus 不是部署默认值；只有单次父任务前置确认明确授权、同一角色前两次均为终态明确 OOM 且预算仍充足时，才允许第三次一次性 Plus。未知状态停止，不得复制提交。

## 验收边界

本机已验证无数字人与有数字人审核预览路线；有数字人 4 句双主持真实任务达到 `review_ready`，切点由 V2 精确帧清单自动批准，Whisper 仅诊断。新电脑的安装/静态测试通过不等于云端账号、私有素材或付费工作流已经验收，必须按本机配置重新做短样本验证。
