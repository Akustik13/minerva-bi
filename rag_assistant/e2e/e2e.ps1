# Команди сценарію: .\e2e.ps1 seed | status | step reassign | search "KW 46" --user e2e_anna | fallback --simulate
. (Join-Path $PSScriptRoot "env.ps1")
if (-not $env:E2E_DB_PASSWORD) { throw "Спершу start_postgres.ps1" }
Set-Location $E2ERepo
& $E2EPython manage.py knowledge_e2e @args
exit $LASTEXITCODE
