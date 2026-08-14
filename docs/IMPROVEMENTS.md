# edge-auto-capture 改善提案

初版対象コミット: `eac2b92`（ブランチ `refactor/spa-event-driven`）
初版作成日: 2026-08-11
**最終見直し: 2026-08-12（コミット `57cd5e5` 時点）**

> **状態サマリ（2026-08-12 見直し）**: 本文書の A 節（バグ）はほぼ全て対応済み。
> `A-1` `A-2` `A-3` `A-4` `A-5` `A-6` は実装済み（`A-5` は D-C4 と併せて対応、下記参照）。
> B・C・D・E 節は大半が未対応のまま。各項目の見出しに **〔済〕/〔部分〕/〔未〕** を付けた。
> 本文中の `file:line` 参照は初版当時のもので、コード拡張によりズレているため、
> 現行の該当箇所（2026-08-12 時点）へ更新した。行番号は今後もずれ得る点に留意。

コード全体（Python 5 ファイル + `badge.js` + テスト + ビルドスクリプト）を通読した上での
改善提案。責務分割・コメント・token 照合・シャドウ DOM 隔離は丁寧に作られているので、
以下は実害のあるものから順に並べる。

| 節 | 内容 |
|----|------|
| A | バグ／実害があるもの |
| B | 設計上の改善 |
| C | 運用まわり |
| D | テスト・ドキュメント |
| E | ページへの影響・未定義動作 |

関連文書: [`ROADMAP.md`](ROADMAP.md)（**文書横断の優先順位**）/
[`FEATURES.md`](FEATURES.md)（機能提案）/
[`DISTRIBUTION.md`](DISTRIBUTION.md)（配布対応）/ [`HANDOFF.md`](HANDOFF.md)（引き継ぎ）

---

## A. バグ／実害があるもの

### A-1. シャッターフラッシュが次のスクショに写り込む（高）〔**済**〕

> **状態: 対応済み**（コミット `6996a72`）。`captureStart` の先頭で進行中のフラッシュを
> 畳む処理を追加（`badge.js:304-307`）。回帰は smoke テストで確認する
> （`tests/smoke_badge.py` に「連続撮影で `.flash` が残らない」ステップあり）。以下は記録として残す。

同一ページで撮影が連続すると、赤いフラッシュが次の PNG に写る。

`capture.py:153` の `_page_lock`（`async with self._page_lock(page)`）は
「バー退避 → 撮影 → 復帰」を直列化しているが、
**フラッシュはロックの外で 500ms 走り続ける**。

1. A の `captureEnd` → `.frame.flash` 開始（`badge.js:290`、CSS アニメ 500ms）＋ `barTimer` 170ms
2. ロック解放 → B が `captureStart`
3. B のバー退避完了は最短でも 170 + 240 = **約 410ms 後**
4. その時点でフラッシュはまだ opacity 0.2 前後で残っている → **赤みが写る**

修正は `captureStart` の先頭でフラッシュを畳むだけ。

```js
if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
if (els.frame) els.frame.classList.remove('flash');
```

### A-2. `captureStart` が無駄に 500ms 待つ（中）〔**済**〕

> **状態: 対応済み**（コミット `6996a72`、A-1 と同一コミット）。退避済みなら即 resolve する
> 分岐を追加（`badge.js:312`）。回帰テストを `tests/smoke_badge.py` に追加済み。以下は記録。

同じく `badge.js:297` の `captureStart`。`barTimer` をクリアした直後だとバーは既に `capturing` を持っているため、
`classList.add('capturing')` が no-op になり **`transitionend` が発火しない**。
結果 `CAP_FALLBACK_MS`（500ms）まで待ってから撮ることになる。
既に退避済みなら即 resolve する分岐を足す。

```js
if (bar.classList.contains('capturing')) { resolve(); return; }
```

### A-3. 全部失敗しても `[saved]` とログに出る（中）〔**済**〕

> **状態: 対応済み**（コミット `642f9e2`）。`_step` に `done` リストを渡し
> （`capture.py:60`）、png / txt / part の 3 つだけが成功時に tag を積む。全滅時は
> `[保存できず]`、成功時は `[saved] ... (png,txt)` と実際に書けたものを併記する
> （`capture.py:185-188`）。以下は記録として残す。

