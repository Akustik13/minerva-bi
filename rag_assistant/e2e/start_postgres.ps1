# Тимчасова PostgreSQL 16 для наскрізного тесту: контейнер minerva-e2e-pg, лише 127.0.0.1:55433.
# Робочих БД не торкається. Пароль генерується один раз у secrets\ (доступ: поточний користувач + SYSTEM).
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "env.ps1")

New-Item -ItemType Directory -Force $E2ESecrets | Out-Null
icacls $E2ESecrets /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
$pwFile = Join-Path $E2ESecrets "db-password.txt"
$envFile = Join-Path $E2ESecrets "postgres.env"
if (-not (Test-Path $pwFile)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    [IO.File]::WriteAllText($pwFile, $pw)
    [IO.File]::WriteAllText($envFile, "POSTGRES_DB=minerva_e2e`nPOSTGRES_USER=minerva_e2e`nPOSTGRES_PASSWORD=$pw`n")
}

$exists = docker ps -a --filter "name=^$E2EContainer$" --format "{{.Names}}"
if ($exists) {
    docker start $E2EContainer | Out-Null
} else {
    docker run -d --name $E2EContainer --env-file $envFile -p 127.0.0.1:55433:5432 postgres:16 | Out-Null
}
for ($i = 0; $i -lt 30; $i++) {
    docker exec $E2EContainer pg_isready -U minerva_e2e -d minerva_e2e 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "PostgreSQL готова: 127.0.0.1:55433/minerva_e2e"; exit 0 }
    Start-Sleep -Seconds 1
}
throw "PostgreSQL не відповіла за 30 с"
