<#
.SYNOPSIS
  build_exe.ps1 - assemble the UNSIGNED Windows x64 Alice AI package (M5).

.DESCRIPTION
  Windows counterpart of packaging/macos/build_app.sh. Produces the same ONE
  frozen binary, two roles (PLAN sec 2.2): the PyWebView shell (default) re-execs
  itself as the uvicorn backend child (alice_entry.py --alice-backend), bound to
  a kill-on-close Job Object so closing the shell kills the backend (M5,
  shell/alice_shell/win_job.py).

  Pipeline (pure Windows tooling + PyInstaller):
    1. (optional) render assets/icons/AliceAI.ico from the Alice mark PNG.
    2. compile + hash-install the Windows deps (mlx->llama-cpp-python swap):
       backend/requirements.win.txt -> backend/requirements.lock.win.txt, then
       pip install --require-hashes.   (CPU wheel by default; -Cuda for GPU.)
    3. PyInstaller one-dir freeze (the same backend/alice-backend.spec; on
       Windows it stops at COLLECT - no macOS BUNDLE) -> backend/dist/AliceAI/.
    4. package UNSIGNED:
         * ALWAYS: a folder-zip   dist/AliceAI-windows-x64.zip   (right-click ->
           Extract -> run AliceAI.exe - the no-installer path).
         * OPTIONAL (-Inno): an Inno Setup installer dist/AliceAI-windows-x64-setup.exe
           (Start-menu shortcut), IF iscc.exe (Inno Setup) is on PATH.
       No code-signing (UNSIGNED per V - no EV cert). SmartScreen will warn on
       first run; see the "UNBLOCK / Run anyway" note printed at the end + the
       onboarding copy.

  Models are NOT bundled (installer stays lean) - first run downloads Alice Lite
  to the user data dir (%LOCALAPPDATA%\Alice or ~\.alice via ALICE_AI_DATA_DIR).

.PARAMETER SkipFreeze
  Reuse an existing backend/dist/AliceAI (skip the PyInstaller step).

.PARAMETER SkipDeps
  Skip the lock-compile + dependency install (use when a prior step, e.g. the CI
  install job, already installed the hash-pinned Windows deps). The freeze still
  runs (unless -SkipFreeze).

.PARAMETER Cuda
  Install the CUDA llama-cpp-python wheel (NVIDIA GPU build) instead of CPU.

.PARAMETER Inno
  Also build the Inno Setup installer (requires iscc.exe on PATH).

.EXAMPLE
  pwsh packaging\windows\build_exe.ps1
  pwsh packaging\windows\build_exe.ps1 -Cuda -Inno
