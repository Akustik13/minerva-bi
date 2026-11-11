# Stop the bench (and a running transcript worker). Exit code 1 if a process could not be stopped
# (started by another user or as administrator), so the GUI does not report a restart that did not happen.
#   powershell -ExecutionPolicy Bypass -File D:\Meeting\app\stop.ps1
$procs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match '-m bench(\.worker)?( |$)' })
if (-not $procs) { Write-Output 'not running'; exit 0 }
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 700
$left = @($procs | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
if ($left) {
  Write-Output "could not stop pid $($left.ProcessId -join ', ') - started by another user or as administrator?"
  exit 1
}
Write-Output "stopped pid $($procs.ProcessId -join ', ')"
