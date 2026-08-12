# 引き継ぎメモ（Opus 4.8 向け）

宛先: 次に作業するモデル（Opus 4.8 を想定）／担当者
作成日: 2026-08-11
ブランチ: `refactor/spa-event-driven`（作業ツリーはクリーン、未 push）
コード起点コミット: `eac2b92` — **ここから先、コードは 1 行も変わっていない**
文書コミット: `07452a6` 以降（`docs/` のみ）

---

## 0. 最初に渡す指示（そのままコピーして使える）

> このリポジトリの `docs/HANDOFF.md` を読んで、そこに書かれた 5 件の修正を
> 上から順に実装してください。各件は独立しているので、1 件ずつコミットしてください。
> `docs/ROADMAP.md` に全体の優先度、`docs/IMPROVEMENTS.md` と
> `docs/DISTRIBUTION.md` に各指摘の根拠があります。
> 作業前に `pip install -e ".[dev]"` を実行してください。
> 実 Edge が無い環境では smoke テストが SKIP になります。その場合は
> 「未検証」と明記して報告してください。勝手にコミット以上のこと
> （push / タグ付け / ブランチ作成）はしないでください。

---

## 1. 経緯と文書の地図

コード全体（Python 5 ファイル + `badge.js` + `tests/` + `build.ps1`）を通読したレビューを行い、
結果を 4 文書に分けてある。**着手前に最低限 `ROADMAP.md` は読むこと。**

| 文書 | 項番 | 内容 |
|------|------|------|
| [`ROADMAP.md`](ROADMAP.md) | — | **3 文書を横断した単一の優先順位。まずここ** |
| [`IMPROVEMENTS.md`](IMPROVEMENTS.md) | `A-` `B-` `E-` | 不具合・設計・運用・ページへの影響（A〜E 節） |
| [`FEATURES.md`](FEATURES.md) | `F-` | 機能提案（今回のスコープ外） |
| [`DISTRIBUTION.md`](DISTRIBUTION.md) | `D-` | 第三者配布に向けた対応 |
| [`DECISIONS.md`](DECISIONS.md) | — | **未決の 3 件。所有者の判断が要る部分と、任せてよい部分の切り分け** |

> **`DECISIONS.md` の扱いに注意。** ライセンス選定（`D-A1`）と証明書取得（`D-D1`）は
> **所有者の権限に属し、エージェントが決めてはいけない**。
> 各件に【任せてよい】と【所有者の確認が要る】を書き分けてあるので、
> 前者を進め、後者に達したら**止まって提示すること。**
> `F-C1`（プロファイル永続化）だけは方針 (b) の了承があれば実装まで完了できる。

**コードの修正はまだ 1 行も行っていない。** 追加したのは `docs/` の文書のみ。
つまり **`eac2b92` 時点のコードがそのまま残っている**状態から着手することになる。

---

## 2. 作業環境の注意（重要）

このツールは **Windows 専用**だが、開発ホストは **macOS (darwin 25.5.0)**。

| 事項 | 状況 |
|------|------|
| `pytest` / `ruff` | **システム python3 に未インストール**。`pip install -e ".[dev]"` から始める |
| `tests/smoke_badge.py` | 実 Edge が必要。macOS でも Edge があれば動く可能性はあるが**未検証**。Edge が無ければ `SKIP`（終了コード 0）で抜ける仕様なので、**PASS 表示を鵜呑みにしないこと** |
| `infra._message_box` | `ctypes.windll` 依存。macOS では except で握られ no-op になる |
| `build.ps1` | PowerShell / Windows 専用。macOS では実行検証できない |
| `%LOCALAPPDATA%` | 下記 S-1 で使うが、macOS には存在しない。フォールバック先の決定はプラットフォーム非依存に書き、**実挙動の確認は Windows に委ねる** |

つまり **`pytest` は動かせるが、`badge.js` と Windows 固有の挙動は実機確認できない可能性が高い。**
確認できなかったものは、報告に**「未検証」と明記**すること。憶測で「動作を確認しました」と書かない。

---

## 3. 今回のスコープ（この順に、1 件ずつコミット）

`ROADMAP.md` の第 1 群と第 2 群の一部。**合計で半日程度。**

