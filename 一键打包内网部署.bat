@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto run_package

where python >nul 2>nul
if errorlevel 1 goto python_missing
set "PYTHON_EXE=python"

:run_package
"%PYTHON_EXE%" tools\build_deployment_package.py --project-root "%CD%"
set "PACKAGE_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %PACKAGE_EXIT%

:python_missing
echo [ERROR] Python not found. Install Python or create .venv first.
echo.
pause
exit /b 1
