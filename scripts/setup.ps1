param(
    [switch]$SkipEnvFile,
    [switch]$SkipInstall,
    [switch]$SkipDev,
    [switch]$SkipAsr,
    [switch]$SkipNode,
    [switch]$SkipLocalTts,
    [switch]$MigrateLegacyVoicebox
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repoRoot

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

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
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
        throw 'Compatible Python was not found. Install 64-bit Python 3.12 and rerun setup.'
    }
    & $pythonCommand @pythonPrefix -m venv (Join-Path $repoRoot '.venv')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the Python 3.12 project virtual environment.'
    }
}

$pythonVersionOutput = & $venvPython -c "import struct, sys; print('.'.join(map(str, sys.version_info[:2])) + ':' + str(struct.calcsize('P') * 8))"
$pythonVersionExitCode = $LASTEXITCODE
$pythonVersion = ([string]$pythonVersionOutput).Trim()
if ($pythonVersionExitCode -ne 0 -or $pythonVersion -ne '3.12:64') {
    throw "OpenMontage full Windows deployment requires 64-bit Python 3.12. Current project environment: $pythonVersion"
}

if (-not $SkipInstall) {
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency installation failed. Use -SkipInstall to create directories only.'
    }

    if (-not $SkipDev) {
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot 'requirements-dev.txt')
        if ($LASTEXITCODE -ne 0) {
            throw 'Development and acceptance-test dependency installation failed. Use -SkipDev only when tests will not run on this machine.'
        }
    }

    if (-not $SkipAsr) {
        & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $repoRoot 'requirements-asr.txt')
        if ($LASTEXITCODE -ne 0) {
            throw 'Local ASR dependency installation failed. Use -SkipAsr only when avatar diagnostics are not required.'
        }
    }

    if (-not $SkipNode) {
        $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
        $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
        if (-not $nodeCommand -or -not $npmCommand) {
            throw 'Node.js and npm were not found. Install Node.js 22+ or rerun with -SkipNode.'
        }
        $nodeMajor = [int]((& $nodeCommand.Source --version).Trim().TrimStart('v').Split('.')[0])
        if ($nodeMajor -lt 22) {
            throw "HyperFrames rendering requires Node.js 22+. Current major version: $nodeMajor"
        }
        Push-Location (Join-Path $repoRoot 'remotion-composer')
        try {
            & $npmCommand.Source ci --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw 'Remotion dependency installation failed.'
            }
        } finally {
            Pop-Location
        }
    }
}

if (-not $SkipLocalTts) {
    $ttsSetup = Join-Path $repoRoot 'scripts\setup_local_tts.ps1'
    $ttsArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ttsSetup)
    if ($SkipInstall) { $ttsArguments += '-SkipInstall' }
    if ($MigrateLegacyVoicebox) { $ttsArguments += '-MigrateVoicebox' }
    & powershell.exe @ttsArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'OpenMontage 本地配音运行时安装失败。可用 -SkipLocalTts 跳过。'
    }
}

$requiredDirs = @(
    'content',
    'content\episodes',
    'content\library',
    'content\templates',
    'projects',
    'renders',
    'cache',
    'temp'
)
foreach ($relative in $requiredDirs) {
    New-Item -ItemType Directory -Path (Join-Path $repoRoot $relative) -Force | Out-Null
}

if (-not $SkipEnvFile) {
    $envLocal = Join-Path $repoRoot '.env.local'
    if (-not (Test-Path -LiteralPath $envLocal)) {
        Copy-Item -LiteralPath (Join-Path $repoRoot '.env.example') -Destination $envLocal
        Write-Output "Created .env.local from .env.example. Add local credentials there; never commit it."
    } else {
        Write-Output '.env.local already exists; preserved it.'
    }
}

Write-Output "OpenMontage workspace ready: $repoRoot"
Write-Output "Python runtime: $venvPython ($pythonVersion)"
if (-not $SkipInstall -and -not $SkipAsr) { Write-Output 'Local ASR dependencies: installed' }
if (-not $SkipInstall -and -not $SkipDev) { Write-Output 'Development and acceptance-test dependencies: installed' }
if (-not $SkipInstall -and -not $SkipNode) { Write-Output 'Remotion dependencies: installed with npm ci' }
