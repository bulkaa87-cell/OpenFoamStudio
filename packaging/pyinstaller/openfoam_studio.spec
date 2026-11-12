# PyInstaller spec of OpenFOAM Studio: one folder (<dist>/OpenFOAM Studio/).
#
#   pyinstaller --noconfirm --distpath <dist> --workpath <work> \
#       packaging/pyinstaller/openfoam_studio.spec
#
# Build the Rust modules first (foamrs.pyd, foamview.pyd, meshfix.pyd in the
# repository root, and the Linux binary rust/foamdist/target/
# x86_64-unknown-linux-musl/release/foamdist). packaging/windows/build.ps1
# does all steps. The program works without the Rust modules too (slower
# field reading, VTK 3D view, no meshfix repair).

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
NAME = "OpenFOAM Studio"

datas = [(os.path.join(ROOT, "i18n"), "i18n"),
         (os.path.join(ROOT, "docs", "INSTALL.en.md"), "docs"),
         (os.path.join(ROOT, "docs", "INSTALL.uk.md"), "docs")]
# The logo (fs_icons reads icons/ next to the modules): window, taskbar.
import glob
for f in glob.glob(os.path.join(ROOT, "icons", "openfoam-studio*.png")):
    datas.append((f, "icons"))
for name in ("NASTANOVA.md", "NASTANOVA.en.md", "SLOVNYK.html", "SLOVNYK.en.html"):
    if os.path.isfile(os.path.join(ROOT, name)):
        datas.append((os.path.join(ROOT, name), "."))
# The Linux binary of the distributed run (fs_backend.FOAMDIST_LOCAL is
# relative to the program folder).
FOAMDIST = os.path.join("rust", "foamdist", "target", "x86_64-unknown-linux-musl",
                        "release", "foamdist")
if os.path.isfile(os.path.join(ROOT, FOAMDIST)):
    datas.append((os.path.join(ROOT, FOAMDIST), os.path.dirname(FOAMDIST)))

binaries = []
hidden = ["pyvistaqt", "vtkmodules.util.numpy_support",
          "vtkmodules.qt.QVTKRenderWindowInteractor"]
for mod in ("foamrs", "foamview", "meshfix"):
    pyd = os.path.join(ROOT, mod + (".pyd" if sys.platform == "win32" else ".so"))
    if os.path.isfile(pyd):
        binaries.append((pyd, "."))
        hidden.append(mod)
    else:
        print(f"WARNING: {pyd} is missing: the program runs without it")
if sys.platform == "win32":
    # The Windows repair service (fs_winfix) imports these at run time.
    hidden += collect_submodules("winrt")

# Big packages that the program does not use. matplotlib stays: VTK (the
# fallback of the 3D view) imports pyvista.plotting, which needs it.
excludes = ["tkinter", "IPython", "jupyter", "notebook", "pytest", "scipy",
            "pandas", "trame", "trame_client", "trame_server", "trame_vtk",
            "trame_vuetify",
            "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
            "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore",
            "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtGraphs",
            "PySide6.QtLocation", "PySide6.QtPositioning",
            "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
            "PySide6.QtSerialPort", "PySide6.QtWebSockets",
            "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSql",
            "PySide6.QtTest", "PySide6.QtQuickWidgets"]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

# The icon of the exe (Explorer, shortcuts): the logo of T33 in icons/.
icon = os.path.join(ROOT, "icons", "openfoam-studio.ico"
                    if sys.platform == "win32" else "openfoam-studio.icns")
if not os.path.isfile(icon):
    icon = None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
