@echo off
chcp 65001 >nul
setlocal
if not exist .venv\Scripts\activate.bat (
  echo [UYARI] .venv yok. Once 1_KURULUM.bat calistirin.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
cmd /k
