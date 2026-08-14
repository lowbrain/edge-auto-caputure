# 引き継ぎメモ（完了記録＋残タスク）

初版宛先: 次に作業するモデル（Opus 4.8 を想定）／担当者
初版作成日: 2026-08-11
**最終見直し: 2026-08-12（コミット `57cd5e5` 時点）**

> **重要（2026-08-12 更新）**: 初版のスコープ **S-1〜S-5 はすべて実装完了**した。
> 初版は「コードは 1 行も変わっていない。この 5 件を上から実装せよ」という**依頼書**だったが、
> その依頼はもう有効ではない。本ファイルは**完了記録＋次の担当者への残タスク**へ書き換えた。
> 旧・依頼文（「0. 最初に渡す指示」）はそのまま実行しないこと（全件対応済みのため空振りになる）。

---

## 1. これまでに完了したこと（S-1〜S-5 と周辺）

初版スコープ（`ROADMAP.md` 第 1 群＋第 2 群の一部）は全件マージ済み。

| # | ID | 内容 | 実装コミット |
|---|-----|------|--------------|
| S-1 | `D-C1` | 書き込み不可の場所で無言終了 → 退避フォールバック | `6510f3f` |
| S-2 | `A-3` | 全失敗でも `[saved]` → 実際に保存できたものを併記／`[保存できず]` | `642f9e2` |
| S-3 | `D-B1` | バージョンを `infra.__version__` に一本化し起動ログへ | `366a4a2` |
| S-4 | `A-1`+`A-2` | フラッシュ写り込み／500ms 無駄待ちを防ぐ（badge.js） | `6996a72` |
| S-5 | `A-6` | BOM 付き config.ini を読む（utf-8-sig） | `37a1fb4` |

スコープ外だが並行して対応済みのもの:

- **`A-4`** シャドウ DOM `closed` 化＋バインディング参照の退避（`e3ce47a`）
- **Python 3.9 化**（型注釈の文字列化をやめ、UP037 を根治。`c777860`）
- **判断待ち 3 件**: `D-A1`（MIT 採用）/ `F-C1`（`profile_dir` オプトインで永続化）/
  `D-D1`（署名受け口を用意・証明書取得は保留）＝`3c1f5c0`
- **Chrome 対応**（Edge 優先→Chrome フォールバック、`browser` / 実行パス設定。`761af00`）— 初版レビュー時点に無かった新機能
- **致命エラーダイアログの macOS 対応**（開発機での確認用。`cf2c14d`）
- **smoke テストの Edge→Chrome フォールバック**（`b3176a9`）と **A-2 の即解決回帰**（`d265845`）
- **配布補助**: `THIRD-PARTY-NOTICES.txt` 生成（`D-A2`）/ ZIP の SHA256 併記（`D-D3`）を `build.ps1` に実装

各文書（`IMPROVEMENTS.md` / `ROADMAP.md` / `FEATURES.md` / `DISTRIBUTION.md`）は
2026-08-12 の見直しで完了マーク（✅済 / 部分 / 未）と現行行番号へ更新済み。

---

## 2. 次の担当者への残タスク（優先度順）

全体の順位は [`ROADMAP.md`](ROADMAP.md) が正。ここでは実務上まず片付けたいものを挙げる。

| 優先 | 項目 | 出典 | 規模 | メモ |
|------|------|------|------|------|
| ★1 | **実機 smoke 検証**（`A-4`/`A-1`/`A-2` の実挙動） | — | 検証のみ | 下記「未検証事項」参照。**Windows/実 Edge で 1 回通すのが最初のタスク** |
| ★2 | `E-4` ダウンロード消失 | IMPROVEMENTS E-4 | 1 行＋検証 | `accept_downloads` / `downloads_path` 未指定。利用者影響が大きい。**実機検証が先** |
| ~~★3~~ | ~~`D-A3` プライバシー注記（＋`D-C2`/`D-B3`）~~ | DISTRIBUTION | 小 | ✅**済**。`USAGE.txt`「■ ご利用にあたって」節へ追記（免責＋保存物の説明、問い合わせ非対応も明記）。個人〜身内利用前提で軽量な文面に留めた |
| ★4 | `B-6` MutationObserver 常時稼働 | IMPROVEMENTS B-6 | badge.js 小 | SPA 検知を使わない人にも常時負荷。単独で入れられる |
| ~~5~~ | ~~依存のピン留め~~ | IMPROVEMENTS C | 極小 | ✅**済**。`pyproject.toml` を `["playwright>=1.60,<2"]` に、`build.ps1` を `pip install -e ".[build]"` に寄せて版を一元化 |
| 6 | CI（pytest+ruff）＋ smoke の `--strict` | IMPROVEMENTS D | 小 | smoke は今 Edge/Chrome 不在で SKIP=0＝CI で無意味に緑になる |
| 7 | `A-5` 使い捨てプロファイルの同時起動衝突 | IMPROVEMENTS A-5 | 小 | `keep=` で再利用分は守れたが `edge-debug-*` 同士は未対応 |
| ~~8~~ | ~~`B-1`/`B-2` URL 監視のイベント駆動化~~ | IMPROVEMENTS B | 中〜大 | ✅**済**。`framenavigated`/`context.on("page")`/`close` へ置換し `poll_interval` を撤去。ブランチ主旨の完遂 |
| 9 | `B-3`/`E-6` キュー上限・ハング保護 | IMPROVEMENTS | 中 | 長時間運用の安全弁 |

