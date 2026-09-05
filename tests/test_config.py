"""設定読み込み（config.py）のユニットテスト。

config.ini のパース・既定値へのフォールバック・自己修復（D-C3）・保存先の解決と
セッションフォルダ（F-C3）・撮影対象 URL の判定（should_capture）を守る。
実 Edge 不要（config は infra だけに依存し Playwright を import しない）。

実行:
    pip install -e ".[dev]"
    pytest
"""

import re
from pathlib import Path

import pytest

import capture
import config as config_mod
import infra
import lineage
from config import Config, load_config, should_capture

# session_stamp の実装本体への参照（conftest の autouse フィクスチャが "" へ差し替える前に押さえる）。
# 差し替え後も本物の書式を検証できるようにするため（F-C3）。
_REAL_SESSION_STAMP = config_mod.session_stamp


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


def test_eval_timeout_sec_converts_milliseconds_to_seconds():
    # config.ini はミリ秒、コード内（asyncio）は秒。換算点は Config へ 1 本化してある（#56）。
    assert Config().eval_timeout_sec == 5.0
    assert Config(eval_timeout=250).eval_timeout_sec == 0.25


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
    # 撮影物の系譜サブフォルダ（lineage.group_subdir）はセッションフォルダ配下。
    assert lineage.group_subdir(c.output_dir, "20260811143025000") == (
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
# summarize_config（D-B2: 採用された設定値を 1 行で残す）
# --------------------------------------------------------------------------- #


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

