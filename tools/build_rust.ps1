# Зібрати Rust-частини програми:
#   foamrs   -> foamrs.pyd   (швидке читання полів, лінії на тілі)
#   foamview -> foamview.pyd (3D-перегляд вкладки «3D» на GPU, wgpu)
#   meshfix  -> meshfix.pyd  (ремонт сітки кнопкою «meshfix», кроки служби Windows)
#   foamdist -> Linux-бінарник для сервера (розподіл між машинами)
#
#   powershell -ExecutionPolicy Bypass -File tools\build_rust.ps1
#
# Програма працює і без них: fs_fast.py тоді читає через VTK, а вкладка «3D»
# малює VTK. Rust стоїть у X:\rust (CARGO_HOME, RUSTUP_HOME нижче).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:CARGO_HOME = "X:\rust\cargo"
$env:RUSTUP_HOME = "X:\rust\rustup"
$env:PYO3_PYTHON = Join-Path $root ".venv\Scripts\python.exe"
$cargo = Join-Path $env:CARGO_HOME "bin\cargo.exe"

function Build-Pyd([string]$crate) {
    Push-Location (Join-Path $root "rust\$crate")
    try {
        & $cargo build --release
        if ($LASTEXITCODE -ne 0) { throw "cargo build ($crate) failed" }
        $dll = Join-Path (Get-Location) "target\release\$crate.dll"
        $pyd = Join-Path $root "$crate.pyd"
        # Відкрита програма тримає .pyd завантаженим, і перезаписати його
        # Windows не дає. Перейменувати — дає: програма працює далі на старій
        # копії, а новий запуск бере новий файл. Старі копії прибираються,
        # коли їх уже ніхто не тримає.
        Get-ChildItem $root -Filter "$crate.pyd.old-*" -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
        if (Test-Path $pyd) {
            try { Remove-Item $pyd -Force -ErrorAction Stop }
            catch { Rename-Item $pyd ("$crate.pyd.old-" + (Get-Date -Format "yyyyMMddHHmmss")) }
        }
        Copy-Item $dll $pyd -Force
        Write-Host "$crate.pyd -> $root"
    }
    finally {
        Pop-Location
    }
}

Build-Pyd "foamrs"
Build-Pyd "foamview"
Build-Pyd "meshfix"

# foamdist — ЕКСПЕРИМЕНТАЛЬНО: один прогін на кількох машинах. Збирається
# під Linux (статичний musl, лінкер rust-lld з самого Rust); програма сама
# кладе його на сервер. Ціль: rustup target add x86_64-unknown-linux-musl.
Push-Location (Join-Path $root "rust\foamdist")
try {
    & $cargo build --release --target x86_64-unknown-linux-musl
    if ($LASTEXITCODE -ne 0) { throw "cargo build (foamdist) failed" }
    Write-Host "foamdist -> rust\foamdist\target\x86_64-unknown-linux-musl\release\foamdist"
}
finally {
    Pop-Location
}
