"""純粋関数（capture）・設定読み込み（config）のユニットテスト。

実 Edge を必要としない速いテスト（smoke_badge.py の補完）。
docstring/コメントに書かれた「微妙な仕様」（切り詰め・フォールバック・
既定値・空値の扱い）を回帰から守ることが狙い。

実行:
    pip install -e ".[dev]"
    pytest
"""

import asyncio
import re
from pathlib import Path

import pytest

import capture
import config as config_mod
import infra
from capture import CaptureRunner, page_label, safe_name
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

    async def stub(page, url, config, selector=""):
        calls.append((page, url, selector))
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
            runner.spawn(page, f"url-{i}", cfg)
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

        runner.spawn(page, "url-1", cfg)
        worker = runner._workers[page]
        for _ in range(5):  # 第1撮影を gate 待ちまで進める
            await asyncio.sleep(0)
        assert calls == [(page, "url-1", "")]  # 1件目が in-flight

        # 撮影中に3回要求 → _pending は最新(url-4, sel-4)で上書きされる
        runner.spawn(page, "url-2", cfg, "sel-2")
        runner.spawn(page, "url-3", cfg, "sel-3")
        runner.spawn(page, "url-4", cfg, "sel-4")

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
        runner.spawn(p1, "a", cfg)
        runner.spawn(p2, "b", cfg)
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
        runner.spawn(page, "first", cfg)
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert page not in runner._workers  # drain 後に退場

        runner.spawn(page, "second", cfg)  # 再度 spawn → 新 worker
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert [c[1] for c in calls] == ["first", "second"]

    asyncio.run(scenario())
