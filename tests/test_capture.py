"""純粋関数（capture）・設定読み込み（config）のユニットテスト。

実 Edge を必要としない速いテスト（smoke_badge.py の補完）。
docstring/コメントに書かれた「微妙な仕様」（切り詰め・フォールバック・
既定値・空値の扱い）を回帰から守ることが狙い。

実行:
    pip install -e ".[dev]"
    pytest
"""

import asyncio
import csv
import re
from pathlib import Path

import pytest

import badge
import capture
import config as config_mod
import infra
from capture import (
    CaptureRequest,
    CaptureRunner,
    page_label,
    safe_name,
    trigger_label,
)
from config import Config, load_config, should_capture

# session_stamp の実装本体への参照（autouse フィクスチャが "" へ差し替える前に押さえる）。
# 差し替え後も本物の書式を検証できるようにするため（F-C3）。
_REAL_SESSION_STAMP = config_mod.session_stamp


@pytest.fixture(autouse=True)
def _no_dialog_no_repo_writes(monkeypatch, tmp_path):
    """テスト中に Windows のメッセージボックスを出さない・リポジトリへログを書かない。

    - notify_fatal 経由の _message_box はダイアログを出しテストを止めるので no-op に。
    - log() の書き込み先（LOG_PATH）を一時フォルダへ逃がす。
    どちらも基盤ユーティリティ（infra）にあるので infra を差し替える。
    - session_stamp（F-C3 の起動時刻サブフォルダ名）を "" に固定する。実時刻由来だと
      load_config が返す output_dir が起動秒ごとに変わり、下の設定パース系テストの
      output_dir 比較が不安定になるため、既定では無効化して基準フォルダのままにする。
      セッションフォルダ挿入そのものは test_load_config_inserts_session_folder 系で検証する。
    """
    monkeypatch.setattr(infra, "_message_box", lambda *a, **k: None)
    monkeypatch.setattr(infra, "LOG_PATH", tmp_path / "log.txt")
    monkeypatch.setattr(config_mod, "session_stamp", lambda: "")


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
# _step（A-3: 全失敗でも [saved] と出さないための done 集約）
# --------------------------------------------------------------------------- #


def test_step_appends_tag_on_success():
    done: list[str] = []
    with capture._step("png", "http://x", done):
        pass
    assert done == ["png"]


def test_step_does_not_append_on_exception():
    done: list[str] = []
    with capture._step("png", "http://x", done):
        raise RuntimeError("boom")  # _step が握って [skip] ログを出す
    assert done == []  # 失敗したステップは積まれない


def test_step_without_done_still_swallows_exception():
    # done を渡さない従来の使い方（load / title）でも例外を握ることは変わらない。
    with capture._step("load", "http://x"):
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# try_eval のハング保護（E-6: 返ってこない evaluate で worker を止めない）
# --------------------------------------------------------------------------- #


class _HangingPage:
    """evaluate が永遠に返らないページ代役（ページのメインスレッド停止を模す）。"""

    def __init__(self) -> None:
        self.eval_started = False

    async def evaluate(self, js, *args):
        self.eval_started = True
        await asyncio.Event().wait()  # 誰も set しない＝永久に待つ


def test_try_eval_gives_up_after_timeout():
    # timeout を渡すと、返らない evaluate でも打ち切って戻る（例外も握る）。
    async def scenario():
        page = _HangingPage()
        # timeout が無ければここで永久にハングする。wait_for で全体を縛って検証。
        await asyncio.wait_for(
            capture.try_eval(page, "never()", timeout=0.05), timeout=1
        )
        assert page.eval_started  # 実際に evaluate へ入ったうえで打ち切ったこと

    asyncio.run(scenario())


def test_try_eval_without_timeout_would_hang():
    # timeout=None（既定）だと打ち切りが無いことの対比確認。0.1 秒待っても終わらない。
    async def scenario():
        page = _HangingPage()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(capture.try_eval(page, "never()"), timeout=0.1)

    asyncio.run(scenario())


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
    assert c.eval_timeout == 5000
    assert c.start_recording is False
    assert "" in c.skip_urls  # 空URLは常にスキップ対象
    assert c.allow_urls == ()  # 既定は無効（撮る URL を絞らない。F-C2）


# --------------------------------------------------------------------------- #
# should_capture（skip_urls / allow_urls / 前方一致・fnmatch。R3/F-C2/B-5）
# --------------------------------------------------------------------------- #


def test_should_capture_default_skips_blank_and_empty():
    # 既定（skip_urls=("about:blank","")）。about:blank と空URLは撮らない。
    c = Config()
    assert should_capture("https://example.com/", c) is True
    assert should_capture("about:blank", c) is False
    assert should_capture("", c) is False


def test_should_capture_empty_pattern_only_matches_empty_url():
    # skip_urls の "" 番兵は「空URL専用」。前方一致で全URLに化けてはいけない。
    c = Config(skip_urls=("",))
    assert should_capture("", c) is False
    assert should_capture("https://example.com/", c) is True


def test_should_capture_skip_is_prefix_match_so_query_still_skipped():
    # 完全一致だとクエリ付きで漏れていた（B-5）。前方一致でクエリ付きも弾く。
    c = Config(skip_urls=("https://skip.me", ""))
    assert should_capture("https://skip.me", c) is False
    assert should_capture("https://skip.me?ref=1", c) is False
    assert should_capture("https://skip.me/logout", c) is False
    assert should_capture("https://keep.me/", c) is True


def test_should_capture_skip_supports_wildcards():
    # * ? [ を含むパターンは fnmatch 扱い（前方一致では書けない末尾一致など）。
    c = Config(skip_urls=("*://*/logout", ""))
    assert should_capture("https://a.example.com/logout", c) is False
    assert should_capture("http://b.test/logout", c) is False
    assert should_capture("https://a.example.com/home", c) is True


def test_should_capture_allow_urls_whitelist_skips_others():
    # allow_urls 指定時は、合致しない URL をすべてスキップ（F-C2）。
    c = Config(allow_urls=("https://example.com/",), skip_urls=("",))
    assert should_capture("https://example.com/page", c) is True
    assert should_capture("https://other.com/", c) is False


def test_should_capture_allow_urls_supports_wildcards():
    c = Config(allow_urls=("https://*.example.com/*",), skip_urls=("",))
    assert should_capture("https://docs.example.com/a", c) is True
    assert should_capture("https://example.org/a", c) is False


