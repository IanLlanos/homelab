<#
.SYNOPSIS
    Weekly backup of this server's app configs into one zip:
    X:\Backups\ServerConfig\ServerConfig-YYYY-MM-DD_HHmm.zip

.DESCRIPTION
    Settings live in config.json next to this script. Log: ConfigBackup.log (capped).
    Posts a Discord alert when something goes wrong. Optionally (config.json NotifyStart /
    NotifySuccess) also posts when a run starts and when it finishes cleanly.

.PARAMETER Test
    Post a test alert to the Discord webhook and exit. Makes no backup.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Scripts\backuper\Backup-ServerConfig.ps1 -Test
#>
[CmdletBinding()]
param([switch]$Test)

Set-StrictMode -Version 1
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile    = Join-Path $ScriptDir 'ConfigBackup.log'
$ConfigFile = Join-Path $ScriptDir 'config.json'
$StagingRoot = Join-Path $ScriptDir '_staging'   # temporary, removed after every run

$script:Config            = $null
$script:Issues            = New-Object System.Collections.Generic.List[object]
$script:Results           = [ordered]@{}
$script:RunLog            = New-Object System.Collections.Generic.List[string]
$script:StoppedContainers = New-Object System.Collections.Generic.List[string]
$script:Staging           = $null
$script:FinalZip          = $null
$script:FinalZipOk        = $false
$script:FinalZipSize      = 0
$script:Aborted           = $false
$script:UrgentSent        = $false
$script:RunStart          = Get-Date
$script:FinalZipCount     = 0
$script:RetentionDeleted  = New-Object System.Collections.Generic.List[string]
$script:Crashed           = $false
$script:DockerExe         = $null
$script:DockerChecked     = $false
$script:DockerError       = $null

# ---------------------------------------------------------------- helpers

function Write-Log([string]$Message, [string]$Level = 'INFO') {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    $script:RunLog.Add($line)
    try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Limit-LogSize {
    $max = 1MB
    if ($script:Config -and $script:Config.LogMaxBytes) { $max = [int64]$script:Config.LogMaxBytes }
    try {
        if (-not (Test-Path -LiteralPath $LogFile)) { return }
        if ((Get-Item -LiteralPath $LogFile).Length -le $max) { return }
        $lines  = [System.IO.File]::ReadAllLines($LogFile)
        $target = [int64]($max * 0.75)
        $total  = 0
        $keepFrom = $lines.Count
        for ($i = $lines.Count - 1; $i -ge 0; $i--) {
            $total += [System.Text.Encoding]::UTF8.GetByteCount($lines[$i]) + 2
            if ($total -gt $target) { break }
            $keepFrom = $i
        }
        $kept = @()
        if ($keepFrom -lt $lines.Count) { $kept = $lines[$keepFrom..($lines.Count - 1)] }
        $header = '{0} [INFO] --- log trimmed to stay under {1} bytes ---' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $max
        [System.IO.File]::WriteAllLines($LogFile, [string[]](@($header) + $kept))
    } catch { }
}

function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return '{0:N2} GB' -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return '{0:N1} MB' -f ($Bytes / 1MB) }
    return '{0:N0} KB' -f ($Bytes / 1KB)
}

function Get-FolderSize([string]$Path) {
    $s = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    if ($s) { return [int64]$s } else { return [int64]0 }
}

function Add-Issue([string]$Step, [string]$Message, [switch]$Urgent) {
    $script:Issues.Add([pscustomobject]@{ Step = $Step; Message = $Message; Urgent = [bool]$Urgent })
}

function Read-Config {
    if (-not (Test-Path -LiteralPath $ConfigFile)) { throw "config.json not found at $ConfigFile" }
    $raw = Get-Content -LiteralPath $ConfigFile -Raw
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        # Keep alerting possible even when config.json is broken.
        $m = [regex]::Match($raw, 'https://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/[^"\s]+')
        if ($m.Success) { $script:Config = [pscustomobject]@{ WebhookUrl = $m.Value; UrgentMention = '@here' } }
        throw "config.json is not valid JSON: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- Discord

function Send-Discord {
    param([string]$Title, [string]$Description, [int]$Color = 15158332, [switch]$Urgent)
    $url = $null
    if ($script:Config) { $url = $script:Config.WebhookUrl }
    if (-not $url) { Write-Log "No WebhookUrl available - alert not sent: $Title" 'ERROR'; return $false }
    if ($Description.Length -gt 3800) { $Description = $Description.Substring(0, 3800) + "`n...(truncated - see ConfigBackup.log)" }
    $payload = @{
        username = 'ServerConfig Backup'
        embeds   = @(@{
            title       = $Title
            description = $Description
            color       = $Color
            timestamp   = (Get-Date).ToUniversalTime().ToString('o')
            footer      = @{ text = "$env:COMPUTERNAME | log: $LogFile" }
        })
    }
    if ($Urgent) {
        $mention = '@here'
        if ($script:Config.UrgentMention) { $mention = $script:Config.UrgentMention }
        $payload.content = "$mention **URGENT:** $Title"
        $payload.allowed_mentions = @{ parse = @('everyone') }
    }
    $json = $payload | ConvertTo-Json -Depth 6
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30 | Out-Null
        Write-Log "Discord alert sent: $Title"
        return $true
    } catch {
        Write-Log "Discord alert FAILED to send ($Title): $($_.Exception.Message)" 'ERROR'
        return $false
    }
}

