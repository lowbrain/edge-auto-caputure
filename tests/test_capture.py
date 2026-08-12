"""純粋関数（capture）・設定読み込み（config）のユニットテスト。

実 Edge を必要としない速いテスト（smoke_badge.py の補完）。
docstring/コメントに書かれた「微妙な仕様」（切り詰め・フォールバック・
既定値・空値の扱い）を回帰から守ることが狙い。

実行:
    pip install -e ".[dev]"
    pytest
"""

from pathlib import Path

import pytest

import capture
import config as config_mod
import infra
from capture import page_label, safe_name
from config import Config, load_config


@pytest.fixture(autouse=True)
def _no_dialog_no_repo_writes(monkeypatch, tmp_path):
    """テスト中に Windows のメッセージボックスを出さない・リポジトリへログを書かない。

    - notify_fatal 経由の _message_box はダイアログを出しテストを止めるので no-op に。
    - log() の書き込み先（LOG_PATH）を一時フォルダへ逃がす。
    どちらも基盤ユーティリティ（infra）にあるので infra を差し替える。
    """
    monkeypatch.setattr(infra, "_message_box", lambda *a, **k: None)
    monkeypatch.setattr(infra, "LOG_PATH", tmp_path / "log.txt")


# --------------------------------------------------------------------------- #
# safe_name
# --------------------------------------------------------------------------- #


def test_safe_name_keeps_japanese():
    # \w は Unicode 対応なので日本語はそのまま残る。
    assert safe_name("テスト") == "テスト"


def test_safe_name_replaces_symbols_and_spaces_with_dash():
    assert safe_name("a/b c") == "a-b-c"


def test_safe_name_empty_falls_back_to_page():
    assert safe_name("") == "page"


def test_safe_name_separator_only_falls_back_to_page():
    # 空白・記号のみは区切り文字（-）だけに潰れるが、前後ストリップで空になり
    # "page" へフォールバックする。
    assert safe_name("   ") == "page"
    assert safe_name("///") == "page"


def test_safe_name_keeps_interior_dashes():
    # 前後だけを落とし、内部の区切りは保持する。
    assert safe_name("-a/b-") == "a-b"


def test_safe_name_strips_leading_trailing_underscore():
    # strip("_") は前後のアンダースコアのみ落とす（内部は残す）。
    assert safe_name("_hello_") == "hello"


def test_safe_name_truncates_to_max_len():
    long = "a" * 200
    result = safe_name(long)
    assert len(result) == capture.NAME_MAX_LEN
    assert result == "a" * capture.NAME_MAX_LEN


# --------------------------------------------------------------------------- #
# page_label
# --------------------------------------------------------------------------- #


def test_page_label_prefers_title():
    assert page_label("My Page", "https://example.com") == "My-Page"


def test_page_label_falls_back_to_url_without_scheme_and_www():
    # タイトルが空なら URL から scheme と www. を落とした名前で代替。
    assert page_label("", "https://www.example.com/path") == "example-com-path"


def test_page_label_whitespace_title_uses_url():
    assert page_label("   ", "http://foo.com") == "foo-com"


# --------------------------------------------------------------------------- #
# Config 既定値
# --------------------------------------------------------------------------- #


def test_config_defaults():
    c = Config()
    assert c.start_url == "about:blank"
    assert c.output_dir == Path("output")
    assert c.poll_interval == 1.0
    assert c.start_recording is False
    assert "" in c.skip_urls  # 空URLは常にスキップ対象


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #


def _write_config(monkeypatch, tmp_path, body: str) -> Path:
    """一時 config.ini を作り、config.CONFIG_PATH をそこへ向ける。"""
    cfg = tmp_path / "config.ini"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    return cfg


def test_load_config_valid(monkeypatch, tmp_path):
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
start_url = https://example.com
output_dir = {out}
poll_interval = 2.5
settle_delay = 0.4
load_timeout = 3000
skip_urls = about:blank, https://skip.me
target_selector = .price
start_recording = true
""",
    )
    c = load_config()
    assert c.start_url == "https://example.com"
    assert c.output_dir == out
    assert c.poll_interval == 2.5
    assert c.settle_delay == 0.4
    assert c.load_timeout == 3000
    # 指定した skip_urls ＋ 常に付く空URL。
    assert c.skip_urls == ("about:blank", "https://skip.me", "")
    assert c.target_selector == ".price"
    assert c.start_recording is True


def test_load_config_missing_lines_use_defaults(monkeypatch, tmp_path):
    # 項目行そのものが無い場合は Config の既定値へフォールバックする。
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
""",
    )
    c = load_config()
    d = Config()
    assert c.poll_interval == d.poll_interval
    assert c.settle_delay == d.settle_delay
    assert c.load_timeout == d.load_timeout
    assert c.start_recording is d.start_recording
    assert c.start_url == "about:blank"


