---
name: verify
description: 変更後の検証4点セット（pytest / smoke_badge --strict / ruff / mypy）を回し、「検証済み・未検証・スキップ」を分けて正直に報告する。「検証して」「4点セット回して」「テスト通る?」「動作確認して」といった依頼、およびコード変更後の報告を書くときに使う。
---

# 検証4点セット

`CONTRIBUTING.md` §3 の 4 点を回し、**何を検証できて何を検証できていないか**を分けて報告する。

このスキルの目的は「コマンドを並べること」ではなく、**緑に見えるが実は検証されていない領域を報告から漏らさないこと**。
CONTRIBUTING §4 の「未検証の項目は必ず『未検証』と明記する。憶測で『動作を確認しました』と書かない」を、
文章の心がけではなく**埋める欄**として強制する。

---

## 1. 実行

前提: `pip install -e ".[dev]"` 済み。このリポジトリでは `.venv/bin/` 配下に入っている。

```bash
pytest
python tests/smoke_badge.py --strict
ruff check .
mypy .
```

**`--strict` を必ず付ける。** 付けないと、Edge も Chrome も無い環境で smoke が `SKIP`（終了コード 0）を返し、
「何も検証していないのに緑」になる。今の開発機には Chrome があるので実際に走るが、
環境が変われば黙って SKIP に戻るため、常に付ける。

4 つは独立しているので、1 つ落ちても残りは回して全体像を掴んでから報告する。

---

## 2. smoke の FAIL は 2 種類ある — 必ず切り分ける

`--strict` 付きの smoke は、**意味が正反対の 2 つの理由**で同じ終了コード 1 を返す。
出力の文字列で判別すること。

| 出力 | 意味 | すべきこと |
|---|---|---|
| `FAIL(--strict): Edge/Chrome のいずれも起動できませんでした` | **何も検証していない** | 「未検証」として報告。**緑と言わない**。原因（ブラウザ不在）を書く |
| 上記以外（**Python の例外トレースバックを含む**） | **本当に壊れた** | 実際の不具合として直す |

**本当の失敗は整形された `FAIL:` 行とは限らない。** 実測では `badge.js` に構文エラーを入れると、
バーが構築されないまま `playwright._impl._errors.TimeoutError: Page.wait_for_selector`（`#__eac_rec_badge__` 待ち）の
**生のトレースバック**で落ちた。「`FAIL` の文字が無いからブラウザの問題だろう」と判断しないこと。
判別の基準は**ブラウザ不在メッセージが出ているかどうかの 1 点だけ**で、それ以外は全て実際の不具合として扱う。

`（起動ブラウザ: chrome）` / `（起動ブラウザ: msedge）` の行が出ていれば実際に走っている。
**どちらのブラウザで走ったかを報告に書く**（`badge.js` は Chromium 系なら検証として等価だが、
実 Edge 固有の挙動は別途 CI の Windows ジョブが見ている）。

---

## 3. 全部緑でも検証されていない領域がある

開発ホストは macOS、本ツールは Windows 専用。**4 点セットが全部緑でも、以下はローカルでは原理的に検証できない。**
今回の変更がどこに当たるかを確認し、該当するものを報告の「未検証」欄へ書く。

| 変更した場所 | 実際に守ってくれる検査 | 全部緑でも残る未検証 |
|---|---|---|
| `config.py` / `lineage.py` / `capture.py` / `browser.py` のロジック | pytest | ほぼ守られる |
| `badge.js` / `badge.py` | **smoke だけ**（pytest では守れない） | 実 Edge 固有の挙動 |
| `infra._message_box_windows` | **どれも守らない** | `ctypes.windll` 経路。macOS では `_message_box_macos`（`osascript`）側しか通らない |
| `infra.resolve_writable_dir`（`D-C1`） | pytest（macOS のみ） | Windows 実挙動。`%LOCALAPPDATA%` が無い macOS では `tempfile.gettempdir()` へ落ちる方だけを通る |
| `build.ps1` | **どれも守らない** | 全部。PowerShell / Windows 専用で macOS では実行すらできない |
| 非 HTML ページの扱い（`E-4` / `E-5`） | **どれも守らない** | PDF 内蔵ビューア・`edge://` 特権ページ・Office のダウンロード |
| 配布物（exe） | **どれも守らない** | PyInstaller onedir・SmartScreen・AV 誤検知 |

### smoke が緑でも Windows 実機検証の代わりにはならない

smoke が見るのは**操作バーの JS** であって、上表の「どれも守らない」行は対象外。
実機検証の現在地は `Issue #78` の「検証状況」が正で、smoke の結果と混同しないこと。

### OS でカバレッジが相補的

`test_resolve_writable_dir_falls_back_when_readonly` と `test_resolve_writable_dir_returns_none_when_nowhere_writable`
の 2 件は、POSIX の chmod が効かない Windows では `pytest.skip` で自己スキップし、macOS では実行される。

**片方の OS で「全部通った」は「全部検証した」ではない。** pytest の `skipped` 件数を必ず確認し、
0 件でないなら何がスキップされたかを報告する。

---

## 4. 報告フォーマット

次の 3 つの見出しで書く。**該当が無い欄も「なし」と書いて省略しない**（欄ごと消すと書き忘れと区別できない）。

```
## 検証済み
- pytest: <N> passed / <M> skipped
- smoke --strict: PASS（起動ブラウザ: <chrome|msedge>）
- ruff: 指摘なし
- mypy: Success（<N> source files）

## 未検証
- <§3 の表から、今回の変更に該当する行を転記。無ければ「なし」>
- <ローカルで確認できない理由を 1 行で>

## スキップ
- <self-skip されたテスト名と理由。0 件なら「0 件」>
```

### 書いてはいけないこと

- 4 点が緑というだけで「**動作を確認しました**」と書く。緑なのは自動検査であって、実機動作ではない
- smoke の SKIP / ブラウザ不在 FAIL を「通った」と書く
- §3 の表で「どれも守らない」に当たる変更をしたのに、未検証欄を空にする

---

## 5. スコープ外

- **自動修正はしない。** ruff / mypy の指摘を直すのは別作業（`/simplify` 等）。ここは検査と報告のみ
- **CI の再現はしない。** CI は Windows smoke と Python 3.9 / 3.12 マトリクスを回すが、ローカルで真似しない。
  ローカルで確認できないものは「CI で確認が必要」と報告に書けばよい
- **git 操作はしない。** コミット・push は別作業
