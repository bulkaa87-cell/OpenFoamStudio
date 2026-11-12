# Build the Windows installer of OpenFOAM Studio.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 `
#       -Python C:\path\to\venv\Scripts\python.exe -Out X:\some\build `
#       [-Version 0.1.0] [-Iscc C:\path\to\ISCC.exe] [-CargoTarget X:\some\target] `
#       [-SkipRust] [-SkipInstaller]
#
# Steps:
#   1. Rust: foamrs, foamview, meshfix (.pyd in the repository root) and the
#      Linux binary foamdist (rust\foamdist\target\x86_64-unknown-linux-musl).
#   2. PyInstaller: <Out>\dist\OpenFOAM Studio\OpenFOAM Studio.exe
#   3. Inno Setup: <Out>\installer\OpenFOAM-Studio-<Version>-windows-x64-setup.exe
# Then test: python tests\test_installer.py <setup.exe>
#
# The Python must have requirements.txt and pyinstaller installed. Use a
# separate venv for the build, not the .venv of the program.
# This file is ASCII only (Windows PowerShell 5.1 reads it without a BOM).

param(
    [string]$Python = "",
    [string]$Out = "",
    [string]$Version = "0.1.0",
    [string]$Iscc = "",
    [string]$CargoTarget = "",
    [switch]$SkipRust,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $Python) { $Python = Join-Path $root ".venv\Scripts\python.exe" }
if (-not $Out) { $Out = Join-Path $root "build" }
if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
New-Item -ItemType Directory -Force $Out | Out-Null

# --- 1. Rust ---------------------------------------------------------------
if (-not $SkipRust) {
    if (-not $env:CARGO_HOME -and (Test-Path "X:\rust\cargo")) { $env:CARGO_HOME = "X:\rust\cargo" }
    if (-not $env:RUSTUP_HOME -and (Test-Path "X:\rust\rustup")) { $env:RUSTUP_HOME = "X:\rust\rustup" }
    $cargo = "cargo"
    if ($env:CARGO_HOME -and (Test-Path (Join-Path $env:CARGO_HOME "bin\cargo.exe"))) {
        $cargo = Join-Path $env:CARGO_HOME "bin\cargo.exe"
    }
    $env:PYO3_PYTHON = $Python
    foreach ($crate in @("foamrs", "foamview", "meshfix")) {
        Push-Location (Join-Path $root "rust\$crate")
        try {
            if ($CargoTarget) { $env:CARGO_TARGET_DIR = $CargoTarget; $tdir = $CargoTarget }
            else { Remove-Item Env:CARGO_TARGET_DIR -ErrorAction SilentlyContinue; $tdir = Join-Path (Get-Location) "target" }
            & $cargo build --release
            if ($LASTEXITCODE -ne 0) { throw "cargo build ($crate) failed" }
            Copy-Item (Join-Path $tdir "release\$crate.dll") (Join-Path $root "$crate.pyd") -Force
            Write-Host "$crate.pyd -> $root"
        } finally { Pop-Location }
    }
    Push-Location (Join-Path $root "rust\foamdist")
    try {
        & $cargo build --release --target x86_64-unknown-linux-musl
        if ($LASTEXITCODE -ne 0) { throw "cargo build (foamdist) failed" }
        if ($CargoTarget) {
            $dst = Join-Path $root "rust\foamdist\target\x86_64-unknown-linux-musl\release"
            New-Item -ItemType Directory -Force $dst | Out-Null
            Copy-Item (Join-Path $CargoTarget "x86_64-unknown-linux-musl\release\foamdist") $dst -Force
        }
    } finally { Pop-Location }
    Remove-Item Env:CARGO_TARGET_DIR -ErrorAction SilentlyContinue
}

# --- 2. PyInstaller --------------------------------------------------------
$dist = Join-Path $Out "dist"
$work = Join-Path $Out "work"
& $Python -m PyInstaller --noconfirm --clean --distpath $dist --workpath $work `
    (Join-Path $root "packaging\pyinstaller\openfoam_studio.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$exe = Join-Path $dist "OpenFOAM Studio\OpenFOAM Studio.exe"
if (-not (Test-Path $exe)) { throw "not built: $exe" }
Write-Host "program: $exe"

# --- 3. Inno Setup ---------------------------------------------------------
if (-not $SkipInstaller) {
    if (-not $Iscc) {
        foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                         "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                         "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $c) { $Iscc = $c; break }
        }
    }
    if (-not $Iscc -or -not (Test-Path $Iscc)) { throw "ISCC.exe (Inno Setup 6) not found: give -Iscc" }
    $instOut = Join-Path $Out "installer"
    & $Iscc "/DAppVersion=$Version" "/DSrcDir=$(Join-Path $dist 'OpenFOAM Studio')" `
        "/DOutDir=$instOut" (Join-Path $root "packaging\windows\openfoam_studio.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "installer: $(Join-Path $instOut "OpenFOAM-Studio-$Version-windows-x64-setup.exe")"
}