| # | ID | 内容 | 主な対象ファイル |
|---|-----|------|------------------|
| S-1 | `D-C1` | 書き込み不可の場所で無言終了する | `infra.py` `config.py` `edge_auto_capture.py` |
| S-2 | `A-3` | 全失敗でも `[saved]` とログに出る | `capture.py` |
| S-3 | `D-B1` | バージョンが exe にもログにも出ない | `infra.py` `pyproject.toml` `edge_auto_capture.py` |
| S-4 | `A-1` + `A-2` | フラッシュ写り込み / 500ms 待ち | `badge.js` |
| S-5 | `A-6` | BOM 付き config.ini が読めない | `config.py` |

**スコープ外**（利用者の判断が要る、または別トラック）:
`A-5` `B` 系 `E-1`〜`E-3` `E-5` `E-6`、`FEATURES.md` の全項目、
`DISTRIBUTION.md` の `D-A1`（ライセンス選定）`D-D1`（証明書）。

> **`A-4` は対応済み**（`badge.js` を `mode:'closed'` 化＋バインディング参照の退避）。
> スコープからは外れているが、**`badge.js` を触る S-4 では影響を受ける**ので、
> 下記の落とし穴 6 を必ず読むこと。

> **旧スコープからの変更**: 当初は `A-1 / A-2 / A-3 / A-6` としていたが、
> `ROADMAP.md` の順位に合わせて `D-C1` と `D-B1` を追加した。理由はそちらに記載。

---

## 4. 各修正の具体案

### S-1 — `D-C1` 書き込み不可の場所で無言終了する

**症状**: `C:\Program Files\` など書き込み権限のない場所へ展開されると、
利用者から見て「**ダブルクリックしても何も起きない**」。ログもダイアログも残らない。

**原因の連鎖**:

1. `log()` は `except: pass`（`infra.py:66`）で静かに失敗
2. `set_log_dir` の `mkdir` も `except: pass`（`infra.py:50`）で静かに失敗
3. `edge_auto_capture.py:321` の `config.output_dir.mkdir(...)` は**保護されておらず**
   `PermissionError` を送出
4. `--noconsole` ビルドなので stderr は誰にも見えない

**方針**: 書き込み可能なフォルダを解決するヘルパを `infra.py` に足し、
`config.py` の `output_dir` 確定時に通す。`%LOCALAPPDATA%` へ退避して**動き続ける**。

```python
# infra.py
def resolve_writable_dir(preferred: Path) -> Optional[Path]:
    """書き込み可能なフォルダを返す。preferred が使えなければ退避先を試す。
    どこにも書けなければ None（呼び出し側が notify_fatal する）。"""
    fallback_base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    candidates = [preferred, Path(fallback_base) / "edge-auto-capture" / preferred.name]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".eac-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            continue
    return None
