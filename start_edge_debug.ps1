# ============================================================
#  edge_auto_capture.py 用ランチャ (PowerShell 版)
#  - 毎回まっさらな一時プロファイルを使用（サインイン/同期ダイアログ回避）
#  - 起動時に前回までの一時プロファイルを掃除
#  - デバッグポートの待機後にキャプチャスクリプトを実行
#  - Ctrl+C で停止すると、このランチャが起動した Edge も終了し
#    一時プロファイルを削除する
#
#  実行方法:
#    powershell -ExecutionPolicy Bypass -File .\start_edge_debug.ps1
#  もしくはファイルを右クリック →「PowerShell で実行」。
#  付属の run.bat をダブルクリックしてもよい。
# ============================================================

$ErrorActionPreference = 'Stop'

# このスクリプトのあるフォルダで実行する（output\ をここに作るため）
Set-Location -Path $PSScriptRoot

$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

# config.ini から起動ページ(start_url)を読む（無指定なら about:blank）
# PowerShell には INI パーサが無いため、[capture] セクションを簡易的に行解析する
$startUrl = 'about:blank'
$cfg = Join-Path $PSScriptRoot 'config.ini'
if (Test-Path $cfg) {
    $inCapture = $false
    foreach ($line in Get-Content -Path $cfg -Encoding UTF8) {
        $t = $line.Trim()
        if ($t -match '^\[(.+)\]$') { $inCapture = ($Matches[1] -eq 'capture'); continue }
        if (-not $inCapture) { continue }
        if ($t -match '^\s*[#;]') { continue }
        if ($t -match '^start_url\s*=\s*(.*)$') {
            $v = $Matches[1].Trim()
            if ($v) { $startUrl = $v }
            break
        }
    }
}
Write-Host "Start page: $startUrl"

# 前回までの一時プロファイルを掃除（使用中のフォルダは自動的にスキップ）
Get-ChildItem -Path $env:TEMP -Directory -Filter 'edge-debug-*' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 今回用の一時プロファイル（末尾の乱数フォルダ名で自分の Edge を後で識別する）
$tmp  = Join-Path $env:TEMP ("edge-debug-" + (Get-Random))
$leaf = Split-Path $tmp -Leaf

$edgeArgs = @(
    '--remote-debugging-port=9222'
    "--user-data-dir=$tmp"
    '--no-first-run'
    '--no-default-browser-check'
    '--disable-sync'
    '--disable-features=msImplicitSignin'
    $startUrl
)
Start-Process -FilePath $edge -ArgumentList $edgeArgs

# デバッグポートが接続可能になるまで待機（最大約30秒）
Write-Host 'Waiting for Edge debug port 9222 ...'
$deadline = (Get-Date).AddSeconds(30)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', 9222)
        $client.Close()
        $ready = $true
        break
    } catch {
        # まだポートが開いていない。少し待って再試行する
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    Write-Host 'Timed out waiting for Edge. Aborting.'
    exit 1
}

Write-Host 'Edge is ready. Starting capture (Ctrl+C to stop) ...'
try {
    # フォアグラウンドで実行。Ctrl+C でキャプチャを停止する
    python edge_auto_capture.py
} finally {
    # Ctrl+C / 正常終了のどちらでもここが走る。
    # このランチャが起動した Edge だけを user-data-dir で識別して終了する
    Write-Host ''
    Write-Host 'Stopping Edge ...'
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$leaf*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    # 一時プロファイルを削除（ロックが残っていても次回起動時に掃除される）
    Start-Sleep -Milliseconds 500
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
