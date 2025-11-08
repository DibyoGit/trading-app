@echo off
echo Starting Trading Application...
echo.
echo Choose an option:
echo 1. Run with Docker (Recommended)
echo 2. Run locally with Python
echo.
set /p choice="Enter your choice (1 or 2): "

if "%choice%"=="1" (
    echo Building and starting Docker container...
    docker-compose up --build
) else if "%choice%"=="2" (
    echo Installing dependencies...
    pip install -r requirements.txt
    echo Starting Flask application...
    python app.py
) else (
    echo Invalid choice. Please run the script again.
    pause
)