def test_should_capture_skip_urls_still_apply_within_allow():
    # allow を通っても skip に当たれば撮らない（ブラックリストが優先）。
    c = Config(
        allow_urls=("https://example.com",),
        skip_urls=("https://example.com/logout", ""),
    )
    assert should_capture("https://example.com/page", c) is True
    assert should_capture("https://example.com/logout", c) is False


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
settle_delay = 0.4
load_timeout = 3000
eval_timeout = 4000
skip_urls = about:blank, https://skip.me
allow_urls = https://example.com/, https://*.example.com/*
target_selector = .price
hide_selectors = #cookie-banner, .sticky-header
start_recording = true
""",
    )
    c = load_config()
    assert c.start_url == "https://example.com"
    assert c.output_dir == out
    assert c.settle_delay == 0.4
    assert c.load_timeout == 3000
    assert c.eval_timeout == 4000
    # 指定した skip_urls ＋ 常に付く空URL。
    assert c.skip_urls == ("about:blank", "https://skip.me", "")
    # allow_urls は指定値のみ（空URL番兵は付けない。F-C2）。
    assert c.allow_urls == ("https://example.com/", "https://*.example.com/*")
    assert c.target_selector == ".price"
    # カンマ区切りをタプル化（空要素は落とす。F-B2）。
    assert c.hide_selectors == ("#cookie-banner", ".sticky-header")
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
    assert c.settle_delay == d.settle_delay
    assert c.load_timeout == d.load_timeout
    assert c.eval_timeout == d.eval_timeout
    assert c.hide_selectors == d.hide_selectors == ()
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


def test_load_config_reads_bom_prefixed_file(monkeypatch, tmp_path):
    # メモ帳保存等で BOM が付いても読めること（A-6）。utf-8 のままだと最初の見出しが
    # 壊れて MissingSectionHeaderError → 起動不能になっていた。
    out = tmp_path / "out"
    cfg = tmp_path / "config.ini"
    # utf-8-sig で書くと先頭に BOM が付く。
    cfg.write_text(
        f"[capture]\noutput_dir = {out}\nstart_url = https://example.com\n",
        encoding="utf-8-sig",
    )
    assert cfg.read_bytes().startswith(b"\xef\xbb\xbf")  # BOM が付いていることを確認
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    c = load_config()
    assert c.start_url == "https://example.com"
    assert c.output_dir == out


def test_load_config_missing_file_self_heals(monkeypatch, tmp_path):
    # D-C3: config.ini が無ければ既定値で作り直して起動する（起動不能にしない）。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    cfg = tmp_path / "does-not-exist.ini"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    assert not cfg.exists()
    c = load_config()
    # 既定ファイルが作られ、配布テンプレートと同一の内容になる。
    assert cfg.exists()
    assert cfg.read_text(encoding="utf-8") == config_mod.DEFAULT_CONFIG_TEXT
    # 既定テンプレートの値で起動する（output は BASE_DIR 配下へ解決）。
    assert c.start_url == "https://www.google.com"
    assert c.output_dir == tmp_path / "output"


def test_load_config_missing_file_uses_defaults_when_unwritable(monkeypatch, tmp_path):
    # 書き出しに失敗しても（読み取り専用等）、メモリ上の既定値で起動する。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "does-not-exist.ini")
    monkeypatch.setattr(config_mod, "_write_default_config", lambda: False)
    c = load_config()
    assert c.start_url == "https://www.google.com"
    assert c.skip_urls == ("about:blank", "")
    assert c.output_dir == tmp_path / "output"


def test_load_config_broken_file_self_heals(monkeypatch, tmp_path):
    # D-C3: [capture] が無い/破損した config.ini は .invalid へ退避し、既定で作り直す。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    cfg = _write_config(monkeypatch, tmp_path, "[wrong]\nfoo = bar\n")
    c = load_config()
    # 壊れた元ファイルは消さず退避される（利用者が中身を確認できる）。
    invalid = tmp_path / "config.ini.invalid"
    assert invalid.exists()
    assert invalid.read_text(encoding="utf-8").startswith("[wrong]")
    # 既定 config.ini を作り直し、既定値で起動する。
    assert cfg.read_text(encoding="utf-8") == config_mod.DEFAULT_CONFIG_TEXT
    assert c.start_url == "https://www.google.com"
    assert c.output_dir == tmp_path / "output"


def test_load_config_corrupt_file_self_heals(monkeypatch, tmp_path):
    # パースできないゴミ（セクション見出しの前に本文）でも退避＆作り直しで起動する。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    cfg = _write_config(monkeypatch, tmp_path, "not a config at all\n= = =\n")
    c = load_config()
    assert (tmp_path / "config.ini.invalid").exists()
    assert cfg.read_text(encoding="utf-8") == config_mod.DEFAULT_CONFIG_TEXT
    assert c.output_dir == tmp_path / "output"


def test_default_config_text_matches_bundled_ini():
    # 自己修復で書き出す既定テキストは、配布する config.ini と同一であること（drift 防止）。
    # read_text は改行を \n へ正規化するので、CRLF の config.ini とも一致する。
    root = Path(__file__).resolve().parent.parent
    bundled = (root / "config.ini").read_text(encoding="utf-8")
    assert bundled == config_mod.DEFAULT_CONFIG_TEXT


def test_config_with_defaults_uses_template_values(monkeypatch, tmp_path):
    # DEFAULT_CONFIG_TEXT から作る既定 Config は配布テンプレートの値になる。
    monkeypatch.setattr(config_mod, "BASE_DIR", tmp_path)
    c = config_mod._config_with_defaults(Config())
    assert c.start_url == "https://www.google.com"
    assert c.skip_urls == ("about:blank", "")
    assert c.output_dir == tmp_path / "output"


def test_load_config_invalid_number_exits(monkeypatch, tmp_path):
    # 数値項目の値だけが空/不正だと変換に失敗して終了（ValueError → sys.exit）。
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
settle_delay = not-a-number
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
        "settle_delay = -0.5",
        "load_timeout = 0",
        "load_timeout = -100",
        "eval_timeout = 0",
        "eval_timeout = -100",
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


# --------------------------------------------------------------------------- #
# セッションフォルダ（F-C3: 起動単位で output_dir の下に 1 段挟む）
# --------------------------------------------------------------------------- #


def test_session_stamp_format():
    # 起動時刻を「YYYY-MM-DD_HHMMSS」で表す（フォルダ名に使うので区切りは - と _ のみ）。
    # autouse フィクスチャが session_stamp を "" に固定するため、実装本体（_REAL_SESSION_STAMP）を呼ぶ。
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}$", _REAL_SESSION_STAMP())


def test_load_config_inserts_session_folder(monkeypatch, tmp_path):
    # F-C3: 確定した output_dir の直下へ、起動時刻のセッションフォルダを 1 段挟む。
    # 実時刻由来だとテストが不安定なので session_stamp を固定値へ差し替える
    #（autouse フィクスチャは "" にしているが、ここでは実挿入を検証するため上書きする）。
    monkeypatch.setattr(config_mod, "session_stamp", lambda: "2026-08-11_143025")
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
""",
    )
    c = load_config()
    assert c.output_dir == out / "2026-08-11_143025"


def test_load_config_session_folder_redirects_log(monkeypatch, tmp_path):
    # log.txt もセッションフォルダへ寄る（set_log_dir が output_dir 直下ではなく
    # セッションフォルダを指す）。受け渡しが「このフォルダを渡す」で閉じる肝。
    monkeypatch.setattr(config_mod, "session_stamp", lambda: "2026-08-11_143025")
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
""",
    )
    load_config()
    assert infra.LOG_PATH == out / "2026-08-11_143025" / "log.txt"


def test_session_folder_keeps_lineage_and_downloads_relative(monkeypatch, tmp_path):
    # lineage-<id> / downloads / index.csv は output_dir からの相対で決まるので、
    # output_dir がセッションフォルダになれば自動でその配下へ入る（相対関係は不変）。
    monkeypatch.setattr(config_mod, "session_stamp", lambda: "2026-08-11_143025")
    out = tmp_path / "out"
    _write_config(
        monkeypatch,
        tmp_path,
        f"""[capture]
