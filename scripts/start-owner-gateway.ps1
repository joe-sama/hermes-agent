[CmdletBinding()]
param(
    [string]$HindsightRuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$HindsightProfile = 'hermes',
    [ValidateRange(1024, 65535)]
    [int]$HindsightPort = 9177,
    [string]$GatewayLauncher = "$env:LOCALAPPDATA\hermes\gateway-service\Hermes_Gateway.vbs",
    [ValidateRange(30, 900)]
    [int]$StartupTimeoutSeconds = 300,
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'
$gatewayPath = [System.IO.Path]::GetFullPath($GatewayLauncher)
$hindsightLauncher = Join-Path $PSScriptRoot 'start-owner-hindsight.ps1'

if (-not (Test-Path -LiteralPath $hindsightLauncher -PathType Leaf)) {
    throw "Isolated Hindsight launcher was not found: $hindsightLauncher"
}

# The model is owned by Hermes' managed local runtime now. Do not wait for or
# start a second external llama-server here: Desktop/gateway endpoint
# resolution boots the same managed Vulkan router and reuses its stable state.
& $hindsightLauncher `
    -RuntimeRoot $HindsightRuntimeRoot `
    -HindsightHome $HindsightHome `
    -Profile $HindsightProfile `
    -Port $HindsightPort `
    -StartupTimeoutSeconds $StartupTimeoutSeconds

if ($ProbeOnly) {
    Write-Output 'Owner Hindsight dependency is ready; the model is Hermes-managed.'
    return
}

if (-not (Test-Path -LiteralPath $gatewayPath -PathType Leaf)) {
    throw "Hermes gateway launcher was not found: $gatewayPath"
}
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $wscript -PathType Leaf)) {
    throw "Windows Script Host was not found: $wscript"
}
Start-Process -FilePath $wscript -ArgumentList @("`"$gatewayPath`"") -WindowStyle Hidden
Write-Output 'Hermes gateway launched after Hindsight became healthy; local Qwen will autoload on demand.'
