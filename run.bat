@echo off
rem ============================================================
rem  Double-click wrapper for start_edge_debug.ps1
rem  Runs the PowerShell launcher with ExecutionPolicy bypassed,
rem  so it works from a double-click without changing system policy.
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_edge_debug.ps1"
