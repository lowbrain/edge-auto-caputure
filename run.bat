@echo off
rem ============================================================
rem  edge_auto_capture.py のダブルクリック起動用ランチャ。
rem  この bat と同じフォルダで Python を実行する
rem  （config.ini と output\ をこのフォルダ基準にするため）。
rem ============================================================
cd /d "%~dp0"
python edge_auto_capture.py
pause