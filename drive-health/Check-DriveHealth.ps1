<#
.SYNOPSIS
    SMART drive health monitor with Discord alerts.

.DESCRIPTION
    Scans every physical disk (/dev/pd0..N) with smartctl -j -a (read-only), maps
    each disk to its drive letter(s), compares against state.json (keyed by serial)
    and posts to Discord only when something changes for the worse.

    Only read-only smartctl options are used (-j, -a, --scan).

.PARAMETER Test
    Post a test message to Discord and exit.

.PARAMETER Report
    Post a summary of all drives to Discord (does not alert or update state).

.PARAMETER DryRun
    Print what would be posted instead of posting, and do not write state.json.

.NOTES
    Exit codes: 0 = OK, 1 = alerts posted, 2 = error.
    Must run elevated (admin or SYSTEM) for smartctl to open /dev/pdN.
#>
[CmdletBinding()]
param(
    [switch]$Test,
    [switch]$Report,
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
$Here       = $PSScriptRoot
$ConfigPath = Join-Path $Here 'config.json'
$StatePath  = Join-Path $Here 'state.json'
$LogPath    = Join-Path $Here 'DriveHealth.log'
$script:MaxLogBytes = 1MB

# ---------------------------------------------------------------- logging
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        if ((Test-Path $LogPath) -and ((Get-Item $LogPath).Length -ge $script:MaxLogBytes)) {
            # Cap: keep the newest half of the lines.
            $lines = @(Get-Content $LogPath)
            $keep  = $lines[[int]($lines.Count / 2)..($lines.Count - 1)]
            Set-Content -Path $LogPath -Value (@('--- log trimmed (size cap) ---') + $keep) -Encoding UTF8
        }
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    } catch { }
    Write-Host $line
}

# ---------------------------------------------------------------- config
if (-not (Test-Path $ConfigPath)) { Write-Log "config.json not found at $ConfigPath" 'ERROR'; exit 2 }
try {
    $Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
} catch {
    Write-Log "config.json is not valid JSON: $($_.Exception.Message)" 'ERROR'; exit 2
}
if ($Config.MaxLogBytes) { $script:MaxLogBytes = [int64]$Config.MaxLogBytes }

$Smartctl    = $Config.SmartctlPath
$Exclude     = @($Config.ExcludeSerials  | Where-Object { $_ } | ForEach-Object { $_.Trim().ToUpper() })
$Priority    = @($Config.PriorityLetters | Where-Object { $_ } | ForEach-Object { $_.Trim().ToUpper() })
$HddTempC    = 55
if ($Config.Thresholds -and $Config.Thresholds.HddTempC) { $HddTempC = [int]$Config.Thresholds.HddTempC }
$LowFreePct  = 5
if ($Config.Thresholds -and $null -ne $Config.Thresholds.LowFreePct) { $LowFreePct = [double]$Config.Thresholds.LowFreePct }
$NvmeWearPct = 80
if ($Config.Thresholds -and $null -ne $Config.Thresholds.NvmeWearPct) { $NvmeWearPct = [int]$Config.Thresholds.NvmeWearPct }

# Emoji built from code points so this file stays plain ASCII (PS 5.1 misreads BOM-less UTF-8).
function U([int[]]$cps) { -join ($cps | ForEach-Object { [char]::ConvertFromUtf32($_) }) }
$EM = @{
    Ok      = U 0x2705           # white check mark
    Warn    = U 0x26A0, 0xFE0F   # warning
    Bad     = U 0x1F6D1          # stop sign
    Siren   = U 0x1F6A8          # rotating light
    Chart   = U 0x1F4CA          # bar chart
    Star    = U 0x2B50           # star (priority drive)
    Temp    = U 0x1F321, 0xFE0F  # thermometer
    Age     = U 0x23F1, 0xFE0F   # stopwatch
    Realloc = U 0x1F9F1          # brick
    Pending = U 0x23F3           # hourglass
    Unc     = U 0x274C           # cross mark
    Crc     = U 0x1F50C          # plug
    Crit    = U 0x2757           # exclamation
    Media   = U 0x1F4A5          # collision
    Wear    = U 0x1F4C9          # chart decreasing
    Space   = U 0x1F4BE          # floppy disk
}
$HostName    = $env:COMPUTERNAME
$DiscordUser = 'Drive Health'
if ($Config.DiscordUsername) { $DiscordUser = $Config.DiscordUsername }

