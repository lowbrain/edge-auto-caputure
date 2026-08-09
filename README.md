# edge-auto-capture

Microsoft Edge で開いたページの **URL / タブが変わるたびに**、フルページの
スクリーンショット(`.png`)とページ全文テキスト(`.txt`)を自動保存するツール。
Playwright が毎回まっさらな一時プロファイルで Edge を起動・監視し、終了時に掃除する。

## 動作条件

- Windows
- Microsoft Edge がインストール済み（`channel="msedge"` でシステムの Edge を使う）
- 開発・ビルド時のみ Python 3.8+（配布した exe の実行に Python は不要）

## リポジトリ構成

```
edge-auto-capture/
├─ edge_auto_capture.py   本体（単一モジュール）
├─ config.ini            既定の設定ファイル
├─ pyproject.toml        依存とパッケージ設定
├─ README.md             このファイル（開発者向け）
├─ USAGE.txt             配布物(exe)に同梱する利用者向けの使い方
└─ build.ps1             配布用 exe のビルド（PyInstaller）
```

生成物（`build/` `dist/` `output/` `__pycache__/` `*.spec`）は Git 管理外。

## 開発時の実行

```bash
pip install -e .
python edge_auto_capture.py
```

同じフォルダの `config.ini` で挙動を設定する。Edge を普通に閲覧すると、
URL/タブの変化ごとに `output\` へ保存される。停止は Ctrl+C か Edge を閉じる。

### 設定（config.ini）

| キー | 意味 |
|------|------|
| `start_url` | 起動時に最初に開くページ（空なら about:blank） |
| `edge_path` | Edge 実行ファイルのパス（空なら自動検出） |
| `output_dir` | 保存先。相対なら本体/exe と同じ場所基準、絶対パスも可 |
| `poll_interval` | URL 変化を確認する間隔（秒） |
| `settle_delay` | 変化検知後、描画が落ち着くまで待つ秒数 |
| `load_timeout` | ページ読み込み待ちの上限（ミリ秒） |
| `skip_urls` | 撮らない URL（カンマ区切り） |
| `target_selector` | 一部抜き出しの CSS セレクタ（空ならスキップ） |

## 配布用 exe のビルド

Python 未導入の Windows PC でも動く、単一 EXE（フォルダ形式）を作る。

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

PyInstaller が `dist\edge-auto-capture\` を生成し、`config.ini` と
`USAGE.txt` を exe の隣へ同梱する。

### 配布方法

`dist\edge-auto-capture\` を **フォルダごと ZIP** にして配る。中身:

```
edge-auto-capture\
├─ edge-auto-capture.exe   ダブルクリックで起動
├─ config.ini              利用者が編集可能
├─ USAGE.txt               利用者向けの使い方
├─ _internal\              ランタイム + playwright ドライバ（必須・触らない）
└─ output\                 実行後に生成される保存先
```

> `_internal\` は動作に必須。exe だけ取り出しても動かない。
> 未署名 exe のため初回に Windows SmartScreen 警告が出る場合がある
> （「詳細情報」→「実行」で起動可能）。