output_dir = {out}
""",
    )
    c = load_config()
    session = out / "2026-08-11_143025"
    # 撮影物の系譜サブフォルダ（capture.group_subdir）はセッションフォルダ配下。
    assert capture.group_subdir(c.output_dir, "20260811143025000") == (
        session / "lineage-20260811143025000"
    )
    # ダウンロード退避先（edge_auto_capture._downloads_dir）もセッションフォルダ配下。
    from edge_auto_capture import _downloads_dir

    assert _downloads_dir(c, "20260811143025000") == (
        session / "lineage-20260811143025000" / "downloads"
    )
    # 索引 CSV はセッションフォルダ直下（全系譜を 1 本にまとめる粒度は据え置き）。
    assert c.output_dir / capture.INDEX_CSV_NAME == session / "index.csv"


# --------------------------------------------------------------------------- #
# バージョン（D-B1: 出所を infra.__version__ に一本化する）
# --------------------------------------------------------------------------- #


def test_infra_version_is_semverish():
    # exe / ログに出す唯一の出所。空や壊れた値を弾く。
    assert re.match(r"^\d+\.\d+\.\d+$", infra.__version__)


def test_pyproject_sources_version_from_infra():
    # pyproject はバージョンを直書きせず infra.__version__ を参照すること。
    # 直書きに戻すと二重管理になり「片方だけ上げる」事故が起きる（D-B1）。
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'dynamic\s*=\s*\[\s*"version"\s*\]', text)
    assert re.search(r'attr\s*=\s*"infra\.__version__"', text)
    # バージョンの直書き行（version = "x.y.z"）が残っていないこと。
    assert not re.search(r'^\s*version\s*=\s*"', text, re.MULTILINE)


# --------------------------------------------------------------------------- #
# 環境情報の起動ログ（D-B2: 切り分けのため OS/採用設定値を1行ずつ残す）
# --------------------------------------------------------------------------- #


def test_startup_environment_line_has_os_and_python():
    # OS・Python・実行形態が1行に入る（通常の Python 実行では 実行=script）。
    line = infra.startup_environment_line()
    assert line.startswith("[env] ")
    assert "OS=" in line
    assert "Python=" in line
    assert "実行=script" in line


def test_summarize_config_reports_key_values():
    from config import summarize_config

    c = Config(
        browser="edge",
        output_dir=Path("/tmp/out"),
        target_selector=".price",
        hide_selectors=("#cookie-banner", ".sticky-header"),
        allow_urls=("https://example.com/",),
    )
    line = summarize_config(c)
    assert line.startswith("[config] ")
    assert "browser=edge" in line
    assert "output_dir=/tmp/out" in line or "output_dir=\\tmp\\out" in line
    assert "target_selector=.price" in line
    assert "hide_selectors=#cookie-banner,.sticky-header" in line
    assert "allow_urls=https://example.com/" in line


def test_summarize_config_marks_empty_values_readably():
    from config import summarize_config

    line = summarize_config(Config())  # 既定（自動選択・使い捨て・セレクタ無し）
    assert "browser=自動(Edge→Chrome)" in line
    assert "edge_path=自動" in line
    assert "profile_dir=使い捨て" in line
    assert "target_selector=(無)" in line
    assert "hide_selectors=(無)" in line
    assert "allow_urls=(無)" in line


# --------------------------------------------------------------------------- #
# resolve_writable_dir（D-C1: 書き込み不可の場所で無言終了しない）
# --------------------------------------------------------------------------- #


def test_resolve_writable_dir_returns_preferred_when_writable(tmp_path):
    # 書き込める場所ならそのまま返し、フォルダを作る。
    preferred = tmp_path / "output"
    resolved = infra.resolve_writable_dir(preferred)
    assert resolved == preferred
    assert preferred.is_dir()


def test_resolve_writable_dir_falls_back_when_readonly(monkeypatch, tmp_path):
    # preferred が書き込み不可なら、LOCALAPPDATA 配下の退避先を返す。
    readonly = tmp_path / "ro"
    readonly.mkdir()
    import os as _os

    _os.chmod(readonly, 0o500)  # r-x（書き込み不可）
    if _os.access(readonly, _os.W_OK):
        pytest.skip("この環境では読み取り専用ディレクトリを再現できない（Windows 等）")

    fallback_base = tmp_path / "appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(fallback_base))

    preferred = readonly / "output"
    resolved = infra.resolve_writable_dir(preferred)
    try:
        assert resolved is not None
        assert resolved != preferred
        assert resolved == fallback_base / "edge-auto-capture" / "output"
        assert resolved.is_dir()
    finally:
        _os.chmod(readonly, 0o700)  # tmp_path 掃除のため書き込みを戻す


def test_resolve_writable_dir_returns_none_when_nowhere_writable(monkeypatch, tmp_path):
    # preferred も退避先も書けなければ None（呼び出し側が notify_fatal する）。
    readonly = tmp_path / "ro"
    readonly.mkdir()
    import os as _os

    _os.chmod(readonly, 0o500)
    if _os.access(readonly, _os.W_OK):
        pytest.skip("この環境では読み取り専用ディレクトリを再現できない（Windows 等）")

    monkeypatch.setenv("LOCALAPPDATA", str(readonly / "appdata"))
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(readonly / "tmp"))

    try:
        assert infra.resolve_writable_dir(readonly / "output") is None
    finally:
        _os.chmod(readonly, 0o700)


def test_cleanup_old_profiles_removes_edge_debug_dirs(monkeypatch, tmp_path):
    # 一時フォルダ内の（十分古い）edge-debug-* を掃除する。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    import os as _os

    a = tmp_path / "edge-debug-a"
    b = tmp_path / "edge-debug-b"
    a.mkdir()
    b.mkdir()
    old = infra.datetime.now().timestamp() - infra._PROFILE_STALE_AGE_SECONDS - 60
    _os.utime(a, (old, old))
    _os.utime(b, (old, old))
    infra.cleanup_old_profiles()
    assert not a.exists()
    assert not b.exists()


def test_cleanup_old_profiles_keeps_excluded_dir(monkeypatch, tmp_path):
    # keep で渡した永続プロファイルは、名前が edge-debug-* に一致しても消さない（A-5 衝突回避）。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    keep = tmp_path / "edge-debug-keep"
    other = tmp_path / "edge-debug-other"
    keep.mkdir()
    other.mkdir()
    # 既定の mtime 保護に引っかからないよう、両方とも十分古い扱いにする。
    old = infra.datetime.now().timestamp() - infra._PROFILE_STALE_AGE_SECONDS - 60
    import os as _os

    _os.utime(keep, (old, old))
    _os.utime(other, (old, old))
    infra.cleanup_old_profiles(keep=keep)
    assert keep.exists()
    assert not other.exists()


def test_cleanup_old_profiles_removes_only_old_dirs(monkeypatch, tmp_path):
    # A-5: 新しい（別インスタンス使用中かもしれない）プロファイルは残し、
    # 十分古いものだけを掃除する。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    import os as _os

    fresh = tmp_path / "edge-debug-fresh"
    stale = tmp_path / "edge-debug-stale"
    fresh.mkdir()
    stale.mkdir()
    now = infra.datetime.now().timestamp()
    _os.utime(fresh, (now, now))  # たった今更新（＝使用中かも）
    old = now - infra._PROFILE_STALE_AGE_SECONDS - 60
    _os.utime(stale, (old, old))
    infra.cleanup_old_profiles()
    assert fresh.exists()
    assert not stale.exists()


def test_cleanup_old_profiles_removes_fresh_when_age_zero(monkeypatch, tmp_path):
    # min_age_seconds=0 なら mtime 保護は無効になり、従来どおり全削除される。
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "edge-debug-a").mkdir()
    infra.cleanup_old_profiles(min_age_seconds=0)
    assert not (tmp_path / "edge-debug-a").exists()


# --------------------------------------------------------------------------- #
# 多重起動抑止（D-C4）: アプリ全体で 1 プロセスだけ起動を許すファイルロック。
# 実プロセスは起こさず、ロックファイルの排他制御そのものをユニットで検証する。
# --------------------------------------------------------------------------- #


def _reset_single_instance(monkeypatch, tmp_path):
    """ロック置き場を tmp へ向け、モジュール保持ハンドルを初期化する。"""
    monkeypatch.setattr(infra.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(infra, "_single_instance_handle", None, raising=False)


def test_acquire_single_instance_lock_first_call_succeeds(monkeypatch, tmp_path):
    _reset_single_instance(monkeypatch, tmp_path)
    try:
        assert infra.acquire_single_instance_lock() is True
        assert infra._single_instance_handle is not None
    finally:
        if infra._single_instance_handle is not None:
            infra._single_instance_handle.close()


def test_acquire_single_instance_lock_second_process_blocked(monkeypatch, tmp_path):
    # 同一プロセスは同じハンドルを共有してしまうため、2 つ目の「別プロセス」は
    # 生のファイルディスクリプタで直接ロックを試み、弾かれることを確かめる。
    _reset_single_instance(monkeypatch, tmp_path)
    assert infra.acquire_single_instance_lock() is True
    rival = open(infra.single_instance_lock_path(), "w")
    try:
        assert infra._try_lock(rival) is False
    finally:
        rival.close()
        if infra._single_instance_handle is not None:
            infra._single_instance_handle.close()


def test_acquire_single_instance_lock_idempotent(monkeypatch, tmp_path):
    # 同一プロセス内で二度呼んでも True（既に自分が保持している）。
    _reset_single_instance(monkeypatch, tmp_path)
    try:
        assert infra.acquire_single_instance_lock() is True
        assert infra.acquire_single_instance_lock() is True
    finally:
        if infra._single_instance_handle is not None:
            infra._single_instance_handle.close()


# --------------------------------------------------------------------------- #
# CaptureRunner の合流（B-3: 撮影キュー無制限の防止）
#
# spawn は「ページごとに実行中1件＋保留1件（最新で置き換え）」に合流させる。
# 実 Edge を使わず、_capture をスタブ化して「実際に何件・どの params で走ったか」だけを
# 見ることで、合流ロジックそのものを速いユニットテストで回帰から守る。
# --------------------------------------------------------------------------- #


class _FakePage:
    """WeakKeyDictionary のキーになれる最小のページ代役（weakref 可能な実体）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<FakePage {self.name}>"


