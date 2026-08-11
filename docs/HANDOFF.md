# 引き継ぎメモ（改善提案 → 実装）

宛先: 次に作業するモデル／担当者
作成日: 2026-08-11
ブランチ: `refactor/spa-event-driven`（作業ツリーはクリーン）
コード起点コミット: `eac2b92` — **ここから先、コードは 1 行も変わっていない**
文書追加コミット: `07452a6` / `f72730c` ほか（`docs/` の 4 文書のみ。未 push）

---

## 0. これまでの経緯（読む順序）

1. このリポジトリのコード全体（Python 5 ファイル + `badge.js` + `tests/` + `build.ps1`）を
   通読してレビューを行った。
2. その結果を **[`docs/IMPROVEMENTS.md`](IMPROVEMENTS.md)** に保存した。
   **まず先にそれを読むこと。** 本メモは「実装するための補足」であり、
   指摘の根拠と全項目は `IMPROVEMENTS.md` 側にある。
3. 別途、機能面（ツールとして何ができるか）の提案を
   **[`docs/FEATURES.md`](FEATURES.md)** にまとめた。今回のスコープ外だが、
   実装方針を考えるときの参考になる。項目 ID は `F-` 接頭辞で区別してある。
4. さらに、第三者配布を前提とした指摘を
   **[`docs/DISTRIBUTION.md`](DISTRIBUTION.md)** にまとめた。項目 ID は `D-` 接頭辞。
   **配布（exe のビルド・受け渡し）を伴う作業に入る前に必ず読むこと。**
   特に `D-A1`（LICENSE 未指定）と `D-C1`（書き込み不可の場所で無言で死ぬ）は、
   コードの品質とは別軸で「配れない／配っても動かない」を引き起こす。
5. **コードの修正はまだ 1 行も行っていない。** 追加したのは `docs/` の 4 文書のみで、
   `07452a6` / `f72730c` としてコミット済み（未 push）。
   つまり **`eac2b92` 時点のコードがそのまま残っている**状態から着手することになる。

---

## 1. 作業環境の注意（重要）

このツールは **Windows 専用**だが、開発ホストは **macOS (darwin 25.5.0)**。
以下を前提に動くこと。

| 事項 | 状況 |
|------|------|
| `pytest` / `ruff` | **システム python3 に未インストール**。`pip install -e ".[dev]"` から始める |
| `tests/smoke_badge.py` | 実 Edge が必要。macOS でも Edge があれば動く可能性はあるが**未検証**。Edge が無ければ `SKIP`（終了コード 0）で抜ける仕様なので、PASS 表示を鵜呑みにしないこと |
| `infra._message_box` | `ctypes.windll` 依存。macOS では except で握られ no-op になる |
| `build.ps1` | PowerShell / Windows 専用。macOS では実行検証できない |

つまり **`pytest` は動かせるが、`badge.js` の挙動は macOS 上では実機確認できない可能性が高い**。
JS 側の修正は「smoke テストを足す」ところまでを成果物とし、
実行確認は Windows 環境に委ねる旨を報告に明記すること。

---

## 2. 今回やってほしいこと

`IMPROVEMENTS.md` の「着手順のおすすめ」の **1 と 2**、すなわち
**A-1 / A-2 / A-3 / A-6** の 4 件。合計 30 行程度の変更。

A-4（Shadow DOM `closed` 化）・A-5（一時プロファイル掃除）・B 系は**今回のスコープ外**。
先に上記 4 件を小さく通してから、利用者に次を確認すること。

`FEATURES.md`（機能追加）と `DISTRIBUTION.md`（配布対応）も**別トラック**であり、
今回は着手しない。ただし **配布作業が発生する場合は `DISTRIBUTION.md` が優先**する
（`D-A1` LICENSE 未指定・`D-C1` 無言終了は、本メモの 4 件より配布上の影響が大きい）。

---

## 3. 各修正の具体案

### A-1 / A-2 — `badge.js` の `captureStart`（`badge.js:259` 付近）

この 2 件は同じ関数を触るので**まとめて 1 コミット**にする。

現状:

```js
  function captureStart() {
    return new Promise((resolve) => {
      if (!els || !els.bar) { resolve(); return; }
      capDepth++;
      if (barTimer) { clearTimeout(barTimer); barTimer = null; }
      if (capDepth > 1) { resolve(); return; }   // 既に退避済み（別の撮影が進行中）
      const bar = els.bar;
      ...
      bar.classList.add('capturing');
```

足すもの:

1. **A-1**: `barTimer` のクリア直後に、進行中のシャッターフラッシュも畳む。
   これを入れないと、直前の撮影のフラッシュ（`.frame.flash`、CSS 500ms）が
   次のスクリーンショットに赤みとして写り込む。

   ```js
   if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
   if (els.frame) els.frame.classList.remove('flash');
   ```

2. **A-2**: `capDepth > 1` の早期 return を抜けても、`barTimer` を消した直後だと
   バーは既に `capturing` クラスを持っている。その状態で `classList.add` しても
   no-op なので `transitionend` が飛ばず、`CAP_FALLBACK_MS`（500ms）まで無駄に待つ。

   ```js
   if (bar.classList.contains('capturing')) { resolve(); return; }
   ```

   ※ `const bar = els.bar;` の直後に置く。

