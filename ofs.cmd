@echo off
rem ofs: the command line of OpenFOAM Studio (fs_cli.py). Example:
rem   ofs queue list --json
setlocal
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\python.exe" (
    "%HERE%.venv\Scripts\python.exe" "%HERE%fs_cli.py" %*
) else (
    python "%HERE%fs_cli.py" %*
)
exit /b %ERRORLEVEL%
