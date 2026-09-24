@echo off
REM Activate the local virtual environment and run the VenTapes learning fork.

cd /d "%~dp0"

if not exist .venv\Scripts\activate.bat (
    echo Missing .venv. Create it and install requirements-windows.txt first. 1>&2
    exit /b 1
)

call .venv\Scripts\activate.bat

where glib-compile-resources >nul 2>&1
if errorlevel 1 (
    echo Warning: glib-compile-resources is unavailable; some bundled action icons may be missing. 1>&2
) else (
    glib-compile-resources --sourcedir=. src\ventapes.gresource.xml --target=src\ventapes.gresource
)

python src\main.py %*
