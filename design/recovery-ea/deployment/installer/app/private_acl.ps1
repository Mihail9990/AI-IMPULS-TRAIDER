param(
    [Parameter(Mandatory)][string]$Directory,
    [Parameter(Mandatory)][ValidateSet('Create', 'Verify')][string]$Mode
)
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $Directory -Force
if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Expected a private local directory.'
}
$sidType = [Security.Principal.SecurityIdentifier]
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User
$allowed = @($current.Value, 'S-1-5-18', 'S-1-5-32-544') | Select-Object -Unique
if ($Mode -eq 'Create') {
    if (@(Get-ChildItem -LiteralPath $Directory -Force).Count -ne 0) {
        throw 'Creation of a private ACL requires a new empty directory.'
    }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($current)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in $allowed) {
        $sid = [Security.Principal.SecurityIdentifier]::new($identity)
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
}
$actual = Get-Acl -LiteralPath $Directory
$rules = @($actual.GetAccessRules($true, $true, $sidType))
if (-not $actual.AreAccessRulesProtected -or $rules.Count -eq 0 -or
    $actual.GetOwner($sidType).Value -notin $allowed) {
    throw 'The directory does not have a private protected ACL.'
}
$ownerFull = $false
foreach ($r in $rules) {
    if ($r.AccessControlType -ne 'Allow' -or $r.IdentityReference.Value -notin $allowed) {
        throw 'Unexpected principal or rule in the private directory.'
    }
    if ($r.IdentityReference.Value -eq $current.Value -and
        ($r.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
            [Security.AccessControl.FileSystemRights]::FullControl -and
        ($r.InheritanceFlags -band 3) -eq 3 -and [int]$r.PropagationFlags -eq 0) {
        $ownerFull = $true
    }
}
if (-not $ownerFull) { throw 'The current owner requires inheritable local access.' }
if ($Mode -eq 'Verify') {
    $keyPath = Join-Path $Directory 'recovery-profile.json'
    $keyFile = Get-Item -LiteralPath $keyPath -Force
    if ($keyFile.PSIsContainer -or ($keyFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Expected a regular local profile file.'
    }
    $keyAcl = Get-Acl -LiteralPath $keyPath
    if ($keyAcl.GetOwner($sidType).Value -notin $allowed) {
        throw 'Unexpected owner of private profile file.'
    }
    foreach ($r in $keyAcl.GetAccessRules($true, $true, $sidType)) {
        if ($r.AccessControlType -eq 'Allow' -and $r.IdentityReference.Value -notin $allowed) {
            throw 'Unexpected principal on private profile file.'
        }
    }
}
