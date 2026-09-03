[CmdletBinding()]
param(
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    [int]$ModelPort = 8081,
    [int]$HindsightPort = 9177,
    [string]$GatewayLauncher = "$env:LOCALAPPDATA\hermes\gateway-service\Hermes_Gateway.vbs",
    [ValidateRange(30, 900)]
    # Model startup allows 180s and Hindsight another 240s. Leave margin for
    # a cold Windows logon rather than racing the declared dependency bounds.
    [int]$StartupTimeoutSeconds = 600,
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$keyPath = Join-Path $statePath 'server-api-key.txt'
$gatewayPath = [System.IO.Path]::GetFullPath($GatewayLauncher)

if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "Local-model API key was not found: $keyPath"
}
$apiKey = [System.IO.File]::ReadAllText($keyPath).Trim()
if (-not $apiKey) { throw "Local-model API key is empty: $keyPath" }

function Test-OwnerDependencies {
    try {
        $model = Invoke-RestMethod -Uri "http://127.0.0.1:$ModelPort/health" -Headers @{ Authorization = "Bearer $apiKey" } -TimeoutSec 3
        if ($model.status -ne 'ok') { return $false }

        $memory = Invoke-RestMethod -Uri "http://127.0.0.1:$HindsightPort/health" -TimeoutSec 3
        $databaseStatus = if ($memory.database -is [string]) {
            $memory.database
        } else {
            $memory.database.status
        }
        return $memory.status -eq 'healthy' -and $databaseStatus -eq 'connected'
    } catch {
        return $false
    }
}

$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
do {
    if (Test-OwnerDependencies) {
        if ($ProbeOnly) {
            Write-Output 'Owner-local model and Hindsight dependencies are ready.'
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
        Write-Output 'Hermes gateway launched after local model and Hindsight became healthy.'
        return
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)

throw "Hermes gateway dependencies did not become healthy within $StartupTimeoutSeconds seconds."
