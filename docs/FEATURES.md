# edge-auto-capture 機能提案

対象コミット: `eac2b92`（ブランチ `refactor/spa-event-driven`）
作成日: 2026-08-11

「ツールとして何ができるか」の観点での提案。
不具合・設計・運用の指摘は [`IMPROVEMENTS.md`](IMPROVEMENTS.md) 側にある。

> **項番について**: `IMPROVEMENTS.md` も A〜D の節を使っているため、
> 混同を避けて本ファイルの項目には **`F-` 接頭辞**を付けている。
> 本文中で `B-3` `B-5` のように接頭辞なしで参照している箇所は
> `IMPROVEMENTS.md` の項目を指す。

---

## A. 保存物の価値を上げる

### F-A1. 索引ファイルがない（価値大・コスト小）

`output/` に `2026-08-11_14-30-25-123_タイトル.png` がフラットに積まれるだけ。
数百枚になると「どれがどのページか」「なぜ撮られたか」を追えない。
撮影のたびに `index.csv`（または `manifest.jsonl`）へ 1 行追記するだけで後工程が一変する。

記録すべき項目:

| 列 | 由来 |
|----|------|
| 撮影時刻 | `capture.py:125` の `ts` |
| URL / タイトル | 既に持っている |
| ファイル名接頭辞 | `stem` |
| **撮影契機** | 手動 / URL変化 / SPA変化 |
| セレクタ | 撮影時の実行時値 |
| 成否 | `IMPROVEMENTS.md` A-3 の `done` リストを流用 |

特に **撮影契機**は現状どこにも残っていない
（ログには `[手動]` `[SPA変化]` と出るが、保存物と紐付いていない）。
`_shoot` が既に 3 経路の合流点（`edge_auto_capture.py:147`）なので、
引数を 1 個足すだけで通せる。

### F-A2. `_part.png` がない（非対称）

セレクタを指定すると `_part.txt` は出るのに、**画像は常にフルページのみ**。
「この表だけ画像で欲しい」に応えられない。
`page.locator(sel).screenshot()` で済み、セレクタは既に配線済みなので追加は小さい。

複数一致時の扱い（先頭のみ / 連番で全部）は要決定。

### F-A3. 無害化済み単一 HTML の保存

PNG は文字検索できず、`.txt` はレイアウト・リンク・表構造が消える。
証跡としてはどちらも欠ける。構造を保った第 3 の保存物が欲しい。

ただし **保存物は「開いても何も起きない」ものでなければならない**（利用者の要件）。
リンクを踏んで実サイトへ飛ぶ、開いた瞬間に外部通信が飛ぶ、スクリプトが動く、
といったことが後から起きると証跡として問題になる。

#### 方式の決定: MHTML は採用しない

> **重要**: 以前この項目は「MHTML（CDP の `Page.captureSnapshot`）」を推していたが、
> 上の要件と噛み合わないため **却下**した。実装者は MHTML へ戻らないこと。

却下理由:

1. **MHTML は「今の live DOM」をそのまま直列化する。**
   無害化するには撮る前に**利用者が見ている実ページを破壊する**しかない。
   閲覧しながら記録するツールなので論外。
2. **後処理が面倒で検証しにくい。**
   保存後に無害化するなら、MIME マルチパート（quoted-printable / base64 混在）を解いて
   `text/html` パートを書き換え、JS パートを落として再構築する必要がある。
   証跡用途で「本当に全部消えたか」を確認しにくい形式は使えない。
3. **「Chrome は MHTML で JS を実行しない」に頼れない。**
   ビューアの実装依存であり、将来の変更や別ツールで開かれた場合に前提が崩れる。
   **保存物そのものが無害である**ほうが確実。

なお **`page.pdf()` も使えない**。Playwright の PDF 生成は headless 専用で、
本ツールは headed 起動（`edge_auto_capture.py:73` の `headless=False`）。

#### 採用方式: DOM のクローンを無害化して直列化

実ページには一切触れずに済む。

```js
() => {
  const root = document.documentElement.cloneNode(true);   // 実ページは無傷
  const kill = (sel) => root.querySelectorAll(sel).forEach(e => e.remove());

  kill('#__eac_rec_badge__');                    // 操作バー（シャドウ内は元々直列化されない）
  kill('script, noscript, template');
  kill('meta[http-equiv="refresh" i]');
  kill('iframe, frame, object, embed, applet');
  kill('link[rel~="preload" i], link[rel~="prefetch" i]');

  root.querySelectorAll('*').forEach(el => {
    [...el.attributes].forEach(a => {
      if (/^on/i.test(a.name)) el.removeAttribute(a.name);        // onclick 等
    });
  });
  root.querySelectorAll('a[href]').forEach(a => {
    a.setAttribute('data-href', a.href);                          // URL は情報として残す
    a.removeAttribute('href');                                    // が、辿れない
  });
  root.querySelectorAll('form').forEach(f => {
    f.removeAttribute('action'); f.removeAttribute('method');
  });
  root.querySelectorAll('input,select,textarea,button').forEach(e => e.setAttribute('disabled',''));
  root.querySelectorAll('[autoplay]').forEach(e => e.removeAttribute('autoplay'));

  return '<!DOCTYPE html>\n' + root.outerHTML;
}
```

