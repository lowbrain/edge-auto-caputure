# edge-auto-capture

Microsoft Edge（無ければ Google Chrome）で開いたページを、**記録ONの間だけ**、フルページのスクリーンショット(`.png`)と
ページ全文テキスト(`.txt`)へ自動保存するツール。CSS セレクタを指定すれば、ページの一部だけを
抜き出したテキスト(`_part.txt`)も保存でき、URL が変わらず中身だけ変わる SPA も検知して撮れる。

> **利用者向けの使い方**（操作バーの操作、SPA検知の使い方、CSSセレクタの入れ方・調べ方、
> トラブル対処など）は **[USAGE.txt](USAGE.txt)** に一本化している。
> この README は**開発・ビルド向け**の情報（仕組み・構成・設定リファレンス・テスト・配布）をまとめる。
> **このコードを触るときの落とし穴・作業環境・検証手順**は [CONTRIBUTING.md](CONTRIBUTING.md)。
> **これから作る / 直すもの（残タスクと優先順）**は
> Issue [#78](https://github.com/lowbrain/edge-auto-caputure/issues/78)（ピン留め）が正。

## 仕組み（概要）

- 記録ON中、Playwright が各ページの URL/タブ変化を**イベント駆動**で検知し（URL変化は
  `page.on("framenavigated")`、新規タブは `context.on("page")`、ページ消滅は `page.on("close")`）、
  変化ごとに capture（`png` / `txt`、セレクタ指定時は `_part.txt`）を走らせる。ポーリング間隔
  （旧 `poll_interval`）は廃止し、変化から撮影までの遅延も無くした。Edge の起動・監視・終了・
  一時プロファイルの後始末までを一括で行う（毎回まっさらな一時プロファイルで起動）。
- **撮影のたびに索引 CSV（`index.csv`）へ 1 行追記する**。列は 時刻 / URL / タイトル /
  ファイル名接頭辞 / 撮影契機（手動・URL変化・SPA） / セレクタ / 成否。時刻は ISO 8601（オフセット付き）、
  Excel で開く前提なので **BOM 付き（`utf-8-sig`）で新規作成し、追記は `utf-8`**（BOM の二重付与を避ける）。
  全系譜ぶんを 1 本にまとめる粒度は `log.txt` と同じ。
- **タブ系譜（グループ）ごとの独立制御**: 記録ON/OFF・SPA検知・セレクタは、セッション全体で
  共有せず「タブ系譜」ごとに独立して持つ。系譜とは、起動時の最初のタブ（または手動で開いた別タブ）と、
  そこから `window.open` / `target="_blank"` で派生したポップアップ/ウィンドウの一族で、
  `page.opener()` の連鎖で判定する。系譜内のページは操作バーの状態を共有し、別系譜には影響しない。
  手動で開いた別タブ（`Ctrl+T` 等、opener が無いページ）は**初期OFF**の独立グループになり、
  そのタブの操作バーで ON にするまで撮影されない（起動時の最初のグループだけ `start_recording` に従う）。
- **保存先は「起動ごと」→「系譜ごと」の 2 段**: `output_dir` の直下に起動 1 回分の
  **セッションフォルダ**（`YYYY-MM-DD_HHMMSS`。`config.session_stamp()`）を 1 段挟み、
  その中を系譜ごとの `lineage-<id>/`（ダウンロードは `lineage-<id>/downloads/`）に分ける。
  **`log.txt` と `index.csv` もセッションフォルダ直下**に置かれる（`output_dir` 直下ではない）。

  ```
  output/2026-08-16_143025/          # 起動 1 回分。受け渡しはこのフォルダを丸ごと渡せば済む
  ├─ log.txt / index.csv
  └─ lineage-20260814101105674/      # タブ系譜ごと
     ├─ *.png / *.txt / *_part.txt
     └─ downloads/
  ```

  `<id>` はその系譜を作った時刻（ミリ秒まで・区切りなし）で、ログの `lineage-<id>`
  表記＝保存フォルダ名と一致するため、ログから保存先をそのまま辿れる。フォルダは保存時に必要に応じて作成する。
  セッション粒度は秒までで、同一秒に二重起動するとまれにフォルダを共有するが、`mkdir` の `exist_ok` と
  同じ許容範囲として扱っている。
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
- バーはこのほかに次を持つ。文言は `badge.py` の `_BADGE_CONFIG` に集約してある。
  - **撮影カウンタ**（`本セッション N 枚`）… 保存できた枚数だけを数える（全滅した回は数えない）。
    動作している実感と、意図しない連写の早期発見のために常時表示する。保存に失敗した回は
    フラッシュを失敗色にして成功と区別する。
  - **「保存先」ボタン** … セッションフォルダを OS のファイルマネージャで開く。
    撮り終わったフォルダをそのまま渡せる導線。
  - **セレクタ履歴** … 確定したセレクタを `datalist` の候補として入力欄に出す（最近使った順・上限あり）。

## 動作条件

- Windows
- Microsoft Edge または Google Chrome がインストール済み（既定は Edge 優先→無ければ Chrome。`config.ini` の `browser` で片方に固定も可能。`channel="msedge"` / `"chrome"` でシステムのブラウザを使う）
- 開発・ビルド時のみ Python 3.9+（配布した exe の実行に Python は不要）

## 既知の制限（非 HTML ページ）

Edge 実機で確認した挙動（Edge 152 / `E-5`）。クラッシュはせず、処理は握られて続行する。

- **PDF（Edge 内蔵ビューア）はテキストが保存されない**。`.png` は表示中ページが画像として
  保存されるが、`.txt` / `_part.txt` は**空**になる（ビューアが本文 DOM を持たないため）。
  `page.title()` も空を返すので、ファイル名末尾の識別名は URL 由来のフォールバックになる。
  → **PDF の証跡は画像（.png）でのみ残る**と理解しておくこと。テキストが要るなら別途 PDF を保存する。
- **Excel / Word など Edge が描画しない形式はダウンロードされる**ため、この制限は当てはまらない。
  タブ内では開かず download イベントが飛び、`downloads/` へ**元ファイルのまま保存**される（`E-4`）。
- **`edge://` 系（`edge://settings` 等）の特権ページには操作バーが注入されない**
  （ブラウザが外部スクリプト注入を禁止する領域のため）。スクリーンショット自体は撮れる。

## リポジトリ構成

役割ごとに分割している。依存方向は下向きの一方向で循環なし:
`edge_auto_capture →（capture / config / badge）→ infra`、`capture → badge`、`config → infra`。
`infra` は Playwright 非依存で、`config`（設定読み込み）も同様なので実 Edge 無しでテストできる。
ページ側 JS は実ファイル `badge.js` に置き、エディタ/リンタで構文検査できるようにしてある。

```
edge-auto-capture/
├─ edge_auto_capture.py   エントリ＋監視セッション（CaptureSession）
├─ capture.py             1ページ分の保存処理（撮影実行器 CaptureRunner）・ページ操作ヘルパ
├─ config.py              設定（Config / config.ini の load_config）
├─ infra.py               基盤ユーティリティ（パス・ログ・致命エラー通知・一時プロファイル掃除）
├─ lineage.py             タブ系譜（lineage）の識別・保存先規約と解決レジストリ
├─ browser.py             Edge/Chrome の起動候補と起動オプションの組み立て
├─ badge.py               操作バーのページ側JS組み立て（表示文言→$CONFIG／バインディング名）
├─ badge.js               操作バーのページ側JS本体（実ファイル）
├─ downloads.py           ダウンロードの保存先解決とファイル退避（E-4）
├─ tests/                 テストと conftest.py（構成は下の「テスト」節）
├─ .github/workflows/ci.yml  CI（ruff+mypy / pytest / smoke --strict）
├─ config.ini             既定の設定ファイル
├─ pyproject.toml         依存とパッケージ設定
├─ README.md              このファイル（開発者向け）
├─ CONTRIBUTING.md        触る人向けの落とし穴・作業環境・検証手順
├─ USAGE.txt              配布物(exe)に同梱する利用者向けの使い方（Shift-JIS）
├─ LICENSE                MIT License
└─ build.ps1              配布用 exe のビルド（PyInstaller）
```

生成物（`build/` `dist/` `output/` `__pycache__/` `*.spec`）は Git 管理外。

## 開発時の実行

```bash
pip install -e .
python edge_auto_capture.py
```

挙動は同じフォルダの `config.ini` で設定する（下表）。実際の操作方法は `USAGE.txt` を参照。
停止は Ctrl+C かブラウザのウィンドウを閉じる。

### 設定（config.ini）

| キー | 意味 |
|------|------|
| `start_url` | 起動時に最初に開くページ（空なら about:blank） |
| `browser` | 使うブラウザ（`edge` / `chrome`）。指定するとそのブラウザだけを起動。空なら Edge→Chrome の順で自動選択 |
| `edge_path` | Edge 実行ファイルのパス（空なら自動検出。非標準インストール時のみ） |
| `chrome_path` | Chrome 実行ファイルのパス（空なら自動検出。非標準インストール時のみ） |
| `output_dir` | 保存先。相対なら本体/exe と同じ場所基準、絶対パスも可 |
| `settle_delay` | 変化検知後、描画が落ち着くまで待つ秒数。SPA 検知経由の撮影ではページ側で既に待っているため、撮影前の sleep は省く（二重待ち回避） |
| `load_timeout` | ページ読み込み待ちの上限（ミリ秒） |
| `eval_timeout` | ページ側 JS（本文取得・撮影の合図）の実行を待つ上限（ミリ秒）。重い処理で固まったページを打ち切って次へ進むための保険 |
| `skip_urls` | 撮らない URL（カンマ区切り）。前方一致で判定（クエリ付きでも効く）。`* ? [` を含めるとワイルドカード（fnmatch）扱い |
| `allow_urls` | 撮る URL をこれだけに絞る（カンマ区切り・空なら無効）。指定すると合致しない URL は全スキップ。`skip_urls` も併用可（合致しても `skip_urls` に当たれば撮らない）。判定は `skip_urls` と同じ前方一致/ワイルドカード |
| `target_selector` | 一部抜き出し／SPA検知の対象 CSS セレクタの初期値（バーで実行時に変更可・空可） |
| `hide_selectors` | 撮影中だけ隠す要素の CSS セレクタ（カンマ区切り・空可）。同意バナーや追従ヘッダが証跡に被るのを防ぐ。撮影の瞬間だけ `visibility:hidden` にして撮影後に戻す |
| `start_recording` | 起動直後に記録を開始するか（`false`=待機で起動、`true`=起動時から記録ON） |
| `profile_dir` | 再利用するブラウザプロファイルの場所（空なら毎回まっさらな使い捨て＝既定）。指定するとログイン状態などを保存し次回へ引き継ぐ。相対なら本体/exe と同じ場所基準。指定フォルダに Cookie・認証情報がディスク保存される点に注意 |

### CSSセレクタ（実装上の注意）

`target_selector`（＝バーの入力値）は 2 か所で使う。用途で参照する API が違う点に注意する。

- **`_part.txt` の抽出**: Playwright の `page.locator(sel)` → CSS に加え Playwright 独自記法
  （`text=` / `:has-text()` / `xpath=` など）も使える。
- **SPA検知の署名**: ページ側 `document.querySelectorAll(sel)` → **標準 CSS のみ**。
  セレクタ未入力のときは主要部（`main`/`article`/本文）を自動監視するので、SPA検知にセレクタは必須ではない。

したがって SPA検知でセレクタを使うなら**標準 CSS**にすること（独自記法は SPA検知では無反応になる）。
書き方・調べ方・確認方法（一致件数）など利用者向けの説明は `USAGE.txt` にまとめている。

### テスト

テストは 2 系統ある。**回し方と合否の見方は [CONTRIBUTING.md](CONTRIBUTING.md) §3「検証手順（4 点セット）」が正**
（`--strict` の要否、ブラウザ不在時の `SKIP` の扱い、CI のジョブ構成を含む）。ここでは何を守っているかだけを書く。

**1. ユニットテスト（pytest・速い／実 Edge 不要）**

実 Edge を使わずに、間違えやすいロジックと「微妙な仕様」を回帰から守る。
守っている範囲は大きく 4 つ。

- **純粋関数と判定ロジック** — ファイル名の安全化、URL 判定（`should_capture`）、
  設定の既定値・自己修復、タブ系譜の解決、起動候補の組み立て
- **「微妙な仕様」の固定** — 保存ステップの集約（`_step`）、ページ側 JS のハング保護（`try_eval`）、
  撮影キューの合流、書き込み先の退避（`resolve_writable_dir`）、多重起動抑止、
  操作バー以外からの呼び出しを弾く合言葉(token)照合
- **言語境界・パッケージ境界の一致** — `badge.py` の `BIND_*` と `badge.js` の `BINDING_NAMES`、
  `pyproject.toml` の `py-modules` と実ファイル、`USAGE.txt` の Shift-JIS 往復一致。
  **どれも壊れても他の 3 点セットが落ちない**ので、テストだけが守っている（#67 / #68 / #69）
- **起動シーケンスと入口** — `cli()` の起動ログの順序・終了コード

**テストファイルはソース側のモジュール構成に合わせてある**（どこに足すか迷わないように）。
一覧は `ls tests/` で見られるので、ここには書かない（増減のたびにこの節が腐るため）。

SPA検知の落ち着き判定はページ側（`badge.js`）へ移したため、その回帰確認はスモークテストが担う。

**2. スモークテスト（実 Edge/Chrome・遅い）**

操作バーの JS（`badge.js`）が実際に構築でき、ページ側ヘルパ（署名/本文取得/バー隠し）と
SPA検知の監視（本文を変えると `__eac_spa_changed` が発火する一連）が例外なく動くかを確認する。
**`badge.js` を守るのはこれだけで、pytest では守れない。**

**変更時に踏みやすい落とし穴（`$CONFIG` 置換・バインディング名の二重管理・Shift-JIS・
新モジュール追加時の同時更新など）は [CONTRIBUTING.md](CONTRIBUTING.md) §1 にまとめてある。**

## 配布用 exe のビルド

Python 未導入の Windows PC でも動く、単一 EXE（フォルダ形式）を作る。

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

PyInstaller が `dist\edge-auto-capture\` を生成し、`config.ini` / `USAGE.txt` /
依存ライセンス表記（`THIRD-PARTY-NOTICES.txt`）/ `LICENSE`（あれば）を exe の隣へ同梱する。
ページ側 JS の `badge.js` は `--add-data` で `_internal\` に同梱され、
実行時は `sys._MEIPASS` から読み込まれる（`badge.py` `capture.py` は import から自動で辿られる）。
最後に配布用の `dist\edge-auto-capture.zip` と、その `*.zip.sha256`（完全性確認用）を作る。

#### コードサイニング署名（任意・`D-D1`）

証明書を持っている場合はビルド時に署名できる。指定が無ければ署名ステップは素通りし、
現状どおり未署名で配布される。

```bash
# PFX ファイルで署名
powershell -ExecutionPolicy Bypass -File build.ps1 -CertPath cert.pfx -CertPassword ****
# 証明書ストア上の証明書を拇印で指定（EV トークン等）
powershell -ExecutionPolicy Bypass -File build.ps1 -CertThumbprint <THUMBPRINT>
```

> **受け口は実装済み、証明書の取得は保留（組織判断）。** 企業環境では SmartScreen の「警告」ではなく
> **「ブロック」**（AppLocker / WDAC / Intune）になることがあり、「詳細情報 → 実行」では回避できない。
> 本格配布ならコードサイニング証明書が最も効く（OV で警告軽減、EV なら SmartScreen 評価が即付く）。
> 年額コストと発行手続きが要るため、技術判断ではなく組織判断として保留している。
> 取得できない間は下の「配布先 IT へ許可登録を依頼する連絡テンプレート」を使う。

### 配布方法

生成された `dist\edge-auto-capture.zip` を配る（中身は `dist\edge-auto-capture\`）。フォルダ構成:

```
edge-auto-capture\
├─ edge-auto-capture.exe      ダブルクリックで起動
├─ config.ini                 利用者が編集可能
├─ USAGE.txt                  利用者向けの使い方
├─ THIRD-PARTY-NOTICES.txt    同梱依存（Playwright 等）のライセンス表記
├─ LICENSE.txt                本体のライセンス（LICENSE がある場合）
├─ _internal\                 ランタイム + playwright ドライバ + badge.js（必須・触らない）
└─ output\                    実行後に生成される保存先
```

配布物の完全性は同梱の `edge-auto-capture.zip.sha256` で確認できる:

```powershell
(Get-FileHash -Algorithm SHA256 edge-auto-capture.zip).Hash
```

> `_internal\` は動作に必須。exe だけ取り出しても動かない。
> 未署名 exe のため初回に Windows SmartScreen 警告が出る場合がある
> （「詳細情報」→「実行」で起動可能）。企業環境では警告ではなくブロックに
> なることがある（AppLocker / WDAC / Intune）。その場合は下の依頼テンプレートで
> 配布先 IT へ許可登録を依頼する。

### 配布前の確認

- **アンチウイルスの誤検知（`D-D2`）** — PyInstaller 製バイナリはヒューリスティックで誤検知されやすい。
  `--onedir`（`--onefile` より誤検知しにくい）は採用済み。**配布前に主要な AV で一度確認**しておく。
  出る場合は署名するかベンダーへ誤検知報告。技術改修ではなく都度対応の運用事項。
- **配布物のサイズ実測** — `--collect-all playwright` は Node ドライバごと同梱するため
  100MB 超になる可能性がある。配布経路（メール添付の可否等）に影響するので実測する。
- `output\` の同梱は `build.ps1` が自動で行う。空の `output\` を作り、展開時点で書き込み可否が
  分かるようにしている（`Compress-Archive` は空フォルダを ZIP に含めないため、フォルダ保持と
  利用者案内を兼ねた説明ファイルを 1 つ置く）。狙いは権限問題に早く気づけること。

### 配布先 IT へ許可登録を依頼する連絡テンプレート

証明書を取得しない間、企業環境でブロックされる場合はこれで許可登録を依頼する。
`< >` は配布ごとに埋める。SHA256 は `build.ps1` が出力する `*.zip.sha256` の値を使う。

```
件名: 業務ツール「edge-auto-capture」の実行許可のお願い

<IT 部門ご担当者> 様

業務調査で使用するツール「edge-auto-capture」を配布します。
現時点でコードサイニング署名が無いため、SmartScreen / AppLocker / WDAC / Intune の
ポリシーによってはブロックされる可能性があります。以下の実行ファイルの許可登録
（許可リストへの追加）をご検討いただけますでしょうか。

- ツール名 : edge-auto-capture
- 用途     : Web ページのスクリーンショット・テキストの自動保存（社内調査用）
- 配布物   : edge-auto-capture.zip（PyInstaller onedir 形式・未署名）
- 実行ファイル: edge-auto-capture.exe
- SHA256(zip): <build.ps1 が出力した SHA256 値>
- 依存      : Microsoft Edge / Google Chrome（同梱の Playwright ドライバ経由）
- 配布元    : <配布者名・連絡先>

ご不明点があればお知らせください。よろしくお願いいたします。
```

## ライセンス

本ツールは [MIT License](LICENSE)（著作権表示: 2026 lowbrain）で公開する。

同梱する [Playwright](https://playwright.dev/) は Apache-2.0。再配布時のライセンス表記は
`build.ps1` が生成する `THIRD-PARTY-NOTICES.txt` に含めて配布物へ同梱する。