# Windows PowerShell 5.1 does not enable TLS 1.2 by default.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ---------------------------------------------------------------- discord
function Send-Discord {
    param([string[]]$Lines)
    # Discord limit is 2000 chars per message; split on line boundaries.
    $chunks = New-Object System.Collections.Generic.List[string]
    $cur = ''
    foreach ($l in $Lines) {
        if ($cur -and (($cur.Length + $l.Length + 1) -gt 1900)) { $chunks.Add($cur); $cur = '' }
        $cur += $l + "`n"
    }
    if ($cur) { $chunks.Add($cur) }

    foreach ($c in $chunks) {
        if ($DryRun) { Write-Host "----- DRY RUN: would post -----`n$c-------------------------------"; continue }
        $body = @{ username = $DiscordUser; content = $c } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $Config.DiscordWebhookUrl -Method Post `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ErrorAction Stop | Out-Null
        Start-Sleep -Milliseconds 1100   # stay under webhook rate limit
    }
}

# ---------------------------------------------------------------- helpers
function Fmt($v) { if ($null -eq $v -or "$v" -eq '') { 'n/a' } else { "$v" } }

function Get-Attr($j, [int]$id) {
    if (-not $j.ata_smart_attributes) { return $null }
    $a = $j.ata_smart_attributes.table | Where-Object { $_.id -eq $id } | Select-Object -First 1
    if ($a) { return [int64]$a.raw.value }
    return $null
}

function Get-DriveLabel($d) {
    $l = $d.Letters
    if (-not $l) { $l = "(no letter, disk $($d.PD))" }
    $tag = ''
    if (Test-IsPriority $d) { $tag = " $($EM.Star)" }
    '**{0}**{1} {2} `{3}`' -f $l, $tag, $d.Model, $d.Serial
}

function Test-IsPriority($d) {
    foreach ($p in $Priority) { if ("$($d.Letters)".ToUpper() -match [regex]::Escape($p)) { return $true } }
    return $false
}

function Get-SortKey($d) {
    $p = 1; if (Test-IsPriority $d) { $p = 0 }
    $l = "$($d.Letters)"; if (-not $l) { $l = 'ZZ' }
    '{0}{1}' -f $p, $l
}

function Format-Bytes([double]$b) {
    if ($b -ge 1TB) { '{0:N2} TB' -f ($b / 1TB) } else { '{0:N0} GB' -f ($b / 1GB) }
}

function Format-Pct([double]$p) { if ($p -lt 10) { '{0:N1}%' -f $p } else { '{0:N0}%' -f $p } }

function Get-SpaceInfo([string]$ltr, $vols) {
    $res = [pscustomobject]@{ Text = $null; FreePct = $null }
    if (-not $ltr) { return $res }
    $list  = @($ltr -split ' ' | Where-Object { $_ })
    $parts = @(); $min = $null
    foreach ($l in $list) {
        $v = $vols[$l]
        if (-not $v -or -not $v.Size) { continue }
        $pct = 100.0 * $v.SizeRemaining / $v.Size
        if ($null -eq $min -or $pct -lt $min) { $min = $pct }
        $txt = '{0}/{1} ({2} free)' -f (Format-Bytes $v.SizeRemaining), (Format-Bytes $v.Size), (Format-Pct $pct)
        if ($list.Count -gt 1) { $txt = "${l} $txt" }
        $parts += $txt
    }
    if ($parts.Count) { $res.Text = $parts -join '; '; $res.FreePct = [math]::Round($min, 1) }
    return $res
}

function B([string]$text, [bool]$cond) { if ($cond) { "**$text**" } else { $text } }

function Get-StatusLines($drives) {
    $out = @()
    foreach ($d in $drives) {
        $hot    = $d.IsHdd -and ($null -ne $d.TempC) -and ($d.TempC -ge $HddTempC)
        $low    = ($null -ne $d.FreePct) -and ($d.FreePct -lt $LowFreePct)
        $worn   = $d.IsNvme -and ($null -ne $d.NvUsed) -and ($d.NvUsed -ge $NvmeWearPct)
        $failed = (-not $d.Readable) -or ($d.Health -eq 'FAILED')
        $warn   = $hot -or $low -or $worn -or ($d.Realloc -gt 0) -or ($d.Pending -gt 0) -or ($d.OfflineUnc -gt 0) -or
                  ($d.NvCritical -gt 0) -or ($d.NvMedia -gt 0) -or ($d.Health -ne 'PASSED')
        $icon = $EM.Ok
        if ($warn)   { $icon = $EM.Warn }
        if ($failed) { $icon = $EM.Bad }

        $star = ''; if (Test-IsPriority $d) { $star = " $($EM.Star)" }
        $ltr  = $d.Letters; if (-not $ltr) { $ltr = "disk $($d.PD)" }
        $line1 = '{0} **{1}**{2} {3} `{4}` {5}' -f $icon, $ltr, $star, $d.Model, $d.Serial, $d.Interface

        $p = @()
        if ($d.Health -ne 'PASSED') { $p += "**SMART $($d.Health)**" }
        if (-not $d.Readable) {
            $p += "**unreadable** (exit $($d.ExitCode))"
        } else {
            $p += B ('{0}{1}C' -f $EM.Temp, (Fmt $d.TempC)) $hot
            if ($null -ne $d.POH) { $p += '{0}{1:N1}y' -f $EM.Age, ($d.POH / 8766) }
            if ($d.IsNvme) {
                $p += B ('{0}{1}' -f $EM.Crit,  (Fmt $d.NvCritical)) ($d.NvCritical -gt 0)
                $p += B ('{0}{1}' -f $EM.Media, (Fmt $d.NvMedia))    ($d.NvMedia -gt 0)
                $p += B ('{0}{1}%' -f $EM.Wear, (Fmt $d.NvUsed)) $worn
            } else {
                $p += B ('{0}{1}' -f $EM.Realloc, (Fmt $d.Realloc))    ($d.Realloc -gt 0)
                $p += B ('{0}{1}' -f $EM.Pending, (Fmt $d.Pending))    ($d.Pending -gt 0)
                $p += B ('{0}{1}' -f $EM.Unc,     (Fmt $d.OfflineUnc)) ($d.OfflineUnc -gt 0)
                $p += B ('{0}{1}' -f $EM.Crc,     (Fmt $d.Crc))        ($d.Crc -gt 0)
            }
        }
        if ($d.Space) { $p += B ('{0}{1}' -f $EM.Space, $d.Space) $low }

        # One string per drive so Send-Discord never splits a drive across messages.
        $out += "$line1`n> $($p -join '  ')"
    }
    return $out
}