#>
[CmdletBinding()]
param(
    [switch]$SkipFreeze,   # reuse an existing backend/dist/AliceAI (skip PyInstaller)
    [switch]$SkipDeps,     # deps already installed (CI installs them in a prior step)
    [switch]$Cuda,
    [switch]$Inno
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# UTF-8 everywhere (PLAN sec 6 / F-class): llama.cpp + HF progress emit non-ASCII;
# cp1252 consoles crash on it. The frozen entry also sets PYTHONUTF8=1, but set
# it for THIS build session (pip/pyinstaller logs) too.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# --- paths ---------------------------------------------------------------- #
$RootDir  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Backend  = Join-Path $RootDir 'backend'
$Dist     = Join-Path $RootDir 'dist'
$AppName  = 'AliceAI'
$Arch     = 'x64'
$FrozenDir = Join-Path $Backend "dist\$AppName"      # PyInstaller COLLECT one-dir
$Zip      = Join-Path $Dist "$AppName-windows-$Arch.zip"
$Setup    = Join-Path $Dist "$AppName-windows-$Arch-setup.exe"
$IcoPath  = Join-Path $RootDir 'assets\icons\AliceAI.ico'
$LockWin  = Join-Path $Backend 'requirements.lock.win.txt'
$ReqWin   = Join-Path $Backend 'requirements.win.txt'

if (-not $IsWindows) { throw 'build_exe.ps1 must run on Windows.' }

# Use the venv python if present, else whatever python is on PATH.
$VenvPy = Join-Path $RootDir '.venv\Scripts\python.exe'
$Py = if (Test-Path $VenvPy) { $VenvPy } else { 'python' }
Write-Host "==> python: $Py"

# --- 1/4  icon (best-effort; spec falls back to no-icon if absent) -------- #
Write-Host '==> 1/4  icon (AliceAI.ico)'
if (-not (Test-Path $IcoPath)) {
    $logoPng = Join-Path $RootDir 'assets\brand\alice-logo.png'
    if (Test-Path $logoPng) {
        try {
            # Pillow renders a multi-size .ico from the brand PNG (composited on
            # the same dark squircle as the .icns would be overkill here - the
            # PNG mark on transparency is fine for the taskbar/exe icon).
            & $Py -c @"
from PIL import Image
import os
src = r'$logoPng'
out = r'$IcoPath'
img = Image.open(src).convert('RGBA')
sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
img.save(out, format='ICO', sizes=sizes)
print('wrote', out)
"@
        } catch {
            Write-Warning "icon build skipped ($_). Spec will build without an icon."
        }
    } else {
        Write-Warning "no assets/brand/alice-logo.png - building without an icon."
    }
} else {
    Write-Host "    reusing $IcoPath"
}

# --- 2/4  deps: hash-pinned Windows lock (the mlx->llama_cpp swap) ---------- #
Write-Host '==> 2/4  Windows deps (llama-cpp-python; mlx omitted)'
if (-not $SkipDeps) {
    & $Py -m pip install --quiet --upgrade uv | Out-Null

    if ($Cuda) {
        # CUDA build: compile the lock with the CUDA wheel index so the GPU wheel
        # + its hash land in the lock. n_gpu_layers=-1 is chosen at RUN time.
        Write-Host '    -Cuda: resolving the CUDA llama-cpp-python wheel'
        $env:PIP_EXTRA_INDEX_URL = 'https://abetlen.github.io/llama-cpp-python/whl/cu124'
    }

    # Generate the hash-pinned lock ON THIS WINDOWS RUNNER (wheel hashes are
    # per-platform; a macOS lock cannot install here). Committed by CI. Run from
    # backend/ so the input's `./vendor` path package resolves (uv/pip resolve
    # relative paths against CWD).
    Push-Location $Backend
    try {
        & $Py -m uv pip compile --generate-hashes --no-header `
            requirements.win.txt -o requirements.lock.win.txt
        if (-not (Test-Path $LockWin)) { throw "lock not generated: $LockWin" }
        # The vendored path package can't be hash-pinned, so uv emits an unhashed
        # `./vendor` line that --require-hashes rejects. Strip it; install the
        # vendored package separately --no-deps (its deps are in the hashed set).
        (Get-Content requirements.lock.win.txt) `
            | Where-Object { $_.Trim() -ne './vendor' } `
            | Set-Content requirements.lock.win.txt
        & $Py -m pip install --quiet --no-deps .\vendor
        & $Py -m pip install --quiet --require-hashes -r requirements.lock.win.txt
    } finally { Pop-Location }
} else {
    Write-Host '    -SkipDeps: deps installed by a prior step (CI) - skipping lock+install'
}

# Sanity: the runtime swap must be satisfied (llama_cpp present, mlx absent).
& $Py -c "import llama_cpp; print('    llama_cpp', llama_cpp.__version__)"
& $Py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('mlx') is None else 1)"
if ($LASTEXITCODE -ne 0) { Write-Warning 'mlx is installed on Windows (unexpected) - it will not be used.' }

# --- 3/4  PyInstaller freeze (one-dir; no BUNDLE on Windows) --------------- #
Write-Host '==> 3/4  PyInstaller freeze'
if ($SkipFreeze -and (Test-Path $FrozenDir)) {
    Write-Host "    -SkipFreeze: reusing $FrozenDir"
} else {
    Push-Location $Backend
    try {
        if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }
        if (Test-Path 'dist')  { Remove-Item -Recurse -Force 'dist'  }
        & $Py -m PyInstaller --noconfirm --log-level=WARN alice-backend.spec
    } finally { Pop-Location }
}
$Exe = Join-Path $FrozenDir 'AliceAI.exe'
if (-not (Test-Path $Exe)) { throw "freeze produced no $Exe" }
$frozenSize = '{0:N0} MB' -f ((Get-ChildItem -Recurse $FrozenDir | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "    $FrozenDir ($frozenSize)"

# --- 4/4  package UNSIGNED: folder-zip (+ optional Inno installer) --------- #
Write-Host '==> 4/4  package (UNSIGNED)'
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
if (Test-Path $Zip) { Remove-Item -Force $Zip }
# The whole one-dir (AliceAI.exe + _internal) -> a single zip. Compress-Archive
# is pure PowerShell (no 7-Zip needed).
Compress-Archive -Path (Join-Path $FrozenDir '*') -DestinationPath $Zip -CompressionLevel Optimal
Write-Host "    zip : $Zip"

if ($Inno) {
    $iscc = Get-Command 'iscc.exe' -ErrorAction SilentlyContinue
    if ($iscc) {
        $issPath = Join-Path $PSScriptRoot 'AliceAI.iss'
        Write-Host "    Inno Setup: $issPath"
        & $iscc.Source `
            "/DAppSourceDir=$FrozenDir" `
            "/DAppIcon=$IcoPath" `
            "/DOutputDir=$Dist" `
            "/DOutputBase=$AppName-windows-$Arch-setup" `
            $issPath
        if (Test-Path $Setup) { Write-Host "    setup : $Setup" }
    } else {
        Write-Warning '-Inno requested but iscc.exe (Inno Setup) not on PATH - skipping installer (zip still produced).'
    }
}

Write-Host ''
Write-Host 'DONE (UNSIGNED Windows x64):'
Write-Host "  one-dir : $FrozenDir"
Write-Host "  zip     : $Zip"
if (Test-Path $Setup) { Write-Host "  setup   : $Setup" }
Write-Host ''
Write-Host 'FIRST-RUN (SmartScreen - UNSIGNED, expected):'
Write-Host '  This build is not code-signed (no EV cert). Windows SmartScreen will'
Write-Host '  show "Windows protected your PC" on first launch. To run it:'
Write-Host '    1. If you downloaded the .zip: right-click it -> Properties ->'
Write-Host '       check "Unblock" -> OK, THEN extract (clears the MOTW so the'
Write-Host '       extracted .exe is not flagged).'
Write-Host '    2. Double-click AliceAI.exe. If SmartScreen still appears, click'
Write-Host '       "More info" -> "Run anyway".'
Write-Host '  (The ed25519 update manifest is the trust anchor later, not a cert.)'
