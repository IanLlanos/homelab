<#
  Posts the Plex Wrapped announcement (@everyone + banner image) to the announcements channel.
  The webhook is read from C:\Scripts\WeeklyUpdate\config.json (PublicWebhookUrl) at run time.
  -Test  sends it to the private alert channel instead (WebhookUrl), without @everyone.
#>
param([switch]$Test)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content 'C:\Scripts\WeeklyUpdate\config.json' -Raw | ConvertFrom-Json
$url = if ($Test) { $cfg.WebhookUrl } else { $cfg.PublicWebhookUrl }
$img = Join-Path $PSScriptRoot 'wrapped-2026.png'
$gift = [char]::ConvertFromUtf32(0x1F381); $pop = [char]::ConvertFromUtf32(0x1F37F); $lock = [char]::ConvertFromUtf32(0x1F512); $point = [char]::ConvertFromUtf32(0x1F449)

$embed = @{
    title       = "$gift Your 2026 Plex Wrapped is here!"
    url         = 'https://<YOUR_DOMAIN>/wrapped'
    description = "Ever wonder how much you actually watched this year? Now you can find out.`n`n$point **[<YOUR_DOMAIN>/wrapped](https://<YOUR_DOMAIN>/wrapped)**"
    color       = 15048717
    fields      = @(
        @{ name = "$pop What's inside"; value = "Your most-watched shows and movies, total hours watched, and more from your year on the server. Sign in with your Plex account to see yours."; inline = $false }
    )
    image       = @{ url = 'attachment://wrapped-2026.png' }
    footer      = @{ text = '<SERVER_NAME> | Happy holidays!' }
}
$payload = @{
    username         = 'Server Status'
    content          = $(if ($Test) { 'TEST (no @everyone)' } else { '@everyone' })
    allowed_mentions = @{ parse = $(if ($Test) { @() } else { @('everyone') }) }
    embeds           = @($embed)
    attachments      = @(@{ id = 0; filename = 'wrapped-2026.png' })
}
$json = $payload | ConvertTo-Json -Depth 8

Add-Type -AssemblyName System.Net.Http
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$client = New-Object System.Net.Http.HttpClient
$form = New-Object System.Net.Http.MultipartFormDataContent
$form.Add((New-Object System.Net.Http.StringContent($json, [Text.Encoding]::UTF8, 'application/json')), 'payload_json')
$file = New-Object System.Net.Http.ByteArrayContent(, [IO.File]::ReadAllBytes($img))
$file.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('image/png')
$form.Add($file, 'files[0]', 'wrapped-2026.png')
$r = $client.PostAsync($url, $form).Result
if (-not $r.IsSuccessStatusCode) { throw "Discord returned $([int]$r.StatusCode): $($r.Content.ReadAsStringAsync().Result)" }

Add-Content -Path (Join-Path $PSScriptRoot 'announcements.log') -Value ("{0} Sent Wrapped announcement{1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $(if ($Test) { ' (TEST)' } else { '' }))
"Sent" + $(if ($Test) { ' (test, to alert channel)' } else { ' to announcements' })
