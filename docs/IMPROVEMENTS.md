# edge-auto-capture 改善提案

対象コミット: `eac2b92`（ブランチ `refactor/spa-event-driven`）
作成日: 2026-08-11

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

### A-1. シャッターフラッシュが次のスクショに写り込む（高）

同一ページで撮影が連続すると、赤いフラッシュが次の PNG に写る。

`capture.py:143` の `_page_lock` は「バー退避 → 撮影 → 復帰」を直列化しているが、
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

### A-2. `captureStart` が無駄に 500ms 待つ（中）

同じく `badge.js:277`。`barTimer` をクリアした直後だとバーは既に `capturing` を持っているため、
`classList.add('capturing')` が no-op になり **`transitionend` が発火しない**。
結果 `CAP_FALLBACK_MS`（500ms）まで待ってから撮ることになる。
既に退避済みなら即 resolve する分岐を足す。

```js
if (bar.classList.contains('capturing')) { resolve(); return; }
```

### A-3. 全部失敗しても `[saved]` とログに出る（中）

`capture.py:174` の `log(f"[saved] {stem}.*")` は無条件。`_step` が png / txt / part を
すべて握り潰した場合でも「保存した」と記録される。ログが唯一の運用情報
（`--noconsole` 配布）なので、これは調査を誤らせる。

各ステップの成否を集めて `[saved] png,txt` のように実際に書けたものを出すのが妥当。

### A-4. `mode:'open'` の Shadow DOM が token 防御を無効化する（中）

`badge.js:317` の `attachShadow({ mode: 'open' })` により、閲覧中サイトのスクリプトから
以下が可能。

```js
document.getElementById('__eac_rec_badge__').shadowRoot
  .querySelector('[data-eac="toggle"]').click();
```

記録の開始/停止・連写・セレクタ書き換えがすべて通る。token 照合
（`edge_auto_capture.py:143`）はバインディング直接呼び出ししか防いでおらず、
UI 経由で素通りする。`mode: 'closed'` にすれば塞がる
（`tests/smoke_badge.py:71-93` が `shadowRoot` を触っているので、
テスト用のアクセサを 1 個生やす必要はある）。

さらに、サイト側は `window.__eac_toggle` を自前関数でラップしておけば、
利用者がボタンを押した瞬間に **token そのものを盗める**。
`add_init_script` はサイト JS より先に走るので、`badge.js` の冒頭で
バインディング参照を IIFE ローカルへ退避しておくのが確実。

```js
const B = {
  toggle: window.__eac_toggle, shot: window.__eac_shot, /* ... */
};
```

### A-5. `cleanup_old_profiles` が同時起動を壊す（中）

`infra.py:94` は起動時に `edge-debug-*` を**無条件で全削除**する。
2 個目のインスタンスを起動すると 1 個目の使用中プロファイルを消しにいく。
`ignore_errors=True` で例外は出ないが、ロックされていないファイルは実際に消えるため、
稼働中の Edge が不安定になり得る。

自分の `tmp` を除外するか、mtime が数時間以上古いものだけを対象にする。

### A-6. BOM 付き config.ini が「読み込み失敗」になる（低〜中）

`config.py:67` は `encoding="utf-8"` 固定。`USAGE.txt` は「config.ini をメモ帳で開いて編集」と
案内しているので、環境や保存時の選択によっては BOM が付き、`[capture]` セクションが
見つからず `MissingSectionHeaderError` → 「config.ini の読み込みに失敗しました」で終了する。
原因が利用者にまず分からない。

`encoding="utf-8-sig"` にすれば BOM の有無どちらでも読める（コスト 0）。

---

## B. 設計上の改善

### B-1. URL 監視もイベント駆動にできる（ブランチの主旨の続き）

SPA 検知はイベント駆動化済みだが、`edge_auto_capture.py:294` のループはまだ
`poll_interval` ポーリング。Playwright には以下のイベントがある。

| 契機 | 現状 | イベント駆動版 |
|------|------|----------------|
| URL 変化 | 毎 tick `pg.url` 比較 | `page.on("framenavigated")` |
| 新規タブ | 毎 tick `context.pages` | `context.on("page")` |
| ページ消滅 | `_prune` | `page.on("close")` |

置き換えれば `poll_interval` 設定ごと消せて、変化から撮影までの遅延も
最大 1 秒 → 即時になる。`seen` / `_prune` も不要になる。

### B-2. 毎 tick の `refresh_panels` はほぼ不要

`edge_auto_capture.py:299` は毎秒・全ページに `page.evaluate` を投げるが、

- 状態変化時は各コールバックが既に `refresh_panels()` を呼んでいる
- サイト側再描画で作り直されたバーは `build()` が `__eac_getstate` で自己同期する（`badge.js:363`）

