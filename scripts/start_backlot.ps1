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
    $logDir = Join-Path $projectRoot '.backlot\logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    # launch_backlot.py captures crashes that happen before the server can log
    # (a failed import, for instance); once running, the server appends to
    # backlot.log by itself (see backlot/__main__.py).
    $outLog = Join-Path $logDir 'backlot.out.log'
    $errLog = Join-Path $logDir 'backlot.err.log'
    $serverLog = Join-Path $logDir 'backlot.log'

    if (Test-BacklotHealth -BaseUrl $workbenchUrl) {
        Write-Host "端口 $Port 上已有工作台在运行，直接复用。" -ForegroundColor Green
    } else {
        Write-Host "正在启动本地工作台，首次启动可能需要约一分钟……" -ForegroundColor Cyan
        # The workbench must not be tied to this console window: launching it
        # with the call operator (& python -m backlot open) kept the server a
        # child of this window, so closing the window killed the workbench and
        # left the port dead with no log to explain it.  PowerShell cannot take
        # over the launching either, because Start-Process and
        # ProcessStartInfo both abort as soon as the host injected the same
        # environment variable twice with different casing (Path/PATH,
        # http_proxy/HTTP_PROXY, ...).  scripts\launch_backlot.py detaches the
        # server with plain subprocess flags, which are immune to that.
        $launcher = Join-Path $projectRoot 'scripts\launch_backlot.py'
        if (-not (Test-Path -LiteralPath $launcher)) {
            throw "缺少启动器：$launcher"
        }

        # A forced stop can leave the port briefly unusable; wait for the
        # previous listener to disappear before starting a fresh server.
        $releaseDeadline = (Get-Date).AddSeconds(15)
        while (@(Get-ListeningProcessIds -TargetPort $Port).Count -and (Get-Date) -lt $releaseDeadline) {
            Start-Sleep -Milliseconds 400
        }

        & $python $launcher
        if ($LASTEXITCODE -ne 0) {
            throw "工作台启动器返回退出码 $LASTEXITCODE（$launcher）。"
        }

        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-BacklotHealth -BaseUrl $workbenchUrl)) {
            if ((Get-Date) -ge $deadline) { break }
            Start-Sleep -Milliseconds 600
        }

        if (-not (Test-BacklotHealth -BaseUrl $workbenchUrl)) {
            $tail = @()
            foreach ($candidate in @($serverLog, $errLog, $outLog)) {
                if (Test-Path -LiteralPath $candidate) {
                    $content = ((Get-Content -LiteralPath $candidate -Tail 15) -join "`n").Trim()
                    if ($content) { $tail += "--- $candidate ---`n$content" }
                }
            }
            throw "本地工作台未能在端口 $Port 就绪。`n$($tail -join "`n")"
        }
        Write-Host "工作台已在后台就绪。" -ForegroundColor Green
    }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.PositionMessage) {
        Write-Host "出错位置：$($_.InvocationInfo.PositionMessage)" -ForegroundColor DarkGray
    }
    Write-Host "请确认端口 $Port 没有被其他程序占用，并检查 .venv 是否已完成依赖安装。" -ForegroundColor Yellow
    Write-Host "详细日志：$(Join-Path $projectRoot '.backlot\logs\backlot.log')" -ForegroundColor Yellow
    exit 1
}

Write-Host "`n工作台已就绪：$workbenchUrl" -ForegroundColor Green
Write-Host "工作台已在后台独立运行，关闭本窗口不会影响它。" -ForegroundColor DarkGray
if (-not $NoBrowser) {
    if (-not (Open-WorkbenchBrowser -Url $workbenchUrl)) {
        Write-Host '工作台已经启动，但系统未能自动打开浏览器。请复制上面的地址到浏览器访问。' -ForegroundColor Yellow
    }
}
