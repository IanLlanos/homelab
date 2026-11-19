<#
  Weekly Windows Update with Discord status messages.

  -Mode Install  (scheduled Monday 3:00 AM)
      1. Finds pending Windows updates. None -> posts "No updates this week" and stops.
      2. Posts "Weekly update starting" with the list of updates.
      3. Downloads and installs them.
      4. If a restart is needed: waits for Plex streams to finish (Tautulli, up to MaxWaitMinutes),
         stops Docker containers and the Sonarr/Radarr services cleanly, then restarts.
         If no restart is needed: runs the health check and posts the result right away.
  -Mode After    (scheduled at logon)
      Runs only if an install run restarted the server. Restarts the containers it stopped,
      waits for apps to come back, then posts "Weekly update finished" with an Errors field.

  -DryRun   Shows what would happen (pending updates, Plex streams, what would be stopped). Changes nothing.
  -Test     Posts sample "starting" and "finished" messages to Discord. Changes nothing.
#>
param(
    [ValidateSet('Install', 'After')][string]$Mode = 'Install',
    [switch]$DryRun,
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$LogFile   = Join-Path $Root 'WeeklyUpdate.log'
$StateFile = Join-Path $Root 'state.json'
$Config    = Get-Content (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json

# ---------------------------------------------------------------- helpers
function Write-Log([string]$Msg, [string]$Level = 'INFO') {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt [int64]$Config.LogMaxBytes) {
            Move-Item $LogFile "$LogFile.old" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch {}
}

function Limit([string]$Text, [int]$Max = 1000) {
    if (-not $Text) { return '-' }
    if ($Text.Length -le $Max) { return $Text }
    return $Text.Substring(0, $Max - 20) + "`n...(see log)"
}

function Send-Discord {
    param([string]$Title, [string]$Description = '', [array]$Fields = @(), [int]$Color = 3447003, [switch]$Urgent)
    if ($DryRun) { Write-Log "DRY RUN - would post: $Title"; return }
    $embed = @{
        title       = $Title
        color       = $Color
        timestamp   = (Get-Date).ToUniversalTime().ToString('o')
        footer      = @{ text = "$env:COMPUTERNAME | $LogFile" }
    }
    if ($Description) { $embed.description = (Limit $Description 3800) }
    if ($Fields.Count) { $embed.fields = @($Fields | ForEach-Object { @{ name = $_.name; value = (Limit $_.value 1000); inline = [bool]$_.inline } }) }
    $payload = @{ username = 'Weekly Update'; embeds = @($embed) }
    if ($Urgent) {
        $payload.content = "$($Config.UrgentMention) **$Title**"
        $payload.allowed_mentions = @{ parse = @('everyone') }
    }
    $json = $payload | ConvertTo-Json -Depth 8
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod -Uri $Config.WebhookUrl -Method Post -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30 | Out-Null
        Write-Log "Discord message sent: $Title"
    } catch { Write-Log "Discord message FAILED ($Title): $($_.Exception.Message)" 'ERROR' }
}

function Send-Public([string]$Title, [string]$Text, [int]$Color = 3447003) {
    # Short, non-technical message for the general announcements channel (optional: PublicWebhookUrl)
    if (-not $Config.PublicWebhookUrl) { return }
    if ($DryRun) { Write-Log "DRY RUN - would post to announcements: $Title"; return }
    $payload = @{ username = $Config.PublicName; embeds = @(@{ title = $Title; description = $Text; color = $Color; timestamp = (Get-Date).ToUniversalTime().ToString('o') }) }
    $json = $payload | ConvertTo-Json -Depth 6
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-RestMethod -Uri $Config.PublicWebhookUrl -Method Post -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30 | Out-Null
        Write-Log "Announcement sent: $Title"
    } catch { Write-Log "Announcement FAILED ($Title): $($_.Exception.Message)" 'ERROR' }
}

function F([string]$Name, [string]$Value, [bool]$Inline = $false) { @{ name = $Name; value = $Value; inline = $Inline } }

