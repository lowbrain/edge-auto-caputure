"""基盤ユーティリティ（infra.py）のユニットテスト。

バージョンの出所（D-B1）・起動環境ログ（D-B2）・書き込み先の退避（D-C1）・
多重起動抑止（D-C4）を守る。Playwright 非依存なので実 Edge 不要。

実行:
    pip install -e ".[dev]"
    pytest
"""

import re
from pathlib import Path

import pytest

import infra

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

