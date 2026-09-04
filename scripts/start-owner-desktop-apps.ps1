[CmdletBinding()]
param(
    [string]$HermesShortcutPath = '',
    [string]$ChatGptPackageName = 'OpenAI.Codex'
)

$ErrorActionPreference = 'Stop'

function Get-FullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [string]$BasePath = ''
    )

    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim().Trim('"'))
    if (-not [System.IO.Path]::IsPathRooted($expanded)) {
        if (-not $BasePath) {
            throw "Cannot resolve relative path without a base directory: $Path"
        }
        $expanded = Join-Path $BasePath $expanded
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    $leftPath = [System.IO.Path]::GetFullPath($Left).TrimEnd('\')
    $rightPath = [System.IO.Path]::GetFullPath($Right).TrimEnd('\')
    return [string]::Equals(
        $leftPath,
        $rightPath,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Test-DesktopAppRunning {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    $processName = [System.IO.Path]::GetFileName($ExecutablePath)
    try {
        $candidates = @(
            Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
                Where-Object { $_.Name -ieq $processName }
        )
        foreach ($candidate in $candidates) {
            if (-not $candidate.ExecutablePath) { continue }
            if (-not (Test-SamePath -Left $candidate.ExecutablePath -Right $ExecutablePath)) {
                continue
            }

            # Electron helpers use the same executable but identify themselves
            # with --type=. Only the browser process proves the app is open.
            if (-not $candidate.CommandLine -or $candidate.CommandLine -notmatch '(?i)(?:^|\s)--type(?:=|\s)') {
                return $true
            }
        }
        return $false
    } catch {
        # CIM can be unavailable during early logon. Get-Process cannot
        # distinguish Electron helpers, but treating any exact-path match as
        # alive is safer than waking/focusing an existing single-instance app.
        try {
            foreach ($candidate in @(Get-Process -Name ([System.IO.Path]::GetFileNameWithoutExtension($processName)) -ErrorAction SilentlyContinue)) {
                try {
                    if ($candidate.Path -and (Test-SamePath -Left $candidate.Path -Right $ExecutablePath)) {
                        return $true
                    }
                } catch {
                    continue
                }
            }
        } catch {
            return $false
        }
        return $false
    }
}

function Test-ArgumentFlag {
    param(
        [string]$ArgumentLine,
        [Parameter(Mandatory = $true)][string]$Flag
    )

    if (-not $ArgumentLine) { return $false }
    $pattern = '(?i)(?:^|\s)' + [regex]::Escape($Flag) + '(?=$|\s)'
    return $ArgumentLine -match $pattern
}

if (-not $HermesShortcutPath) {
    $programsDirectory = [Environment]::GetFolderPath('Programs')
    if (-not $programsDirectory) {
        throw 'The current user Start Menu Programs folder could not be resolved.'
    }
    $HermesShortcutPath = Join-Path $programsDirectory 'Hermes.lnk'
}
$shortcutPath = [System.IO.Path]::GetFullPath($HermesShortcutPath)
if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
    throw "Hermes Start Menu shortcut was not found: $shortcutPath"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if (-not $shortcut.TargetPath) {
    throw "Hermes Start Menu shortcut has no executable target: $shortcutPath"
}

$shortcutDirectory = Split-Path $shortcutPath -Parent
$hermesPath = Get-FullPath -Path ([string]$shortcut.TargetPath) -BasePath $shortcutDirectory
if (-not (Test-Path -LiteralPath $hermesPath -PathType Leaf)) {
    throw "Hermes executable referenced by the Start Menu shortcut was not found: $hermesPath"
}

$hermesWorkingDirectory = [string]$shortcut.WorkingDirectory
if ($hermesWorkingDirectory) {
    $hermesWorkingDirectory = Get-FullPath -Path $hermesWorkingDirectory -BasePath $shortcutDirectory
} else {
    $hermesWorkingDirectory = Split-Path $hermesPath -Parent
}
if (-not (Test-Path -LiteralPath $hermesWorkingDirectory -PathType Container)) {
    throw "Hermes working directory referenced by the Start Menu shortcut was not found: $hermesWorkingDirectory"
}

$hermesArguments = ([string]$shortcut.Arguments).Trim()
foreach ($requiredFlag in @('--local', '--start-hidden')) {
    if (-not (Test-ArgumentFlag -ArgumentLine $hermesArguments -Flag $requiredFlag)) {
        $hermesArguments = ($hermesArguments + ' ' + $requiredFlag).Trim()
    }
}

if (Test-DesktopAppRunning -ExecutablePath $hermesPath) {
    Write-Output "Hermes Desktop is already running: $hermesPath"
} else {
    Start-Process -FilePath $hermesPath -ArgumentList $hermesArguments -WorkingDirectory $hermesWorkingDirectory -WindowStyle Hidden | Out-Null
    Write-Output "Hermes Desktop started in the background: $hermesPath"
}

try {
    $chatGptPackage = @(
        Get-AppxPackage -Name $ChatGptPackageName -ErrorAction Stop |
            Where-Object { $_.InstallLocation } |
            Sort-Object -Property Version -Descending
    ) | Select-Object -First 1
} catch {
    throw "Could not resolve the $ChatGptPackageName package: $($_.Exception.Message)"
}
if (-not $chatGptPackage) {
    throw "The $ChatGptPackageName package is not installed for the current user."
}

$chatGptPath = Join-Path ([string]$chatGptPackage.InstallLocation) 'app\ChatGPT.exe'
$chatGptPath = [System.IO.Path]::GetFullPath($chatGptPath)
if (-not (Test-Path -LiteralPath $chatGptPath -PathType Leaf)) {
    throw "ChatGPT.exe was not found in the installed $ChatGptPackageName package: $chatGptPath"
}

if (Test-DesktopAppRunning -ExecutablePath $chatGptPath) {
    Write-Output "ChatGPT is already running: $chatGptPath"
} else {
    $backgroundVariable = 'CODEX_ELECTRON_START_IN_BACKGROUND'
    $previousBackgroundValue = Get-Item -LiteralPath "Env:$backgroundVariable" -ErrorAction SilentlyContinue
    try {
        # Start-Process inherits this short-lived launcher's environment. The
        # value is restored immediately and is never written to the user or
        # machine environment, so only the new ChatGPT child receives it.
        Set-Item -LiteralPath "Env:$backgroundVariable" -Value '1'
        Start-Process -FilePath $chatGptPath -WorkingDirectory (Split-Path $chatGptPath -Parent) -WindowStyle Hidden | Out-Null
    } finally {
        if ($null -ne $previousBackgroundValue) {
            Set-Item -LiteralPath "Env:$backgroundVariable" -Value ([string]$previousBackgroundValue.Value)
        } else {
            Remove-Item -LiteralPath "Env:$backgroundVariable" -ErrorAction SilentlyContinue
        }
    }
    Write-Output "ChatGPT started in the background: $chatGptPath"
}
