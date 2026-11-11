# Install or update the meeting test bench on the RAG PC (run as the RAG PC user, no admin needed).
#   powershell -ExecutionPolicy Bypass -File D:\Meeting\app\setup.ps1 [-SkipModels]
# Code is expected in D:\Meeting\app (bench\, tools\, requirements.txt). Models go to D:\Meeting\models,
# recordings to D:\Meeting\data (readable only by this user and SYSTEM).
param(
  [string]$Root = 'D:\Meeting',
  [string]$Python = 'D:\AI_training\Python312\python.exe',
  [switch]$SkipModels
)
$ErrorActionPreference = 'Stop'
$app = Join-Path $Root 'app'
$models = Join-Path $Root 'models'
$data = Join-Path $Root 'data'
foreach ($d in $data, "$models\parakeet", "$models\vad", "$models\whisper", "$Root\pip-cache") {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Output '== recordings folder: only this user and SYSTEM'
& icacls $data /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

Write-Output '== Python environment'
$py = Join-Path $Root 'venv\Scripts\python.exe'
if (-not (Test-Path $py)) { & $Python -m venv (Join-Path $Root 'venv') }
$env:PIP_CACHE_DIR = Join-Path $Root 'pip-cache'
& $py -m pip install --disable-pip-version-check -q --upgrade pip
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $app 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

if (-not $SkipModels) {
  $parakeet = "$models\parakeet\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
  if (-not (Test-Path "$parakeet\tokens.txt")) {
    Write-Output '== Parakeet-TDT-0.6B-v3 int8 (487 MB)'
    $tar = "$models\parakeet\parakeet.tar.bz2"
    & curl.exe -s -L -m 1800 -o $tar 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2'
    # Windows 10 tar.exe cannot read .bz2 -> unpack with Python
    & $py -c "import sys, tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter='data')" $tar "$models\parakeet"
    Remove-Item $tar
    if (-not (Test-Path "$parakeet\tokens.txt")) { throw 'Parakeet was not unpacked' }
  }
  if (-not (Test-Path "$models\vad\silero_vad.onnx")) {
    Write-Output '== Silero VAD'
    & curl.exe -s -L -m 300 -o "$models\vad\silero_vad.onnx" 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx'
  }
  Write-Output '== Whisper large-v3 and large-v3-turbo (about 4.6 GB, skipped when present)'
  $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
  $env:HF_HUB_DISABLE_TELEMETRY = '1'
  & $py -c "from faster_whisper.utils import download_model as d; c=r'$models\whisper'; [d(m, cache_dir=c) for m in ('large-v3-turbo', 'large-v3')]; print('ok')"
  if ($LASTEXITCODE -ne 0) { throw 'Whisper download failed' }
}

Write-Output '== self-check'
Push-Location $app
& $py -B -m bench.check --root $Root
Pop-Location
