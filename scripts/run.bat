@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
pushd "%REPO_ROOT%"

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    ) else (
        where py >nul 2>nul
        if not errorlevel 1 (
            set "PYTHON_EXE=py -3"
        ) else (
            echo Python was not found on PATH.
            echo Install Python 3 or set PYTHON_EXE before running this script.
            popd
            exit /b 1
        )
    )
)

%PYTHON_EXE% -m pip install -r host\requirements.txt
if %ERRORLEVEL% NEQ 0 (
    set "EXIT_CODE=%ERRORLEVEL%"
    popd
    exit /b %EXIT_CODE%
)

%PYTHON_EXE% host\app.py
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
