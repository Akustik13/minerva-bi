# Dev-Minerva для наскрізного тесту: схема з моделей у minerva_e2e, сервер лише на 127.0.0.1.
param([int]$Port = 8010)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "env.ps1")
if (-not $env:E2E_DB_PASSWORD) { throw "Спершу start_postgres.ps1" }

Set-Location $E2ERepo
& $E2EPython manage.py migrate --run-syncdb --noinput
if ($LASTEXITCODE -ne 0) { throw "migrate --run-syncdb не вдався" }
& $E2EPython manage.py runserver "127.0.0.1:$Port" --noreload
