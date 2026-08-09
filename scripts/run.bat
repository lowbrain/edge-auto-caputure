@echo off
rem ============================================================
rem  edge_auto_capture.py のダブルクリック起動用ランチャ。
rem  scripts\ にあるため、親フォルダ(プロジェクトルート)へ移動して
rem  Python を実行する（config.ini と output\ をルート基準にするため）。
rem ============================================================
cd /d "%~dp0.."
python edge_auto_capture.py
pause