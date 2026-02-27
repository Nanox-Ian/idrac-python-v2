@echo off
REM ---------------------------------------------
REM Idrac Flask App - Offline 24/7 Deployment Script
REM ---------------------------------------------

SET PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

:START_APP
echo ---------------------------------------------
echo Starting iDRAC Flask app at %DATE% %TIME%
echo ---------------------------------------------
echo.

REM Activate virtual environment
CALL venv\Scripts\activate.bat
IF ERRORLEVEL 1 (
    echo [ERROR] Could not activate virtual environment.
    pause
    exit /b 1
)

REM Install dependencies from wheels (offline)
echo Installing dependencies from wheels (offline)...
python -m pip install --upgrade pip
python -m pip install --no-index --find-links=.\wheels -r requirements.txt

REM Load environment variables from .env if it exists
IF EXIST ".env" (
    echo Loading environment variables from .env
    for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
        set %%A=%%B
    )
)

REM Run app and log output
echo Running Flask app via Waitress...
python app.py >> idrac_app.log 2>&1

echo.
echo [WARNING] App crashed or exited at %DATE% %TIME%. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto START_APP
