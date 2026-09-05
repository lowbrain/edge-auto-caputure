"""ブラウザ起動候補（browser.py）のユニットテスト。

browser_candidates が組み立てる候補（指定時は 1 つだけ／空なら Edge→Chrome／
実行パスの優先／タプルの並び）と、config 側の別名表 _BROWSER_ALIASES と
browser 側の定義表 BROWSER_BY_KEY が暗黙に一致している前提を守る。
実 Edge 不要（browser.py は Playwright を import しない純粋モジュール）。

実行:
    pip install -e ".[dev]"
    pytest
"""

import config as config_mod
from browser import AUTO_BROWSER_ORDER, BROWSER_BY_KEY, browser_candidates
from config import Config

# --------------------------------------------------------------------------- #
# browser_candidates: config.browser 指定あり（フォールバックしない）
# --------------------------------------------------------------------------- #


def test_browser_candidates_specified_edge_returns_only_edge():
    # 指定時に Chrome を足さないのは意図的な挙動（未インストールなら起動失敗＝終了）。
    # 親切心で「Edge が無ければ Chrome」を後から足さないための固定。config.ini の
    # browser 項目のコメントにも「指定したブラウザだけを起動する」と書いてある。
    assert browser_candidates(Config(browser="edge")) == [("msedge", "Edge", "")]


def test_browser_candidates_specified_chrome_returns_only_chrome():
    assert browser_candidates(Config(browser="chrome")) == [("chrome", "Chrome", "")]


# --------------------------------------------------------------------------- #
# browser_candidates: config.browser 空（自動選択）
# --------------------------------------------------------------------------- #


def test_browser_candidates_empty_follows_auto_browser_order():
    # 既定（browser="")は AUTO_BROWSER_ORDER どおり Edge→Chrome の順。
    assert browser_candidates(Config()) == [
        ("msedge", "Edge", ""),
        ("chrome", "Chrome", ""),
    ]


def test_browser_candidates_empty_follows_browser_table():
    # 並びが AUTO_BROWSER_ORDER と定義表 BROWSER_BY_KEY 由来であることまで縛る
    # （表を並べ替えたり項目を足したりしたら候補もそのまま追従する）。
    expected = [(BROWSER_BY_KEY[k][0], BROWSER_BY_KEY[k][1], "") for k in AUTO_BROWSER_ORDER]
    assert browser_candidates(Config()) == expected


# --------------------------------------------------------------------------- #
# browser_candidates: edge_path / chrome_path（自動検出より優先）
# --------------------------------------------------------------------------- #


def test_browser_candidates_uses_edge_path_when_set():
    # 空でなければ実行パスとして候補へ載る（browser_launch_kwargs 側で
    # executable_path となり、channel による自動検出より優先される）。
    c = Config(browser="edge", edge_path=r"C:\edge.exe")
    assert browser_candidates(c) == [("msedge", "Edge", r"C:\edge.exe")]


def test_browser_candidates_uses_chrome_path_when_set():
    c = Config(browser="chrome", chrome_path=r"C:\chrome.exe")
    assert browser_candidates(c) == [("chrome", "Chrome", r"C:\chrome.exe")]


def test_browser_candidates_applies_each_path_to_its_own_browser():
    # 自動選択時、edge_path / chrome_path はそれぞれ対応する候補にだけ載る（取り違え防止）。
    c = Config(edge_path=r"C:\edge.exe", chrome_path=r"C:\chrome.exe")
    assert browser_candidates(c) == [
        ("msedge", "Edge", r"C:\edge.exe"),
        ("chrome", "Chrome", r"C:\chrome.exe"),
    ]


# --------------------------------------------------------------------------- #
# 返すタプルの並び（_launch_browser がこの順で分解する）
# --------------------------------------------------------------------------- #


def test_browser_candidates_tuple_order_is_channel_label_path():
    # edge_auto_capture._launch_browser が `for channel, label, executable_path in candidates`
    # と分解しているので、並べ替えると起動オプションが静かに壊れる。
    channel, label, executable_path = browser_candidates(Config(browser="edge", edge_path="X"))[0]
    assert channel == "msedge"      # Playwright の channel 名
    assert label == "Edge"          # ログ／通知に出す表示名
    assert executable_path == "X"   # 実行ファイルのパス（空なら自動検出）


# --------------------------------------------------------------------------- #
# _BROWSER_ALIASES と BROWSER_BY_KEY の暗黙一致（#52）
# --------------------------------------------------------------------------- #


def test_browser_alias_targets_all_exist_in_browser_table():
    # config 側の別名表が正規化した先を browser 側が必ず持っていること。
    # 片方だけ足すと設定バリデーションは通り、browser_candidates の
    # BROWSER_BY_KEY[key] が生の KeyError で落ちる（notify_fatal を通らないので
    # --noconsole ビルドでは「何も起きない」無言死になる）。
    # 依存方向（browser.py → config.py）があり config 側から実行時に検証できないため、
    # この不変条件はテストで縛る。
    assert set(config_mod._BROWSER_ALIASES.values()) <= set(BROWSER_BY_KEY)


def test_auto_browser_order_keys_all_exist_in_browser_table():
    # 自動選択の優先順も同じ表を引くので、同様に縛る。
    assert set(AUTO_BROWSER_ORDER) <= set(BROWSER_BY_KEY)


def test_browser_table_path_attrs_are_config_fields():
    # path_attr は getattr(config, path_attr, "") で引かれる。Config 側の項目名を
    # 変えると既定の "" に化けて「パス指定が黙って無視される」ため、実在を確かめる。
    c = Config()
    for _, _, path_attr in BROWSER_BY_KEY.values():
        assert hasattr(c, path_attr)
