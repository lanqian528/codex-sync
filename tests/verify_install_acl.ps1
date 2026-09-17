$ErrorActionPreference='Stop'
$installer=Join-Path $PSScriptRoot '../scripts/install.ps1'
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($installer,[ref]$null,[ref]$errors)
if ($errors) { throw 'Installer syntax error' }
$definition=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Protect-Path'},$true)
if (-not $definition) { throw 'Permission function missing' }
# Evaluate only this project's permission function, not the installer actions.
Invoke-Expression $definition.Extent.Text
$binaryInstaller=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-ClientBinary'},$true)
if (-not $binaryInstaller) { throw 'Binary installation function missing' }
Invoke-Expression $binaryInstaller.Extent.Text
$temporaryBase=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temporaryRoot=Join-Path $temporaryBase ('codex-sync-acl-'+[Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    Protect-Path $temporaryRoot
    Protect-Path $temporaryRoot
    $testFile=Join-Path $temporaryRoot 'fake.json'
    [IO.File]::WriteAllText($testFile,'{"fake":true}')
    Protect-Path $testFile
    Protect-Path $testFile
    $expected=@([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,'S-1-5-18')
    foreach ($path in @($temporaryRoot,$testFile)) {
        $acl=Get-Acl -LiteralPath $path
        if (-not $acl.AreAccessRulesProtected) { throw 'ACL inheritance was not disabled' }
        foreach ($rule in $acl.Access) {
            if ($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $expected) { throw 'Unexpected ACL principal' }
        }
    }
    Write-Output 'Fresh install and repeated ACL protection passed without administrator privileges.'
    $source=Join-Path $temporaryRoot 'new.bin'
    $destination=Join-Path $temporaryRoot 'installed.bin'
    [IO.File]::WriteAllText($source,'new-version')
    [IO.File]::WriteAllText($destination,'old-version')
    $held=[IO.File]::Open($destination,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $failed=$false
    try { Install-ClientBinary $source $destination } catch { $failed=$true } finally { $held.Dispose() }
    if (-not $failed -or [IO.File]::ReadAllText($destination) -ne 'old-version') { throw 'A blocked update changed the existing binary' }
    Install-ClientBinary $source $destination
    if ([IO.File]::ReadAllText($destination) -ne 'new-version') { throw 'Atomic binary replacement failed' }
    Write-Output 'Blocked upgrades preserve the old binary; replacement succeeds after the handle is released.'
} finally {
    $resolved=[IO.Path]::GetFullPath($temporaryRoot)
    if ($resolved.StartsWith($temporaryBase.TrimEnd('\')+'\')) { Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue }
}
