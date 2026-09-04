[CmdletBinding()]
param(
    # Omit this to use the newest verified Vulkan runtime installed by Hermes.
    # An explicit path remains authoritative for development and recovery.
    [string]$RuntimeRoot,
    # Runtime builds are replaceable; credentials, PID ownership, and logs are
    # stable owner state and must not move when Hermes installs a newer build.
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    [string]$ModelRoot = 'G:\LocalAI\models\Qwen3.8-27B-Uncensored-HauhauCS-Aggressive',
    # 8080 is owned by Yousef's WhatsApp bridge after login. Keep the local
    # model on its own stable loopback port so startup order cannot decide
    # which assistant survives a reboot.
    [int]$Port = 8081,
    [int]$ContextLength = 65536,
    # The llama.cpp CLI advertises max, but this exact Qwen template rejects
    # it. xhigh is the highest working tier for the selected model.
    [ValidateSet('low', 'medium', 'xhigh')]
    [string]$ReasoningEffort = 'xhigh',
    # Keep xhigh reasoning useful without allowing a routine turn to spend
    # ten-thousand-plus tokens thinking invisibly. llama.cpp closes the
    # reasoning block when this budget is reached, leaving room for the answer.
    [ValidateRange(256, 8192)]
    [int]$ReasoningBudget = 2048,
    # Release the model weights and KV cache after a quiet period while
    # leaving llama-server alive. The next Desktop or Telegram request wakes
    # it automatically; three minutes releases the 20+ GiB allocation soon
    # after an active chat while still covering normal back-to-back turns.
    [ValidateRange(60, 86400)]
    [int]$SleepIdleSeconds = 180,
    [string]$HindsightRuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$HindsightProfile = 'hermes',
    [ValidateRange(1024, 65535)]
    [int]$HindsightPort = 9177
)

$ErrorActionPreference = 'Stop'

function Resolve-OwnerManagedLlamaCppVulkanRuntime {
    param([Parameter(Mandatory = $true)][string]$LocalAppData)

    if ([string]::IsNullOrWhiteSpace($LocalAppData)) {
        throw 'LOCALAPPDATA is unavailable; cannot locate the Hermes-managed llama.cpp runtime.'
    }

    $runtimesRoot = [System.IO.Path]::GetFullPath((
        Join-Path $LocalAppData 'hermes\runtimes\llamacpp'
    ))
    $candidates = @()
    if (Test-Path -LiteralPath $runtimesRoot -PathType Container) {
        foreach ($tagDirectory in @(Get-ChildItem -LiteralPath $runtimesRoot -Directory)) {
            if ($tagDirectory.Name -notmatch '^b(?<release>[0-9]+)$') {
                continue
            }
            $releaseNumber = 0L
            if (-not [long]::TryParse($Matches['release'], [ref]$releaseNumber)) {
                continue
            }

            $vulkanRoot = Join-Path $tagDirectory.FullName 'vulkan'
            $manifestPath = Join-Path $vulkanRoot 'manifest.json'
            if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
                continue
            }
            try {
                $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
            } catch {
                continue
            }
            if (-not $manifest.verified_version -or [string]::IsNullOrWhiteSpace([string]$manifest.verified_version)) {
                continue
            }

            $candidates += [pscustomobject]@{
                ReleaseNumber = $releaseNumber
                Tag = $tagDirectory.Name
                RuntimeRoot = $vulkanRoot
            }
        }
    }

    $selected = $candidates |
        Sort-Object -Property ReleaseNumber, Tag -Descending |
        Select-Object -First 1
    if (-not $selected) {
        throw "No verified Hermes-managed llama.cpp Vulkan runtime was found under $runtimesRoot. Install the managed Vulkan runtime in Hermes first or pass -RuntimeRoot explicitly."
    }
    return [string]$selected.RuntimeRoot
}

$runtimeRootWasExplicit = $PSBoundParameters.ContainsKey('RuntimeRoot')
if (-not $runtimeRootWasExplicit) {
    $RuntimeRoot = Resolve-OwnerManagedLlamaCppVulkanRuntime -LocalAppData $env:LOCALAPPDATA
}

