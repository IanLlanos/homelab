<#
  Turns Transmission's turtle mode (alternate speed limits) ON while anyone is watching Plex,
  and OFF when nobody is. Stateless: every run checks Tautulli for active streams and sets
  Transmission to match, so it's safe to run as often as you like.

  Called by Tautulli (Script notification agent) on playback start/stop/pause/resume,
  and by a scheduled task every 10 minutes as a safety net.

  -Status   Show streams and Transmission's current mode, change nothing.
  -Force on|off   Set turtle mode manually (for testing).
#>
param([switch]$Status, [ValidateSet('on','off')][string]$Force)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Config = Get-Content (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
$LogFile = Join-Path $Root 'TransmissionTurtle.log'

function Write-Log([string]$Msg, [string]$Level = 'INFO') {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 524288) { Move-Item $LogFile "$LogFile.old" -Force }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
    } catch {}
}

# ---- Tautulli: who is watching (paused streams don't count; they use no bandwidth)
function Get-ActiveStreams {
    $line = Get-Content $Config.TautulliConfigIni | Where-Object { $_ -match '^\s*api_key\s*=' } | Select-Object -First 1
    $key = ($line -split '=', 2)[1].Trim()
    $r = Invoke-RestMethod -Uri ("{0}/api/v2?apikey={1}&cmd=get_activity" -f $Config.TautulliUrl, $key) -TimeoutSec 20
    return ,@(@($r.response.data.sessions) | Where-Object { $_ -and $_.state -ne 'paused' })
}

# ---- Transmission RPC (credentials read from the compose file at runtime, never logged)
$script:SessionId = $null
function Get-TransmissionCred {
    $compose = Get-Content $Config.TransmissionCompose
    $u = (($compose | Where-Object { $_ -match '^\s*-\s*USER=' } | Select-Object -First 1) -split '=', 2)[1].Trim()
    $p = (($compose | Where-Object { $_ -match '^\s*-\s*PASS=' } | Select-Object -First 1) -split '=', 2)[1].Trim()
    $pair = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${u}:${p}"))
    return "Basic $pair"
}
function Invoke-Transmission([hashtable]$Body) {
    $auth = Get-TransmissionCred
    $json = $Body | ConvertTo-Json -Depth 5
    for ($i = 0; $i -lt 2; $i++) {
        $h = @{ Authorization = $auth }
        if ($script:SessionId) { $h['X-Transmission-Session-Id'] = $script:SessionId }
        try { return Invoke-RestMethod -Uri $Config.TransmissionRpcUrl -Method Post -Headers $h -ContentType 'application/json' -Body $json -TimeoutSec 20 }
        catch {
            $resp = $_.Exception.Response
            if ($resp -and [int]$resp.StatusCode -eq 409) { $script:SessionId = $resp.Headers['X-Transmission-Session-Id']; continue }
            throw
        }
    }
    throw 'Transmission RPC: could not get a session id'
}
function Get-Turtle { [bool](Invoke-Transmission @{ method = 'session-get'; arguments = @{ fields = @('alt-speed-enabled') } }).arguments.'alt-speed-enabled' }
function Set-Turtle([bool]$On) {
    $args2 = @{ 'alt-speed-enabled' = $On }
    if ($Config.TurtleDownKBps) { $args2['alt-speed-down'] = [int]$Config.TurtleDownKBps }
    if ($Config.TurtleUpKBps)   { $args2['alt-speed-up']   = [int]$Config.TurtleUpKBps }
    [void](Invoke-Transmission @{ method = 'session-set'; arguments = $args2 })
}

try {
    if ($Force) { Set-Turtle ($Force -eq 'on'); Write-Log "Manual: turtle mode $Force"; return }
    Start-Sleep -Seconds ([int]$Config.SettleSeconds)   # let a just-stopped stream drop off Tautulli's list
    $streams = Get-ActiveStreams
    $want = $streams.Count -gt 0
    $now = Get-Turtle
    $who = ($streams | ForEach-Object { '{0} ({1})' -f $_.friendly_name, $_.full_title }) -join '; '
    if ($Status) { Write-Log ("Status: {0} active stream(s){1}; turtle mode is {2}" -f $streams.Count, $(if ($who) { " - $who" } else { '' }), $(if ($now) { 'ON' } else { 'OFF' })); return }
    if ($want -ne $now) {
        Set-Turtle $want
        Write-Log ("Turtle mode {0} - {1} active stream(s){2}" -f $(if ($want) { 'ON' } else { 'OFF' }), $streams.Count, $(if ($who) { ": $who" } else { '' }))
    }
} catch {
    Write-Log "Failed: $($_.Exception.Message)" 'ERROR'
    exit 1
}
