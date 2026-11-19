# Removes duplicate copies listed in dedupe-plan.csv (built Nov 4 from Plex's library).
# Rule: keep the bigger file (higher quality). Within 5% = same quality -> keep the copy on the drive with more free space.
# If the copy being removed is the one Sonarr/Radarr tracks, the better copy is moved into its place instead.
# Preview:  powershell -ExecutionPolicy Bypass -File C:\Scripts\dedupe\dedupe.ps1
# Run:      powershell -ExecutionPolicy Bypass -File C:\Scripts\dedupe\dedupe.ps1 -Run
param([switch]$Run)
$ErrorActionPreference = 'Continue'
$plan = Import-Csv (Join-Path $PSScriptRoot 'dedupe-plan.csv')
$vid = '.mkv','.mp4','.avi','.m4v','.ts','.wmv'
function Api($app) { [xml]$c = Get-Content "C:\ProgramData\$app\config.xml"; @{ Base = "http://localhost:$($c.Config.Port)$($c.Config.UrlBase)/api/v3"; H = @{ 'X-Api-Key' = $c.Config.ApiKey } } }
$son = Api 'Sonarr'
$radarr = $null
try { $rad = Api 'Radarr'; $radarr = @{}; foreach ($m in Invoke-RestMethod "$($rad.Base)/movie" -Headers $rad.H) { if ($m.movieFile -and $m.movieFile.path) { $radarr[$m.movieFile.path.ToLower()] = $m.id } } }
catch { Write-Warning "Couldn't reach Radarr, movie rows will be skipped: $($_.Exception.Message)" }
$moved = @{}; $sonIds = @{}; $radIds = @{}; $dirs = @{}; $freed = 0; $n = 0
foreach ($r in $plan) {
  $keepPath = $r.Keep; if ($moved.ContainsKey($keepPath.ToLower())) { $keepPath = $moved[$keepPath.ToLower()] }
  $keep = Get-Item -LiteralPath $keepPath -ErrorAction SilentlyContinue
  $rem  = Get-Item -LiteralPath $r.Remove -ErrorAction SilentlyContinue
  if (-not $keep -or -not $rem) { "SKIP (file no longer there): $($r.Item)"; continue }
  $act = $r.Action
  if ($r.Kind -eq 'Movie') {
    if ($null -eq $radarr) { "SKIP (Radarr unavailable): $($r.Item)"; continue }
    $rid = $radarr[$rem.FullName.ToLower()]; $kid = $radarr[$keep.FullName.ToLower()]
    if ($rid -and -not $kid) { $act = 'REPLACE' }
    if ($rid) { $radIds[[int]$rid] = 1 }; if ($kid) { $radIds[[int]$kid] = 1 }
  } elseif ($r.SonarrId) { $sonIds[[int]$r.SonarrId] = 1 }
  if ($act -eq 'REPLACE') {
    $dest = Join-Path $rem.DirectoryName ([IO.Path]::GetFileNameWithoutExtension($rem.Name) + $keep.Extension)
    "REPLACE $($r.Item): move better copy $($keep.FullName) [$($r.KeepGB) GB] into the tracked spot, remove $($rem.FullName) [$($r.RemoveGB) GB]"
    if ($Run) { Remove-Item -LiteralPath $rem.FullName -Force; Move-Item -LiteralPath $keep.FullName -Destination $dest; $moved[$r.Keep.ToLower()] = $dest; $dirs[$keep.DirectoryName] = 1 }
  } else {
    "DELETE  $($r.Item): $($rem.FullName) [$($r.RemoveGB) GB]   keeping $($keep.FullName) [$($r.KeepGB) GB]"
    if ($Run) { Remove-Item -LiteralPath $rem.FullName -Force; $dirs[$rem.DirectoryName] = 1 }
  }
  $freed += [double]$r.RemoveGB; $n++
}
if ($Run) {
  foreach ($d in $dirs.Keys) {
    foreach ($x in @($d, (Split-Path $d -Parent))) {
      if (-not (Test-Path -LiteralPath $x)) { continue }
      if ((Split-Path $x -Leaf) -in 'TV','MOVIES' -or (Split-Path $x -Parent) -match '^[A-Z]:\\?$') { continue }
      if (-not (Get-ChildItem -LiteralPath $x -Recurse -File | Where-Object { $vid -contains $_.Extension.ToLower() })) { Remove-Item -LiteralPath $x -Recurse -Force; "Removed empty folder $x" }
    }
  }
  foreach ($id in $sonIds.Keys) { Invoke-RestMethod -Method Post "$($son.Base)/command" -Headers $son.H -ContentType 'application/json' -Body (@{ name = 'RescanSeries'; seriesId = $id } | ConvertTo-Json) | Out-Null }
  if ($radIds.Count) { Invoke-RestMethod -Method Post "$($rad.Base)/command" -Headers $rad.H -ContentType 'application/json' -Body (@{ name = 'RescanMovie'; movieIds = @($radIds.Keys) } | ConvertTo-Json) | Out-Null }
  "Sonarr rescans: $($sonIds.Count) shows. Radarr rescans: $($radIds.Count) movies."
}
"{0} {1} files, {2:N1} GB" -f ($(if ($Run) { 'Removed' } else { 'Would remove' })), $n, $freed
