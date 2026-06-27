@echo off
setlocal

cd /d "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

if exist "%CD%\.venv\Scripts\python.exe" (
    "%CD%\.venv\Scripts\python.exe" -m cueforge %*
    goto :done
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -m cueforge %*
    goto :done
)

python -m cueforge %*

:done
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo CueForge exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
