# GitHub 与跨电脑部署

更新时间：2026-09-01

## 权威入口

- 私有仓库：`https://github.com/hrbj18/haike`
- Windows 完整步骤：`docs/DEPLOYMENT_WINDOWS_ZH-CN.md`
- 环境变量模板：`.env.example`
- 一键安装：`scripts/setup.ps1`
- 启动与预检：`启动工作台.bat`、`scripts/preflight.ps1`

## 新电脑顺序

1. 安装 Git、64 位 Python 3.12、Node.js 22+。
2. 克隆仓库，在根目录运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1`。
3. 只在 `.env.local` 或 `.env.secrets.local` 写密钥；不要把真实值写回模板。
4. 私下导入克隆音色、人物参考图和音乐。模型由安装器/官方加载器下载，不从 Git 获取。
5. 配置 Pexels；有数字人路线再配置 RunningHub key、已发布 workflow ID、两位角色图与音色。
6. 运行 `scripts/preflight.ps1`、上下文审计，以及 `tests/backlot tests/contracts tests/lib tests/tools tests/unit` 自动套件；pytest 必须带 `--import-mode=importlib --basetemp=.p`，全部通过后才进行短样本真实验收。不要把旧 `tests/qa` 脚本当作默认自动套件。

## 发布边界

Git 只保存源码、测试、公开配置模板、精确帧 RunningHub API 模板和交接文档。禁止提交 `.env*` 实值、`.backlot/`、`projects/`、模型、私有音色、人物图、音乐、渲染、付费任务产物、审计证据、pytest 临时目录或个人黄金样稿。

RunningHub Plus 不是部署默认值；只有单次父任务前置确认明确授权、同一角色前两次均为终态明确 OOM 且预算仍充足时，才允许第三次一次性 Plus。未知状态停止，不得复制提交。

## 验收边界

本机已验证无数字人与有数字人审核预览路线；有数字人 4 句双主持真实任务达到 `review_ready`，切点由 V2 精确帧清单自动批准，Whisper 仅诊断。新电脑的安装/静态测试通过不等于云端账号、私有素材或付费工作流已经验收，必须按本机配置重新做短样本验证。
