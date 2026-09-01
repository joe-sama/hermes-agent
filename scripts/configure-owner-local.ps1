[CmdletBinding()]
param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$HermesPython = $(if ($env:HERMES_HOME) { "$env:HERMES_HOME\hermes-agent\venv\Scripts\python.exe" } else { "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" }),
    [string]$RuntimeRoot = 'G:\LocalAI\llama.cpp\b10621',
    [switch]$SkipHindsightInstall,
    [switch]$SkipStartupTask
)

$ErrorActionPreference = 'Stop'
$homePath = [System.IO.Path]::GetFullPath($HermesHome)
$runtimePath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$stateRoot = [System.IO.Path]::GetFullPath((Split-Path $runtimePath -Parent))
$apiKeyPath = Join-Path $stateRoot 'server-api-key.txt'
$configPath = Join-Path $homePath 'config.yaml'
$envPath = Join-Path $homePath '.env'
$hindsightDir = Join-Path $homePath 'hindsight'
$hindsightPath = Join-Path $hindsightDir 'config.json'

if (-not (Test-Path -LiteralPath $apiKeyPath -PathType Leaf)) {
    throw "Start the local model server once so its key exists: $apiKeyPath"
}
$apiKey = [System.IO.File]::ReadAllText($apiKeyPath).Trim()
if (-not $apiKey) { throw "Local model API key is empty: $apiKeyPath" }

[System.IO.Directory]::CreateDirectory($homePath) | Out-Null
[System.IO.Directory]::CreateDirectory($hindsightDir) | Out-Null

$configYaml = @'
providers:
  local-qwen38:
    api: http://127.0.0.1:8080/v1
    key_env: LLAMA_API_KEY
    transport: chat_completions
    default_model: qwen38-27b-aggressive
    discover_models: false
    models:
      qwen38-27b-aggressive:
        context_length: 65536
        supports_vision: true
        supports_reasoning: true
        supports_tools: true
    extra_body:
      reasoning_effort: xhigh
      chat_template_kwargs:
        enable_thinking: true
        reasoning_effort: xhigh
        preserve_thinking: true

model:
  default: qwen38-27b-aggressive
  provider: custom:local-qwen38
  base_url: http://127.0.0.1:8080/v1
  api_mode: chat_completions
  context_length: 65536
  supports_vision: true
  reasoning_echo: true

agent:
  max_turns: null
  run_budget_seconds: null
  gateway_timeout: 0
  reasoning_effort: xhigh
  tool_use_enforcement: true
  execution_guidance: true
  intent_ack_continuation: true
  stall_guards: true
  task_completion_guidance: true
  parallel_tool_call_guidance: true
  image_input_mode: native
  turn_liveness:
    timeout_s: 0
    poll_s: 15

approvals:
  mode: off
  cron_mode: approve
  single_query_mode: approve
  unattended_mode: approve
  mcp_reload_confirm: false
  destructive_slash_confirm: false
  deny: []

compression:
  enabled: true
  checkpoint_required: false
  threshold: 0.75
  threshold_tokens: 48000
  target_ratio: 0.20
  tail_mode: lean
  protect_first_n: 0
  protect_last_n: 8
  min_tail_user_messages: 2
  max_attempts: 6
  proactive_prune_tokens: 0
  micro_compact: false

memory:
  memory_enabled: true
  user_profile_enabled: true
  write_approval: false
  memory_char_limit: 6000
  user_char_limit: 3500
  nudge_interval: 3
  provider: hindsight

auxiliary:
  background_review:
    enabled: true
    provider: auto
    model: ""
    timeout: 600
    reasoning_effort: xhigh
    max_input_tokens: 48000

delegation:
  reasoning_effort: xhigh
  max_concurrent_children: 4
  orchestrator_enabled: true

display:
  memory_notifications: verbose
'@
[System.IO.File]::WriteAllText($configPath, $configYaml, [System.Text.UTF8Encoding]::new($false))

$hindsightConfig = [ordered]@{
    mode = 'local_embedded'
    llm_provider = 'openai_compatible'
    llm_base_url = 'http://127.0.0.1:8080/v1'
    llm_model = 'qwen38-27b-aggressive'
    bank_id = 'hermes-owner'
    bank_id_template = 'hermes-{profile}'
    bank_mission = "Be Yousef's durable personal-assistant memory. Prefer verified facts, explicit preferences, decisions, corrections, environment state, commitments, and reusable procedures. Update stale facts instead of duplicating contradictions."
    bank_retain_mission = 'Extract durable facts, preferences, decisions, corrections, successful procedures, unresolved commitments, and important project state. Skip transient chatter and secrets.'
    recall_budget = 'high'
    memory_mode = 'hybrid'
    recall_prefetch_method = 'reflect'
    recall_types = 'observation'
    auto_recall = $true
    recall_sync = $true
    recall_max_tokens = 4096
    recall_max_input_chars = 4000
    auto_retain = $true
    retain_every_n_turns = 1
    retain_async = $false
    prefetch_waits_for_retain = $true
    prefetch_retain_drain_timeout = 60
    timeout = 600
    idle_timeout = 0
    port_health_grace_timeout = 120
    recall_indicator = $true
    retain_indicator = $true
}
$hindsightJson = $hindsightConfig | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($hindsightPath, $hindsightJson, [System.Text.UTF8Encoding]::new($false))

function Set-PrivateEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = if (Test-Path -LiteralPath $Path) {
        [System.IO.File]::ReadAllLines($Path, [System.Text.UTF8Encoding]::new($false))
    } else { @() }
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
}

Set-PrivateEnvValue -Path $envPath -Name 'LLAMA_API_KEY' -Value $apiKey
Set-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_LLM_API_KEY' -Value $apiKey
Set-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_TIMEOUT' -Value '600'
Set-PrivateEnvValue -Path $envPath -Name 'HINDSIGHT_IDLE_TIMEOUT' -Value '0'
& icacls.exe $envPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict the Hermes .env ACL: $envPath" }
& icacls.exe $hindsightPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict the Hindsight config ACL: $hindsightPath" }

if (-not $SkipHindsightInstall) {
    if (-not (Test-Path -LiteralPath $HermesPython -PathType Leaf)) {
        throw "Hermes Python was not found: $HermesPython"
    }
    $uv = (Get-Command uv -ErrorAction Stop).Source
    & $uv pip install --python $HermesPython hindsight-all
    if ($LASTEXITCODE -ne 0) { throw "hindsight-all installation failed with exit code $LASTEXITCODE" }
}

$cua = Get-Command cua-driver -ErrorAction SilentlyContinue
if ($cua) {
    & $cua.Source telemetry disable | Out-Null
}

if (-not $SkipStartupTask) {
    $installedStartScript = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\scripts\start-owner-local-ai.ps1'
    if (-not (Test-Path -LiteralPath $installedStartScript -PathType Leaf)) {
        throw "Installed local-AI launcher was not found: $installedStartScript"
    }
    $powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) {
        throw "Windows PowerShell was not found: $powershellExe"
    }
    $actionArgs = "-NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installedStartScript`""
    $action = New-ScheduledTaskAction -Execute $powershellExe -Argument $actionArgs
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'HermesLocalAI' -Action $action -Trigger $trigger -Principal $principal -Description 'Start the private native llama.cpp server for owner-first Hermes.' -Force | Out-Null
}

Write-Output "Owner-local Hermes configuration written to $homePath (64K, xhigh reasoning, Hindsight hybrid memory)."
