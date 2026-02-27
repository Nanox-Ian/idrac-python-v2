@echo off
REM ---------------------------------------------
REM Idrac Flask App - Offline Deployment Script
REM ---------------------------------------------

REM Set project root (assumes this BAT is in the project folder)
SET PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

REM 1. Activate virtual environment
CALL venv\Scripts\activate.bat
IF ERRORLEVEL 1 (
    echo [ERROR] Could not activate virtual environment. Make sure venv exists.
    pause
    exit /b 1
)

REM 2. Install dependencies from wheels (offline)
echo Installing dependencies from wheels...
python -m pip install --upgrade pip
python -m pip install --no-index --find-links=.\wheels -r requirements.txt

REM 3. Load environment variables from .env if it exists
IF EXIST ".env" (
    echo Loading environment variables from .env
    for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
        set %%A=%%B
    )
)

REM 4. Start the Flask app via Waitress
echo Starting iDRAC Flask app...
python app.py

REM 5. Keep window open (optional)
pause