function Get-BackupStatusText {
    if ($script:FinalZip -and $script:FinalZipOk) {
        $failed = @($script:Results.Keys | Where-Object { $script:Results[$_] -eq 'FAILED' })
        if ($failed.Count -gt 0) {
            return "**Yes, but incomplete:** ``$($script:FinalZip)`` ($(Format-Size $script:FinalZipSize)). Missing or failed: $($failed -join ', ')."
        }
        return "**Yes:** ``$($script:FinalZip)`` ($(Format-Size $script:FinalZipSize))."
    }
    if ($script:Aborted) { return '**No.** The run stopped before making a backup. Older backups on X: were not touched.' }
    return '**No.** No usable backup zip was created this run. Older backups on X: were not touched.'
}

# ---------------------------------------------------------------- process runner

function ConvertTo-ArgString([string[]]$ArgList) {
    $out = foreach ($a in $ArgList) {
        if ($a -eq '') { '""' }
        elseif ($a -match '[\s"]') { '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
        else { $a }
    }
    return ($out -join ' ')
}

function Invoke-Native {
    param([string]$FilePath, [string[]]$ArgList = @(), [string]$RawArgs = $null, [int]$TimeoutSec = 300)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    if ($RawArgs) { $psi.Arguments = $RawArgs } else { $psi.Arguments = ConvertTo-ArgString $ArgList }
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        throw "$(Split-Path -Leaf $FilePath) timed out after $TimeoutSec s"
    }
    $p.WaitForExit()
    return [pscustomobject]@{ ExitCode = $p.ExitCode; StdOut = $outTask.Result.Trim(); StdErr = $errTask.Result.Trim() }
}

function Invoke-Docker([string[]]$ArgList, [int]$TimeoutSec = 300) {
    $r = Invoke-Native -FilePath $script:DockerExe -ArgList $ArgList -TimeoutSec $TimeoutSec
    if ($r.ExitCode -ne 0) { throw "docker $($ArgList[0]) failed (exit $($r.ExitCode)): $($r.StdErr)" }
    return $r.StdOut
}

function Test-Docker {
    if ($script:DockerChecked) { if ($script:DockerError) { throw $script:DockerError }; return }
    $script:DockerChecked = $true
    try {
        $exe = $script:Config.DockerExe
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { $exe = (Get-Command docker -ErrorAction Stop).Source }
        $script:DockerExe = $exe
        $v = Invoke-Docker -ArgList @('version', '--format', '{{.Server.Version}}') -TimeoutSec 90
        Write-Log "Docker engine $v is available"
    } catch {
        $script:DockerError = "Docker is not available: $($_.Exception.Message)"
        throw $script:DockerError
    }
}

# ---------------------------------------------------------------- step runner

function Invoke-Step([string]$Name, [scriptblock]$Action, $Arg = $null) {
    Write-Log "=== ${Name}: starting"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $Action $Arg
        $script:Results[$Name] = 'OK'
        Write-Log ("=== ${Name}: OK ({0} s)" -f [int]$sw.Elapsed.TotalSeconds)
    } catch {
        $msg = $_.Exception.Message
        $script:Results[$Name] = 'FAILED'
        Add-Issue $Name $msg
        Write-Log "=== ${Name}: FAILED - $msg" 'ERROR'
    }
}

function New-StagingDir([string]$Relative) {
    $d = Join-Path $script:Staging $Relative
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

function Wait-FileStable([string]$Path, [int]$TimeoutSec = 300) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $last = -1
    while ((Get-Date) -lt $deadline) {
        $len = (Get-Item -LiteralPath $Path).Length
        if ($len -gt 0 -and $len -eq $last) { return }
        $last = $len
        Start-Sleep -Seconds 5
    }
    throw "File was still changing after $TimeoutSec s: $Path"
}

# ---------------------------------------------------------------- app steps

