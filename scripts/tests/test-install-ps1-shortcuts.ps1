# Behavioral tests for install.ps1's Windows desktop shortcuts.
#
# The real New-DesktopShortcuts function is lifted through the PowerShell AST
# and uses the real WScript.Shell COM implementation. Destinations are confined
# to a temporary directory, so the user's Desktop and Start Menu are untouched.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts\install.ps1"

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $installScript, [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw "install.ps1 has parse errors: $($parseErrors -join '; ')"
}

$fnAst = $ast.FindAll(
    {
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq "New-DesktopShortcuts"
    }, $true
) | Select-Object -First 1
if (-not $fnAst) { throw "New-DesktopShortcuts not found in install.ps1" }
. ([scriptblock]::Create($fnAst.Extent.Text))

function Write-Success { param([string]$Message) Write-Host "OK: $Message" }
function Write-Warn { param([string]$Message) Write-Host "WARN: $Message" }

function Assert-Equal {
    param([string]$Expected, [string]$Actual, [string]$Label)
    if (-not [string]::Equals($Expected, $Actual, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label -- expected '$Expected', got '$Actual'"
    }
    Write-Host "OK: $Label"
}

function Assert-True {
    param($Condition, [string]$Label)
    if (-not $Condition) { throw $Label }
    Write-Host "OK: $Label"
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("hermes-shortcuts-" + [Guid]::NewGuid().ToString("N"))
try {
    $appDir = Join-Path $tempRoot "packed app"
    $resourcesDir = Join-Path $appDir "resources"
    New-Item -ItemType Directory -Force -Path $resourcesDir | Out-Null
    $targetExe = Join-Path $appDir "Hermes.exe"
    $iconIco = Join-Path $resourcesDir "icon.ico"
    New-Item -ItemType File -Path $targetExe, $iconIco | Out-Null

    $desktopLink = Join-Path $tempRoot "Desktop\Hermes.lnk"
    $programsLink = Join-Path $tempRoot "Programs\Hermes.lnk"
    New-DesktopShortcuts -TargetExe $targetExe -ShortcutPaths @($desktopLink, $programsLink)

    $shell = New-Object -ComObject WScript.Shell
    foreach ($linkPath in @($desktopLink, $programsLink)) {
        Assert-True (Test-Path -LiteralPath $linkPath -PathType Leaf) "$linkPath persisted as a file"
        $saved = $shell.CreateShortcut($linkPath)
        Assert-Equal $targetExe $saved.TargetPath "$linkPath targets the packed Hermes.exe directly"
        Assert-Equal $appDir $saved.WorkingDirectory "$linkPath preserves the app working directory"
        Assert-Equal "$iconIco,0" $saved.IconLocation "$linkPath preserves the Hermes icon"
    }

    Remove-Item -LiteralPath $iconIco -Force
    $fallbackLink = Join-Path $tempRoot "fallback-icon\Hermes.lnk"
    New-DesktopShortcuts -TargetExe $targetExe -ShortcutPaths @($fallbackLink)
    $fallback = $shell.CreateShortcut($fallbackLink)
    Assert-Equal "$targetExe,0" $fallback.IconLocation `
        "the executable supplies a durable icon when resources/icon.ico is absent"

    $missingError = $null
    try {
        New-DesktopShortcuts `
            -TargetExe (Join-Path $appDir "missing.exe") `
            -ShortcutPaths @(Join-Path $tempRoot "missing\Hermes.lnk")
    } catch {
        $missingError = $_.Exception.Message
    }
    Assert-True ($missingError -match "executable is missing") `
        "a missing executable fails before a dangling shortcut is written"

    # One destination is deliberately blocked by a file in the parent slot.
    # The function must still create the other destination, then fail the
    # overall desktop stage instead of silently reporting a complete install.
    $partialGood = Join-Path $tempRoot "partial-good\Hermes.lnk"
    $blockedParent = Join-Path $tempRoot "blocked-parent"
    New-Item -ItemType File -Path $blockedParent | Out-Null
    $partialError = $null
    try {
        New-DesktopShortcuts `
            -TargetExe $targetExe `
            -ShortcutPaths @($partialGood, (Join-Path $blockedParent "Hermes.lnk"))
    } catch {
        $partialError = $_.Exception.Message
    }
    Assert-True (Test-Path -LiteralPath $partialGood -PathType Leaf) `
        "one bad destination does not prevent the other shortcut from being repaired"
    Assert-True ($partialError -match "shortcut setup was incomplete") `
        "a non-persisted destination fails the desktop stage clearly"
    Assert-True ($partialError -match [regex]::Escape($targetExe)) `
        "the failure names the directly launchable executable"

    Write-Host "All Windows shortcut behavior tests passed."
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
