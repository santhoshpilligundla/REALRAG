@echo off
echo === RealRAG Setup ===

echo.
echo Step 1: Creating Python virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11 from https://python.org
    pause
    exit /b 1
)

echo.
echo Step 2: Installing dependencies...
.venv\Scripts\pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo Step 3: Setting up .env file...
if not exist .env (
    copy .env.example .env
    echo .env created from .env.example
    echo IMPORTANT: Open .env and add your ANTHROPIC_API_KEY and OPENAI_API_KEY
) else (
    echo .env already exists, skipping.
)

echo.
echo Step 4: Initializing database...
.venv\Scripts\python scripts\dev_up.py
if errorlevel 1 (
    echo WARNING: dev_up.py failed. You may need to add API keys to .env first.
)

echo.
echo === Setup complete! ===
echo.
echo Next steps:
echo   1. Open .env and add your ANTHROPIC_API_KEY and OPENAI_API_KEY
echo   2. Run the app: run_realrag.bat
echo.
pause
