"""pytest 共通設定。

プロジェクト直下（このファイルの親の親）を import パスへ入れ、
tests/ からトップレベルモジュール（infra / config / capture / badge / edge_auto_capture）を
そのまま import できるようにする。

あわせて、テスト全体に効く安全弁（ダイアログを出さない・ログを一時フォルダへ逃がす・
session_stamp を固定する）を autouse フィクスチャで張る。以前は test_capture.py の中だけに
置いていたため、テストをモジュールごとに分けると各ファイルへ同じものを複製することになる。
ここへ移して 1 か所で持ち、tests/ 配下すべてに同じ前提を効かせる。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as config_mod  # noqa: E402  （上の sys.path 挿入より後でないと import できない）
import infra  # noqa: E402


@pytest.fixture(autouse=True)
def _no_dialog_no_repo_writes(monkeypatch, tmp_path):
    """テスト中に Windows のメッセージボックスを出さない・リポジトリへログを書かない。

    - notify_fatal 経由の _message_box はダイアログを出しテストを止めるので no-op に。
    - log() の書き込み先（LOG_PATH）を一時フォルダへ逃がす。
    どちらも基盤ユーティリティ（infra）にあるので infra を差し替える。
    - session_stamp（F-C3 の起動時刻サブフォルダ名）を "" に固定する。実時刻由来だと
      load_config が返す output_dir が起動秒ごとに変わり、設定パース系テストの
      output_dir 比較が不安定になるため、既定では無効化して基準フォルダのままにする。
      セッションフォルダ挿入そのものは test_load_config_inserts_session_folder 系で検証する。
    """
    monkeypatch.setattr(infra, "_message_box", lambda *a, **k: None)
    monkeypatch.setattr(infra, "LOG_PATH", tmp_path / "log.txt")
    monkeypatch.setattr(config_mod, "session_stamp", lambda: "")