function Get-Legend {
    '-# {0} OK  {1} check  {2} failed/unreadable  {3} priority | {4} temp  {5} power-on years  {6} reallocated  {7} pending  {8} uncorrectable  {9} CRC (cable)  {10} NVMe critical  {11} media errors  {12} NVMe wear used  {13} free space' -f `
        $EM.Ok, $EM.Warn, $EM.Bad, $EM.Star, $EM.Temp, $EM.Age, $EM.Realloc, $EM.Pending, $EM.Unc, $EM.Crc, $EM.Crit, $EM.Media, $EM.Wear, $EM.Space
}

function ConvertTo-NvCriticalText([int]$v) {
    $bits = @()
    if ($v -band 1)  { $bits += 'spare below threshold' }
    if ($v -band 2)  { $bits += 'temperature' }
    if ($v -band 4)  { $bits += 'reliability degraded' }
    if ($v -band 8)  { $bits += 'read-only mode' }
    if ($v -band 16) { $bits += 'volatile backup failed' }
    if ($bits.Count) { '0x{0:X2} ({1})' -f $v, ($bits -join ', ') } else { '0x{0:X2}' -f $v }
}

# ---------------------------------------------------------------- collection
function Get-DriveData {
    $letters = @{}
    Get-Partition -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } | ForEach-Object {
        $n = [int]$_.DiskNumber
        if (-not $letters.ContainsKey($n)) { $letters[$n] = @() }
        $letters[$n] += "$($_.DriveLetter):"
    }
    $winDisks = @{}
    Get-Disk -ErrorAction SilentlyContinue | ForEach-Object { $winDisks[[int]$_.Number] = $_ }
    $vols = @{}
    Get-Volume -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter } | ForEach-Object { $vols["$($_.DriveLetter):"] = $_ }

    $scanCount = 0
    try { $scan = & $Smartctl -j --scan | Out-String | ConvertFrom-Json; $scanCount = @($scan.devices).Count } catch { }
    $maxWin = 0
    if ($winDisks.Count) { $maxWin = ($winDisks.Keys | Measure-Object -Maximum).Maximum }
    # Windows can drop a dying disk while smartctl still reaches it, so cover both counts plus margin.
    $maxN = [Math]::Max([int]$maxWin, $scanCount - 1) + 2

    $drives = @()
    foreach ($n in 0..$maxN) {
        $raw  = & $Smartctl -j -a "/dev/pd$n" | Out-String
        $code = $LASTEXITCODE
        $j = $null
        try { $j = $raw | ConvertFrom-Json } catch { }
        $wd = $winDisks[$n]
        $winSerial = $null
        if ($wd -and $wd.SerialNumber) { $winSerial = $wd.SerialNumber.Trim().TrimEnd('.').ToUpper() }

        $ltr = ''
        if ($letters.ContainsKey($n)) { $ltr = ($letters[$n] | Sort-Object) -join ' ' }
        $sp = Get-SpaceInfo $ltr $vols

        if (-not $j -or -not $j.serial_number) {
            if (-not $wd) { continue }   # nothing at this index
            # Windows sees a disk that smartctl cannot identify at all.
            $drives += [pscustomobject]@{
                PD = $n; Letters = $ltr; Serial = $winSerial; WinSerial = $winSerial
                Model = "$($wd.FriendlyName)".Trim(); Interface = "$($wd.BusType)"
                Readable = $false; ExitCode = $code; Health = 'UNKNOWN'; IsHdd = $false; IsNvme = $false
                TempC = $null; POH = $null; Realloc = $null; Pending = $null; OfflineUnc = $null; Crc = $null
                NvCritical = $null; NvMedia = $null; NvUsed = $null; Space = $sp.Text; FreePct = $sp.FreePct
            }
            continue
        }

        $isNvme = ($j.device.protocol -eq 'NVMe')
        $iface  = 'ATA'
        if ($isNvme) { $iface = 'NVMe' } elseif ($wd) { $iface = "$($wd.BusType)" }
        $isHdd  = (-not $isNvme) -and ([int]$j.rotation_rate -gt 0)
        $nv     = $j.nvme_smart_health_information_log
        $hasData = ($isNvme -and $nv) -or ((-not $isNvme) -and $j.ata_smart_attributes)
        # exit bit 0 = cmdline error, bit 1 = device open failed. Bit 2 (some command failed)
        # only counts if no SMART data came back. Bits 3+ are disk status, not read failures.
        $readable = (($code -band 3) -eq 0) -and [bool]$hasData

        $health = 'UNKNOWN'
        if ($null -ne $j.smart_status) { if ($j.smart_status.passed) { $health = 'PASSED' } else { $health = 'FAILED' } }

        $drives += [pscustomobject]@{
            PD = $n; Letters = $ltr; Serial = "$($j.serial_number)".Trim().ToUpper(); WinSerial = $winSerial
            Model = "$($j.model_name)".Trim(); Interface = $iface
            Readable = $readable; ExitCode = $code; Health = $health; IsHdd = $isHdd; IsNvme = $isNvme
            TempC = $j.temperature.current; POH = $j.power_on_time.hours
            Realloc = (Get-Attr $j 5); Pending = (Get-Attr $j 197); OfflineUnc = (Get-Attr $j 198); Crc = (Get-Attr $j 199)
            NvCritical = $nv.critical_warning; NvMedia = $nv.media_errors; NvUsed = $nv.percentage_used
            Space = $sp.Text; FreePct = $sp.FreePct
        }
    }
    return $drives
}

function Test-Excluded($d) {
    ($Exclude -contains "$($d.Serial)".ToUpper()) -or ($d.WinSerial -and ($Exclude -contains "$($d.WinSerial)".ToUpper()))
}

# ---------------------------------------------------------------- -Test
if ($Test) {
    try {
        Send-Discord @(":white_check_mark: **Test message** from Check-DriveHealth on **$HostName** at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'). Webhook works.")
        Write-Log 'Test message posted.'
        exit 0
    } catch {
        Write-Log "Test post failed: $($_.Exception.Message)" 'ERROR'; exit 2
    }
}

# ---------------------------------------------------------------- scan
if (-not (Test-Path $Smartctl)) { Write-Log "smartctl not found at $Smartctl" 'ERROR'; exit 2 }

Write-Log "Run started (Report=$Report DryRun=$DryRun)."
$all      = @(Get-DriveData)
$excluded = @($all | Where-Object { Test-Excluded $_ })
$drives   = @($all | Where-Object { -not (Test-Excluded $_) } | Sort-Object { Get-SortKey $_ })
foreach ($e in $excluded) { Write-Log "Skipping excluded drive $($e.Model) SN $($e.Serial) (disk $($e.PD))." }
foreach ($d in $drives) {
    Write-Log ('disk {0} {1} {2} SN {3} read={4} exit={5} health={6} temp={7} realloc={8} pend={9} unc={10} crc={11} nvcrit={12} nvmedia={13} nvused={14} free={15}%' -f `
        $d.PD, (Fmt $d.Letters), $d.Model, $d.Serial, $d.Readable, $d.ExitCode, $d.Health, (Fmt $d.TempC),
        (Fmt $d.Realloc), (Fmt $d.Pending), (Fmt $d.OfflineUnc), (Fmt $d.Crc), (Fmt $d.NvCritical), (Fmt $d.NvMedia), (Fmt $d.NvUsed), (Fmt $d.FreePct))
}

