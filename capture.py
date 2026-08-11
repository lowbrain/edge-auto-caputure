"""設定・保存処理まわり（アプリの土台）。

- 基準フォルダ/ログなどの基盤ユーティリティ（BASE_DIR, log, try_eval …）
- config.ini の読み込み（Config / load_config）
- 1 ページ分の保存処理（capture / spawn_capture）と後始末（cleanup_old_profiles）

ページ側 JS の呼び出し式・文言は badge モジュールに集約してあり、ここから参照する。
"""

import asyncio
import configparser
import re
import shutil
import sys
import tempfile
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Tuple

from playwright.async_api import Page

import badge


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

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80

# 実行ログの出力先。コンソール無し（windowed exe）で実行しても後から動作を追える
# ようにする。既定は基準フォルダだが、設定読み込み後に PNG などと同じ保存先
# （output_dir）へ切り替える（set_log_dir）。設定を読む前の初期ログはここへ出る。
LOG_PATH = BASE_DIR / "log.txt"


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
    """致命的エラーを Windows のメッセージボックスで通知する（失敗しても無視）。

    コンソールを持たない windowed exe では print が見えないため、
    起動失敗などはダイアログで利用者に伝える。
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def notify_fatal(msg: str) -> None:
    """致命的メッセージをログとダイアログの両方へ出す（終了処理は呼び出し側）。"""
    log(msg)
    _message_box(msg)


async def try_eval(page: Page, js: str) -> None:
    """ページ側 JS を実行。失敗しても無視する（バッジの表示/非表示など副次処理用）。"""
    try:
        await page.evaluate(js)
    except Exception:
        pass


# 実行中の capture タスクへの強参照を保持する集合。
# これが無いとイベントループはタスクを弱参照でしか持たず、
# 実行途中で GC されて消える恐れがある（例外も握り潰される）。
_tasks: "set[asyncio.Task]" = set()

# ページごとの撮影を直列化するためのロック（page -> Lock）。
# 同じページで撮影が重なると（例: 「今すぐ1枚」と自動保存がほぼ同時、リダイレクト連鎖）、
# 一方の captureEnd がバーを復帰させた直後にもう一方が screenshot 中、という並びが起き得て、
# 退避したはずの操作バーが画像へ写り込む。撮影のクリティカル区間（バー退避→撮影→復帰）を
# このロックで page 単位に直列化して防ぐ。別ページ同士は別ロックなので並行できる。
# ページが閉じたらエントリは自動で消えるよう WeakKeyDictionary を使う。
_page_locks: "weakref.WeakKeyDictionary[Page, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _page_lock(page: Page) -> asyncio.Lock:
    """そのページ用の撮影ロックを返す（無ければ作る）。"""
    lock = _page_locks.get(page)
    if lock is None:
        lock = asyncio.Lock()
        _page_locks[page] = lock
    return lock


@dataclass
class Config:
    """config.ini から読み込む設定値一式。

    各フィールドの初期値が「既定値」を兼ねる: config.ini にその項目行が
    無い場合、load_config() はここの値へフォールバックする。
    """

    start_url: str = "about:blank"          # Edge 起動時に最初に開くページ
    edge_path: str = ""                     # Edge 実行ファイルのパス（空なら channel="msedge"）
    output_dir: Path = Path("output")       # 保存先フォルダ（png / txt / log.txt もここ）
    poll_interval: float = 1.0              # URL変化を確認する間隔（秒）
    settle_delay: float = 0.8               # 変化検知後、描画が落ち着くまで待つ秒数
    load_timeout: int = 5000                # ページ読み込み待ちの上限（ミリ秒）
    skip_urls: Tuple[str, ...] = ("about:blank", "")   # 撮らないURL
    target_selector: str = ""               # 一部抜き出しの CSS セレクタ（空ならスキップ）
    start_recording: bool = False           # 起動直後に記録を開始するか（False=待機状態で起動）


def load_config() -> Config:
    """config.ini を読み込み、Config を返す。

    ファイルが無い / 値が不正な場合はメッセージを表示して終了する。

    既定値まわりの挙動（現状仕様）:
      - config.ini / [capture] セクションが無い    → メッセージ表示して終了。
      - 項目の「行そのものが無い」                 → Config の既定値を使う
        （sec.get / getfloat / getint の第2引数が既定値）。
      - 数値項目の値だけが空（例: poll_interval =）→ 変換に失敗し終了（ValueError）。
      - output_dir の値が空 → 既定値（output）へフォールバックする
        （空だと Path('.') でカレントへ保存してしまう事故を防ぐ）。
      - edge_path など他の文字列項目が空 → 空文字がそのまま入る（空が正常値）。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
      - 数値の範囲が不正（poll_interval<=0 / settle_delay<0 / load_timeout<=0）
        → メッセージ表示して終了（暴走・無意味値を防ぐ）。
    """
    if not CONFIG_PATH.exists():
        notify_fatal(
            f"設定ファイルが見つかりません: {CONFIG_PATH}\n"
            "exe と同じフォルダに config.ini を置いてください。"
        )
        sys.exit(1)

    # 各 get の第2引数は「その項目行が無い」ときのフォールバック既定値
    # （項目行はあり値だけ空、の場合は空文字/変換エラー側になる点に注意）。
    defaults = Config()
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
        sec = parser["capture"]

        # 保存先。値が空ならカレントへ落ちないよう既定へ戻す（配布先で編集ミスが起きても安全側に）。
        raw_out = sec.get("output_dir", str(defaults.output_dir)).strip()
        if not raw_out:
            log("[config] output_dir が空のため既定値を使います。")
            raw_out = str(defaults.output_dir)
        # 相対パスは基準フォルダ基準に固定（exe 隣の output\ に確実に保存する）。
        # 絶対パス指定時はそのまま使う（config.ini で任意の保存先に変更可能）。
        output_dir = Path(raw_out)
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir

        # ログも PNG などと同じ保存先へ寄せる（保存先が確定したこの時点で切り替え）。
        set_log_dir(output_dir)

        # 数値項目。範囲を検証し、不正なら理由付き ValueError（下の except で通知＆終了）。
        poll_interval = sec.getfloat("poll_interval", defaults.poll_interval)
        settle_delay = sec.getfloat("settle_delay", defaults.settle_delay)
        load_timeout = sec.getint("load_timeout", defaults.load_timeout)
        if poll_interval <= 0:
            raise ValueError(f"poll_interval は正の数にしてください（現在: {poll_interval}）")
        if settle_delay < 0:
            raise ValueError(f"settle_delay は 0 以上にしてください（現在: {settle_delay}）")
        if load_timeout <= 0:
            raise ValueError(f"load_timeout は正の整数にしてください（現在: {load_timeout}）")

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        urls = [u.strip() for u in sec.get("skip_urls", "").split(",") if u.strip()]

        return Config(
            start_url=sec.get("start_url", defaults.start_url).strip() or "about:blank",
            edge_path=sec.get("edge_path", "").strip(),
            output_dir=output_dir,
            poll_interval=poll_interval,
            settle_delay=settle_delay,
            load_timeout=load_timeout,
            skip_urls=tuple(urls) + ("",),
            target_selector=sec.get("target_selector", "").strip(),
            start_recording=sec.getboolean("start_recording", defaults.start_recording),
        )
    except (configparser.Error, KeyError, ValueError) as e:
        notify_fatal(
            f"config.ini の読み込みに失敗しました: {e}\n"
            "[capture] セクションと各項目の値を確認してください。"
        )
        sys.exit(1)


def safe_name(text: str) -> str:
    """任意の文字列をファイル名に使える形へ変換（長すぎる場合は先頭 NAME_MAX_LEN 文字）。

    使えない文字は - にまとめる。\\w は Unicode 対応なので日本語タイトルはそのまま残る。
    前後の _ / - は落とす。空白・記号のみの入力（例: "   ", "///"）は変換後に
    区切り文字だけが残るため、それらも除いて空になれば "page" へフォールバックする。
    """
    name = re.sub(r"[^\w\-]+", "-", text)[:NAME_MAX_LEN].strip("_-")
    return name or "page"


def page_label(title: str, url: str) -> str:
    """ファイル名末尾に付ける「人が読む識別名」を作る。

    ページタイトルを最優先で使う（人はタイトルでページを覚えているため）。
    タイトルが空/空白のみのときは URL から scheme と www. を落とした控えめな名前で代替する。
    """
    if title.strip():
        return safe_name(title)
    cleaned = re.sub(r"^\w+://(www\.)?", "", url)   # https:// や www. の定型頭を除去
    return safe_name(cleaned)


@contextmanager
def _step(tag: str, url: str):
    """保存処理 1 ステップ分の共通ラッパ。

    例外が出ても [skip <tag>] を表示して握り、他ステップの続行を妨げない。
    （png / txt / part の 3 ステップで同じ try/except を書かないための共通化）
    """
    try:
        yield
    except Exception as e:
        log(f"[skip {tag}] {url}  ({e})")


async def capture(page: Page, url: str, config: Config, selector: str = "") -> None:
    # selector は「一部抜き出し(_part.txt)」の対象 CSS セレクタ。操作バーの入力欄で
    # 実行時に変えられるため、config 固定値ではなく呼び出し時の値を使う
    #（初期値は config.target_selector）。空なら _part.txt はスキップ。
    # ファイル名は「日時（ミリ秒まで）_ページタイトル」。ミリ秒付き日時で一意性と
    # 時系列順を保証し、末尾のタイトルは人がページを見分けるための情報。
    # ts は await より前に確定させる。タイトルはページ読み込み後に確定させる。
    now = datetime.now()
    ms = now.strftime("%f")[:3]                          # マイクロ秒の先頭3桁＝ミリ秒
    ts = f"{now:%Y-%m-%d_%H-%M-%S}-{ms}"                 # 例: 2026-08-11_14-30-25-123

    # 読み込み完了を待つ（タイムアウトしても続行）
    with _step("load", url):
        await page.wait_for_load_state("load", timeout=config.load_timeout)
    await asyncio.sleep(config.settle_delay)

    # タイトルを取得してファイル名の識別名を確定（失敗しても URL 由来の名前で代替）。
    title = ""
    with _step("title", url):
        title = await page.title()
    stem = f"{ts}_{page_label(title, url)}"              # 3ファイルで同じ接頭辞を共有

    # 1) フルページ スクリーンショット
    #    撮影の合図つき: バーを上へ退避し切ってから撮り（保存画像へ写し込まない）、
    #    撮影後にシャッターフラッシュ＋バー復帰。captureEnd は撮影が失敗しても必ず呼ぶ
    #    （でないとバーが退避したまま戻らないため finally で実行）。
    #    同一ページで撮影が重なるとバーの退避/復帰が競合して写り込むため、page 単位の
    #    ロックでこの区間だけ直列化する（別ページ同士は別ロックなので並行できる）。
    async with _page_lock(page):
        with _step("png", url):
            try:
                await try_eval(page, badge.CAPTURE_START_CALL)  # 退避し切るまで待つ
                await page.screenshot(
                    path=str(config.output_dir / f"{stem}.png"), full_page=True
                )
            finally:
                await try_eval(page, badge.CAPTURE_END_CALL)     # フラッシュ＋復帰（必ず実行）

    # 2) ページ全文テキスト（操作パネルは除外して取得）
    with _step("txt", url):
        text = await page.evaluate(badge.BODY_TEXT_CALL)
        (config.output_dir / f"{stem}.txt").write_text(
            f"URL: {url}\n\n{text}", encoding="utf-8"
        )

    # 3) 一部抜き出し（セレクタ設定時のみ）
    if selector:
        with _step("part", url):
            # 操作パネルはシャドウ内にあり locator（querySelector 相当）は境界を越えない。
            # 広いセレクタ（div / body / * など）でもパネルの文言は拾わないので隠す必要はない。
            parts = await page.locator(selector).all_inner_texts()
            # 空文字（該当なし要素）は落とす。
            parts = [p for p in parts if p.strip()]
            body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
            (config.output_dir / f"{stem}_part.txt").write_text(
                f"URL: {url}\nSELECTOR: {selector}\n\n{body}",
                encoding="utf-8",
            )

    log(f"[saved] {stem}.*  <- {url}")


def spawn_capture(page: Page, url: str, config: Config, selector: str = "") -> None:
    """capture() をバックグラウンドタスクとして起動し、参照を保持する。

    撮影の合図（バー退避→撮影→シャッターフラッシュ＋復帰）は capture() のスクショ処理が
    内部で行うので、ここでは起動と参照保持だけを担う。
    selector は _part.txt 抜き出しの対象（実行時のバー入力値）を capture() へ渡す。
    タスクを _tasks に入れて GC を防ぎ、完了時に取り除く。
    """

    task = asyncio.create_task(capture(page, url, config, selector))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def cleanup_old_profiles() -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。
    """
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
