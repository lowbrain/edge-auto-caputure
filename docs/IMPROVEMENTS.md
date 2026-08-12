# edge-auto-capture 改善提案

初版対象コミット: `eac2b92`（ブランチ `refactor/spa-event-driven`）
初版作成日: 2026-08-11
**最終見直し: 2026-08-12（コミット `57cd5e5` 時点）**

> **状態サマリ（2026-08-12 見直し）**: 本文書の A 節（バグ）はほぼ全て対応済み。
> `A-1` `A-2` `A-3` `A-4` `A-6` は実装済み、`A-5` は部分対応（下記参照）。
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

### A-5. `cleanup_old_profiles` が同時起動を壊す（中）〔**部分**〕

> **状態: 部分対応**（コミット `3c1f5c0` 他）。`cleanup_old_profiles` に `keep=` 引数が
> 追加され、再利用プロファイル（`profile_dir` 指定＝F-C1）は掃除対象から除外されるように
> なった（`infra.py:166`）。ただし **`keep` は「自分の再利用プロファイル」を守るだけ**で、
> **使い捨てプロファイル（`edge-debug-*`）同士の同時起動衝突は未対応**。下記の本質的な問題は残る。

`infra.py:166` の `cleanup_old_profiles` は起動時に `edge-debug-*` を**（keep 指定分を除き）全削除**する。
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

### B-1. URL 監視もイベント駆動にできる（ブランチの主旨の続き）〔**未**〕

SPA 検知はイベント駆動化済みだが、`edge_auto_capture.py:335` の `run` ループはまだ
`poll_interval` ポーリング。Playwright には以下のイベントがある。

| 契機 | 現状 | イベント駆動版 |
|------|------|----------------|
| URL 変化 | 毎 tick `pg.url` 比較 | `page.on("framenavigated")` |
| 新規タブ | 毎 tick `context.pages` | `context.on("page")` |
| ページ消滅 | `_prune` | `page.on("close")` |

置き換えれば `poll_interval` 設定ごと消せて、変化から撮影までの遅延も
最大 1 秒 → 即時になる。`seen` / `_prune` も不要になる。

### B-2. 毎 tick の `refresh_panels` はほぼ不要〔**未**〕

`edge_auto_capture.py:340`（`run` ループ内の `await self.refresh_panels()`）は
毎秒・全ページに `page.evaluate` を投げるが、

- 状態変化時は各コールバックが既に `refresh_panels()` を呼んでいる
- サイト側再描画で作り直されたバーは `build()` が `__eac_getstate` で自己同期する（`badge.js:414`）

ので、定常状態では純粋なオーバーヘッド。残すとしても `asyncio.gather` で並列化すべき
（現状はページ数ぶん直列 await）。

### B-3. 撮影スパウンに上限もレート制限もない〔**未**〕

`capture.py:110` の `spawn` は無制限にタスクを積む。更新の激しいダッシュボードで
SPA 検知 ON にすると、ロックで直列化されるだけでキューは伸び続け、
フルページ PNG がディスクを食い潰す。

ページ単位の「保留 1 件まで（最新で置き換え）」か、最小撮影間隔（例: 2 秒）を入れる。

関連して、`ts` は `_capture` 冒頭（＝スパウン直後）で確定する（`capture.py:129-131`）ため、
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

### B-6. MutationObserver が常時稼働している（実害あり）〔**未**〕

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
- **`build.ps1` が依存を二重管理**〔未〕 — pyproject に `[build]` extras があるのに
  `pip install pyinstaller playwright` を直書き（`build.ps1:24`）。
  `pip install -e ".[build]"` に寄せると一元化できる。
- **依存がピン留めされていない**〔未〕 — `pyproject.toml:13-14` は `dependencies = ["playwright"]` のみ。
  **配布用 exe をビルドするたびに、その時点の最新 Playwright が入る。**
  Playwright は API 変更も browser 対応バージョンの変更も相応の頻度で入るため、
  「先月のビルドは動いたのに今月のは動かない」が起こり得て、原因究明も難しい。
  `playwright>=1.40,<2` のような範囲指定に加え、**配布ビルド用には `constraints.txt` で
  厳密固定**して再現可能なビルドにする。配布物である以上、後者は特に価値がある
  （`DISTRIBUTION.md` D-B1 のバージョン表示と噛み合う）。

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

### E-4. ダウンロードしたファイルが消える（影響大）〔**実装済**（実機検証は推奨のまま）〕

