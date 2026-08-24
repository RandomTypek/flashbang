@echo off
REM ---------------------------------------------------------------------
REM  Flashbang - Windows build
REM
REM  Needs Python 3.9+ from python.org with "Add to PATH" ticked.
REM  Double-click this file. The exes land in dist\.
REM ---------------------------------------------------------------------
setlocal

cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   Python was not found on PATH.
        echo   Install it from https://www.python.org/downloads/windows/
        echo   and tick "Add python.exe to PATH" during setup.
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

echo.
echo   [1/3] Checking tkinter...
%PY% -c "import tkinter" 2>nul
if errorlevel 1 (
    echo.
    echo   Your Python has no tkinter. Re-run the Python installer,
    echo   choose Modify, and tick "tcl/tk and IDLE".
    echo.
    pause
    exit /b 1
)

echo   [2/3] Installing PyInstaller...
%PY% -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 (
    echo   pip failed - check your connection.
    pause
    exit /b 1
)

echo   [3/3] Building...
%PY% -m PyInstaller --clean --noconfirm flashbang.spec
if errorlevel 1 (
    echo.
    echo   Build failed. The log above says why.
    pause
    exit /b 1
)

echo.
echo   Done.
echo     dist\Flashbang.exe       the GUI
echo     dist\flashbang-cli.exe   the command line version
echo.
echo   Both are self-contained - no Python needed on the target machine.
echo   Drop ruffle.exe next to Flashbang.exe and the "Open in Ruffle"
echo   toggle will find it automatically.
echo.
pause