`capture.py:174`（当時）の `log(f"[saved] {stem}.*")` は無条件だった。`_step` が png / txt / part を
すべて握り潰した場合でも「保存した」と記録される。ログが唯一の運用情報
（`--noconsole` 配布）なので、これは調査を誤らせていた。

各ステップの成否を集めて `[saved] png,txt` のように実際に書けたものを出すのが妥当。

### A-4. `mode:'open'` の Shadow DOM が token 防御を無効化する（中）〔**済**〕

> **状態: 対応済み。** 本リポジトリを公開したまま運用する判断に伴い、
> 他項目に先行して修正した。以下は記録として残す。
> 修正内容は本節末尾の「対応」を参照。

**問題**: `attachShadow({ mode: 'open' })` により、閲覧中サイトのスクリプトから
以下が可能だった。

```js
document.getElementById('__eac_rec_badge__').shadowRoot
  .querySelector('[data-eac="toggle"]').click();
```

記録の開始/停止・連写・セレクタ書き換えがすべて通る。token 照合
（`edge_auto_capture.py:184` の `_authorized`）はバインディング直接呼び出ししか防いでおらず、
UI 経由で素通りしていた。

さらに、サイト側は `window.__eac_toggle` を自前関数でラップしておけば、
利用者がボタンを押した瞬間に **token そのものを盗めた**。
盗まれれば token 照合は無意味になり、以後は自由に記録操作・連写ができる。

#### 対応

1. **シャドウを `mode: 'closed'` へ**（`badge.js` の `build()` 内）。
   `host.shadowRoot` が `null` を返すようになり、UI 経由の迂回が塞がった。
2. **バインディング参照を IIFE 冒頭で退避**（`BOUND` + `callBinding`）。
   `add_init_script` はサイト JS より先に走るため、ここで掴んだ参照が本物。
   利用者のクリックはサイトのラッパを通らず、token が漏れない。
   退避できていない場合は実行時の `window` を見るフォールバックがあり、
   バインディングを公開しないスモークテストでも従来どおり動く。
3. **スモークテスト用アクセサ** `window.__eac_debugRoot()` を追加。
   公開条件は `TOK` が空のときだけ（＝`badge.BADGE_SCRIPT` のテスト用ビルド）。
   実運用は必ず token 付きで組み立てられるため、本番のページには存在しない。
4. **回帰テスト**を `tests/smoke_badge.py` に追加
   （`host.shadowRoot` が `null` であること）。

> **検証状況**: `pytest`（34 件）と `ruff`（0 件）は通過。
> 生成スクリプト側も、`closed` 化・`open` の消失・`__eac_debugRoot` の gate・
> `$CONFIG` 置換を確認済み。
>
> **スモークテストは未実行**（開発ホストが macOS で Edge が無く、SKIP になる）。
> 代替ブラウザを入れての実走は**利用者の判断で省略した**（検証漏れではない）。
> したがって `mode: 'closed'` と `callBinding` の**実挙動は未確認**であり、
> Edge のある Windows 環境でスモークを 1 回通すことが残っている。

### A-5. `cleanup_old_profiles` が同時起動を壊す（中）〔**済**〕

> **状態: 対応済み**。二段構えで解決した。
> 1. **多重起動そのものを入口で抑止**（D-C4）。単一起動ロックで 2 個目のプロセスが
>    起動しなくなり、同時起動による掃除の衝突条件自体が消えた。
> 2. **掃除側の保険（mtime フィルタ）**。`cleanup_old_profiles` に `min_age_seconds`
>    （既定 3 時間）を追加し、新しい＝別インスタンス使用中かもしれない `edge-debug-*` は
>    掃除しないようにした（`infra.py`）。抑止をすり抜けた場合（別 `output_dir` を
>    指定した 2 起動など、ロックは利用者ごと 1 つなので通常は掛かる）でも、稼働中の
>    プロファイルを消さない。`keep=`（F-C1 の再利用プロファイル除外）は従来どおり。
> 回帰は `tests/test_capture.py`（`removes_only_old_dirs` / `age_zero` ほか）で担保。
> 以下は対応前の記録。