function Backup-Arr([string]$App) {
    $c = $script:Config.$App
    [xml]$x = Get-Content -LiteralPath $c.ConfigXml -Raw
    $key = $x.Config.ApiKey
    if (-not $key) { throw "No ApiKey found in $($c.ConfigXml)" }
    $h   = @{ 'X-Api-Key' = $key }
    $api = '{0}/api/{1}' -f $c.Url.TrimEnd('/'), $c.ApiVersion

    $before = @{}
    Get-ChildItem -LiteralPath $c.BackupDir -Recurse -File -Filter '*.zip' -ErrorAction SilentlyContinue |
        ForEach-Object { $before[$_.FullName] = $true }

    $cmd = Invoke-RestMethod -Uri "$api/command" -Method Post -Headers $h -ContentType 'application/json' `
        -Body '{"name":"Backup"}' -TimeoutSec 60
    Write-Log "${App}: backup command queued (id $($cmd.id))"

    $deadline = (Get-Date).AddSeconds([int]$c.TimeoutSec)
    do {
        Start-Sleep -Seconds 3
        $st = Invoke-RestMethod -Uri "$api/command/$($cmd.id)" -Headers $h -TimeoutSec 60
        if (@('failed', 'aborted', 'cancelled', 'orphaned') -contains $st.status) {
            throw "$App backup command ended with status '$($st.status)'. $($st.message)"
        }
    } until ($st.status -eq 'completed' -or (Get-Date) -gt $deadline)
    if ($st.status -ne 'completed') { throw "$App backup did not finish within $($c.TimeoutSec) s (status '$($st.status)')" }

    $new = Get-ChildItem -LiteralPath $c.BackupDir -Recurse -File -Filter '*.zip' |
        Where-Object { -not $before.ContainsKey($_.FullName) } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $new) { throw "$App said the backup completed, but no new zip appeared in $($c.BackupDir)" }
    Wait-FileStable $new.FullName 120
    $new = Get-Item -LiteralPath $new.FullName
    if ($new.Length -le 0) { throw "$App backup zip is empty: $($new.FullName)" }

    $dest = New-StagingDir $App
    Copy-Item -LiteralPath $new.FullName -Destination $dest
    Write-Log "${App}: copied $($new.Name) ($(Format-Size $new.Length))"
}

function Backup-Tautulli {
    $c   = $script:Config.Tautulli
    $ini = Get-Content -LiteralPath (Join-Path $c.DataDir 'config.ini')
    function Get-IniValue([string]$Name) {
        $line = $ini | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -First 1
        if (-not $line) { return $null }
        return (($line -replace "^\s*$Name\s*=\s*", '').Trim().Trim('"'))
    }
    $key = Get-IniValue 'api_key'
    if (-not $key) { throw 'No api_key found in Tautulli config.ini' }
    $dir = Get-IniValue 'backup_dir'
    if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { $dir = Join-Path $c.DataDir 'backups' }

    $before = @{}
    Get-ChildItem -LiteralPath $dir -File | ForEach-Object { $before[$_.Name] = $true }

    foreach ($cmdName in 'backup_db', 'backup_config') {
        $uri = '{0}/api/v2?apikey={1}&cmd={2}' -f $c.Url.TrimEnd('/'), $key, $cmdName
        $r = Invoke-RestMethod -Uri $uri -TimeoutSec ([int]$c.TimeoutSec)
        if ($r.response.result -ne 'success') { throw "Tautulli $cmdName returned '$($r.response.result)': $($r.response.message)" }
        Write-Log "Tautulli: $cmdName OK"
    }

    $new = @(Get-ChildItem -LiteralPath $dir -File | Where-Object { -not $before.ContainsKey($_.Name) })
    $db  = $new | Where-Object { $_.Extension -eq '.db' }  | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $cfg = $new | Where-Object { $_.Extension -eq '.ini' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $db)  { throw "Tautulli reported success, but no new .db backup appeared in $dir" }
    if (-not $cfg) { throw "Tautulli reported success, but no new .ini backup appeared in $dir" }

    $dest = New-StagingDir 'Tautulli'
    foreach ($f in @($db, $cfg)) {
        Wait-FileStable $f.FullName 600
        Copy-Item -LiteralPath $f.FullName -Destination $dest
        Write-Log "Tautulli: copied $($f.Name) ($(Format-Size (Get-Item -LiteralPath $f.FullName).Length))"
    }
}

function Backup-Plex {
    $c    = $script:Config.Plex
    $dest = New-StagingDir 'Plex'

    $regFile = Join-Path $dest 'PlexMediaServer-Preferences.reg'
    $r = Invoke-Native -FilePath (Join-Path $env:SystemRoot 'System32\reg.exe') -ArgList @('export', $c.RegistryKey, $regFile, '/y') -TimeoutSec 60
    if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $regFile)) { throw "Registry export of '$($c.RegistryKey)' failed: $($r.StdErr)" }
    Write-Log "Plex: exported registry preferences ($(Format-Size (Get-Item -LiteralPath $regFile).Length))"

    $cutoff = (Get-Date).AddMinutes(-[int]$c.SkipIfModifiedWithinMinutes)
    $files  = Get-ChildItem -LiteralPath $c.DatabaseDir -File |
        Where-Object { $_.Name -match '^com\.plexapp\.plugins\.library\.(blobs\.)?db-\d{4}-\d{2}-\d{2}$' }
    $sets = $files | Group-Object { $_.Name -replace '^.*\.db-', '' } | Sort-Object Name -Descending

    $chosen = $null
    foreach ($s in $sets) {
        $main  = $s.Group | Where-Object { $_.Name -like 'com.plexapp.plugins.library.db-*' }
        $blobs = $s.Group | Where-Object { $_.Name -like 'com.plexapp.plugins.library.blobs.db-*' }
        if (-not $main -or -not $blobs) { Write-Log "Plex: backup set $($s.Name) is incomplete - skipping it" 'WARN'; continue }
        if ($s.Group | Where-Object { $_.LastWriteTime -gt $cutoff }) {
            Write-Log "Plex: backup set $($s.Name) changed in the last $($c.SkipIfModifiedWithinMinutes) min (Plex may still be writing it) - using an older set" 'WARN'
            continue
        }
        $chosen = $s
        break
    }
    if (-not $chosen) {
        throw "No complete Plex automatic database backup found in $($c.DatabaseDir). The registry preferences were still saved."
    }
    foreach ($f in $chosen.Group) {
        Copy-Item -LiteralPath $f.FullName -Destination $dest
        Write-Log "Plex: copied $($f.Name) ($(Format-Size $f.Length))"
    }
    $age = ((Get-Date) - [datetime]::ParseExact($chosen.Name, 'yyyy-MM-dd', $null)).TotalDays
    if ($age -gt [double]$c.MaxBackupAgeDays) {
        throw ("The newest usable Plex database backup is {0} days old ({1}), so Plex's automatic backups may have stopped. That set was still included." -f [int]$age, $chosen.Name)
    }
}

function Start-AndVerifyContainer([string]$Name) {
    $state = 'unknown'
    $lastErr = ''
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-Docker -ArgList @('start', $Name) -TimeoutSec 120 | Out-Null } catch { $lastErr = $_.Exception.Message }
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Seconds 2
            try { $state = Invoke-Docker -ArgList @('inspect', '-f', '{{.State.Status}}', $Name) -TimeoutSec 30 }
            catch { $state = "unknown ($($_.Exception.Message))" }
        } until ($state -eq 'running' -or (Get-Date) -gt $deadline)
        if ($state -eq 'running') {
            [void]$script:StoppedContainers.Remove($Name)
            Write-Log "${Name}: running again"
            return
        }
        Write-Log "${Name}: not running after start attempt $i (state: $state) $lastErr" 'WARN'
    }
    $msg = "Container '$Name' was stopped for the backup and did NOT come back up (state: $state). $lastErr Start it by hand: docker start $Name"
    Write-Log $msg 'ERROR'
    Add-Issue "Container restart: $Name" $msg -Urgent
    $sent = Send-Discord -Title "Container '$Name' is DOWN after the config backup" -Urgent -Color 10038562 `
        -Description "$msg`n`n**Usable backup created:** not known yet - the run is still going. A summary follows."
    if ($sent) { $script:UrgentSent = $true }
}