def _recording_runner(gate: "asyncio.Event | None" = None):
    """_capture を「呼び出し記録用スタブ」に差し替えた CaptureRunner を返す。

    gate を渡すと各撮影は gate がセットされるまで待つ（撮影を in-flight に保ち、
    その間に来た spawn が合流することを確かめるため）。戻り値の calls に
    (page, url, selector) が実行順で積まれる。
    """
    runner = CaptureRunner()
    calls: list[tuple] = []

    async def stub(req):
        calls.append((req.page, req.url, req.selector))
        if gate is not None:
            await gate.wait()

    runner._capture = stub  # インスタンス属性がクラスメソッドを上書きする
    return runner, calls


def test_spawn_coalesces_synchronous_burst():
    # worker が動き出す前に連続 spawn した分は、最新1件へ合流する。
    async def scenario():
        runner, calls = _recording_runner()
        page = _FakePage("p")
        cfg = Config()
        for i in range(5):
            runner.spawn(CaptureRequest(page, f"url-{i}", cfg))
        assert len(runner._tasks) == 1  # ページごとに worker は1つだけ
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert calls == [(page, "url-4", "")]  # 走るのは最後の1件だけ

    asyncio.run(scenario())


def test_spawn_coalesces_requests_during_capture():
    # 撮影中(in-flight)に来た複数要求は、最新1件だけに合流して次に走る。
    async def scenario():
        gate = asyncio.Event()
        runner, calls = _recording_runner(gate)
        page = _FakePage("p")
        cfg = Config()

        runner.spawn(CaptureRequest(page, "url-1", cfg))
        worker = runner._workers[page]
        for _ in range(5):  # 第1撮影を gate 待ちまで進める
            await asyncio.sleep(0)
        assert calls == [(page, "url-1", "")]  # 1件目が in-flight

        # 撮影中に3回要求 → _pending は最新(url-4, sel-4)で上書きされる
        runner.spawn(CaptureRequest(page, "url-2", cfg, "sel-2"))
        runner.spawn(CaptureRequest(page, "url-3", cfg, "sel-3"))
        runner.spawn(CaptureRequest(page, "url-4", cfg, "sel-4"))

        gate.set()  # 1件目を解放。worker がループして最新1件だけを撮る
        await asyncio.wait_for(worker, timeout=1)

        # 4回積んでも走ったのは2件（in-flight の1 + 合流後の最新1）。selector も最新。
        assert calls == [(page, "url-1", ""), (page, "url-4", "sel-4")]
        assert page not in runner._workers  # 空になったら worker は退場
        assert page not in runner._pending

    asyncio.run(scenario())


def test_spawn_different_pages_run_independently():
    # 別ページは別 worker。互いに合流せず、それぞれ撮られる。
    async def scenario():
        runner, calls = _recording_runner()
        p1, p2 = _FakePage("1"), _FakePage("2")
        cfg = Config()
        runner.spawn(CaptureRequest(p1, "a", cfg))
        runner.spawn(CaptureRequest(p2, "b", cfg))
        assert len(runner._tasks) == 2  # ページごとに worker
        await asyncio.wait_for(asyncio.gather(*list(runner._tasks)), timeout=1)
        assert sorted(c[1] for c in calls) == ["a", "b"]

    asyncio.run(scenario())


def test_spawn_restarts_worker_after_drain():
    # 保留を撃ち尽くして worker が退場した後の spawn は、新しい worker を立てて走る
    #（終了と再要求の競合で取りこぼさないことの確認）。
    async def scenario():
        runner, calls = _recording_runner()
        page = _FakePage("p")
        cfg = Config()
        runner.spawn(CaptureRequest(page, "first", cfg))
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert page not in runner._workers  # drain 後に退場

        runner.spawn(CaptureRequest(page, "second", cfg))  # 再度 spawn → 新 worker
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert [c[1] for c in calls] == ["first", "second"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# CaptureRequest（撮影 1 回分の要求オブジェクト）
#
# spawn→_pending→_worker→_capture を貫通する位置引数タプルを 1 オブジェクトへ集約した器。
# 既定値と、要求が _capture まで欠けずに届くこと（R1 の配線）を回帰から守る。
# --------------------------------------------------------------------------- #


def test_capture_request_defaults():
    # selector / group_id / trigger は省略時に空文字（未指定）になる。
    page = _FakePage("p")
    cfg = Config()
    req = CaptureRequest(page, "https://example.test/", cfg)
    assert req.page is page
    assert req.url == "https://example.test/"
    assert req.config is cfg
    assert req.selector == ""
    assert req.group_id == ""
    assert req.trigger == ""


def test_capture_request_holds_all_fields():
    # 全フィールドを与えると、その値がそのまま保持される。
    page = _FakePage("p")
    cfg = Config()
    req = CaptureRequest(
        page, "https://example.test/x", cfg, "#main", "20260814101105674", "spa"
    )
    assert (req.selector, req.group_id, req.trigger) == (
        "#main", "20260814101105674", "spa"
    )


def test_spawn_delivers_request_to_capture_unchanged():
    # spawn した CaptureRequest が、page/url/selector/group_id を欠かさず _capture へ届く。
    async def scenario():
        runner = CaptureRunner()
        received: list[CaptureRequest] = []

        async def stub(req):
            received.append(req)

        runner._capture = stub
        page = _FakePage("p")
        cfg = Config()
        req = CaptureRequest(page, "https://example.test/y", cfg, "#part", "20260814101105674")
        runner.spawn(req)
        await asyncio.wait_for(runner._workers[page], timeout=1)

        assert len(received) == 1
        got = received[0]
        assert got is req  # 同じオブジェクトがそのまま渡る
        assert (got.page, got.url, got.selector, got.group_id) == (
            page, "https://example.test/y", "#part", "20260814101105674"
        )

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# タブ系譜グループ（CaptureSession）
#
# 記録ON/OFF・SPA検知・セレクタは「opener 連鎖で決まるグループ」ごとに独立して持つ。
# 実 Edge を使わず、opener() を返すフェイクページと spawn 記録用スタブで、グループの
# 合流・独立性・SPA ゲートを速いユニットテストで回帰から守る。
# --------------------------------------------------------------------------- #


class _GroupPage:
    """opener() を持つ最小のページ代役（グループ解決テスト用）。"""

    def __init__(self, name: str, url: str = "https://example.test/", opener=None) -> None:
        self.name = name
        self.url = url
        self._opener = opener

    async def opener(self):
        return self._opener

    def __repr__(self) -> str:
        return f"<GroupPage {self.name}>"


class _FakeContext:
    def __init__(self, pages) -> None:
        self.pages = list(pages)


class _RecRunner:
    """runner.spawn(CaptureRequest) を記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.group_ids: list[int] = []
        self.triggers: list[str] = []

    def spawn(self, req):
        self.calls.append((req.page, req.url, req.selector))
        self.group_ids.append(req.group_id)
        self.triggers.append(req.trigger)


def _make_session(pages, roots=None, config=None):
    """フェイク context を持つ CaptureSession を作る。

    roots: {page: GroupState} を渡すと、setup() の種入れ相当（page_root/groups の登録）を
    済ませた状態にする。runner は記録用スタブへ、refresh_panels は no-op へ差し替える。
    """
    from edge_auto_capture import CaptureSession

    session = CaptureSession(_FakeContext(pages), config or Config())
    session.runner = _RecRunner()

    async def _noop():
        return None

    session.refresh_panels = _noop  # 実 evaluate を避ける（グループ判定だけ検証する）
    for pg, state in (roots or {}).items():
        session.page_root[pg] = pg
        session.groups[pg] = state
    return session


class _SetupPage(_GroupPage):
    """setup() の監視配線（page.on）を受け取るページ代役。配線されたイベント名を記録する。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.events: list[str] = []

    def on(self, event, handler) -> None:
        self.events.append(event)


class _SetupContext(_FakeContext):
    """setup() が呼ぶ context 側 API を受け取る代役（実 Edge 無しで setup() を通す）。"""

    def __init__(self, pages) -> None:
        super().__init__(pages)
        self.bindings: list[str] = []
        self.init_scripts: list[str] = []
        self.events: list[str] = []

    async def expose_binding(self, name, callback) -> None:
        self.bindings.append(name)

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)

    def on(self, event, handler) -> None:
        self.events.append(event)