`infra.py` の `cleanup_old_profiles` は起動時に `edge-debug-*` を**（keep 指定分を除き）全削除**する。
2 個目のインスタンスを起動すると 1 個目の使用中の使い捨てプロファイルを消しにいく。
`ignore_errors=True` で例外は出ないが、ロックされていないファイルは実際に消えるため、
稼働中の Edge が不安定になり得る。

自分の `tmp`（今回起動分）を除外するか、mtime が数時間以上古いものだけを対象にする。

### A-6. BOM 付き config.ini が「読み込み失敗」になる（低〜中）〔**済**〕

> **状態: 対応済み**（コミット `37a1fb4`）。`config.py:101` を `encoding="utf-8-sig"` にした。
> BOM の有無どちらでも読める。以下は記録として残す。

`config.py:67`（当時）は `encoding="utf-8"` 固定だった。`USAGE.txt` は「config.ini をメモ帳で開いて編集」と
案内しているので、環境や保存時の選択によっては BOM が付き、`[capture]` セクションが
見つからず `MissingSectionHeaderError` → 「config.ini の読み込みに失敗しました」で終了していた。
原因が利用者にまず分からない。

`encoding="utf-8-sig"` にすれば BOM の有無どちらでも読める（コスト 0）。

---

## B. 設計上の改善

### B-1. URL 監視もイベント駆動にできる（ブランチの主旨の続き）〔**済**〕

**対応済み。** `run` ループの `poll_interval` ポーリングを撤去し、Playwright のイベントへ
置き換えた（`edge_auto_capture.py` の `_track_page` / `_on_navigated` / `_shoot_if_changed`
/ `_on_page_closed`）。

| 契機 | 旧（ポーリング） | 現（イベント駆動） |
|------|------------------|--------------------|
| URL 変化 | 毎 tick `pg.url` 比較 | `page.on("framenavigated")`（メインフレームのみ） |
| 新規タブ | 毎 tick `context.pages` | `context.on("page")`（従来から） |
| ページ消滅 | `_prune` | `page.on("close")` → `_on_page_closed` |

`framenavigated` は `history.pushState` 等の同一ドキュメント遷移でも発火するため SPA の
URL ルーティングも拾える。`_shoot_if_changed` は `_url_key`（`#...` 除去）で scroll-spy の
ハッシュのみ変化を弾き、記録ON かつ URL が実際に変わったときだけ撮る。`run` は起動直後の
一掃（既読み込みページを記録ONなら1枚）だけ行い、あとは `closed.wait()` で待つ。
これで `poll_interval` 設定を**完全撤去**し（`config.py` / `config.ini` / README から削除）、
変化から撮影までの遅延も無くした。回帰は `tests/test_capture.py` の
「URL変化のイベント駆動化（B-1）」節（`_shoot_if_changed` / `_on_navigated` / `_on_page_closed`）。

### B-2. 毎 tick の `refresh_panels` はほぼ不要〔**済**〕

**対応済み**（B-1 に内包）。ポーリングループの廃止で毎 tick の `refresh_panels()` 呼び出しが
無くなった。状態配布は記録ON/OFF・SPA検知・セレクタが変わった各コールバックが呼ぶだけになり、
新規タブ・サイト側再描画で作り直されたバーはバー自身が `__eac_getstate` で自己同期する。
`refresh_panels` 本体もページ数ぶんの直列 await をやめ `asyncio.gather` で並列化した。

以下は対応前の記録。

`run` ループ内の `await self.refresh_panels()` は毎秒・全ページに `page.evaluate` を
投げるが、状態変化時は各コールバックが既に `refresh_panels()` を呼び、サイト側再描画で
作り直されたバーは `build()` が `__eac_getstate` で自己同期するので、定常状態では純粋な
オーバーヘッドだった。

### B-3. 撮影スパウンに上限もレート制限もない〔**済**〕

**対応済み**（coalesce 案で実装）。`CaptureRunner` を「ページごとに 1 つの worker」構成へ
変え、進行中に来た要求は `_pending` の「保留 1 件・最新で置き換え」に合流させる
（`capture.py` の `spawn` / `_worker`）。あるページのキューは「実行中 1 件＋保留 1 件」で
構造的に頭打ちになり、総数は「開いているページ数 × 2」で自然に有界。上限値のマジック
ナンバーは持たない。中間フレームは捨てるが、要求後の状態は必ず撮れるので撮り逃さない。

