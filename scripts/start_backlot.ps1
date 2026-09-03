[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 4754,
    [switch]$NoBrowser,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$workbenchUrl = "http://127.0.0.1:$Port/"

function Repair-ProcessPathEnvironment {
    # Some launch hosts expose both `Path` and `PATH` with different values.
    # Windows PowerShell's Start-Process copies them into a case-insensitive
    # dictionary and then fails before the child process is created.  Merge the
    # variants for this launcher and its children only; do not touch the user's
    # persistent machine or account environment.
    $environment = [Environment]::GetEnvironmentVariables('Process')
    $pathKeys = @($environment.Keys | Where-Object { [string]$_ -ieq 'Path' })
    if ($pathKeys.Count -lt 2) { return }

    $orderedKeys = @($pathKeys | Sort-Object {
        if ([string]$_ -ceq 'Path') { 0 }
        elseif ([string]$_ -ceq 'PATH') { 1 }
        else { 2 }
    })
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($key in $orderedKeys) {
        foreach ($part in ([string]$environment[$key] -split ';')) {
            $trimmed = $part.Trim()
            if ($trimmed -and $seen.Add($trimmed)) {
                [void]$parts.Add($trimmed)
            }
        }
    }
    $mergedPath = $parts -join ';'
    [Environment]::SetEnvironmentVariable('Path', $mergedPath, 'Process')
    [Environment]::SetEnvironmentVariable('PATH', $mergedPath, 'Process')
}

Repair-ProcessPathEnvironment

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "未找到项目虚拟环境：$python" -ForegroundColor Red
    Write-Host "请先按 README_zh-CN.md 完成一次依赖安装，再双击“启动工作台.bat”。" -ForegroundColor Yellow
    exit 1
}

function Get-ListeningProcessIds {
    param([int]$TargetPort)

    $listeners = @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue)
    $ownerIds = @($listeners | ForEach-Object { [int]$_.OwningProcess } | Where-Object { $_ -gt 0 } | Select-Object -Unique)
    if ($ownerIds.Count) { return $ownerIds }

    # Get-NetTCPConnection may return nothing in a normal non-elevated shell.
    # netstat is available on supported Windows versions and still exposes the
    # owning PID without requiring administrator privileges.
    $netstat = Join-Path $env:SystemRoot 'System32\netstat.exe'
    if (-not (Test-Path -LiteralPath $netstat)) { $netstat = 'netstat.exe' }
    $pattern = "^\s*TCP\s+(?:127\.0\.0\.1|\[::1\]):$TargetPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
    $fallbackIds = New-Object System.Collections.Generic.List[int]
    foreach ($line in (& $netstat -ano -p tcp 2>$null)) {
        if ($line -match $pattern) {
            [void]$fallbackIds.Add([int]$matches[1])
        }
    }
    return @($fallbackIds | Select-Object -Unique)
}

function Stop-VerifiedBacklotServer {
    param([int]$TargetPort)

    $ownerIds = @(Get-ListeningProcessIds -TargetPort $TargetPort)
    if (-not $ownerIds.Count) { return }

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$TargetPort/api/health" -Method Get -TimeoutSec 3
    } catch {
        throw "端口 $TargetPort 有监听程序，但无法确认它是 OpenMontage；为保护该程序，未执行重启。"
    }
    if ($health.ok -ne $true -or $health.app -ne 'backlot') {
        throw "端口 $TargetPort 的健康响应不属于 OpenMontage；为保护该程序，未执行重启。"
    }

    $verifiedIds = New-Object System.Collections.Generic.List[int]
    foreach ($ownerId in $ownerIds) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId" -ErrorAction SilentlyContinue
        if ($process -and ($process.CommandLine -notmatch '(^|\s)-m\s+backlot\s+serve(\s|$)' -or $process.CommandLine -notmatch "--port\s+$TargetPort(\s|$)")) {
            throw "端口 $TargetPort 正被非 OpenMontage 服务使用；为保护该程序，未执行重启。"
        }
        if (-not (Get-Process -Id $ownerId -ErrorAction SilentlyContinue)) {
            throw "端口 $TargetPort 的监听进程已经变化；为保护新进程，未执行重启。"
        }
        [void]$verifiedIds.Add([int]$ownerId)
    }

    $currentOwnerIds = @(Get-ListeningProcessIds -TargetPort $TargetPort)
    if (@(Compare-Object -ReferenceObject $ownerIds -DifferenceObject $currentOwnerIds).Count) {
        throw "端口 $TargetPort 的监听进程已经变化；为保护新进程，未执行重启。"
    }
    $targets = @($verifiedIds | Select-Object -Unique)
    Write-Host "正在停止旧版 OpenMontage 服务（端口 $TargetPort）…" -ForegroundColor Yellow
    Stop-Process -Id $targets -Force -ErrorAction Stop
    Start-Sleep -Milliseconds 700
}

function Test-BacklotHealth {
    param([string]$BaseUrl)

    try {
        $health = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/api/health') -Method Get -TimeoutSec 5
        return $null -ne $health
    } catch {
        return $false
    }
}

function Open-WorkbenchBrowser {
    param([string]$Url)

    try {
        Start-Process -FilePath $Url -ErrorAction Stop | Out-Null
        return $true
    } catch {
        try {
            # explorer.exe delegates HTTP URLs to the Windows shell and is a
            # useful fallback when PowerShell cannot resolve the browser file
            # association directly.
            Start-Process -FilePath 'explorer.exe' -ArgumentList $Url -ErrorAction Stop | Out-Null
            return $true
        } catch {
            return $false
        }
    }
}

try {
    Set-Location -LiteralPath $projectRoot
    $env:BACKLOT_PORT = "$Port"
    $ttsStarter = Join-Path $projectRoot 'scripts\start_local_tts.ps1'
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ttsStarter -TimeoutSeconds 45
        if ($LASTEXITCODE -ne 0) { throw "本地配音启动器返回退出码 $LASTEXITCODE" }
    } catch {
        Write-Host "本地配音暂未就绪：$($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host '工作台仍会启动；需要配音时请先运行 scripts\setup_local_tts.ps1。' -ForegroundColor Yellow
    }
    if ($Restart) {
        Stop-VerifiedBacklotServer -TargetPort $Port
    }
    Write-Host "正在启动本地工作台，首次启动可能需要约一分钟……" -ForegroundColor Cyan
    & $python -m backlot open --no-browser
    if ($LASTEXITCODE -ne 0) {
        throw "本地工作台未能在端口 $Port 启动。"
    }
    if (-not (Test-BacklotHealth -BaseUrl $workbenchUrl)) {
        throw "本地工作台进程已启动，但健康检查未通过（端口 $Port）。"
    }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "请确认端口 $Port 没有被其他程序占用，并检查 .venv 是否已完成依赖安装。" -ForegroundColor Yellow
    exit 1
}

Write-Host "`n工作台已就绪：$workbenchUrl" -ForegroundColor Green
if (-not $NoBrowser) {
    if (-not (Open-WorkbenchBrowser -Url $workbenchUrl)) {
        Write-Host '工作台已经启动，但系统未能自动打开浏览器。请复制上面的地址到浏览器访问。' -ForegroundColor Yellow
    }
}
