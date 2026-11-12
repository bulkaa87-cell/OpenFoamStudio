@echo off
rem Start MeshFix Studio (mesh repair) with the .venv of OpenFOAM Studio.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo .venv not found. Make it with:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "fs_meshfix_app.py" %*
