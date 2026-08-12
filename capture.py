"""ページ操作と 1 ページ分の保存処理。

- ページ側 JS を実行する小ヘルパ（try_eval）
- ファイル名スラッグの生成（safe_name / page_label）
- 1 ページ分の保存（png / txt / _part.txt）を担う撮影実行器（CaptureRunner）

撮影の実行時状態（実行中タスク・ページ単位ロック）は、以前モジュールグローバルだったが、
CaptureRunner のインスタンスが own する（1 監視セッションに 1 個。状態の所在を明確にする）。
ページ側 JS の呼び出し式・文言は badge モジュールに集約してあり、ここから参照する。
"""

import asyncio
import re
import weakref
from contextlib import contextmanager
from datetime import datetime

from playwright.async_api import Page

import badge
from config import Config
from infra import log

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80


async def try_eval(page: Page, js: str) -> None:
    """ページ側 JS を実行。失敗しても無視する（操作バーの表示/非表示など副次処理用）。"""
    try:
        await page.evaluate(js)
    except Exception:
        pass


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


class CaptureRunner:
    """1 監視セッション分の撮影実行器。

    撮影のバックグラウンドタスクと、ページ単位の撮影ロックを own する。以前は
    モジュールグローバル（_tasks / _page_locks）だったものをここへ集約し、状態が
    セッションに閉じる（テスト時の持ち越しや、複数セッション時の共有事故を避ける）。
    """

    def __init__(self) -> None:
        # 実行中の capture タスクへの強参照を保持する集合。
        # これが無いとイベントループはタスクを弱参照でしか持たず、
        # 実行途中で GC されて消える恐れがある（例外も握り潰される）。
        self._tasks: set[asyncio.Task] = set()

        # ページごとの撮影を直列化するためのロック（page -> Lock）。
        # 同じページで撮影が重なると（例: 「今すぐ1枚」と自動保存がほぼ同時、リダイレクト連鎖）、
        # 一方の captureEnd がバーを復帰させた直後にもう一方が screenshot 中、という並びが起き得て、
        # 退避したはずの操作バーが画像へ写り込む。撮影のクリティカル区間（バー退避→撮影→復帰）を
        # このロックで page 単位に直列化して防ぐ。別ページ同士は別ロックなので並行できる。
        # ページが閉じたらエントリは自動で消えるよう WeakKeyDictionary を使う。
        self._page_locks: weakref.WeakKeyDictionary[Page, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    def _page_lock(self, page: Page) -> asyncio.Lock:
        """そのページ用の撮影ロックを返す（無ければ作る）。"""
        lock = self._page_locks.get(page)
        if lock is None:
            lock = asyncio.Lock()
            self._page_locks[page] = lock
        return lock

    def spawn(self, page: Page, url: str, config: Config, selector: str = "") -> None:
        """_capture() をバックグラウンドタスクとして起動し、参照を保持する。

        撮影の合図（バー退避→撮影→シャッターフラッシュ＋復帰）は _capture() のスクショ処理が
        内部で行うので、ここでは起動と参照保持だけを担う。
        selector は _part.txt 抜き出しの対象（実行時のバー入力値）を _capture() へ渡す。
        タスクを _tasks に入れて GC を防ぎ、完了時に取り除く。
        """
        task = asyncio.create_task(self._capture(page, url, config, selector))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, page: Page, url: str, config: Config, selector: str = "") -> None:
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
        #    撮影の合図つき: バーを上へ退避し切ってから撮り（保存画像へ写し込まない）、撮影後に
        #    シャッターフラッシュ＋バー復帰。captureEnd は失敗時も戻すため finally で必ず呼ぶ。
        #    同一ページでの撮影重なりによる写り込みを防ぐため、この区間を page 単位のロックで直列化
        #    する（_page_lock 参照。別ページ同士は別ロックなので並行できる）。
        async with self._page_lock(page):
            with _step("png", url):
                try:
                    await try_eval(page, badge.CAPTURE_START_CALL)  # 退避し切るまで待つ
                    await page.screenshot(
                        path=str(config.output_dir / f"{stem}.png"), full_page=True
                    )
                finally:
                    await try_eval(page, badge.CAPTURE_END_CALL)     # フラッシュ＋復帰（必ず実行）

        # 2) ページ全文テキスト（操作バーは除外して取得）
        with _step("txt", url):
            text = await page.evaluate(badge.BODY_TEXT_CALL)
            (config.output_dir / f"{stem}.txt").write_text(
                f"URL: {url}\n\n{text}", encoding="utf-8"
            )

        # 3) 一部抜き出し（セレクタ設定時のみ）
        if selector:
            with _step("part", url):
                # 操作バーはシャドウ内にあり locator（querySelector 相当）は境界を越えない。
                # 広いセレクタ（div / body / * など）でもバーの文言は拾わないので隠す必要はない。
                parts = await page.locator(selector).all_inner_texts()
                # 空文字（該当なし要素）は落とす。
                parts = [p for p in parts if p.strip()]
                body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
                (config.output_dir / f"{stem}_part.txt").write_text(
                    f"URL: {url}\nSELECTOR: {selector}\n\n{body}",
                    encoding="utf-8",
                )

        log(f"[saved] {stem}.*  <- {url}")