function Save-State($s) { $s | ConvertTo-Json -Depth 8 | Set-Content -Path $StateFile -Encoding UTF8 }

function Docker {
    # Runs docker.exe without letting its stderr trip $ErrorActionPreference = 'Stop' (PowerShell 5.1)
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $Config.DockerExe @args 2>$null } finally { $ErrorActionPreference = $old }
}

# ---------------------------------------------------------------- Windows Update
function Get-PendingUpdates {
    $session  = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $result   = $searcher.Search("IsInstalled=0 and IsHidden=0")
    $list = @()
    foreach ($u in $result.Updates) { $list += $u }
    return ,$list
}

function Format-UpdateList($updates) {
    ($updates | ForEach-Object {
        $mb = [math]::Round($_.MaxDownloadSize / 1MB)
        $size = if ($mb -ge 1024) { '{0:N1} GB' -f ($mb / 1024) } elseif ($mb -gt 0) { "$mb MB" } else { '' }
        '- ' + $_.Title + $(if ($size) { "  ($size)" } else { '' })
    }) -join "`n"
}

function Install-Updates($updates) {
    $session = New-Object -ComObject Microsoft.Update.Session
    $coll = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $updates) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$coll.Add($u) }

    Write-Log "Downloading $($coll.Count) update(s)"
    $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll
    $dlr = $dl.Download()
    Write-Log "Download result code: $($dlr.ResultCode)"

    $toInstall = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $coll) { if ($u.IsDownloaded) { [void]$toInstall.Add($u) } }
    $results = @()
    $reboot = $false
    if ($toInstall.Count -gt 0) {
        Write-Log "Installing $($toInstall.Count) update(s)"
        $inst = $session.CreateUpdateInstaller(); $inst.Updates = $toInstall
        $ir = $inst.Install()
        $reboot = [bool]$ir.RebootRequired
        for ($i = 0; $i -lt $toInstall.Count; $i++) {
            $r = $ir.GetUpdateResult($i)
            $results += [pscustomobject]@{ Title = $toInstall.Item($i).Title; Code = [int]$r.ResultCode; HResult = ('0x{0:X8}' -f $r.HResult) }
        }
    }
    foreach ($u in $coll) {
        if (-not $u.IsDownloaded) { $results += [pscustomobject]@{ Title = $u.Title; Code = 4; HResult = 'download failed' } }
    }
    foreach ($r in $results) { Write-Log ("Result {0} {1}: {2}" -f $r.Code, $r.HResult, $r.Title) }
    return [pscustomobject]@{ Results = $results; RebootRequired = $reboot }
}

function Get-ResultText($results) {
    $names = @{ 2 = 'installed'; 3 = 'installed with errors'; 4 = 'FAILED'; 5 = 'aborted' }
    $ok  = @($results | Where-Object { $_.Code -eq 2 })
    $bad = @($results | Where-Object { $_.Code -ne 2 })
    $okText  = if ($ok.Count)  { ($ok  | ForEach-Object { '- ' + $_.Title }) -join "`n" } else { 'None' }
    $badText = @($bad | ForEach-Object { '- {0}: {1} ({2})' -f $_.Title, $names[$_.Code], $_.HResult })
    return [pscustomobject]@{ Installed = $okText; Errors = $badText }
}

# ---------------------------------------------------------------- Plex (Tautulli)
function Get-TautulliKey {
    $ini = Get-Content $Config.TautulliConfigIni
    $line = $ini | Where-Object { $_ -match '^\s*api_key\s*=' } | Select-Object -First 1
    if ($line) { return ($line -split '=', 2)[1].Trim() }
    throw 'api_key not found in Tautulli config.ini'
}

function Get-PlexStreams {
    $key = Get-TautulliKey
    $r = Invoke-RestMethod -Uri ("{0}/api/v2?apikey={1}&cmd=get_activity" -f $Config.TautulliUrl, $key) -TimeoutSec 30
    $sessions = @($r.response.data.sessions)
    return ,@($sessions | Where-Object { $_ } | ForEach-Object { '{0} - {1}' -f $_.friendly_name, $_.full_title })
}

