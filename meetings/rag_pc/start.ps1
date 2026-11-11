# Start the bench in the background (survives the SSH / RDP session); does nothing if it already runs.
#   powershell -ExecutionPolicy Bypass -File D:\Meeting\app\start.ps1
param([string]$Root = 'D:\Meeting')
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match '-m bench( |$)' -and $_.CommandLine -notmatch 'bench\.(check|worker)' }
if ($running) { Write-Output "already running: pid $($running.ProcessId -join ', ')"; exit 0 }
$exe = Join-Path $Root 'venv\Scripts\pythonw.exe'
$app = Join-Path $Root 'app'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = "`"$exe`" -B -m bench --root `"$Root`""; CurrentDirectory = $app
}
if ($r.ReturnValue -ne 0) { throw "start failed: $($r.ReturnValue)" }
Start-Sleep -Seconds 4
Write-Output "started pid $($r.ProcessId)"
Get-Content (Join-Path $Root 'data\logs\bench.log') -Tail 3 -Encoding UTF8 -ErrorAction SilentlyContinue
