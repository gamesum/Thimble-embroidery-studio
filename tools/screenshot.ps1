param([string]$Query = "", [string]$Out = "ui.png", [int]$W = 1600, [int]$H = 980)
$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
$profile = Join-Path $env:TEMP ("thimble_shot_" + [guid]::NewGuid().ToString("N"))
$outPath = Join-Path (Join-Path $PSScriptRoot "..\output\tests") $Out
& $edge --headless=new --disable-gpu --hide-scrollbars --user-data-dir="$profile" --window-size="$W,$H" `
  --virtual-time-budget=9000 --screenshot="$outPath" "http://127.0.0.1:5311/$Query" 2>$null | Out-Null
Start-Sleep -Milliseconds 300
Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $outPath) { "saved $outPath" } else { "no screenshot" }
