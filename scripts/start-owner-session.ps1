[CmdletBinding()]
param(
    [string]$HermesHome = $env:HERMES_HOME,
    [string]$HindsightRuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$HindsightProfile = 'hermes',
    [int]$HindsightPort = 9177
)

$ErrorActionPreference = 'Stop'
if (-not $HermesHome) { throw 'HermesHome must name the installed, persistent data directory.' }
$ownerHome = [IO.Path]::GetFullPath($HermesHome)
if (-not (Test-Path -LiteralPath (Join-Path $ownerHome 'config.yaml') -PathType Leaf)) {
    throw "The configured Hermes home has no config.yaml: $ownerHome"
}
$logRoot = Join-Path $ownerHome 'logs'
[IO.Directory]::CreateDirectory($logRoot) | Out-Null
$receipt = [ordered]@{ started_at = [DateTime]::UtcNow.ToString('o'); home = $ownerHome; components = @{} }
$previousHome = $env:HERMES_HOME
$errorsFound = @()
Start-Transcript -LiteralPath (Join-Path $logRoot 'windows-startup.log') -Append | Out-Null
try {
    $env:HERMES_HOME = $ownerHome
    # Independent entry points: a memory-service failure must not keep the
    # Desktop/model offline, and a missing ChatGPT install must not stop bots.
    foreach ($component in @('desktop-apps', 'gateway')) {
        try {
            if ($component -eq 'desktop-apps') {
                & (Join-Path $PSScriptRoot 'start-owner-desktop-apps.ps1')
            } else {
                & (Join-Path $PSScriptRoot 'start-owner-gateway.ps1') `
                    -HindsightRuntimeRoot $HindsightRuntimeRoot `
                    -HindsightHome $HindsightHome -HindsightProfile $HindsightProfile `
                    -HindsightPort $HindsightPort
            }
            $receipt.components[$component] = @{ launch = 'completed' }
        } catch {
            $message = $_.Exception.Message
            $receipt.components[$component] = @{ launch = 'failed'; error = $message }
            $errorsFound += $component
            Write-Warning "$component startup failed: $message"
        }
    }
} finally {
    $env:HERMES_HOME = $previousHome
    $receipt.completed_at = [DateTime]::UtcNow.ToString('o')
    # This is deliberately a launch receipt, not a claim that inference was
    # verified. test-owner-local-ai.ps1 exercises the actual generation path.
    $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logRoot 'windows-startup.json') -Encoding UTF8
    Stop-Transcript | Out-Null
}
if ($errorsFound.Count) { throw "Startup needs attention: $($errorsFound -join ', '). See $logRoot\windows-startup.log" }