落とすもの:

| 対象 | 理由 |
|------|------|
| `<script>` / `on*` 属性 | 実行されうるもの全般 |
| `<a href>` | クリックで実サイトへ飛ぶ。URL は `data-href` にテキストとして残す |
| `<form action>` / 入力要素 | 誤って送信されうる |
| `<iframe>` / `<object>` / `<embed>` | 外部コンテンツの読み込み・実行 |
| `<meta http-equiv="refresh">` | 開いた瞬間の自動遷移 |
| `autoplay` | 音声・動画の自動再生 |
| `preload` / `prefetch` | 開いた瞬間の外部通信 |

#### 二重の保険: meta CSP を埋め込む

上のリストから漏れがあっても効くよう、保存する HTML の `<head>` 先頭に差し込む。

```html
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; style-src 'unsafe-inline' data:; font-src data:">
```

これで**開いた瞬間の外部通信もスクリプト実行もブラウザ側で止まる**。
DOM の無害化（攻めの防御）と CSP（受けの防御）の二段構えにすることで、
「全部消せたか」を人力で保証しなくて済む。

> `file://` で開いたときに meta CSP が効くことは、実装前に手元で確認すること。
> Chromium 系では効くはずだが、**確定情報として扱わないこと**。

#### 画像・外部リソースの扱い（v1 は割り切る）

`<img src="https://...">` が残っていると、**ファイルを開いた瞬間に実サイトへリクエストが飛ぶ**
（アクセスログに残る、トラッキングビーコンが動く）。対処は 2 通り:

- **(a) data: URI へインライン化** — 見た目は完全に残るが、ページ内 `fetch` が要る・
  ファイルが数 MB になる・CORS で失敗する画像がある
- **(b) 外部参照を全部剥がす** — 画像は消えるが、**外部通信ゼロが保証される**

**v1 は (b) を採用**する。本ツールは既にフルページ PNG を撮っているので、
**見た目は PNG、検索可能な構造は無害化 HTML** と役割分担すれば足りる。
上の CSP（`img-src data:` のみ許可）とも整合する。

#### 注意点（クローン方式の限界）

| 事象 | 影響 | 対処 |
|------|------|------|
| `input` の**入力値**が保存されない | `value` プロパティは属性に反映されないため、画面に見えている入力内容が消える | 直列化前に `el.setAttribute('value', el.value)` で焼き込む |
| `<canvas>` の描画内容が消える | グラフ等が空白になる | `toDataURL()` で `<img>` に差し替え（同一オリジンのみ。CORS 汚染時は例外） |
| Shadow DOM の中身が出ない | Web Components 主体のサイトは中身が空 | v1 では割り切り。なお**操作バーがシャドウ内にあるおかげで自動的に混入しない**という利点でもある |

#### コスト

CSP と (b) の割り切りを採れば、**上の JS 40 行程度 + Python 側の書き出し数行**で v1 が成立する。
MHTML 後処理より確実に安く、検証もしやすい（`grep -c '<script' saved.html` で確認できる）。

受け入れ確認の目安:

- `grep -ci '<script\|onclick\|href="http' saved.html` が 0
- ネットワークを切った状態で開いても、見た目・内容が変わらない
- 開発者ツールの Network タブにリクエストが 1 件も出ない

---

## B. 撮影品質

### F-B1. 遅延読み込み画像が空のまま撮れる（実務で効く）

`full_page=True` は Chromium が `captureBeyondViewport` で撮るため、
**IntersectionObserver 系の lazy-load 画像が読み込まれないまま**写ることが多い。
長いページほど下半分が空白になる。

撮影前に一度ページ末尾までスクロールして戻す「プリロードスクロール」を入れると解消する
（`preload_scroll = true` のような設定でオプトイン）。
`capture.py:130` の `settle_delay` sleep の直後が差し込み位置。

### F-B2. Cookie バナー・固定ヘッダの除去

