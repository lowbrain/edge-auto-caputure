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
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, async_playwright

import badge
from capture import CaptureRunner, group_folder_name, group_stamp, group_subdir, try_eval
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


def _downloads_dir(config: Config, group_id: str = "") -> Path:
    """利用者のダウンロードを残す保存先を返す（E-4）。

    撮影成果物（png/txt/log.txt）と混ざらないよう downloads サブフォルダに分ける。
    保存物と同じく系譜（lineage）ごとにまとめるため、group_id 採番済みなら
    output_dir/lineage-<id>/downloads、未採番(空)なら output_dir/downloads を返す。
    """
    return group_subdir(config.output_dir, group_id) / "downloads"


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


@dataclass
class GroupState:
    """タブ系譜（グループ）1 つぶんの実行時状態。

    グループ = root ページ（起動時の最初のタブ、または手動で開いた別タブ）と、そこから
    window.open / target="_blank" で派生したポップアップ/ウィンドウの一族。各グループが
    記録ON/OFF・SPA検知・対象セレクタを独立して持ち、系譜内のページはこの状態を共有する。
    """

    on: bool          # 記録中か（このグループの自動保存マスタースイッチ）
    spa_on: bool      # SPA検知（中身変化を契機に保存）
    selector: str     # 検知/抜き出しの対象 CSS セレクタ
    id: str = ""      # 系譜を作った時刻（ミリ秒まで・区切りなし）。フォルダ名/ログの識別子。空＝未採番


