"""E-4（ダウンロード消失）の回帰テスト。

Playwright は既定でダウンロードを一時領域に受け、コンテキストを閉じる際に削除する。
起動オプションで受け皿を明示し、利用者のダウンロードが終了時に消えないことを守る。
実ブラウザは不要（起動オプションの組み立てと受け皿パスの純粋な検証）。
"""

from pathlib import Path

from config import Config
from edge_auto_capture import _browser_launch_kwargs, _downloads_dir


def test_downloads_dir_is_under_output_dir():
    c = Config(output_dir=Path("/tmp/out"))
    assert _downloads_dir(c) == Path("/tmp/out") / "downloads"


def test_launch_kwargs_enable_and_pin_downloads():
    c = Config(output_dir=Path("/tmp/out"))
    kwargs = _browser_launch_kwargs(c, user_data_dir="/tmp/prof", channel="msedge")
    # 受け入れが有効で、受け皿が output_dir 配下へ固定されていること。
    assert kwargs["accept_downloads"] is True
    assert kwargs["downloads_path"] == str(Path("/tmp/out") / "downloads")
