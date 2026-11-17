@echo off
setlocal

set "MODE=%~1"
if not "%~2"=="" goto usage
if not "%MODE%"=="" if not "%MODE%"=="--setup" if not "%MODE%"=="--check" goto usage

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
pushd "%REPO_ROOT%" || exit /b 1

if exist ".venv\Scripts\python.exe" goto environment_ready
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv .venv
if errorlevel 1 goto finish
goto environment_ready

:use_python
where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.11+ was not found. Install Python and add py or python to PATH. >&2
    goto failed
)
python -m venv .venv
if errorlevel 1 goto finish

:environment_ready
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if "%MODE%"=="--setup" (
    "%PYTHON_EXE%" -m pip install -r host\requirements.txt
    if errorlevel 1 goto finish
)
if not "%MODE%"=="" goto check

if not defined CARGO_TARGET_DIR set "CARGO_TARGET_DIR=%REPO_ROOT%\desktop\src-tauri\target"
cargo build --manifest-path desktop\src-tauri\Cargo.toml
if errorlevel 1 goto finish
set "CAFETERIA_PROJECT_ROOT=%REPO_ROOT%"
"%CARGO_TARGET_DIR%\debug\cafeteria-ticket-ocr.exe"
goto finish

:check
set "PYTHONDONTWRITEBYTECODE=1"
"%PYTHON_EXE%" -B -c "import sys; sys.path.insert(0, 'host'); import recognition; recognition.get_rapidocr_engine(allow_download=sys.argv[1] == '--setup'); problem = recognition.ocr_unavailable_reason(recognition.DEFAULT_OCR_ENGINE); problem and sys.exit('app_check: ' + problem); from ocr_menu import load_menu_items; print(f'app_check: ok (Japanese RapidOCR, {len(load_menu_items(recognition.MENU_DATABASE_PATH))} menu items)')" "%MODE%"

:finish
set "EXIT_CODE=%ERRORLEVEL%"
goto done

:failed
set "EXIT_CODE=1"

:done
popd
exit /b %EXIT_CODE%

:usage
echo Usage: %~nx0 [--setup^|--check] >&2
exit /b 2
