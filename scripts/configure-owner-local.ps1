[CmdletBinding()]
param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$HermesPython = $(if ($env:HERMES_HOME) { "$env:HERMES_HOME\hermes-agent\venv\Scripts\python.exe" } else { "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" }),
    [string]$RuntimeRoot = 'G:\LocalAI\llama.cpp\b10621',
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
$runtimePath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$stateRoot = [System.IO.Path]::GetFullPath((Split-Path $runtimePath -Parent))
$apiKeyPath = Join-Path $stateRoot 'server-api-key.txt'
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
$icaclsExe = Join-Path $env:SystemRoot 'System32\icacls.exe'
$aclPrincipal = '*' + [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

if ($HindsightProfile -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Invalid Hindsight profile name: $HindsightProfile"
}
if ((Split-Path $hindsightHomePath -Leaf) -ne '.hindsight') {
    throw "HindsightHome must name a .hindsight directory: $hindsightHomePath"
}

function Invoke-Icacls {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    # The canonical test runner intentionally strips PATHEXT. Invoking a native
    # executable in a PowerShell pipeline then classifies it as a document on
    # Windows PowerShell 5.1. Start it directly and use its process exit code.
    $quotedArguments = @($Arguments | ForEach-Object {
        if ($_.Contains('"')) { throw "Unsupported quote in icacls argument." }
        '"' + $_ + '"'
    })
    $process = Start-Process -FilePath $icaclsExe -ArgumentList ($quotedArguments -join ' ') -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw $FailureMessage }
}

function Protect-PrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    [System.IO.Directory]::CreateDirectory($Path) | Out-Null
    # Apply the inheritable ACE to the directory itself only. Applying an
    # (OI)(CI) ACE recursively writes an inherit-only ACE onto regular files,
    # which leaves those files with no effective access. Existing children are
    # reset separately so they inherit a real file/directory ACE from this root.
    Invoke-Icacls -Arguments @($Path, '/inheritance:r', '/grant:r', "${aclPrincipal}:(OI)(CI)(F)") -FailureMessage "Could not restrict private directory ACL: $Path"
    $hasChildren = @(Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1).Count -gt 0
    if ($hasChildren) {
        $childrenPattern = Join-Path $Path '*'
        Invoke-Icacls -Arguments @($childrenPattern, '/reset', '/T', '/C') -FailureMessage "Could not reset child ACLs under private directory: $Path"
    }
}

function Protect-PrivateFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        [System.IO.File]::WriteAllBytes($Path, [byte[]]@())
    }
    # Remove inherited grants before any secret bytes are written. Repeating
    # the operation after a write is harmless and verifies that the DACL was
    # not replaced by an editor or migration helper.
    Invoke-Icacls -Arguments @($Path, '/inheritance:r', '/grant:r', "${aclPrincipal}:(F)") -FailureMessage "Could not restrict private file ACL: $Path"
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

if (-not (Test-Path -LiteralPath $apiKeyPath -PathType Leaf)) {
    throw "Start the local model server once so its key exists: $apiKeyPath"
}
$apiKey = [System.IO.File]::ReadAllText($apiKeyPath).Trim()
if (-not $apiKey) { throw "Local model API key is empty: $apiKeyPath" }
if (-not (Test-Path -LiteralPath $HermesPython -PathType Leaf)) {
    throw "Hermes Python was not found: $HermesPython"
}

[System.IO.Directory]::CreateDirectory($homePath) | Out-Null
[System.IO.Directory]::CreateDirectory($hindsightDir) | Out-Null
Protect-PrivateDirectory -Path $hindsightProfileDir
# Keep pg0's executable installation untouched. Only the per-profile database
# instances carry owner memory and need this inheritable private DACL.
Protect-PrivateDirectory -Path $pg0InstancesPath

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
providers:
  local-qwen38:
    api: http://127.0.0.1:8081/v1
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
  base_url: http://127.0.0.1:8081/v1
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
  mode: "off"
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
    $mergeArguments = @('-I', $configMergeScript, $configPath, $ownerOverlayPath)
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

Set-PrivateEnvValue -Path $envPath -Name 'LLAMA_API_KEY' -Value $apiKey
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
    $installedStartScript = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\scripts\start-owner-local-ai.ps1'
    $installedGatewayStartScript = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\scripts\start-owner-gateway.ps1'
    if (-not (Test-Path -LiteralPath $installedStartScript -PathType Leaf)) {
        throw "Installed local-AI launcher was not found: $installedStartScript"
    }
    if (-not (Test-Path -LiteralPath $installedGatewayStartScript -PathType Leaf)) {
        throw "Installed owner-gateway launcher was not found: $installedGatewayStartScript"
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
    $startupLauncher = Join-Path $startupDir 'Hermes_Local_AI.vbs'
    $command = "`"$powershellExe`" -NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installedStartScript`""
    $escapedCommand = $command.Replace('"', '""')
    $launcherText = "Set shell = CreateObject(`"WScript.Shell`")`r`nshell.Run `"$escapedCommand`", 0, False`r`n"
    [System.IO.File]::WriteAllText($startupLauncher, $launcherText, [System.Text.Encoding]::ASCII)

    # Keep the normal Hermes gateway service launcher, but delay invoking it
    # until the local model and isolated Hindsight are both healthy. Windows
    # runs Startup entries independently, so filename order alone cannot
    # prevent an immediate post-logon message from missing memory.
    $gatewayStartupLauncher = Join-Path $startupDir 'Hermes_Gateway.vbs'
    $gatewayCommand = "`"$powershellExe`" -NoProfile -NoLogo -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$installedGatewayStartScript`""
    $escapedGatewayCommand = $gatewayCommand.Replace('"', '""')
    $gatewayLauncherText = "Set shell = CreateObject(`"WScript.Shell`")`r`nshell.Run `"$escapedGatewayCommand`", 0, False`r`n"
    [System.IO.File]::WriteAllText($gatewayStartupLauncher, $gatewayLauncherText, [System.Text.Encoding]::ASCII)
}

Write-Output "Owner-local Hermes configuration written to $homePath (64K, xhigh reasoning, isolated Hindsight hybrid memory)."