if (($drives | Where-Object { $_.Readable }).Count -eq 0) {
    # Do not mark every drive missing; this is almost certainly a permissions / smartctl problem.
    Write-Log 'No readable drives found. Is the script running elevated?' 'ERROR'
    try { Send-Discord @(":x: **Check-DriveHealth on $HostName could not read SMART from any drive.** Check that it runs elevated and smartctl works.") } catch { }
    exit 2
}

# ---------------------------------------------------------------- -Report
if ($Report) {
    $lines = @("$($EM.Chart) **Drive health report - $HostName** ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))")
    $lines += Get-StatusLines $drives
    if ($excluded.Count) { $lines += "-# Excluded: $(($excluded | ForEach-Object { "$($_.Model) $($_.Serial)" }) -join ', ')" }
    $lines += Get-Legend
    try { Send-Discord $lines; if ($DryRun) { Write-Log 'Dry run: report not posted.' } else { Write-Log 'Report posted.' }; exit 0 }
    catch { Write-Log "Report post failed: $($_.Exception.Message)" 'ERROR'; exit 2 }
}

# ---------------------------------------------------------------- compare with state
$prevState = @{}
if (Test-Path $StatePath) {
    try {
        $s = Get-Content $StatePath -Raw | ConvertFrom-Json
        foreach ($p in $s.PSObject.Properties) { $prevState[$p.Name] = $p.Value }
    } catch { Write-Log "state.json unreadable, starting fresh: $($_.Exception.Message)" 'WARN' }
} else {
    Write-Log 'No state.json yet - first run, establishing baseline.'
}

