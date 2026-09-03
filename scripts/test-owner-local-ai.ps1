[CmdletBinding()]
param(
    [string]$StateRoot = 'G:\LocalAI\llama.cpp',
    [int]$Port = 8081,
    [int]$ExpectedContextLength = 65536
)

$ErrorActionPreference = 'Stop'
$statePath = [System.IO.Path]::GetFullPath($StateRoot)
$keyPath = Join-Path $statePath 'server-api-key.txt'
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "Local API key file is missing: $keyPath"
}
$apiKey = [System.IO.File]::ReadAllText($keyPath).Trim()
$headers = @{ Authorization = "Bearer $apiKey"; 'Content-Type' = 'application/json' }
$baseUrl = "http://127.0.0.1:$Port"

$health = Invoke-RestMethod -Uri "$baseUrl/health" -Headers $headers -TimeoutSec 10
if ($health.status -ne 'ok') { throw 'Health check failed.' }

$props = Invoke-RestMethod -Uri "$baseUrl/props" -Headers $headers -TimeoutSec 30
$actualContextLength = [int]$props.default_generation_settings.n_ctx
if ($actualContextLength -ne $ExpectedContextLength) {
    throw "Context verification failed: expected $ExpectedContextLength, server reports $actualContextLength."
}

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
$reply = Invoke-RestMethod -Method Post -Uri "$baseUrl/v1/chat/completions" -Headers $headers -Body $body -TimeoutSec 180
$content = [string]$reply.choices[0].message.content
if ($content.Trim() -ne 'LOCAL_AI_OK') {
    throw "Unexpected local model reply: $content"
}

Write-Output "Local AI verified: health=ok, model=$modelId, context=$actualContextLength, response=LOCAL_AI_OK."
