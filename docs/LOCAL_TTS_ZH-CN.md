# OpenMontage 内置本地配音

OpenMontage 已内置独立的 Qwen3-TTS 服务。工作台、每日生产和数字人驱动音频不需要安装或启动 Voicebox；历史 Python 类名 `VoiceboxTTS` 仅作为项目状态兼容层保留。

## 新机器安装

在项目根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_local_tts.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\start_local_tts.ps1
```

安装器把重型依赖放在 `.backlot\tts-runtime\.venv`。模型首次使用时下载到 `.backlot\tts\models`；这两个目录都被 Git 忽略。工作台一键启动器会尝试后台启动服务，失败时仍允许工作台打开并显示具体原因。

干净安装自带 Qwen Serena、Vivian、Dylan 三个中文预设。默认地址是 `http://127.0.0.1:17494`，服务只允许绑定 localhost。

## 迁移现有雅雅、檬檬

只需在旧数据仍存在时执行一次：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_local_tts.ps1 -MigrateVoicebox
```

迁移器只读 `%APPDATA%\sh.voicebox.app\voicebox.db`，保留 profile ID，并把参考 WAV 复制到 `.backlot\tts\profiles`。原数据库和原音频不会被修改。若要复用已经下载的模型，可显式增加 `-ImportModels base`、`custom` 或 `all`，并用 `-LegacyModelCache` 指定旧模型缓存；同盘默认使用硬链接，不重复占用模型空间。

## 跨机器保留私有角色音色

克隆音色可能包含个人或角色声音，不能随公开 Git 仓库提交。使用私有音色包单独迁移：

```powershell
# 旧机器导出，可重复指定 --profile-id
$voicePack = Join-Path $env:USERPROFILE 'OpenMontage-private\voices.zip'
python scripts\local_tts_profiles.py export --output $voicePack --profile-id <PROFILE_ID>

# 新机器完成依赖安装后导入
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\setup_local_tts.ps1 -ImportProfilePack $voicePack
```

音色包不包含模型权重；新机器仍由官方加载器下载模型。请把音色包按私有素材管理，不要提交到公共仓库。

## 运行机制

- Base 模型用于参考音频克隆；CustomVoice 模型用于无需样本的官方预设。
- 模型按需加载并常驻，同类型连续生成不重复冷启动。
- 默认一次只驻留一个 1.7B 模型，切换预设/克隆时释放旧模型，避免同时占用过多内存。
- 所有任务进入全局串行队列，避免多个片段同时抢显存或内存。
- 超长文本按句号、问号、感叹号等边界分段生成，再以短静音拼接。
- 服务重启会把残留的 queued/generating 任务标为失败并提示重试，不会永久假卡住。

## 常用配置

在 `.env.local` 或 `.env.secrets.local` 中配置，均为可选：

```dotenv
OPENMONTAGE_TTS_BASE_URL=http://127.0.0.1:17494
OPENMONTAGE_TTS_DEVICE=auto
OPENMONTAGE_TTS_PROFILE_NAME=Qwen Serena
OPENMONTAGE_TTS_PROFILE_ID=
OPENMONTAGE_TTS_DATA_DIR=
OPENMONTAGE_TTS_MODEL_CACHE=
OPENMONTAGE_TTS_MAX_LOADED_MODELS=1
OPENMONTAGE_TTS_MAX_CHARS=500
```

`auto` 会优先使用可用 CUDA，否则使用 CPU。当前机器的真实验证是 CPU；1.7B 模型第一次冷加载较慢，后续同模型任务会复用内存。

## 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:17494/health
Invoke-RestMethod http://127.0.0.1:17494/profiles
```

健康结果必须同时满足 `status=healthy` 和 `service=openmontage-local-tts`。日志位于 `.backlot\tts\logs`。