class CaptureSession:
    """1 回の起動ぶんの監視セッション。

    タブ系譜（グループ）ごとの実行時状態（記録中/SPA検知/セレクタ）と、それを操作する各
    コールバック・監視ループをまとめて持つ。以前は main() 内のネスト関数群だったものを
    クラスへ集約し、状態の所在を明確にして main() を薄くする。

    状態はセッション全体で共有せず、ページの opener 連鎖で決まるグループ単位に持つ
    （groups / page_root）。各ページの操作バーは自分の属するグループの状態を表示・操作し、
    無関係な別タブ（手動で開いたもの）は初期OFFの独立グループになる。

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
        # --- グループ単位の実行時状態 ---
        # 記録ON/OFF・SPA検知・セレクタは「タブ系譜（グループ）」ごとに独立して持つ。
        self.groups: dict[Page, GroupState] = {}  # root ページ -> そのグループの状態
        self.page_root: dict[Page, Page] = {}     # 各ページ -> 所属グループの root（メモ化）
        # --- ページごとの追跡情報 ---
        self.seen: dict[Page, str] = {}           # page -> 直近のURL
        # framenavigated / close を配線済みのページ（二重配線を防ぐ。B-1）
        self._tracked: set[Page] = set()

    # ---- ページ側とのやり取り ----

    async def refresh_panels(self) -> None:
        """開いている全ページの操作バーへ、そのページのグループの状態を反映する。

        記録ON/OFF・SPA検知・セレクタが変わった各コールバックがこれを呼ぶ。新規タブや
        サイト側の再描画で作り直されたバーは、バー自身が __eac_getstate で自己同期する
        （badge.js）ため、ここでの毎tick配布は不要（B-1/B-2 でポーリングを廃止）。状態は
        グループごとに違うため、ページ単位で自分のグループの値を配る。ページ数ぶんを直列に
        待たず asyncio.gather で並列に流す。
        """
        async def _apply(pg) -> None:
            grp = await self._resolve_group(pg)
            flag = "true" if grp.on else "false"
            spa_flag = "true" if grp.spa_on else "false"
            sel = json.dumps(grp.selector)  # 日本語/記号を含んでも安全に JS リテラル化
            await try_eval(
                pg,
                f"window.__eacApplyState && window.__eacApplyState({flag}, {spa_flag}, {sel})",
            )

        await asyncio.gather(*(_apply(pg) for pg in list(self.context.pages)))

    # ---- expose_binding で公開するコールバック ----
    #
    # これらは全ページの window に公開されるため、閲覧中サイトのスクリプトからも呼べてしまう。
    # 各コールバックは第1引数 token を self.token と照合し、一致しない呼び出し（＝操作バー以外）
    # は黙って無視する（ログも出さない: 不一致呼び出しを連打されてもログを氾濫させないため）。
    # 引数には既定値を与え、任意個数/不正な引数で呼ばれても TypeError で落ちないようにする。

    def _authorized(self, token) -> bool:
        """操作バーからの正規の呼び出しか（合言葉が一致するか）を判定する。"""
        return isinstance(token, str) and secrets.compare_digest(token, self.token)

    # ---- タブ系譜（グループ）の解決 ----

    def _make_group(self, on: bool, spa_on: bool, selector: str) -> GroupState:
        """GroupState を作る。id は「作った時刻（ミリ秒まで・区切りなし）」で、フォルダ名/ログに使う。"""
        return GroupState(on=on, spa_on=spa_on, selector=selector, id=group_stamp())

    async def _find_root(self, page) -> Page:
        """page の所属グループの root ページを opener 連鎖から求める。

        既知の root（page_root に載っているページ）に達したらそれを、opener が None に達したら
        その末端ページ自身を root とする。opener 取得に失敗したら、その時点のページを root 扱い
        にする（系譜を辿れないページは、それ自身を独立グループの起点にする）。
        """
        p = page
        while True:
            known = self.page_root.get(p)
            if known is not None:
                return known
            try:
                parent = await p.opener()
            except Exception:
                return p
            if parent is None:
                return p
            p = parent

    async def _resolve_group(self, page) -> GroupState:
        """page が属するグループの状態を返す（無ければ新規グループを OFF で作る）。

        opener を辿って root を決めてメモ化する。root のグループがまだ無い＝手動で開かれた
        新規タブなので、初期OFF（無関係タブを勝手に撮らない）の独立グループを作る。起動時の
        最初のグループは setup() が start_recording に従って先に用意している。
        """
        root = self.page_root.get(page)
        if root is None:
            root = await self._find_root(page)
            self.page_root[page] = root
        grp = self.groups.get(root)
        if grp is None:
            grp = self._make_group(on=False, spa_on=False, selector=self.config.target_selector)
            self.groups[root] = grp
            log(f"[{group_folder_name(grp.id)}] 新しいタブを認識しました（初期は待機）")
        return grp

    def _group_pages(self, root: Page) -> list[Page]:
        """root を共有する現存ページ（＝同じグループのページ）を返す。"""
        return [pg for pg in self.context.pages if self.page_root.get(pg) is root]

    def _shoot(self, pg, grp: "GroupState") -> Optional[str]:
        """1ページを撮る。url 取得失敗と skip_urls を弾き、撮れば url を返す（弾けば None）。

        「url 取得 → skip 判定 → runner.spawn」の定型を1か所に集約する（各コールバックと監視
        ループで同じ並びを書かないため）。記録状態のゲートは呼び出し側の責務（ここでは見ない）。
        撮影対象の抜き出しセレクタは、そのページが属するグループの selector を使う。
        """
        try:
            url = pg.url
        except Exception:
            return None
        if url in self.config.skip_urls:
            return None
        self.runner.spawn(pg, url, self.config, grp.selector, grp.id)
        return url

    async def on_toggle(self, source, token=None) -> None:
        """「記録開始／停止」ボタン: 押したページのグループの記録状態を反転する。

        ON にした瞬間は同じグループの現存ページを即撮影し、seen を現在 URL に
        そろえる（撮り始めの体感を良くしつつ、直後のループでの二重取りも防ぐ）。
        他のグループ（無関係な別タブ）の記録状態は変えない。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        grp.on = not grp.on
        try:
            where = source["page"].url
        except Exception:
            where = ""
        log(f"[記録] {'開始' if grp.on else '停止'} {group_folder_name(grp.id)}"
            + (f"  {where}" if where else ""))
        await self.refresh_panels()
        if grp.on:
            root = self.page_root[source["page"]]
            for pg in self._group_pages(root):
                url = self._shoot(pg, grp)
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
        grp = await self._resolve_group(source["page"])
        url = self._shoot(source["page"], grp)
        if url is not None:
            log(f"[手動] {group_folder_name(grp.id)}  {url}")

    async def on_spa_toggle(self, source, token=None) -> None:
        """「SPA検知」ボタン: 中身の変化を契機にした自動保存を ON/OFF する。

        セレクタ未設定でも既定ルート（main/article/本文）を監視するため、常に切替可。
        署名基準の取り直し（開始直後の無駄撮り回避）はページ側（badge.js の spaSyncBaseline）が
        状態反映のタイミングで行うので、ここでは状態を反転してバーへ反映するだけでよい。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        grp.spa_on = not grp.spa_on
        log(f"[SPA] {'ON' if grp.spa_on else 'OFF'} {group_folder_name(grp.id)}")
        await self.refresh_panels()

    async def on_set_selector(self, source, token=None, value="") -> None:
        """セレクタ入力欄の変更（入力のたびに呼ばれる）。実行時セレクタを更新する。

        空にしても SPA検知は落とさない（既定ルート監視に切り替わるため）。SPA検知中に
        対象が変わったときの署名基準の取り直しは、状態反映を受けたページ側が行う。
        ログは氾濫を避けるためここでは出さず、確定時（on_commit_selector）に出す。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        new = (value or "").strip()
        if new == grp.selector:
            return
        grp.selector = new
        await self.refresh_panels()

    async def on_spa_changed(self, source, token=None, sig=None) -> None:
        """SPA検知の通知: ページ側が「落ち着いた中身の変化」を検知したときに呼ばれる。

        通知元ページのグループが記録ON かつ SPA検知ON のときだけ、通知元ページを1枚撮る。
        落ち着き判定・重複除外（同じ署名は通知しない）はページ側（badge.js）が済ませているので、
        ここではグループの記録状態のゲートと skip_urls だけ見て撮る。token 不一致（操作バー以外）
        は黙って無視する。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        if not (grp.on and grp.spa_on):
            return
        url = self._shoot(source["page"], grp)
        if url is not None:
            log(f"[SPA変化] {group_folder_name(grp.id)}  {url}")

    async def on_commit_selector(self, source, token=None, value="") -> None:
        """セレクタ入力の確定（blur / Enter）。最終値をログに残す。

        入力のたびに出すとログが氾濫するため、確定時にだけ実際に使う値を記録する。
        これにより「どのセレクタで動かしたか」がログと実態で一致する。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        new = (value or "").strip()
        log(f"[セレクタ] {'クリア' if not new else repr(new)} {group_folder_name(grp.id)}")

    async def get_state(self, source, token=None) -> dict:
        """操作バーが描画前に現在の状態を問い合わせるためのバインディング。

        ページ遷移直後、新しいドキュメントのバーはこれを見てから描画するので、
        記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。SPA検知の
        ON/OFF・セレクタ値も同時に返し、遷移後も入力欄・ボタンを正しく初期化する。

        token 不一致（操作バー以外からの問い合わせ）には既定状態を返し、実際の
        記録状態やセレクタ値を外部スクリプトへ漏らさない。

        返すのは問い合わせ元ページが属するグループの状態。新しいドキュメントのバーが最初に
        これを呼ぶタイミングでグループを確定・メモ化する（監視ループ到達前でも取りこぼさない）。
        """
        if not self._authorized(token):
            return {"recording": False, "spa": False, "selector": ""}
        grp = await self._resolve_group(source["page"])
        return {"recording": grp.on, "spa": grp.spa_on, "selector": grp.selector}

    # ---- ダウンロードの退避（E-4） ----

    async def on_download(self, download) -> None:
        """利用者がブラウザで落としたファイルを保存先へ退避する（E-4）。

        Playwright は既定でダウンロードをコンテキスト終了時に削除する。
        accept_downloads / downloads_path を指定しても削除される（一時置き場が変わる
        だけ）ので、ここで save_as して初めて手元に残る。元のファイル名のまま、撮影物と
        同じく系譜（lineage）ごとの output_dir/lineage-<id>/downloads へ保存し、同名衝突時は連番を付ける。
        どの系譜かは発生元ページ（download.page）の所属グループで決める。
        token 照合は不要（ブラウザ本体が発火するイベントで、ページ側から詐称できない）。
        """
        name = download.suggested_filename or "download"
        # 発生元ページの系譜を解決（取れなければ未採番＝空 として output_dir/downloads へ）。
        group_id = ""
        try:
            page = download.page
            if page is not None:
                group_id = (await self._resolve_group(page)).id
        except Exception:
            group_id = ""
        dl_dir = _downloads_dir(self.config, group_id)
        try:
            dl_dir.mkdir(parents=True, exist_ok=True)
            target = _unique_path(dl_dir, name)
            await download.save_as(str(target))
            log(f"[DL] {group_folder_name(group_id)} 保存しました: {target.name}" if group_id
                else f"[DL] 保存しました: {target.name}")
        except Exception as e:
            # 例: 保存前にウィンドウを閉じられ一時ファイルが消えた等。無言にはしない。
            log(f"[DL] ダウンロードの保存に失敗しました: {name} ({e})")

    # ---- セットアップと監視ループ ----

    async def setup(self) -> None:
        """ページ内から呼ぶコールバックを公開し、操作バーを全ページへ注入する。

        context 単位なので以後開く新規タブにも自動適用される
        （expose_binding は add_init_script より前に登録する）。
        起動時点で開いているページを root として登録し、その最初のグループだけは
        start_recording に従う（以後手動で開かれるタブは on_new_page で初期OFFの独立グループになる）。
        """
        # 起動時の各ページ（通常は1枚）を root とし、start_recording に従うグループを用意する。
        # あわせて URL変化・消滅の監視を配線する（この後 main() が行う start_url への goto の
        # framenavigated も拾えるよう、goto より前に張っておく。B-1）。
        for pg in self.context.pages:
            self.page_root[pg] = pg
            self.groups[pg] = self._make_group(
                on=self.config.start_recording,
                spa_on=False,
                selector=self.config.target_selector,
            )
            self._track_page(pg)
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
        # 新規ページ（ポップアップ・手動タブ）の所属グループを開いた時点で確定する。
        self.context.on("page", self.on_new_page)

    async def on_new_page(self, page) -> None:
        """新しく開いたページの所属グループを確定し、URL変化・消滅の監視を配線する（B-1）。

        opener を辿って既存グループへ合流させるか、無ければ初期OFFの独立グループを作る
        （_resolve_group がメモ化まで行う）。合流先グループが記録中なら、そのページを即撮り
        する（撮り始めの体感を良くする）。以後の遷移は framenavigated が拾う。
        """
        self._track_page(page)
        await self._shoot_if_changed(page)

    # ---- URL変化・消滅のイベント配線（B-1: ポーリング廃止） ----

    def _track_page(self, page) -> None:
        """このページの URL 変化（framenavigated）と消滅（close）をイベントで拾う。

        起動時から開いているページは setup() が、以後開くページは on_new_page が呼ぶ。
        二重配線を防ぐため _tracked で一度きりにする。framenavigated は同一ドキュメント
        遷移（history.pushState 等）でも発火するため、SPA のURLルーティングも拾える。
        """
        if page in self._tracked:
            return
        self._tracked.add(page)
        page.on("framenavigated", lambda frame, pg=page: self._on_navigated(pg, frame))
        page.on("close", lambda *_a, pg=page: self._on_page_closed(pg))

    async def _on_navigated(self, page, frame) -> None:
        """ページの遷移通知。メインフレームの遷移のときだけ、変化していれば撮る。

        子フレーム（iframe 等）の遷移では撮らない（ページ本体のURLは変わっていない）。
        """
        if frame is not page.main_frame:
            return
        await self._shoot_if_changed(page)

    async def _shoot_if_changed(self, page) -> None:
        """記録ONのグループのページを、前回と違うURLになっていれば1枚撮る。

        フラグメント（#...）だけの変化（scroll-spy）は撮り直さない。記録OFFのグループでは
        seen を更新しないので、ON にした瞬間に現在ページが「変化」として検知され撮れる
        （on_toggle でも即撮りするため通常は先回り）。skip_urls と url 取得失敗を弾く。
        """
        grp = await self._resolve_group(page)
        if not grp.on:
            return
        try:
            url = page.url
        except Exception:
            return
        if url in self.config.skip_urls:
            return
        key = _url_key(url)
        if self.seen.get(page) != key:
            self.seen[page] = key
            self.runner.spawn(page, url, self.config, grp.selector, grp.id)

    def _on_page_closed(self, page) -> None:
        """閉じられたページを管理から除去する（毎tickの _prune を置き換え）。

        seen / page_root / _tracked から消し、どの生存ページからも参照されなくなった
        root のグループ状態も捨てる（root ページ自身が閉じても、ポップアップが残る間は保持する）。
        """
        self.seen.pop(page, None)
        self.page_root.pop(page, None)
        self._tracked.discard(page)
        alive_roots = set(self.page_root.values())
        for root in list(self.groups):
            if root not in alive_roots:
                del self.groups[root]

    async def run(self, closed: asyncio.Event) -> None:
        """ブラウザのウィンドウが閉じるまで待つ（イベント駆動。ポーリング無し。B-1/B-2）。

        URL変化は page.on("framenavigated")、新規タブは context.on("page")、ページ消滅は
        page.on("close")、中身変化（SPA検知）は __eac_spa_changed（on_spa_changed）が、
        それぞれイベントで保存を要求する。このメソッド自身は毎tickの URL 比較も状態配布も
        行わず、閉じるまで待つだけ（poll_interval は廃止）。
        """
        # 起動直後の一掃: 既に読み込み済みで今後 framenavigated が来ない初期ページを、
        # 記録ONなら1枚撮る（旧ループの最初のtick相当）。start_url への goto が既に
        # framenavigated を出して撮っていれば seen で重複を弾く。念のため配線も冪等に確認。
        for pg in list(self.context.pages):
            self._track_page(pg)
            await self._shoot_if_changed(pg)

        await closed.wait()