ので、定常状態では純粋なオーバーヘッド。残すとしても `asyncio.gather` で並列化すべき
（現状はページ数ぶん直列 await）。

### B-3. 撮影スパウンに上限もレート制限もない

`capture.py:112` の `spawn` は無制限にタスクを積む。更新の激しいダッシュボードで
SPA 検知 ON にすると、ロックで直列化されるだけでキューは伸び続け、
フルページ PNG がディスクを食い潰す。

ページ単位の「保留 1 件まで（最新で置き換え）」か、最小撮影間隔（例: 2 秒）を入れる。

関連して、`ts` は `spawn` 時点で確定する（`capture.py:125`）ため、キューが詰まると
ファイル名の時刻と中身が数秒ずれる。

### B-4. `settle_delay` が 2 つの役割を兼務

`capture.py:130` の撮影前 sleep と、`badge.js:64` のデバウンスの両方に使われている。
SPA 検知経由の撮影ではページ側で既に落ち着きを待った後に、Python 側でもう一度
`settle_delay` 待つ**二重待ち**になる。

`settle_delay`（撮影前）と `spa_settle`（デバウンス）に分けるか、
SPA 経由では sleep を省くのが素直。

### B-5. `skip_urls` が完全一致のみ

`edge_auto_capture.py:157` は `url in self.config.skip_urls`。
利用者が `https://example.com` を指定してもクエリやパスが付いた瞬間に効かない。
前方一致か `fnmatch` のワイルドカードにすると期待どおり動く。

### B-6. MutationObserver が常時稼働している（実害あり）

`badge.js:497` と `badge.js:503` の 2 つの `MutationObserver` が、
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

- **log.txt が無限に伸びる** — `infra.py:56`。追記のみで上限なし。
  サイズ上限＋1 世代ローテートで十分。
- **output に保持ポリシーがない** — フルページ PNG は容易に数 MB。
  長時間の記録でディスクが埋まったときの挙動が「`[skip png]` が静かに並ぶだけ」になる。
- **`badge.py` の import 時 I/O** — `badge.py:114` の `BADGE_SCRIPT = build_badge_script()` は
  module import 時に `badge.js` を読む。実運用では使わない（スモークテスト専用）ので、
  遅延生成にすると凍結環境での失敗経路が 1 つ減る。
- **`build.ps1` が依存を二重管理** — pyproject に `[build]` extras があるのに
  `pip install pyinstaller playwright` を直書き（`build.ps1:11`）。
  `pip install -e ".[build]"` に寄せると一元化できる。
- **依存がピン留めされていない** — `pyproject.toml:8` は `dependencies = ["playwright"]` のみ。
  **配布用 exe をビルドするたびに、その時点の最新 Playwright が入る。**
  Playwright は API 変更も browser 対応バージョンの変更も相応の頻度で入るため、
  「先月のビルドは動いたのに今月のは動かない」が起こり得て、原因究明も難しい。
  `playwright>=1.40,<2` のような範囲指定に加え、**配布ビルド用には `constraints.txt` で
  厳密固定**して再現可能なビルドにする。配布物である以上、後者は特に価値がある
  （`DISTRIBUTION.md` D-B1 のバージョン表示と噛み合う）。

---

## D. テスト・ドキュメント

- **CI がない** — `pytest` + `ruff` を GitHub Actions で回すだけでも、
  今回のような回帰は拾える。スモークは Edge 必須なので Windows runner 限定 or 手動トリガで。
- **テストの空白域** — `_shoot` の skip 判定、`CaptureRunner` のロック直列化、
  `refresh_panels` はいずれも未テスト。特に `_shoot`（`edge_auto_capture.py:147`）は
  依存が薄く、モック 1 個でテストできる。A-1 のフラッシュ写り込みも
  スモークに「連続撮影で `.flash` が残らない」を足せば守れる。
- **docstring のドリフト** — `edge_auto_capture.py:26` が
  `capture.py … 設定読み込み・1ページ分の保存処理・基盤ユーティリティ` のままだが、
  実際は `config.py` / `infra.py` へ分割済み。
- **README の `poll_interval`** — `README.md:83` が
  「URL 変化・**中身変化**を確認する間隔」となっているが、
  中身変化はイベント駆動化されたので URL 変化のみが正。
- **smoke テストが「Edge が無ければ緑」になる** — `tests/smoke_badge.py:38` は
  Edge を起動できないと `SKIP` かつ**終了コード 0** で抜ける。手元での実行には親切だが、
  **CI に載せると「何も検証していないのに緑」**になる。`--strict` オプション
  （Edge 不在なら失敗）を足し、CI ではそちらを使う。上の「CI がない」とセット。
