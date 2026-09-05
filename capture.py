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
import csv
import re
import weakref
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

import badge
from config import Config
from infra import iso_timestamp, log, ms3
from lineage import group_folder_name, group_subdir

# 索引 CSV のファイル名と見出し。撮影ごとに 1 行追記して「いつ・何を撮ったか」を一覧にする（F-A1）。
INDEX_CSV_NAME = "index.csv"
INDEX_CSV_HEADER = ["時刻", "URL", "タイトル", "ファイル名接頭辞", "撮影契機", "セレクタ", "成否"]

# 撮影契機（CaptureRequest.trigger）の内部値 → 索引 CSV に書く日本語表記。
# 内部は経路を跨いでも壊れにくい短い英字（"manual"/"url"/"spa"）、CSV は人が読む列なので日本語。
_TRIGGER_LABELS = {"manual": "手動", "url": "URL変化", "spa": "SPA変化"}


def trigger_label(trigger: str) -> str:
    """撮影契機の内部値を索引 CSV 用の日本語表記へ。未知・未設定はそのまま（空なら空）返す。"""
    return _TRIGGER_LABELS.get(trigger, trigger)

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80


def now_stamp() -> str:
    """現在時刻を「YYYY-MM-DD_HH-MM-SS-mmm」（ミリ秒まで）で返す。

    保存ファイル名の接頭辞に使う（人が時系列で見分けやすいよう区切り付き）。
    ミリ秒 3 桁の切り出しは infra.ms3 が持つ（lineage.group_stamp と共通の 1 点。#56）。
    書式そのものは共通化しない（あちらは `lineage-<id>` のトークンで区切り無し）。
    """
    now = datetime.now()
    return f"{now:%Y-%m-%d_%H-%M-%S}-{ms3(now)}"


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