```

要件:

- 退避が発生したら **`log()` と `notify_fatal` の両方で利用者に知らせる**
  （どこへ保存されたか分からないほうが困る）
- `edge_auto_capture.py:321` の裸の `mkdir` は、解決済みパスを使う形にするか
  `try` で包んで `notify_fatal` する。**例外が素通りする経路を残さないこと**
- `set_log_dir`（`infra.py:41`）も同じ解決結果を使い、
  ログが確実に書ける場所へ向くようにする

**テスト**: `tmp_path` に読み取り専用ディレクトリを作り、フォールバックが働くことを検証。
`os.chmod` が効かない環境（Windows の一部）ではスキップ扱いにしてよい。

### S-2 — `A-3` 全失敗でも `[saved]` とログに出る

`_step`（`capture.py:59`）が png / txt / part の例外を握り潰すため、
**全部失敗しても `capture.py:174` の `[saved]` が出る**。
`--noconsole` 配布でログが唯一の運用情報なので直す。

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

> **注意**: png ステップは `_step` の内側にさらに `try/finally` がある
> （`finally` で `captureEnd` を必ず呼ぶ）。`screenshot` が例外を投げれば
> `_step` が捕まえるので `done` には積まれない — **この挙動は正しいので変えないこと。**

**テスト**: `_step` に `done` を渡し、例外なし → tag が積まれる／例外あり → 積まれない。純粋なので簡単。

### S-3 — `D-B1` バージョンが exe にもログにも出ない

`version = "0.1.0"` は `pyproject.toml:3` にあるだけで、
**exe にもログにも UI にも一切出ていない**（`grep` で確認済み）。
第三者から「動きません」と言われたときに「どの版ですか」に答えられない。

最低限やること:

1. `infra.py` に `__version__ = "0.1.0"` を置く（`infra` は依存の最下層なので循環しない）
2. `pyproject.toml` を `dynamic = ["version"]` +
   `[tool.setuptools.dynamic] version = {attr = "infra.__version__"}` にして**出所を 1 つにする**
3. `edge_auto_capture.py:378` の
   `log("=== edge-auto-capture 起動 ===")` にバージョンを入れる

余力があれば（任意）:

- `build.ps1` に PyInstaller の `--version-file` を足し、exe のプロパティにも出す
  （Windows 固有・実機検証が要るので、できなければ見送ってよい）
- `D-B2`（環境情報のログ）も 2 行程度で入る。起動時に **Edge のバージョン・OS・
  採用された設定値**を 1 行出すと切り分けが一気に楽になる

**テスト**: `pyproject.toml` と `infra.__version__` が一致することを検証すると、
片方だけ上げる事故を防げる。

### S-4 — `A-1` / `A-2` `badge.js` の `captureStart`（`badge.js:259` 付近）

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

**A-1**: `barTimer` のクリア直後に、進行中のシャッターフラッシュも畳む。
これを入れないと、直前の撮影のフラッシュ（`.frame.flash`、CSS 500ms）が
次のスクリーンショットに赤みとして写り込む。

```js
if (frameTimer) { clearTimeout(frameTimer); frameTimer = null; }
if (els.frame) els.frame.classList.remove('flash');
```

**A-2**: `capDepth > 1` の早期 return を抜けても、`barTimer` を消した直後だと
バーは既に `capturing` クラスを持っている。その状態で `classList.add` しても
no-op なので `transitionend` が飛ばず、`CAP_FALLBACK_MS`（500ms）まで無駄に待つ。
`const bar = els.bar;` の直後に置く:

```js
if (bar.classList.contains('capturing')) { resolve(); return; }
```

> **根拠の確度**: CSS のアニメ時間（`.bar` transition 240ms / `.frame.flash` 500ms）と
> JS 定数（`CAP_FALLBACK_MS` 500 / `BAR_RETURN_MS` 170）からの**タイミング計算による導出**で、
> 実ブラウザでの再現は未確認。修正自体は無害だが、**報告時はこの点を正直に書くこと。**

**テスト**: `tests/smoke_badge.py` に、`captureEnd()` → 直後に `captureStart()` を呼び、
`.frame` に `flash` クラスが残っていないことを確認するステップを足す。
macOS で実行できなければ**コードだけ足して未実行と報告**する。

### S-5 — `A-6` BOM 付き config.ini が読めない

`config.py:67` の

```python
parser.read(CONFIG_PATH, encoding="utf-8")
```

を

```python
parser.read(CONFIG_PATH, encoding="utf-8-sig")
```

に変更するだけ。BOM の有無どちらでも読める。

`USAGE.txt` が「config.ini をメモ帳で編集」と案内しているため、BOM 混入時に
`MissingSectionHeaderError` →「config.ini の読み込みに失敗しました」となり、
利用者が原因に辿り着けない。

**テスト**: BOM 付きの一時 config.ini を書いて `load_config()` が成功することを検証。
既存の `_write_config`（`tests/test_capture.py:114`）を流用できる。

---

## 5. コードベース固有の落とし穴

知らないと壊す仕掛けがある。

1. **`badge.js` の `$CONFIG` 置換** — `badge.py:109` が
   `src.replace("$CONFIG", json.dumps(config))` で単純置換している。
   `badge.js` 内でテンプレートリテラルの `${...}` 補間を**新たに使ってはいけない**
   （`$CONFIG` と衝突しうるため、現状は意図的に避けてある）。
   CSS の時間と JS の定数も、この制約ゆえ手動で整合を取っている（`badge.js:44` のコメント参照）。

2. **Python は 3.9+**（`pyproject.toml` の `requires-python = ">=3.9"`、`target-version = "py39"`）。
   PEP 585 の `dict[...]` / `set[...]` / `tuple[...]` を**素で書いてよい**
   （インスタンス属性・dataclass フィールドの注釈も実行時評価が通る）。
   ただし `X | Y` 記法は 3.10 以降なので使わないこと。
   `Optional[Path]` を使うなら `typing` から import する。

   > **かつては 3.8 だったため、注釈を文字列で書く決まりがあった。**
   > `ruff --fix` がその引用符を外して黙って互換を壊す罠になっていたので、
   > 3.9 へ上げて根治した。**古いコミットのコメントに「文字列で書くこと」と
   > 書いてあっても、もう従わなくてよい。**

3. **例外の握り潰しは意図的** — `try_eval` / `_step` / `infra` の各 `except: pass` は
   堅牢性のための設計で、各所にコメントがある。
   **「握り潰しを直す」方向の一括リファクタはしない。**
   S-1 と S-2 は「握り潰しの結果が利用者に伝わらない」ことへの対処であって、
   握り潰しそのものを消す作業ではない。
   `ruff` も `B008` を意図的に ignore している。

4. **`USAGE.txt` は Shift-JIS** — 編集する場合は文字コードを維持すること
   （`README.md` / `docs/` は UTF-8）。

5. **バインディング名は 2 箇所に存在** — `badge.py:120-126` の `BIND_*` と
   `badge.js` 内の `BINDING_NAMES` / `callBinding('__eac_*')`。
   言語境界のため一元化できていない。
   片方だけ変えると **無言失敗する**（気づけない）。
   `badge.js` 側でバインディングを追加するときは `BINDING_NAMES` にも足すこと
   （足さなくてもフォールバックで動いてしまうため、抜けに気づきにくい）。

6. **`badge.js` のシャドウは `closed`・呼び出しは `callBinding` 経由**（A-4 対応済み）。
   - `host.shadowRoot` は `null` を返す。中を触るテストは
     `window.__eac_debugRoot()`（token 無しビルドでのみ公開）を使う
   - `window.__eac_toggle(...)` のような**直接呼び出しを新たに書かないこと**。
     必ず `callBinding('__eac_toggle', TOK, ...)` を使う。直接呼ぶと、
     サイト側が差し替えた関数へ token を渡してしまう
   - `mode: 'open'` に戻すとスモークテストが失敗する（回帰チェックを入れてある）

7. **`ruff check` は緑（終了コード 0）が正常** — 以前は UP037 が 3 件出る状態だったが、
   Python 3.9 化で解消済み。**指摘が出たらそれは新しく入れた問題**なので直すこと。

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

## 7. コミット・報告について

- **1 件（S-1〜S-5）ごとに 1 コミット。** まとめない
- コミットメッセージは既存の慣習（日本語・`種別: 内容` 形式）に合わせる
  例: `修正: 保存先が書き込み不可のとき無言終了せず退避先へフォールバックする`
- **push / タグ付け / ブランチ作成はしない。** 利用者に確認する
- 文書（`docs/`）を直す必要が出たら直してよいが、**その旨を報告に含める**
- 未検証の項目は必ず「未検証」と明記する

### 終わったあとに利用者へ確認すべきこと

| 項目 | 内容 |
|------|------|
| `A-4` の実機確認 | **対応済みだが実 Edge で未検証**（macOS 開発ホストのため SKIP。代替ブラウザでの実走は利用者判断で省略）。**Windows でスモークを 1 回通すこと** — これが最初のタスクとして適切 |
| **未決 3 件** | [`DECISIONS.md`](DECISIONS.md) を提示する。`D-A1` ライセンス / `D-D1` 証明書 / `F-C1` プロファイル永続化。**判断を仰ぐ形にすること。代わりに決めない** |
| `B-6` | MutationObserver 常時稼働（`ROADMAP.md` 11 位）。`badge.js` の小変更で全利用者の負荷が下がる |
| `E-4` | ダウンロード消失（`ROADMAP.md` 5 位）。**実機検証が先** |
| `D-A3` | プライバシー注意書き（`ROADMAP.md` 6 位）。文面は法務確認が要るかもしれない |

### 判断を待たずに進めてよいもの

`DECISIONS.md` に挙げた 3 件のうち、以下は**回答を待たずに着手できる**。
手が空いたらここから片付けるとよい。

- **`D-A2`** サードパーティ表記（`THIRD-PARTY-NOTICES.txt`）— ライセンス選定に依存しない
- **`D-D3`** ZIP の SHA256 併記 — 署名が無い間の完全性確認手段になる
