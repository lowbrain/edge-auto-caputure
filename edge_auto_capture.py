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
  - capture.py           … 1ページ分の保存処理（撮影実行器 CaptureRunner）・ページ操作ヘルパ。
  - config.py            … 設定（Config / config.ini の load_config）。
  - infra.py             … 基盤ユーティリティ（パス・ログ・致命エラー通知・一時プロファイル掃除）。

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
import sys
import tempfile
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, async_playwright

import badge
from capture import CaptureRunner, try_eval
from config import Config, load_config
from infra import (
    __version__,
    acquire_single_instance_lock,
    cleanup_old_profiles,
    log,
    notify_fatal,
)


def _url_key(url: str) -> str:
    """URL変化の「同じページか」判定に使う比較キー。フラグメント（#...）を除く。

    多くのドキュメントSPA（例: Vuetify）は scroll-spy で、スクロールに追従して URL の
    ハッシュ（#見出し）だけを書き換える。ハッシュ違いは同じドキュメントなので、#以降を
    落として「同じページ」とみなし、スクロールのたびの二重撮りを防ぐ。ハッシュルーティング
    型SPAの本当の中身変化は SPA検知（本文署名）が担うため、ここで落としても取りこぼさない。
    """
    return url.split("#", 1)[0]


# ブラウザの定義: config.browser のキー → (channel, 表示名, 実行パスの config 項目名)。
#   channel   … Playwright の channel 名（標準インストール先を自動検出。未インストールなら起動時に例外）。
#   path_attr … 実行ファイルパスを持つ config 項目名（空なら自動検出）。
BROWSER_BY_KEY = {
    "edge": ("msedge", "Edge", "edge_path"),
    "chrome": ("chrome", "Chrome", "chrome_path"),
}
# browser 未指定（自動選択）時に試す優先順。
AUTO_BROWSER_ORDER = ("edge", "chrome")


def _browser_candidates(config: Config) -> list[tuple[str, str, str]]:
    """起動を試すブラウザの候補を (channel, 表示名, 実行パス) の優先順で返す。

    config.browser が指定されていれば、その1つだけ（無ければ起動失敗＝終了）。
    空なら Edge→Chrome の順で自動フォールバックする。実行パスは対応する
    config 項目（edge_path / chrome_path）が空でなければそれを使う。
    """
    keys = [config.browser] if config.browser else list(AUTO_BROWSER_ORDER)
    candidates = []
    for key in keys:
        channel, label, path_attr = BROWSER_BY_KEY[key]
        candidates.append((channel, label, getattr(config, path_attr, "")))
    return candidates


def _downloads_dir(config: Config) -> Path:
    """利用者のダウンロードを残す保存先（output_dir/downloads）を返す（E-4）。

    撮影成果物（png/txt/log.txt）と混ざらないよう downloads サブフォルダに分ける。
    """
    return config.output_dir / "downloads"


