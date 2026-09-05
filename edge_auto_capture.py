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
  - lineage.py           … タブ系譜（lineage）の識別・保存先規約と解決レジストリ。
  - browser.py           … Edge/Chrome の起動候補と起動オプションの組み立て。
  - downloads.py         … ダウンロードの退避（保存先規約・衝突回避・save_as 実行）。

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
import secrets
import shutil
import sys
import tempfile
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

from playwright.async_api import Page, async_playwright

import badge
import downloads
from browser import browser_candidates, browser_launch_kwargs
from capture import (
    CaptureRequest,
    CaptureRunner,
    try_eval,
)
from config import Config, ConfigFatalError, load_config, should_capture, summarize_config
from infra import (
    __version__,
    acquire_single_instance_lock,
    cleanup_old_profiles,
    log,
    notify_fatal,
    open_in_file_manager,
    startup_environment_line,
)
from lineage import (
    GroupState,
    LineageRegistry,
    group_folder_name,
    make_group,
)


def _url_key(url: str) -> str:
    """URL変化の「同じページか」判定に使う比較キー。フラグメント（#...）を除く。

    多くのドキュメントSPA（例: Vuetify）は scroll-spy で、スクロールに追従して URL の
    ハッシュ（#見出し）だけを書き換える。ハッシュ違いは同じドキュメントなので、#以降を
    落として「同じページ」とみなし、スクロールのたびの二重撮りを防ぐ。ハッシュルーティング
    型SPAの本当の中身変化は SPA検知（本文署名）が担うため、ここで落としても取りこぼさない。
    """
    return url.split("#", 1)[0]