`D-B1` の exe プロパティ埋め込み（PyInstaller `--version-file`・Windows 固有）も任意で残っている。

---

## 3. 未検証事項（憶測で「確認済み」と書かないこと）

開発ホストは **macOS (darwin)**、本ツールは **Windows 専用**。以下は**実機未確認**のまま。

- **`A-4`（`closed` 化）/ `A-1`・`A-2`（フラッシュ・待ち時間）の実挙動** —
  smoke に回帰ステップは入れてあるが、開発機に Edge/Chrome が無ければ SKIP される。
  **Windows/実 Edge で smoke を 1 回通すこと**が最優先の残タスク。
- **`A-1`/`A-2` のタイミング根拠** — CSS アニメ時間（`.bar` 240ms / `.frame.flash` 500ms）と
  JS 定数（`CAP_FALLBACK_MS` 500 / `BAR_RETURN_MS` 170）からの**計算による導出**で、
  実ブラウザでの再現は未確認。修正自体は無害。
- **`E-4` ダウンロード消失** — Playwright のバージョンで挙動が異なり得る。実機確認が先。
- **`file://` での meta CSP 挙動**（`FEATURES.md` F-A3）— 実装前に手元確認が要る。

---

## 4. 作業環境の注意

| 事項 | 状況 |
|------|------|
| `pytest` / `ruff` | `pip install -e ".[dev]"` を先に。`ruff check` は緑（終了コード 0）が正常 |
| `tests/smoke_badge.py` | 実 Edge/Chrome が要る。無ければ `SKIP`（終了コード 0）で抜けるので **PASS 表示を鵜呑みにしない** |
| `infra._message_box` | Windows は `ctypes.windll`、macOS は `osascript`（`cf2c14d` で分岐追加） |
| `build.ps1` | PowerShell / Windows 専用。macOS では実行検証できない |
| `%LOCALAPPDATA%` | `D-C1` の退避先。macOS には存在せず `tempfile.gettempdir()` へフォールバック |

---

## 5. コードベース固有の落とし穴（引き続き有効）

知らないと壊す仕掛け。**ここは今も正確なので必ず読むこと。**

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
   S-1・S-2 は「握り潰しの結果が利用者に伝わらない」ことへの対処であって、
   握り潰しそのものを消す作業ではなかった。`ruff` も `B008` を意図的に ignore している。

4. **`USAGE.txt` は Shift-JIS** — 編集する場合は文字コードを維持すること
   （`README.md` / `docs/` は UTF-8）。`D-A3`/`D-B3` の追記で既にここを触っている。以後の編集も同様に注意。

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
   **指摘が出たらそれは新しく入れた問題**なので直すこと。

---

## 6. 検証手順

```bash
pip install -e ".[dev]"
pytest
ruff check .
python tests/smoke_badge.py   # Edge/Chrome があれば。SKIP されたら「未検証」と報告する
```

`ruff` は `line-length = 100`、`select = ["E","F","I","UP","B"]`。

---

## 7. コミット・報告の慣習

- **1 件ずつ 1 コミット**。まとめない。
- コミットメッセージは既存の慣習（日本語・`種別: 内容` 形式）に合わせる。
  例: `修正: ダウンロードの保存先を output 配下へ明示する（E-4）`
- **push / タグ付けは利用者に確認**してから。
- 文書（`docs/`）を直したら、その旨を報告に含める。
- **未検証の項目は必ず「未検証」と明記する。** 憶測で「動作を確認しました」と書かない。
