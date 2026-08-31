[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    $dirty = git status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the Hermes repository."
    }
    if ($dirty) {
        throw "The working tree has uncommitted changes. Commit or stash them before syncing."
    }

    $upstreamUrl = git remote get-url upstream 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $upstreamUrl) {
        git remote add upstream https://github.com/NousResearch/hermes-agent.git
    }

    git fetch --no-tags upstream main
    if ($LASTEXITCODE -ne 0) {
        throw "Fetching upstream/main failed."
    }

    git switch main
    if ($LASTEXITCODE -ne 0) {
        throw "Switching to main failed."
    }

    git merge --no-edit upstream/main
    if ($LASTEXITCODE -ne 0) {
        throw "Upstream has a real merge conflict. Resolve it locally, then push main."
    }

    git push origin main
    if ($LASTEXITCODE -ne 0) {
        throw "The merge succeeded locally, but pushing origin/main failed."
    }

    Write-Host "Owner-first Hermes is synced with upstream/main."
}
finally {
    Pop-Location
}
