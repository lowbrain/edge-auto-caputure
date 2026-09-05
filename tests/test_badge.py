"""操作バー（badge.py / badge.js）の言語境界を守るユニットテスト。

CONTRIBUTING §1-5 が「バインディング名は 2 箇所に存在し、片方だけ変えると
**無言失敗する**」と名指ししている箇所を機械的に固定する（#67）。文章としての
警告はあったが、忘れたことを検出する仕組みが無かった。

- badge.py の BIND_* 定数群（Python → expose_binding で公開する名前）
- badge.js の BINDING_NAMES 配列（ページ側で BOUND へ退避し、E-3 のため window から消す名前）

この 2 つは 1:1 で一致していなければならない。ずれても例外もログも出ず、
操作バーのボタンが黙って効かなくなるだけなので、ここで縛る。
あわせて §1-6 の「callBinding 経由で呼ぶ」も、呼び出し名が BINDING_NAMES に
含まれることとして固定する（直接呼び出しを新たに書かせないための担保）。

実 Edge 不要（badge.py は Playwright を import しない）。

実行:
    pip install -e ".[dev]"
    pytest
"""

import re

import badge

# --------------------------------------------------------------------------- #
# badge.js の読み出し（場所の解決は badge.py 自身に任せる）
# --------------------------------------------------------------------------- #


def _badge_js_source() -> str:
    # _badge_js_path() は凍結（PyInstaller）時の _MEIPASS も見るので、パスを
    # ここで組み立て直さず badge 側の解決をそのまま使う。
    return badge._badge_js_path().read_text(encoding="utf-8")


def _js_binding_names() -> set:
    """badge.js の BINDING_NAMES 配列に並ぶ名前を取り出す。

    配列は複数行に分かれているので DOTALL で括弧の中をまとめて取り、
    その中のシングルクォート文字列を拾う（badge.js は引用符に ' を使う）。
    """
    src = _badge_js_source()
    m = re.search(r"BINDING_NAMES\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert m, "badge.js に BINDING_NAMES の配列が見つからない（定義の書き方を変えたらこの抽出も直す）"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def _py_binding_names() -> set:
    # badge.py 側は BIND_* という命名に集約されている（モジュール docstring と
    # 定数群のコメント参照）。名前で拾うので、新しい BIND_* を足せば自動で対象に入る。
    return {v for k, v in vars(badge).items() if k.startswith("BIND_")}


# --------------------------------------------------------------------------- #
# BIND_* と BINDING_NAMES の一致（#67・CONTRIBUTING §1-5）
# --------------------------------------------------------------------------- #


def test_binding_names_match_between_python_and_js():
    # 片方だけ足す/直すと無言失敗する（JS 側は try/catch で握るため例外も出ない）。
    # 集合として完全一致であることを縛る。
    assert _py_binding_names() == _js_binding_names()


def test_binding_names_are_not_empty():
    # 上の比較は「両方とも空」でも通ってしまう。抽出が壊れた（badge.js の書き方を
    # 変えた・BIND_* の命名を変えた）ときに気づけるよう、非空であることも見る。
    assert _py_binding_names()


# --------------------------------------------------------------------------- #
# callBinding の呼び出し名（CONTRIBUTING §1-6）
# --------------------------------------------------------------------------- #


def test_call_binding_targets_are_all_declared():
    # §1-6 は window.__eac_toggle(...) のような直接呼び出しを禁じ、
    # callBinding('__eac_*', TOK, ...) を使うと定めている。callBinding は BOUND から
    # 引くので、BINDING_NAMES に無い名前を呼ぶと（退避されておらず）黙って何も起きない。
    called = set(re.findall(r"callBinding\('([^']+)'", _badge_js_source()))
    assert called, "badge.js に callBinding の呼び出しが見つからない"
    assert called <= _js_binding_names()
