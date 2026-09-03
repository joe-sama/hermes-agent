[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'G:\LocalAI\llama.cpp\b10621',
    [string]$ModelRoot = 'G:\LocalAI\models\Qwen3.8-27B-Uncensored-HauhauCS-Aggressive',
    # 8080 is owned by Yousef's WhatsApp bridge after login. Keep the local
    # model on its own stable loopback port so startup order cannot decide
    # which assistant survives a reboot.
    [int]$Port = 8081,
    [int]$ContextLength = 65536,
    # The b10621 binary advertises max, but this exact Qwen template rejects
    # it. xhigh is the highest working tier for the selected model.
    [ValidateSet('low', 'medium', 'xhigh')]
    [string]$ReasoningEffort = 'xhigh',
    # Keep xhigh reasoning useful without allowing a routine turn to spend
    # ten-thousand-plus tokens thinking invisibly. llama.cpp closes the
    # reasoning block when this budget is reached, leaving room for the answer.
    [ValidateRange(256, 8192)]
    [int]$ReasoningBudget = 2048,
    [string]$HindsightRuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$HindsightProfile = 'hermes',
    [ValidateRange(1024, 65535)]
    [int]$HindsightPort = 9177
)

$ErrorActionPreference = 'Stop'
$serverPath = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'llama-server.exe'))
$modelPath = [System.IO.Path]::GetFullPath((Join-Path $ModelRoot 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf'))
$projectorPath = [System.IO.Path]::GetFullPath((Join-Path $ModelRoot 'mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf'))
$stateRoot = [System.IO.Path]::GetFullPath((Split-Path $RuntimeRoot -Parent))
$keyPath = Join-Path $stateRoot 'server-api-key.txt'
$pidPath = Join-Path $stateRoot 'server.pid'
$stdoutPath = Join-Path $stateRoot 'server.out.log'
$stderrPath = Join-Path $stateRoot 'server.err.log'
$aclHelpers = Join-Path $PSScriptRoot 'windows-owner-acl.ps1'

if (-not (Test-Path -LiteralPath $aclHelpers -PathType Leaf)) {
    throw "Owner-only Windows ACL helpers were not found: $aclHelpers"
}
. $aclHelpers

foreach ($required in @($serverPath, $modelPath, $projectorPath)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required local-AI file is missing: $required"
    }
}

if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    # Create and lock the empty file before any secret bytes are written.
    [System.IO.File]::WriteAllBytes($keyPath, [byte[]]@())
    Set-OwnerOnlyFileAcl -Path $keyPath
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    # BitConverter works in the Windows PowerShell 5.1/.NET Framework runtime
    # used by the logon task; Convert.ToHexString is .NET Core-only there.
    $apiKey = 'hermes-local-' + [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    [System.IO.File]::WriteAllText($keyPath, $apiKey, [System.Text.UTF8Encoding]::new($false))
}
Set-OwnerOnlyFileAcl -Path $keyPath
$apiKey = [System.IO.File]::ReadAllText($keyPath).Trim()
if (-not $apiKey) {
    throw "Local API key file is empty: $keyPath"
}

$process = $null
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existingPid = 0
    [void][int]::TryParse([System.IO.File]::ReadAllText($pidPath).Trim(), [ref]$existingPid)
    if ($existingPid -gt 0) {
        $existing = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($existing -and $existing.ProcessName -eq 'llama-server') {
            $existingCim = Get-CimInstance Win32_Process -Filter "ProcessId = $existingPid"
            $existingPath = if ($existingCim.ExecutablePath) {
                [System.IO.Path]::GetFullPath($existingCim.ExecutablePath)
            } else { '' }
            if ($existingPath -eq $serverPath) {
                $process = $existing
            }
        }
    }
    if (-not $process) {
        Remove-Item -LiteralPath $pidPath -Force
    }
}

$serverArgs = @(
    '--model', $modelPath,
    '--alias', 'qwen38-27b-aggressive',
    '--mmproj', $projectorPath,
    '--image-min-tokens', '1024',
    '--gpu-layers', 'all',
    '--ctx-size', "$ContextLength",
    '--parallel', '1',
    '--cache-type-k', 'q8_0',
    '--cache-type-v', 'q8_0',
    '--fit', 'off',
    '--flash-attn', 'on',
    '--jinja',
    '--reasoning', 'on',
    '--reasoning-effort', $ReasoningEffort,
    '--reasoning-budget', "$ReasoningBudget",
    '--no-reasoning-preserve',
    '--reasoning-format', 'deepseek',
    '--temp', '1.0',
    '--top-p', '0.95',
    '--top-k', '20',
    '--min-p', '0',
    '--presence-penalty', '0',
    '--repeat-penalty', '1.0',
    '--spec-type', 'draft-mtp',
    '--spec-draft-n-max', '2',
    '--spec-draft-p-min', '0',
    '--host', '127.0.0.1',
    '--port', "$Port",
    '--cors-origins', 'localhost',
    '--api-key-file', $keyPath,
    '--no-ui',
    '--slots'
)

if (-not $process) {
    $process = Start-Process -FilePath $serverPath -ArgumentList $serverArgs -WorkingDirectory $RuntimeRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    [System.IO.File]::WriteAllText($pidPath, [string]$process.Id, [System.Text.UTF8Encoding]::new($false))
}

$headers = @{ Authorization = "Bearer $apiKey" }
$deadline = [DateTime]::UtcNow.AddMinutes(3)
do {
    if ($process.HasExited) {
        if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
            Remove-Item -LiteralPath $pidPath -Force
        }
        throw "llama-server exited during startup with code $($process.ExitCode). See $stderrPath"
    }
    $health = $null
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers $headers -TimeoutSec 3
    } catch {
        # The server commonly refuses connections while model tensors load.
    }
    if ($health.status -eq 'ok') {
        $props = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/props" -Headers $headers -TimeoutSec 10
        $actualContextLength = [int]$props.default_generation_settings.n_ctx
        if ($actualContextLength -ne $ContextLength) {
            throw "Local AI is healthy but reports context $actualContextLength instead of $ContextLength. Stop it and restart with the requested configuration."
        }
        $hindsightLauncher = Join-Path $PSScriptRoot 'start-owner-hindsight.ps1'
        if (-not (Test-Path -LiteralPath $hindsightLauncher -PathType Leaf)) {
            throw "Isolated Hindsight launcher was not found: $hindsightLauncher"
        }
        & $hindsightLauncher -RuntimeRoot $HindsightRuntimeRoot -HindsightHome $HindsightHome -Profile $HindsightProfile -Port $HindsightPort
        Write-Output "Local AI ready on http://127.0.0.1:$Port/v1 (PID $($process.Id), context $actualContextLength, reasoning $ReasoningEffort, thinking budget $ReasoningBudget)."
        exit 0
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)

throw "llama-server did not become healthy within 3 minutes. See $stderrPath"