- **型チェッカーが未導入** — `ruff` は入っているが型検査はしていない。
  `Optional[str]` の扱いや Python 3.8 互換の注釈（文字列注釈の徹底、`3-2` 参照）は
  `mypy` / `pyright` があれば機械的に守れる。規模も小さく導入コストは低い。

---

## E. ページへの影響・未定義動作

閲覧中の実ページへ介入するツールである以上、**サイト側に与える影響**と
**想定外のコンテンツでの挙動**は独立した検討軸になる。

### E-1. 全ページにフォーカス可能な要素を注入している

バーのボタン・入力欄は Tab キーの移動先に入る。`document.body.appendChild` なので
順序は末尾で影響は小さいが、**利用者がどのサイトでも Tab を押し続けると
最後にツールの UI へ来る**。

併せてアクセシビリティも未対応。`role="switch"` は付いているが（`badge.js:181`）
**`aria-checked` が無く**、状態が支援技術に伝わらない。
透過ボタンも `title` のみで `aria-label` が無い。
配布先に支援技術の利用者がいる場合の課題になる。

### E-2. `history.pushState` の書き換えが永続的

`badge.js:507-511` は全ページで `history.pushState` / `replaceState` を差し替え、
**元に戻さない**。ほとんどのサイトでは問題ないが、フレームワークによっては
自前のラップ順序に依存していたり、モンキーパッチを検知するものがある。
閲覧中のページを改変している以上、リスクとして認識しておく。

### E-3. サイト側からツールの存在を検知できる

`window.__eacApplyState` / `__eac_bodyText` などが全ページのグローバルに露出している。
サイト側は `'__eacApplyState' in window` で**このツールで見られていることを判別でき**、
表示を変える・ブロックするといった対応が可能。

> A-4（`closed` 化）は「**操作される**」問題、こちらは「**検知される**」問題で別軸。
> ヘルパをグローバルに置かず、`page.evaluate` に関数を直接渡す形にすれば露出を減らせる。

### E-4. ダウンロードしたファイルが消える（要検証・影響大）

`edge_auto_capture.py:70` の起動オプションに `accept_downloads` / `downloads_path` の
指定がない。Playwright はダウンロードを自前の一時領域へ受け、
**コンテキストを閉じるときに削除する**。

つまり利用者が閲覧中に何かをダウンロードすると、**終了時に消えている**可能性が高い。
「Edge で普通に閲覧してください」と案内しているツール（`USAGE.txt`）としては
驚きの挙動である。

`downloads_path` を `output_dir` 配下などへ明示するだけで解決する。

> Playwright のバージョンで挙動が異なる可能性があるため、**実機での確認を推奨**。

### E-5. 非 HTML コンテンツでの挙動が未定義

PDF ファイルを開いた場合（Edge の内蔵ビューア）、`page.title()` / `bodyText()` /
`_part.txt` が何を返すか未検証。おそらく空か無意味な内容の `.txt` が生成される。
`edge://` 系の内部ページも同様。

`skip_urls` は撮影を止められるが、**バーの注入自体は行われる**。
せめて挙動を確認し、`USAGE.txt` に「PDF は正しく保存されません」と書くだけでも支援になる。

### E-6. `page.evaluate` のハングに対する保護がない

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

| 順 | 項目 | 規模 | 効果 |
|----|------|------|------|
| 1 | A-1 / A-2 | badge.js 数行 | 保存物の品質に直結 |
| 2 | A-3 / A-6 | 各数行 | ログとエラーメッセージの信頼性 |
| 3 | **B-6** | badge.js 小 | 全利用者への恒常的な負荷を止める。単独で入れられる |
| 4 | **E-4** | 1 行 + 検証 | 利用者にとって最も驚きが大きい（ダウンロード消失） |
| 5 | A-4 / A-5 | 中 | セキュリティと同時起動 |
| 6 | B-1 / B-2 / B-6 | 中〜大 | ブランチの主旨の完遂。`poll_interval` を消せる |
| 7 | B-3 / E-6 | 中 | 長時間運用の安全弁（キュー上限とハング保護） |

A-1〜A-3 と A-6 は合計でも 30 行程度の変更。

**B-6 と E-4 は他とほぼ独立**して入れられるうえ効果が大きいので、
最初の 4 件（A-1/A-2/A-3/A-6）の直後に置くのが費用対効果として良い。

E-1〜E-3（ページへの影響）は、いずれも「今すぐ壊れている」わけではないため
優先度は低い。ただし**配布先が広がるほど顕在化しやすい**種類の問題なので、
`DISTRIBUTION.md` の検討と合わせて判断すること。
