[CmdletBinding()]
param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$HermesPython = $(if ($env:HERMES_HOME) { "$env:HERMES_HOME\hermes-agent\venv\Scripts\python.exe" } else { "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" }),
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    [string]$ModelSourceRoot = 'G:\LocalAI\models\Qwen3.8-27B-Uncensored-HauhauCS-Aggressive',
    [string]$ManagedModelRoot = 'D:\LocalAI\hermes-models',
    [string]$HindsightRuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$HindsightProfile = 'hermes',
    [ValidateRange(1024, 65535)]
    [int]$HindsightPort = 9177,
    [string]$HindsightVersion = '0.9.1',
    [string]$StartupDirectory = '',
    [switch]$SkipHindsightInstall,
    [switch]$SkipCuaTelemetry,
    [switch]$SkipStartupTask
)

$ErrorActionPreference = 'Stop'
$homePath = [System.IO.Path]::GetFullPath($HermesHome)
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$legacyApiKeyPath = [System.IO.Path]::Combine($statePath, 'server-api-key.txt')
$sourceRootPath = [System.IO.Path]::GetFullPath($ModelSourceRoot)
$managedModelRootPath = [System.IO.Path]::GetFullPath($ManagedModelRoot)
$sourceModelPath = Join-Path $sourceRootPath 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf'
$sourceProjectorPath = Join-Path $sourceRootPath 'mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf'
$managedModelPath = Join-Path $managedModelRootPath 'Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf'
$managedAssetsPath = Join-Path $managedModelRootPath 'assets'
$managedProjectorPath = Join-Path $managedAssetsPath 'mmproj-Qwen3.8-27B-BF16.gguf'
$managedModelsLink = Join-Path $homePath 'models'
$managedApiKeyPath = Join-Path $homePath 'runtimes\llamacpp\.api_key'
$configPath = Join-Path $homePath 'config.yaml'
$envPath = Join-Path $homePath '.env'
$hindsightDir = Join-Path $homePath 'hindsight'
$hindsightPath = Join-Path $hindsightDir 'config.json'
$hindsightRuntimePath = [System.IO.Path]::GetFullPath($HindsightRuntimeRoot)
$hindsightHomePath = [System.IO.Path]::GetFullPath($HindsightHome)
$hindsightUserHome = Split-Path $hindsightHomePath -Parent
$hindsightProfileDir = Join-Path $hindsightHomePath 'profiles'
$hindsightProfilePath = Join-Path $hindsightProfileDir "$HindsightProfile.env"
$pg0InstancesPath = Join-Path $hindsightUserHome '.pg0\instances'
$hindsightPython = Join-Path $hindsightRuntimePath 'Scripts\python.exe'
$aclHelpers = Join-Path $PSScriptRoot 'windows-owner-acl.ps1'

if (-not (Test-Path -LiteralPath $aclHelpers -PathType Leaf)) {
    throw "Owner-only Windows ACL helpers were not found: $aclHelpers"
}
. $aclHelpers

if ($HindsightProfile -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Invalid Hindsight profile name: $HindsightProfile"
}
if ((Split-Path $hindsightHomePath -Leaf) -ne '.hindsight') {
    throw "HindsightHome must name a .hindsight directory: $hindsightHomePath"
}

function Protect-PrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    Set-OwnerOnlyDirectoryTreeAcl -Path $Path
}

function Protect-PrivateFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        [System.IO.File]::WriteAllBytes($Path, [byte[]]@())
    }
    # Remove every other grant before any secret bytes are written. Repeating
    # the operation after a write is harmless and verifies that the DACL was
    # not replaced by an editor or migration helper.
    Set-OwnerOnlyFileAcl -Path $Path
}

function Write-PrivateFileContent {
    param([string]$Path, [string]$Content)
    Protect-PrivateFile -Path $Path
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
    Protect-PrivateFile -Path $Path
}

function Set-PrivateEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    Protect-PrivateFile -Path $Path
    $lines = [System.IO.File]::ReadAllLines($Path, [System.Text.UTF8Encoding]::new($false))
    $prefix = "$Name="
    $updated = New-Object System.Collections.Generic.List[string]
    $found = $false
    foreach ($line in $lines) {
        if ($line.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
            if (-not $found) { $updated.Add($prefix + $Value); $found = $true }
        } else {
            $updated.Add($line)
        }
    }
    if (-not $found) { $updated.Add($prefix + $Value) }
    [System.IO.File]::WriteAllLines($Path, $updated, [System.Text.UTF8Encoding]::new($false))
    Protect-PrivateFile -Path $Path
}

