"""
記録ONの間、Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

撮影のタイミングは利用者が操作する。各ページ上部の操作バーで「記録開始／停止」により
記録期間を制御し、「今すぐ1枚」で今のページを1回だけ撮れる。既定は記録OFF（待機）で
起動する（config.ini の start_recording で変更可）。操作バーには記録中/待機中の表示に
加え、上記ボタン・セレクタ入力欄・「SPA検知」トグル・バーを半透明にする「透過」トグルが
並ぶ（保存する png / txt / part のいずれにも写し込まない）。

SPA（URLが変わらず中身だけ変わるページ）向けに「SPA検知」を ON にすると、記録ON中はページの
中身が変わるたびに自動保存する（同じ内容は署名比較で撮らない）。中身変化の検出はページ側
（badge.js）がイベント駆動（MutationObserver＋落ち着きのデバウンス）で行う。入力欄に CSS
セレクタを入れればその要素を監視し、空ならページ主要部（main/article、無ければ本文全体）を
自動監視する。このセレクタは _part.txt の抜き出し対象も兼ねる（初期値は config.ini の
target_selector）。

Edge の起動・監視・後始末はこのスクリプトが一括で行う（Playwright が毎回まっさらな一時
プロファイルで Edge を起動し、終了時に自動で掃除する）。

構成（役割ごとにモジュール分割）:
  - edge_auto_capture.py … 本ファイル。エントリと監視セッション（CaptureSession）。
  - badge.py / badge.js  … 各ページ上部の操作バー（ページ側 JS 一式）。
  - capture.py           … 設定読み込み・1ページ分の保存処理・基盤ユーティリティ。

事前準備:
  pip install -e .          （または pip install playwright）
  ※ インストール済みの Edge をそのまま使うため、playwright install は不要。

起動方法:
  - python edge_auto_capture.py（開発時）、または
  - ビルドした edge-auto-capture.exe をダブルクリック（配布時）
  設定は同じフォルダの config.ini で変更する（起動ページ start_url、保存先など。
  start_url が空なら about:blank）。開いた Edge で普通に閲覧すれば、記録ONの間だけ
  URL/タブの変化ごとに output\\ へ自動保存される。

停止は「Edge のウィンドウを閉じる」だけでよい（コンソール実行時は Ctrl + C も使える）。
停止時に、起動した Edge の終了と一時プロファイルの削除まで行う。動作ログは保存先
（output_dir）フォルダの log.txt に残る。
"""

import asyncio
import json
import secrets
import shutil
import tempfile
from typing import Optional

from playwright.async_api import Page, async_playwright

import badge
from capture import CaptureRunner, try_eval
from config import Config, load_config
from infra import cleanup_old_profiles, log, notify_fatal


def _url_key(url: str) -> str:
    """URL変化の「同じページか」判定に使う比較キー。フラグメント（#...）を除く。

    多くのドキュメントSPA（例: Vuetify）は scroll-spy で、スクロールに追従して URL の
    ハッシュ（#見出し）だけを書き換える。ハッシュ違いは同じドキュメントなので、#以降を
    落として「同じページ」とみなし、スクロールのたびの二重撮りを防ぐ。ハッシュルーティング
    型SPAの本当の中身変化は SPA検知（本文署名）が担うため、ここで落としても取りこぼさない。
    """
    return url.split("#", 1)[0]


def _edge_launch_kwargs(config: Config, user_data_dir: str) -> dict:
    """launch_persistent_context に渡す Edge 起動オプションを組み立てる。"""
    edge_args = [
        # まっさらなプロファイルで起動する（サインイン/同期/初回セットアップを回避）。
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=msImplicitSignin",
        # 既定で最大化して起動する。
        "--start-maximized",
    ]
    kwargs = dict(
        user_data_dir=user_data_dir,
        channel="msedge",
        headless=False,
        args=edge_args,
        # Playwright は既定で --no-sandbox を付け、Edge が黄色い警告バナーを出す。
        # サンドボックスを有効化してバナーを消す（撮影画像への映り込みも防ぐ）。
        chromium_sandbox=True,
        # 固定ビューポートのエミュレーションを外し、ウィンドウサイズにページを
        # 追従させる（--start-maximized も no_viewport でないと効かない）。
        no_viewport=True,
    )
    if config.edge_path:
        kwargs["executable_path"] = config.edge_path
    return kwargs


