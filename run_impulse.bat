@echo off
chcp 65001 >nul
cd /d "D:\Vcode\TRD bot"
python run_impulse_once.py > run_impulse_output.txt 2>&1
