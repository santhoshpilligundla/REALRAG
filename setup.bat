@echo off
cd /d %~dp0
echo === RealRAG Setup ===

echo.
echo Step 1: Checking Python installation...
where py >nul 2>&1
if errorlevel 1 (
    where python >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python not found. Install Python 3.11 from https://python.org
        echo        Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    ) else (
        set PYTHON=python
    )
) else (
    set PYTHON=py -3.11
)
echo Python found: %PYTHON%

echo.
echo Step 2: Creating Python virtual environment...
%PYTHON% -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

echo.
echo Step 3: Installing dependencies...
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Step 4: Setting up .env file...
if not exist .env (
    copy .env.example .env
    echo .env created. Open it and add your ANTHROPIC_API_KEY and OPENAI_API_KEY
) else (
    echo .env already exists, skipping.
)

echo.
echo === Setup complete! ===
echo.
echo Next steps:
echo   1. Open .env and add your ANTHROPIC_API_KEY and OPENAI_API_KEY
echo   2. Run the app: run_realrag.bat
echo.
pause