class CaptureSession:
    """1 回の起動ぶんの監視セッション。

    実行時状態（記録中/SPA検知/セレクタ）と、それを共有する各コールバック・監視ループを
    まとめて持つ。以前は main() 内のネスト関数群だったものをクラスへ集約し、状態の
    所在を明確にして main() を薄くする。

    SPA検知の中身変化の検出はページ側（badge.js）がイベント駆動で行い、落ち着いた変化を
    __eac_spa_changed で通知してくる。ここではその通知を受けて撮る（毎tickの署名評価は無い）。
    セレクタ未設定でも既定ルート（main/article/本文）を監視するため、spa_on は selector の
    有無に依らず ON にできる。
    """

    def __init__(self, context, config: Config) -> None:
        self.context = context
        self.config = config
        # ページ側から公開バインディング（__eac_* 群）を呼ぶときの合言葉。起動ごとにランダム
        # 生成して badge.js へ埋め込む。閲覧中サイトのスクリプトが token を知らずに記録操作・
        # 連写・セレクタ書き換えを試みても、下の各コールバックが token 不一致で無視する。
        self.token = secrets.token_hex(16)
        # 撮影の実行器（実行中タスク・ページ単位ロックを own する）。1 セッションに 1 個。
        self.runner = CaptureRunner()
        # --- 実行時状態 ---
        self.on = config.start_recording          # 記録中か（自動保存のマスタースイッチ）
        self.spa_on = False                       # SPA検知（中身変化を契機に保存）
        self.selector = config.target_selector    # 検知/抜き出しの対象 CSS セレクタ
        # --- ページごとの追跡情報 ---
        self.seen: dict[Page, str] = {}           # page -> 直近のURL

    # ---- ページ側とのやり取り ----

    async def refresh_panels(self) -> None:
        """開いている全ページの操作バーへ現在の状態（記録中/SPA検知/セレクタ）を反映する。

        新規タブ（初期は待機表示で描画）や、サイト側の再描画で作り直されたバーも、
        毎 tick これを呼ぶことで追従する。
        """
        flag = "true" if self.on else "false"
        spa_flag = "true" if self.spa_on else "false"
        sel = json.dumps(self.selector)  # 日本語/記号を含んでも安全に JS リテラル化
        for pg in list(self.context.pages):
            await try_eval(
                pg,
                f"window.__eacApplyState && window.__eacApplyState({flag}, {spa_flag}, {sel})",
            )

    # ---- expose_binding で公開するコールバック ----
    #
    # これらは全ページの window に公開されるため、閲覧中サイトのスクリプトからも呼べてしまう。
    # 各コールバックは第1引数 token を self.token と照合し、一致しない呼び出し（＝操作バー以外）
    # は黙って無視する（ログも出さない: 不一致呼び出しを連打されてもログを氾濫させないため）。
    # 引数には既定値を与え、任意個数/不正な引数で呼ばれても TypeError で落ちないようにする。

    def _authorized(self, token) -> bool:
        """操作バーからの正規の呼び出しか（合言葉が一致するか）を判定する。"""
        return isinstance(token, str) and secrets.compare_digest(token, self.token)

    def _shoot(self, pg) -> Optional[str]:
        """1ページを撮る。url 取得失敗と skip_urls を弾き、撮れば url を返す（弾けば None）。

        「url 取得 → skip 判定 → runner.spawn」の定型を1か所に集約する（各コールバックと監視
        ループで同じ並びを書かないため）。記録状態のゲートは呼び出し側の責務（ここでは見ない）。
        """
        try:
            url = pg.url
        except Exception:
            return None
        if url in self.config.skip_urls:
            return None
        self.runner.spawn(pg, url, self.config, self.selector)
        return url

    async def on_toggle(self, source, token=None) -> None:
        """「記録開始／停止」ボタン: 記録状態を反転する。

        ON にした瞬間は現在開いている全ページを即撮影し、seen を現在 URL に
        そろえる（撮り始めの体感を良くしつつ、直後のループでの二重取りも防ぐ）。
        """
        if not self._authorized(token):
            return
        self.on = not self.on
        log(f"[記録] {'開始' if self.on else '停止'}")
        await self.refresh_panels()
        if self.on:
            for pg in list(self.context.pages):
                url = self._shoot(pg)
                if url is not None:
                    self.seen[pg] = _url_key(url)

    async def on_shot(self, source, token=None) -> None:
        """「今すぐ1枚」ボタン: 記録状態に関わらず、押したページを1回だけ撮る。

        seen は触らないので自動保存の判定には影響しない（記録ON中でも同一 URL の
        「撮り直し」として別ファイルにもう1枚保存される）。撮影後はシャッター
        フラッシュで知らせる。
        """
        if not self._authorized(token):
            return
        url = self._shoot(source["page"])
        if url is not None:
            log(f"[手動] {url}")

    async def on_spa_toggle(self, source, token=None) -> None:
        """「SPA検知」ボタン: 中身の変化を契機にした自動保存を ON/OFF する。

        セレクタ未設定でも既定ルート（main/article/本文）を監視するため、常に切替可。
        署名基準の取り直し（開始直後の無駄撮り回避）はページ側（badge.js の spaSyncBaseline）が
        状態反映のタイミングで行うので、ここでは状態を反転してバーへ反映するだけでよい。
        """
        if not self._authorized(token):
            return
        self.spa_on = not self.spa_on
        log(f"[SPA] {'ON' if self.spa_on else 'OFF'}")
        await self.refresh_panels()

    async def on_set_selector(self, source, token=None, value="") -> None:
        """セレクタ入力欄の変更（入力のたびに呼ばれる）。実行時セレクタを更新する。

        空にしても SPA検知は落とさない（既定ルート監視に切り替わるため）。SPA検知中に
        対象が変わったときの署名基準の取り直しは、状態反映を受けたページ側が行う。
        ログは氾濫を避けるためここでは出さず、確定時（on_commit_selector）に出す。
        """
        if not self._authorized(token):
            return
        new = (value or "").strip()
        if new == self.selector:
            return
        self.selector = new
        await self.refresh_panels()

    async def on_spa_changed(self, source, token=None, sig=None) -> None:
        """SPA検知の通知: ページ側が「落ち着いた中身の変化」を検知したときに呼ばれる。

        記録ON かつ SPA検知ON のときだけ、通知元ページを1枚撮る。落ち着き判定・重複除外
        （同じ署名は通知しない）はページ側（badge.js）が済ませているので、ここでは記録状態の
        ゲートと skip_urls だけ見て撮る。token 不一致（操作バー以外）は黙って無視する。
        """
        if not self._authorized(token):
            return
        if not (self.on and self.spa_on):
            return
        url = self._shoot(source["page"])
        if url is not None:
            log(f"[SPA変化] {url}")

    async def on_commit_selector(self, source, token=None, value="") -> None:
        """セレクタ入力の確定（blur / Enter）。最終値をログに残す。

        入力のたびに出すとログが氾濫するため、確定時にだけ実際に使う値を記録する。
        これにより「どのセレクタで動かしたか」がログと実態で一致する。
        """
        if not self._authorized(token):
            return
        new = (value or "").strip()
        log(f"[セレクタ] {'クリア' if not new else repr(new)}")

    async def get_state(self, source, token=None) -> dict:
        """操作バーが描画前に現在の状態を問い合わせるためのバインディング。

        ページ遷移直後、新しいドキュメントのバーはこれを見てから描画するので、
        記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。SPA検知の
        ON/OFF・セレクタ値も同時に返し、遷移後も入力欄・ボタンを正しく初期化する。

        token 不一致（操作バー以外からの問い合わせ）には既定状態を返し、実際の
        記録状態やセレクタ値を外部スクリプトへ漏らさない。
        """
        if not self._authorized(token):
            return {"recording": False, "spa": False, "selector": ""}
        return {"recording": self.on, "spa": self.spa_on, "selector": self.selector}

    # ---- セットアップと監視ループ ----

    async def setup(self) -> None:
        """ページ内から呼ぶコールバックを公開し、操作バーを全ページへ注入する。

        context 単位なので以後開く新規タブにも自動適用される
        （expose_binding は add_init_script より前に登録する）。
        """
        # バインディング名は badge.py の BIND_* に集約（badge.js 側の呼び出し名と一致）。
        await self.context.expose_binding(badge.BIND_TOGGLE, self.on_toggle)
        await self.context.expose_binding(badge.BIND_SHOT, self.on_shot)
        await self.context.expose_binding(badge.BIND_SPA_TOGGLE, self.on_spa_toggle)
        await self.context.expose_binding(badge.BIND_SET_SELECTOR, self.on_set_selector)
        await self.context.expose_binding(badge.BIND_COMMIT_SELECTOR, self.on_commit_selector)
        await self.context.expose_binding(badge.BIND_SPA_CHANGED, self.on_spa_changed)
        await self.context.expose_binding(badge.BIND_GETSTATE, self.get_state)
        # badge.js には今回の合言葉（token）と SPA検知のデバウンス時間（settle_delay をミリ秒へ）を
        # 埋め込む。token は各バインディング呼び出しの照合、settle は落ち着き判定に使う。
        settle_ms = int(self.config.settle_delay * 1000)
        await self.context.add_init_script(badge.build_badge_script(self.token, settle_ms))

    def _prune(self, pages) -> None:
        """閉じられたページを管理から除去する。"""
        for pg in list(self.seen):
            if pg not in pages:
                del self.seen[pg]

    async def run(self, closed: asyncio.Event) -> None:
        """Edge のウィンドウが閉じるまで監視する。

        URL変化・新規タブを契機に保存する。中身変化（SPA検知）はページ側がイベント駆動で
        検知し、__eac_spa_changed（on_spa_changed）経由で保存を要求してくるため、この
        ループでは扱わない（毎tickの署名評価が無くなり負荷が下がる）。
        """
        while not closed.is_set():
            pages = list(self.context.pages)
            self._prune(pages)

            # 記録状態を全バーに反映（新規タブ・再描画にも毎 tick 追従）
            await self.refresh_panels()

            # 記録ON の間だけ検知して保存。OFF の間は seen を更新しないので、
            # ON にした瞬間に現在ページが「変化」として検知され撮れる
            #（on_toggle でも即撮りするため通常は先回り）。
            if self.on:
                for pg in pages:
                    try:
                        url = pg.url
                    except Exception:
                        continue
                    if url in self.config.skip_urls:
                        continue

                    # フラグメント（#...）を除いたキーで「同じページか」を判定する。
                    # scroll-spy によるハッシュだけの変化では撮り直さない。
                    key = _url_key(url)
                    if self.seen.get(pg) != key:
                        self.seen[pg] = key
                        self.runner.spawn(pg, url, self.config, self.selector)

            await asyncio.sleep(self.config.poll_interval)