async def main(config: Config) -> None:
    # output_dir は load_config で書き込み可能な場所へ解決済み（D-C1）だが、その後に
    # 消される等の可能性もあるため裸で放置せず、失敗したら無言終了ではなく通知して抜ける。
    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        # 撮影物・ダウンロードは系譜（lineage）ごとの output_dir/lineage-<id>/ 以下へ保存する。
        # これらのサブフォルダは保存時に必要に応じて作るため、ここでは output_dir だけ用意する。
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

        # ブラウザのウィンドウを閉じたら監視ループを抜けるためのフラグ。
        # playwright は close を「context を引数に」発火する（emit(Events.Close, self)）。
        # 0 引数ラムダだと呼び出し時に TypeError となり closed.set() が走らず、
        # run() の await closed.wait() を抜けられない。引数を捨てる *_ で受ける。
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())

        try:
            # 最初のページで start_url を開く（about:blank ならそのまま）
            page = context.pages[0] if context.pages else await context.new_page()
            # setup() は起動時に存在したページだけを種入れする。ページが1枚も無く
            # ここで作った場合に備え、その1枚を start_recording に従う root グループにする。
            if page not in session.page_root:
                session.page_root[page] = page
                session.groups[page] = session._make_group(
                    on=config.start_recording, spa_on=False, selector=config.target_selector
                )
            if config.start_url and config.start_url != "about:blank":
                try:
                    await page.goto(config.start_url)
                except Exception as e:
                    log(f"[skip goto] {config.start_url}  ({e})")

            log(
                f"記録は{'ON' if config.start_recording else 'OFF（待機）'}で開始しました。"
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
