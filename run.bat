@echo off
rem ============================================================
rem  edge_auto_capture.py のダブルクリック起動用ラッパ
rem  bat のあるフォルダへ移動してから Python を実行する
rem  （output\ をこのフォルダに作るため）。
rem ============================================================
cd /d "%~dp0"
python edge_auto_capture.py
pause