worker が同一ページの撮影を完全に直列化するため、以前スクショ区間だけを守っていた
ページ単位ロック（旧 `_page_locks`）は不要になり撤去した。`ts` は `_pending` を pop した
直後の `_capture` 冒頭で確定するため、長い待ち行列が存在せずファイル名の時刻ズレも解消。
回帰は `tests/test_capture.py` の「CaptureRunner の合流」節（合流・別ページ独立・
worker 再起動の 4 ケース）で担保。

以下は対応前の記録。

`capture.py` の `spawn` は無制限にタスクを積んでいた。更新の激しいダッシュボードで
SPA 検知 ON にすると、ロックで直列化されるだけでキューは伸び続け、
フルページ PNG がディスクを食い潰す。

ページ単位の「保留 1 件まで（最新で置き換え）」か、最小撮影間隔（例: 2 秒）を入れる。

関連して、`ts` は `_capture` 冒頭（＝スパウン直後）で確定するため、
キューが詰まるとファイル名の時刻と中身が数秒ずれる。

### B-4. `settle_delay` が 2 つの役割を兼務〔**未**〕

`capture.py:136` の撮影前 sleep と、`badge.js:102` の `SPA_SETTLE_MS` デバウンス
（`settle_delay` をミリ秒化して埋め込む）の両方に使われている。
SPA 検知経由の撮影ではページ側で既に落ち着きを待った後に、Python 側でもう一度
`settle_delay` 待つ**二重待ち**になる。

`settle_delay`（撮影前）と `spa_settle`（デバウンス）に分けるか、
SPA 経由では sleep を省くのが素直。

### B-5. `skip_urls` が完全一致のみ〔**未**〕

`edge_auto_capture.py:351`（`run` ループ内）と `:198`（`_shoot`）は `url in self.config.skip_urls`。
利用者が `https://example.com` を指定してもクエリやパスが付いた瞬間に効かない。
前方一致か `fnmatch` のワイルドカードにすると期待どおり動く。

### B-6. MutationObserver が常時稼働している（実害あり）〔**済**〕

**対応済み**（下の「対処」どおりに実装。`badge.js` から常時稼働の 2 つを撤去し、
バー再構築は `ensureBarObserver`（`document.body` の `childList` のみ・body 確定後に一度張る）、
SPA 検知は `spaObserverConnect` / `spaObserverDisconnect`（`spaActive()` の切り替わりで
`spaSyncBaseline` 内から接続/切断）へ移した。SPA 検知OFF・記録OFF の間は Observer が
存在しないため発火ゼロ。`tests/smoke_badge.py` に回帰ステップ 10 を追加
（(a) host 削除でバー再構築、(b) SPA 検知OFF後は DOM 変化で通知が飛ばない）。）

以下は対応前の記録。

`badge.js:556` と `badge.js:562` の 2 つの `MutationObserver` が、
**記録OFF・SPA検知OFF でも、全タブ・全ページで無条件に動き続ける**。

```js
new MutationObserver(() => { if (!document.getElementById(ID)) build(); })
  .observe(_root, { childList: true, subtree: true });

new MutationObserver(() => spaSchedule())
  .observe(_root, { childList: true, subtree: true, characterData: true });
```

`spaSchedule` は先頭で `if (!spaActive()) return;` と早期 return するが、
**コールバックが呼ばれること自体のコストは避けられない**。
`subtree: true` + `characterData: true` で文書全体を監視しているため、
チャットアプリや実況ダッシュボードのような更新の激しいサイトでは毎秒数千回発火する。

SPA 検知を使っていない利用者にも、閲覧しているだけで恒常的な負荷がかかっている。

対処:

- SPA 用（2 つ目）は `spaActive()` の切り替わり（`spaSyncBaseline` 内）で
  `observe` / `disconnect` する
- バー再構築用（1 つ目）は `childList` のみ・監視対象を `document.body` に絞る

> **これは「イベント駆動化で負荷を下げた」という本ブランチの主旨に照らして、やり残しにあたる。**
> B-1（URL 監視のイベント駆動化）と同じ流れで対応するのが自然。

---

## C. 運用まわり

- **log.txt が無限に伸びる**〔未〕 — `infra.py:91` の `log`。追記のみで上限なし。
  サイズ上限＋1 世代ローテートで十分。
