[CmdletBinding()]
param(
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 45,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.backlot\tts-runtime\.venv\Scripts\python.exe'
$dataDir = Join-Path $repoRoot '.backlot\tts'
$logDir = Join-Path $dataDir 'logs'
$baseUrl = 'http://127.0.0.1:17494'

function Read-LocalEnvValue([string]$Name, [string]$DefaultValue = '') {
    foreach ($path in @(
        (Join-Path $repoRoot '.env.secrets.local'),
        (Join-Path $repoRoot '.env.local'),
        (Join-Path $repoRoot '.env')
    )) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($line in (Get-Content -LiteralPath $path)) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") {
                $value = $matches[1].Trim().Trim('"').Trim("'")
                if ($value) { return $value }
            }
        }
    }
    return $DefaultValue
}

$baseUrl = (Read-LocalEnvValue 'HAIKE_VIDEO_TTS_BASE_URL' $baseUrl).TrimEnd('/')
$baseUri = [Uri]$baseUrl
if ($baseUri.Scheme -ne 'http' -or -not $baseUri.IsLoopback) {
    throw "HAIKE_VIDEO_TTS_BASE_URL 必须是本机 HTTP 地址：$baseUrl"
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Haike Video 本地配音尚未安装。请先运行 scripts\setup_local_tts.ps1。'
}

if ($Restart) {
    $listeners = @(Get-NetTCPConnection -LocalPort $baseUri.Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not $process -or $process.CommandLine -notmatch 'tools\.audio\.haike_video_tts_server') {
            throw "端口 $($baseUri.Port) 被非 Haike Video 本地配音程序占用，未执行停止。"
        }
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction Stop
    }
    if ($listeners.Count) { Start-Sleep -Milliseconds 700 }
}

try {
    $health = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get -TimeoutSec 5
    if ($health.status -eq 'healthy' -and $health.service -eq 'haike_video-local-tts') {
        Write-Host '[OK] Haike Video 本地配音服务已经运行'
        exit 0
    }
} catch {
    # Continue with a verified local start below.
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$env:HAIKE_VIDEO_TTS_DATA_DIR = Read-LocalEnvValue 'HAIKE_VIDEO_TTS_DATA_DIR' $dataDir
$configuredCache = Read-LocalEnvValue 'HAIKE_VIDEO_TTS_MODEL_CACHE' ''
if ($configuredCache) {
    $env:HAIKE_VIDEO_TTS_MODEL_CACHE = $configuredCache
} else {
    $env:HAIKE_VIDEO_TTS_MODEL_CACHE = Join-Path $env:HAIKE_VIDEO_TTS_DATA_DIR 'models'
}
$env:HF_HUB_CACHE = $env:HAIKE_VIDEO_TTS_MODEL_CACHE
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$configuredDevice = Read-LocalEnvValue 'HAIKE_VIDEO_TTS_DEVICE' 'auto'
$env:HAIKE_VIDEO_TTS_DEVICE = $configuredDevice

$serverArgs = @(
    '-m', 'tools.audio.haike_video_tts_server',
    '--host', '127.0.0.1',
    '--port', [string]$baseUri.Port,
    '--data-dir', ('"' + $env:HAIKE_VIDEO_TTS_DATA_DIR + '"')
)
Start-Process -FilePath $python -ArgumentList $serverArgs -WorkingDirectory $repoRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir 'server.out.log') `
    -RedirectStandardError (Join-Path $logDir 'server.err.log') | Out-Null

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "$baseUrl/health" -Method Get -TimeoutSec 5
        if ($health.service -eq 'haike_video-local-tts') {
            if ($health.status -ne 'healthy') {
                $missing = ($health.missing_dependencies -join ', ')
                throw "本地配音服务已启动，但依赖不完整：$missing"
            }
            Write-Host '[OK] Haike Video 本地配音服务启动成功'
            exit 0
        }
    } catch {
        $lastError = $_.Exception.Message
    }
} while ((Get-Date) -lt $deadline)

$errorLog = Join-Path $logDir 'server.err.log'
$detail = if (Test-Path -LiteralPath $errorLog) { (Get-Content -LiteralPath $errorLog -Tail 12) -join ' | ' } else { $lastError }
throw "Haike Video 本地配音未能在 $TimeoutSeconds 秒内启动：$detail；日志：$errorLog"
