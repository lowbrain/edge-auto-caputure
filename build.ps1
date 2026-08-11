# ============================================================
#  edge_auto_capture.py を単一EXE(フォルダ形式)へビルドする。
#  Python 未導入の Windows PC へ配布できる形にまとめる。
#  実行例: powershell -ExecutionPolicy Bypass -File build.ps1
# ============================================================
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot   # このスクリプトのあるフォルダ＝プロジェクトルート
Set-Location -Path $root

Write-Host "[1/3] ビルド用の依存を確認します (pyinstaller / playwright) ..."
pip install pyinstaller playwright
if ($LASTEXITCODE -ne 0) { Write-Host "依存のインストールに失敗しました。" -ForegroundColor Red; exit 1 }

Write-Host "[2/3] PyInstaller でビルドします ..."
# --collect-all playwright : Playwright の node ドライバを同梱（凍結時に必須）
# --noconsole              : 黒いコンソール窓を出さない（動作ログは log.txt に出力）
# --add-data "badge.js;."  : 操作バーのページ側 JS を同梱（badge.py が _MEIPASS から読む）
#   ※ badge.py / capture.py / config.py / infra.py は import から自動で辿られるので指定不要。
pyinstaller --noconfirm --onedir --noconsole --name edge-auto-capture --collect-all playwright --add-data "badge.js;." edge_auto_capture.py
if ($LASTEXITCODE -ne 0) { Write-Host "ビルドに失敗しました。上のメッセージを確認してください。" -ForegroundColor Red; exit 1 }

Write-Host "[3/3] config.ini と説明書を配布フォルダへ同梱します ..."
$dist = Join-Path $root "dist\edge-auto-capture"
Copy-Item -Force (Join-Path $root "config.ini")                (Join-Path $dist "config.ini")
Copy-Item -Force (Join-Path $root "USAGE.txt")                 (Join-Path $dist "USAGE.txt")

Write-Host ""
Write-Host "完了: dist\edge-auto-capture\ フォルダを ZIP にして配布してください。" -ForegroundColor Green