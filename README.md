# edge-auto-capture

Microsoft Edge で開いたページを、**記録ONの間だけ**、フルページのスクリーンショット(`.png`)と
ページ全文テキスト(`.txt`)へ自動保存するツール。CSS セレクタを指定すれば、ページの一部だけを
抜き出したテキスト(`_part.txt`)も保存できる。

キャプチャのタイミングは利用者が操作する。各ページ上部の操作バーで次を行える。

- **記録開始／停止** … 記録期間の制御（記録ON中は URL / タブが変わるたびに自動保存）
- **今すぐ1枚** … 記録ON/OFF に関わらず、今のページを1回だけ保存
- **CSSセレクタ入力** … 一部抜き出し(`_part.txt`)／SPA検知の対象要素を実行時に指定（右に「一致 N件」、ホバーで調べ方を表示）
- **SPA検知トグル** … 下記参照（セレクタを入れると操作可能になる）

既定は記録OFF（待機）で起動する（`start_recording` で変更可）。
Playwright が毎回まっさらな一時プロファイルで Edge を起動・監視し、終了時に掃除する。

## SPA 対応（中身だけ変わるページの自動保存）

URL が変わらず中身だけ変わる SPA（タブ切替・フィルタ・検索結果の差し替え等）は、通常の
URL 変化検知では撮れない。**CSSセレクタ入力**に監視したい要素のセレクタを入れて
**SPA検知**を ON にすると、記録ON中はその要素の中身が変わって落ち着くたびに自動保存する。

- 同じ内容は**コンテンツ署名（ハッシュ）比較で重複除外**する。
- SPA検知は**記録ON中のみ**有効（記録がマスタースイッチ）。
- セレクタが空だと SPA検知トグルは無効（対象が無いため）。
- 対象セレクタは `_part.txt` の抜き出し対象も兼ねる（初期値は `config.ini` の `target_selector`）。
- 広すぎるセレクタ（`body` 等）は小さな変化まで拾って撮りすぎるので、`#id` や `.class` で絞るのが安全。

## 動作条件

- Windows
- Microsoft Edge がインストール済み（`channel="msedge"` でシステムの Edge を使う）
- 開発・ビルド時のみ Python 3.8+（配布した exe の実行に Python は不要）

## リポジトリ構成

役割ごとに分割している。ページ側 JS は実ファイル `badge.js` に置き、エディタ/リンタで
構文検査できるようにしてある。

```
edge-auto-capture/
├─ edge_auto_capture.py   エントリ＋監視セッション（CaptureSession）
├─ capture.py             設定読み込み・1ページ分の保存処理・基盤ユーティリティ
├─ badge.py               操作バーのページ側JS組み立て（表示文言→$CONFIG に集約）
├─ badge.js               操作バーのページ側JS本体（実ファイル）
├─ tests/
│  └─ smoke_badge.py      操作バーJSのスモークテスト（Edge headless で構築確認）
├─ config.ini             既定の設定ファイル
├─ pyproject.toml         依存とパッケージ設定
├─ README.md              このファイル（開発者向け）
├─ USAGE.txt              配布物(exe)に同梱する利用者向けの使い方（Shift-JIS）
└─ build.ps1             配布用 exe のビルド（PyInstaller）
```

生成物（`build/` `dist/` `output/` `__pycache__/` `*.spec`）は Git 管理外。

## 開発時の実行

```bash
pip install -e .
python edge_auto_capture.py
```

同じフォルダの `config.ini` で挙動を設定する。Edge を普通に閲覧し、バーの「記録開始」を
押すと、以後 URL/タブの変化ごとに `output\` へ保存される（「記録停止」で一時停止、
「今すぐ1枚」で今のページを単発保存）。停止は Ctrl+C か Edge を閉じる。

### 設定（config.ini）

| キー | 意味 |
|------|------|
| `start_url` | 起動時に最初に開くページ（空なら about:blank） |
| `edge_path` | Edge 実行ファイルのパス（空なら自動検出） |
| `output_dir` | 保存先。相対なら本体/exe と同じ場所基準、絶対パスも可 |
| `poll_interval` | URL 変化・中身変化を確認する間隔（秒） |
| `settle_delay` | 変化検知後、描画が落ち着くまで待つ秒数 |
| `load_timeout` | ページ読み込み待ちの上限（ミリ秒） |
| `skip_urls` | 撮らない URL（カンマ区切り） |
| `target_selector` | 一部抜き出し／SPA検知の対象 CSS セレクタの初期値（バーで実行時に変更可・空可） |
| `start_recording` | 起動直後に記録を開始するか（`false`=待機で起動、`true`=起動時から記録ON） |

### テスト

操作バーの JS（`badge.js`）が実際に構築でき、ページ側ヘルパが例外なく動くかを、
システムの Edge を headless で使って確認する。

```bash
python tests/smoke_badge.py
```

`PASS`（終了コード 0）で正常。以前混入した JS 構文エラー（`render` 引数と `st` の
二重宣言）のような不具合を、実行前に自動検出することを狙ったもの。

## 配布用 exe のビルド

Python 未導入の Windows PC でも動く、単一 EXE（フォルダ形式）を作る。

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

PyInstaller が `dist\edge-auto-capture\` を生成し、`config.ini` と `USAGE.txt` を
exe の隣へ同梱する。ページ側 JS の `badge.js` は `--add-data` で `_internal\` に同梱され、
実行時は `sys._MEIPASS` から読み込まれる（`badge.py` `capture.py` は import から自動で辿られる）。

### 配布方法

`dist\edge-auto-capture\` を **フォルダごと ZIP** にして配る。中身:

```
edge-auto-capture\
├─ edge-auto-capture.exe   ダブルクリックで起動
├─ config.ini              利用者が編集可能
├─ USAGE.txt               利用者向けの使い方
├─ _internal\              ランタイム + playwright ドライバ + badge.js（必須・触らない）
└─ output\                 実行後に生成される保存先
```

> `_internal\` は動作に必須。exe だけ取り出しても動かない。
> 未署名 exe のため初回に Windows SmartScreen 警告が出る場合がある
> （「詳細情報」→「実行」で起動可能）。
