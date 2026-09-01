param(
    [int]$TimeoutSeconds = 45
)

# Historical compatibility wrapper. All runtime ownership now belongs to
# Haike Video; no standalone Voicebox executable or data directory is used.
$starter = Join-Path $PSScriptRoot 'start_local_tts.ps1'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $starter -TimeoutSeconds $TimeoutSeconds
exit $LASTEXITCODE
