# 引き継ぎメモ（このコードを触る人向け）

宛先: 次に作業するモデル／担当者
**最終整理: 2026-08-14（コミット `e07bb34` 時点。docs 統合に伴い恒久情報へ絞った）**

> このファイルは「知らないと壊す仕掛け・作業環境・検証手順」に絞った恒久メモ。
> **これから何を作る/直すか（残タスクと優先順）は [`ROADMAP.md`](ROADMAP.md) が正**、
> 配布まわりは [`DISTRIBUTION.md`](DISTRIBUTION.md)。過去の完了作業の実装内容は git 履歴を参照。
> `A-`/`B-`/`E-`/`F-`/`D-` の ID は旧文書（`IMPROVEMENTS.md`/`FEATURES.md` は統合削除済み）由来の
> 安定した識別子。

---

## 1. コードベース固有の落とし穴（必ず読む）

知らないと壊す仕掛け。**ここは今も正確。**

1. **`badge.js` の `$CONFIG` 置換** — `badge.py:109` が
   `src.replace("$CONFIG", json.dumps(config))` で単純置換している。
   `badge.js` 内でテンプレートリテラルの `${...}` 補間を**新たに使ってはいけない**
   （`$CONFIG` と衝突しうるため、現状は意図的に避けてある）。
   CSS の時間と JS の定数も、この制約ゆえ手動で整合を取っている（`badge.js:82` 付近のコメント参照）。

2. **Python は 3.9+**（`pyproject.toml` の `requires-python = ">=3.9"`、`target-version = "py39"`）。
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
   （`README.md` / `docs/` は UTF-8）。プライバシー注記・問い合わせ先の追記で既にここを触っている。

5. **バインディング名は 2 箇所に存在** — `badge.py:120-126` の `BIND_*` と
   `badge.js` 内の `BINDING_NAMES` / `callBinding('__eac_*')`。言語境界のため一元化できていない。
   片方だけ変えると **無言失敗する**（気づけない）。
   `badge.js` 側でバインディングを追加するときは `BINDING_NAMES` にも足すこと。

6. **`badge.js` のシャドウは `closed`・呼び出しは `callBinding` 経由**（A-4 対応済み）。
   - `host.shadowRoot` は `null` を返す。中を触るテストは
     `window.__eac_debugRoot()`（token 無しビルドでのみ公開）を使う（`badge.js:362-366` 付近）
   - `window.__eac_toggle(...)` のような**直接呼び出しを新たに書かないこと**。
     必ず `callBinding('__eac_toggle', TOK, ...)` を使う（サイト側が差し替えた関数へ token を渡さないため）
   - `mode: 'open'` に戻すとスモークテストが失敗する（回帰チェックを入れてある）

7. **`ruff check` は緑（終了コード 0）が正常** — UP037 は Python 3.9 化で解消済み。
   **指摘が出たらそれは新しく入れた問題**なので直すこと。`mypy` も導入済み（緑が正常）。

8. **新モジュール（`lineage.py` / `browser.py` / サニタイズ器 等）を足すときは 2 箇所を同時更新** —
   漏れると配布/CI が黙って割れる。[`pyproject.toml`](../pyproject.toml) の
   `[tool.setuptools] py-modules`（~L35）と `[tool.mypy] files`（~L57）の**両方**へモジュール名を追加する。
   `ruff` は `extend-exclude` 方式なので追記不要、[`build.ps1`](../build.ps1) は import 追従なので変更不要。
   （旧 `REFACTORING.md` §4 の恒久ルールをここへ集約）

9. **コメントは資産。関数を移動するときは一緒に運ぶ** — 各所の日本語コメントは
   A-1/A-2/B-3/E-6 等の落とし穴回避の記録。リファクタで関数を移すときもコメントを削らない・要約しない。
   （旧 `REFACTORING.md` §0 の大原則より）

---

## 2. 未検証事項（憶測で「確認済み」と書かないこと）

開発ホストは **macOS (darwin)**、本ツールは **Windows 専用**。

> **2026-08-15 に Windows 11 + 実 Edge `151.0.4129.78` で下記の実機検証を実施し、
> `A-4`/`A-1`/`A-2` の smoke と `file://` meta CSP は確認済みへ移した。** 実施環境は
> `pip install -e ".[dev]"` を `.venv`（Python 3.15）へ入れて `pytest`(82 passed,2 skipped)
> / `ruff`(緑) / `mypy`(緑) / smoke(`--strict` PASS, msedge 起動) を通した。詳細は
> [`ROADMAP.md`](ROADMAP.md) の「実機検証の残り」。
>
> **同日 macOS 側（Python 3.9）でも `pytest` / `ruff` / `mypy` を回し全緑を確認**（`verify/
> windows-real-machine-mypy` ブランチを引き継いで実施）。macOS は `pytest` が **84 passed(skip 0)** で、
> Windows で skip される読み取り専用ディレクトリのテスト 2 件
> （`test_resolve_writable_dir_*`＝D-C1 の退避ロジック。Windows は POSIX chmod が効かず self-skip）が
> **macOS では実行され通る**。両 OS でカバレッジが相補的。`mypy`/`ruff` も両 OS で緑
> （型検査ターゲット 3.10・`Any` 化は OS 非依存で、macOS でも問題ゼロを確認済み）。

