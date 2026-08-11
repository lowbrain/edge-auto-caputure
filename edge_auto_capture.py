"""
記録ONの間、Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

キャプチャのタイミングは利用者が操作する。各ページ上部の操作パネルで
「記録開始／停止」で記録期間を制御でき、「今すぐ1枚」で今のページを1回だけ撮れる。
既定は記録OFF（待機）で起動する（config.ini の start_recording で変更可）。

SPA（URLが変わらず中身だけ変わるページ）向けに、パネルの入力欄へ CSS セレクタを
入れて「SPA検知」を ON にすると、記録ON中はそのセレクタ要素の中身が変わるたびに
自動保存する（同じ内容は署名比較で撮らない）。セレクタ入力が空だと SPA検知は使えない。
このセレクタは _part.txt の抜き出し対象も兼ねる（初期値は config.ini の target_selector）。

このスクリプトが Edge の起動・監視・後始末までを一括で行う（Playwright が
毎回まっさらな一時プロファイルで Edge を起動し、終了時に自動で掃除する）。

構成（役割ごとにモジュール分割）:
  - edge_auto_capture.py … 本ファイル。エントリと監視セッション（CaptureSession）。
  - badge.py / badge.js  … 各ページ上部の操作バー（ページ側 JS 一式）。
  - capture.py           … 設定読み込み・1ページ分の保存処理・基盤ユーティリティ。

事前準備:
  pip install -e .          （または pip install playwright）
  ※ システムにインストール済みの Edge をそのまま使うため、
    playwright install（ブラウザ同梱バイナリの取得）は不要。

起動方法:
  - python edge_auto_capture.py（開発時）、または
  - ビルドした edge-auto-capture.exe をダブルクリック（配布時）
  最初に開くページ・保存先などは同じフォルダの config.ini で指定する
  （起動ページは start_url。空なら about:blank）。開いた Edge で普通に
  閲覧し、記録ONの間だけ URL/タブの変化ごとに output\\ へ自動保存される。

設定はソースではなく、同じフォルダの config.ini を編集して変更する。
停止は「Edge のウィンドウを閉じる」だけでよい（コンソール実行時は Ctrl + C
も使える）。停止すると、このスクリプトが起動した Edge の終了と一時プロファイル
の削除まで行う。動作ログは保存先（output_dir）フォルダの log.txt に残る。
各ページの上部には操作パネル（記録中/待機中の表示＋「記録開始/停止」＋「今すぐ1枚」＋
セレクタ入力欄＋「SPA検知」トグル）を表示する
（保存するスクリーンショットにも、抽出する txt / part テキストにも含めない）。
"""

import asyncio
import json
import shutil
import tempfile

from playwright.async_api import Page, async_playwright

