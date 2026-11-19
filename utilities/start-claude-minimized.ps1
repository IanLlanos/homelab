$id = (Get-StartApps | Where-Object Name -eq 'Claude').AppID
Start-Process explorer.exe "shell:AppsFolder\$id"
Add-Type -Namespace W -Name U -MemberDefinition '[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);'
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep 1
  $w = Get-Process claude -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }
  if ($w) { Start-Sleep 2; $w | ForEach-Object { [W.U]::ShowWindow($_.MainWindowHandle, 6) | Out-Null }; break }
}
