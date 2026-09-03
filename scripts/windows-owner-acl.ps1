# Shared owner-only Windows ACL primitives for the local model and memory stack.
# Callers dot-source this file; it intentionally performs no work on import.
# Do not replace these with `icacls /grant:r`: that command replaces grants for
# only the named SID, so unrelated explicit ACEs survive on managed volumes.

function Set-ExactFileSystemAcl {
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.FileSystemInfo]$Item,
        [Parameter(Mandatory = $true)]
        [System.Security.AccessControl.FileSystemSecurity]$Security
    )

    $securityType = $Security.GetType()
    $instanceMethod = $Item.GetType().GetMethod(
        'SetAccessControl',
        [Type[]]@($securityType)
    )
    if ($null -ne $instanceMethod) {
        # Windows PowerShell 5.1 / .NET Framework.
        [void]$instanceMethod.Invoke($Item, [object[]]@($Security))
        return
    }

    # PowerShell 7 exposes SetAccessControl as a .NET extension method.
    $extensionsType = 'System.IO.FileSystemAclExtensions' -as [type]
    if ($null -eq $extensionsType) {
        throw "Could not locate the Windows filesystem ACL API: $($Item.FullName)"
    }
    $extensionMethod = $extensionsType.GetMethod(
        'SetAccessControl',
        [Type[]]@($Item.GetType(), $securityType)
    )
    if ($null -eq $extensionMethod) {
        throw "Could not locate SetAccessControl for: $($Item.FullName)"
    }
    [void]$extensionMethod.Invoke($null, [object[]]@($Item, $Security))
}

function Set-OwnerOnlyFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Owner-only ACL target is not a file: $Path"
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = [System.Security.AccessControl.FileSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $security.SetAccessRule($rule)
    Set-ExactFileSystemAcl -Item ([System.IO.FileInfo]::new($Path)) -Security $security
}

function Set-OwnerOnlyDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    [System.IO.Directory]::CreateDirectory($Path) | Out-Null
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $security = [System.Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    [System.Security.AccessControl.InheritanceFlags]$inheritanceFlags =
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritanceFlags,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $security.SetAccessRule($rule)
    Set-ExactFileSystemAcl -Item ([System.IO.DirectoryInfo]::new($Path)) -Security $security
}

function Set-OwnerOnlyDirectoryTreeAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    [System.IO.Directory]::CreateDirectory($Path) | Out-Null
    $root = [System.IO.DirectoryInfo]::new($Path)
    $pending = New-Object 'System.Collections.Generic.Queue[System.IO.DirectoryInfo]'
    $items = New-Object 'System.Collections.Generic.List[System.IO.FileSystemInfo]'
    $pending.Enqueue($root)

    # Inventory the tree without asking a recursive filesystem API to walk it.
    # icacls /T follows directory junctions, so a stale child could otherwise
    # make an owner-only reset change ACLs outside the requested data root.
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        if (($directory.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to protect a directory tree containing a reparse point: $($directory.FullName)"
        }
        $items.Add($directory)
        foreach ($child in $directory.GetFileSystemInfos()) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to protect a directory tree containing a reparse point: $($child.FullName)"
            }
            if ($child -is [System.IO.DirectoryInfo]) {
                $pending.Enqueue($child)
            } else {
                $items.Add($child)
            }
        }
    }

    # The breadth-first inventory keeps every parent ahead of its children.
    # Protect the root with one inheritable owner ACE, then reset each existing
    # child to an empty, unprotected DACL so Windows derives that same ACE from
    # its parent. Recheck attributes at action time rather than trusting only
    # the inventory pass.
    Set-OwnerOnlyDirectoryAcl -Path $root.FullName
    foreach ($item in $items | Select-Object -Skip 1) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to protect a directory tree containing a reparse point: $($item.FullName)"
        }
        if ($item -is [System.IO.DirectoryInfo]) {
            $security = [System.Security.AccessControl.DirectorySecurity]::new()
        } elseif ($item -is [System.IO.FileInfo]) {
            $security = [System.Security.AccessControl.FileSecurity]::new()
        } else {
            throw "Unsupported filesystem item in owner-only directory tree: $($item.FullName)"
        }
        $security.SetAccessRuleProtection($false, $false)
        Set-ExactFileSystemAcl -Item $item -Security $security
    }
}
