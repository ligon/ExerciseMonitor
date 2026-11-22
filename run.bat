@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Stelo + Garmin 24/7 Metabolic ^& Exercise Monitor
echo ========================================================

:: If cloned via Git, automatically pull latest updates on launch
if exist ".git" (
    where git >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        echo [Update] Pulling latest updates via Git...
        git pull --ff-only
    )
) else (
    if exist ".venv\Scripts\python.exe" (
        for /f "delims=" %%C in ('".venv\Scripts\python.exe" -c "import certifi; print(certifi.where())" 2^>nul') do set SSL_CERT_FILE=%%C
        ".venv\Scripts\python.exe" update.py
    )
)

:: Locate Python (python or py -3)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set PY_CMD=python
) else (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set PY_CMD=py -3
    ) else (
        echo [ERROR] Python 3 is not installed or not in PATH.
        echo Install Python 3.11+ from https://www.python.org/downloads/
        echo ^(Make sure to check "Add python.exe to PATH" during installation^).
        pause
        exit /b 1
    )
)

:: Create virtual environment on first run
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating local Python virtual environment ^(.venv^)...
    %PY_CMD% -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [2/3] Installing required packages...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\pip.exe" install -r requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Package installation failed.
        pause
        exit /b 1
    )
) else (
    ".venv\Scripts\pip.exe" install -q -r requirements.txt
)

echo [3/3] Starting dashboard at http://localhost:8000 ...
:: Stop any previous ExerciseMonitor server still holding port 8000
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%P >nul 2>nul
)
start "" "http://localhost:8000/?v=%RANDOM%"
".venv\Scripts\uvicorn.exe" app.main:app --host 127.0.0.1 --port 8000
pause