- **output に保持ポリシーがない**〔未〕 — フルページ PNG は容易に数 MB。
  長時間の記録でディスクが埋まったときの挙動が「`[skip png]` が静かに並ぶだけ」になる。
- **`badge.py` の import 時 I/O**〔未〕 — `badge.py:114` の `BADGE_SCRIPT = build_badge_script()` は
  module import 時に `badge.js` を読む。実運用では使わない（スモークテスト専用）ので、
  遅延生成にすると凍結環境での失敗経路が 1 つ減る。
- **`build.ps1` が依存を二重管理**〔済〕 — 旧 `pip install pyinstaller playwright` の直書きを
  `pip install -e ".[build]"` に寄せた（`build.ps1`）。playwright は base の dependencies、
  pyinstaller は `[build]` extras から入り、版は pyproject に一元化された（下記ピン留めが効く）。
- **依存がピン留めされていない**〔済〕 — `pyproject.toml` の `dependencies` を
  `["playwright>=1.60,<2"]` に変更。**動作確認済みの版（.venv 実測 1.60.0）を下限**にして
  未検証の旧版を弾き、**メジャー更新（`<2`）**へ自動で飛び移らせない。これで
  「先月のビルドは動いたのに今月のは動かない」を宣言側で防ぐ。
  厳密な1点固定は互換範囲の宣言を潰すため pyproject には書かない方針（完全再現が必要になったら
  `constraints.txt` / `build.ps1` の `==` 固定で対応する）。`DISTRIBUTION.md` D-B1 のバージョン表示と噛み合う。

---

## D. テスト・ドキュメント

- **CI がない**〔未〕 — `pytest` + `ruff` を GitHub Actions で回すだけでも、
  今回のような回帰は拾える。スモークは Edge/Chrome 必須なので Windows runner 限定 or 手動トリガで。
- **テストの空白域**〔一部改善〕 — `_shoot` の skip 判定、`CaptureRunner` のロック直列化、
  `refresh_panels` はいずれも未テスト。特に `_shoot`（`edge_auto_capture.py:188`）は
  依存が薄く、モック 1 個でテストできる。A-1 のフラッシュ写り込みは
  スモークに「連続撮影で `.flash` が残らない」ステップが**追加済み**（A-2 の即解決回帰も追加済み）。
- ~~**docstring のドリフト**~~〔**済**〕 — `edge_auto_capture.py` の構成コメントが
  `capture.py … 設定読み込み・基盤ユーティリティ` のままだったが、`config.py` / `infra.py` への
  分割を反映するよう**修正済み**（本見直しで対応）。
- ~~**README の `poll_interval`**~~〔**済**〕 — `README.md` の表が
  「URL 変化・**中身変化**を確認する間隔」だったのを、中身変化はイベント駆動で間隔に依らない旨へ
  **修正済み**（本見直しで対応）。
- **smoke テストが「Edge/Chrome が無ければ緑」になる**〔一部改善・未完〕 —
  `tests/smoke_badge.py:53-54` は Edge も Chrome も起動できないと `SKIP` かつ**終了コード 0** で抜ける。
  Edge→Chrome フォールバックは**実装済み**（コミット `b3176a9`）だが、
  **CI に載せると「何も検証していないのに緑」**になる問題は残る。`--strict` オプション
  （ブラウザ不在なら失敗）を足し、CI ではそちらを使う。上の「CI がない」とセット。
- ~~**`ruff --fix` が Python 3.8 互換を壊す（罠）**~~ 〔**解消済み**〕 —
  `capture.py` / `edge_auto_capture.py` の文字列注釈に UP037 が出ていたが、
  この引用符は Python 3.8 で `dict[Page, str]` が実行時評価されるのを避けるための
  **意図的**なもので、`ruff check --fix` すると黙って 3.8 互換が壊れる状態だった。
  **`requires-python` を `>=3.9` へ上げて根治した**（引用符が不要になった）。
  この対応で `ruff check` が初めて緑（終了コード 0）になり、CI 導入の障害が消えている。
- **型チェッカーが未導入**〔未〕 — `ruff` は入っているが型検査はしていない。
  `mypy` / `pyright` があれば機械的に守れる。規模も小さく導入コストは低い。

---

## E. ページへの影響・未定義動作