function Backup-ContainerVolume($Spec) {
    Test-Docker
    $name = $Spec.Container
    $wasRunning = (Invoke-Docker -ArgList @('inspect', '-f', '{{.State.Running}}', $name) -TimeoutSec 60) -eq 'true'
    $dest = Join-Path (New-StagingDir 'DockerVolumes') $name
    $copyErr = $null
    try {
        if ($wasRunning) {
            Write-Log "${name}: stopping"
            $script:StoppedContainers.Add($name)
            Invoke-Docker -ArgList @('stop', '-t', '30', $name) -TimeoutSec 120 | Out-Null
        } else {
            Write-Log "${name}: was not running - copying without stopping, and it will be left stopped" 'WARN'
        }
        Invoke-Docker -ArgList @('cp', "${name}:$($Spec.Path)", $dest) -TimeoutSec 900 | Out-Null
    } catch {
        $copyErr = $_.Exception.Message
    } finally {
        if ($wasRunning) { Start-AndVerifyContainer $name }
    }
    if ($copyErr) { throw $copyErr }
    if (-not (Test-Path -LiteralPath $dest)) { throw "docker cp reported success but $dest does not exist" }
    $size = Get-FolderSize $dest
    if ($size -le 0) { throw "docker cp of ${name}:$($Spec.Path) produced no files" }
    Write-Log "${name}: copied $($Spec.Path) ($(Format-Size $size))"
}

