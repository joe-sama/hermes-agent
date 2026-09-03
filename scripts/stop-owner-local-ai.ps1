[CmdletBinding()]
param(
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    # When supplied, only this exact runtime may be stopped. Otherwise the PID
    # must resolve to a manifest-verified Hermes-managed Vulkan runtime.
    [string]$RuntimeRoot
)

$ErrorActionPreference = 'Stop'
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$pidPath = Join-Path $statePath 'server.pid'
$runtimeRootWasExplicit = $PSBoundParameters.ContainsKey('RuntimeRoot')

function Test-OwnerManagedVulkanExecutable {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { return $false }

    $managedRoot = [System.IO.Path]::GetFullPath((
        Join-Path $env:LOCALAPPDATA 'hermes\runtimes\llamacpp'
    ))
    $executable = [System.IO.Path]::GetFullPath($ExecutablePath)
    if (-not (Split-Path $executable -Leaf).Equals(
        'llama-server.exe',
        [System.StringComparison]::OrdinalIgnoreCase
    )) { return $false }

    $backendRoot = Split-Path $executable -Parent
    if (-not (Split-Path $backendRoot -Leaf).Equals(
        'vulkan',
        [System.StringComparison]::OrdinalIgnoreCase
    )) { return $false }

    $tagRoot = Split-Path $backendRoot -Parent
    $tag = Split-Path $tagRoot -Leaf
    if ($tag -notmatch '^b[0-9]+$') { return $false }
    if (-not ([System.IO.Path]::GetFullPath((Split-Path $tagRoot -Parent))).Equals(
        $managedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) { return $false }

    $manifestPath = Join-Path $backendRoot 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    } catch {
        return $false
    }

    return (
        [string]$manifest.tag -ceq $tag -and
        [string]$manifest.backend -ceq 'vulkan' -and
        -not [string]::IsNullOrWhiteSpace([string]$manifest.verified_version)
    )
}

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
if ($process.ProcessName -ne 'llama-server' -or -not $actualPath) {
    throw "Refusing to stop PID $serverPid because it is not the configured llama-server."
}

if ($runtimeRootWasExplicit) {
    $serverPath = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'llama-server.exe'))
    if (-not $actualPath.Equals($serverPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to stop PID $serverPid because it is not the configured llama-server."
    }
} elseif (-not (Test-OwnerManagedVulkanExecutable -ExecutablePath $actualPath)) {
    throw "Refusing to stop PID $serverPid because its executable is not a verified Hermes-managed Vulkan llama-server."
}

Stop-Process -Id $serverPid
Wait-Process -Id $serverPid -Timeout 30 -ErrorAction SilentlyContinue
if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
    throw "Local AI process PID $serverPid did not stop within 30 seconds."
}
Remove-Item -LiteralPath $pidPath -Force
Write-Output "Local AI stopped (PID $serverPid)."
