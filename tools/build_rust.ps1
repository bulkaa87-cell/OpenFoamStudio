# Зібрати Rust-частини програми:
#   foamrs   -> foamrs.pyd   (швидке читання полів, лінії на тілі)
#   foamdist -> Linux-бінарник для сервера (розподіл між машинами)
#
# Ремонт (meshfix) і 3D-перегляд (foamview) — спільні пакунки MeshFix Studio
# (fs_shared.py): їх ставить requirements.txt, тут вони не збираються. Для
# розробки: python tools\build_wheels.py --develop у клоні MeshFix Studio з
# VIRTUAL_ENV=<ця тека>\.venv. Старі meshfix.pyd / foamview.pyd у корені
# закривали б пакунки: скрипт перейменовує їх на *.pyd.old-<час>.
#
#   powershell -ExecutionPolicy Bypass -File tools\build_rust.ps1
#
# Програма працює і без foamrs: fs_fast.py тоді читає через VTK.
# Rust стоїть у X:\rust (CARGO_HOME, RUSTUP_HOME нижче).

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

# Старі модулі попередніх версій: вони мали імʼя пакунків meshfix і foamview.
foreach ($old in @("meshfix", "foamview")) {
    $pyd = Join-Path $root "$old.pyd"
    if (Test-Path $pyd) {
        Rename-Item $pyd ("$old.pyd.old-" + (Get-Date -Format "yyyyMMddHHmmss"))
        Write-Host "$old.pyd -> $old.pyd.old-* (now the package $old)"
    }
}

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
