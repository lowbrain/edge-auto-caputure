# このコードを触る人へ

「知らないと壊す仕掛け・作業環境・検証手順」に絞った恒久メモ。**変更前に §1 を読むこと。**

- **これから作る / 直すもの（残タスクと優先順）は Issue [#38](https://github.com/lowbrain/edge-auto-caputure/issues/38)（ピン留め）が正。**
- ツールの仕組み・設定リファレンス・ビルド・配布は [`README.md`](README.md)。
- 過去の完了作業の実装内容は git 履歴を参照。

## 課題タグ（`A-` / `B-` / `D-` / `E-` / `F-` / `R`）の凡例

コード中のコメントに散在する `A-3` / `B-1` / `D-C1` / `E-6` / `F-D3` / `R5b` のような ID は、
**退役済みの旧文書由来の安定した識別子**。会話や履歴での参照用で、「どのファイルか」の意味はもう持たない。
接頭辞ごとの由来と意味は次のとおり。

| 接頭辞 | 由来（退役済みの旧文書） | 意味 | 例 |
|---|---|---|---|
| `A-` | `IMPROVEMENTS.md` §A「バグ／実害があるもの」 | 実害の出る不具合の対策 | `A-1` シャッターフラッシュの写り込み |
| `B-` | `IMPROVEMENTS.md` §B「設計上の改善」 | 設計・挙動の改善 | `B-3` 撮影キューの合流（無制限化の防止） |
| `E-` | `IMPROVEMENTS.md` §E「ページへの影響・未定義動作」 | 見に行ったページへの副作用・未定義動作の抑止 | `E-3` サイト側からの存在検知の防止 |
| `D-` | `DISTRIBUTION.md`（§A 法務 / §B サポート性 / §C 第三者環境で壊れる箇所 / §D 導入障壁 / §E 配布物） | 第三者へ配って動かすための対策 | `D-C1` 書き込み不可時の退避 |
| `F-` | `FEATURES.md`（§A 保存物の価値 / §B 撮影品質 / §C 運用の実用性 / §D 操作性） | 機能追加 | `F-A1` 索引 CSV |
| `R` | `REFACTORING.md` §2「基盤リファクタの中身」 | 振る舞いを変えない内部整理 | `R5b` 副作用の分離 |

- **ID の形**: `D-` / `F-` は「節の英字＋連番」が続く（`D-C1` / `F-A1`）。
  `A-` / `B-` / `E-` / `R` は連番のみで、細分は末尾に英字を足す（`A-1` / `R3b` / `R5a`）。
- **個々のタグが何を指し、いま済んでいるかは Issue [#38](https://github.com/lowbrain/edge-auto-caputure/issues/38) が正。**
  ここに全タグの一覧は作らない（二重管理になる。§4 末尾の方針）。この表は接頭辞の凡例だけを持つ。
- 旧文書の本文は git 履歴に残っている。退役コミットは
  `git log --all --diff-filter=D --name-only --oneline -- 'docs/*'` で辿れ、
  そのコミットの親から `git show <コミット>^:docs/IMPROVEMENTS.md` のように読める。

---

## 1. コードベース固有の落とし穴（必ず読む）

知らないと壊す仕掛け。

1. **`badge.js` の `$CONFIG` 置換** — `badge.py` の `build_badge_script()` が
   `src.replace("$CONFIG", json.dumps(config))` で**単純置換**している。
   `badge.js` 内でテンプレートリテラルの `${...}` 補間を**新たに使ってはいけない**
   （`$CONFIG` と衝突しうるため、現状は意図的に避けてある）。
   CSS の時間と JS の定数も、この制約ゆえ手動で整合を取っている（`badge.js` の定数定義付近のコメント参照）。

2. **Python は 3.9+**（`pyproject.toml` の `requires-python = ">=3.9"`、ruff の `target-version = "py39"`）。
   PEP 585 の `dict[...]` / `set[...]` / `tuple[...]` を**素で書いてよい**。
   ただし `X | Y` 記法は 3.10 以降なので使わないこと。`Optional[Path]` は `typing` から import。

   > **かつては 3.8 だったため注釈を文字列で書く決まりがあった。** `ruff --fix` がその引用符を
   > 外して黙って互換を壊す罠だったので 3.9 へ上げて根治した。
   > **古いコミットのコメントに「文字列で書くこと」とあっても、もう従わなくてよい。**

3. **例外の握り潰しは意図的** — `try_eval` / `_step` / `infra` の各 `except: pass` は
   堅牢性のための設計で、各所にコメントがある。**「握り潰しを直す」方向の一括リファクタはしない。**
   利用者に伝わらない握り潰し（無言終了・嘘のログ）への対処は済んでいるが、
   握り潰しそのものを消す作業ではない。`ruff` も `B008` を意図的に ignore している。

4. **`USAGE.txt` は Shift-JIS** — 編集する場合は文字コードを維持すること
   （`README.md` / `CONTRIBUTING.md` は UTF-8）。

   > **実務上の注意**: `iconv` で UTF-8 へ出して編集し、`iconv -f UTF-8 -t SHIFT_JIS` で戻すのが安全。
   > このとき、**ASCII のバックスラッシュ `\` は Shift-JIS へ変換できずエラーになる**ので、
   > パス区切りは既存記述と同じ `¥`（U+00A5）で書く。**絵文字も Shift-JIS には入らない**ので、
   > バーのラベルを引用するときは絵文字を落として「保存先」のように書く。
   > 書き戻したら往復変換して元と一致するか確かめること。

5. **バインディング名は 2 箇所に存在** — `badge.py` の `BIND_*` 定数群と、
   `badge.js` 内の `BINDING_NAMES` / `callBinding('__eac_*')`。言語境界のため一元化できていない。
   片方だけ変えると **無言失敗する**（気づけない）。
   `badge.js` 側でバインディングを追加するときは `BINDING_NAMES` にも足すこと。

6. **`badge.js` のシャドウは `closed`・呼び出しは `callBinding` 経由**（`A-4` 対応済み）。
   - `host.shadowRoot` は `null` を返す。中を触るテストは
     `window.__eac_debugRoot()`（token 無しビルドでのみ公開）を使う
   - `window.__eac_toggle(...)` のような**直接呼び出しを新たに書かないこと**。
     必ず `callBinding('__eac_toggle', TOK, ...)` を使う（サイト側が差し替えた関数へ token を渡さないため）
   - `mode: 'open'` に戻すとスモークテストが失敗する（回帰チェックを入れてある）
   - **固定名を `window` に生やさない**（`E-3` 対応済み・存在検知の防止）:
     - Python→ページのヘルパは固定名（`window.__eacApplyState` 等）ではなく、起動ごとの
       ランダム名 `ns`（`badge.new_namespace()`）の**非列挙**プロパティ `window[ns]` に
       まとめて公開する。呼び出し式は `badge.*_call(ns, ...)`（`body_text_call` / `sig_call` /
       `capture_start_call` / `capture_end_call` / `apply_state_call` / `set_count_call` /
       `set_history_call`）が `ns` 込みで組み立てる。**`ns` を第1引数に取る**ので、呼び出し側
       （`edge_auto_capture` の `self.ns`・`capture` の `runner.ns`）から必ず渡すこと。
     - ページ→Python の `expose_binding` 固定名（`__eac_toggle` 等）は、`badge.js` 冒頭で本物の
       参照を `BOUND` へ退避したうえで `delete window[name]` して消す。この退避＋削除は
       **最上位フレームの早期 `return` より前**で全フレーム分行う（iframe にも生えるため）。
       token 無し（スモーク）ビルドは削除せず `callBinding` の実行時フォールバックに任せる。
     - `window.__eacApplyState` 等の**固定名代入を復活させない**こと（`E-3` が無効化する）。
       スモークテストの手順 13 が `'__eacApplyState' in window` / `'__eac_toggle' in window` を
       回帰チェックしている。

7. **`ruff check` / `mypy` は緑（終了コード 0）が正常** — **指摘が出たらそれは新しく入れた問題**なので直すこと。

8. **`[tool.mypy] python_version = "3.10"` と `requires-python = ">=3.9"` の食い違いは意図的** —
   mypy 2.3.0 が 3.9 ターゲットを廃止したため引き上げた。**これは mypy の型検査ターゲット設定であって、
   実行系の要件ではない**（実行時は 3.9 のままで、macOS の Python 3.9 でも緑）。
   知らずに 3.9 へ戻すと mypy が動かなくなる。

9. **`infra._message_box_windows` の `ctypes.windll` は `Any` 経由の属性アクセスが正** —
   `# type: ignore[attr-defined]` は macOS スタブでは必要・Windows スタブでは不要で、
   `warn_unused_ignores = true` ゆえ **OS 次第で必ず片方が落ちる**。だから ignore を撤去して
   `Any` 経由に変えてある。`getattr` は ruff の `B009` と衝突するため不採用。

10. **新モジュール（`lineage.py` / `browser.py` / サニタイズ器 等）を足すときは `py-modules` を更新** —
    漏れると配布が**黙って割れる**。[`pyproject.toml`](pyproject.toml) の
    `[tool.setuptools] py-modules` へモジュール名を追加する。**手で追記が要るのはここ 1 箇所だけ。**
    `[tool.mypy]` は `files = ["."]` + `exclude` 方式なので追記不要（列挙方式は新モジュールを
    検査対象へ入れ忘れて型エラーが素通りする罠で、#40 で実際に起きたため構造的に潰した。
    理由は [`pyproject.toml`](pyproject.toml) の `[tool.mypy]` のコメント参照。**列挙方式へ戻さないこと**）。
    `ruff` も `extend-exclude` 方式なので追記不要、[`build.ps1`](build.ps1) は import 追従なので変更不要。
    **CI も `mypy .`（対象は `[tool.mypy] files` 由来）で回す。**CI 側に検査対象を列挙し直さないこと —
    引数は `files` を上書きするので、`files = ["."]` にしていても CI だけ新モジュールを検査しなくなる。

11. **コメントは資産。関数を移動するときは一緒に運ぶ** — 各所の日本語コメントは
    `A-1` / `A-2` / `B-3` / `E-6` 等の落とし穴回避の記録。リファクタで関数を移すときも
    コメントを削らない・要約しない。**接頭辞の意味は冒頭の「課題タグの凡例」を参照。**

> **行番号について**: このファイルは意図的に行番号ではなく**シンボル名**で場所を指している。
> 過去に行番号で書いた参照はコードの成長で軒並みずれた。

---

## 2. 作業環境の注意

開発ホストは **macOS (darwin)**、本ツールは **Windows 専用**。

| 事項 | 状況 |
|------|------|
| `pytest` / `ruff` / `mypy` | `pip install -e ".[dev]"` を先に。`ruff check` / `mypy` は緑（終了コード 0）が正常 |
| `tests/smoke_badge.py` | 実 Edge/Chrome が要る。無ければ `SKIP`（終了コード 0）で抜けるので **PASS 表示を鵜呑みにしない**（`--strict` で FAIL 化） |
| `infra._message_box` | Windows は `ctypes.windll`、macOS は `osascript`（開発機での確認用に分岐追加済み） |
| `build.ps1` | PowerShell / Windows 専用。macOS では実行検証できない |
| `%LOCALAPPDATA%` | `D-C1` の退避先。macOS には存在せず `tempfile.gettempdir()` へフォールバック |

**両 OS でカバレッジが相補的**な点に注意。Windows では `test_resolve_writable_dir_*` 2 件
（`D-C1` の退避ロジック）が POSIX chmod の効かなさゆえ self-skip され、macOS では実行されて通る。
片方の OS だけで「全部通った」と判断しないこと。

---

## 3. 検証手順（4 点セット）

変更のたびに **pytest / smoke / ruff / mypy の 4 点**を全部緑にする。

```bash
pip install -e ".[dev]"
pytest                                 # 速い純粋関数・token 照合・DL の回帰
python tests/smoke_badge.py --strict   # 実 Edge。バー構築・SPA検知・写り込み防止・検知不能化(E-3)・JS エラー無し
ruff check .
mypy .
```

- **smoke は実 Edge/Chrome 必須**（Windows で実行）。手元に無ければ `SKIP`（終了コード 0）で抜けるので
  **PASS 表示を鵜呑みにしない**。CI・検証では **`--strict`** を付けて FAIL 化する（付けないと
  ブラウザ不在環境で「何も検証せず緑」になる）。SKIP されたら報告では「未検証」と明記する。
- **新モジュールを足したときは** §1-10 のとおり `[tool.setuptools] py-modules` を更新する
  （`[tool.mypy]` は `files = ["."]` + `exclude` 方式なので追記不要。列挙方式へ戻さないこと）。
- リファクタでは**新規テストを足せる場所は足す**（純粋関数・判定ロジック・レジストリ等はブラウザ無しで単体化できる）。
- `ruff` は `line-length = 120`、`select = ["E", "F", "I", "UP", "B"]`。

CI（GitHub Actions）でも同じ 4 点が回る（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）。
ジョブは 3 本に分かれていて、`ruff` + `mypy` と `pytest` は Linux、**smoke は実 Edge が要るので Windows runner** で
`--strict` 付きで回す。

---

## 4. コミット・報告の慣習

- **1 件ずつ 1 コミット**。まとめない。
- コミットメッセージは既存の慣習（日本語・`種別: 内容` 形式）に合わせる。
  例: `修正: ダウンロードの保存先を output 配下へ明示する（E-4）`
- **push / タグ付けは利用者に確認**してから。
- ドキュメント（`README.md` / `CONTRIBUTING.md` / `USAGE.txt`）を直したら、その旨を報告に含める。
- **未検証の項目は必ず「未検証」と明記する。** 憶測で「動作を確認しました」と書かない。
- 残タスクの状態は Issue 側が正。**ドキュメントに残タスクの一覧を作らない**
  （二重管理になり、実際に食い違いが起きた。経緯は [#38](https://github.com/lowbrain/edge-auto-caputure/issues/38) 冒頭）。