@pytest.mark.parametrize("recording", [True, False])
def test_setup_seeds_startup_pages_as_root_groups(recording):
    """起動時に開いているページは、start_recording に従う root グループとして種入れされる。

    main() はこの種入れに全面的に依存する（自前で page_root/groups を触らない）ため、
    「setup() を通せば root 採番・start_recording 反映・監視配線が揃う」ことを固定する。
    """
    from edge_auto_capture import CaptureSession

    async def scenario():
        page = _SetupPage("startup")
        ctx = _SetupContext([page])
        session = CaptureSession(ctx, Config(start_recording=recording, target_selector="#main"))
        await session.setup()

        assert session.page_root[page] is page          # 自分が root
        grp = session.groups[page]
        assert (grp.on, grp.spa_on, grp.selector) == (recording, False, "#main")
        assert page.events == ["framenavigated", "close"]   # URL変化・消滅の監視も配線される
        assert ctx.events == ["download", "page"]           # DL 退避と新規タブ検知も配線される

    asyncio.run(scenario())


def test_setup_without_pages_seeds_nothing():
    """ページが 1 枚も無ければ種入れは起きない（main() が setup() 前に 1 枚用意する前提）。

    以前は main() が setup() の後に new_page() し、種入れを自前で書き足していた。その順序だと
    setup() が張った context.on("page") 経由の on_new_page が先に走りえて、start_recording では
    なく初期OFFの独立グループとして採番されうる。現在は main() が setup() の前に 1 枚用意し、
    種入れは setup() だけが行う。
    """
    from edge_auto_capture import CaptureSession

    async def scenario():
        ctx = _SetupContext([])
        session = CaptureSession(ctx, Config(start_recording=True))
        await session.setup()

        assert session.groups == {}
        assert session.page_root == {}

    asyncio.run(scenario())


def test_manual_tab_becomes_independent_off_group():
    # opener=None の手動タブは、それ自身が root の新グループ・初期OFFになる。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        manual = _GroupPage("manual", opener=None)
        session = _make_session(
            [root, manual], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        grp = await session._resolve_group(manual)
        assert grp.on is False  # 勝手に撮らない
        assert session.page_root[manual] is manual  # 自分が root
        assert grp is not session.groups[root]  # root グループとは別物

    asyncio.run(scenario())


def test_popup_joins_parent_group():
    # opener=root のポップアップは root と同じグループに合流し、状態を共有する。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        session = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=True, selector="#x")}
        )
        grp = await session._resolve_group(popup)
        assert grp is session.groups[root]  # 同一グループ（状態共有）
        assert session.page_root[popup] is root

    asyncio.run(scenario())


def test_grandchild_popup_resolves_to_root_group():
    # ポップアップのポップアップ（孫）も root グループへ合流する（推移性）。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        child = _GroupPage("child", opener=root)
        grand = _GroupPage("grand", opener=child)
        session = _make_session(
            [root, child, grand], roots={root: GroupState(on=False, spa_on=False, selector="")}
        )
        grp = await session._resolve_group(grand)
        assert grp is session.groups[root]
        assert session.page_root[grand] is root

    asyncio.run(scenario())


def test_toggle_one_group_does_not_affect_another():
    # あるグループの記録ON/OFFは、無関係な別グループに波及しない。
    from edge_auto_capture import GroupState

    async def scenario():
        r1 = _GroupPage("r1")
        r2 = _GroupPage("r2")
        session = _make_session(
            [r1, r2],
            roots={
                r1: GroupState(on=False, spa_on=False, selector=""),
                r2: GroupState(on=False, spa_on=False, selector=""),
            },
        )
        await session.on_toggle({"page": r1}, token=session.token)
        assert session.groups[r1].on is True   # 押した側だけON
        assert session.groups[r2].on is False  # 別グループは不変
        # ONにした瞬間、r1 グループの現存ページ（r1）が即撮りされる
        assert [c[0] for c in session.runner.calls] == [r1]

    asyncio.run(scenario())


def test_spa_changed_gated_by_group_state():
    # on_spa_changed はそのページのグループが on かつ spa_on のときだけ撮る。
    from edge_auto_capture import GroupState

    async def scenario():
        # on=True, spa_on=True → 撮る
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=True, selector="#s")})
        await s.on_spa_changed({"page": r}, token=s.token)
        assert s.runner.calls == [(r, r.url, "#s")]

        # spa_on=False → 撮らない
        r2 = _GroupPage("r2")
        s2 = _make_session([r2], roots={r2: GroupState(on=True, spa_on=False, selector="")})
        await s2.on_spa_changed({"page": r2}, token=s2.token)
        assert s2.runner.calls == []

        # on=False → 撮らない
        r3 = _GroupPage("r3")
        s3 = _make_session([r3], roots={r3: GroupState(on=False, spa_on=True, selector="")})
        await s3.on_spa_changed({"page": r3}, token=s3.token)
        assert s3.runner.calls == []

    asyncio.run(scenario())


def test_shoot_passes_group_id_to_spawn():
    # 撮影要求にはグループの id（作成時刻）が渡り、保存先フォルダ/ログで系譜を見分けられる。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session(
            [r], roots={r: GroupState(on=True, spa_on=True, selector="", id="20260814101105674")}
        )
        await s.on_spa_changed({"page": r}, token=s.token)
        assert s.runner.group_ids == ["20260814101105674"]

    asyncio.run(scenario())


def test_shoot_skips_url_out_of_scope_and_does_not_spawn():
    # _shoot は should_capture で弾いた URL では None を返し、runner.spawn を呼ばない（#35）。
    # 判定ロジック自体は should_capture 側で厚く検証済み。ここは _shoot が結果を尊重する配線を守る。
    from edge_auto_capture import GroupState

    cfg = Config(skip_urls=("https://skip.test/",))
    s = _make_session([], config=cfg)
    skip = _GroupPage("skip", url="https://skip.test/logout")  # skip_urls に前方一致
    grp = GroupState(on=True, spa_on=False, selector="#x")
    assert s._shoot(skip, grp, "manual") is None
    assert s.runner.calls == []


def test_shoot_returns_none_when_url_unavailable():
    # pg.url の取得が失敗（ページが切断された等）したら、判定へ進まず None を返し spawn しない（#35）。
    from edge_auto_capture import GroupState

    class _UrlErrorPage:
        name = "boom"

        @property
        def url(self):
            raise RuntimeError("page detached")

    s = _make_session([])
    grp = GroupState(on=True, spa_on=False, selector="#x")
    assert s._shoot(_UrlErrorPage(), grp, "url") is None
    assert s.runner.calls == []


def test_refresh_panels_distributes_each_pages_own_group_state():
    # refresh_panels は開いている各ページへ、そのページが属するグループの状態を配る（#35）。
    # ページごとに状態が違うことを、2 グループのダブルで検証する。
    from edge_auto_capture import GroupState

    class _PanelPage:
        """opener() を持ちつつ、try_eval 経由で実行された JS を記録するページ代役。"""

        def __init__(self, name: str) -> None:
            self.name = name
            self.evals: list[str] = []

        async def evaluate(self, js, *args):
            self.evals.append(js)
            return None

        async def opener(self):
            return None

    async def scenario():
        p1 = _PanelPage("p1")
        p2 = _PanelPage("p2")
        g1 = GroupState(on=True, spa_on=True, selector="#one")
        g2 = GroupState(on=False, spa_on=False, selector="")
        s = _make_session([p1, p2], roots={p1: g1, p2: g2})
        del s.refresh_panels  # _make_session の no-op を外し、本物の refresh_panels を検証する
        await s.refresh_panels()
        # 各ページには自分のグループの状態から組んだ applyState 呼び出しだけが届く。
        assert p1.evals == [badge.apply_state_call(s.ns, g1.on, g1.spa_on, g1.selector)]
        assert p2.evals == [badge.apply_state_call(s.ns, g2.on, g2.spa_on, g2.selector)]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# F-D4: 保存先フォルダを開く
#
# 「保存先」ボタンは config.output_dir（起動単位のセッションフォルダ）を OS の
# ファイルマネージャで開く。グループ状態には依存せず、token 照合だけを見る。
# 実際にフォルダを開くのは避け、open_in_file_manager をスタブして呼び出しを検証する。
# --------------------------------------------------------------------------- #


