# ============================================================
#  edge_auto_capture.py を単一EXE(フォルダ形式)へビルドする。
#  Python 未導入の Windows PC へ配布できる形にまとめる。
#  実行例: powershell -ExecutionPolicy Bypass -File build.ps1
#
#  任意でコードサイニング署名も行える（証明書が無ければ自動で素通り）:
#    # PFX ファイルで署名する場合
#    powershell -ExecutionPolicy Bypass -File build.ps1 -CertPath cert.pfx -CertPassword ****
#    # 証明書ストア上の証明書を拇印で指定する場合（EV トークン等）
#    powershell -ExecutionPolicy Bypass -File build.ps1 -CertThumbprint <THUMBPRINT>
# ============================================================
param(
    # 署名用の設定（未指定なら署名ステップは丸ごと素通りする＝現状どおり未署名で配布）。
    [string]$CertPath = "",                                   # PFX ファイルのパス
    [string]$CertPassword = "",                               # PFX のパスワード（任意）
    [string]$CertThumbprint = "",                             # 証明書ストア上の証明書拇印（PFX の代わり）
    [string]$TimestampUrl = "http://timestamp.digicert.com"   # タイムスタンプ サーバ
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot   # このスクリプトのあるフォルダ＝プロジェクトルート
Set-Location -Path $root

Write-Host "[1/6] ビルド用の依存を確認します (pyinstaller / playwright) ..."
# 依存は pyproject に一本化する（版の二重管理を避ける）。
#   playwright  : base の dependencies（>=1.60,<2 の範囲指定が効く）
#   pyinstaller : [build] extras
pip install -e ".[build]"
if ($LASTEXITCODE -ne 0) { Write-Host "依存のインストールに失敗しました。" -ForegroundColor Red; exit 1 }

Write-Host "[2/6] exe プロパティ用のバージョン情報ファイルを生成します ..."
# 右クリック→プロパティ→詳細でバージョンが見えるようにする（--version-file）。
# 版の出所は infra.__version__ に一本化（D-B1）。ここで直書きせず Python から取り出す。
$ver = (python -c "import infra; print(infra.__version__)")
if ($LASTEXITCODE -ne 0) { Write-Host "バージョンの取得に失敗しました。" -ForegroundColor Red; exit 1 }
$ver = $ver.Trim()
# filevers/prodvers は 4 個の整数タプル。x.y.z を (x, y, z, 0) に整える（欠けは 0 埋め）。
$parts = @($ver.Split("."))
while ($parts.Count -lt 3) { $parts += "0" }
$fv = "$($parts[0]), $($parts[1]), $($parts[2]), 0"
$verFile = Join-Path $root "build\version_info.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $verFile) | Out-Null
# PyInstaller が読む VSVersionInfo（Python 構文で eval される）。ASCII のみで書く
# （文字コード差による化けを避ける）。langID 0409=US English / codepage 04B0=Unicode。
$verContent = @"
# 自動生成（build.ps1）。編集しても次回ビルドで上書きされる。版の出所は infra.__version__。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($fv),
    prodvers=($fv),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Auto-capture screenshots and page text on Edge/Chrome URL or tab changes'),
      StringStruct('FileVersion', '$ver'),
      StringStruct('InternalName', 'edge-auto-capture'),
      StringStruct('OriginalFilename', 'edge-auto-capture.exe'),
      StringStruct('ProductName', 'edge-auto-capture'),
      StringStruct('ProductVersion', '$ver')
    ])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
  ]
)
"@
Set-Content -Encoding ascii -Path $verFile -Value $verContent
Write-Host "  バージョン $ver を埋め込みます: $verFile" -ForegroundColor Green