function Backup-FileSource($Src) {
    if (-not (Test-Path -LiteralPath $Src.Path)) { throw "Folder not found: $($Src.Path)" }
    $root     = (Get-Item -LiteralPath $Src.Path).FullName.TrimEnd('\')
    $dest     = Join-Path (New-StagingDir 'Files') $Src.Name
    $maxBytes = [int64]([double]$Src.MaxFileMB * 1MB)
    $excludes = @($Src.ExcludePaths | Where-Object { $_ } | ForEach-Object { $_.Trim('\') })
    $copied = 0; $bytes = [int64]0; $skipped = @()

    $gciErr = $null
    $all = Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue -ErrorVariable gciErr
    foreach ($f in $all) {
        $rel = $f.FullName.Substring($root.Length + 1)
        $isExcluded = $false
        foreach ($ex in $excludes) { if ($rel -like "$ex\*") { $isExcluded = $true; break } }
        if ($isExcluded) { continue }
        if ($f.Length -gt $maxBytes) { $skipped += "$rel ($(Format-Size $f.Length))"; continue }
        $target = Join-Path $dest $rel
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $target -Force
        $copied++; $bytes += $f.Length
    }
    foreach ($s in $skipped) { Write-Log "$($Src.Name): skipped large non-config file $s" 'WARN' }
    Write-Log "$($Src.Name): copied $copied files ($(Format-Size $bytes))"
    if ($gciErr -and $gciErr.Count -gt 0) {
        throw "Copied $copied files, but could not read: $((@($gciErr | ForEach-Object { $_.TargetObject }) | Select-Object -First 5) -join '; ')"
    }
    if ($copied -eq 0) { throw "No files found to copy in $root" }
}

function Backup-Homebridge {
    $c    = $script:Config.Homebridge
    $ssh  = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    $tarW = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path -LiteralPath $ssh))       { throw "ssh.exe not found at $ssh" }
    if (-not (Test-Path -LiteralPath $c.KeyFile)) { throw "SSH key not found: $($c.KeyFile)" }

    $dest   = New-StagingDir 'Homebridge'
    $out    = Join-Path $dest 'homebridge.tar.gz'
    $errLog = Join-Path $StagingRoot 'homebridge-ssh.err'
    $excl   = ($c.Exclude | ForEach-Object { "--exclude=$_" }) -join ' '
    $remote = "tar -czf - -C $($c.ParentDir) $excl $($c.FolderName)"
    $target = "$($c.User)@$($c.Host)"
    $sshArgs = "-n -i `"$($c.KeyFile)`" -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 $target"
    # cmd.exe redirection keeps the tar stream binary-safe (PowerShell 5.1 pipes are not).
    $cmdLine = "/d /s /c `"`"$ssh`" $sshArgs `"$remote`" > `"$out`" 2> `"$errLog`"`""

    $ok = $false; $lastMsg = ''
    for ($i = 1; $i -le [int]$c.Retries; $i++) {
        Remove-Item -LiteralPath $out, $errLog -Force -ErrorAction SilentlyContinue
        $r = Invoke-Native -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') -RawArgs $cmdLine -TimeoutSec ([int]$c.TimeoutSec)
        $errText = ''
        if (Test-Path -LiteralPath $errLog) { $errText = ((Get-Content -LiteralPath $errLog -Raw) + '').Trim() }
        $size = 0
        if (Test-Path -LiteralPath $out) { $size = (Get-Item -LiteralPath $out).Length }

        if ($r.ExitCode -eq 0 -and $size -gt 0) { $ok = $true; break }
        if ($r.ExitCode -eq 1 -and $size -gt 0 -and $errText -match 'file changed as we read it') {
            Write-Log "Homebridge: tar noted a file changed while reading (normal for the live log) - archive kept" 'WARN'
            $ok = $true; break
        }
        if ($r.ExitCode -eq 255) { $lastMsg = "SSH to $target failed: $errText" }
        else { $lastMsg = "Remote tar on $target failed (exit $($r.ExitCode)): $errText" }
        Write-Log "Homebridge: attempt $i of $($c.Retries) failed - $lastMsg" 'WARN'
        if ($i -lt [int]$c.Retries) { Start-Sleep -Seconds ([int]$c.RetryDelaySec) }
    }
    Remove-Item -LiteralPath $errLog -Force -ErrorAction SilentlyContinue
    if (-not $ok) {
        Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
        throw "$lastMsg (gave up after $($c.Retries) attempts)"
    }

    # Verify the archive is readable and contains config.json.
    $list = Invoke-Native -FilePath $tarW -ArgList @('-tzf', $out) -TimeoutSec 300
    if ($list.ExitCode -ne 0) { throw "Homebridge archive is corrupt: $($list.StdErr)" }
    $entries = $list.StdOut -split "`r?`n"
    if (-not ($entries | Where-Object { $_ -match "^(\./)?$([regex]::Escape($c.FolderName))/config\.json$" })) {
        throw "Homebridge archive was created but has no $($c.FolderName)/config.json in it"
    }
    Write-Log "Homebridge: archive OK ($(Format-Size (Get-Item -LiteralPath $out).Length), $($entries.Count) entries)"
}

# ---------------------------------------------------------------- zip, checks, retention

function Test-BackupDrive {
    $folder = $script:Config.BackupFolder
    $root   = [System.IO.Path]::GetPathRoot($folder)
    if (-not (Test-Path -LiteralPath $root)) {
        Add-Issue 'Backup drive' "Backup drive $root is missing or not connected. No backup was made."
        return $false
    }
    $free = (New-Object System.IO.DriveInfo($root)).AvailableFreeSpace
    $freeGB = [math]::Round($free / 1GB, 1)
    if ($freeGB -lt [double]$script:Config.MinFreeGB) {
        Add-Issue 'Backup drive' "Only $freeGB GB free on $root (minimum is $($script:Config.MinFreeGB) GB). No backup was made, to avoid filling the drive."
        return $false
    }
    Write-Log "Backup drive $root has $freeGB GB free"
    if (-not (Test-Path -LiteralPath $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        Write-Log "Created backup folder $folder"
    }
    return $true
}

function Get-ExistingBackups {
    Get-ChildItem -LiteralPath $script:Config.BackupFolder -File -Filter 'ServerConfig-*.zip' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^ServerConfig-\d{4}-\d{2}-\d{2}_\d{4}\.zip$' } |
        Sort-Object Name -Descending
}

function New-BackupZip([string]$Stamp) {
    $previous = Get-ExistingBackups | Select-Object -First 1

    # Put the restore guide and a manifest of this run inside the zip.
    $restore = Join-Path $ScriptDir 'RESTORE.md'
    if (Test-Path -LiteralPath $restore) { Copy-Item -LiteralPath $restore -Destination $script:Staging }
    $manifest = @("ServerConfig backup $Stamp on $env:COMPUTERNAME", '', 'Step results:')
    foreach ($k in $script:Results.Keys) { $manifest += ('  {0,-35} {1}' -f $k, $script:Results[$k]) }
    $manifest += @('', 'Run log:') + $script:RunLog
    Set-Content -LiteralPath (Join-Path $script:Staging 'MANIFEST.txt') -Value $manifest -Encoding UTF8

    $staged = Get-FolderSize $script:Staging
    Write-Log "Zipping $(Format-Size $staged) of staged files"
    $tempZip  = Join-Path $StagingRoot "ServerConfig-$Stamp.zip"
    $finalZip = Join-Path $script:Config.BackupFolder "ServerConfig-$Stamp.zip"
    if (Test-Path -LiteralPath $finalZip) { throw "$finalZip already exists - not overwriting it" }

    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    # Build entries by hand so paths use '/' (ZipFile.CreateFromDirectory in Windows PowerShell 5.1 writes '\').
    $zipOut = [System.IO.Compression.ZipFile]::Open($tempZip, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $base = $script:Staging.TrimEnd('\')
        foreach ($f in Get-ChildItem -LiteralPath $base -Recurse -File -Force) {
            $entryName = $f.FullName.Substring($base.Length + 1).Replace('\', '/')
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zipOut, $f.FullName, $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zipOut.Dispose() }

    try {
        Copy-Item -LiteralPath $tempZip -Destination $finalZip
    } catch {
        # Remove only the half-written file this run just created.
        if (Test-Path -LiteralPath $finalZip) { Remove-Item -LiteralPath $finalZip -Force -ErrorAction SilentlyContinue }
        throw "Could not write the zip to $($script:Config.BackupFolder): $($_.Exception.Message)"
    }
    $script:FinalZip = $finalZip

    # Verify: exists, not empty, same size as the local copy, and readable.
    if (-not (Test-Path -LiteralPath $finalZip)) { throw "The finished zip is missing: $finalZip" }
    $len = (Get-Item -LiteralPath $finalZip).Length
    if ($len -le 0) { throw "The finished zip is empty: $finalZip" }
    if ($len -ne (Get-Item -LiteralPath $tempZip).Length) { throw "The zip on X: is a different size from the one that was built - the copy may be damaged" }
    $zip = [System.IO.Compression.ZipFile]::OpenRead($finalZip)
    try { $count = $zip.Entries.Count } finally { $zip.Dispose() }
    if ($count -le 0) { throw "The finished zip has no files in it: $finalZip" }
    $script:FinalZipOk    = $true
    $script:FinalZipSize  = $len
    $script:FinalZipCount = $count
    Write-Log "Backup written: $finalZip ($(Format-Size $len), $count files)"

    if ($previous) {
        $pct = [double]$script:Config.ShrinkAlertPercent
        if ($len -lt ($previous.Length * $pct / 100)) {
            Add-Issue 'Size check' ("The new backup ({0}) is much smaller than the previous one, {1} ({2}). Something may be missing." -f (Format-Size $len), $previous.Name, (Format-Size $previous.Length))
        }
    }
}

function Invoke-Retention {
    $keep = [int]$script:Config.KeepCount
    if ($keep -lt 1) { throw "KeepCount must be at least 1 (is $keep)" }
    $old = @(Get-ExistingBackups | Select-Object -Skip $keep)
    foreach ($f in $old) {
        if ($f.FullName -eq $script:FinalZip) { continue }
        Remove-Item -LiteralPath $f.FullName -Force
        $script:RetentionDeleted.Add($f.Name)
        Write-Log "Retention: deleted old backup $($f.Name)"
    }
    if ($old.Count -eq 0) { Write-Log "Retention: nothing to delete (keeping newest $keep)" }
}

# ---------------------------------------------------------------- main

$exitCode = 0
$mutex = $null
$haveMutex = $false
try {
    $script:Config = Read-Config
    Limit-LogSize

    if ($Test) {
        Write-Log 'Test mode: sending a test alert'
        $sent = Send-Discord -Title 'Test alert from ServerConfig backup' -Color 3447003 `
            -Description "The webhook works. This is only a test from ``$($MyInvocation.MyCommand.Path)`` - no backup was made.`n`n**Usable backup created:** n/a (test)"
        if (-not $sent) { $exitCode = 1 }
    } else {
        $mutex = New-Object System.Threading.Mutex($false, 'Global\ServerConfigBackup')
        try { $haveMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveMutex = $true }
        if (-not $haveMutex) {
            $script:Aborted = $true
            Add-Issue 'Startup' 'Another backup run is still going, so this run was skipped.'
        } else {
            $stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
            Write-Log "########## Backup run $stamp starting ##########"
            $script:RunStart = Get-Date

            if ($script:Config.NotifyStart) {
                $inc = $script:Config.Include
                $labels = @{ ContainerVolumes = 'Seerr + Wizarr data'; Files = 'compose/config files' }
                $parts = @($inc.PSObject.Properties | Where-Object { $_.Value } | ForEach-Object {
                    if ($labels.ContainsKey($_.Name)) { $labels[$_.Name] } else { $_.Name } })
                [void](Send-Discord -Title 'ServerConfig backup started' -Color 9807270 `
                    -Description ("Run ``{0}`` on {1}.`n**Backing up:** {2}`nUsually takes about 5 minutes. Seerr and Wizarr will be stopped for a few seconds each." -f $stamp, $env:COMPUTERNAME, ($parts -join ', ')))
            }

            if (-not (Test-BackupDrive)) {
                $script:Aborted = $true
            } else {
                if (Test-Path -LiteralPath $StagingRoot) { Remove-Item -LiteralPath $StagingRoot -Recurse -Force }
                $script:Staging = Join-Path $StagingRoot "ServerConfig-$stamp"
                New-Item -ItemType Directory -Path $script:Staging -Force | Out-Null

                $inc = $script:Config.Include
                foreach ($app in 'Sonarr', 'Radarr', 'Prowlarr') {
                    if ($inc.$app) { Invoke-Step $app { param($a) Backup-Arr $a } $app } else { Write-Log "${app}: skipped (disabled in config.json)" }
                }
                if ($inc.Tautulli) { Invoke-Step 'Tautulli' { Backup-Tautulli } }
                if ($inc.Plex)     { Invoke-Step 'Plex'     { Backup-Plex } }
                if ($inc.ContainerVolumes) {
                    foreach ($v in $script:Config.ContainerVolumes) { Invoke-Step "Docker volume: $($v.Container)" { param($s) Backup-ContainerVolume $s } $v }
                }
                if ($inc.Files) {
                    foreach ($s in $script:Config.FileSources) { Invoke-Step "Files: $($s.Name)" { param($x) Backup-FileSource $x } $s }
                }
                if ($inc.Homebridge) { Invoke-Step 'Homebridge (SSH)' { Backup-Homebridge } }

                try {
                    New-BackupZip $stamp
                } catch {
                    Add-Issue 'Create zip' $_.Exception.Message
                    Write-Log "Create zip FAILED - $($_.Exception.Message)" 'ERROR'
                }

                if (-not $script:FinalZipOk) {
                    Write-Log 'Retention skipped: no good new backup' 'WARN'
                } elseif ($script:Issues.Count -gt 0) {
                    Write-Log 'Retention skipped: this backup had problems, so older backups are kept' 'WARN'
                } else {
                    try { Invoke-Retention } catch {
                        Add-Issue 'Retention cleanup' $_.Exception.Message
                        Write-Log "Retention FAILED - $($_.Exception.Message)" 'ERROR'
                    }
                }
            }
        }
    }
} catch {
    $script:Crashed = $true
    $msg = "$($_.Exception.Message) (script line $($_.InvocationInfo.ScriptLineNumber))"
    try { Write-Log "CRASH: $msg" 'ERROR' } catch { }
    Add-Issue 'Script crash' $msg
} finally {
    # Safety net: never leave a container stopped.
    foreach ($n in @($script:StoppedContainers)) {
        try { Write-Log "${n}: still stopped after the run - starting it"; Start-AndVerifyContainer $n } catch { }
    }

    if ($script:Issues.Count -gt 0) {
        $exitCode = 1
        try {
            $title = 'ServerConfig backup: {0} problem(s)' -f $script:Issues.Count
            if ($script:Crashed) { $title = 'ServerConfig backup CRASHED' }
            $lines = foreach ($i in $script:Issues) {
                $tag = ''
                if ($i.Urgent) { $tag = '[URGENT] ' }
                "- $tag**$($i.Step)**: $($i.Message)"
            }
            $desc = ($lines -join "`n") + "`n`n**Usable backup created:** " + (Get-BackupStatusText)
            $color = 15158332                                   # red
            if ($script:FinalZipOk) { $color = 15105570 }        # orange: partial
            # Ping again only if the immediate urgent alert could not be sent.
            $isUrgent = [bool]($script:Issues | Where-Object { $_.Urgent }) -and -not $script:UrgentSent
            if ($script:Issues | Where-Object { $_.Urgent }) { $title = "[URGENT] $title" }
            [void](Send-Discord -Title $title -Description $desc -Color $color -Urgent:$isUrgent)
        } catch { try { Write-Log "Could not send the summary alert: $($_.Exception.Message)" 'ERROR' } catch { } }
    } elseif (-not $Test) {
        Write-Log 'Backup finished with no problems'
        if ($script:Config.NotifySuccess -and $script:FinalZipOk) {
            try {
                $took = (Get-Date) - $script:RunStart
                $okSteps = @($script:Results.Keys).Count
                if ($script:RetentionDeleted.Count -gt 0) {
                    $ret = "deleted $($script:RetentionDeleted.Count) old backup(s): $($script:RetentionDeleted -join ', ')"
                } else {
                    $ret = "nothing to delete (keeping newest $($script:Config.KeepCount))"
                }
                $root = [System.IO.Path]::GetPathRoot($script:Config.BackupFolder)
                $freeGB = [math]::Round((New-Object System.IO.DriveInfo($root)).AvailableFreeSpace / 1GB, 1)
                $desc = ("**File:** ``{0}`` ({1}, {2} files)`n**Took:** {3} min {4} s`n**Steps:** all {5} OK`n**Retention:** {6}`n**Free on {7}** {8} GB" -f `
                    $script:FinalZip, (Format-Size $script:FinalZipSize), $script:FinalZipCount, [int][math]::Floor($took.TotalMinutes), $took.Seconds, $okSteps, $ret, $root.TrimEnd('\'), $freeGB)
                [void](Send-Discord -Title 'ServerConfig backup succeeded' -Color 3066993 -Description $desc)
            } catch { try { Write-Log "Could not send the success message: $($_.Exception.Message)" 'WARN' } catch { } }
        }
    }

    if (-not $Test -and $haveMutex) {
        try { if (Test-Path -LiteralPath $StagingRoot) { Remove-Item -LiteralPath $StagingRoot -Recurse -Force } } catch {
            Write-Log "Could not remove the temporary folder $StagingRoot : $($_.Exception.Message)" 'WARN'
        }
        Write-Log '########## Backup run finished ##########'
    }
    Limit-LogSize
    if ($mutex) { try { if ($haveMutex) { $mutex.ReleaseMutex() } } catch { }; $mutex.Dispose() }
}
exit $exitCode