閲覧中の実ページへ介入するツールである以上、**サイト側に与える影響**と
**想定外のコンテンツでの挙動**は独立した検討軸になる。

### E-1. 全ページにフォーカス可能な要素を注入している〔**未**〕

バーのボタン・入力欄は Tab キーの移動先に入る。`document.body.appendChild` なので
順序は末尾で影響は小さいが、**利用者がどのサイトでも Tab を押し続けると
最後にツールの UI へ来る**。

併せてアクセシビリティも未対応。`role="switch"` は付いているが（`badge.js:219`）
**`aria-checked` が無く**、状態が支援技術に伝わらない。
透過ボタンも `title` のみで `aria-label` が無い。
配布先に支援技術の利用者がいる場合の課題になる。

### E-2. `history.pushState` の書き換えが永続的〔**未**〕

`badge.js:570-571`（`hookHistory('pushState')` / `hookHistory('replaceState')`）は
全ページで `history.pushState` / `replaceState` を差し替え、
**元に戻さない**。ほとんどのサイトでは問題ないが、フレームワークによっては
自前のラップ順序に依存していたり、モンキーパッチを検知するものがある。
閲覧中のページを改変している以上、リスクとして認識しておく。

### E-3. サイト側からツールの存在を検知できる〔**未**〕

`window.__eacApplyState` / `__eac_bodyText` などが全ページのグローバルに露出している。
サイト側は `'__eacApplyState' in window` で**このツールで見られていることを判別でき**、
表示を変える・ブロックするといった対応が可能。

> A-4（`closed` 化）は「**操作される**」問題、こちらは「**検知される**」問題で別軸。
> ヘルパをグローバルに置かず、`page.evaluate` に関数を直接渡す形にすれば露出を減らせる。

### E-4. ダウンロードしたファイルが消える（影響大）〔**実装済・実機検証済**〕

（旧状態）`_browser_launch_kwargs` の起動オプションに `accept_downloads` / `downloads_path`
の指定がなかった。Playwright はダウンロードを自前の一時領域へ受け、
**コンテキストを閉じるときに削除する**ため、利用者が閲覧中に落としたファイルが
**終了時に消える**可能性が高かった。「Edge で普通に閲覧してください」と案内している
ツール（`USAGE.txt`）としては驚きの挙動だった。

> **実機検証で判明した重要点（Playwright 1.60.0 / Chrome）**:
> 当初案の「`downloads_path` を `output_dir` 配下へ明示するだけ」では**解決しない**。
> Playwright は `downloads_path` を指定しても**コンテキスト終了時にダウンロードを削除する**
> （一時置き場が変わるだけ）。実際に検証したところ、終了後に受け皿は空だった。
> **`download` イベントで `save_as()` して退避して初めて残る。**

**対応**（実機検証に基づく最終版）:
> - `_browser_launch_kwargs`: `accept_downloads=True` を明示（受理する意図）。
>   `downloads_path` は単独では効かないため付けない。
> - `CaptureSession.on_download`: `context.on("download")`（context 単位・全タブ対象）で
>   受け、`output_dir/downloads/<元のファイル名>` へ `save_as` する。同名衝突は連番。
>   受け皿フォルダは `main()` で起動前に作成する。
> - 撮影成果物（png/txt/log.txt）と混ざらないよう `downloads` サブフォルダに分けた。

**実機検証の結果**（`edge_auto_capture` の実経路 `setup()` で確認）:
> - 閉じた後も `output_dir/downloads` にファイルが残る（対照実験として、退避なしでは消えることも確認）。
> - HTTP の `Content-Disposition`（`filename*=UTF-8''…`）経由なら**日本語ファイル名も正しく保持**（例: `資料.txt`）。
> - 同名を続けて落とすと `name(1).txt` … と連番になり上書きしない。
>
> 既知の軽微な癖: `data:` URL の `download` 属性から落とした場合、Chromium が推定する
> ファイル名が文字化けすることがある（`data:` 特有で、通常の HTTP ダウンロードでは発生しない）。
> 退避自体は行われるため消失は起きない。

回帰は `tests/test_downloads.py` で担保（退避先・連番・`save_as` 呼び出しをスタブで検証）。

### E-5. 非 HTML コンテンツでの挙動が未定義〔**未**〕