def _unique_path(directory: Path, name: str) -> Path:
    """directory/name を返す。既に在れば name(1)/name(2)… と連番を付けて衝突を避ける。

    同名ファイルを続けて落としても上書きしないようにするため（拡張子は保つ）。
    """
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while True:
        cand = directory / f"{stem}({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def _browser_launch_kwargs(
    config: Config, user_data_dir: str, channel: str, executable_path: str = ""
) -> dict:
    """launch_persistent_context に渡すブラウザ起動オプションを組み立てる。

    channel は "msedge" / "chrome" のいずれか（Chromium系で共通の起動引数を使う）。
    executable_path が空でなければ、自動検出よりそのパスを優先する。
    """
    browser_args = [
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
        channel=channel,
        headless=False,
        args=browser_args,
        # Playwright は既定で --no-sandbox を付け、ブラウザが黄色い警告バナーを出す。
        # サンドボックスを有効化してバナーを消す（撮影画像への映り込みも防ぐ）。
        chromium_sandbox=True,
        # 固定ビューポートのエミュレーションを外し、ウィンドウサイズにページを
        # 追従させる（--start-maximized も no_viewport でないと効かない）。
        no_viewport=True,
        # ダウンロードを受理する（E-4）。既定でも真だが意図を明示する。
        # ただし accept_downloads / downloads_path だけでは足りない: Playwright は
        # どちらの場合もコンテキスト終了時にダウンロードを削除するため（実機で確認）、
        # 別途 download イベントで save_as して退避する（CaptureSession.on_download）。
        accept_downloads=True,
    )
    if executable_path:
        kwargs["executable_path"] = executable_path
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

    # ---- ダウンロードの退避（E-4） ----

    async def on_download(self, download) -> None:
        """利用者がブラウザで落としたファイルを保存先へ退避する（E-4）。

        Playwright は既定でダウンロードをコンテキスト終了時に削除する。
        accept_downloads / downloads_path を指定しても削除される（一時置き場が変わる
        だけ）ので、ここで save_as して初めて手元に残る。元のファイル名のまま
        output_dir/downloads へ保存し、同名衝突時は連番を付ける。
        token 照合は不要（ブラウザ本体が発火するイベントで、ページ側から詐称できない）。
        """
        name = download.suggested_filename or "download"
        target = _unique_path(_downloads_dir(self.config), name)
        try:
            await download.save_as(str(target))
            log(f"[DL] 保存しました: {target.name}")
        except Exception as e:
            # 例: 保存前にウィンドウを閉じられ一時ファイルが消えた等。無言にはしない。
            log(f"[DL] ダウンロードの保存に失敗しました: {name} ({e})")

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
        # ダウンロードは context 単位で拾う（全タブ・以後開く新規タブも自動対象）。
        # 各ファイルを保存先へ退避しないと終了時に消える（E-4）。
        self.context.on("download", self.on_download)

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
    # output_dir は load_config で書き込み可能な場所へ解決済み（D-C1）だが、その後に
    # 消される等の可能性もあるため裸で放置せず、失敗したら無言終了ではなく通知して抜ける。
    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        # ダウンロードの受け皿（output_dir/downloads）も先に用意する（E-4）。
        # Playwright に downloads_path として渡すため、起動前に存在させておく。
        _downloads_dir(config).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        notify_fatal(f"保存先フォルダを作成できませんでした: {config.output_dir}\n({e})")
        return

    # プロファイルの置き場所を決める。
    #   profile_dir 未指定（既定）: 毎回まっさらな使い捨てプロファイル。終了時に削除する。
    #   profile_dir 指定        : そのフォルダを再利用する（ログイン状態などを保持）。削除しない。
    if config.profile_dir:
        user_data_dir = config.profile_dir
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        ephemeral = False
        # 使い捨て分（edge-debug-*）だけを掃除する。再利用プロファイルは掃除対象外
        # （命名も置き場所も別なので glob に一致しないが、念のため除外指定も渡す）。
        cleanup_old_profiles(keep=Path(user_data_dir))
    else:
        cleanup_old_profiles()
        user_data_dir = tempfile.mkdtemp(prefix="edge-debug-")  # 今回用の一時プロファイル
        ephemeral = True

    async with async_playwright() as p:
        # config.browser 指定があればそのブラウザのみ、無ければ Edge→Chrome の順で試す。
        candidates = _browser_candidates(config)
        context = None
        errors: list[str] = []
        for channel, label, executable_path in candidates:
            try:
                context = await p.chromium.launch_persistent_context(
                    **_browser_launch_kwargs(config, user_data_dir, channel, executable_path)
                )
                log(f"{label} を起動しました。")
                break
            except Exception as e:
                # 未インストール等で起動できなければ次の候補へ回す（候補が1つなら終了）。
                log(f"[skip] {label} を起動できませんでした: {e}")
                errors.append(f"- {label}: {e}")

        if context is None:
            tried = " / ".join(label for _, label, _ in candidates)
            notify_fatal(
                f"ブラウザを起動できませんでした（試行: {tried}）。\n"
                f"{tried} がインストールされているか確認してください。\n"
                + "\n".join(errors)
            )
            if ephemeral:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            return

        session = CaptureSession(context, config)
        await session.setup()

        # ブラウザのウィンドウを閉じたら監視ループを抜けるためのフラグ
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
                f"記録は{'ON' if session.on else 'OFF（待機）'}で開始しました。"
                "ページ上部の操作バーで記録開始/停止・今すぐ1枚・SPA検知を"
                "操作できます（終了するにはブラウザのウィンドウを閉じてください）"
            )

            await session.run(closed)
        finally:
            # Ctrl+C / ウィンドウを閉じた場合のどちらでもここが走る。
            # 起動したブラウザを終了する。使い捨てプロファイルのみ削除し、
            # 再利用プロファイル（profile_dir 指定）は次回のために残す。
            try:
                await context.close()
            except Exception:
                pass
            if ephemeral:
                shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    # 先に設定を読み、ログの出力先を保存先（output_dir）へ切り替えてから記録を始める。
    # （load_config が output_dir 確定時に set_log_dir を呼ぶ。以後のログはそこへ残る）
    config = load_config()

    # ログは追記のみ（既存があればそのまま末尾へ足す。削除・作り直しはしない）。
    log(f"=== edge-auto-capture v{__version__} 起動 ===")

    # 多重起動を入口で止める（D-C4）。二度押しで 2 つ目が起動すると、output/・
    # log.txt・使い捨てプロファイル（A-5）を先行インスタンスと奪い合うため、
    # ブラウザを起こす前・掃除（cleanup_old_profiles）が走る前にここで抑止する。
    if not acquire_single_instance_lock():
        notify_fatal(
            "edge-auto-capture はすでに起動しています。\n"
            "二重に起動することはできません。すでに開いているウィンドウをご確認ください。"
        )
        log("=== 終了（多重起動を抑止） ===")
        sys.exit(1)

    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        # コンソール実行時のみ届く保険的な停止経路。
        log("停止しました。")
    log("=== 終了 ===")