function Wait-ForPlex {
    $deadline = (Get-Date).AddMinutes([int]$Config.MaxWaitMinutes)
    $posted = $false
    while ($true) {
        try { $streams = Get-PlexStreams } catch { Write-Log "Couldn't check Plex streams: $($_.Exception.Message) - continuing" 'WARN'; return 'Plex check failed, restarted without waiting' }
        if ($streams.Count -eq 0) { return $(if ($posted) { 'Waited for Plex streams to finish' } else { 'No one was watching' }) }
        Write-Log "$($streams.Count) Plex stream(s) active: $($streams -join '; ')"
        if ($DryRun) { return "Would wait for: $($streams -join '; ')" }
        if (-not $posted) {
            Send-Discord -Title 'Weekly update: waiting for Plex' -Color 16776960 `
                -Description ("Updates are installed. The restart is on hold until these finish (up to {0} min):`n{1}" -f $Config.MaxWaitMinutes, (($streams | ForEach-Object { '- ' + $_ }) -join "`n"))
            $posted = $true
        }
        if ((Get-Date) -gt $deadline) { return "Restarted after waiting $($Config.MaxWaitMinutes) min - still streaming: $($streams -join '; ')" }
        Start-Sleep -Seconds ([int]$Config.PlexPollSeconds)
    }
}

# ---------------------------------------------------------------- shutdown / health
function Stop-Apps {
    $notes = @(); $stopped = @()
    try {
        $running = @(Docker ps --format '{{.Names}}' | Where-Object { $_ })
        if ($DryRun) { $notes += "Would stop containers: $($running -join ', ')" }
        elseif ($running.Count) {
            Write-Log "Stopping containers: $($running -join ', ')"
            Docker stop --time 60 @running | Out-Null
            $stopped = $running
        }
    } catch { $notes += "Docker stop failed: $($_.Exception.Message)" }
    foreach ($svc in $Config.Services) {
        try {
            $s = Get-Service -Name $svc -ErrorAction Stop
            if ($DryRun) { $notes += "Would stop service $svc ($($s.Status))" }
            elseif ($s.Status -eq 'Running') { Write-Log "Stopping service $svc"; Stop-Service -Name $svc -Force -ErrorAction Stop }
        } catch { $notes += "Service $svc stop failed: $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ Containers = $stopped; Notes = $notes }
}

function Test-Url([string]$Url) {
    try { Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10 | Out-Null; return $true }
    catch { if ($_.Exception.Response) { return $true }; return $false }   # any HTTP answer = app is up
}

function Test-Tcp([string]$HostName, [int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $t = $c.ConnectAsync($HostName, $Port); return ($t.Wait(5000) -and $c.Connected) } catch { return $false } finally { $c.Dispose() }
}

function Invoke-HealthCheck($containers) {
    $deadline = (Get-Date).AddMinutes([int]$Config.HealthWaitMinutes)
    # Docker Desktop first
    while ((Get-Date) -lt $deadline) {
        Docker info --format '{{.ServerVersion}}' | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep 15
    }
    $problems = @()
    if ($containers) {
        $running = @(Docker ps --format '{{.Names}}')
        $missing = @($containers | Where-Object { $running -notcontains $_ })
        if ($missing.Count) {
            Write-Log "Starting containers that didn't come back: $($missing -join ', ')"
            Docker start @missing | Out-Null
        }
    }
    $checks = @($Config.HealthChecks)
    $status = @{}
    do {
        $down = @()
        foreach ($c in $checks) {
            if ($status[$c.Name]) { continue }
            $ok = $false
            if ($c.Url) { $ok = Test-Url $c.Url }
            elseif ($c.Tcp) { $hp = $c.Tcp -split ':'; $ok = Test-Tcp $hp[0] ([int]$hp[1]) }
            if ($ok) { $status[$c.Name] = $true } else { $down += $c.Name }
        }
        $runningNow = @(Docker ps --format '{{.Names}}')
        $downC = @($containers | Where-Object { $runningNow -notcontains $_ })
        if (-not $down.Count -and -not $downC.Count) { break }
        Start-Sleep 20
    } while ((Get-Date) -lt $deadline)
    foreach ($n in $down) { $problems += "$n is not responding" }
    foreach ($n in $downC) { $problems += "Container $n is not running" }
    try {
        $gh = (Docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' gluetun)
        if ($gh -and $gh -ne 'healthy') { $problems += "VPN (gluetun) is $gh" }
    } catch {}
    $up = @($checks | Where-Object { $status[$_.Name] } | ForEach-Object { $_.Name })
    $summary = 'Apps up: ' + $(if ($up.Count) { $up -join ', ' } else { 'none' })
    if ($containers) { $summary += "`nContainers running: $((@($containers).Count - $downC.Count)) of $(@($containers).Count)" }
    return [pscustomobject]@{ Summary = $summary; Problems = $problems }
}

function Send-Finished($state, $health) {
    $errors = @($state.Errors) + @($health.Problems) | Where-Object { $_ }
    $color = if ($errors.Count) { 15158332 } else { 3066993 }
    $title = if ($errors.Count) { 'Weekly update finished with problems' } else { 'Weekly update finished' }
    $timing = "Started: $($state.Started)"
    if ($state.RestartAt) { $timing += "`nRestarted: $($state.RestartAt)`nBack up: $(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
    else { $timing += "`nFinished: $(Get-Date -Format 'yyyy-MM-dd HH:mm') (no restart needed)" }
    $fields = @(
        (F 'Installed' $state.Installed),
        (F 'Timing' $timing $true),
        (F 'Plex' $state.PlexNote $true),
        (F 'Health check' $health.Summary),
        (F 'Errors' $(if ($errors.Count) { ($errors | ForEach-Object { '- ' + $_ }) -join "`n" } else { 'None' }))
    )
    Send-Discord -Title $title -Fields $fields -Color $color -Urgent:([bool]($health.Problems.Count))
}

# ---------------------------------------------------------------- modes
function Invoke-Install {
    Write-Log "########## Weekly update ($(if ($DryRun) {'DRY RUN'} else {'install'})) ##########"
    $started = Get-Date -Format 'yyyy-MM-dd HH:mm'
    $pending = Get-PendingUpdates
    Write-Log "Pending updates: $($pending.Count)"
    if ($pending.Count -eq 0 -and $DryRun) {
        Write-Log 'DRY RUN - no updates pending; checking the rest of the steps anyway'
        Write-Log ("DRY RUN - Plex: " + (Wait-ForPlex))
        (Stop-Apps).Notes | ForEach-Object { Write-Log "DRY RUN - $_" }
    }
    if ($pending.Count -eq 0) {
        Send-Discord -Title 'Weekly update: no updates this week' -Description 'Windows Update has nothing to install. No restart.' -Color 9807270
        return
    }
    $listText = Format-UpdateList $pending
    Send-Discord -Title "Weekly update starting: $($pending.Count) update(s)" -Color 3447003 -Fields @(
        (F 'Updates' $listText),
        (F 'Plan' ("Install now. If a restart is needed, wait for Plex streams to finish (up to {0} min), stop Docker containers and {1}, then restart." -f $Config.MaxWaitMinutes, ($Config.Services -join '/')))
    )
    if ($DryRun) {
        Write-Log "DRY RUN - would install:`n$listText"
        Write-Log ("DRY RUN - Plex: " + (Wait-ForPlex))
        Send-Public 'Server restarting for updates' $Config.PublicDownText
        (Stop-Apps).Notes | ForEach-Object { Write-Log "DRY RUN - $_" }
        Write-Log 'DRY RUN - would restart if an update needs it. Nothing was changed.'
        return
    }
    $state = [ordered]@{ Started = $started; Phase = 'installing'; Installed = ''; Errors = @(); PlexNote = '-'; Containers = @(); RestartAt = $null }
    Save-State $state
    $inst = Install-Updates $pending
    $rt = Get-ResultText $inst.Results
    $state.Installed = $rt.Installed
    $state.Errors = @($rt.Errors)
    $pendingReboot = $inst.RebootRequired -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
    if (-not $pendingReboot) {
        $state.Phase = 'done'; $state.PlexNote = 'No restart needed'
        Save-State $state
        Send-Finished $state (Invoke-HealthCheck @())
        return
    }
    $state.PlexNote = Wait-ForPlex
    Send-Public 'Server restarting for updates' $Config.PublicDownText 15105570
    $stop = Stop-Apps
    $state.Containers = @($stop.Containers)
    $state.Errors = @($state.Errors) + @($stop.Notes)
    $state.Phase = 'rebooting'
    $state.RestartAt = Get-Date -Format 'yyyy-MM-dd HH:mm'
    Save-State $state
    Write-Log 'Restarting now'
    Restart-Computer -Force
}

function Invoke-After {
    if (-not (Test-Path $StateFile)) { return }
    $state = Get-Content $StateFile -Raw | ConvertFrom-Json
    if ($state.Phase -ne 'rebooting') { return }
    Write-Log '########## Weekly update: after restart ##########'
    $state.Phase = 'checking'; Save-State $state
    $health = Invoke-HealthCheck @($state.Containers)
    Send-Finished $state $health
    if ($health.Problems | Where-Object { $_ -like 'Plex*' }) { Send-Public 'Server update: still working on it' $Config.PublicTroubleText 15158332 }
    else { Send-Public 'Server is back up' $Config.PublicUpText 3066993 }
    $state.Phase = 'done'; Save-State $state
}

function Invoke-Test {
    Send-Discord -Title 'TEST - Weekly update starting: 3 update(s)' -Color 3447003 -Fields @(
        (F 'Updates' "- 2026-11 Cumulative Update for Windows 11 (KB0000001)  (812 MB)`n- Security Intelligence Update for Microsoft Defender (KB2267602)  (95 MB)`n- Windows Malicious Software Removal Tool (KB890830)  (70 MB)"),
        (F 'Plan' "This is a test message. Nothing is being installed.")
    )
    Send-Discord -Title 'TEST - Weekly update finished' -Color 3066993 -Fields @(
        (F 'Installed' "- 2026-11 Cumulative Update for Windows 11 (KB0000001)`n- Security Intelligence Update for Microsoft Defender (KB2267602)"),
        (F 'Timing' "Started: 03:00`nRestarted: 03:22`nBack up: 03:27" $true),
        (F 'Plex' 'Waited for Plex streams to finish' $true),
        (F 'Health check' "Apps up: Sonarr, Radarr, Plex, Tautulli, Seerr, Prowlarr, Homebridge`nContainers running: 11 of 11"),
        (F 'Errors' '- Windows Malicious Software Removal Tool (KB890830): FAILED (0x80240022)')
    )
    Send-Public 'TEST - Server restarting for updates' $Config.PublicDownText 15105570
    Send-Public 'TEST - Server is back up' $Config.PublicUpText 3066993
}

try {
    if ($Test) { Invoke-Test }
    elseif ($Mode -eq 'After') { Invoke-After }
    else { Invoke-Install }
} catch {
    $msg = "$($_.Exception.Message)`nAt: $($_.InvocationInfo.PositionMessage)"
    Write-Log "CRASH: $msg" 'ERROR'
    Send-Discord -Title "Weekly update script crashed ($Mode)" -Color 15158332 -Urgent -Fields @(
        (F 'Errors' $msg),
        (F 'What this means' 'Updates may be partly installed. Check Windows Update and the log on the server.')
    )
    exit 1
}
