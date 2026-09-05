"""配布物の体裁を守るユニットテスト。

「漏れても pytest / ruff / mypy / smoke のどれも落ちないが、配布した先で壊れる」
種類の不変条件をここへ集める。開発中は pip install -e . で動いてしまうため、
気づくのが配布 exe を作った後（しかも --noconsole なら無言死）になる。

- pyproject.toml の [tool.setuptools] py-modules と、リポジトリ直下の実ファイルの一致（#68）

標準ライブラリだけで完結させる。CI は 3.9 と 3.12 の両方で pytest を回すので、
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
