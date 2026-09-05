"""配布物の体裁を守るユニットテスト。

「漏れても pytest / ruff / mypy / smoke のどれも落ちないが、配布した先で壊れる」
種類の不変条件をここへ集める。開発中は pip install -e . で動いてしまうため、
気づくのが配布 exe を作った後（しかも --noconsole なら無言死）になる。

- pyproject.toml の [tool.setuptools] py-modules と、リポジトリ直下の実ファイルの一致（#68）
- USAGE.txt が Shift-JIS として健全であること（#69）

どちらも標準ライブラリだけで完結させる。CI は 3.9 と 3.12 の両方で pytest を回すので、
tomllib（3.11+）は使えない（pyproject は正規表現で読む）。

実行:
    pip install -e ".[dev]"
    pytest
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# py-modules と実ファイルの一致（#68・CONTRIBUTING §1-10）
# --------------------------------------------------------------------------- #


def _declared_py_modules() -> set:
    """pyproject.toml の [tool.setuptools] py-modules に並ぶモジュール名。"""
    src = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^py-modules\s*=\s*\[([^\]]*)\]", src, re.MULTILINE)
    assert m, "pyproject.toml に py-modules の配列が見つからない（書き方を変えたらこの抽出も直す）"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _actual_top_level_modules() -> set:
    """リポジトリ直下に実在するトップレベル・モジュール名。

    tests/ は直下ではないので自然に外れる。build / dist / output などの生成物も
    ディレクトリなので glob("*.py") には掛からない。
    """
    return {p.stem for p in ROOT.glob("*.py")}


def test_py_modules_matches_actual_files():
    # 手で追記が要るのはここ 1 箇所だけ（§1-10）。漏れると配布が黙って割れるのに、
    # 開発中は pip install -e . で動くため 4 点セットのどれも落ちない。
    # [tool.mypy] は files = ["."] + exclude 方式なので追記不要（#40 の再発防止）。
    # 逆向き（消したモジュールが py-modules に残る）も同時に縛るため集合比較にする。
    assert _declared_py_modules() == _actual_top_level_modules()


def test_declared_py_modules_are_not_empty():
    # 抽出が壊れて両方空になると上の比較が素通りするので、非空も見る。
    assert _declared_py_modules()


# --------------------------------------------------------------------------- #
# USAGE.txt の Shift-JIS 妥当性（#69・CONTRIBUTING §1-4）
# --------------------------------------------------------------------------- #
# USAGE.txt はリポジトリ内で唯一 Shift-JIS（他は UTF-8）。UTF-8 で保存し直す・
# 絵文字が混入する、といった事故はどのチェックにも掛からず、配布先の利用者が
# 開くまで気づけない。バーのラベルには絵文字（📂 / 📸）が入っているので、
# 文言を USAGE.txt へ引用するときに持ち込みやすい。
#
# 注意: ASCII のバックスラッシュ `\` の混入はここでは検出できない。Python の
# shift_jis コーデックは 0x5C として素通しするため（iconv は同じ入力でエラーにする）。
# 詳細と使い分けは CONTRIBUTING §1-4 とスキル .claude/skills/usage-txt/ を参照。


def _usage_bytes() -> bytes:
    return (ROOT / "USAGE.txt").read_bytes()


def test_usage_txt_decodes_as_shift_jis():
    # デコードできなければ例外で落ちる（＝文字コードが変わった）。
    assert _usage_bytes().decode("shift_jis")


def test_usage_txt_roundtrips_byte_identical():
    # デコード → 再エンコードで元のバイト列に戻ること。CONTRIBUTING §1-4 が
    # 編集後に手でやれと言っている往復確認を、そのままテストにしたもの。
    raw = _usage_bytes()
    assert raw.decode("shift_jis").encode("shift_jis") == raw


def test_usage_txt_has_no_non_bmp_characters():
    # 絵文字（BMP 外）は Shift-JIS に無い。混入していれば上のデコードか往復で
    # 落ちるはずだが、原因が「絵文字」だと分かる形でも縛っておく。
    text = _usage_bytes().decode("shift_jis")
    assert [c for c in text if ord(c) > 0xFFFF] == []