async def main(config: Config) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)

    cleanup_old_profiles()
    tmp = tempfile.mkdtemp(prefix="edge-debug-")  # 今回用の一時プロファイル

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                **_edge_launch_kwargs(config, tmp)
            )
        except Exception as e:
            notify_fatal(
                f"Edge を起動できませんでした: {e}\n"
                "Edge がインストールされているか、config.ini の edge_path を確認してください。"
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return

        session = CaptureSession(context, config)
        await session.setup()

        # Edge のウィンドウを閉じたら監視ループを抜けるためのフラグ
        closed = asyncio.Event()
        context.on("close", lambda: closed.set())

        try:
            # 最初のページで start_url を開く（about:blank ならそのまま）
            page = context.pages[0] if context.pages else await context.new_page()
            if config.start_url and config.start_url != "about:blank":
                try:
                    await page.goto(config.start_url)
                except Exception as e:
                    log(f"[skip goto] {config.start_url}  ({e})")

            log(
                f"Edge を起動しました（記録は{'ON' if session.on else 'OFF（待機）'}で開始）。"
                "ページ上部の操作バーで記録開始/停止・今すぐ1枚・SPA検知を"
                "操作できます（終了するには Edge のウィンドウを閉じてください）"
            )

            await session.run(closed)
        finally:
            # Ctrl+C / ウィンドウを閉じた場合のどちらでもここが走る。
            # 起動した Edge を終了し、一時プロファイルを削除する。
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # 先に設定を読み、ログの出力先を保存先（output_dir）へ切り替えてから記録を始める。
    # （load_config が output_dir 確定時に set_log_dir を呼ぶ。以後のログはそこへ残る）
    config = load_config()

    # ログは追記のみ（既存があればそのまま末尾へ足す。削除・作り直しはしない）。
    log("=== edge-auto-capture 起動 ===")
    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        # コンソール実行時のみ届く保険的な停止経路。
        log("停止しました。")
    log("=== 終了 ===")
