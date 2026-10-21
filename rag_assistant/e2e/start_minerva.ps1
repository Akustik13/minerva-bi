# Dev-Minerva для наскрізного тесту: схема з моделей у minerva_e2e, сервер лише на 127.0.0.1.
# -Background — окремий прихований процес (не залежить від вікна чи сесії), журнал у minerva-e2e\logs.
param([int]$Port = 8010, [switch]$Background)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "env.ps1")
if (-not $env:E2E_DB_PASSWORD) { throw "Спершу start_postgres.ps1" }

if ($Background) {
    $logs = Join-Path $env:MINERVA_E2E_HOME "logs"
    New-Item -ItemType Directory -Force $logs | Out-Null
    # Через WMI: процес не успадковує консоль і дескриптори того, хто запускає (інакше виклик «висить»)
    $cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port " +
           "> `"$logs\minerva.out.log`" 2> `"$logs\minerva.err.log`""
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $E2ERepo }
    if ($r.ReturnValue -ne 0) { throw "Не вдалося запустити процес (код $($r.ReturnValue))" }
    for ($i = 0; $i -lt 60; $i++) {
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
            Write-Host "Dev-Minerva: http://127.0.0.1:$Port (процес $($r.ProcessId), журнал $logs)"; exit 0
        }
        Start-Sleep -Seconds 1
    }
    throw "Сервер не відповів за 60 с — див. $logs\minerva.err.log"
}

Set-Location $E2ERepo
& $E2EPython manage.py migrate --run-syncdb --noinput
if ($LASTEXITCODE -ne 0) { throw "migrate --run-syncdb не вдався" }
& $E2EPython manage.py runserver "127.0.0.1:$Port" --noreload
