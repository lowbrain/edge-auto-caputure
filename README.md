# edge-auto-capture

Microsoft Edge で開いたページを、**記録ONの間だけ**、フルページのスクリーンショット(`.png`)と
ページ全文テキスト(`.txt`)へ自動保存するツール。CSS セレクタを指定すれば、ページの一部だけを
抜き出したテキスト(`_part.txt`)も保存でき、URL が変わらず中身だけ変わる SPA も検知して撮れる。

> **利用者向けの使い方**（操作バーの操作、SPA検知の使い方、CSSセレクタの入れ方・調べ方、
> トラブル対処など）は **[USAGE.txt](USAGE.txt)** に一本化している。
> この README は**開発・ビルド向け**の情報（仕組み・構成・設定リファレンス・テスト・配布）をまとめる。

## 仕組み（概要）

- 記録ON中、Playwright が `poll_interval` ごとに各ページの URL/タブ変化を検知し、変化ごとに
  capture（`png` / `txt`、セレクタ指定時は `_part.txt`）を走らせる。Edge の起動・監視・終了・
  一時プロファイルの後始末までを一括で行う（毎回まっさらな一時プロファイルで起動）。
- **SPA検知**: 中身変化の検出はページ側（`badge.js`）がイベント駆動で行う。`MutationObserver` で
  DOM 変化を捉え、`settle_delay` ぶん変化が止まって「落ち着いた」ら対象の innerText を短いハッシュ
  （コンテンツ署名）にし、前回保存時と署名が異なるときだけ Python へ通知して保存する（重複除外＋
  描画途中の撮影回避）。監視対象はセレクタ指定時はその要素、未指定時はページ主要部（`main`/`article`、
  無ければ本文全体）。`history.pushState` 等のルート変化はフックして基準を取り直すだけにし、URL変化
  側の1枚と二重に撮らない。記録ONがマスタースイッチで、SPA検知は記録ON中のみ動く。従来の「Python が
  毎 tick 全ページの署名を評価するポーリング」を廃したので、変化が無い間は署名計算が走らない。
- 操作バーは各ページへ `add_init_script` で注入し、**Shadow DOM** の中に作る。サイト側 CSS の
  影響を受けず、`document.querySelectorAll` にも紛れ込まないため、全文/一部抜き出しにバーの
  文言が混ざらない。スクリーンショット撮影の瞬間だけバーを隠すので、保存物（png/txt/_part.txt）
  にも写り込まない。保存が終わるとバーを一瞬フラッシュして「保存した」ことを知らせる。
- バー右端の**「透過」トグル**（枠なしの目アイコン）で、バーを一時的に半透明にして下に隠れた
  ページ内容を確認できる（見た目だけのローカル状態で、記録状態や保存物には影響しない）。

## 動作条件

- Windows
- Microsoft Edge がインストール済み（`channel="msedge"` でシステムの Edge を使う）
- 開発・ビルド時のみ Python 3.8+（配布した exe の実行に Python は不要）

## リポジトリ構成

役割ごとに分割している。依存方向は下向きの一方向で循環なし:
`edge_auto_capture →（capture / config）→ infra`、`capture → badge`、`config → infra`。
`infra` は Playwright 非依存で、`config`（設定読み込み）も同様なので実 Edge 無しでテストできる。
ページ側 JS は実ファイル `badge.js` に置き、エディタ/リンタで構文検査できるようにしてある。

```
edge-auto-capture/
├─ edge_auto_capture.py   エントリ＋監視セッション（CaptureSession）
├─ capture.py             1ページ分の保存処理（撮影実行器 CaptureRunner）・ページ操作ヘルパ
├─ config.py              設定（Config / config.ini の load_config）
├─ infra.py               基盤ユーティリティ（パス・ログ・致命エラー通知・一時プロファイル掃除）
├─ badge.py               操作バーのページ側JS組み立て（表示文言→$CONFIG／バインディング名）
├─ badge.js               操作バーのページ側JS本体（実ファイル）
├─ tests/
│  ├─ smoke_badge.py         操作バーJS＋SPA検知監視のスモークテスト（Edge headless）
│  ├─ test_capture.py        純粋関数（capture）・設定読み込み（config）のユニットテスト（pytest）
│  ├─ test_session_auth.py   合言葉(token)照合のユニットテスト（pytest）
│  └─ conftest.py            pytest 共通設定
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

挙動は同じフォルダの `config.ini` で設定する（下表）。実際の操作方法は `USAGE.txt` を参照。
停止は Ctrl+C か Edge のウィンドウを閉じる。

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

### CSSセレクタ（実装上の注意）

`target_selector`（＝バーの入力値）は 2 か所で使う。用途で参照する API が違う点に注意する。

- **`_part.txt` の抽出**: Playwright の `page.locator(sel)` → CSS に加え Playwright 独自記法
  （`text=` / `:has-text()` / `xpath=` など）も使える。
- **SPA検知の署名**: ページ側 `document.querySelectorAll(sel)` → **標準 CSS のみ**。
  セレクタ未入力のときは主要部（`main`/`article`/本文）を自動監視するので、SPA検知にセレクタは必須ではない。

したがって SPA検知でセレクタを使うなら**標準 CSS**にすること（独自記法は SPA検知では無反応になる）。
書き方・調べ方・確認方法（一致件数）など利用者向けの説明は `USAGE.txt` にまとめている。

### テスト

テストは 2 系統ある。

**1. ユニットテスト（pytest・速い／実 Edge 不要）**

実 Edge を使わずに、間違えやすいロジックと「微妙な仕様」を回帰から守る。

- `test_capture.py` … 純粋関数（`safe_name` / `page_label`）と設定読み込み（`load_config`）。
  切り詰め・フォールバック・既定値・空値や範囲外値の扱いを検証。
- `test_session_auth.py` … 操作バー以外からの呼び出しを弾く合言葉(token)照合
  （SPA変化通知 `__eac_spa_changed` の記録状態ゲートを含む）。

SPA検知の落ち着き判定はページ側（`badge.js`）へ移したため、その回帰確認はスモークテストが担う。

```bash
pip install -e ".[dev]"
pytest
```

**2. スモークテスト（実 Edge headless・遅い）**

操作バーの JS（`badge.js`）が実際に構築でき、ページ側ヘルパ（署名/本文取得/バー隠し）と
SPA検知の監視（本文を変えると `__eac_spa_changed` が発火する一連）が例外なく動くかを、
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
