# ============================================================
#  edge_auto_capture.py を単一EXE(フォルダ形式)へビルドする。
#  Python 未導入の Windows PC へ配布できる形にまとめる。
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "[1/3] ビルド用の依存を確認します (pyinstaller / playwright) ..."
pip install pyinstaller playwright
if ($LASTEXITCODE -ne 0) { Write-Host "依存のインストールに失敗しました。" -ForegroundColor Red; exit 1 }

Write-Host "[2/3] PyInstaller でビルドします ..."
# --collect-all playwright : Playwright の node ドライバを同梱（凍結時に必須）
# --console                : print 表示と Ctrl+C 停止のためコンソールを残す
pyinstaller --noconfirm --onedir --console --name edge_auto_capture --collect-all playwright edge_auto_capture.py
if ($LASTEXITCODE -ne 0) { Write-Host "ビルドに失敗しました。上のメッセージを確認してください。" -ForegroundColor Red; exit 1 }

Write-Host "[3/3] config.ini と説明書を配布フォルダへ同梱します ..."
$dist = Join-Path $PSScriptRoot "dist\edge_auto_capture"
Copy-Item -Force (Join-Path $PSScriptRoot "config.ini")      (Join-Path $dist "config.ini")
Copy-Item -Force (Join-Path $PSScriptRoot "README_dist.txt") (Join-Path $dist "README.txt")

Write-Host ""
Write-Host "完了: dist\edge_auto_capture\ フォルダを ZIP にして配布してください。" -ForegroundColor Green