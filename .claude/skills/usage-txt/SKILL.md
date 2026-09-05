---
name: usage-txt
description: USAGE.txt（Shift-JIS）を編集するときの手順。文字コードを壊さずに直し、往復変換でバイト一致を確認する。「USAGE.txt を直して」「使い方の説明を更新して」「配布物の説明を書き換えて」といった依頼で使う。
---

# USAGE.txt（Shift-JIS）の編集

`USAGE.txt` は**配布物に同梱する利用者向けの説明**で、リポジトリ内で唯一 **Shift-JIS**。
（`README.md` / `CONTRIBUTING.md` / `CLAUDE.md` は UTF-8。）

出所は `CONTRIBUTING.md` §1-4。このスキルはその手順を、**実測で裏を取った形**に具体化したもの。

---

## 1. まず知るべきこと — `0x5C` の解釈がツールで違う

Windows のパス区切りは Shift-JIS ではバイト `0x5C`。**このバイトの読み方が iconv と Python で違う。**
実測（macOS, 2026-09-05）:

| | `0x5C` を読むと | `¥`(U+00A5) を書くと | `\`(U+005C) を書くと |
|---|---|---|---|
| `iconv SHIFT_JIS` | **`¥`** (U+00A5) | `0x5C` ✓ | **エラーで停止** ✗ |
| Python `shift_jis` | **`\`** (U+005C) | `0x5C` ✓ | `0x5C` ✓ |

ここから 2 つの帰結がある。

- **iconv で取り出した UTF-8 に見える `¥` は、著者が円記号を書いたのではない。** ただの `0x5C` の描画。
  「Windows のパスなのに円記号はおかしい」と `\` へ直してはいけない（書き戻しがエラーになる）。
- **CONTRIBUTING §1-4 の「ASCII の `\` は Shift-JIS へ変換できずエラーになる」は iconv の話。**
  Python の `shift_jis` は `\` を受け付けてしまう。**どのツールで書き戻すかで正解が変わる。**

### 経路を混ぜない

実測した 4 通り:

| 取り出し → 書き戻し | 結果 |
|---|---|
| iconv → iconv | **バイト完全一致** ✓（これを使う） |
| Python → Python | バイト完全一致 ✓ |
| iconv → Python | 一致 ✓（Python が `¥` も受けるため） |
| **Python → iconv** | **失敗** ✗（`\` を変換できない） |

**iconv で統一する。** iconv は厳しい側なので、間違えると黙って壊れずエラーで止まる。

---

## 2. 手順

一時ファイルはスクラッチパッドへ置く（リポジトリを汚さない）。

```bash
# 1. 取り出す（Shift-JIS → UTF-8）
iconv -f SHIFT_JIS -t UTF-8 USAGE.txt > "$SCRATCH/usage_u8.txt"

# 2. $SCRATCH/usage_u8.txt を編集する（§3 の内容ルールを守る）

# 3. 書き戻す（UTF-8 → Shift-JIS）。ここでエラーが出たら §3 違反
iconv -f UTF-8 -t SHIFT_JIS "$SCRATCH/usage_u8.txt" > "$SCRATCH/usage_sjis.txt"

# 4. 確認してから置き換える
cp "$SCRATCH/usage_sjis.txt" USAGE.txt
```

**手順 3 でエラーが出たら、書き戻さずに §3 を見直す。** 部分的に書けたファイルで上書きしない。

---

## 3. 内容のルール

| 禁止 | 理由 | 代わりに |
|---|---|---|
| ASCII の `\` | iconv が Shift-JIS へ変換できない | `¥`（U+00A5）で書く。既存記述と同じ |
| 絵文字 | Shift-JIS に存在しない（iconv も Python も失敗） | 落として書く。例: `📂 保存先` → **「保存先」** |
| その他の非 Shift-JIS 文字 | 同上 | 環境依存文字・㈱等の機種依存文字を避ける |

`badge.py` のバーのラベルには絵文字が入っている（`📂` / `📸`）。
**`USAGE.txt` からバーのボタンを指すときは絵文字を落とす。**

---

## 4. 編集後の確認（必須）

書き換えた `USAGE.txt` が壊れていないことを、**バイトで**確かめる。

```bash
# Shift-JIS として妥当か / 往復してバイト一致するか / 禁止文字が無いか
python3 - <<'PY'
import pathlib
raw = pathlib.Path("USAGE.txt").read_bytes()
txt = raw.decode("shift_jis")                 # 妥当でなければここで例外
assert txt.encode("shift_jis") == raw, "往復でバイトが変わった"
assert not [c for c in txt if ord(c) > 0xFFFF], "BMP 外の文字（絵文字）が混入"
print(f"OK: Shift-JIS 妥当 / 往復一致 / {len(txt)} 文字")
PY
```

さらに `git diff -- USAGE.txt` で**意図した箇所だけが変わっている**ことを見る。
UTF-8 端末では中身が文字化けして見えるので、**変更行数が想定どおりか**で判断する。
全行が差分になっていたら文字コードごと変わった疑い。その場合は `git checkout -- USAGE.txt` で戻してやり直す。

---

## 5. 報告に書くこと

- Shift-JIS のまま保てたか（上の確認スクリプトの結果）
- 絵文字・`\` を落とした箇所があればその旨
- `CONTRIBUTING.md` §4 に従い、ドキュメントを直したことを報告に含める

---

## 6. スコープ外

- **`README.md` / `CONTRIBUTING.md` / `CLAUDE.md` は UTF-8。** このスキルは使わない
- USAGE.txt の**内容**をどう書くか（利用者向けの説明の質）は別の判断。ここは文字コードを壊さない手順のみ