function Remove-PrivateEnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    Protect-PrivateFile -Path $Path
    $prefix = "$Name="
    $remaining = @(
        [System.IO.File]::ReadAllLines($Path, [System.Text.UTF8Encoding]::new($false)) |
            Where-Object { -not $_.StartsWith($prefix, [System.StringComparison]::Ordinal) }
    )
    [System.IO.File]::WriteAllLines($Path, $remaining, [System.Text.UTF8Encoding]::new($false))
    Protect-PrivateFile -Path $Path
}

if (-not (Test-Path -LiteralPath $HermesPython -PathType Leaf)) {
    throw "Hermes Python was not found: $HermesPython"
}
foreach ($requiredModelFile in @($sourceModelPath, $sourceProjectorPath)) {
    if (-not (Test-Path -LiteralPath $requiredModelFile -PathType Leaf)) {
        throw "Required existing Qwen file is missing: $requiredModelFile"
    }
}
if (-not [string]::Equals(
    [System.IO.Path]::GetPathRoot($sourceRootPath),
    [System.IO.Path]::GetPathRoot($managedModelRootPath),
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'ManagedModelRoot must be on the same volume as ModelSourceRoot so Hermes can reuse the existing weights without copying them.'
}

[System.IO.Directory]::CreateDirectory($homePath) | Out-Null
[System.IO.Directory]::CreateDirectory($hindsightDir) | Out-Null
[System.IO.Directory]::CreateDirectory($managedModelRootPath) | Out-Null
[System.IO.Directory]::CreateDirectory($managedAssetsPath) | Out-Null
[System.IO.Directory]::CreateDirectory((Split-Path $managedApiKeyPath -Parent)) | Out-Null
Protect-PrivateDirectory -Path $hindsightProfileDir
# Keep pg0's executable installation untouched. Only the per-profile database
# instances carry owner memory and need this inheritable private DACL.
Protect-PrivateDirectory -Path $pg0InstancesPath

function Add-OwnerModelHardLink {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $sourceLength = (Get-Item -LiteralPath $Source).Length
        $destinationLength = (Get-Item -LiteralPath $Destination).Length
        if ($sourceLength -ne $destinationLength) {
            throw "Managed model destination already exists with the wrong size: $Destination"
        }
        return
    }
    New-Item -ItemType HardLink -Path $Destination -Target $Source -ErrorAction Stop | Out-Null
}

# Stage the user's existing uncensored Qwen under its exact identity. Hard
# links consume no second 17 GB copy and leave the original files untouched.
Add-OwnerModelHardLink -Source $sourceModelPath -Destination $managedModelPath
Add-OwnerModelHardLink -Source $sourceProjectorPath -Destination $managedProjectorPath

