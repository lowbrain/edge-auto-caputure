"""基盤ユーティリティ（アプリの土台。Playwright 非依存）。

- 基準フォルダ（BASE_DIR / _base_dir）
- ログ（LOG_PATH / set_log_dir / log）
- 致命的エラー通知（notify_fatal / _message_box）
- 一時プロファイルの後始末（cleanup_old_profiles）

Playwright やページ操作には依存しないので、実 Edge 無しで import・テストできる
（config.py はここだけに依存し、Edge 無しで設定読み込みを検証できる）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional


def _base_dir() -> Path:
    """設定・保存先の基準フォルダを返す。

    PyInstaller で exe 化した場合（sys.frozen）は exe のあるフォルダ、
    通常の Python 実行時はこのスクリプトのあるフォルダを基準にする。
    これにより、配布した exe の隣に置いた config.ini を読み、
    output\\ も exe の隣に作れる（＝第三者が config.ini を編集できる）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# 設定・出力の基準フォルダ（通常実行なら本ファイル、exe 実行なら exe と同じ場所）。
BASE_DIR = _base_dir()

# 実行ログの出力先。コンソール無し（windowed exe）で実行しても後から動作を追える
# ようにする。既定は基準フォルダだが、設定読み込み後に PNG などと同じ保存先
# （output_dir）へ切り替える（set_log_dir）。設定を読む前の初期ログはここへ出る。
LOG_PATH = BASE_DIR / "log.txt"


def resolve_writable_dir(preferred: Path) -> Optional[Path]:
    """書き込み可能なフォルダを返す。preferred が使えなければ退避先を試す。

    第三者が exe を C:\\Program Files\\ など書き込み権限の無い場所へ展開した場合、
    preferred（＝設定された output_dir）への mkdir / 書き込みが PermissionError で
    失敗する。--noconsole ビルドでは stderr も見えず「ダブルクリックしても何も
    起きない」状態になる（D-C1）。それを避けるため、preferred が駄目なら
    %LOCALAPPDATA%（無ければ一時フォルダ）配下へ退避して動き続けられるようにする。

    実際に mkdir して小さなファイルを書ける（そして消せる）ことまで確認した候補だけを
    返す。どこにも書けなければ None（呼び出し側が notify_fatal して終了する）。
    退避が起きたかどうかは、返り値が preferred と一致するかで呼び出し側が判定する。
    """
    fallback_base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    candidates = [preferred, Path(fallback_base) / "edge-auto-capture" / preferred.name]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".eac-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            continue
    return None


def set_log_dir(directory: Path) -> None:
    """ログの出力先フォルダを PNG などの保存先（output_dir）へ寄せる。

    設定読み込みで output_dir が確定した直後に呼ぶ。以後の log() は
    <output_dir>\\log.txt へ追記する。まだ無ければフォルダを作る
    （直後の書き込みで取りこぼさないため）。
    """
    global LOG_PATH
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    LOG_PATH = directory / "log.txt"


def log(msg: str) -> None:
    """メッセージを log.txt へ追記し、可能ならコンソールにも出す。

    windowed exe（コンソール無し）では sys.stdout が None になり得るため、
    print は失敗しても無視する。ファイルへの記録を主とする。
    """
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def _message_box(msg: str, title: str = "edge-auto-capture") -> None:
    """致命的エラーをネイティブのダイアログで通知する（失敗しても無視）。

    コンソールを持たない windowed exe（Windows 配布物）では print が見えないため、
    起動失敗などはダイアログで利用者に伝える。配布対象は Windows だが、開発機の
    macOS でも同じエラーをダイアログで確認できるよう OS ごとに分岐する。
    どの経路でも例外は握り潰す（ログ側で確実に残るため、通知は best-effort）。
    """
    if sys.platform == "darwin":
        _message_box_macos(msg, title)
    else:
        _message_box_windows(msg, title)


def _message_box_windows(msg: str, title: str) -> None:
    """Windows のメッセージボックス（MB_ICONERROR）で通知する。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _message_box_macos(msg: str, title: str) -> None:
    """macOS で osascript を使いエラーダイアログを表示する（開発時の確認用）。

    メッセージ・タイトルは AppleScript 文字列リテラルとして安全に埋め込む
    （\\ と " をエスケープ）。AppleScript の文字列は生の改行を扱えないため、
    改行は 'return' 連結（" & return & "）へ置き換える（notify_fatal の
    複数行メッセージでも欠けずに表示できるように）。osascript が無い/失敗しても無視する。
    """
    def esc(s: str) -> str:
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        return s.replace("\n", '" & return & "')

    script = (
        f'display dialog "{esc(msg)}" with title "{esc(title)}" '
        'buttons {"OK"} default button "OK" with icon stop'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


def notify_fatal(msg: str) -> None:
    """致命的メッセージをログとダイアログの両方へ出す（終了処理は呼び出し側）。"""
    log(msg)
    _message_box(msg)


def cleanup_old_profiles(keep: Optional[Path] = None) -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。

    keep を渡すと、そのフォルダは掃除対象から除外する（再利用する永続
    プロファイル [F-C1] を誤って消さないための安全弁）。永続プロファイルは
    通常 edge-debug-* とは別名・別置き場所なので glob には一致しないが、
    利用者が一時フォルダ配下に edge-debug- で始まる名前を指定した場合の保険。
    """
    keep_resolved = keep.resolve() if keep is not None else None
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            if keep_resolved is not None and d.resolve() == keep_resolved:
                continue
            shutil.rmtree(d, ignore_errors=True)
