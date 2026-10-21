# Зупинити dev-Minerva (порт 8010) і тестову PostgreSQL.
# -Remove — видалити контейнер разом з його тестовими даними (лише minerva-e2e-pg).
param([switch]$Remove, [int]$Port = 8010)
. (Join-Path $PSScriptRoot "env.ps1")
foreach ($c in Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Stop-Process -Id $c.OwningProcess -Force -Confirm:$false
    Write-Host "Dev-Minerva (процес $($c.OwningProcess)) зупинено"
}
if ($Remove) { docker rm -f -v $E2EContainer | Out-Null; Write-Host "Контейнер $E2EContainer видалено" }
else { docker stop $E2EContainer | Out-Null; Write-Host "Контейнер $E2EContainer зупинено" }