（旧状態）`_browser_launch_kwargs` の起動オプションに `accept_downloads` / `downloads_path`
の指定がなかった。Playwright はダウンロードを自前の一時領域へ受け、
**コンテキストを閉じるときに削除する**ため、利用者が閲覧中に落としたファイルが
**終了時に消える**可能性が高かった。「Edge で普通に閲覧してください」と案内している
ツール（`USAGE.txt`）としては驚きの挙動だった。

**対応**（`_browser_launch_kwargs`）: `accept_downloads=True` を付け、
`downloads_path` を `output_dir/downloads` に固定した。受け皿フォルダは起動前に
`main()` で作成する。撮影成果物（png/txt/log.txt）と混ざらないようサブフォルダに分けた。
起動オプションの組み立ては `tests/test_downloads.py` で回帰を張った。

> Playwright のバージョンで挙動が異なる可能性があるため、**実機での最終確認は引き続き推奨**。
> （ダウンロードが実際に `output_dir/downloads` に残るかは実ブラウザでの確認が要る。）

### E-5. 非 HTML コンテンツでの挙動が未定義〔**未**〕

PDF ファイルを開いた場合（Edge の内蔵ビューア）、`page.title()` / `bodyText()` /
`_part.txt` が何を返すか未検証。おそらく空か無意味な内容の `.txt` が生成される。
`edge://` 系の内部ページも同様。

`skip_urls` は撮影を止められるが、**バーの注入自体は行われる**。
せめて挙動を確認し、`USAGE.txt` に「PDF は正しく保存されません」と書くだけでも支援になる。

### E-6. `page.evaluate` のハングに対する保護がない〔**未**〕

`_step` は例外を捕まえるが、**返ってこない場合は捕まえられない**。
サイトのメインスレッドが重い処理で詰まると、`bodyText()` の `evaluate` が長時間戻らず、
そのタスクが `_tasks` に残り続ける（B-3 のキュー無制限と複合すると悪化する）。

`context.set_default_timeout()` の設定を検討する。

> ただし `evaluate` にデフォルトタイムアウトが効くかは Playwright のバージョン依存。
> **要検証**。

---

## 着手順のおすすめ（本文書内のみ）

> **注意**: これは本文書の項目だけを並べたもの。
> `FEATURES.md` / `DISTRIBUTION.md` も含めた**全体の優先順位は
> [`ROADMAP.md`](ROADMAP.md) が正**。実際の作業順はそちらに従うこと。
> （例: `DISTRIBUTION.md` の `D-C1`「無言終了」は全体 1 位だが、本表には現れない）

> **2026-08-12 時点の消化状況**: 1（A-1/A-2）・2（A-3/A-6）・5 のうち A-4 は**対応済み**。
> 残る着手候補は **B-6・E-4・A-5（部分）・B 系・E-6** など。以下の表は初版のままの参考。

| 順 | 項目 | 規模 | 効果 | 状態 |
|----|------|------|------|------|
| 1 | A-1 / A-2 | badge.js 数行 | 保存物の品質に直結 | ✅済 |
| 2 | A-3 / A-6 | 各数行 | ログとエラーメッセージの信頼性 | ✅済 |
| 3 | **B-6** | badge.js 小 | 全利用者への恒常的な負荷を止める。単独で入れられる | 未 |
| 4 | **E-4** | 1 行 + 検証 | 利用者にとって最も驚きが大きい（ダウンロード消失） | 未 |
| 5 | A-4 / A-5 | 中 | セキュリティと同時起動 | A-4✅ / A-5 部分 |
| 6 | B-1 / B-2 / B-6 | 中〜大 | ブランチの主旨の完遂。`poll_interval` を消せる | 未 |
| 7 | B-3 / E-6 | 中 | 長時間運用の安全弁（キュー上限とハング保護） | 未 |

A-1〜A-3 と A-6 は合計でも 30 行程度の変更だった（いずれも対応済み）。

**B-6 と E-4 は他とほぼ独立**して入れられるうえ効果が大きいので、
最初の 4 件（A-1/A-2/A-3/A-6）の直後に置くのが費用対効果として良い。

E-1〜E-3（ページへの影響）は、いずれも「今すぐ壊れている」わけではないため
優先度は低い。ただし**配布先が広がるほど顕在化しやすい**種類の問題なので、
`DISTRIBUTION.md` の検討と合わせて判断すること。
