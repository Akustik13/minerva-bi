# Спільне оточення наскрізного тесту (dot-source з інших скриптів). Значення секретів не друкуються.
param([string]$DataDir = $(if ($env:MINERVA_E2E_HOME) { $env:MINERVA_E2E_HOME } else { Join-Path $env:USERPROFILE "minerva-e2e" }))

$script:E2ERepo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script:E2EPython = if ($env:MINERVA_PYTHON) { $env:MINERVA_PYTHON } else { "C:\tabele_mvp\venv\Scripts\python.exe" }
$script:E2ESecrets = Join-Path $DataDir "secrets"
$script:E2EContainer = "minerva-e2e-pg"

$env:MINERVA_E2E_HOME = $DataDir
$env:DJANGO_SETTINGS_MODULE = "tabele.settings_e2e"
$env:MINERVA_BACKUP_SCHEDULER_STARTED = "1"      # без автобекапів у тестовому середовищі
$env:PYTHONIOENCODING = "utf-8"
$pwFile = Join-Path $script:E2ESecrets "db-password.txt"
if (Test-Path $pwFile) { $env:E2E_DB_PASSWORD = (Get-Content $pwFile -Raw).Trim() }
