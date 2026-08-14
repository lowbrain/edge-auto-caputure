"""ページ操作と 1 ページ分の保存処理。

- ページ側 JS を実行する小ヘルパ（try_eval）
- ファイル名スラッグの生成（safe_name / page_label）
- 1 ページ分の保存（png / txt / _part.txt）を担う撮影実行器（CaptureRunner）

撮影の実行時状態（実行中タスク・ページ単位ワーカー・保留要求）は、以前モジュール
グローバルだったが、CaptureRunner のインスタンスが own する（1 監視セッションに 1 個。
状態の所在を明確にする）。同一ページの撮影はワーカーで直列化し、進行中に来た要求は
「保留1件・最新で置き換え」に合流させる（B-3: キュー無制限の防止）。
ページ側 JS の呼び出し式・文言は badge モジュールに集約してあり、ここから参照する。
"""

import asyncio
import re
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

import badge
from config import Config
from infra import log

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80


def now_stamp() -> str:
    """現在時刻を「YYYY-MM-DD_HH-MM-SS-mmm」（ミリ秒まで）で返す。

    保存ファイル名の接頭辞に使う（人が時系列で見分けやすいよう区切り付き）。
    """
    now = datetime.now()
    return f"{now:%Y-%m-%d_%H-%M-%S}-{now.strftime('%f')[:3]}"


def group_stamp() -> str:
    """系譜（lineage）の id を「YYYYMMDDHHMMSSmmm」（ミリ秒まで・区切りなし）で返す。

    系譜を新たに作った時刻をそのまま id にする。区切り記号を入れないので `lineage-<id>` の
    <id> 部分は連続した数字になる（例: 20260814102028731）。
    """
    now = datetime.now()
    return f"{now:%Y%m%d%H%M%S}{now.strftime('%f')[:3]}"


def group_folder_name(group_id: str) -> str:
    """系譜（lineage）の表示名を返す（フォルダ名とログ表記で共用）。

    id は系譜を新たに作った時刻（ミリ秒まで・区切りなし）。`lineage-<id>` の形にして、
    フォルダ名とログのトークンを一致させ、ログから保存フォルダをそのまま辿れるようにする。
    """
    return f"lineage-{group_id}"


def group_subdir(output_dir: Path, group_id: str) -> Path:
    """系譜（lineage）ごとの保存先サブフォルダを返す。

    保存物を系譜ごとにまとめるための共通規約（`output_dir/lineage-<id>`）。edge_auto_capture の
    ダウンロード退避先もこれに揃える。group_id が空（未採番）なら output_dir 直下を返す。
    """
    return output_dir / group_folder_name(group_id) if group_id else output_dir


async def try_eval(page: Page, js: str, timeout: Optional[float] = None) -> None:
    """ページ側 JS を実行。失敗しても無視する（操作バーの表示/非表示など副次処理用）。

    timeout（秒）を渡すと、その時間内に返らなければ諦める（E-6: ページのメインスレッドが
    詰まって evaluate が戻らないと worker が永久に止まるのを防ぐ）。打ち切りは TimeoutError
    になるが、この関数は元々あらゆる例外を握って無視するので呼び出し側は続行できる。
    """
    try:
        if timeout is None:
            await page.evaluate(js)
        else:
            await asyncio.wait_for(page.evaluate(js), timeout=timeout)
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
def _step(tag: str, url: str, done=None):
    """保存処理 1 ステップ分の共通ラッパ。

    例外が出ても [skip <tag>] を表示して握り、他ステップの続行を妨げない。
    （png / txt / part の 3 ステップで同じ try/except を書かないための共通化）

    done を渡すと、例外なく完了したときだけ tag を追記する（A-3）。呼び出し側は
    done に積まれた tag で「実際に何を保存できたか」を判定し、全滅時に誤って
    [saved] と記録しないようにする。保存物ではない load / title には渡さない。
    """
    try:
        yield
        if done is not None:
            done.append(tag)
    except Exception as e:
        log(f"[skip {tag}] {url}  ({e})")