- ~~**`A-4`（`closed` 化）/ `A-1`・`A-2`（フラッシュ・待ち時間）の実挙動**~~ — **済（2026-08-15）**。
  `python tests/smoke_badge.py --strict` を実 Edge で PASS。
- **`A-1`/`A-2` のタイミング根拠** — CSS アニメ時間（`.bar` 240ms / `.frame.flash` 500ms）と
  JS 定数（`CAP_FALLBACK_MS` 500 / `BAR_RETURN_MS` 170）からの**計算による導出**。smoke で
  「退避済み即解決(A-2)は 250ms 未満」「フラッシュ写り込み無し(A-1)」は実機で確認したが、
  体感の見た目そのもの（人間の目視）は未確認。修正自体は無害。
- ~~**`file://` での meta CSP 挙動**（`ROADMAP.md` の `F-A3`）~~ — **済（2026-08-15）**。実 Edge で
  meta CSP が `file://` でも機能することを確認。`response` 0・外部は `requestfailed(csp)`。
  受入判定の注意点は `ROADMAP.md` 側に記載。

> **実機検証で判明した mypy の綻び（2026-08-15 に対応済み）**: 実 Windows で `mypy .` を回すと
> 当初 2 件出た。①新しい mypy 2.3.0 が 3.9 ターゲットを廃止 → `[tool.mypy] python_version` を
> **3.10 へ引上げ**（実行時は `requires-python=">=3.9"` のまま）。②`ctypes.windll` の
> `# type: ignore[attr-defined]` は macOS スタブでは必要・Windows スタブでは不要で
> `warn_unused_ignores=true` ゆえ OS 次第で必ず片方が落ちた → **`Any` 経由の属性アクセス**に変え
> ignore 自体を撤去（`infra.py` の `_message_box_windows`）。`getattr` は ruff B009 と衝突するため
> 不採用。**Windows 実機（Python 3.15）・macOS（Python 3.9）の両 OS で `mypy`・`ruff` とも緑を確認済み
> （2026-08-15）**。`python_version=3.10` は mypy の型検査ターゲット設定であって実行系の要件ではなく、
> macOS は Python 3.9 のまま緑（ツール実行時要件も `requires-python=">=3.9"` で不変）。

---

## 3. 作業環境の注意

| 事項 | 状況 |
|------|------|
| `pytest` / `ruff` / `mypy` | `pip install -e ".[dev]"` を先に。`ruff check` / `mypy` は緑（終了コード 0）が正常 |
| `tests/smoke_badge.py` | 実 Edge/Chrome が要る。無ければ `SKIP`（終了コード 0）で抜けるので **PASS 表示を鵜呑みにしない**（CI では `--strict` で FAIL 化） |
| `infra._message_box` | Windows は `ctypes.windll`、macOS は `osascript`（開発機での確認用に分岐追加済み） |
| `build.ps1` | PowerShell / Windows 専用。macOS では実行検証できない |
| `%LOCALAPPDATA%` | `D-C1` の退避先。macOS には存在せず `tempfile.gettempdir()` へフォールバック |

---

## 4. 検証手順（4 点セット）

変更のたびに **pytest / smoke / ruff / mypy の 4 点**を全部緑にする（旧 `REFACTORING.md` §5 の「各フェーズ末で 4 点」を恒久化）。

```bash
pip install -e ".[dev]"
pytest                                 # 速い純粋関数・token 照合・DL の回帰
python tests/smoke_badge.py --strict   # 実 Edge。バー構築・SPA検知・写り込み防止・JS エラー無し
ruff check .
mypy .
```

- **smoke は実 Edge/Chrome 必須**（Windows で実行）。手元に無ければ `SKIP`（終了コード 0）で抜けるので
  **PASS 表示を鵜呑みにしない**。CI・検証では **`--strict`** を付けて FAIL 化する（付けないと
  ブラウザ不在環境で「何も検証せず緑」になる）。SKIP されたら報告では「未検証」と明記する。
- **新モジュールを足したときは** mypy を回す前に §1-8 の 2 箇所（`py-modules` / `[tool.mypy] files`）を更新する。
- リファクタでは**新規テストを足せる場所は足す**（純粋関数・判定ロジック・レジストリ等はブラウザ無しで単体化できる）。
- `ruff` は `line-length = 100`、`select = ["E","F","I","UP","B"]`。

---

## 5. コミット・報告の慣習

- **1 件ずつ 1 コミット**。まとめない。
- コミットメッセージは既存の慣習（日本語・`種別: 内容` 形式）に合わせる。
  例: `修正: ダウンロードの保存先を output 配下へ明示する（E-4）`
- **push / タグ付けは利用者に確認**してから。
- 文書（`docs/`）を直したら、その旨を報告に含める。
- **未検証の項目は必ず「未検証」と明記する。** 憶測で「動作を確認しました」と書かない。