Write-Host "[3/6] PyInstaller でビルドします ..."
# --collect-all playwright : Playwright の node ドライバを同梱（凍結時に必須）
# --noconsole              : 黒いコンソール窓を出さない（動作ログは log.txt に出力）
# --add-data "badge.js;."  : 操作バーのページ側 JS を同梱（badge.py が _MEIPASS から読む）
# --version-file           : exe プロパティへ版を埋め込む（上で生成した version_info.txt）
#   ※ badge.py / capture.py / config.py / infra.py は import から自動で辿られるので指定不要。
pyinstaller --noconfirm --onedir --noconsole --name edge-auto-capture --collect-all playwright --add-data "badge.js;." --version-file "$verFile" edge_auto_capture.py
if ($LASTEXITCODE -ne 0) { Write-Host "ビルドに失敗しました。上のメッセージを確認してください。" -ForegroundColor Red; exit 1 }

$dist = Join-Path $root "dist\edge-auto-capture"
$exe  = Join-Path $dist "edge-auto-capture.exe"

Write-Host "[4/6] config.ini と説明書、依存ライセンス表記を配布フォルダへ同梱します ..."
Copy-Item -Force (Join-Path $root "config.ini") (Join-Path $dist "config.ini")
Copy-Item -Force (Join-Path $root "USAGE.txt")  (Join-Path $dist "USAGE.txt")
if (Test-Path (Join-Path $root "LICENSE")) {
    Copy-Item -Force (Join-Path $root "LICENSE") (Join-Path $dist "LICENSE.txt")
}
# D-A2: 同梱する依存（Playwright など）のライセンス表記をまとめて配布物へ入れる。
# pip-licenses が無ければ導入を試み、失敗しても配布自体は止めない（警告のみ）。
$notices = Join-Path $dist "THIRD-PARTY-NOTICES.txt"
pip install pip-licenses *> $null
if ($LASTEXITCODE -eq 0) {
    pip-licenses --format=plain-vertical --with-license-file --no-license-path --with-urls |
        Out-File -Encoding utf8 $notices
    Write-Host "  依存ライセンス表記を生成しました: $notices" -ForegroundColor Green
} else {
    Write-Host "  [warn] pip-licenses を用意できず THIRD-PARTY-NOTICES.txt を生成できませんでした。" -ForegroundColor Yellow
    Write-Host "         手動で依存ライセンスを $notices にまとめてください。" -ForegroundColor Yellow
}

Write-Host "[5/6] コードサイニング署名 ..."
# D-D1: 署名の受け口。証明書の指定が無ければ何もせず素通りする（現状どおり未署名）。
#       証明書を入手したら -CertPath か -CertThumbprint を渡すだけで署名できる。
if ($CertPath -ne "" -or $CertThumbprint -ne "") {
    $signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signtool) {
        Write-Host "  [error] signtool.exe が見つかりません（Windows SDK が必要）。署名をスキップします。" -ForegroundColor Red
    } else {
        $signArgs = @("sign", "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")
        if ($CertPath -ne "") {
            $signArgs += @("/f", $CertPath)
            if ($CertPassword -ne "") { $signArgs += @("/p", $CertPassword) }
        } else {
            $signArgs += @("/sha1", $CertThumbprint)
        }
        $signArgs += $exe
        & $signtool.Source @signArgs
        if ($LASTEXITCODE -ne 0) { Write-Host "署名に失敗しました。" -ForegroundColor Red; exit 1 }
        Write-Host "  署名しました: $exe" -ForegroundColor Green
    }
} else {
    Write-Host "  証明書の指定が無いため署名をスキップしました（未署名で配布）。" -ForegroundColor Yellow
}

Write-Host "[6/6] 配布用 ZIP と SHA256 を作成します ..."
# D-D3: 配布 ZIP と、その SHA256 を併記する（署名が無い間の完全性確認手段）。
$zip    = Join-Path $root "dist\edge-auto-capture.zip"
$sha    = "$zip.sha256"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $dist -DestinationPath $zip
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
# 「<ハッシュ>  <ファイル名>」形式（sha256sum -c と同じ並び）で残す。
"$hash  $(Split-Path -Leaf $zip)" | Out-File -Encoding ascii $sha
Write-Host "  ZIP:    $zip"
Write-Host "  SHA256: $hash" -ForegroundColor Green

Write-Host ""
Write-Host "完了: dist\edge-auto-capture.zip を配布してください（SHA256 は $sha）。" -ForegroundColor Green