PDF ファイルを開いた場合（Edge の内蔵ビューア）、`page.title()` / `bodyText()` /
`_part.txt` が何を返すか未検証。おそらく空か無意味な内容の `.txt` が生成される。
`edge://` 系の内部ページも同様。

`skip_urls` は撮影を止められるが、**バーの注入自体は行われる**。
せめて挙動を確認し、`USAGE.txt` に「PDF は正しく保存されません」と書くだけでも支援になる。

### E-6. `page.evaluate` のハングに対する保護がない〔**対応済み**〕

`_step` は例外を捕まえるが、**返ってこない場合は捕まえられない**。
サイトのメインスレッドが重い処理で詰まると、`bodyText()` の `evaluate` が長時間戻らず、
そのタスクが `_tasks` に残り続ける。B-3（合流）でキューは有界化したが、戻らない
`evaluate` はそのページの worker を占有し続けるため、当該ページの撮影は止まったままになる。

> **対応（2026-08）**: 生の `page.evaluate` を `asyncio.wait_for(..., timeout=eval_timeout)`
> で打ち切るようにした（`config.eval_timeout`、既定 5000ms）。対象は本文取得
> （`BODY_TEXT_CALL`）と撮影の合図（`try_eval` 経由の `CAPTURE_START/END`）。本文取得の
> 打ち切りは `_step` が握って `[skip txt]` を出し、worker は次へ進むため撮影が止まらない。
>
> 当初案の `context.set_default_timeout()` は**採らなかった**: `page.evaluate` は
> タイムアウト引数を持たず、デフォルトタイムアウトも効かない（`set_default_timeout` の
> 対象外）。そのため `asyncio.wait_for` で外側から縛る方式にした。ハング時に worker が
> 解放されることは `tests/test_capture.py` の `try_eval` ハング保護テストで回帰から守る。

---

## 着手順のおすすめ（本文書内のみ）

> **注意**: これは本文書の項目だけを並べたもの。
> `FEATURES.md` / `DISTRIBUTION.md` も含めた**全体の優先順位は
> [`ROADMAP.md`](ROADMAP.md) が正**。実際の作業順はそちらに従うこと。
> （例: `DISTRIBUTION.md` の `D-C1`「無言終了」は全体 1 位だが、本表には現れない）

> **消化状況**: 1（A-1/A-2）・2（A-3/A-6）・5 のうち A-4、B-1/B-2/B-6・E-4・E-6・A-5・B-3 は
> **対応済み**。残るのは `B-4`（`settle_delay` の二重待ち）・`B-5`（`skip_urls` の前方一致化）
> と C 節の運用まわり（log ローテート等）。以下の表は初版のままの参考。

| 順 | 項目 | 規模 | 効果 | 状態 |
|----|------|------|------|------|
| 1 | A-1 / A-2 | badge.js 数行 | 保存物の品質に直結 | ✅済 |
| 2 | A-3 / A-6 | 各数行 | ログとエラーメッセージの信頼性 | ✅済 |
| 3 | **B-6** | badge.js 小 | 全利用者への恒常的な負荷を止める。単独で入れられる | 未 |
| 4 | **E-4** | 1 行 + 検証 | 利用者にとって最も驚きが大きい（ダウンロード消失） | 未 |
| 5 | A-4 / A-5 | 中 | セキュリティと同時起動 | A-4✅ / A-5✅（D-C4 と併せて対応） |
| 6 | B-1 / B-2 / B-6 | 中〜大 | ブランチの主旨の完遂。`poll_interval` を消せる | ✅済（B-1/B-2/B-6 すべて対応） |
| 7 | B-3 / E-6 | 中 | 長時間運用の安全弁（キュー上限とハング保護） | B-3✅ / E-6✅ |

A-1〜A-3 と A-6 は合計でも 30 行程度の変更だった（いずれも対応済み）。

**B-6 と E-4 は他とほぼ独立**して入れられるうえ効果が大きいので、
最初の 4 件（A-1/A-2/A-3/A-6）の直後に置くのが費用対効果として良い。

E-1〜E-3（ページへの影響）は、いずれも「今すぐ壊れている」わけではないため
優先度は低い。ただし**配布先が広がるほど顕在化しやすい**種類の問題なので、
`DISTRIBUTION.md` の検討と合わせて判断すること。
