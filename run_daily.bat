@echo off
rem Windowsタスクスケジューラ用: 毎日実行ラッパー(ログは logs\task.log)
cd /d "%~dp0"
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> logs\task.log
set MERCARI_NOTIFY=1
python mercari_ads_export.py >> logs\task.log 2>&1
echo exit code: %errorlevel% >> logs\task.log
