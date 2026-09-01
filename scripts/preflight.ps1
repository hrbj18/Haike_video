param(
    [string]$Project = '',
    [switch]$Strict,
    [switch]$Providers
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repoRoot

$failures = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Test-CommandAvailable([string]$Name, [string]$Label) {
    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        Write-Host "[OK] $Label"
        return $true
    }
    $warnings.Add("$Label not found: $Name")
    Write-Host "[WARN] $Label not found: $Name"
    return $false
}

function Resolve-MediaRuntimePair {
    $pathFfmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    $pathFfprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
    if ($pathFfmpeg -and $pathFfprobe) {
        return @{
            Source = 'PATH'
            Ffmpeg = $pathFfmpeg.Source
            Ffprobe = $pathFfprobe.Source
        }
    }

    # Keep preflight read-only: inspect the pinned package files directly.
    # Do not import static_ffmpeg or call add_paths(), because that helper may
    # download binaries when the package cache is incomplete.
    $staticBin = Join-Path $repoRoot '.venv\Lib\site-packages\static_ffmpeg\bin\win32'
    $staticFfmpeg = Join-Path $staticBin 'ffmpeg.exe'
    $staticFfprobe = Join-Path $staticBin 'ffprobe.exe'
    if ((Test-Path -LiteralPath $staticFfmpeg) -and (Test-Path -LiteralPath $staticFfprobe)) {
        return @{
            Source = 'project .venv static-ffmpeg'
            Ffmpeg = $staticFfmpeg
            Ffprobe = $staticFfprobe
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'config\workspace.yaml'))) {
    $failures.Add('config/workspace.yaml is missing')
}
$envFiles = @(
    (Join-Path $repoRoot '.env.secrets.local'),
    (Join-Path $repoRoot '.env.local'),
    (Join-Path $repoRoot '.env')
)
$existingEnvFiles = @($envFiles | Where-Object { Test-Path -LiteralPath $_ })
if ($existingEnvFiles.Count -eq 0) {
    $warnings.Add('No local environment file found; provider checks may be unavailable')
}

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = $true
    $pythonCommand = $venvPython
    Write-Host '[OK] Python (.venv)'
} else {
$python = Test-CommandAvailable 'python' 'Python'
    $pythonCommand = 'python'
}

if ($python) {
    & $pythonCommand scripts/audit_context_handoff.py
    if ($LASTEXITCODE -ne 0) {
        $failures.Add('Lightweight context handoff audit failed')
    } else {
        Write-Host '[OK] Lightweight context handoff'
    }
}

$node = $false
if (Get-Command node -ErrorAction SilentlyContinue) {
    $node = $true
    Write-Host '[OK] Node.js'
} else {
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode) {
        $node = $true
        Write-Host '[OK] Node.js (bundled workspace runtime)'
    } else {
        $warnings.Add('Node.js not found: node')
        Write-Host '[WARN] Node.js not found: node'
    }
}
$mediaRuntime = Resolve-MediaRuntimePair
if ($mediaRuntime) {
    Write-Host "[OK] FFmpeg + ffprobe ($($mediaRuntime.Source))"
} else {
    $warnings.Add('A complete FFmpeg + ffprobe pair was not found on PATH or in the project .venv static-ffmpeg package')
    Write-Host '[WARN] FFmpeg + ffprobe pair not found'
}

if ($Project) {
    $manifest = Join-Path $repoRoot ("content\episodes\{0}\project.yaml" -f $Project)
    if (-not (Test-Path -LiteralPath $manifest)) {
        $failures.Add("Project manifest is missing: $manifest")
    } elseif ($python) {
        & $pythonCommand -m lib.project_manifest $manifest
        if ($LASTEXITCODE -ne 0) {
            $failures.Add("Project manifest validation failed: $Project")
        } else {
            & $pythonCommand scripts/qa_project.py --project $Project
            if ($LASTEXITCODE -ne 0) {
                $failures.Add("Project contract QA failed: $Project")
            }
        }
    }
}

$envText = if ($existingEnvFiles.Count -gt 0) {
    (($existingEnvFiles | ForEach-Object { Get-Content -LiteralPath $_ -Raw }) -join "`n")
} else { '' }
if ($envText -notmatch '(?m)^PEXELS_API_KEY=\s*[^\r\n]+') {
    $warnings.Add('PEXELS_API_KEY is not configured in a local environment file')
}

if ($envText -notmatch '(?m)^HAIKE_VIDEO_TTS_BASE_URL=\s*[^\r\n]+') {
    $warnings.Add('HAIKE_VIDEO_TTS_BASE_URL is not configured; localhost:17494 will be used')
}
$ttsPython = Join-Path $repoRoot '.backlot\tts-runtime\.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $ttsPython) {
    Write-Host '[OK] Haike Video local TTS runtime'
} else {
    $warnings.Add('Haike Video local TTS runtime is not installed; run scripts/setup_local_tts.ps1')
}

if ($Providers) {
    if ($envText -match '(?m)^PEXELS_API_KEY=\s*([^\r\n]+)') {
        $pexelsKey = $matches[1].Trim().Trim('"').Trim("'")
        try {
            $pexelsResponse = Invoke-RestMethod -Uri 'https://api.pexels.com/v1/search?query=technology&per_page=1' -Headers @{ Authorization = $pexelsKey } -Method Get -TimeoutSec 20
            if ($null -ne $pexelsResponse) {
                Write-Host '[OK] Pexels API'
            } else {
                $warnings.Add('Pexels API returned an empty response')
            }
        } catch {
            $warnings.Add("Pexels API check failed: $($_.Exception.Message)")
        }
    }

    $ttsBaseUrl = 'http://127.0.0.1:17494'
    if ($envText -match '(?m)^HAIKE_VIDEO_TTS_BASE_URL=\s*([^\r\n]+)') {
        $ttsBaseUrl = $matches[1].Trim().Trim('"').Trim("'").TrimEnd('/')
    }
    try {
        $ttsHealth = Invoke-RestMethod -Uri "$ttsBaseUrl/health" -Method Get -TimeoutSec 10
        if ($ttsHealth.status -eq 'healthy' -and $ttsHealth.service -eq 'haike_video-local-tts') {
            Write-Host '[OK] Haike Video local TTS health'
        } else {
            $warnings.Add("Haike Video local TTS is reachable but not healthy: $($ttsHealth.status)")
        }
    } catch {
        $warnings.Add("Haike Video local TTS health check failed: $($_.Exception.Message)")
    }
}

Write-Output ''
Write-Output 'Preflight summary'
Write-Output "  Repository: $repoRoot"
Write-Output "  Project:    $Project"
Write-Output "  Warnings:   $($warnings.Count)"
Write-Output "  Failures:   $($failures.Count)"

foreach ($warning in $warnings) { Write-Output "[WARN] $warning" }
foreach ($failure in $failures) { Write-Output "[FAIL] $failure" }

if ($failures.Count -gt 0 -or ($Strict -and $warnings.Count -gt 0)) {
    exit 1
}
exit 0
