[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$MigrateVoicebox,
    [ValidateSet('none', 'base', 'custom', 'all')]
    [string]$ImportModels = 'none',
    [ValidateSet('hardlink', 'copy')]
    [string]$ModelImportMode = 'hardlink',
    [string]$ImportProfilePack = '',
    [string]$LegacyModelCache = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = Join-Path $repoRoot '.backlot\tts-runtime'
$venvRoot = Join-Path $runtimeRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'

function Test-Python312Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandPath,
        [string[]]$Prefix = @()
    )

    try {
        & $CommandPath @Prefix -c "import struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') * 8 == 64 else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonCommand = $null
    $pythonPrefix = @()
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher -and (Test-Python312Command -CommandPath $pyLauncher.Source -Prefix @('-3.12'))) {
        $pythonCommand = $pyLauncher.Source
        $pythonPrefix = @('-3.12')
    }
    if (-not $pythonCommand) {
        $systemPython = Get-Command python -ErrorAction SilentlyContinue
        if ($systemPython -and (Test-Python312Command -CommandPath $systemPython.Source)) {
            $pythonCommand = $systemPython.Source
        }
    }
    if (-not $pythonCommand) {
        throw '未找到兼容的 Python。请安装 64 位 Python 3.12 后重新运行本脚本。'
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    & $pythonCommand @pythonPrefix -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw '创建本地配音独立虚拟环境失败。' }
}

$pythonVersionOutput = & $venvPython -c "import struct, sys; print('.'.join(map(str, sys.version_info[:2])) + ':' + str(struct.calcsize('P') * 8))"
$pythonVersionExitCode = $LASTEXITCODE
$pythonVersion = ([string]$pythonVersionOutput).Trim()
if ($pythonVersionExitCode -ne 0 -or $pythonVersion -ne '3.12:64') {
    throw "OpenMontage 本地配音运行时要求 64 位 Python 3.12。当前环境：$pythonVersion"
}

if (-not $SkipInstall) {
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw '升级本地配音 pip 失败。' }
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot 'requirements-tts.txt')
    if ($LASTEXITCODE -ne 0) { throw '安装 OpenMontage 本地配音依赖失败。' }
}

$dataDir = Join-Path $repoRoot '.backlot\tts'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

if ($MigrateVoicebox) {
    $sourceData = Join-Path $env:APPDATA 'sh.voicebox.app'
    $arguments = @(
        (Join-Path $repoRoot 'scripts\migrate_voicebox_tts.py'),
        '--source', $sourceData,
        '--destination', $dataDir,
        '--models', $ImportModels,
        '--model-mode', $ModelImportMode
    )
    if ($ImportModels -ne 'none') {
        if (-not $LegacyModelCache) {
            throw '使用 -ImportModels 时必须同时提供 -LegacyModelCache。'
        }
        $arguments += @('--model-cache', $LegacyModelCache)
    }
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw '迁移现有 Voicebox 音色失败。原数据未被修改。' }
}

if ($ImportProfilePack) {
    & $venvPython (Join-Path $repoRoot 'scripts\local_tts_profiles.py') --data-dir $dataDir import --package $ImportProfilePack
    if ($LASTEXITCODE -ne 0) { throw '导入 OpenMontage 私有音色包失败。' }
}

Write-Host "[OK] OpenMontage 本地配音运行时已就绪：$venvPython" -ForegroundColor Green
Write-Host '模型会在第一次使用对应音色时自动下载到 .backlot\tts\models。' -ForegroundColor Cyan