# セレクタ履歴（F-D2）の保持上限。datalist の候補が無限に伸びないよう頭打ちにする。
# 新しい値を先頭に積み、上限を超えた古い値から落とす。
SELECTOR_HISTORY_MAX = 20


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
        # Python→ページのヘルパ（applyState/captureStart 等）を収める window プロパティ名（E-3）。
        # 起動ごとにランダム生成し、固定名を window に生やさないことでサイトからの存在検知を防ぐ。
        # badge.js へ埋め込み、各呼び出し式（badge.*_call）にもこの名前を渡す。
        self.ns = badge.new_namespace()
        # 撮影の実行器（実行中タスク・ページ単位ロックを own する）。1 セッションに 1 個。
        # 撮影 1 回ごとの成否は on_result で受け、撮影カウンタ／失敗把握に使う（F-D3）。
        self.runner = CaptureRunner()
        self.runner.on_result = self._on_capture_result
        # 撮影の合図（captureStart/captureEnd）と本文取得（bodyText）を呼ぶときの名前空間を渡す。
        self.runner.ns = self.ns
        # 本セッションで保存できた枚数（F-D3）。動作実感＋暴走の早期発見のため全バーへ配る。
        # 本体はここに持ち、成功のたびに増やして全ページの操作バーへ反映する。
        self.shots = 0
        # 過去に確定したセレクタの履歴（F-D2）。入力欄の datalist 候補として全バーへ配る。
        # 新しいものが先頭・重複なし・上限あり（下の _remember_selector）。グループ横断で共有する
        # （どのタブで入れた値でも次に別タブで使い回せる方が実用的なので、あえて session 単位）。
        self.selector_history: list[str] = []
        # --- グループ単位の実行時状態 ---
        # 記録ON/OFF・SPA検知・セレクタは「タブ系譜（グループ）」ごとに独立して持つ。系譜の
        # 状態（groups / page_root）と解決ロジックは lineage.LineageRegistry へ寄せてある（#36）。
        # 新規タブに与える既定セレクタは config.target_selector（レジストリが握って使う）。
        # 採番（resolve）・種入れ（seed_root）・後始末（release）は全てレジストリの API で行い、
        # こちら側は下のプロパティで読み取り専用ビューだけを公開する（#48）。
        self._lineage = LineageRegistry(config.target_selector)
        # --- ページごとの追跡情報 ---
        self.seen: dict[Page, str] = {}           # page -> 直近のURL
        # framenavigated / close を配線済みのページ（二重配線を防ぐ。B-1）
        self._tracked: set[Page] = set()

    # 系譜の状態はレジストリが own する。ここは参照（in 判定・取り出し・列挙）専用の窓で、
    # 書き換えはレジストリの API（resolve / seed_root / release）に限る（#48）。
    # 以前は内部辞書をそのまま返していたため _on_page_closed が外から直接書き換えていて、
    # 「系譜の生存管理」だけが 2 モジュールに分かれていた。MappingProxyType で包むのは、
    # うっかり `session.groups[k] = v` と書いたときに黙って通らずその場で落とすため。
    @property
    def groups(self) -> Mapping[Page, GroupState]:
        return MappingProxyType(self._lineage.groups)

    @property
    def page_root(self) -> Mapping[Page, Page]:
        return MappingProxyType(self._lineage.page_root)

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
            await try_eval(
                pg,
                badge.apply_state_call(self.ns, grp.on, grp.spa_on, grp.selector),
            )

        await asyncio.gather(*(_apply(pg) for pg in list(self.context.pages)))

    async def _on_capture_result(self, ok: bool) -> None:
        """撮影 1 回分の成否を受け、撮影カウンタを更新して全バーへ配る（F-D3）。

        runner から撮影 1 回ごとに呼ばれる。1 種でも保存できた（ok=True）ときだけ枚数を増やす
        （全滅は「撮れた 1 枚」に数えない。フラッシュ側も失敗色にして区別する）。枚数は全ページ
        共通の本セッション累計なので、更新のたびに全ページの操作バーへ同じ値を配る。バーが
        サイト側の再描画で作り直されても __eac_getstate（count 同梱）で自己同期する。
        """
        if not ok:
            return
        self.shots += 1
        await self._push_count()

    async def _broadcast(self, call: str) -> None:
        """1 つのページ側呼び出し式を、開いている全ページの操作バーへ配る。

        撮影カウンタ（F-D3）・セレクタ履歴（F-D2）のように「全ページ共通の値」を配る経路の
        共通部分。ページ数ぶんを直列に待たず asyncio.gather で並列に流し、個々の失敗は
        try_eval が握る（閉じかけのページが混ざっても他ページへの配布は止まらない）。
        値がグループごとに違う refresh_panels は式をページ単位で組み立てるため、ここは通さない。
        """
        await asyncio.gather(*(try_eval(pg, call) for pg in list(self.context.pages)))

    async def _push_count(self) -> None:
        """現在の撮影カウンタ（本セッション枚数）を開いている全ページの操作バーへ配る（F-D3）。"""
        await self._broadcast(badge.set_count_call(self.ns, self.shots))

    def _remember_selector(self, value: str) -> bool:
        """確定したセレクタを履歴へ積む（F-D2）。新規に積んだら True、変化なしなら False。

        新しい値を先頭に置き、重複は先頭へ繰り上げ（＝最近使った順）、上限
        SELECTOR_HISTORY_MAX で古い方から落とす。空文字（クリア）は積まない。
        """
        v = (value or "").strip()
        if not v:
            return False
        if self.selector_history and self.selector_history[0] == v:
            return False  # 直近と同じなら並びも配布も変わらない
        # 既にあれば一旦除いて先頭へ繰り上げる（重複を作らず最近使った順を保つ）。
        if v in self.selector_history:
            self.selector_history.remove(v)
        self.selector_history.insert(0, v)
        del self.selector_history[SELECTOR_HISTORY_MAX:]
        return True

    async def _push_history(self) -> None:
        """現在のセレクタ履歴（datalist 候補）を開いている全ページの操作バーへ配る（F-D2）。"""
        await self._broadcast(badge.set_history_call(self.ns, self.selector_history))

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

    async def _resolve_group(self, page) -> GroupState:
        """page が属するグループの状態を返す（LineageRegistry へ委譲）。無ければ新規 OFF で採番。"""
        return await self._lineage.resolve(page)

    def _group_pages(self, root: Page) -> list[Page]:
        """root を共有する現存ページ（＝同じグループのページ）を返す。"""
        return [pg for pg in self.context.pages if self.page_root.get(pg) is root]

    def _shoot(self, pg, grp: "GroupState", trigger: str) -> Optional[str]:
        """1ページを撮る。url 取得失敗と撮影対象外 URL を弾き、撮れば url を返す（弾けば None）。

        「url 取得 → 撮影可否判定 → runner.spawn」の定型を1か所に集約する（各コールバックと監視
        ループで同じ並びを書かないため。R3b）。撮影可否は should_capture に一元化（skip_urls /
        allow_urls / 前方一致・fnmatch。R3/F-C2/B-5）。記録状態のゲートは呼び出し側の責務（ここでは見ない）。
        撮影対象の抜き出しセレクタは、そのページが属するグループの selector を使う。
        trigger は撮影契機（"manual"/"url"/"spa"）で、CaptureRequest に載せて索引 CSV まで通す（F-A1）。
        """
        try:
            url = pg.url
        except Exception:
            return None
        if not should_capture(url, self.config):
            return None
        self.runner.spawn(CaptureRequest(pg, url, self.config, grp.selector, grp.id, trigger))
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
                # 記録開始時の即撮りは URL 追跡の基準（seen）を張る初回撮影なので契機は "url"。
                url = self._shoot(pg, grp, "url")
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
        url = self._shoot(source["page"], grp, "manual")
        if url is not None:
            log(f"[手動] {group_folder_name(grp.id)}  {url}")

    async def on_open_folder(self, source, token=None) -> None:
        """「保存先」ボタン: 撮影物・ダウンロード・log.txt の保存先フォルダを開く（F-D4）。

        開くのは起動単位のセッションフォルダ（config.output_dir。F-C3 で 1 段挟んだ場所）。
        記録状態やグループには依存しない（どのページのバーから押しても同じ保存先を開く）ので
        グループ解決はしない。OS のファイルマネージャで開く操作はローカルで完結し、閲覧中の
        サイトへは何も送らない。token 不一致（操作バー以外）は黙って無視する。
        """
        if not self._authorized(token):
            return
        target = self.config.output_dir
        ok = open_in_file_manager(target)
        log(f"[フォルダ] {'開きました' if ok else '開けませんでした'}: {target}")

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
        url = self._shoot(source["page"], grp, "spa")
        if url is not None:
            log(f"[SPA変化] {group_folder_name(grp.id)}  {url}")

    async def on_commit_selector(self, source, token=None, value="") -> None:
        """セレクタ入力の確定（blur / Enter）。最終値をログに残し、履歴（datalist）へ積む。

        入力のたびに出すとログが氾濫するため、確定時にだけ実際に使う値を記録する。
        これにより「どのセレクタで動かしたか」がログと実態で一致する。あわせて確定値を
        セレクタ履歴へ積み（F-D2）、変化があれば全バーの入力候補（datalist）を更新する。
        履歴はグループ横断で共有する（別タブで入れた値も次の入力欄で使い回せる）。
        """
        if not self._authorized(token):
            return
        grp = await self._resolve_group(source["page"])
        new = (value or "").strip()
        log(f"[セレクタ] {'クリア' if not new else repr(new)} {group_folder_name(grp.id)}")
        if self._remember_selector(new):
            await self._push_history()

    async def get_state(self, source, token=None) -> dict:
        """操作バーが描画前に現在の状態を問い合わせるためのバインディング。

        ページ遷移直後、新しいドキュメントのバーはこれを見てから描画するので、
        記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。SPA検知の
        ON/OFF・セレクタ値・撮影カウンタ（count）も同時に返し、遷移後も入力欄・ボタン・
        枚数表示を正しく初期化する（作り直したバーが 0 枚へ戻って見えないように。F-D3）。

        token 不一致（操作バー以外からの問い合わせ）には既定状態を返し、実際の
        記録状態やセレクタ値を外部スクリプトへ漏らさない（枚数は秘匿情報ではないので返す。
        セレクタ履歴は利用者自身が入れた候補で、非正規呼び出しには返さない）。

        返すのは問い合わせ元ページが属するグループの状態。新しいドキュメントのバーが最初に
        これを呼ぶタイミングでグループを確定・メモ化する（監視ループ到達前でも取りこぼさない）。
        セレクタ履歴（datalist 候補。F-D2）も同時に返し、遷移後も入力候補を保つ。
        """
        if not self._authorized(token):
            return {
                "recording": False, "spa": False, "selector": "",
                "count": self.shots, "history": [],
            }
        grp = await self._resolve_group(source["page"])
        return {
            "recording": grp.on, "spa": grp.spa_on, "selector": grp.selector,
            "count": self.shots, "history": list(self.selector_history),
        }

    # ---- ダウンロードの退避（E-4） ----

    async def on_download(self, download) -> None:
        """発生元ページの系譜を解決し、保存の本体（downloads.save）へ委譲する（E-4）。

        系譜解決はページ集合の状態（_resolve_group）を持つこちらの責務、保存先規約・
        連番衝突回避・save_as の実行は downloads.py 側の責務（#59 でそちらへ切り出した）。
        どの系譜かは発生元ページ（download.page）の所属グループで決める。
        token 照合は不要（ブラウザ本体が発火するイベントで、ページ側から詐称できない）。
        """
        # 発生元ページの系譜を解決（取れなければ未採番＝空 として output_dir/downloads へ）。
        group_id = ""
        try:
            page = download.page
            if page is not None:
                group_id = (await self._resolve_group(page)).id
        except Exception:
            group_id = ""
        await downloads.save(download, self.config, group_id)

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
            self._lineage.seed_root(
                pg,
                make_group(
                    on=self.config.start_recording,
                    spa_on=False,
                    selector=self.config.target_selector,
                ),
            )
            self._track_page(pg)
        # バインディング名は badge.py の BIND_* に集約（badge.js 側の呼び出し名と一致）。
        await self.context.expose_binding(badge.BIND_TOGGLE, self.on_toggle)
        await self.context.expose_binding(badge.BIND_SHOT, self.on_shot)
        await self.context.expose_binding(badge.BIND_OPEN_FOLDER, self.on_open_folder)
        await self.context.expose_binding(badge.BIND_SPA_TOGGLE, self.on_spa_toggle)
        await self.context.expose_binding(badge.BIND_SET_SELECTOR, self.on_set_selector)
        await self.context.expose_binding(badge.BIND_COMMIT_SELECTOR, self.on_commit_selector)
        await self.context.expose_binding(badge.BIND_SPA_CHANGED, self.on_spa_changed)
        await self.context.expose_binding(badge.BIND_GETSTATE, self.get_state)
        # badge.js には今回の合言葉（token）と SPA検知のデバウンス時間（settle_delay をミリ秒へ）を
        # 埋め込む。token は各バインディング呼び出しの照合、settle は落ち着き判定に使う。
        settle_ms = int(self.config.settle_delay * 1000)
        await self.context.add_init_script(
            badge.build_badge_script(self.token, settle_ms, self.config.hide_selectors, self.ns)
        )
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
        （on_toggle でも即撮りするため通常は先回り）。撮影対象外 URL と url 取得失敗は _shoot が弾く。
        """
        grp = await self._resolve_group(page)
        if not grp.on:
            return
        try:
            url = page.url
        except Exception:
            return
        # 変化ゲート（seen 比較）は当所の責務。skip 判定と spawn は _shoot に委譲する（R3b）。
        # 撮れなかった（撮影対象外）ときは seen を更新せず、次に撮れる URL まで撮り直しを待つ。
        key = _url_key(url)
        if self.seen.get(page) != key and self._shoot(page, grp, "url") is not None:
            self.seen[page] = key

    def _on_page_closed(self, page) -> None:
        """閉じられたページを管理から除去する（毎tickの _prune を置き換え）。

        このセッションが持つ追跡情報（seen / _tracked）から消し、系譜側の後始末（page_root の
        メモと、どの生存ページからも参照されなくなった root のグループ状態の破棄）はレジストリの
        release() へ任せる（root ページ自身が閉じても、ポップアップが残る間は保持される。#48）。
        """
        self.seen.pop(page, None)
        self._tracked.discard(page)
        self._lineage.release(page)

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


def _prepare_output_dir(config: Config) -> bool:
    """保存先フォルダを用意する。作れたら True、作れなければ通知して False を返す。

    output_dir は load_config が書き込み可能な場所へ解決済み（D-C1）で、起動単位の
    セッションフォルダ（F-C3。例: .../output/2026-08-11_143025）まで含んでいる。その後に
    消される等の可能性もあるため裸で放置せず、失敗したら無言終了ではなく通知する
    （中断するかどうかは呼び出し側が決める）。撮影物・ダウンロードを収める系譜ごとの
    lineage-<id>/ は保存時に必要に応じて作るため、ここでは output_dir だけ用意する。
    """
    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        notify_fatal(f"保存先フォルダを作成できませんでした: {config.output_dir}\n({e})")
        return False


def _prepare_profile_dir(config: Config) -> tuple[str, bool]:
    """ブラウザプロファイルの置き場所を決め、(パス, 使い捨てか) を返す。

    - profile_dir 指定あり: そのフォルダを再利用する（ログイン状態などを保持）。削除しない。
    - profile_dir 未指定（既定）: 毎回まっさらな一時プロファイルを作る。

    どちらの経路でも、前回までに残った使い捨てプロファイル（edge-debug-*）の掃除を行う。
    再利用プロファイルは命名も置き場所も別なので glob に一致しないが、念のため keep で
    除外指定も渡す。返す第 2 要素が True のとき、呼び出し側は終了時にそのフォルダを
    消す責務を負う（後始末の所在をここで一意に決める）。
    """
    if config.profile_dir:
        user_data_dir = config.profile_dir
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        cleanup_old_profiles(keep=Path(user_data_dir))
        return user_data_dir, False
    cleanup_old_profiles()
    return tempfile.mkdtemp(prefix="edge-debug-"), True   # 今回用の一時プロファイル


async def _launch_browser(p, config: Config, user_data_dir: str):
    """候補ブラウザを優先順に試して persistent context を返す。全滅なら通知して None を返す。

    候補は browser.browser_candidates が決める（config.browser 指定があればその 1 つだけ、
    空なら Edge→Chrome の順）。起動できたら採用ブラウザの実バージョンをログへ残す（D-B2。
    Edge/Chrome の更新で挙動が変わったとき log.txt だけで版を追えるようにする。取得に
    失敗しても起動は妨げない）。全候補が失敗したときは、試した順と各失敗理由を添えて
    ダイアログとログで知らせる（無言終了にしない）。

    一時プロファイルの後始末はここでは行わない（作った側＝呼び出し側の責務）。
    """
    candidates = browser_candidates(config)
    errors: list[str] = []
    for channel, label, executable_path in candidates:
        try:
            context = await p.chromium.launch_persistent_context(
                **browser_launch_kwargs(config, user_data_dir, channel, executable_path)
            )
        except Exception as e:
            # 未インストール等で起動できなければ次の候補へ回す（候補が1つなら終了）。
            log(f"[skip] {label} を起動できませんでした: {e}")
            errors.append(f"- {label}: {e}")
            continue
        log(f"{label} を起動しました。")
        try:
            browser = context.browser
            log(f"[env] {label} version={browser.version if browser else '不明'}")
        except Exception as e:
            log(f"[env] {label} version 取得失敗: {e}")
        return context

    tried = " / ".join(label for _, label, _ in candidates)
    notify_fatal(
        f"ブラウザを起動できませんでした（試行: {tried}）。\n"
        f"{tried} がインストールされているか確認してください。\n"
        + "\n".join(errors)
    )
    return None


async def main(config: Config) -> None:
    """起動から監視終了までの流れ。各段の中身は上のヘルパへ委ね、ここは順序だけを持つ。"""
    if not _prepare_output_dir(config):
        return

    user_data_dir, ephemeral = _prepare_profile_dir(config)

    async with async_playwright() as p:
        context = await _launch_browser(p, config, user_data_dir)
        if context is None:
            if ephemeral:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            return

        # 監視セッションを組む前に、操作対象の1枚を必ず用意しておく。setup() は「起動時に
        # 開いているページ」を root グループとして種入れし（start_recording に従う）、URL変化・
        # 消滅の監視まで配線するので、先に作っておけばページが1枚も無い環境でもその1枚が
        # 同じ経路に乗る。setup() の後に作ると、setup() が張った context.on("page") 経由で
        # on_new_page が先に走りえて、start_recording ではなく初期OFFの独立グループとして
        # 採番されてしまう（種入れの重複実装もそこから生まれていた）。
        if not context.pages:
            await context.new_page()

        session = CaptureSession(context, config)
        await session.setup()

        # ブラウザのウィンドウを閉じたら監視ループを抜けるためのフラグ。
        # playwright は close を「context を引数に」発火する（emit(Events.Close, self)）。
        # 0 引数ラムダだと呼び出し時に TypeError となり closed.set() が走らず、
        # run() の await closed.wait() を抜けられない。引数を捨てる *_ で受ける。
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())

        try:
            # 最初のページで start_url を開く（about:blank ならそのまま）。種入れと監視配線は
            # setup() が済ませてある（上で 1 枚を確保済み）。ここへ来るまでにその 1 枚が閉じられた
            # 場合だけ新しく開くが、その 1 枚は setup() が張った on_new_page が拾って採番する。
            page = context.pages[0] if context.pages else await context.new_page()
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


def cli(run: Callable[[Coroutine[Any, Any, None]], None] = asyncio.run) -> int:
    """起動シーケンス（設定読み込み・起動ログ・多重起動抑止・監視の実行）。終了コードを返す。

    `if __name__ == "__main__":` へ直書きしていた約 28 行をここへ括り出したもの（#50）。
    プロセスを落とすのは `__main__` の `sys.exit(cli())` 1 行だけにして、この関数は
    「何をどの順でやり、どの終了コードになるか」を返す純粋な手続きに保つ。こうしておくと、
    実ブラウザ無しでもロック取得・ログの順序・終了コードを回帰テストで押さえられる
    （多重起動抑止 D-C4 は失敗すると保存先を 2 プロセスで奪い合う、実害のある分岐）。

    run は `asyncio.run` の差し替え口。テストからブラウザを起こさずに、ここまでの
    シーケンスが正しく組まれているかだけを確かめるために開けてある。

    **順序に意味がある**（変えないこと）:
      - `load_config()` より前のログは BASE_DIR/log.txt へ出る（load_config が
        output_dir 確定時に set_log_dir を呼んで、そこで初めて出力先が切り替わる）。
      - ロック取得は「ブラウザを起こす前・cleanup_old_profiles が走る前」。
    """
    # 先に設定を読み、ログの出力先を保存先（output_dir）へ切り替えてから記録を始める。
    # （load_config が output_dir 確定時に set_log_dir を呼ぶ。以後のログはそこへ残る）
    #
    # 設定を読み切れないとき（保存先がどこにも書けない／数値項目が不正）は config が
    # ConfigFatalError を投げる。ライブラリ層はプロセスを落とさないので、通知と終了は
    # 入口のここ 1 か所で決める（#49）。文面は例外に載っているものをそのまま出す。
    try:
        config = load_config()
    except ConfigFatalError as e:
        notify_fatal(str(e))
        return 1

    # ログは追記のみ（既存があればそのまま末尾へ足す。削除・作り直しはしない）。
    log(f"=== edge-auto-capture v{__version__} 起動 ===")
    # 環境情報と採用設定値を起動時に1行ずつ残す（D-B2）。切り分けが楽になる。
    # ブラウザの実バージョンは起動後に main() 内で別行として出す。
    log(startup_environment_line())
    log(summarize_config(config))

    # 多重起動を入口で止める（D-C4）。二度押しで 2 つ目が起動すると、output/・
    # log.txt・使い捨てプロファイル（A-5）を先行インスタンスと奪い合うため、
    # ブラウザを起こす前・掃除（cleanup_old_profiles）が走る前にここで抑止する。
    if not acquire_single_instance_lock():
        notify_fatal(
            "edge-auto-capture はすでに起動しています。\n"
            "二重に起動することはできません。すでに開いているウィンドウをご確認ください。"
        )
        log("=== 終了（多重起動を抑止） ===")
        return 1

    try:
        run(main(config))
    except KeyboardInterrupt:
        # コンソール実行時のみ届く保険的な停止経路。
        log("停止しました。")
    log("=== 終了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
