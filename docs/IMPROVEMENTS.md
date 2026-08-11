# edge-auto-capture 改善提案

対象コミット: `eac2b92`（ブランチ `refactor/spa-event-driven`）
作成日: 2026-08-11

コード全体（Python 5 ファイル + `badge.js` + テスト + ビルドスクリプト）を通読した上での
改善提案。責務分割・コメント・token 照合・シャドウ DOM 隔離は丁寧に作られているので、
以下は実害のあるものから順に並べる。

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

---

## 着手順のおすすめ

| 順 | 項目 | 規模 | 効果 |
|----|------|------|------|
| 1 | A-1 / A-2 | badge.js 数行 | 保存物の品質に直結 |
| 2 | A-3 / A-6 | 各数行 | ログとエラーメッセージの信頼性 |
| 3 | A-4 / A-5 | 中 | セキュリティと同時起動 |
| 4 | B-1 / B-2 | 中〜大 | ブランチの主旨の完遂。`poll_interval` を消せる |
| 5 | B-3 | 中 | 長時間運用の安全弁 |

A-1〜A-3 と A-6 は合計でも 30 行程度の変更。
