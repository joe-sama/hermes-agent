[CmdletBinding()]
param(
    [string]$RuntimeRoot = 'G:\LocalAI\hindsight-runtime',
    [string]$HindsightHome = "$env:USERPROFILE\.hindsight",
    [string]$Profile = 'hermes',
    [ValidateRange(1024, 65535)]
    [int]$Port = 9177,
    [ValidateRange(10, 600)]
    [int]$StartupTimeoutSeconds = 240
)

$ErrorActionPreference = 'Stop'
$runtimePath = [System.IO.Path]::GetFullPath($RuntimeRoot)
$hindsightHomePath = [System.IO.Path]::GetFullPath($HindsightHome)
$python = Join-Path $runtimePath 'Scripts\python.exe'
$profileEnv = Join-Path (Join-Path $hindsightHomePath 'profiles') "$Profile.env"
$healthUrl = "http://127.0.0.1:$Port/health"
$aclHelpers = Join-Path $PSScriptRoot 'windows-owner-acl.ps1'

if (-not (Test-Path -LiteralPath $aclHelpers -PathType Leaf)) {
    throw "Owner-only Windows ACL helpers were not found: $aclHelpers"
}
. $aclHelpers

if ($Profile -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Invalid Hindsight profile name: $Profile"
}
if ((Split-Path $hindsightHomePath -Leaf) -ne '.hindsight') {
    throw "HindsightHome must name a .hindsight directory: $hindsightHomePath"
}
$hindsightUserHome = Split-Path $hindsightHomePath -Parent
$profileDirectory = Split-Path $profileEnv -Parent
$pg0Instances = Join-Path $hindsightUserHome '.pg0\instances'

function Protect-HindsightDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    Set-OwnerOnlyDirectoryTreeAcl -Path $Path
}

foreach ($privateDirectory in @($profileDirectory, $pg0Instances)) {
    Protect-HindsightDirectory -Path $privateDirectory
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Isolated Hindsight runtime was not found: $python"
}
if (-not (Test-Path -LiteralPath $profileEnv -PathType Leaf)) {
    throw "Hindsight profile was not found: $profileEnv"
}
Set-OwnerOnlyFileAcl -Path $profileEnv

$configuredPort = $null
foreach ($line in [System.IO.File]::ReadAllLines($profileEnv, [System.Text.UTF8Encoding]::new($false))) {
    if ($line.StartsWith('HINDSIGHT_API_PORT=', [System.StringComparison]::Ordinal)) {
        $configuredPort = $line.Substring('HINDSIGHT_API_PORT='.Length).Trim()
        break
    }
}
if ($configuredPort -ne [string]$Port) {
    throw "Hindsight profile port '$configuredPort' does not match requested port $Port."
}

function Get-HindsightHealth {
    try {
        $result = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        # Hindsight 0.9 returns database="connected". Older/newer builds may
        # expose the same state as database.status; accept both wire shapes.
        $databaseStatus = if ($result.database -is [string]) {
            $result.database
        } else {
            $result.database.status
        }
        if ($result.status -eq 'healthy' -and $databaseStatus -eq 'connected') {
            return $result
        }
    } catch {
        # Connection refusal is expected while the native daemon initializes.
    }
    return $null
}

function Test-PathInsideRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )
    try {
        $candidatePath = [System.IO.Path]::GetFullPath($Candidate)
        $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    } catch {
        return $false
    }
    return $candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith(
            $rootPath + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )
}

function Assert-IsolatedRuntimeListener {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if (-not $listeners) {
        throw "Hindsight passed health but no listener was found on port $Port."
    }
    $unsafe = @($listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') })
    if ($unsafe) {
        throw "Hindsight port $Port is exposed beyond loopback."
    }

    # A healthy response alone cannot distinguish the isolated daemon from a
    # still-running copy launched by Hermes' old venv. Prove that every socket
    # owner (or a live ancestor) executes from the configured runtime before
    # accepting it. Failure to inspect the process fails closed.
    $ownerProcessIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerProcessId in $ownerProcessIds) {
        $currentProcessId = [uint32]$ownerProcessId
        $visited = New-Object 'System.Collections.Generic.HashSet[uint32]'
        $runtimeProcess = $null
        while ($currentProcessId -gt 0 -and $visited.Add($currentProcessId)) {
            $processInfo = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $currentProcessId" -ErrorAction SilentlyContinue
            if (-not $processInfo) { break }
            if ($processInfo.ExecutablePath -and (Test-PathInsideRuntime -Candidate $processInfo.ExecutablePath -Root $runtimePath)) {
                $runtimeProcess = $processInfo
                break
            }
            $currentProcessId = [uint32]$processInfo.ParentProcessId
        }
        if (-not $runtimeProcess) {
            throw "Healthy Hindsight on port $Port is owned by PID $ownerProcessId, which does not belong to isolated runtime $runtimePath. Stop the old daemon before migration."
        }
    }
}

$health = Get-HindsightHealth
if ($health) {
    Assert-IsolatedRuntimeListener
    Write-Output "Isolated Hindsight ready on $healthUrl."
    return
}

# Never hand an occupied, unhealthy port to Hindsight's stale-process recovery:
# it cannot safely prove that an arbitrary listener belongs to this runtime.
$occupied = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($occupied) {
    throw "Port $Port is occupied but does not serve healthy Hindsight; refusing to replace that process."
}

# hindsight-embed resolves profiles and pg0 state from Path.home(). Point the
# isolated child at the explicitly validated home. Python chooses its standard
# stream encoding before hindsight loads the profile env, so seed UTF-8 in the
# launcher's environment as well; otherwise a Unicode diagnostic can trigger a
# cp1252 logging traceback on Windows even though the memory operation succeeds.
$previousUserProfile = $env:USERPROFILE
$previousPythonIoEncoding = $env:PYTHONIOENCODING
$previousPythonUtf8 = $env:PYTHONUTF8
$launchExitCode = 1
try {
    $env:USERPROFILE = Split-Path $hindsightHomePath -Parent
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    & $python -I -m hindsight_embed.cli -p $Profile daemon start
    $launchExitCode = $LASTEXITCODE
} finally {
    $env:USERPROFILE = $previousUserProfile
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
    $env:PYTHONUTF8 = $previousPythonUtf8
}
if ($launchExitCode -ne 0) {
    throw "Isolated Hindsight launcher failed with exit code $launchExitCode."
}

$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
do {
    $health = Get-HindsightHealth
    if ($health) {
        Assert-IsolatedRuntimeListener
        Write-Output "Isolated Hindsight ready on $healthUrl."
        return
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)

throw "Isolated Hindsight did not become healthy within $StartupTimeoutSeconds seconds. See $profileEnv and the adjacent profile log."
