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
$icaclsExe = Join-Path $env:SystemRoot 'System32\icacls.exe'
$aclPrincipal = '*' + [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

if ($Profile -notmatch '^[A-Za-z0-9_-]+$') {
    throw "Invalid Hindsight profile name: $Profile"
}
if ((Split-Path $hindsightHomePath -Leaf) -ne '.hindsight') {
    throw "HindsightHome must name a .hindsight directory: $hindsightHomePath"
}
$hindsightUserHome = Split-Path $hindsightHomePath -Parent
$profileDirectory = Split-Path $profileEnv -Parent
$pg0Instances = Join-Path $hindsightUserHome '.pg0\instances'

function Invoke-Icacls {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )
    $quotedArguments = @($Arguments | ForEach-Object {
        if ($_.Contains('"')) { throw "Unsupported quote in icacls argument." }
        '"' + $_ + '"'
    })
    $process = Start-Process -FilePath $icaclsExe -ArgumentList ($quotedArguments -join ' ') -NoNewWindow -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw $FailureMessage }
}

function Protect-HindsightDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    [System.IO.Directory]::CreateDirectory($Path) | Out-Null
    Invoke-Icacls -Arguments @($Path, '/inheritance:r', '/grant:r', "${aclPrincipal}:(OI)(CI)(F)") -FailureMessage "Could not protect Hindsight data directory: $Path"
    $hasChildren = @(Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1).Count -gt 0
    if ($hasChildren) {
        $childrenPattern = Join-Path $Path '*'
        Invoke-Icacls -Arguments @($childrenPattern, '/reset', '/T', '/C') -FailureMessage "Could not reset child ACLs under Hindsight data directory: $Path"
    }
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
Invoke-Icacls -Arguments @($profileEnv, '/inheritance:r', '/grant:r', "${aclPrincipal}:(F)") -FailureMessage "Could not protect Hindsight profile environment: $profileEnv"

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
# isolated child at the explicitly validated home while leaving the caller's
# persistent user environment unchanged.
$env:USERPROFILE = Split-Path $hindsightHomePath -Parent
& $python -I -m hindsight_embed.cli -p $Profile daemon start
if ($LASTEXITCODE -ne 0) {
    throw "Isolated Hindsight launcher failed with exit code $LASTEXITCODE."
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