def test_load_config_relative_output_dir_resolves_under_base_dir(monkeypatch, tmp_path):
    # 相対パスは BASE_DIR 基準に固定される（exe 隣の output\ に確実に保存するため）。
    # load_config は config モジュールに import 済みの BASE_DIR を参照するのでそちらを差し替える。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    _write_config(
        monkeypatch,
        tmp_path,
        """[capture]
output_dir = mydata
""",
    )
    c = load_config()
    assert c.output_dir == tmp_path / "mydata"
    assert c.output_dir.is_absolute()


def test_load_config_empty_start_url_becomes_about_blank(monkeypatch, tmp_path):
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
start_url =
output_dir = {out}
""",
    )
    c = load_config()
    assert c.start_url == "about:blank"


def test_load_config_missing_file_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "does-not-exist.ini")
    with pytest.raises(SystemExit) as e:
        load_config()
    assert e.value.code == 1


def test_load_config_missing_section_exits(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "[wrong]\nfoo = bar\n")
    with pytest.raises(SystemExit) as e:
        load_config()
    assert e.value.code == 1


def test_load_config_invalid_number_exits(monkeypatch, tmp_path):
    # 数値項目の値だけが空/不正だと変換に失敗して終了（ValueError → sys.exit）。
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
poll_interval = not-a-number
""",
    )
    with pytest.raises(SystemExit) as e:
        load_config()
    assert e.value.code == 1


def test_load_config_empty_output_dir_falls_back_to_default(monkeypatch, tmp_path):
    # output_dir が空でもカレントへ落とさず、既定（BASE_DIR/output）へ戻す。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    _write_config(
        monkeypatch,
        tmp_path,
        """[capture]
output_dir =
""",
    )
    c = load_config()
    assert c.output_dir == tmp_path / "output"


@pytest.mark.parametrize(
    "line",
    [
        "poll_interval = 0",
        "poll_interval = -1",
        "settle_delay = -0.5",
        "load_timeout = 0",
        "load_timeout = -100",
    ],
)
def test_load_config_out_of_range_numbers_exit(monkeypatch, tmp_path, line):
    # 範囲外の数値（0/負数）は暴走・無意味値になるため終了する。
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
{line}
""",
    )
    with pytest.raises(SystemExit) as e:
        load_config()
    assert e.value.code == 1


# --------------------------------------------------------------------------- #
# profile_dir（F-C1: プロファイル永続化のオプトイン）
# --------------------------------------------------------------------------- #


def test_config_default_profile_dir_is_empty():
    # 既定は空＝毎回まっさらな使い捨てプロファイル（従来どおりの挙動）。
    assert Config().profile_dir == ""


def test_load_config_profile_dir_empty_stays_empty(monkeypatch, tmp_path):
    # 項目行はあるが値が空 → 使い捨て（空のまま）。
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
profile_dir =
""",
    )
    assert load_config().profile_dir == ""


def test_load_config_profile_dir_relative_resolves_under_base_dir(monkeypatch, tmp_path):
    # 相対パスは BASE_DIR 基準の絶対パス文字列へ固定される（output_dir と同じ扱い）。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    _write_config(
        monkeypatch,
        tmp_path,
        """[capture]
output_dir = out
profile_dir = myprofile
""",
    )
    c = load_config()
    assert c.profile_dir == str(tmp_path / "myprofile")
    assert Path(c.profile_dir).is_absolute()


def test_load_config_profile_dir_absolute_is_kept(monkeypatch, tmp_path):
    # 絶対パス指定はそのまま保持する。
    prof = tmp_path / "abs-profile"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {tmp_path / "out"}
profile_dir = {prof}
""",
    )
    assert load_config().profile_dir == str(prof)


def test_cleanup_old_profiles_removes_edge_debug_dirs(monkeypatch, tmp_path):
    # 一時フォルダ内の edge-debug-* を掃除する。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "edge-debug-a").mkdir()
    (tmp_path / "edge-debug-b").mkdir()
    infra.cleanup_old_profiles()
    assert not (tmp_path / "edge-debug-a").exists()
    assert not (tmp_path / "edge-debug-b").exists()


def test_cleanup_old_profiles_keeps_excluded_dir(monkeypatch, tmp_path):
    # keep で渡した永続プロファイルは、名前が edge-debug-* に一致しても消さない（A-5 衝突回避）。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    keep = tmp_path / "edge-debug-keep"
    other = tmp_path / "edge-debug-other"
    keep.mkdir()
    other.mkdir()
    infra.cleanup_old_profiles(keep=keep)
    assert keep.exists()
    assert not other.exists()