function ConvertFrom-OwnerWindowsCommandLine {
    param([Parameter(Mandatory = $true)][string]$CommandLine)

    # Win32_Process.CommandLine is one raw Windows command line. Parse it with
    # the platform routine instead of splitting on spaces: model/runtime paths
    # are allowed to contain spaces, and a false mismatch here would strand a
    # perfectly healthy server after every logon.
    if (-not ('HermesOwnerCommandLine.NativeMethods' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace HermesOwnerCommandLine {
    public static class NativeMethods {
        [DllImport("shell32.dll", SetLastError = true)]
        public static extern IntPtr CommandLineToArgvW(
            [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
            out int argumentCount
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr LocalFree(IntPtr memory);
    }
}
'@ -ErrorAction Stop
    }

    $argumentCount = 0
    $argumentVector = [HermesOwnerCommandLine.NativeMethods]::CommandLineToArgvW(
        $CommandLine,
        [ref]$argumentCount
    )
    if ($argumentVector -eq [IntPtr]::Zero) {
        throw "Could not parse the existing llama-server command line (Win32 error $([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))."
    }

    try {
        [string[]]$arguments = New-Object string[] $argumentCount
        for ($index = 0; $index -lt $argumentCount; $index++) {
            $itemPointer = [System.Runtime.InteropServices.Marshal]::ReadIntPtr(
                $argumentVector,
                $index * [IntPtr]::Size
            )
            $arguments[$index] = [System.Runtime.InteropServices.Marshal]::PtrToStringUni($itemPointer)
        }
        return $arguments
    } finally {
        [void][HermesOwnerCommandLine.NativeMethods]::LocalFree($argumentVector)
    }
}

function ConvertTo-OwnerPowerShellLiteral {
    param([AllowEmptyString()][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Get-OwnerLocalAiRestartMessage {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $rerun = @(
        '& ' + (ConvertTo-OwnerPowerShellLiteral $PSCommandPath),
        '-RuntimeRoot ' + (ConvertTo-OwnerPowerShellLiteral $RuntimeRoot),
        '-StateRoot ' + (ConvertTo-OwnerPowerShellLiteral $StateRoot),
        '-ModelRoot ' + (ConvertTo-OwnerPowerShellLiteral $ModelRoot),
        '-Port ' + $Port,
        '-ContextLength ' + $ContextLength,
        '-ReasoningEffort ' + (ConvertTo-OwnerPowerShellLiteral $ReasoningEffort),
        '-ReasoningBudget ' + $ReasoningBudget,
        '-SleepIdleSeconds ' + $SleepIdleSeconds,
        '-HindsightRuntimeRoot ' + (ConvertTo-OwnerPowerShellLiteral $HindsightRuntimeRoot),
        '-HindsightHome ' + (ConvertTo-OwnerPowerShellLiteral $HindsightHome),
        '-HindsightProfile ' + (ConvertTo-OwnerPowerShellLiteral $HindsightProfile),
        '-HindsightPort ' + $HindsightPort
    ) -join ' '

    return @"
Existing owner local-AI server PID $ProcessId was launched with different settings, so the requested settings were NOT applied. Hermes will not replace an active server automatically.
Stop it exactly with: Stop-Process -Id $ProcessId
Then restart it exactly with: $rerun
"@
}

function Assert-OwnerLocalAiProcessArguments {
    param(
        [Parameter(Mandatory = $true)]$ProcessInfo,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [Parameter(Mandatory = $true)][string[]]$ExpectedArguments,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )

    if ([string]::IsNullOrWhiteSpace([string]$ProcessInfo.CommandLine)) {
        throw (Get-OwnerLocalAiRestartMessage -ProcessId $ProcessId)
    }

    try {
        $actualCommand = @(ConvertFrom-OwnerWindowsCommandLine -CommandLine $ProcessInfo.CommandLine)
    } catch {
        throw ((Get-OwnerLocalAiRestartMessage -ProcessId $ProcessId) + "`nCommand-line inspection failed: $($_.Exception.Message)")
    }

    $matches = $actualCommand.Count -eq ($ExpectedArguments.Count + 1)
    if ($matches) {
        try {
            $actualExecutable = [System.IO.Path]::GetFullPath($actualCommand[0])
        } catch {
            $matches = $false
        }
        if ($matches -and -not $actualExecutable.Equals(
            $ExpectedExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            $matches = $false
        }
    }

    if ($matches) {
        $pathValueOptions = @('--model', '--mmproj', '--api-key-file')
        for ($index = 0; $index -lt $ExpectedArguments.Count; $index++) {
            $actualValue = $actualCommand[$index + 1]
            $expectedValue = $ExpectedArguments[$index]
            $isPathValue = $index -gt 0 -and $ExpectedArguments[$index - 1] -in $pathValueOptions
            if ($isPathValue) {
                try {
                    $actualValue = [System.IO.Path]::GetFullPath($actualValue)
                    $expectedValue = [System.IO.Path]::GetFullPath($expectedValue)
                    $valueMatches = $actualValue.Equals(
                        $expectedValue,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )
                } catch {
                    $valueMatches = $false
                }
            } else {
                $valueMatches = $actualValue -ceq $expectedValue
            }
            if (-not $valueMatches) {
                $matches = $false
                break
            }
        }
    }

    if (-not $matches) {
        throw (Get-OwnerLocalAiRestartMessage -ProcessId $ProcessId)
    }
}

$serverPath = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot 'llama-server.exe'))
$modelPath = [System.IO.Path]::GetFullPath((Join-Path $ModelRoot 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf'))
$projectorPath = [System.IO.Path]::GetFullPath((Join-Path $ModelRoot 'mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf'))
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$keyPath = [System.IO.Path]::Combine($statePath, 'server-api-key.txt')
$pidPath = [System.IO.Path]::Combine($statePath, 'server.pid')
$stdoutPath = [System.IO.Path]::Combine($statePath, 'server.out.log')
$stderrPath = [System.IO.Path]::Combine($statePath, 'server.err.log')
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
    [System.IO.Directory]::CreateDirectory($statePath) | Out-Null
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
    '--sleep-idle-seconds', "$SleepIdleSeconds",
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
            if ($existingPath.Equals(
                $serverPath,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                # A same-binary process is not necessarily the requested
                # server. Compare the complete owner-managed launch contract
                # before reusing it. In particular, changing xhigh/budget or
                # --no-reasoning-preserve must never print a false success while
                # the old settings remain resident in the live process.
                Assert-OwnerLocalAiProcessArguments `
                    -ProcessInfo $existingCim `
                    -ExpectedExecutable $serverPath `
                    -ExpectedArguments $serverArgs `
                    -ProcessId $existingPid
                $process = $existing
            } else {
                # A runtime update can make $serverPath point at a newer
                # managed build while the previous build is still serving.
                # Never discard its ownership record and launch a competitor:
                # the new process would race the old one for the stable port,
                # and a health probe could then mistake the old server for the
                # process we just started.
                throw (Get-OwnerLocalAiRestartMessage -ProcessId $existingPid)
            }
        }
    }
    if (-not $process) {
        Remove-Item -LiteralPath $pidPath -Force
    }
}

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