def test_open_folder_opens_session_output_dir(monkeypatch, tmp_path):
    # 押すと config.output_dir がそのまま opener へ渡る（記録状態やグループに依らない）。
    import edge_auto_capture as eac

    async def scenario():
        opened: list = []
        monkeypatch.setattr(eac, "open_in_file_manager", lambda p: opened.append(p) or True)
        out = tmp_path / "2026-08-11_143025"
        s = _make_session([_GroupPage("r")], config=Config(output_dir=out))
        await s.on_open_folder({"page": _GroupPage("r")}, token=s.token)
        assert opened == [out]

    asyncio.run(scenario())


def test_open_folder_ignores_wrong_token(monkeypatch, tmp_path):
    # token 不一致（操作バー以外からの呼び出し）はフォルダを開かない。
    import edge_auto_capture as eac

    async def scenario():
        opened: list = []
        monkeypatch.setattr(eac, "open_in_file_manager", lambda p: opened.append(p) or True)
        s = _make_session([_GroupPage("r")], config=Config(output_dir=tmp_path))
        await s.on_open_folder({"page": _GroupPage("r")}, token="wrong")
        assert opened == []

    asyncio.run(scenario())


def test_open_in_file_manager_returns_false_on_error(monkeypatch):
    # 開けない（存在しない・権限不足で例外）ときは握り潰して False を返す（バー操作を壊さない）。
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(infra.subprocess, "run", boom)
    monkeypatch.setattr(infra.os, "startfile", boom, raising=False)
    assert infra.open_in_file_manager(Path("/no/such/folder")) is False


def test_open_in_file_manager_invokes_platform_opener(monkeypatch, tmp_path):
    # 開けたら True。macOS では open コマンドへ対象パスを渡す（OS 分岐の回帰）。
    monkeypatch.setattr(infra.sys, "platform", "darwin")
    calls: list = []
    monkeypatch.setattr(infra.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    assert infra.open_in_file_manager(tmp_path) is True
    assert calls == [["open", str(tmp_path)]]


# --------------------------------------------------------------------------- #
# F-D2: セレクタ履歴（入力欄の datalist 候補）
#
# 確定したセレクタ（blur/Enter）をセッション横断の履歴へ積み、全バーの入力候補として配る。
# 新しい順・重複なし・上限あり。get_state にも同梱して遷移後のバーが候補を失わないようにする。
# --------------------------------------------------------------------------- #


def test_remember_selector_dedup_recency_and_cap():
    from edge_auto_capture import SELECTOR_HISTORY_MAX, CaptureSession

    s = CaptureSession(_FakeContext([]), Config())

    # 新規は先頭へ積まれ True を返す。
    assert s._remember_selector("#a") is True
    assert s._remember_selector("#b") is True
    assert s.selector_history == ["#b", "#a"]

    # 空文字（クリア）は積まない。
    assert s._remember_selector("   ") is False
    assert s.selector_history == ["#b", "#a"]

    # 直近と同じは並びも変わらず False。
    assert s._remember_selector("#b") is False
    assert s.selector_history == ["#b", "#a"]

    # 既出を入れ直すと重複を作らず先頭へ繰り上げる（最近使った順）。
    assert s._remember_selector("#a") is True
    assert s.selector_history == ["#a", "#b"]

    # 上限を超えたら古い方から落ちる。
    for i in range(SELECTOR_HISTORY_MAX + 5):
        s._remember_selector(f"#sel-{i}")
    assert len(s.selector_history) == SELECTOR_HISTORY_MAX
    assert s.selector_history[0] == f"#sel-{SELECTOR_HISTORY_MAX + 4}"  # 最後に入れたものが先頭


def test_commit_selector_records_history():
    # on_commit_selector が確定値を履歴へ積む（token 一致時のみ）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})

        await s.on_commit_selector({"page": r}, token=s.token, value="#x")
        assert s.selector_history == ["#x"]

        # 別の値を確定 → 先頭へ積む。
        await s.on_commit_selector({"page": r}, token=s.token, value=".price")
        assert s.selector_history == [".price", "#x"]

        # クリア（空）は積まない。
        await s.on_commit_selector({"page": r}, token=s.token, value="")
        assert s.selector_history == [".price", "#x"]

        # token 不一致は履歴を触らない。
        await s.on_commit_selector({"page": r}, token="wrong", value="#leak")
        assert s.selector_history == [".price", "#x"]

    asyncio.run(scenario())


def test_get_state_includes_selector_history():
    # get_state は履歴を同梱する（遷移後のバーが datalist 候補を失わない）。
    # token 不一致には履歴を漏らさない（利用者が入れた候補は返さない）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})
        s.selector_history = ["#x", ".price"]

        state = await s.get_state({"page": r}, token=s.token)
        assert state["history"] == ["#x", ".price"]

        denied = await s.get_state({"page": r}, token="wrong")
        assert denied["history"] == []

    asyncio.run(scenario())


def test_set_history_call_serializes_values():
    # 日本語/記号を含む値も JS 配列リテラルとして安全に埋め込む。E-3: 固定名でなく
    # 起動ごとのランダム名 ns の隠しオブジェクト window[ns].setHistory(...) を呼ぶ式を組む。
    call = badge.set_history_call("nabc", ["#main", ".一覧"])
    assert call.startswith('window["nabc"] && window["nabc"].setHistory && '
                           'window["nabc"].setHistory(')
    # json.dumps で ASCII 化（\uXXXX）され、二重引用符の配列になる。
    assert '"#main"' in call


def test_trigger_threaded_per_path():
    # 撮影契機が投入元 3 経路から CaptureRequest.trigger に載る（F-A1）:
    # on_shot="manual" / on_spa_changed="spa" / _shoot_if_changed="url" /
    # 記録開始(on_toggle)の即撮り="url"。
    from edge_auto_capture import GroupState

    async def scenario():
        # 今すぐ1枚 → manual（記録状態に関わらず撮る）
        r1 = _GroupPage("r1")
        s1 = _make_session([r1], roots={r1: GroupState(on=False, spa_on=False, selector="")})
        await s1.on_shot({"page": r1}, token=s1.token)
        assert s1.runner.triggers == ["manual"]

        # SPA変化 → spa
        r2 = _GroupPage("r2")
        s2 = _make_session([r2], roots={r2: GroupState(on=True, spa_on=True, selector="")})
        await s2.on_spa_changed({"page": r2}, token=s2.token)
        assert s2.runner.triggers == ["spa"]

        # URL変化 → url
        r3 = _GroupPage("r3", url="https://example.test/a")
        s3 = _make_session([r3], roots={r3: GroupState(on=True, spa_on=False, selector="")})
        await s3._shoot_if_changed(r3)
        assert s3.runner.triggers == ["url"]

        # 記録開始の即撮り → url
        r4 = _GroupPage("r4")
        s4 = _make_session([r4], roots={r4: GroupState(on=False, spa_on=False, selector="")})
        await s4.on_toggle({"page": r4}, token=s4.token)
        assert s4.runner.triggers == ["url"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# URL変化のイベント駆動化（B-1）: _shoot_if_changed / _on_navigated / _on_page_closed
#
# 旧・毎tickポーリングの run ループを廃し、framenavigated / close で撮る/掃除する。
# 実 Playwright を使わず、url を書き換えられるフェイクページで挙動を回帰から守る。
# --------------------------------------------------------------------------- #


def test_shoot_if_changed_only_on_real_url_change():
    # 記録ONのページは、URLが前回と変わったときだけ撮る。ハッシュのみの変化・
    # 同一URL・skip_urls・記録OFF では撮らない。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r", url="https://example.test/a")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})

        # 初回: seen 空 → 撮る。seen が現URLキーにそろう。
        await s._shoot_if_changed(r)
        assert [c[1] for c in s.runner.calls] == ["https://example.test/a"]

        # 同一URLで再度 → 撮らない（seen 一致）。
        await s._shoot_if_changed(r)
        assert len(s.runner.calls) == 1

        # ハッシュだけ変化（scroll-spy）→ 撮らない。
        r.url = "https://example.test/a#sec2"
        await s._shoot_if_changed(r)
        assert len(s.runner.calls) == 1

        # 本当のURL変化 → 撮る。selector はグループの値が渡る。
        r.url = "https://example.test/b"
        await s._shoot_if_changed(r)
        assert s.runner.calls[-1] == (r, "https://example.test/b", "#x")

    asyncio.run(scenario())


def test_shoot_if_changed_gated_by_recording_and_skip_urls():
    from edge_auto_capture import GroupState

    async def scenario():
        # 記録OFF → 撮らず seen も更新しない（ON にした瞬間の即撮りに委ねる）。
        off = _GroupPage("off", url="https://example.test/x")
        s = _make_session([off], roots={off: GroupState(on=False, spa_on=False, selector="")})
        await s._shoot_if_changed(off)
        assert s.runner.calls == []
        assert off not in s.seen

        # skip_urls に一致 → 撮らない。
        cfg = Config(skip_urls=("https://skip.test/",))
        skip = _GroupPage("skip", url="https://skip.test/")
        s2 = _make_session(
            [skip], roots={skip: GroupState(on=True, spa_on=False, selector="")}, config=cfg
        )
        await s2._shoot_if_changed(skip)
        assert s2.runner.calls == []

    asyncio.run(scenario())


def test_on_navigated_ignores_subframe_navigations():
    # 子フレーム（iframe 等）の遷移では撮らない。メインフレームの遷移だけ拾う。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r", url="https://example.test/a")
        r.main_frame = object()          # メインフレームの目印
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="")})

        await s._on_navigated(r, frame=object())   # 別フレーム → 無視
        assert s.runner.calls == []

        await s._on_navigated(r, frame=r.main_frame)  # メインフレーム → 撮る
        assert [c[1] for c in s.runner.calls] == ["https://example.test/a"]

    asyncio.run(scenario())


