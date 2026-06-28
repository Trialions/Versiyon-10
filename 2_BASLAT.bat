@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist .venv\Scripts\python.exe (
  echo [UYARI] .venv bulunamadi. Once 1_KURULUM.bat calistiriliyor...
  call 1_KURULUM.bat
)
call .venv\Scripts\activate.bat
echo TRBOT President System sanal ortamdan baslatiliyor...
python app.py
pause