import badge
from capture import (
    Config,
    notify_fatal,
    spawn_capture,
    try_eval,
    cleanup_old_profiles,
    load_config,
    log,
)


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
        # サンドボックスを有効化してバナーを消す（キャプチャ画像への映り込みも防ぐ）。
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

    不変条件: selector が空なら spa_on は必ず False（検知対象が無いため）。
    """

    def __init__(self, context, config: Config) -> None:
        self.context = context
        self.config = config
        # --- 実行時状態 ---
        self.on = config.start_recording          # 記録中か（自動保存のマスタースイッチ）
        self.spa_on = False                       # SPA検知（中身変化を契機に保存）
        self.selector = config.target_selector    # 検知/抜き出しの対象 CSS セレクタ
        # --- ページごとの追跡情報 ---
        self.seen: "dict[Page, str]" = {}         # page -> 直近のURL
        self.sig_seen: "dict[Page, str]" = {}     # page -> 最後に撮った署名
        self.sig_prev: "dict[Page, str]" = {}     # page -> 前 tick の署名（落ち着き判定用）

    # ---- ページ側とのやり取り ----

    async def _sig(self, page: Page) -> str:
        """現在のセレクタで、そのページのコンテンツ署名を得る。"""
        return await page.evaluate(badge.SIG_CALL, self.selector)

    async def reseed_signatures(self) -> None:
        """全ページの SPA署名基準を「現在の内容」に取り直す。

        SPA検知を ON にした瞬間やセレクタを変えた直後に呼ぶ。基準を現状に
        合わせることで、開始/変更の直後に無駄撮りせず、以後の「変化」だけを契機にする。
        """
        for pg in list(self.context.pages):
            try:
                sig = await self._sig(pg)
            except Exception:
                continue
            self.sig_seen[pg] = sig
            self.sig_prev[pg] = sig

    async def refresh_panels(self) -> None:
        """開いている全ページの操作パネルへ現在の状態を反映する。

        記録中/SPA検知/セレクタの3つを送る。新規タブ（初期は待機表示で描画）や、
        サイト側の再描画で作り直されたパネルも、毎 tick これを呼ぶことで追従する。
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

    async def on_toggle(self, source) -> None:
        """「記録開始／停止」ボタン: 記録状態を反転する。

        ON にした瞬間は現在開いている全ページを即キャプチャし、seen を現在 URL に
        そろえる（撮り始めの体感を良くしつつ、直後のループでの二重取りも防ぐ）。
        """
        self.on = not self.on
        log(f"[記録] {'開始' if self.on else '停止'}")
        await self.refresh_panels()
        if self.on:
            for pg in list(self.context.pages):
                try:
                    url = pg.url
                except Exception:
                    continue
                if url in self.config.skip_urls:
                    continue
                self.seen[pg] = url
                spawn_capture(pg, url, self.config, self.selector)

    async def on_shot(self, source) -> None:
        """「今すぐ1枚」ボタン: 記録状態に関わらず、押したページを1回だけ撮る。

        seen は触らないので自動キャプチャの判定には影響しない（記録ON中でも
        同一 URL の「撮り直し」として別ファイルにもう1枚保存される）。
        保存が終わると spawn_capture がパネルを一瞬フラッシュして知らせる。
        """
        pg = source["page"]
        try:
            url = pg.url
        except Exception:
            return
        if url in self.config.skip_urls:
            return
        log(f"[手動] {url}")
        spawn_capture(pg, url, self.config, self.selector)

    async def on_spa_toggle(self, source) -> None:
        """「SPA検知」ボタン: 中身の変化を契機にした自動保存を ON/OFF する。

        セレクタ未設定では検知対象が無いので no-op（UI 側でも無効化しているが二重防御）。
        ON にした瞬間は署名基準を現状に取り直し、開始直後の無駄撮りを避ける。
        """
        if not self.selector:
            return
        self.spa_on = not self.spa_on
        log(f"[SPA] {'ON' if self.spa_on else 'OFF'}")
        if self.spa_on:
            await self.reseed_signatures()
        await self.refresh_panels()

    async def on_set_selector(self, source, value) -> None:
        """セレクタ入力欄の変更（入力のたびに呼ばれる）。実行時セレクタを更新する。

        空になったら SPA検知を OFF に落とす（検知対象が無いため）。SPA検知中に
        対象が変わったら署名基準を取り直す（旧セレクタの署名で誤検知しないため）。
        ログは氾濫を避けるためここでは出さず、確定時（on_commit_selector）に出す。
        """
        new = (value or "").strip()
        if new == self.selector:
            return
        self.selector = new
        if not new:
            self.spa_on = False
        elif self.spa_on:
            await self.reseed_signatures()
        await self.refresh_panels()

    async def on_commit_selector(self, source, value) -> None:
        """セレクタ入力の確定（blur / Enter）。最終値をログに残す。

        入力のたびに出すとログが氾濫するため、確定時にだけ実際に使う値を記録する。
        これにより「どのセレクタで動かしたか」がログと実態で一致する。
        """
        new = (value or "").strip()
        log(f"[セレクタ] {'クリア' if not new else repr(new)}")

    async def get_state(self, source) -> dict:
        """パネルが描画前に現在の状態を問い合わせるためのバインディング。

        ページ遷移直後、新しいドキュメントのパネルはこれを見てから描画するので、
        記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。SPA検知の
        ON/OFF・セレクタ値も同時に返し、遷移後も入力欄・ボタンを正しく初期化する。
        """
        return {"recording": self.on, "spa": self.spa_on, "selector": self.selector}

    # ---- セットアップと監視ループ ----

    async def setup(self) -> None:
        """ページ内から呼ぶコールバックを公開し、操作バーを全ページへ注入する。

        context 単位なので以後開く新規タブにも自動適用される
        （expose_binding は add_init_script より前に登録する）。
        """
        await self.context.expose_binding("__eac_toggle", self.on_toggle)
        await self.context.expose_binding("__eac_shot", self.on_shot)
        await self.context.expose_binding("__eac_spa_toggle", self.on_spa_toggle)
        await self.context.expose_binding("__eac_set_selector", self.on_set_selector)
        await self.context.expose_binding("__eac_commit_selector", self.on_commit_selector)
        await self.context.expose_binding("__eac_getstate", self.get_state)
        await self.context.add_init_script(badge.BADGE_SCRIPT)

    def _prune(self, pages) -> None:
        """閉じられたページを管理から除去（seen と SPA署名の両方）。"""
        for pg in list(self.seen):
            if pg not in pages:
                del self.seen[pg]
        for pg in list(self.sig_prev):
            if pg not in pages:
                self.sig_prev.pop(pg, None)
                self.sig_seen.pop(pg, None)

    async def run(self, closed: asyncio.Event) -> None:
        """Edge のウィンドウが閉じるまで、URL変化・新規タブ・（SPA検知ONなら）中身変化を監視する。"""
        while not closed.is_set():
            pages = list(self.context.pages)
            self._prune(pages)

            # 記録状態を全パネルに反映（新規タブ・再描画にも毎 tick 追従）
            await self.refresh_panels()

            # 記録ON の間だけ検知して保存。OFF の間は seen を更新しないので、
            # ON にした瞬間に現在ページが「変化」として検知され撮れる
            #（on_toggle でも即撮りするため通常は先回り）。
            if self.on:
                spa_active = self.spa_on and bool(self.selector)
                for pg in pages:
                    try:
                        url = pg.url
                    except Exception:
                        continue
                    if url in self.config.skip_urls:
                        continue

                    url_changed = self.seen.get(pg) != url
                    if url_changed:
                        self.seen[pg] = url
                        spawn_capture(pg, url, self.config, self.selector)

                    # SPA検知: セレクタ要素の中身の変化を契機に保存。
                    # 「前回撮影時と署名が違う」かつ「前 tick から署名が不変（＝落ち着いた）」
                    # 時だけ撮る（描画途中の多段レンダを撮らない）。
                    if spa_active:
                        try:
                            sig = await self._sig(pg)
                        except Exception:
                            sig = None
                        if sig is not None:
                            if url_changed:
                                # 遷移直後は URL 側で撮ったので、その内容を基準にして二重撮り防止。
                                self.sig_seen[pg] = sig
                            elif sig != self.sig_seen.get(pg) and sig == self.sig_prev.get(pg):
                                self.sig_seen[pg] = sig
                                spawn_capture(pg, url, self.config, self.selector)
                            self.sig_prev[pg] = sig

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
                "ページ上部のパネルで記録開始/停止・今すぐ1枚・SPA検知（セレクタ入力時）を"
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
