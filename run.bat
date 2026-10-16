@echo off
rem ShieldLang launcher (Windows)
cd /d "%~dp0"
echo ShieldLang
echo   [1] Open ShieldLang Studio (web UI)
echo   [2] Compile examples\payments.shl (security report)
echo   [3] Run the test suite
set /p c="Choose [1]: "
if "%c%"=="2" (python shieldlang.py compile examples\payments.shl --emit report & pause & exit /b)
if "%c%"=="3" (python shieldlang.py test & pause & exit /b)
py -3 shieldlang.py serve --port 8090
