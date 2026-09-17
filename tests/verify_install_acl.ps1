$ErrorActionPreference='Stop'
$installer=Join-Path $PSScriptRoot '../scripts/install.ps1'
$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($installer,[ref]$null,[ref]$errors)
if ($errors) { throw 'Installer syntax error' }
$definition=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Protect-Path'},$true)
if (-not $definition) { throw 'Permission function missing' }
# Evaluate only this project's permission function, not the installer actions.
Invoke-Expression $definition.Extent.Text
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
} finally {
    $resolved=[IO.Path]::GetFullPath($temporaryRoot)
    if ($resolved.StartsWith($temporaryBase.TrimEnd('\')+'\')) { Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue }
}
