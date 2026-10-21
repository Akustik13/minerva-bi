# Зупинити тестову PostgreSQL. -Remove — видалити контейнер разом з його тестовими даними (лише minerva-e2e-pg).
param([switch]$Remove)
. (Join-Path $PSScriptRoot "env.ps1")
if ($Remove) { docker rm -f -v $E2EContainer | Out-Null; Write-Host "Контейнер $E2EContainer видалено" }
else { docker stop $E2EContainer | Out-Null; Write-Host "Контейнер $E2EContainer зупинено" }
