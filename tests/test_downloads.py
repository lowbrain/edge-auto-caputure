"""E-4（ダウンロード消失）の回帰テスト。

Playwright は既定でダウンロードを一時領域に受け、コンテキストを閉じる際に削除する。
accept_downloads / downloads_path を指定しても削除されるため（実機で確認済）、
download イベントで save_as して保存先へ退避することで初めて残る。
ここでは実ブラウザなしで、退避ロジック（保存先・連番・save_as 呼び出し）を検証する。
"""

import asyncio
from pathlib import Path

from browser import browser_launch_kwargs
from config import Config
from edge_auto_capture import (
    CaptureSession,
    _downloads_dir,
    _unique_path,
)


class FakeDownload:
    """download.suggested_filename と save_as だけを持つスタブ。"""

    def __init__(self, name: str, content: str = "x") -> None:
        self.suggested_filename = name
        self._content = content

    async def save_as(self, path: str) -> None:
        Path(path).write_text(self._content, encoding="utf-8")


def _session(output_dir: Path) -> CaptureSession:
    return CaptureSession(context=None, config=Config(output_dir=output_dir))


# --------------------------------------------------------------------------- #
# 保存先・起動オプション
# --------------------------------------------------------------------------- #


def test_downloads_dir_is_under_output_dir():
    c = Config(output_dir=Path("/tmp/out"))
    assert _downloads_dir(c) == Path("/tmp/out") / "downloads"


def test_launch_kwargs_accept_downloads():
    c = Config(output_dir=Path("/tmp/out"))
    kwargs = browser_launch_kwargs(c, user_data_dir="/tmp/prof", channel="msedge")
    assert kwargs["accept_downloads"] is True


# --------------------------------------------------------------------------- #
# 連番による衝突回避
# --------------------------------------------------------------------------- #


def test_unique_path_returns_name_when_free(tmp_path):
    assert _unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"


def test_unique_path_appends_counter_on_collision(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    assert _unique_path(tmp_path, "a.txt") == tmp_path / "a(1).txt"
    (tmp_path / "a(1).txt").write_text("2")
    assert _unique_path(tmp_path, "a.txt") == tmp_path / "a(2).txt"


def test_unique_path_keeps_suffix_for_dotted_names(tmp_path):
    (tmp_path / "report.tar.gz").write_text("1")
    # Path.stem/suffix は最後のドットで分けるので report.tar / .gz になる。
    assert _unique_path(tmp_path, "report.tar.gz") == tmp_path / "report.tar(1).gz"


# --------------------------------------------------------------------------- #
# on_download: save_as で保存先へ退避する
# --------------------------------------------------------------------------- #


def test_on_download_saves_with_suggested_name(tmp_path):
    dd = _downloads_dir(Config(output_dir=tmp_path))
    dd.mkdir(parents=True, exist_ok=True)
    s = _session(tmp_path)
    asyncio.run(s.on_download(FakeDownload("hello.txt", "Hello, E-4!")))
    saved = dd / "hello.txt"
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == "Hello, E-4!"


def test_on_download_does_not_overwrite_same_name(tmp_path):
    dd = _downloads_dir(Config(output_dir=tmp_path))
    dd.mkdir(parents=True, exist_ok=True)
    s = _session(tmp_path)
    asyncio.run(s.on_download(FakeDownload("dup.txt", "first")))
    asyncio.run(s.on_download(FakeDownload("dup.txt", "second")))
    assert (dd / "dup.txt").read_text(encoding="utf-8") == "first"
    assert (dd / "dup(1).txt").read_text(encoding="utf-8") == "second"


def test_on_download_uses_fallback_name_when_missing(tmp_path):
    dd = _downloads_dir(Config(output_dir=tmp_path))
    dd.mkdir(parents=True, exist_ok=True)
    s = _session(tmp_path)
    asyncio.run(s.on_download(FakeDownload("", "body")))
    assert (dd / "download").read_text(encoding="utf-8") == "body"
