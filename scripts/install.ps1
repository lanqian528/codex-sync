param([string]$Repo = 'lanqian528/codex-sync', [string]$Version = 'latest')
$ErrorActionPreference = 'Stop'
if ($Repo -notmatch '^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$' -or $Version -notmatch '^(latest|v[a-zA-Z0-9_.-]+)$') { throw 'Invalid repository/version' }
if (-not [Environment]::Is64BitOperatingSystem) { throw '64-bit Windows required' }
$dest = Join-Path $env:LOCALAPPDATA 'codex-sync'
$config = Join-Path $env:USERPROFILE '.config/codex-sync'
function Protect-Path([string]$Path) {
    if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse paths unsupported' }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    # Build a DACL-only descriptor. Reusing Get-Acl may carry audit/owner state
    # and make Set-Acl request SeSecurityPrivilege during an ordinary upgrade.
    $isDirectory = Test-Path -LiteralPath $Path -PathType Container
    $acl = if ($isDirectory) { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]::None
    if ($isDirectory) { $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' }
    foreach ($principal in @($sid, [Security.Principal.SecurityIdentifier]'S-1-5-18')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', $inherit, 'None', 'Allow'))
    }
    if (-not ('CodexSync.NativePermissions' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace CodexSync {
  public static class NativePermissions {
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern uint SetNamedSecurityInfoW(string name, int type, uint flags,
      IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);
    [DllImport("advapi32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool GetSecurityDescriptorDacl(IntPtr descriptor,
      out bool present, out IntPtr dacl, out bool defaulted);
    public static void SetDacl(string path, byte[] descriptor) {
      var handle = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
      try {
        bool present, defaulted; IntPtr dacl;
        if (!GetSecurityDescriptorDacl(handle.AddrOfPinnedObject(), out present, out dacl, out defaulted))
          throw new Win32Exception(Marshal.GetLastWin32Error());
        if (!present || dacl == IntPtr.Zero) throw new InvalidOperationException("Missing private DACL");
        uint error = SetNamedSecurityInfoW(path, 1, 0x80000004u,
          IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
        if (error != 0) throw new Win32Exception((int)error);
      } finally { handle.Free(); }
    }
  }
}
'@
    }
    [CodexSync.NativePermissions]::SetDacl($Path, $acl.GetSecurityDescriptorBinaryForm())
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
    # Task Scheduler may leave the hidden child alive. Match our exact install path only.
    $installedExe = Join-Path $dest 'codex-sync.exe'
    Get-CimInstance Win32_Process -Filter "Name = 'codex-sync.exe'" | Where-Object { $_.ExecutablePath -eq $installedExe } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Copy-Item -LiteralPath $binary -Destination (Join-Path $dest 'codex-sync.exe') -Force
    $connection = Join-Path $config 'connection.json'
    if (-not (Test-Path -LiteralPath $connection)) {
        Write-Host 'First confirm Pro works. Configuration will be updated directly; reopen Codex afterward to verify.'
        $codexDir = Read-Host 'Codex directory (blank = CODEX_HOME or ~/.codex)'
        if (-not $codexDir) { $codexDir = $env:CODEX_HOME }
        if (-not $codexDir) { $codexDir = Join-Path $env:USERPROFILE '.codex' }
        if (-not (Test-Path -LiteralPath $codexDir -PathType Container)) { throw 'Initialize Pro first' }
        Protect-Path $codexDir
        $codexConfig = Join-Path $codexDir 'config.toml'
        if (Test-Path -LiteralPath $codexConfig) { Protect-Path $codexConfig }
        $previousConnectionPath = $env:CODEX_SYNC_CONFIG
        try {
            $env:CODEX_SYNC_CONFIG = $connection
            & $installedExe config --codex-home $codexDir
            if ($LASTEXITCODE -ne 0) { throw 'Connection validation failed; autostart was not enabled.' }
        } finally { $env:CODEX_SYNC_CONFIG = $previousConnectionPath }
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
    # Persist for future terminals and update this terminal when installed via iex.
    $userCommandPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $userEntries = @($userCommandPath -split ';' | Where-Object { $_ })
    if (-not ($userEntries | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $dest.TrimEnd('\') })) {
        [Environment]::SetEnvironmentVariable('Path', (($userEntries + $dest) -join ';'), 'User')
    }
    if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $dest.TrimEnd('\') })) { $env:Path += ";$dest" }
    Write-Host 'Installed. Run codex-sync to view status and manage the monitor.'
    Write-Host 'Other already-open terminals may need to be reopened to load the updated PATH.'
} finally {
    # Only remove the verified temporary child created in this installer.
    $resolved = [IO.Path]::GetFullPath($tmp)
    if ($resolved.StartsWith([IO.Path]::GetFullPath($dest) + [IO.Path]::DirectorySeparatorChar)) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