def test_on_page_closed_prunes_state_and_gcs_group():
    # 閉じたページは seen / page_root / _tracked から消え、どの生存ページからも
    # 参照されなくなった root のグループも捨てられる。ポップアップが残る間は保持する。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        s = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        # popup を root グループへ合流させ、追跡・seen も載せておく。
        await s._resolve_group(popup)
        s._tracked.update({root, popup})
        s.seen[root] = "k1"
        s.seen[popup] = "k2"

        # root を閉じる: popup がまだ root を参照しているのでグループは残る。
        s._on_page_closed(root)
        assert root not in s.seen and root not in s.page_root and root not in s._tracked
        assert root in s.groups  # popup が参照中なので生存

        # popup も閉じる: root への参照が消え、グループが GC される。
        s._on_page_closed(popup)
        assert popup not in s.seen and popup not in s.page_root
        assert s.groups == {}

    asyncio.run(scenario())


def test_group_subdir_and_folder_name():
    # 採番済みは output_dir/lineage-<id>、未採番(空)は output_dir 直下。
    from pathlib import Path

    from capture import group_folder_name, group_subdir

    out = Path("/tmp/out")
    assert group_folder_name("20260814101105674") == "lineage-20260814101105674"
    assert group_subdir(out, "20260814101105674") == out / "lineage-20260814101105674"
    assert group_subdir(out, "") == out


def test_make_group_id_is_timestamp():
    # 新規グループの id は「作成時刻（ミリ秒まで）」の文字列（ログ/フォルダ識別用）。
    async def scenario():
        s = _make_session([])
        g = s._make_group(on=False, spa_on=False, selector="")
        # 例: 20260814101105674（YYYYMMDDHHMMSSmmm・区切りなし＝17桁）
        assert re.fullmatch(r"\d{17}", g.id)

    asyncio.run(scenario())


def test_get_state_returns_group_state():
    # get_state は問い合わせ元ページのグループの状態を返す（token 一致時）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=True, selector="#c")})
        state = await s.get_state({"page": r}, token=s.token)
        # 撮影カウンタ（count）とセレクタ履歴（history）も同梱する（F-D3/F-D2。
        # 再描画されたバーの枚数復元・datalist 候補復元に使う）。
        assert state == {
            "recording": True, "spa": True, "selector": "#c", "count": 0, "history": [],
        }
        # token 不一致には既定（状態を漏らさない）。count は秘匿情報ではないので返す。
        assert await s.get_state({"page": r}, token="wrong") == {
            "recording": False, "spa": False, "selector": "", "count": 0, "history": [],
        }

    asyncio.run(scenario())


def test_prune_drops_group_when_all_pages_closed():
    # ページが全て閉じたグループは、状態が捨てられる（root が閉じても系譜が残れば保持）。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        session = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        await session._resolve_group(popup)  # popup を root グループへ合流させる

        # root だけ閉じる（popup は生存）→ グループは残る
        session._on_page_closed(root)
        assert root in session.groups
        assert session.page_root.get(popup) is root

        # popup も閉じる → 参照ゼロでグループ破棄
        session._on_page_closed(popup)
        assert session.groups == {}
        assert session.page_root == {}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 索引 CSV（F-A1）+ 撮影時刻（F-A4）
#
# 撮影ごとに output_dir/index.csv へ 1 行追記する。地雷 2 つ（BOM 付き utf-8-sig で書く／
# 時刻は ISO 8601 オフセット付き）と、追記時に BOM・見出しを重複させないことを回帰から守る。
# --------------------------------------------------------------------------- #


def test_iso_timestamp_is_offset_aware_iso8601():
    # F-A4: ISO 8601・ミリ秒・UTC オフセット付き（例: 2026-08-11T14:30:25.123+09:00）。
    ts = infra.iso_timestamp()
    from datetime import datetime

    parsed = datetime.fromisoformat(ts)   # 解釈不能なら例外で落ちる
    assert parsed.tzinfo is not None      # オフセット（tzinfo）を必ず持つ
    # ミリ秒精度: 小数第 3 位まで（マイクロ秒の 6 桁ではない）。
    assert re.search(r"T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$", ts)