# Hermes' machine-scoped model directory normally lives on C:. Point that one
# directory at the G: staging area so updates keep discovering the same model.
if (Test-Path -LiteralPath $managedModelsLink -PathType Container) {
    $linkItem = Get-Item -LiteralPath $managedModelsLink -Force
    if (($linkItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        $linkTarget = @($linkItem.Target) | Select-Object -First 1
        if (-not $linkTarget) {
            throw "Existing Hermes models reparse point has no readable target: $managedModelsLink"
        }
        if (-not [System.IO.Path]::IsPathRooted([string]$linkTarget)) {
            $linkTarget = Join-Path (Split-Path $managedModelsLink -Parent) ([string]$linkTarget)
        }
        $resolvedTarget = [System.IO.Path]::GetFullPath([string]$linkTarget).TrimEnd('\')
        if (-not [string]::Equals(
            $resolvedTarget,
            $managedModelRootPath.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Hermes models already points somewhere else: $managedModelsLink -> $resolvedTarget"
        }
    } else {
        $existingEntries = @(Get-ChildItem -LiteralPath $managedModelsLink -Force)
        if ($existingEntries.Count -gt 0) {
            throw "Hermes models directory is not empty; refusing to replace it: $managedModelsLink"
        }
        [System.IO.Directory]::Delete($managedModelsLink)
        New-Item -ItemType Junction -Path $managedModelsLink -Target $managedModelRootPath -ErrorAction Stop | Out-Null
    }
} else {
    New-Item -ItemType Junction -Path $managedModelsLink -Target $managedModelRootPath -ErrorAction Stop | Out-Null
}

# Keep the endpoint credential stable while moving ownership from the old
# external launcher into Hermes' managed runtime. Hindsight keeps the same key,
# so memory requests do not break during the migration.
if (-not (Test-Path -LiteralPath $managedApiKeyPath -PathType Leaf)) {
    $seedKey = ''
    if (Test-Path -LiteralPath $legacyApiKeyPath -PathType Leaf) {
        Set-OwnerOnlyFileAcl -Path $legacyApiKeyPath
        $seedKey = [System.IO.File]::ReadAllText($legacyApiKeyPath).Trim()
    }
    if (-not $seedKey) {
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $seedKey = 'hermes-local-' + [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    }
    Write-PrivateFileContent -Path $managedApiKeyPath -Content $seedKey
}
Set-OwnerOnlyFileAcl -Path $managedApiKeyPath
$apiKey = [System.IO.File]::ReadAllText($managedApiKeyPath).Trim()
if (-not $apiKey) { throw "Managed local-model API key is empty: $managedApiKeyPath" }

# Provision and validate the isolated runtime before changing Hermes to
# local_external. If package resolution or the fresh-process import fails, the
# existing configuration is left pointing at its current memory runtime.
if (-not $SkipHindsightInstall) {
    $uv = (Get-Command uv -ErrorAction Stop).Source
    if (-not (Test-Path -LiteralPath $hindsightPython -PathType Leaf)) {
        & $uv venv --python $HermesPython $hindsightRuntimePath
        if ($LASTEXITCODE -ne 0) { throw "Hindsight runtime creation failed with exit code $LASTEXITCODE" }
    }
    & $uv pip install --python $hindsightPython "hindsight-all==$HindsightVersion" 'mcp<2'
    if ($LASTEXITCODE -ne 0) { throw "isolated hindsight-all installation failed with exit code $LASTEXITCODE" }
    & $uv pip check --python $hindsightPython
    if ($LASTEXITCODE -ne 0) { throw "isolated Hindsight dependency check failed with exit code $LASTEXITCODE" }
    # Reproduce the former failure from a brand-new interpreter. This import
    # reaches FastMCP's server surface and catches an accidental MCP 2 upgrade
    # before the live daemon is ever restarted.
    & $hindsightPython -I -c "from importlib.metadata import version; from hindsight import HindsightEmbedded; import hindsight_embed.daemon_embed_manager; assert int(version('mcp').split('.')[0]) < 2"
    if ($LASTEXITCODE -ne 0) { throw "isolated Hindsight fresh-process import check failed with exit code $LASTEXITCODE" }
}

$ownerConfigYaml = @'
# This owner profile is intentionally local-only. A stale fallback chain from
# an earlier configuration must not send a conversation to a cloud provider
# when the loopback Qwen server is unavailable.
fallback_providers: []

model:
  default: Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P
  provider: llamacpp
  base_url: ""
  api_mode: chat_completions
  context_length: 65536
  max_tokens: 4096
  supports_vision: true
  reasoning_echo: false

# One owner model, one Hermes-managed Vulkan router, one stable endpoint. The
# alias keeps old sessions and Hindsight compatible while Desktop displays the
# exact uncensored model identity it rediscovers on every boot.
local_runtime:
  enabled: true
  backend: vulkan
  models_max: 1
  port: 8081
  detect_ports: []
  idle_unload_seconds: 60
  preset_overrides:
    Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P:
      alias: qwen38-27b-aggressive
      reasoning: "on"
      reasoning-effort: xhigh
      reasoning-budget: 2048
      reasoning-preserve: false
      reasoning-format: deepseek
      sleep-idle-seconds: 60
      image-min-tokens: 1024
      parallel: 1
      mtp-capable: true
      mmproj-asset: mmproj-Qwen3.8-27B-BF16.gguf
      spec-draft-n-max: 2
      spec-draft-p-min: 0

agent:
  # One turn may still do substantial multi-step work, but cannot spiral into
  # hundreds of model/tool round-trips. Independent tools can run in parallel.
  max_turns: 32
  run_budget_seconds: 600
  gateway_timeout: 600
  reasoning_effort: xhigh
  tool_use_enforcement: true
  execution_guidance: true
  intent_ack_continuation: true
  stall_guards: true
  task_completion_guidance: true
  parallel_tool_call_guidance: true
  image_input_mode: native
  turn_liveness:
    timeout_s: 600
    poll_s: 15

approvals:
  mode: "off"
  cron_mode: approve
  single_query_mode: approve
  unattended_mode: approve
  mcp_reload_confirm: false
  destructive_slash_confirm: false
  deny: []

tool_loop_guardrails:
  warnings_enabled: true
  hard_stop_enabled: true
  warn_after:
    exact_failure: 2
    same_tool_failure: 3
    idempotent_no_progress: 2
  hard_stop_after:
    exact_failure: 3
    same_tool_failure: 6
    idempotent_no_progress: 4
  loop_caps:
    max_web_searches: 16
    max_subagents: 8

# Conversations never expire on an idle or daily timer. They compact in place
# at the context threshold and reset only when the user explicitly asks.
session_reset:
  mode: none

# WAL supports the Desktop and Telegram processes sharing one persistent home.
# Match the existing database mode; do not attempt an online mode transition
# while another process holds a connection.
database:
  journal_mode: wal

compression:
  enabled: true
  checkpoint_required: false
  threshold: 0.75
  # Keep the full 64K server window while leaving room for a 4K answer. The
  # ratio trigger stays above the 48K absolute cap, so threshold_tokens is the
  # effective trigger instead of silently compacting at 32K.
  threshold_tokens: 48000
  target_ratio: 0.20
  tail_mode: lean
  protect_first_n: 0
  protect_last_n: 8
  min_tail_user_messages: 2
  max_attempts: 4
  proactive_prune_tokens: 24000
  proactive_prune_min_result_chars: 2000
  proactive_prune_min_reclaim_tokens: 2048
  micro_compact: false

memory:
  memory_enabled: true
  user_profile_enabled: true
  write_approval: false
  memory_char_limit: 6000
  user_char_limit: 3500
  nudge_interval: 10
  provider: hindsight

auxiliary:
  compression:
    provider: auto
    model: ""
    timeout: 600
    reasoning_effort: low
    extra_body:
      reasoning_effort: low
      chat_template_kwargs:
        enable_thinking: true
        reasoning_effort: low
        preserve_thinking: false
  background_review:
    enabled: true
    provider: auto
    model: ""
    timeout: 600
    reasoning_effort: xhigh
    max_input_tokens: 32000

delegation:
  reasoning_effort: xhigh
  max_concurrent_children: 4
  orchestrator_enabled: true

display:
  memory_notifications: verbose
'@
$configMergeScript = Join-Path $PSScriptRoot 'merge-owner-config.py'
if (-not (Test-Path -LiteralPath $configMergeScript -PathType Leaf)) {
    throw "Owner configuration merge helper was not found: $configMergeScript"
}
# The overlay contains only settings owned by this local stack. Deep-merging
# those leaves preserves plugins, _config_version, and future user settings on
# every rerun (including after an explicit `hermes gateway install`). The
# helper uses Hermes' fail-closed, atomic config writer.
$ownerOverlayPath = Join-Path $homePath ('.owner-config-' + [System.Guid]::NewGuid().ToString('N') + '.yaml.tmp')
try {
    [System.IO.File]::WriteAllText($ownerOverlayPath, $ownerConfigYaml, [System.Text.UTF8Encoding]::new($false))
    $mergeArguments = @(
        '-I', $configMergeScript, $configPath, $ownerOverlayPath,
        '--remove', 'providers.local-qwen38'
    )
    $quotedMergeArguments = @($mergeArguments | ForEach-Object {
        if ($_.Contains('"')) { throw "Unsupported quote in config merge argument." }
        '"' + $_ + '"'
    })
    $mergeProcess = Start-Process -FilePath $HermesPython -ArgumentList ($quotedMergeArguments -join ' ') -NoNewWindow -Wait -PassThru
    if ($mergeProcess.ExitCode -ne 0) {
        throw "Owner configuration merge failed with exit code $($mergeProcess.ExitCode)"
    }
} finally {
    if (Test-Path -LiteralPath $ownerOverlayPath -PathType Leaf) {
        Remove-Item -LiteralPath $ownerOverlayPath -Force
    }
}

$hindsightConfig = [ordered]@{
    # Keep the third-party daemon outside Hermes' venv. Hermes intentionally
    # ships MCP 2, while Hindsight 0.9.1's FastMCP server requires MCP <2.
    # local_external leaves Hermes with only the lightweight HTTP client, so a
    # normal `hermes update` cannot replace or invalidate the memory runtime.
    mode = 'local_external'
    api_url = "http://127.0.0.1:$HindsightPort"
    profile = $HindsightProfile
    llm_provider = 'openai_compatible'
    llm_base_url = 'http://127.0.0.1:8081/v1'
    llm_model = 'qwen38-27b-aggressive'
    bank_id = 'hermes-owner'
    bank_id_template = 'hermes-{profile}'
    bank_mission = "Be Yousef's durable personal-assistant memory. Prefer verified facts, explicit preferences, decisions, corrections, environment state, commitments, and reusable procedures. Update stale facts instead of duplicating contradictions."
    bank_retain_mission = 'Extract durable facts, preferences, decisions, corrections, successful procedures, unresolved commitments, and important project state. Skip transient chatter and secrets.'
    recall_budget = 'high'
    memory_mode = 'hybrid'
    recall_prefetch_method = 'recall'
    recall_types = 'observation'
    auto_recall = $true
    recall_sync = $true
    recall_max_tokens = 4096
    recall_max_input_chars = 4000
    auto_retain = $true
    retain_every_n_turns = 1
    retain_async = $true
    prefetch_waits_for_retain = $true
    prefetch_retain_drain_timeout = 60
    timeout = 600
    idle_timeout = 0
    port_health_grace_timeout = 120
    recall_indicator = $true
    retain_indicator = $true
}
$hindsightJson = $hindsightConfig | ConvertTo-Json -Depth 8
Write-PrivateFileContent -Path $hindsightPath -Content $hindsightJson

Remove-PrivateEnvValue -Path $envPath -Name 'LLAMA_API_KEY'
# local_external reads timeout/idle behavior from config.json, while the
# isolated daemon reads its LLM key from the protected profile env below.
# Remove the old duplicates so Hermes' own .env stores only its model key.
Remove-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_LLM_API_KEY'
Remove-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_TIMEOUT'
Remove-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_IDLE_TIMEOUT'

# The isolated daemon keeps using the existing `hermes` profile and therefore
# the same pg0://hindsight-embed-hermes database. Only its interpreter moves;
# no memory data is copied or migrated.
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_LLM_PROVIDER' -Value 'openai'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_LLM_API_KEY' -Value $apiKey
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_LLM_MODEL' -Value 'qwen38-27b-aggressive'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_LLM_BASE_URL' -Value 'http://127.0.0.1:8081/v1'
# Chat gets the sole local-model slot at xhigh. Automatic fact extraction must
# stay bounded: with the server default, an 810-character retain consumed 6,149
# output tokens, timed out twice, and occupied the slot for 336 seconds. Low is
# the least reasoning value this exact Qwen template accepts; its retain prompt
# and schema still do the extraction. One capped attempt avoids orphaned server
# work and retry amplification. Deep consolidation and reflection keep xhigh.
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_RETAIN_LLM_REASONING_EFFORT' -Value 'low'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS' -Value '4096'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_RETAIN_LLM_TIMEOUT' -Value '90'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES' -Value '0'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_RETAIN_WALL_TIMEOUT' -Value '120'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_HOST' -Value '127.0.0.1'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_PORT' -Value ([string]$HindsightPort)
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_API_LOG_LEVEL' -Value 'info'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT' -Value '0'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'PYTHONUTF8' -Value '1'
Set-PrivateEnvValue -Path $hindsightProfilePath -Name 'PYTHONIOENCODING' -Value 'utf-8'

if (-not $SkipCuaTelemetry) {
    $cua = Get-Command cua-driver -ErrorAction SilentlyContinue
    if ($cua) {
        & $cua.Source telemetry disable | Out-Null
    }
}

if (-not $SkipStartupTask) {
    # Older owner-local profiles used Task Scheduler. Some Windows builds
    # reject otherwise-valid interactive-token actions with 0xFFFD0000, so
    # remove that launcher and use the user's Startup folder instead.
    try {
        if (Get-ScheduledTask -TaskName 'HermesLocalAI' -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName 'HermesLocalAI' -Confirm:$false -ErrorAction Stop
        }
    } catch {
        Write-Warning "Could not remove the superseded HermesLocalAI task: $($_.Exception.Message)"
    }
    # A normal Hermes update refreshes only the inner gateway-service VBS and
    # leaves our gated Startup wrapper intact. An explicit `hermes gateway
    # install`, however, may register this Scheduled Task and bypass Startup.
    # Remove it before claiming owner-stack persistence; fail closed if Windows
    # will not let this user reconcile it.
    try {
        if (Get-ScheduledTask -TaskName 'Hermes_Gateway' -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName 'Hermes_Gateway' -Confirm:$false -ErrorAction Stop
        }
    } catch {
        throw "Could not remove the direct Hermes_Gateway Scheduled Task; the dependency-gated Startup path cannot be guaranteed: $($_.Exception.Message)"
    }
    # The configured home is authoritative. LOCALAPPDATA can resolve to a
    # different physical tree inside an MSIX host than it does at Windows logon.
    $installedSessionStartScript = Join-Path $homePath 'hermes-agent\scripts\start-owner-session.ps1'
    if (-not (Test-Path -LiteralPath $installedSessionStartScript -PathType Leaf)) {
        throw "Installed owner-session launcher was not found: $installedSessionStartScript"
    }
    $powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) {
        throw "Windows PowerShell was not found: $powershellExe"
    }
    $startupDir = if ($StartupDirectory) {
        [System.IO.Path]::GetFullPath($StartupDirectory)
    } else {
        [Environment]::GetFolderPath('Startup')
    }
    if (-not $startupDir) { throw 'Windows Startup folder could not be resolved.' }
    [System.IO.Directory]::CreateDirectory($startupDir) | Out-Null
    $legacyStartupLauncher = Join-Path $startupDir 'Hermes_Local_AI.vbs'
    if (Test-Path -LiteralPath $legacyStartupLauncher -PathType Leaf) {
        Remove-Item -LiteralPath $legacyStartupLauncher -Force
    }

    # One logged entry point owns logon. Keep a recoverable copy of the old
    # separate wrapper so it cannot race the new session launcher.
    $gatewayStartupLauncher = Join-Path $startupDir 'Hermes_Gateway.vbs'
    if (Test-Path -LiteralPath $gatewayStartupLauncher -PathType Leaf) {
        $startupBackup = Join-Path $homePath ('startup-backup-' + [guid]::NewGuid().ToString('N'))
        [System.IO.Directory]::CreateDirectory($startupBackup) | Out-Null
        Move-Item -LiteralPath $gatewayStartupLauncher -Destination (Join-Path $startupBackup 'Hermes_Gateway.vbs')
    }

    # Launch the packaged Hermes Desktop and ChatGPT without a console or an
    # initial foreground window. The PowerShell launcher resolves both apps at
    # run time so Start Menu and MSIX update paths can change safely.
    $desktopAppsStartupLauncher = Join-Path $startupDir 'Hermes_Desktop_Apps.vbs'
    $desktopAppsCommand = "`"$powershellExe`" -NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installedSessionStartScript`" -HermesHome `"$homePath`" -HindsightRuntimeRoot `"$hindsightRuntimePath`" -HindsightHome `"$hindsightHomePath`" -HindsightProfile `"$HindsightProfile`" -HindsightPort $HindsightPort"
    $escapedDesktopAppsCommand = $desktopAppsCommand.Replace('"', '""')
    $desktopAppsLauncherText = "Set shell = CreateObject(`"WScript.Shell`")`r`nshell.Run `"$escapedDesktopAppsCommand`", 0, False`r`n"
    [System.IO.File]::WriteAllText($desktopAppsStartupLauncher, $desktopAppsLauncherText, [System.Text.Encoding]::ASCII)
}

Write-Output "Owner-local Hermes configuration written to $homePath (managed 27B Vulkan model, 64K, bounded xhigh reasoning, one-minute VRAM release, isolated Hindsight memory)."