**根拠の確度**: CSS のアニメ時間（`.bar` transition 240ms / `.frame.flash` 500ms）と
JS 定数（`CAP_FALLBACK_MS` 500 / `BAR_RETURN_MS` 170）からの**タイミング計算による導出**で、
実ブラウザでの再現は未確認。修正自体は無害だが、報告時はこの点を正直に書くこと。

### A-3 — `capture.py:174` の無条件 `[saved]` ログ

`_step`（`capture.py:59`）が png / txt / part の例外を握り潰すため、
**全部失敗しても `[saved]` が出る**。`--noconsole` 配布でログが唯一の運用情報なので直す。

`_step` に成功記録用のリストを渡せるようにするのが最小変更:

```python
@contextmanager
def _step(tag: str, url: str, done=None):
    try:
        yield
        if done is not None:
            done.append(tag)
    except Exception as e:
        log(f"[skip {tag}] {url}  ({e})")
```

`_capture` 側で `done = []` を用意し、**png / txt / part の 3 つだけ**に `done` を渡す
（`load` / `title` は保存物ではないので渡さない）。最後を:

```python
if done:
    log(f"[saved] {stem}.*  ({','.join(done)})  <- {url}")
else:
    log(f"[保存できず] {stem}  <- {url}")
```

**注意**: png ステップは `_step` の内側にさらに `try/finally` がある
（`finally` で `captureEnd` を必ず呼ぶ）。`screenshot` が例外を投げれば
`_step` が捕まえるので `done` には積まれない — この挙動は正しいので変えないこと。

### A-6 — `config.py:67` の encoding

```python
parser.read(CONFIG_PATH, encoding="utf-8")
```
を
```python
parser.read(CONFIG_PATH, encoding="utf-8-sig")
```
に変更するだけ。BOM の有無どちらでも読める。

`USAGE.txt` が「config.ini をメモ帳で編集」と案内しているため、BOM 混入時に
`MissingSectionHeaderError` → 「config.ini の読み込みに失敗しました」となり、
利用者が原因に辿り着けない。

---

## 4. テストの追加

現状 `_capture` / `CaptureRunner` は未テスト。最低限、以下を足す。

- **A-3 の回帰** (`tests/test_capture.py`): `_step` に `done` を渡して、
  例外なし → tag が積まれる／例外あり → 積まれない、を検証。純粋なので簡単。
- **A-1 の回帰** (`tests/smoke_badge.py`): 既存の 3) ヘルパ確認の後に、
  `captureEnd()` → 直後に `captureStart()` を呼び、`.frame` に `flash` クラスが
  残っていないことを確認するステップを足す。
  smoke は Edge 必須なので、macOS で実行できなければ**コードだけ足して未実行と報告**する。

---

## 5. コードベース固有の落とし穴

このリポジトリには、知らないと壊す仕掛けがいくつかある。

1. **`badge.js` の `$CONFIG` 置換** — `badge.py:109` が
   `src.replace("$CONFIG", json.dumps(config))` で単純置換している。
   `badge.js` 内でテンプレートリテラルの `${...}` 補間を**新たに使ってはいけない**
   （`$CONFIG` と衝突しうるため、現状は意図的に避けてある）。
   CSS の時間と JS の定数も、この制約ゆえ手動で整合を取っている（`badge.js:44` のコメント参照）。

2. **Python 3.8 互換** — `pyproject.toml` の `requires-python = ">=3.8"`。
   `edge_auto_capture.py:117` の `self.seen: "dict[Page, str]" = {}` のように、
   **インスタンス属性の注釈は文字列で書く**こと（素の `dict[...]` は 3.8 で実行時 TypeError）。
   関数ローカル変数の注釈は評価されないので素で書いてよい。

3. **例外の握り潰しは意図的** — `try_eval` / `_step` / `infra` の各 `except: pass` は
   堅牢性のための設計で、各所にコメントがある。**「握り潰しを直す」方向のリファクタはしない。**
   `ruff` も `B008` を意図的に ignore している。

4. **`USAGE.txt` は Shift-JIS** — 編集する場合は文字コードを維持すること
   （`README.md` / `docs/` は UTF-8）。

5. **バインディング名は 2 箇所に存在** — `badge.py:120-126` の `BIND_*` と
   `badge.js` 内の `window.__eac_*` 呼び出し。言語境界のため一元化できていない。
   片方だけ変えると **JS 側の try/catch で無言失敗する**（気づけない）。

---

## 6. 検証手順

```bash
pip install -e ".[dev]"
pytest
ruff check .
python tests/smoke_badge.py   # Edge があれば。SKIP されたら「未検証」と報告する
```

`ruff` は `line-length = 100`、`select = ["E","F","I","UP","B"]`。

---

## 7. コミットについて

利用者はまだコミットを指示していない。**勝手にコミット／プッシュしないこと。**
変更が揃った時点で内容を報告し、コミットするか確認する。

コミットメッセージは既存の慣習（日本語・`種別: 内容` 形式）に合わせる。
例: `修正: 連続撮影時にシャッターフラッシュが次のスクショへ写り込む問題`

`docs/` の 3 文書は `07452a6` でコミット済み（**未 push**）。
push するかどうかも利用者の判断なので、勝手に push しないこと。
