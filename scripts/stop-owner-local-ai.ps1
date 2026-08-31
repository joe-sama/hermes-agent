[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'G:\LocalAI\llama.cpp\b10621'
)

$ErrorActionPreference = 'Stop'
$serverPath = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'llama-server.exe'))
$stateRoot = [System.IO.Path]::GetFullPath((Split-Path $RuntimeRoot -Parent))
$pidPath = Join-Path $stateRoot 'server.pid'

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Output 'Local AI PID file is absent; nothing to stop.'
    exit 0
}

$serverPid = 0
if (-not [int]::TryParse([System.IO.File]::ReadAllText($pidPath).Trim(), [ref]$serverPid) -or $serverPid -le 0) {
    throw "Invalid PID file: $pidPath"
}

$process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $pidPath -Force
    Write-Output 'Local AI process was already stopped.'
    exit 0
}

$cim = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid"
$actualPath = if ($cim.ExecutablePath) { [System.IO.Path]::GetFullPath($cim.ExecutablePath) } else { '' }
if ($process.ProcessName -ne 'llama-server' -or $actualPath -ne $serverPath) {
    throw "Refusing to stop PID $serverPid because it is not the configured llama-server."
}

Stop-Process -Id $serverPid
Wait-Process -Id $serverPid -Timeout 30 -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $pidPath -Force
Write-Output "Local AI stopped (PID $serverPid)."