$newState = @{}
$alerts   = @()   # each: @{ Drive = obj; Changes = @() }
$now      = Get-Date -Format 's'

foreach ($d in $drives) {
    $key = $d.Serial
    # A drive smartctl can't identify is keyed by its Windows serial; find its old entry that way.
    if (-not $prevState.ContainsKey($key) -and $d.WinSerial) {
        foreach ($k in $prevState.Keys) { if ($prevState[$k].WinSerial -eq $d.WinSerial) { $key = $k; break } }
    }
    $prev = $null
    if ($prevState.ContainsKey($key)) { $prev = $prevState[$key] }
    if ($prev -and $prev.Missing) { Write-Log "Drive SN $key ($($d.Model)) is back after being missing." }
    if ($prev -and -not $d.Model) { $d.Model = $prev.Model }

    $changes = @()
    $firstTxt = 'first check'

    if (-not $d.Readable) {
        if (-not $prev -or $prev.Readable -ne $false) {
            $old = 'readable'; if (-not $prev) { $old = $firstTxt }
            $changes += "SMART unreadable: $old -> unreadable (smartctl exit $($d.ExitCode))"
        }
    }

    if ($d.Health -ne 'PASSED' -and $d.Health -ne 'UNKNOWN') {
        if (-not $prev -or $prev.Health -ne $d.Health) {
            $old = $firstTxt; if ($prev) { $old = Fmt $prev.Health }
            $changes += "SMART health: $old -> $($d.Health)"
        }
    }

    if ($d.Readable) {
        foreach ($c in @(@('Realloc','Reallocated sectors'), @('Pending','Pending sectors'), @('OfflineUnc','Offline uncorrectable'))) {
            $new = $d.($c[0])
            if ($null -eq $new -or $new -le 0) { continue }
            $old = $null; if ($prev) { $old = $prev.($c[0]) }
            if ($null -eq $old -or $new -gt $old) {
                $oldTxt = $firstTxt; if ($null -ne $old) { $oldTxt = "$old" }
                $changes += "$($c[1]): $oldTxt -> $new"
            }
        }

        if ($null -ne $d.Crc -and $prev -and $null -ne $prev.Crc -and $d.Crc -gt $prev.Crc) {
            $changes += "UDMA CRC errors: $($prev.Crc) -> $($d.Crc) (likely a cable/connection issue, not the disk itself)"
        }

        if ($d.IsHdd -and $null -ne $d.TempC -and $d.TempC -ge $HddTempC) {
            if (-not $prev -or $null -eq $prev.TempC -or $prev.TempC -lt $HddTempC) {
                $old = $firstTxt; if ($prev -and $null -ne $prev.TempC) { $old = "$($prev.TempC)C" }
                $changes += "Temperature: $old -> $($d.TempC)C (threshold ${HddTempC}C)"
            }
        }

        if ($d.IsNvme) {
            if ($null -ne $d.NvCritical -and $d.NvCritical -ne 0) {
                if (-not $prev -or $prev.NvCritical -ne $d.NvCritical) {
                    $old = $firstTxt; if ($prev -and $null -ne $prev.NvCritical) { $old = ConvertTo-NvCriticalText $prev.NvCritical }
                    $changes += "NVMe critical warning: $old -> $(ConvertTo-NvCriticalText $d.NvCritical)"
                }
            }
            if ($null -ne $d.NvMedia -and $d.NvMedia -gt 0) {
                $old = $null; if ($prev) { $old = $prev.NvMedia }
                if ($null -eq $old -or $d.NvMedia -gt $old) {
                    $oldTxt = $firstTxt; if ($null -ne $old) { $oldTxt = "$old" }
                    $changes += "NVMe media errors: $oldTxt -> $($d.NvMedia)"
                }
            }
            if ($null -ne $d.NvUsed -and $d.NvUsed -ge $NvmeWearPct) {
                if (-not $prev -or $null -eq $prev.NvUsed -or $prev.NvUsed -lt $NvmeWearPct) {
                    $old = $firstTxt; if ($prev -and $null -ne $prev.NvUsed) { $old = "$($prev.NvUsed)%" }
                    $changes += "NVMe wear (rated life used): $old -> $($d.NvUsed)% (threshold $NvmeWearPct%) - plan a replacement"
                }
            }
        }
    }

    # Free space works off the volume, so it's checked even if SMART is unreadable.
    if ($null -ne $d.FreePct -and $d.FreePct -lt $LowFreePct) {
        if (-not $prev -or $null -eq $prev.FreePct -or $prev.FreePct -ge $LowFreePct) {
            $old = $firstTxt; if ($prev -and $null -ne $prev.FreePct) { $old = "$($prev.FreePct)% free" }
            $changes += "Low free space: $old -> $($d.FreePct)% free (threshold $LowFreePct%)"
        }
    }

    if ($changes.Count) { $alerts += @{ Drive = $d; Changes = $changes } }

    # When unreadable, keep the last good counters so increases are still caught later.
    $src = $d
    if (-not $d.Readable -and $prev) { $src = $prev }
    $newState[$key] = [ordered]@{
        Model = $d.Model; Letters = $d.Letters; Interface = $d.Interface; WinSerial = $d.WinSerial
        Readable = $d.Readable; Health = $d.Health; IsHdd = $d.IsHdd
        TempC = $d.TempC; Realloc = $src.Realloc; Pending = $src.Pending; OfflineUnc = $src.OfflineUnc; Crc = $src.Crc
        NvCritical = $src.NvCritical; NvMedia = $src.NvMedia; NvUsed = $src.NvUsed
        FreePct = $d.FreePct; LastSeen = $now; Missing = $false
    }
}