同意バナーや追従ヘッダが証跡画像に被り続ける。
操作バー自体は退避する仕組み（`captureStart`）を既に持っているので、
**同じ仕組みに `hide_selectors` を通すだけ**。
撮影中だけ指定セレクタを `visibility:hidden` にして戻す。設定 1 行で画像品質が上がる。

---

## C. 運用の実用性

### F-C1. ログイン状態が保持されない（設計判断が必要）

毎回 `tempfile.mkdtemp` でまっさらな一時プロファイルを作る（`edge_auto_capture.py:324`）。
これは「サインイン/同期/初回セットアップを回避」という**意図的な設計**だが、
裏返すと **認証が要るサイトは毎回ログインし直し**。業務調査で使うなら実質ブロッカーになり得る。

`config.ini` に `profile_dir`（指定時のみ再利用、空なら現状どおり使い捨て）を足すのが折衷案。
ただし `cleanup_old_profiles`（`infra.py:94`）が `edge-debug-*` を掃除する対象と
衝突しないよう分離が要る。

> **これは仕様の方針決定が先。** 実装前に「まっさら起動」を崩してよいか判断すること。

### F-C2. `allow_urls`（記録対象の限定）

現状は `skip_urls` の除外リストのみ。
「この業務システムだけ撮りたい」場合、除外を列挙するのは非現実的。
許可リスト（指定時はそれ以外を全部スキップ）のほうが実用場面は多い。
`IMPROVEMENTS.md` B-5（パターンマッチ化）とセットで入れると自然。

### F-C3. 起動ごとのセッションフォルダ

`output/` 直下に全起動分が混ざる。
`output/2026-08-11_143025/` のようにセッション単位で切ると、
成果物の受け渡しがそのまま「このフォルダを渡す」で済む。
`IMPROVEMENTS.md` C 節の log.txt 肥大とも噛み合う。

---

## D. 操作性

### F-D1. セレクタのピッカー（価値大・コスト中〜大）

現状は「開発者ツールで F12 → Elements で id/class を確認 → 手入力」。
`badge.py:38-50` の title に**12 行の手順書**が埋まっていること自体が、
この導線の難しさを示している。

ページ上でホバー → 要素をハイライト → クリックでセレクタ自動生成、を操作バーに足すと、
このツールの敷居が一段下がる。実装は中規模（オーバーレイ + セレクタ生成ロジック）だが、
費用対効果は高い。

### F-D2. セレクタの履歴・プリセット（コスト小）

入力欄に `<datalist>` で過去に使った値を出すだけ。
同じサイトを繰り返し撮る運用では効く。

### F-D3. 撮影枚数カウンタと失敗表示

バーに「本セッション N 枚」を出す。動作している実感が得られるだけでなく、
**暴走（`IMPROVEMENTS.md` B-3 のレート制限なし問題）の早期発見**になる。

併せて、保存失敗時に成功と同じ赤いフラッシュを出しているのは誤解を招く
（現状 `[skip png]` がログに出るだけ）。失敗時はフラッシュの色を変えるか、バーに短く出す。
`IMPROVEMENTS.md` A-3 の成否判定と対になる UX 側の対応。

### F-D4. 保存先フォルダを開くボタン

バーからワンクリックで `output/` を開く。
Python 側は `os.startfile` の 1 行、バインディング追加のみ。地味だが利用頻度は高い。

---

## 優先度まとめ

| 提案 | 価値 | コスト | 備考 |
|------|------|--------|------|
| F-A1 索引ファイル | 大 | 小 | **最優先候補** |
| F-B2 hide_selectors | 中 | 小 | 既存の退避機構に相乗り |
| F-A2 `_part.png` | 中 | 小 | 非対称の解消 |
| F-D4 フォルダを開く | 小 | 極小 | |
| F-D2 セレクタ履歴 | 小 | 小 | |
| F-D3 カウンタ・失敗表示 | 中 | 小 | A-3 とセット |
| F-B1 プリロードスクロール | 大 | 中 | 実務で効く |
| F-C2 `allow_urls` | 中 | 小 | B-5 とセット |
| F-C3 セッションフォルダ | 中 | 小 | |
| F-A3 無害化済み HTML | 中 | 中 | **MHTML は却下済み**。meta CSP の file:// 挙動は要検証 |
| F-C1 プロファイル永続化 | 大 | 中 | **仕様判断が先** |
| F-D1 セレクタピッカー | 大 | 中〜大 | 敷居を下げる |

**最初の一手の推奨**: F-A1（索引）+ F-B2（バナー除去）+ F-A2（`_part.png`）。
いずれも小さく入れられて保存物の実用度が明確に上がる。

F-C1 だけは方針決定が要るので、進める前に判断すること。