def test_log_line_uses_offset_timestamp(monkeypatch, tmp_path):
    # log() の行頭時刻もオフセット付き ISO（索引と突き合わせられるよう表記をそろえる）。
    monkeypatch.setattr(infra, "LOG_PATH", tmp_path / "log.txt")
    infra.log("hello")
    line = (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()[0]
    stamp, _, msg = line.partition(" ")
    assert msg == "hello"
    from datetime import datetime

    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_trigger_label_maps_known_and_passes_through():
    assert trigger_label("manual") == "手動"
    assert trigger_label("url") == "URL変化"
    assert trigger_label("spa") == "SPA変化"
    assert trigger_label("") == ""          # 未設定はそのまま
    assert trigger_label("other") == "other"  # 未知値は素通し


def _read_index(path: Path) -> list[list[str]]:
    """index.csv を読み、ヘッダ込みの行リストを返す（BOM は utf-8-sig で剥がす）。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_append_index_writes_bom_header_and_row(tmp_path):
    # 新規作成時: 先頭 BOM＋見出し＋データ 1 行。列は仕様どおりの並び・値。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path)
    cfg.output_dir.mkdir(exist_ok=True)
    runner._append_index(
        cfg, "2026-08-11T14:30:25.123+09:00", "https://example.test/x",
        "タイトル,あり", "2026-08-11_14-30-25-123_stem", "spa", "#main", ["png", "txt"],
    )
    path = tmp_path / "index.csv"
    # 地雷1: BOM 付きで書く（BOM 無しだと Excel で文字化け）。
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _read_index(path)
    assert rows[0] == ["時刻", "URL", "タイトル", "ファイル名接頭辞", "撮影契機", "セレクタ", "成否"]
    assert rows[1] == [
        "2026-08-11T14:30:25.123+09:00", "https://example.test/x",
        "タイトル,あり",   # カンマ入りタイトルも csv が退避して 1 セルに収まる
        "2026-08-11_14-30-25-123_stem", "SPA変化", "#main", "成功",
    ]


def test_append_index_appends_without_duplicate_bom_or_header(tmp_path):
    # 追記時: BOM も見出しも増やさず、行だけ足す。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path)
    cfg.output_dir.mkdir(exist_ok=True)
    runner._append_index(cfg, "t1", "u1", "titleA", "stemA", "manual", "", ["png"])
    runner._append_index(cfg, "t2", "u2", "titleB", "stemB", "url", "#s", [])
    raw = (tmp_path / "index.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1        # BOM は先頭 1 回だけ
    rows = _read_index(tmp_path / "index.csv")
    assert len(rows) == 3                          # 見出し + 2 行
    assert rows[1][4] == "手動" and rows[1][6] == "成功"
    assert rows[2][4] == "URL変化" and rows[2][6] == "失敗"  # done 空 → 失敗


def test_append_index_failure_does_not_raise(monkeypatch, tmp_path):
    # 索引の書き込み失敗は握って撮影を巻き込まない（ログに [skip index] を出すだけ）。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path / "missing")   # 親フォルダが無く open が失敗する
    logged: list[str] = []
    monkeypatch.setattr(capture, "log", lambda m: logged.append(m))
    runner._append_index(cfg, "t", "u", "ti", "st", "manual", "", ["png"])  # 例外は出ない
    assert any("[skip index]" in m for m in logged)


# --------------------------------------------------------------------------- #
# F-D3: 撮影カウンタ / 失敗表示（成否を JS へ通知する経路）
# --------------------------------------------------------------------------- #


def test_capture_end_call_encodes_success_and_failure():
    # done 有無を真偽値としてページ側 captureEnd へ渡す呼び出し式を組む（成功=赤/失敗=琥珀）。
    # E-3: 固定名でなく起動ごとのランダム名 ns の隠しオブジェクト window[ns].captureEnd(...) を呼ぶ。
    assert badge.capture_end_call("nabc", True) == (
        'window["nabc"] && window["nabc"].captureEnd && window["nabc"].captureEnd(true)'
    )
    assert badge.capture_end_call("nabc", False) == (
        'window["nabc"] && window["nabc"].captureEnd && window["nabc"].captureEnd(false)'
    )


def test_set_count_call_encodes_count():
    # 撮影カウンタ（本セッション枚数）をバーへ配る呼び出し式を組む（E-3: window[ns].setCount）。
    assert badge.set_count_call("nabc", 0) == (
        'window["nabc"] && window["nabc"].setCount && window["nabc"].setCount(0)'
    )
    assert badge.set_count_call("nabc", 7) == (
        'window["nabc"] && window["nabc"].setCount && window["nabc"].setCount(7)'
    )


def test_new_namespace_is_random_and_carries_no_hint():
    # E-3: 起動ごとに使い捨てるランダム名。先頭は英字（数値インデックス的扱いを避ける）で、
    # __eac のような固定の手掛かりを含まない（含めると全文一致でなくても勘付かれうる）。
    a, b = badge.new_namespace(), badge.new_namespace()
    assert a != b
    assert a[0].isalpha()
    assert "__eac" not in a and "eac" not in a


def test_build_badge_script_hides_fixed_globals():
    # E-3: 固定名（window.__eacApplyState 等）を生やさず、ランダム名 ns の非列挙プロパティへ収める。
    # ②のページ→Python バインディング固定名は退避後に window から削除する。
    script = badge.build_badge_script(token="t", ns="nXYZ")
    assert '"ns": "nXYZ"' in script                       # ns が $CONFIG に載る
    assert "window.__eacApplyState =" not in script       # 旧方式の固定名代入が残っていない
    assert "window.__eacSetCount =" not in script
    assert "Object.defineProperty(window, NS" in script   # ①を隠しプロパティで公開
    assert "delete window[n]" in script                   # ②の固定名を削除


class _EvalPage:
    """try_eval の宛先になる最小のページ代役。実行された JS を記録する。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.evals: list[str] = []

    async def evaluate(self, js, *args):
        self.evals.append(js)
        return None


def test_session_wires_runner_on_result():
    # CaptureSession は runner の成否通知を自分のカウンタ更新へ配線する（F-D3）。
    from edge_auto_capture import CaptureSession

    session = CaptureSession(_FakeContext([]), Config())
    assert session.runner.on_result == session._on_capture_result
    assert session.shots == 0


def test_on_capture_result_counts_only_success_and_pushes():
    # ok=True のときだけ枚数を増やし、その値を全ページの操作バーへ配る。ok=False は数えない。
    from edge_auto_capture import CaptureSession

    async def scenario():
        p1, p2 = _EvalPage("1"), _EvalPage("2")
        session = CaptureSession(_FakeContext([p1, p2]), Config())

        await session._on_capture_result(True)
        assert session.shots == 1
        # 成功のたびに現在の枚数を全ページへ配る（set_count_call の式が evaluate される）。
        # E-3: 呼び出し式はセッションのランダム名 ns（window[ns].setCount）を使う。
        assert p1.evals == [badge.set_count_call(session.ns, 1)]
        assert p2.evals == [badge.set_count_call(session.ns, 1)]

        await session._on_capture_result(False)   # 全滅は「撮れた1枚」に数えない
        assert session.shots == 1
        assert len(p1.evals) == 1                  # 配布もしない（枚数が変わらないため）

        await session._on_capture_result(True)
        assert session.shots == 2
        assert p1.evals[-1] == badge.set_count_call(session.ns, 2)

    asyncio.run(scenario())


def test_get_state_reports_current_shot_count():
    # get_state は現在の撮影カウンタを count で返す（再描画されたバーの枚数復元用）。
    # 記録状態を伏せる token 不一致の応答にも count は載る（枚数は秘匿情報ではない）。
    from edge_auto_capture import CaptureSession

    async def scenario():
        session = CaptureSession(_FakeContext([]), Config())
        await session._on_capture_result(True)
        await session._on_capture_result(True)
        state = await session.get_state(None, token="wrong")
        assert state["count"] == 2

    asyncio.run(scenario())


class _FakeCapturePage:
    """_capture を実 Edge 無しで通すための最小ページ代役（成功/失敗を切り替えられる）。"""

    def __init__(self, *, screenshot_ok: bool = True, text_ok: bool = True) -> None:
        self.screenshot_ok = screenshot_ok
        self.text_ok = text_ok
        self.eval_calls: list[str] = []

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def title(self):
        return "T"

    async def screenshot(self, path=None, full_page=None):
        if not self.screenshot_ok:
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"png")

    async def evaluate(self, js, *args):
        self.eval_calls.append(js)
        # 既定の CaptureRunner は ns="" なので、_capture は body_text_call("") を渡す（E-3）。
        if js == badge.body_text_call(""):
            if not self.text_ok:
                raise RuntimeError("no text")
            return "body text"
        return None


def test_capture_notifies_result_true_on_success(tmp_path):
    # png/txt が撮れたら captureEnd に ok=true を渡し、on_result にも True が届く（F-D3）。
    async def scenario():
        runner = CaptureRunner()
        got: list[bool] = []

        async def record(ok):
            got.append(ok)

        runner.on_result = record
        page = _FakeCapturePage(screenshot_ok=True, text_ok=True)
        cfg = Config(output_dir=tmp_path, settle_delay=0)
        await runner._capture(CaptureRequest(page, "https://ok.test/", cfg, trigger="manual"))

        assert got == [True]
        assert badge.capture_end_call(runner.ns, True) in page.eval_calls
        assert badge.capture_end_call(runner.ns, False) not in page.eval_calls

    asyncio.run(scenario())


def test_capture_notifies_result_false_on_total_failure(tmp_path):
    # 全滅（png も txt も失敗）なら captureEnd に ok=false を渡し、on_result にも False が届く。
    async def scenario():
        runner = CaptureRunner()
        got: list[bool] = []

        async def record(ok):
            got.append(ok)

        runner.on_result = record
        page = _FakeCapturePage(screenshot_ok=False, text_ok=False)
        cfg = Config(output_dir=tmp_path, settle_delay=0)
        await runner._capture(CaptureRequest(page, "https://ng.test/", cfg, trigger="manual"))

        assert got == [False]
        assert badge.capture_end_call(runner.ns, False) in page.eval_calls
        assert badge.capture_end_call(runner.ns, True) not in page.eval_calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "trigger, expect_settle_sleep",
    [
        ("manual", True),   # 手動：load 直後にまだ描画が動くので settle_delay を待つ
        ("url", True),      # URL遷移：同上
        ("spa", False),     # SPA：ページ側デバウンスで確定済み。二重待ちを避けて省く（B-4, #18）
    ],
)
def test_capture_skips_settle_sleep_only_for_spa(tmp_path, monkeypatch, trigger, expect_settle_sleep):
    # settle_delay 分の sleep が SPA 経由でだけ省かれることを、記録した sleep 秒数で確かめる。
    async def scenario():
        slept: list[float] = []

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(capture.asyncio, "sleep", fake_sleep)

        runner = CaptureRunner()
        page = _FakeCapturePage(screenshot_ok=True, text_ok=True)
        cfg = Config(output_dir=tmp_path, settle_delay=0.4)
        await runner._capture(CaptureRequest(page, "https://ok.test/", cfg, trigger=trigger))

        assert (0.4 in slept) is expect_settle_sleep

    asyncio.run(scenario())