# Drives in state that are gone now.
foreach ($k in $prevState.Keys) {
    if ($newState.Contains($k)) { continue }
    $p = $prevState[$k]
    if (($Exclude -contains "$k".ToUpper()) -or ($p.WinSerial -and ($Exclude -contains "$($p.WinSerial)".ToUpper()))) {
        Write-Log "Dropping excluded drive SN $k from state."
        continue
    }
    if (-not $p.Missing) {
        $ghost = [pscustomobject]@{ Letters = $p.Letters; Model = $p.Model; Serial = $k; PD = '?' }
        $alerts += @{ Drive = $ghost; Changes = @("Drive missing: present (last seen $($p.LastSeen)) -> not detected") }
    }
    $p.Missing = $true
    $newState[$k] = $p
}

# ---------------------------------------------------------------- post + save
# Every run posts the full status; alerts (if any) go on top.
$exitCode = 0
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
if ($alerts.Count) {
    $lines = @("$($EM.Siren) **Drive health alert - $HostName** ($stamp)")
    foreach ($a in ($alerts | Sort-Object { Get-SortKey $_.Drive })) {
        $block = Get-DriveLabel $a.Drive
        foreach ($c in $a.Changes) {
            $block += "`n- $c"
            Write-Log "ALERT $($a.Drive.Letters) SN $($a.Drive.Serial): $c" 'WARN'
        }
        $lines += $block
    }
    $lines += "`n$($EM.Chart) **Status of all drives**"
    $exitCode = 1
} else {
    Write-Log 'No changes requiring an alert.'
    $lines = @("$($EM.Chart) **Weekly drive status - $HostName** ($stamp) - no new problems")
}
$lines += Get-StatusLines $drives
if ($excluded.Count) { $lines += "-# Excluded: $(($excluded | ForEach-Object { "$($_.Model) $($_.Serial)" }) -join ', ')" }
$lines += Get-Legend
try {
    Send-Discord $lines
    if ($DryRun) { Write-Log "Dry run: status ($($alerts.Count) alert(s)) not posted." }
    else { Write-Log "Posted status with $($alerts.Count) drive alert(s)." }
} catch {
    # Don't save state so the same alerts are retried next run.
    Write-Log "Discord post failed, state not saved so alerts retry next run: $($_.Exception.Message)" 'ERROR'
    exit 2
}

if ($DryRun) {
    Write-Log 'Dry run: state.json not written.'
} else {
    try {
        $newState | ConvertTo-Json -Depth 5 | Set-Content -Path $StatePath -Encoding UTF8
    } catch { Write-Log "Failed to write state.json: $($_.Exception.Message)" 'ERROR'; exit 2 }
}
Write-Log "Run finished ($($drives.Count) drives checked, $($excluded.Count) excluded)."
exit $exitCode
