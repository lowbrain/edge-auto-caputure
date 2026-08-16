"""基盤ユーティリティ（アプリの土台。Playwright 非依存）。

- 基準フォルダ（BASE_DIR / _base_dir）
- ログ（LOG_PATH / set_log_dir / log / iso_timestamp）
- 致命的エラー通知（notify_fatal / _message_box）
- 一時プロファイルの後始末（cleanup_old_profiles）

Playwright やページ操作には依存しないので、実 Edge 無しで import・テストできる
（config.py はここだけに依存し、Edge 無しで設定読み込みを検証できる）。
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# バージョンの単一の出所（D-B1）。pyproject.toml は
# [tool.setuptools.dynamic] version = {attr = "infra.__version__"} でここを参照する。
# infra は依存の最下層（Playwright 非依存）なので循環せず、exe / ログ / UI から参照できる。
__version__ = "0.1.0"


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


def iso_timestamp() -> str:
    """現在時刻を ISO 8601（ミリ秒・UTC オフセット付き）で返す。

    例: 2026-08-11T14:30:25.123+09:00 。壁時計の値は変えず（日本の PC なら JST のまま）、
    末尾に「どの時間帯か」を示すオフセットを添えて時刻を自己記述的にする。ログや索引 CSV は
    あとから機械的に検索・照合される前提なので、環境・TZ に依存せず一意に解釈できるこの形で残す。
    astimezone() が実行環境のローカル TZ を採用する（日本環境なら +09:00）。
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def log(msg: str) -> None:
    """メッセージを log.txt へ追記し、可能ならコンソールにも出す。

    windowed exe（コンソール無し）では sys.stdout が None になり得るため、
    print は失敗しても無視する。ファイルへの記録を主とする。
    時刻は ISO 8601 オフセット付き（F-A4）。索引 CSV と時刻表記をそろえ、後から突き合わせられる。
    """
    line = f"{iso_timestamp()} {msg}"
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

        # windll は Windows 専用（型スタブは OS で異なり、macOS には windll が無い）。この関数は
        # Windows でしか呼ばれない前提。Any 経由で属性アクセスし、OS でスタブが違っても型チェックが
        # 割れないようにする。`# type: ignore` は Windows 側で warn_unused_ignores に、getattr は
        # ruff B009 に引っかかるため、どちらも避けて Any を採る。
        ctypes_any: Any = ctypes
        ctypes_any.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
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


def startup_environment_line() -> str:
    """起動環境（OS・Python・実行形態・基準フォルダ）を1行に整形する（D-B2）。

    channel="msedge"/"chrome" は環境の Edge/Chrome に依存するため、どの OS・どの
    ランタイム・どの場所で動いたのかをログへ残すと不具合の切り分けが速い。実際に
    採用された設定値は config.summarize_config が、ブラウザの版（起動後にしか分から
    ない）は起動側が別行で出す。ここは Playwright 非依存で分かる範囲だけを担う。
    """
    frozen = "exe" if getattr(sys, "frozen", False) else "script"
    return (
        f"[env] OS={platform.platform()} "
        f"Python={platform.python_version()} "
        f"実行={frozen} base={BASE_DIR}"
    )


# 使い捨てプロファイルを「掃除対象」とみなす下限の経過時間（秒）。
# これより新しい edge-debug-* は、別インスタンスが今まさに使用中の可能性が高いので
# 触らない（A-5 同時起動衝突の保険）。通常は D-C4 の多重起動抑止で他インスタンスが
# そもそも起動しないため発火しないが、抑止をすり抜けた場合の最後の砦として残す。
_PROFILE_STALE_AGE_SECONDS = 3 * 60 * 60  # 3 時間


def cleanup_old_profiles(
    keep: Optional[Path] = None,
    min_age_seconds: float = _PROFILE_STALE_AGE_SECONDS,
) -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。

    keep を渡すと、そのフォルダは掃除対象から除外する（再利用する永続
    プロファイル [F-C1] を誤って消さないための安全弁）。永続プロファイルは
    通常 edge-debug-* とは別名・別置き場所なので glob には一致しないが、
    利用者が一時フォルダ配下に edge-debug- で始まる名前を指定した場合の保険。

    min_age_seconds より新しいフォルダは掃除しない（A-5）。多重起動が抑止を
    すり抜けた場合でも、別インスタンスが使用中の新しいプロファイルを消して
    稼働中の Edge を壊さないための保険。mtime が取れないものは安全側に倒して残す。
    """
    keep_resolved = keep.resolve() if keep is not None else None
    now = datetime.now().timestamp()
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            if keep_resolved is not None and d.resolve() == keep_resolved:
                continue
            try:
                age = now - d.stat().st_mtime
            except OSError:
                # mtime が取れない（消えた/権限）なら触らない（安全側）。
                continue
            if age < min_age_seconds:
                continue
            shutil.rmtree(d, ignore_errors=True)


# 取得した単一起動ロックのハンドル。プロセスが生きている間、OS ロックを保持し続ける
# ために参照を残す（ガベージコレクトで閉じるとロックが外れてしまう）。プロセス終了
# 時に OS が自動解放するので、クラッシュしてもロックが残り続けることはない。
_single_instance_handle = None


def single_instance_lock_path() -> Path:
    """単一起動ロックファイルの置き場所（アプリ全体で 1 つ・利用者ごと）。

    一時フォルダは Windows では利用者ごと（%TEMP%）なので、同一利用者の
    二重起動だけを弾く。output/ と log.txt を共有するのはこの単位なので過不足ない。
    """
    return Path(tempfile.gettempdir()) / "edge-auto-capture.lock"


def acquire_single_instance_lock() -> bool:
    """アプリ全体で 1 プロセスだけ起動を許す（D-C4 多重起動抑止）。

    取得できたら True、既に他インスタンスが保持していれば False を返す。
    第三者は反応が無いと二度押しするため、2 つ目が起動して output/・log.txt・
    使い捨てプロファイル（A-5）を奪い合うのを入口で止める。

    OS のファイルロック（POSIX: flock / Windows: msvcrt）を使う。プロセスが
    終了すると OS が自動でロックを外すので、クラッシュ後に残ったロックファイルが
    次回起動を妨げることはない。取得したハンドルはモジュール変数に保持し、
    プロセス寿命の間は閉じない。
    """
    global _single_instance_handle
    if _single_instance_handle is not None:
        return True  # 二重取得はしない（既に自分が保持）。
    try:
        handle = open(single_instance_lock_path(), "w")
    except OSError:
        # ロックファイルすら作れない環境では、多重起動抑止を諦めて起動を通す
        # （抑止は best-effort。ここで止めると正常起動まで塞いでしまう）。
        return True
    if not _try_lock(handle):
        handle.close()
        return False
    _single_instance_handle = handle
    return True


def _try_lock(handle) -> bool:
    """開いたファイルに OS の排他ロックを非ブロッキングで掛ける。掛けられたら True。"""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True
