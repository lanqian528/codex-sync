param([string]$Repo = 'lanqian528/codex-sync', [string]$Version = 'latest')
$ErrorActionPreference = 'Stop'
if ($Repo -notmatch '^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$' -or $Version -notmatch '^(latest|v[a-zA-Z0-9_.-]+)$') { throw 'Invalid repository/version' }
if (-not [Environment]::Is64BitOperatingSystem) { throw '64-bit Windows required' }
$dest = Join-Path $env:LOCALAPPDATA 'codex-sync'
$config = Join-Path $env:USERPROFILE '.config/codex-sync'
function Protect-Path([string]$Path) {
    if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse paths unsupported' }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
    $inherit = [Security.AccessControl.InheritanceFlags]::None
    if (Test-Path -LiteralPath $Path -PathType Container) { $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }
    foreach ($principal in @($sid, [Security.Principal.SecurityIdentifier]'S-1-5-18')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', $inherit, 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
New-Item -ItemType Directory -Force -Path $dest,$config | Out-Null
Protect-Path $dest
Protect-Path $config
$tmp = Join-Path $dest ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    $base = "https://github.com/$Repo/releases/latest/download"
    if ($Version -ne 'latest') { $base = "https://github.com/$Repo/releases/download/$Version" }
    $asset = 'codex-sync-windows-x64.zip'
    Invoke-WebRequest "$base/$asset" -OutFile (Join-Path $tmp $asset) -UseBasicParsing
    Invoke-WebRequest "$base/$asset.sha256" -OutFile (Join-Path $tmp "$asset.sha256") -UseBasicParsing
    $expected = ((Get-Content -LiteralPath (Join-Path $tmp "$asset.sha256") -Raw) -split '\s+')[0]
    if ($expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath (Join-Path $tmp $asset) -Algorithm SHA256).Hash -ne $expected) { throw 'Checksum mismatch' }
    Expand-Archive -LiteralPath (Join-Path $tmp $asset) -DestinationPath (Join-Path $tmp 'unpacked')
    $binary = Join-Path $tmp 'unpacked/codex-sync-windows-x64/codex-sync.exe'
    & $binary --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Binary check failed' }
    $task = Get-ScheduledTask -TaskName 'CodexSync' -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName 'CodexSync'; Start-Sleep -Seconds 2 }
    Copy-Item -LiteralPath $binary -Destination (Join-Path $dest 'codex-sync.exe') -Force
    $connection = Join-Path $config 'connection.json'
    if (-not (Test-Path -LiteralPath $connection)) {
        Write-Host 'First confirm Pro works; exit Codex before synchronization.'
        $url = Read-Host 'Worker HTTPS URL'
        $username = Read-Host 'Read username'
        $secure = Read-Host 'Read password' -AsSecureString
        $codexDir = Read-Host 'Codex directory (blank = CODEX_HOME or ~/.codex)'
        if (-not $codexDir) { $codexDir = $env:CODEX_HOME }
        if (-not $codexDir) { $codexDir = Join-Path $env:USERPROFILE '.codex' }
        $password = [Net.NetworkCredential]::new('', $secure).Password
        $json = @{url=$url; username=$username; password=$password; codex_home=$codexDir} | ConvertTo-Json
        [IO.File]::WriteAllText($connection, $json, [Text.UTF8Encoding]::new($false))
        $password = $null; $json = $null
        Protect-Path $connection
        if (-not (Test-Path -LiteralPath $codexDir -PathType Container)) { throw 'Initialize Pro first' }
        Protect-Path $codexDir
        $codexConfig = Join-Path $codexDir 'config.toml'
        if (Test-Path -LiteralPath $codexConfig) { Protect-Path $codexConfig }
    }
    # wscript runs a hidden console child and waits, so Task Scheduler tracks its lifetime.
    $exe = (Join-Path $dest 'codex-sync.exe').Replace('"','""')
    $conn = $connection.Replace('"','""')
    $vbs = "Set sh = CreateObject(""WScript.Shell"")`r`nsh.Environment(""PROCESS"")(""CODEX_SYNC_CONFIG"") = ""$conn""`r`nWScript.Quit sh.Run(Chr(34) & ""$exe"" & Chr(34) & "" watch"", 0, True)`r`n"
    $launcher = Join-Path $dest 'watch.vbs'
    [IO.File]::WriteAllText($launcher, $vbs, [Text.UnicodeEncoding]::new($false,$true))
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wscript.exe" -Argument "`"$launcher`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName 'CodexSync' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'CodexSync'
    Write-Host "Installed, hidden at login. Diagnose after stopping the task: & '$dest\codex-sync.exe' sync"
} finally {
    # Only remove the verified temporary child created in this installer.
    $resolved = [IO.Path]::GetFullPath($tmp)
    if ($resolved.StartsWith([IO.Path]::GetFullPath($dest) + [IO.Path]::DirectorySeparatorChar)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