@dataclass
class CaptureRequest:
    """撮影 1 回分の要求。spawn→_pending→_worker→_capture を貫通する 1 オブジェクト。

    以前は `(url, config, selector, group_id)` の位置引数タプルが 4 経路を貫通していた
    （順序に依存し、要素を 1 個足すたびに 4 箇所の分解を直す必要があった）。1 オブジェクトへ
    集約したことで、以後の機能（F-A1 索引 CSV の「撮影契機」など）は「フィールドを 1 個
    足す」だけで全経路へ伝わる。trigger はその最初の実例（フェーズ 2）。

    page は撮影対象ページ。_pending / _workers のキーでもあるが、要求そのものにも保持して
    「1 要求＝1 オブジェクト」で完結させる。selector は _part.txt 抜き出しの対象（実行時の
    バー入力値）。group_id は保存先サブフォルダ（lineage-<id>）と保存ログの系譜表記に使う。
    trigger は撮影契機（"manual"=今すぐ1枚 / "url"=URL変化・記録開始時 / "spa"=SPA変化）で、
    投入元 3 経路から _capture の索引 CSV まで貫通させる（F-A1）。既定は空（未指定）。
    """

    page: Page
    url: str
    config: Config
    selector: str = ""
    group_id: str = ""
    trigger: str = ""


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
        # 撮影 1 回が終わるたびに成否（done 有無）を通知するコールバック（F-D3）。
        # 監視セッション（CaptureSession）が撮影カウンタの本体を持つため、ここで結果だけを渡す。
        # 既定は None（撮影実行器を単体で使うテストや、通知が要らない場面では何もしない）。
        self.on_result: Optional[Callable[[bool], Awaitable[None]]] = None

        # Python→ページのヘルパ（captureStart/captureEnd/bodyText）を収める window プロパティ名（E-3）。
        # 監視セッション（CaptureSession）が起動ごとのランダム名を生成し、ここへ配る。空（既定）は
        # 未公開＝呼び出しは no-op（撮影実行器を単体で使うテストや、ヘルパを呼ばない場面向け）。
        self.ns: str = ""

        # 実行中の worker タスクへの強参照を保持する集合。
        # これが無いとイベントループはタスクを弱参照でしか持たず、
        # 実行途中で GC されて消える恐れがある（例外も握り潰される）。
        self._tasks: set[asyncio.Task] = set()

        # ページごとの「最新の保留要求」(CaptureRequest)。新しい要求で上書きするのが
        # coalesce の本体。撮影中に何度要求が来ても、次に走るのは最後の1件だけ。
        # ページが閉じたらエントリは自動で消えるよう WeakKeyDictionary を使う。
        self._pending: weakref.WeakKeyDictionary[Page, CaptureRequest] = (
            weakref.WeakKeyDictionary()
        )

        # ページごとに走っている worker タスク。存在すれば新規起動せず、既存 worker が
        # 現在の撮影を終えたあと _pending を拾う。1 ページ＝1 worker なので同ページの
        # 撮影は完全に直列化される（＝以前の page 単位ロックは不要になった）。
        self._workers: weakref.WeakKeyDictionary[Page, asyncio.Task] = (
            weakref.WeakKeyDictionary()
        )

    def spawn(self, req: CaptureRequest) -> None:
        """撮影要求（CaptureRequest）を投入する（ページごとに合流。B-3）。

        最新要求で _pending を上書きし、そのページの worker が居なければ起動する。
        撮影の合図（バー退避→撮影→シャッターフラッシュ＋復帰）は _capture() が保存処理全体を
        くるんで行うので、ここでは要求の登録と worker 起動だけを担う。
        req.selector は _part.txt 抜き出しの対象（実行時のバー入力値）を _capture() へ渡す。
        req.group_id は保存先サブフォルダ（lineage-<id>）と保存ログの系譜表記に使う識別子。
        id は系譜を作った時刻（ミリ秒まで）。空文字なら未採番として output_dir 直下へ保存する。

        この関数は同期で、内部に await が無い＝不可分に実行される。worker の終了
        シーケンス（_worker 参照）も不可分なので、両者は「前か後」でしか噛み合わず、
        要求が宙に浮く取りこぼしは起きない。
        """
        page = req.page
        self._pending[page] = req
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
            await self._capture(req)

    async def _capture(self, req: CaptureRequest) -> None:
        """撮影 1 回分の司会。ts 確定 → load 待ち → title 確定 → save_dir 用意 →
        各 _save_* 呼び出し → [saved]/[保存できず] ログ、の順に進める。

        個々の保存（png / txt / part）は _save_screenshot / _save_text / _save_part に
        委ねる。F-A2（_part.png）・F-A3（HTML）・F-A1（索引 CSV）はここへ保存物を足すため、
        「保存物 1 種＝メソッド 1 本」の粒度に分けてある。保存順・ファイル名・ログ文言は不変。
        """
        # req.selector は「一部抜き出し(_part.txt)」の対象 CSS セレクタ。操作バーの入力欄で
        # 実行時に変えられるため、config 固定値ではなく呼び出し時の値を使う
        #（初期値は config.target_selector）。空なら _part.txt はスキップ。
        # ファイル名は「日時（ミリ秒まで）_ページタイトル」。ミリ秒付き日時で一意性と
        # 時系列順を保証し、末尾のタイトルは人がページを見分けるための情報。
        # ts は await より前に確定させる。タイトルはページ読み込み後に確定させる。
        # ローカルへ展開するのは多用する page / url だけに留め、他は req.* のまま参照する
        # （CaptureRequest にフィールドを足したときに触る行を増やさないため。#55）。
        page = req.page
        url = req.url
        ts = now_stamp()                                     # 例: 2026-08-11_14-30-25-123
        # 索引 CSV 用の撮影時刻。ファイル名（ts）とは別に、オフセット付き ISO で撮影開始時刻を
        # 押さえる（F-A4）。後から遡って直せない情報なので、await より前のこの時点で確定させる。
        captured_at = iso_timestamp()                        # 例: 2026-08-11T14:30:25.123+09:00

        # 読み込み完了を待つ（タイムアウトしても続行）
        with _step("load", url):
            await page.wait_for_load_state("load", timeout=req.config.load_timeout)
        # 描画が落ち着くまで待つ（settle_delay）。ただし SPA 経由は、ページ側 badge.js が
        # SPA_SETTLE_MS のデバウンスで既に「変化が止まってから」通知している。ここで再び
        # settle_delay を待つと二重待ちになり体感が遅れるだけなので省く（B-4, #18）。
        # URL遷移/手動は load 直後にまだ描画が動きうるので従来どおり待つ。
        if req.trigger != "spa":
            await asyncio.sleep(req.config.settle_delay)

        # タイトルを取得してファイル名の識別名を確定（失敗しても URL 由来の名前で代替）。
        title = ""
        with _step("title", url):
            title = await page.title()
        stem = f"{ts}_{page_label(title, url)}"              # 3ファイルで同じ接頭辞を共有

        # 保存先は系譜（lineage）ごとのサブフォルダ（output_dir/lineage-<id>）。未採番なら直下。
        # 3ファイルとも同じフォルダへ。フォルダが無ければ作る（失敗しても各 _step が握って skip）。
        save_dir = group_subdir(req.config.output_dir, req.group_id)
        save_dir.mkdir(parents=True, exist_ok=True)

        # 実際に保存できたステップの記録（A-3）。png / txt / part だけを積み、
        # 全滅時に [saved] と嘘のログを残さないための判定材料にする。
        # done は各 _save_* へ渡し、成功したステップだけが自分の tag を積む。
        done: list[str] = []

        # 撮影の合図つきで 3 種を保存する。バーを退避し切ってからスクショを撮り（画像に写し込ま
        # ない）、全種の保存後に成否（done 有無）に応じた色でシャッターフラッシュ＋バー復帰。
        # 退避は png のためだが、txt/txt(part) は本文取得側でバーを除外するので退避したままでも
        # 支障はなく、フラッシュ色を _capture 全体の成否に一致させるため復帰は最後にまとめて行う。
        # captureEnd は失敗時も戻すため finally で必ず呼ぶ（フラッシュ色は done 有無で決める）。
        eval_timeout = req.config.eval_timeout_sec               # ミリ秒 → 秒の換算は Config 側（E-6）
        await try_eval(page, badge.capture_start_call(self.ns), eval_timeout)
        try:
            # 1) フルページ スクリーンショット
            await self._save_screenshot(page, save_dir, stem, url, done)
            # 2) ページ全文テキスト
            await self._save_text(page, save_dir, stem, url, req.config, done)
            # 3) 一部抜き出し（セレクタ設定時のみ）
            if req.selector:
                await self._save_part(page, save_dir, stem, url, req.selector, done)
        finally:
            await try_eval(page, badge.capture_end_call(self.ns, bool(done)), eval_timeout)

        # 1つでも保存できたら [saved]（何を保存したか併記）。全滅なら正直に「保存できず」。
        # group_id が採番済みなら、どの系譜（lineage）の保存かも併記する（＝保存先フォルダ名）。
        who = f"{group_folder_name(req.group_id)} " if req.group_id else ""
        if done:
            log(f"[saved] {who}{stem}.*  ({','.join(done)})  <- {url}")
        else:
            log(f"[保存できず] {who}{stem}  <- {url}")

        # 撮影ごとに索引 CSV へ 1 行追記（F-A1）。成否は done（実際に保存できたステップ）で決める。
        self._append_index(req, captured_at, title, stem, done)

        # 撮影 1 回分の成否を監視セッションへ通知する（F-D3。撮影カウンタ／失敗の把握に使う）。
        # 通知先が未設定（単体テスト等）や通知自体が失敗しても、撮影本体は成立しているので握る。
        if self.on_result is not None:
            try:
                await self.on_result(bool(done))
            except Exception as e:
                log(f"[skip result] {url}  ({e})")

    def _append_index(
        self,
        req: CaptureRequest,
        captured_at: str,
        title: str,
        stem: str,
        done: list[str],
    ) -> None:
        """撮影 1 回分を索引 CSV（output_dir/index.csv）へ 1 行追記する（F-A1）。

        系譜ごとのサブフォルダではなく output_dir 直下に置き、全系譜の撮影を 1 本の索引にまとめる
        （log.txt と同じ粒度）。列は時刻/URL/タイトル/ファイル名接頭辞/撮影契機/セレクタ/成否。

        撮影要求そのもの（req）を受け取り、config / url / trigger / selector はそこから取る（#55）。
        以前は 8 個の位置引数で、隣接する同型（str）の trigger と selector を取り違えても型検査で
        止まらなかった。列を足すたびに並びを直す作業も CaptureRequest へフィールドを足すだけで済む。
        引数に残る captured_at / title / stem は撮影中に確定する値で、req には載らない。

        文字化け対策（地雷）: Excel で開く前提なので BOM 付き（utf-8-sig）で書く。ただし追記のたびに
        utf-8-sig で開くと毎回 BOM を書き足して行頭へ紛れ込むため、BOM は新規作成時の 1 度だけにし、
        既存への追記は utf-8 で開く。csv.writer に任せて 値中のカンマ/改行/引用符を正しく退避する。
        newline="" は csv が改行を二重化しないための定石（Windows でも空行が入らない）。
        撮影本体は成立しているので、索引の書き込み失敗はログに残すだけで握り、撮影を巻き込まない。
        """
        path = req.config.output_dir / INDEX_CSV_NAME
        row = [
            captured_at, req.url, title, stem,
            trigger_label(req.trigger), req.selector, "成功" if done else "失敗",
        ]
        try:
            new_file = not path.exists()
            encoding = "utf-8-sig" if new_file else "utf-8"
            with path.open("a", encoding=encoding, newline="") as f:
                writer = csv.writer(f)
                if new_file:
                    writer.writerow(INDEX_CSV_HEADER)
                writer.writerow(row)
        except Exception as e:
            log(f"[skip index] {req.url}  ({e})")

    async def _save_screenshot(
        self, page: Page, save_dir: Path, stem: str, url: str, done: list[str]
    ) -> None:
        """フルページ スクリーンショット（png）を保存する。

        バーの退避（撮影直前）と復帰＋シャッターフラッシュ（撮影直後）は、呼び出し元の _capture()
        が 3 種の保存全体をくるむ形で受け持つ（フラッシュ色を _capture 全体の成否に合わせるため）。
        ここでは退避済み前提でスクショだけ撮る。同一ページの撮影は worker が1件ずつ直列化するので
        （_worker 参照）、退避中に別撮影が割り込んで操作バーが写り込むことはない。
        """
        with _step("png", url, done):
            await page.screenshot(
                path=str(save_dir / f"{stem}.png"), full_page=True
            )

    async def _save_text(
        self, page: Page, save_dir: Path, stem: str, url: str, config: Config, done: list[str]
    ) -> None:
        """ページ全文テキスト（txt）を保存する（操作バーは除外して取得）。

        evaluate はタイムアウト引数を持たず set_default_timeout も効かないため、
        asyncio.wait_for で打ち切る（E-6）。戻らないと _step の外＝worker が止まる。
        打ち切りの TimeoutError は _step が握って [skip txt] を出し、worker は次へ進む。
        """
        with _step("txt", url, done):
            text = await asyncio.wait_for(
                page.evaluate(badge.body_text_call(self.ns)), timeout=config.eval_timeout_sec
            )
            (save_dir / f"{stem}.txt").write_text(
                f"URL: {url}\n\n{text}", encoding="utf-8"
            )

    async def _save_part(
        self, page: Page, save_dir: Path, stem: str, url: str, selector: str, done: list[str]
    ) -> None:
        """セレクタで指定した一部だけを抜き出したテキスト（_part.txt）を保存する。"""
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
