[CmdletBinding()]
param(
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    [int]$Port = 8081,
    [int]$ExpectedContextLength = 65536,
    [switch]$VerifyIdleUnload,
    [ValidateRange(1, 86400)]
    [int]$ExpectedIdleSeconds = 60
)

$ErrorActionPreference = 'Stop'
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$keyPath = [System.IO.Path]::Combine($statePath, 'server-api-key.txt')
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "Local API key file is missing: $keyPath"
}
$apiKey = [System.IO.File]::ReadAllText($keyPath).Trim()
$headers = @{ Authorization = "Bearer $apiKey"; 'Content-Type' = 'application/json' }
$baseUrl = "http://127.0.0.1:$Port"

$health = Invoke-RestMethod -Uri "$baseUrl/health" -Headers $headers -TimeoutSec 10
if ($health.status -ne 'ok') { throw 'Health check failed.' }

$models = Invoke-RestMethod -Uri "$baseUrl/v1/models" -Headers $headers -TimeoutSec 30
$modelId = [string]$models.data[0].id
if (-not $modelId) { throw 'No model was reported by the server.' }

$body = @{
    model = $modelId
    messages = @(@{ role = 'user'; content = 'Reply with exactly LOCAL_AI_OK.' })
    temperature = 0
    max_tokens = 128
    # xhigh is this exact Qwen chat template's highest accepted tier.
    reasoning_effort = 'xhigh'
} | ConvertTo-Json -Depth 8
$generationTimer = [Diagnostics.Stopwatch]::StartNew()
$reply = Invoke-RestMethod -Method Post -Uri "$baseUrl/v1/chat/completions" -Headers $headers -Body $body -TimeoutSec 180
$generationTimer.Stop()
$content = [string]$reply.choices[0].message.content
if ($content.Trim() -ne 'LOCAL_AI_OK') {
    throw "Unexpected local model reply: $content"
}

# The router intentionally unloads the model after the configured idle period.
# Bare /props describes the router itself (n_ctx=0); select the child model
# after the request has exercised the real lazy-load path.
$encodedModelId = [System.Uri]::EscapeDataString($modelId)
$props = Invoke-RestMethod -Uri "$baseUrl/props?model=$encodedModelId" -Headers $headers -TimeoutSec 30
$actualContextLength = [int]$props.default_generation_settings.n_ctx
if ($actualContextLength -ne $ExpectedContextLength) {
    throw "Context verification failed: expected $ExpectedContextLength, server reports $actualContextLength."
}

Write-Output "Local AI verified: health=ok, model=$modelId, context=$actualContextLength, response=LOCAL_AI_OK."
Write-Output ('Generation elapsed: {0:N1}s (includes loading if the model was asleep).' -f $generationTimer.Elapsed.TotalSeconds)

if ($VerifyIdleUnload) {
    # /models inspects router residency without waking the selected model.
    # Do not poll /props here: that endpoint can load the child being tested.
    $idleTimer = [Diagnostics.Stopwatch]::StartNew()
    $observedLoaded = $false
    $unloaded = $false
    do {
        $residency = Invoke-RestMethod -Uri "$baseUrl/models" -Headers $headers -TimeoutSec 10
        $modelState = @($residency.data | Where-Object { $_.id -ceq $modelId })
        if ($modelState.Count -ne 1) { throw 'The selected model disappeared from the router catalog.' }
        $state = [string]$modelState[0].status.value
        if ($state -in @('loaded', 'ready')) { $observedLoaded = $true }
        # Native llama.cpp sleep frees weights/KV while keeping a small worker
        # alive. Router eject instead reports "unloaded" and removes the worker.
        if ($state -in @('sleeping', 'unloaded')) { $unloaded = $true; break }
        Start-Sleep -Seconds 2
    } while ($idleTimer.Elapsed.TotalSeconds -lt ($ExpectedIdleSeconds + 30))
    $idleTimer.Stop()
    if (-not $observedLoaded) { throw 'Did not observe the loaded model before the idle test.' }
    if (-not $unloaded) { throw "The model did not unload within $($ExpectedIdleSeconds + 30) seconds." }
    if ($idleTimer.Elapsed.TotalSeconds -lt [Math]::Max(0, $ExpectedIdleSeconds - 10)) {
        throw 'The model unloaded unexpectedly early; cannot verify the configured idle period.'
    }
    Write-Output ('Idle VRAM release verified: {0:N1}s (state={1}); the model remains in the catalog for automatic reload.' -f $idleTimer.Elapsed.TotalSeconds, $state)
}
