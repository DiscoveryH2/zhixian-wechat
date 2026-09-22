@echo off
cd /d "%~dp0"
if exist "outputs\build\Zhixian\Zhixian.exe" (
  start "" "outputs\build\Zhixian\Zhixian.exe"
) else if exist "outputs\Zhixian\Zhixian.exe" (
  start "" "outputs\Zhixian\Zhixian.exe"
) else if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "src\main.py"
) else (
  echo Please use the portable build in outputs\Zhixian.
  pause
)
