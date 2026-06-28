@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo TRBOT President System - Sanal Ortam Kurulum
where python >nul 2>nul
if errorlevel 1 (
  echo [HATA] Python bulunamadi. Python 3.10+ kurup PATH'e ekleyin.
  pause
  exit /b 1
)

if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m compileall -q .
if errorlevel 1 (
  echo [HATA] Kurulum veya compile kontrolu basarisiz.
  pause
  exit /b 1
)
echo.
echo Kurulum tamam. Calistirmak icin 2_BASLAT.bat
pause