class CaptureRunner:
    """1 監視セッション分の撮影実行器。

    撮影のバックグラウンドタスクを own する。以前はモジュールグローバル（_tasks 等）
    だったものをここへ集約し、状態がセッションに閉じる（テスト時の持ち越しや、
    複数セッション時の共有事故を避ける）。

    撮影は **ページごとに1つのワーカー**で直列化し、進行中に来た新しい要求は
    「保留1件・最新で置き換え」に合流させる（B-3: キュー無制限の防止）。
    更新の激しいダッシュボードで SPA 検知 ON にしても、あるページのキューは
    「実行中1件＋保留1件」で構造的に頭打ちになり、フルページ PNG がディスクを
    食い潰さない。中間フレームは捨てるが、要求後の状態は必ず撮れるので撮り逃さない。
    """

    def __init__(self) -> None:
        # 実行中の worker タスクへの強参照を保持する集合。
        # これが無いとイベントループはタスクを弱参照でしか持たず、
        # 実行途中で GC されて消える恐れがある（例外も握り潰される）。
        self._tasks: set[asyncio.Task] = set()

        # ページごとの「最新の保留要求」(url, config, selector)。新しい要求で上書き
        # するのが coalesce の本体。撮影中に何度要求が来ても、次に走るのは最後の1件だけ。
        # ページが閉じたらエントリは自動で消えるよう WeakKeyDictionary を使う。
        self._pending: weakref.WeakKeyDictionary[Page, tuple[str, Config, str]] = (
            weakref.WeakKeyDictionary()
        )

        # ページごとに走っている worker タスク。存在すれば新規起動せず、既存 worker が
        # 現在の撮影を終えたあと _pending を拾う。1 ページ＝1 worker なので同ページの
        # 撮影は完全に直列化される（＝以前の page 単位ロックは不要になった）。
        self._workers: weakref.WeakKeyDictionary[Page, asyncio.Task] = (
            weakref.WeakKeyDictionary()
        )

    def spawn(
        self, page: Page, url: str, config: Config, selector: str = "", group_id: str = ""
    ) -> None:
        """撮影要求を投入する（ページごとに合流。B-3）。

        最新要求で _pending を上書きし、そのページの worker が居なければ起動する。
        撮影の合図（バー退避→撮影→シャッターフラッシュ＋復帰）は _capture() のスクショ処理が
        内部で行うので、ここでは要求の登録と worker 起動だけを担う。
        selector は _part.txt 抜き出しの対象（実行時のバー入力値）を _capture() へ渡す。
        group_id は保存先サブフォルダ（lineage-<id>）と保存ログの系譜表記に使う識別子。
        id は系譜を作った時刻（ミリ秒まで）。空文字なら未採番として output_dir 直下へ保存する。

        この関数は同期で、内部に await が無い＝不可分に実行される。worker の終了
        シーケンス（_worker 参照）も不可分なので、両者は「前か後」でしか噛み合わず、
        要求が宙に浮く取りこぼしは起きない。
        """
        self._pending[page] = (url, config, selector, group_id)
        if page not in self._workers:
            task = asyncio.create_task(self._worker(page))
            self._workers[page] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _worker(self, page: Page) -> None:
        """1 ページ分の撮影ワーカー。_pending が尽きるまで最新要求を1件ずつ撮る。

        撮影中に spawn が _pending を何度上書きしても、次のループで拾うのは最後の1件
        だけ（＝合流）。_pending が空になったら、その場（await を挟まず）で _workers から
        自分を外して終了する。この「pop が None → _workers から除去 → return」の区間に
        await が無いことが、spawn 側との競合を防ぐ肝（下の説明どおり不可分に実行される）。
        """
        while True:
            req = self._pending.pop(page, None)
            if req is None:
                # ここから return まで await を挟まない。この間に spawn は割り込めないので、
                # 「空を見て終了しようとした矢先に新要求が来て取りこぼす」競合は起きない。
                self._workers.pop(page, None)
                return
            url, config, selector, group_id = req
            await self._capture(page, url, config, selector, group_id)

    async def _capture(
        self, page: Page, url: str, config: Config, selector: str = "", group_id: str = ""
    ) -> None:
        # selector は「一部抜き出し(_part.txt)」の対象 CSS セレクタ。操作バーの入力欄で
        # 実行時に変えられるため、config 固定値ではなく呼び出し時の値を使う
        #（初期値は config.target_selector）。空なら _part.txt はスキップ。
        # ファイル名は「日時（ミリ秒まで）_ページタイトル」。ミリ秒付き日時で一意性と
        # 時系列順を保証し、末尾のタイトルは人がページを見分けるための情報。
        # ts は await より前に確定させる。タイトルはページ読み込み後に確定させる。
        ts = now_stamp()                                     # 例: 2026-08-11_14-30-25-123

        # 読み込み完了を待つ（タイムアウトしても続行）
        with _step("load", url):
            await page.wait_for_load_state("load", timeout=config.load_timeout)
        await asyncio.sleep(config.settle_delay)

        # タイトルを取得してファイル名の識別名を確定（失敗しても URL 由来の名前で代替）。
        title = ""
        with _step("title", url):
            title = await page.title()
        stem = f"{ts}_{page_label(title, url)}"              # 3ファイルで同じ接頭辞を共有

        # 保存先は系譜（lineage）ごとのサブフォルダ（output_dir/lineage-<id>）。未採番なら直下。
        # 3ファイルとも同じフォルダへ。フォルダが無ければ作る（失敗しても各 _step が握って skip）。
        save_dir = group_subdir(config.output_dir, group_id)
        save_dir.mkdir(parents=True, exist_ok=True)

        # 実際に保存できたステップの記録（A-3）。png / txt / part だけを積み、
        # 全滅時に [saved] と嘘のログを残さないための判定材料にする。
        done: list[str] = []

        # 1) フルページ スクリーンショット
        #    撮影の合図つき: バーを上へ退避し切ってから撮り（保存画像へ写し込まない）、撮影後に
        #    シャッターフラッシュ＋バー復帰。captureEnd は失敗時も戻すため finally で必ず呼ぶ。
        #    同一ページの撮影は worker が1件ずつ直列化するので（_worker 参照）、退避中に別撮影が
        #    割り込んで操作バーが写り込むことはない。別ページ同士は別 worker なので並行できる。
        eval_timeout = config.eval_timeout / 1000               # ミリ秒 → 秒（E-6）
        with _step("png", url, done):
            try:
                # 退避し切るまで待つ。ページが固まって戻らない場合は eval_timeout で打ち切り、
                # 退避の合図に永久に張り付いて worker を止めない（E-6）。
                await try_eval(page, badge.CAPTURE_START_CALL, eval_timeout)
                await page.screenshot(
                    path=str(save_dir / f"{stem}.png"), full_page=True
                )
            finally:
                # フラッシュ＋復帰（必ず実行）。ここも同じく打ち切り付きで待つ。
                await try_eval(page, badge.CAPTURE_END_CALL, eval_timeout)

        # 2) ページ全文テキスト（操作バーは除外して取得）
        #    evaluate はタイムアウト引数を持たず set_default_timeout も効かないため、
        #    asyncio.wait_for で打ち切る（E-6）。戻らないと _step の外＝worker が止まる。
        #    打ち切りの TimeoutError は _step が握って [skip txt] を出し、worker は次へ進む。
        with _step("txt", url, done):
            text = await asyncio.wait_for(
                page.evaluate(badge.BODY_TEXT_CALL), timeout=config.eval_timeout / 1000
            )
            (save_dir / f"{stem}.txt").write_text(
                f"URL: {url}\n\n{text}", encoding="utf-8"
            )

        # 3) 一部抜き出し（セレクタ設定時のみ）
        if selector:
            with _step("part", url, done):
                # 操作バーはシャドウ内にあり locator（querySelector 相当）は境界を越えない。
                # 広いセレクタ（div / body / * など）でもバーの文言は拾わないので隠す必要はない。
                parts = await page.locator(selector).all_inner_texts()
                # 空文字（該当なし要素）は落とす。
                parts = [p for p in parts if p.strip()]
                body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
                (save_dir / f"{stem}_part.txt").write_text(
                    f"URL: {url}\nSELECTOR: {selector}\n\n{body}",
                    encoding="utf-8",
                )

        # 1つでも保存できたら [saved]（何を保存したか併記）。全滅なら正直に「保存できず」。
        # group_id が採番済みなら、どの系譜（lineage）の保存かも併記する（＝保存先フォルダ名）。
        who = f"{group_folder_name(group_id)} " if group_id else ""
        if done:
            log(f"[saved] {who}{stem}.*  ({','.join(done)})  <- {url}")
        else:
            log(f"[保存できず] {who}{stem}  <- {url}")
