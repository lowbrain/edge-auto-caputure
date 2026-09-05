"""タブ系譜（lineage）モジュールのユニットテスト。

保存先の規約（group_stamp / group_folder_name / group_subdir / make_group）と、実行時レジストリ
（LineageRegistry の find_root / resolve / seed_root / release）を、実 Edge 無しで直接固定する。

系譜の判定を間違えると「保存先フォルダが分かれる／混ざる」という利用者に見える壊れ方をするのに、
これまでの回帰の網は CaptureSession 経由の間接検証（tests/test_session.py）だけだった。
opener 連鎖の遡り・既知 root への合流・opener 失敗時のフォールバックといった分岐を、
ブラウザ相当のフェイクを組んだ大きな文脈ではなくここで直接押さえる（#53）。

実行:
    pip install -e ".[dev]"
    pytest
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

import lineage
from lineage import (
    GroupState,
    LineageRegistry,
    group_folder_name,
    group_stamp,
    group_subdir,
    make_group,
)


class _Page:
    """opener() を持つ最小のページ代役（tests/test_session.py の _GroupPage と同じ役割）。

    opener() の呼び出し回数を数える。メモ化（2 回目は opener を辿り直さない）と、
    既知 root で連鎖の遡りが止まること（その先の opener を呼ばないこと）の検証に使う。
    """

    def __init__(self, name: str, opener=None) -> None:
        self.name = name
        self._opener = opener
        self.opener_calls = 0

    async def opener(self):
        self.opener_calls += 1
        return self._opener

    def __repr__(self) -> str:
        return f"<Page {self.name}>"


class _BrokenPage(_Page):
    """opener() が例外を投げるページ代役（系譜を辿れないページ）。

    実際には「ページ/ブラウザが既に閉じられている」等で opener() が落ちる。
    """

    async def opener(self):
        self.opener_calls += 1
        raise RuntimeError("target page, context or browser has been closed")


# --------------------------------------------------------------------------- #
# find_root … opener 連鎖から所属グループの root を決める
# --------------------------------------------------------------------------- #


def test_find_root_walks_opener_chain_to_the_top():
    # ポップアップのポップアップ（孫）でも、opener を遡って一番上の root へ行き着く。
    async def scenario():
        root = _Page("root")
        child = _Page("child", opener=root)
        grand = _Page("grand", opener=child)
        reg = LineageRegistry(default_selector="")

        assert await reg.find_root(grand) is root

    asyncio.run(scenario())


def test_find_root_returns_terminal_page_when_opener_is_none():
    # opener が None（手動で開いたタブ）なら、その末端ページ自身が root。
    async def scenario():
        page = _Page("manual", opener=None)
        reg = LineageRegistry(default_selector="")

        assert await reg.find_root(page) is page

    asyncio.run(scenario())


def test_find_root_stops_at_known_root():
    # 既知の root（page_root に載っているページ）に達したらそこで打ち切る。
    # メモ済みのページの opener は呼ばない（既に確定した所属をたどり直さない）。
    async def scenario():
        root = _Page("root")
        child = _Page("child", opener=root)
        grand = _Page("grand", opener=child)
        reg = LineageRegistry(default_selector="")
        await reg.resolve(child)  # child -> root をメモ化させる

        assert await reg.find_root(grand) is root
        assert child.opener_calls == 1  # resolve 時の 1 回だけ（遡りで再度は呼ばない）

    asyncio.run(scenario())


def test_find_root_merges_into_group_of_known_root():
    # seed_root で種入れ済みの root へ、後から開いたポップアップが合流する。
    # root 自身は既知なので、root の opener は一度も呼ばれない。
    async def scenario():
        root = _Page("root")
        popup = _Page("popup", opener=root)
        reg = LineageRegistry(default_selector="")
        reg.seed_root(root, GroupState(on=True, spa_on=False, selector=""))

        assert await reg.find_root(popup) is root
        assert root.opener_calls == 0

    asyncio.run(scenario())


def test_find_root_falls_back_to_page_when_opener_raises():
    # opener 取得に失敗したページは、それ自身を独立グループの起点にする
    # （系譜を辿れないページを、たまたま近い別系譜へ混ぜてしまわないため）。
    async def scenario():
        broken = _BrokenPage("broken")
        reg = LineageRegistry(default_selector="")

        assert await reg.find_root(broken) is broken

    asyncio.run(scenario())


def test_find_root_falls_back_at_the_page_that_failed_midway():
    # 連鎖の途中で opener が落ちたら、その時点のページ（root ではない）を root 扱いにする。
    async def scenario():
        root = _Page("root")
        broken = _BrokenPage("broken", opener=root)
        grand = _Page("grand", opener=broken)
        reg = LineageRegistry(default_selector="")

        assert await reg.find_root(grand) is broken

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# resolve … ページ→グループ状態の解決・メモ化・未知 root の採番
# --------------------------------------------------------------------------- #


def test_resolve_creates_off_group_for_unknown_root():
    # まだグループの無い root（手動で開かれた新規タブ）は、初期OFF の独立グループになる
    # （無関係タブを勝手に撮らない）。id は採番され、保存先フォルダ名に使える。
    async def scenario():
        page = _Page("manual")
        reg = LineageRegistry(default_selector="")

        grp = await reg.resolve(page)
        assert (grp.on, grp.spa_on) == (False, False)
        assert re.fullmatch(r"\d{17}", grp.id)
        assert reg.groups[page] is grp
        assert reg.page_root[page] is page

    asyncio.run(scenario())


def test_resolve_applies_default_selector_to_new_groups():
    # 新規グループの初期セレクタは default_selector（config.target_selector 由来）。
    async def scenario():
        reg = LineageRegistry(default_selector="#main")

        grp = await reg.resolve(_Page("manual"))
        assert grp.selector == "#main"

    asyncio.run(scenario())


def test_resolve_memoizes_page_root():
    # 一度解決したページは page_root のメモから即答する（opener を辿り直さない）。
    async def scenario():
        root = _Page("root")
        popup = _Page("popup", opener=root)
        reg = LineageRegistry(default_selector="")

        first = await reg.resolve(popup)
        assert popup.opener_calls == 1
        second = await reg.resolve(popup)
        assert second is first          # 同じグループ状態を返す
        assert popup.opener_calls == 1  # 2 回目は opener を呼ばない

    asyncio.run(scenario())


def test_resolve_joins_popup_into_the_root_group():
    # ポップアップは root のグループへ合流し、状態（記録ON/SPA/セレクタ）を共有する。
    async def scenario():
        root = _Page("root")
        popup = _Page("popup", opener=root)
        reg = LineageRegistry(default_selector="")
        seeded = GroupState(on=True, spa_on=True, selector="#x")
        reg.seed_root(root, seeded)

        grp = await reg.resolve(popup)
        assert grp is seeded                  # 新規採番せず合流する
        assert reg.page_root[popup] is root
        assert list(reg.groups) == [root]     # グループは増えない

    asyncio.run(scenario())


def test_resolve_gives_independent_groups_to_unrelated_tabs():
    # opener で繋がらない 2 タブは、それぞれ自分が root の別グループになる（状態を共有しない）。
    # id の比較はしない: 同じミリ秒に採番されると一致しうる（時刻由来なので当然）。
    async def scenario():
        reg = LineageRegistry(default_selector="")
        a, b = _Page("a"), _Page("b")

        ga = await reg.resolve(a)
        gb = await reg.resolve(b)
        assert ga is not gb
        assert (reg.page_root[a], reg.page_root[b]) == (a, b)

        ga.on = True
        assert gb.on is False  # 片方の記録ONは他方へ波及しない

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# seed_root … 起動時ページの種入れ（CaptureSession.setup() が使う）
# --------------------------------------------------------------------------- #


def test_seed_root_registers_page_as_its_own_root():
    # 起動時ページは start_recording に従うグループを呼び出し側が作って預ける。
    # 以後 resolve しても再採番されず、そのグループがそのまま返る。
    async def scenario():
        page = _Page("startup")
        reg = LineageRegistry(default_selector="")
        grp = GroupState(on=True, spa_on=False, selector="#main")
        reg.seed_root(page, grp)

        assert reg.page_root[page] is page
        assert await reg.resolve(page) is grp

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# release … 閉じたページの後始末（#48 でここへ移した）
# --------------------------------------------------------------------------- #


def test_release_keeps_group_while_another_page_of_the_lineage_survives():
    # root ページ自身が閉じても、ポップアップが残る間はグループ状態を保持する
    # （残ったページの記録ON/セレクタが巻き戻らないようにするため）。
    async def scenario():
        root = _Page("root")
        popup = _Page("popup", opener=root)
        reg = LineageRegistry(default_selector="")
        reg.seed_root(root, GroupState(on=True, spa_on=False, selector=""))
        await reg.resolve(popup)

        reg.release(root)
        assert root not in reg.page_root
        assert root in reg.groups            # popup が参照中なので生存
        assert reg.page_root[popup] is root  # popup の所属は変わらない

    asyncio.run(scenario())


def test_release_drops_group_when_the_last_page_closes():
    # 系譜のページが全て閉じたら、どの生存ページからも参照されないグループを捨てる。
    async def scenario():
        root = _Page("root")
        popup = _Page("popup", opener=root)
        reg = LineageRegistry(default_selector="")
        reg.seed_root(root, GroupState(on=True, spa_on=False, selector=""))
        await reg.resolve(popup)

        reg.release(root)
        reg.release(popup)
        assert reg.groups == {}
        assert reg.page_root == {}

    asyncio.run(scenario())


def test_release_does_not_touch_other_lineages():
    # 別系譜のグループは巻き添えで捨てない（保存先が途中で変わってしまわないため）。
    async def scenario():
        a, b = _Page("a"), _Page("b")
        reg = LineageRegistry(default_selector="")
        reg.seed_root(a, GroupState(on=True, spa_on=False, selector=""))
        kept = await reg.resolve(b)

        reg.release(a)
        assert a not in reg.groups
        assert reg.groups[b] is kept

    asyncio.run(scenario())


def test_release_of_unknown_page_is_harmless():
    # 追跡していないページの close でも例外にならず、生存中のグループも消さない。
    async def scenario():
        root = _Page("root")
        reg = LineageRegistry(default_selector="")
        reg.seed_root(root, GroupState(on=True, spa_on=False, selector=""))

        reg.release(_Page("never-seen"))
        assert root in reg.groups
        assert reg.page_root[root] is root

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 系譜 id と保存先の規約（group_stamp / group_folder_name / group_subdir / make_group）
# --------------------------------------------------------------------------- #


def test_group_stamp_is_17_digits():
    # YYYYMMDDHHMMSSmmm（区切りなし・ミリ秒まで）＝ 17 桁の数字。
    assert re.fullmatch(r"\d{17}", group_stamp())


def test_group_stamp_formats_current_time_to_milliseconds(monkeypatch):
    # 区切り記号を入れず、マイクロ秒は切り詰めてミリ秒 3 桁にする
    # （lineage-<id> の <id> 部分が連続した数字になる規約）。
    class _FixedDatetime:
        @staticmethod
        def now():
            return datetime(2026, 8, 14, 10, 20, 28, 731456)

    monkeypatch.setattr(lineage, "datetime", _FixedDatetime)
    assert group_stamp() == "20260814102028731"


def test_group_subdir_and_folder_name():
    # 採番済みは output_dir/lineage-<id>、未採番(空)は output_dir 直下。
    out = Path("/tmp/out")
    assert group_folder_name("20260814101105674") == "lineage-20260814101105674"
    assert group_subdir(out, "20260814101105674") == out / "lineage-20260814101105674"
    assert group_subdir(out, "") == out


def test_make_group_id_is_timestamp():
    # 新規グループの id は「作成時刻（ミリ秒まで）」の文字列（ログ/フォルダ識別用）。
    g = make_group(on=False, spa_on=False, selector="")
    # 例: 20260814101105674（YYYYMMDDHHMMSSmmm・区切りなし＝17桁）
    assert re.fullmatch(r"\d{17}", g.id)
