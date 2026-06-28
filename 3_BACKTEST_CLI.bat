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
echo GUI olmadan hizli backtest (son 30 gun, 1h, top 20)...
python backtest.py --days 30 --interval 1h --top 20 --out backtest_results/cli_son --president-mode live
pause
