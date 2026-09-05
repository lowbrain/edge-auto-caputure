"""監視セッション（edge_auto_capture.CaptureSession）と起動シーケンスのユニットテスト。

タブ系譜（グループ）の解決・記録状態のゲート・URL変化のイベント駆動（B-1）・操作バーへの
状態配布（F-D2 / F-D3 / F-D4）・main() から切り出した起動シーケンスを、実 Edge 無しで
（opener を返すページ代役と spawn 記録用スタブで）守る。

実行:
    pip install -e ".[dev]"
    pytest
"""

import asyncio
import re
import shutil
from pathlib import Path

import pytest

import badge
import capture
import infra
from capture import CaptureRequest, CaptureRunner
from config import Config

# --------------------------------------------------------------------------- #
# タブ系譜グループ（CaptureSession）
#
# 記録ON/OFF・SPA検知・セレクタは「opener 連鎖で決まるグループ」ごとに独立して持つ。
# 実 Edge を使わず、opener() を返すフェイクページと spawn 記録用スタブで、グループの
# 合流・独立性・SPA ゲートを速いユニットテストで回帰から守る。
# --------------------------------------------------------------------------- #


class _GroupPage:
    """opener() を持つ最小のページ代役（グループ解決テスト用）。"""

    def __init__(self, name: str, url: str = "https://example.test/", opener=None) -> None:
        self.name = name
        self.url = url
        self._opener = opener

    async def opener(self):
        return self._opener

    def __repr__(self) -> str:
        return f"<GroupPage {self.name}>"


class _FakeContext:
    def __init__(self, pages) -> None:
        self.pages = list(pages)


class _RecRunner:
    """runner.spawn(CaptureRequest) を記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.group_ids: list[int] = []
        self.triggers: list[str] = []

    def spawn(self, req):
        self.calls.append((req.page, req.url, req.selector))
        self.group_ids.append(req.group_id)
        self.triggers.append(req.trigger)


def _make_session(pages, roots=None, config=None):
    """フェイク context を持つ CaptureSession を作る。

    roots: {page: GroupState} を渡すと、setup() の種入れ相当（page_root/groups の登録）を
    済ませた状態にする。種入れは setup() と同じくレジストリの seed_root で行う（#48 以降、
    session.groups / session.page_root は読み取り専用ビューなので外から書けない）。
    runner は記録用スタブへ、refresh_panels は no-op へ差し替える。
    """
    from edge_auto_capture import CaptureSession

    session = CaptureSession(_FakeContext(pages), config or Config())
    session.runner = _RecRunner()

    async def _noop():
        return None

    session.refresh_panels = _noop  # 実 evaluate を避ける（グループ判定だけ検証する）
    for pg, state in (roots or {}).items():
        session._lineage.seed_root(pg, state)
    return session


class _SetupPage(_GroupPage):
    """setup() の監視配線（page.on）を受け取るページ代役。配線されたイベント名を記録する。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.events: list[str] = []

    def on(self, event, handler) -> None:
        self.events.append(event)


class _SetupContext(_FakeContext):
    """setup() が呼ぶ context 側 API を受け取る代役（実 Edge 無しで setup() を通す）。"""

    def __init__(self, pages) -> None:
        super().__init__(pages)
        self.bindings: list[str] = []
        self.init_scripts: list[str] = []
        self.events: list[str] = []

    async def expose_binding(self, name, callback) -> None:
        self.bindings.append(name)

    async def add_init_script(self, script) -> None:
        self.init_scripts.append(script)

    def on(self, event, handler) -> None:
        self.events.append(event)


@pytest.mark.parametrize("recording", [True, False])
def test_setup_seeds_startup_pages_as_root_groups(recording):
    """起動時に開いているページは、start_recording に従う root グループとして種入れされる。

    main() はこの種入れに全面的に依存する（自前で page_root/groups を触らない）ため、
    「setup() を通せば root 採番・start_recording 反映・監視配線が揃う」ことを固定する。
    """
    from edge_auto_capture import CaptureSession

    async def scenario():
        page = _SetupPage("startup")
        ctx = _SetupContext([page])
        session = CaptureSession(ctx, Config(start_recording=recording, target_selector="#main"))
        await session.setup()

        assert session.page_root[page] is page          # 自分が root
        grp = session.groups[page]
        assert (grp.on, grp.spa_on, grp.selector) == (recording, False, "#main")
        assert page.events == ["framenavigated", "close"]   # URL変化・消滅の監視も配線される
        assert ctx.events == ["download", "page"]           # DL 退避と新規タブ検知も配線される

    asyncio.run(scenario())


def test_setup_without_pages_seeds_nothing():
    """ページが 1 枚も無ければ種入れは起きない（main() が setup() 前に 1 枚用意する前提）。

    以前は main() が setup() の後に new_page() し、種入れを自前で書き足していた。その順序だと
    setup() が張った context.on("page") 経由の on_new_page が先に走りえて、start_recording では
    なく初期OFFの独立グループとして採番されうる。現在は main() が setup() の前に 1 枚用意し、
    種入れは setup() だけが行う。
    """
    from edge_auto_capture import CaptureSession

    async def scenario():
        ctx = _SetupContext([])
        session = CaptureSession(ctx, Config(start_recording=True))
        await session.setup()

        assert session.groups == {}
        assert session.page_root == {}

    asyncio.run(scenario())


def test_manual_tab_becomes_independent_off_group():
    # opener=None の手動タブは、それ自身が root の新グループ・初期OFFになる。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        manual = _GroupPage("manual", opener=None)
        session = _make_session(
            [root, manual], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        grp = await session._resolve_group(manual)
        assert grp.on is False  # 勝手に撮らない
        assert session.page_root[manual] is manual  # 自分が root
        assert grp is not session.groups[root]  # root グループとは別物

    asyncio.run(scenario())


def test_popup_joins_parent_group():
    # opener=root のポップアップは root と同じグループに合流し、状態を共有する。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        session = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=True, selector="#x")}
        )
        grp = await session._resolve_group(popup)
        assert grp is session.groups[root]  # 同一グループ（状態共有）
        assert session.page_root[popup] is root

    asyncio.run(scenario())


def test_grandchild_popup_resolves_to_root_group():
    # ポップアップのポップアップ（孫）も root グループへ合流する（推移性）。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        child = _GroupPage("child", opener=root)
        grand = _GroupPage("grand", opener=child)
        session = _make_session(
            [root, child, grand], roots={root: GroupState(on=False, spa_on=False, selector="")}
        )
        grp = await session._resolve_group(grand)
        assert grp is session.groups[root]
        assert session.page_root[grand] is root

    asyncio.run(scenario())


def test_toggle_one_group_does_not_affect_another():
    # あるグループの記録ON/OFFは、無関係な別グループに波及しない。
    from edge_auto_capture import GroupState

    async def scenario():
        r1 = _GroupPage("r1")
        r2 = _GroupPage("r2")
        session = _make_session(
            [r1, r2],
            roots={
                r1: GroupState(on=False, spa_on=False, selector=""),
                r2: GroupState(on=False, spa_on=False, selector=""),
            },
        )
        await session.on_toggle({"page": r1}, token=session.token)
        assert session.groups[r1].on is True   # 押した側だけON
        assert session.groups[r2].on is False  # 別グループは不変
        # ONにした瞬間、r1 グループの現存ページ（r1）が即撮りされる
        assert [c[0] for c in session.runner.calls] == [r1]

    asyncio.run(scenario())


def test_spa_changed_gated_by_group_state():
    # on_spa_changed はそのページのグループが on かつ spa_on のときだけ撮る。
    from edge_auto_capture import GroupState

    async def scenario():
        # on=True, spa_on=True → 撮る
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=True, selector="#s")})
        await s.on_spa_changed({"page": r}, token=s.token)
        assert s.runner.calls == [(r, r.url, "#s")]

        # spa_on=False → 撮らない
        r2 = _GroupPage("r2")
        s2 = _make_session([r2], roots={r2: GroupState(on=True, spa_on=False, selector="")})
        await s2.on_spa_changed({"page": r2}, token=s2.token)
        assert s2.runner.calls == []

        # on=False → 撮らない
        r3 = _GroupPage("r3")
        s3 = _make_session([r3], roots={r3: GroupState(on=False, spa_on=True, selector="")})
        await s3.on_spa_changed({"page": r3}, token=s3.token)
        assert s3.runner.calls == []

    asyncio.run(scenario())


def test_shoot_passes_group_id_to_spawn():
    # 撮影要求にはグループの id（作成時刻）が渡り、保存先フォルダ/ログで系譜を見分けられる。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session(
            [r], roots={r: GroupState(on=True, spa_on=True, selector="", id="20260814101105674")}
        )
        await s.on_spa_changed({"page": r}, token=s.token)
        assert s.runner.group_ids == ["20260814101105674"]

    asyncio.run(scenario())


def test_shoot_skips_url_out_of_scope_and_does_not_spawn():
    # _shoot は should_capture で弾いた URL では None を返し、runner.spawn を呼ばない（#35）。
    # 判定ロジック自体は should_capture 側で厚く検証済み。ここは _shoot が結果を尊重する配線を守る。
    from edge_auto_capture import GroupState

    cfg = Config(skip_urls=("https://skip.test/",))
    s = _make_session([], config=cfg)
    skip = _GroupPage("skip", url="https://skip.test/logout")  # skip_urls に前方一致
    grp = GroupState(on=True, spa_on=False, selector="#x")
    assert s._shoot(skip, grp, "manual") is None
    assert s.runner.calls == []


def test_shoot_returns_none_when_url_unavailable():
    # pg.url の取得が失敗（ページが切断された等）したら、判定へ進まず None を返し spawn しない（#35）。
    from edge_auto_capture import GroupState

    class _UrlErrorPage:
        name = "boom"

        @property
        def url(self):
            raise RuntimeError("page detached")

    s = _make_session([])
    grp = GroupState(on=True, spa_on=False, selector="#x")
    assert s._shoot(_UrlErrorPage(), grp, "url") is None
    assert s.runner.calls == []


def test_refresh_panels_distributes_each_pages_own_group_state():
    # refresh_panels は開いている各ページへ、そのページが属するグループの状態を配る（#35）。
    # ページごとに状態が違うことを、2 グループのダブルで検証する。
    from edge_auto_capture import GroupState

    class _PanelPage:
        """opener() を持ちつつ、try_eval 経由で実行された JS を記録するページ代役。"""

        def __init__(self, name: str) -> None:
            self.name = name
            self.evals: list[str] = []

        async def evaluate(self, js, *args):
            self.evals.append(js)
            return None

        async def opener(self):
            return None

    async def scenario():
        p1 = _PanelPage("p1")
        p2 = _PanelPage("p2")
        g1 = GroupState(on=True, spa_on=True, selector="#one")
        g2 = GroupState(on=False, spa_on=False, selector="")
        s = _make_session([p1, p2], roots={p1: g1, p2: g2})
        del s.refresh_panels  # _make_session の no-op を外し、本物の refresh_panels を検証する
        await s.refresh_panels()
        # 各ページには自分のグループの状態から組んだ applyState 呼び出しだけが届く。
        assert p1.evals == [badge.apply_state_call(s.ns, g1.on, g1.spa_on, g1.selector)]
        assert p2.evals == [badge.apply_state_call(s.ns, g2.on, g2.spa_on, g2.selector)]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# F-D4: 保存先フォルダを開く
#
# 「保存先」ボタンは config.output_dir（起動単位のセッションフォルダ）を OS の
# ファイルマネージャで開く。グループ状態には依存せず、token 照合だけを見る。
# 実際にフォルダを開くのは避け、open_in_file_manager をスタブして呼び出しを検証する。
# --------------------------------------------------------------------------- #


def test_open_folder_opens_session_output_dir(monkeypatch, tmp_path):
    # 押すと config.output_dir がそのまま opener へ渡る（記録状態やグループに依らない）。
    import edge_auto_capture as eac

    async def scenario():
        opened: list = []
        monkeypatch.setattr(eac, "open_in_file_manager", lambda p: opened.append(p) or True)
        out = tmp_path / "2026-08-11_143025"
        s = _make_session([_GroupPage("r")], config=Config(output_dir=out))
        await s.on_open_folder({"page": _GroupPage("r")}, token=s.token)
        assert opened == [out]

    asyncio.run(scenario())


def test_open_folder_ignores_wrong_token(monkeypatch, tmp_path):
    # token 不一致（操作バー以外からの呼び出し）はフォルダを開かない。
    import edge_auto_capture as eac

    async def scenario():
        opened: list = []
        monkeypatch.setattr(eac, "open_in_file_manager", lambda p: opened.append(p) or True)
        s = _make_session([_GroupPage("r")], config=Config(output_dir=tmp_path))
        await s.on_open_folder({"page": _GroupPage("r")}, token="wrong")
        assert opened == []

    asyncio.run(scenario())


def test_open_in_file_manager_returns_false_on_error(monkeypatch):
    # 開けない（存在しない・権限不足で例外）ときは握り潰して False を返す（バー操作を壊さない）。
    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(infra.subprocess, "run", boom)
    monkeypatch.setattr(infra.os, "startfile", boom, raising=False)
    assert infra.open_in_file_manager(Path("/no/such/folder")) is False


def test_open_in_file_manager_invokes_platform_opener(monkeypatch, tmp_path):
    # 開けたら True。macOS では open コマンドへ対象パスを渡す（OS 分岐の回帰）。
    monkeypatch.setattr(infra.sys, "platform", "darwin")
    calls: list = []
    monkeypatch.setattr(infra.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    assert infra.open_in_file_manager(tmp_path) is True
    assert calls == [["open", str(tmp_path)]]


# --------------------------------------------------------------------------- #
# F-D2: セレクタ履歴（入力欄の datalist 候補）
#
# 確定したセレクタ（blur/Enter）をセッション横断の履歴へ積み、全バーの入力候補として配る。
# 新しい順・重複なし・上限あり。get_state にも同梱して遷移後のバーが候補を失わないようにする。
# --------------------------------------------------------------------------- #


def test_remember_selector_dedup_recency_and_cap():
    from edge_auto_capture import SELECTOR_HISTORY_MAX, CaptureSession

    s = CaptureSession(_FakeContext([]), Config())

    # 新規は先頭へ積まれ True を返す。
    assert s._remember_selector("#a") is True
    assert s._remember_selector("#b") is True
    assert s.selector_history == ["#b", "#a"]

    # 空文字（クリア）は積まない。
    assert s._remember_selector("   ") is False
    assert s.selector_history == ["#b", "#a"]

    # 直近と同じは並びも変わらず False。
    assert s._remember_selector("#b") is False
    assert s.selector_history == ["#b", "#a"]

    # 既出を入れ直すと重複を作らず先頭へ繰り上げる（最近使った順）。
    assert s._remember_selector("#a") is True
    assert s.selector_history == ["#a", "#b"]

    # 上限を超えたら古い方から落ちる。
    for i in range(SELECTOR_HISTORY_MAX + 5):
        s._remember_selector(f"#sel-{i}")
    assert len(s.selector_history) == SELECTOR_HISTORY_MAX
    assert s.selector_history[0] == f"#sel-{SELECTOR_HISTORY_MAX + 4}"  # 最後に入れたものが先頭


def test_commit_selector_records_history():
    # on_commit_selector が確定値を履歴へ積む（token 一致時のみ）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})

        await s.on_commit_selector({"page": r}, token=s.token, value="#x")
        assert s.selector_history == ["#x"]

        # 別の値を確定 → 先頭へ積む。
        await s.on_commit_selector({"page": r}, token=s.token, value=".price")
        assert s.selector_history == [".price", "#x"]

        # クリア（空）は積まない。
        await s.on_commit_selector({"page": r}, token=s.token, value="")
        assert s.selector_history == [".price", "#x"]

        # token 不一致は履歴を触らない。
        await s.on_commit_selector({"page": r}, token="wrong", value="#leak")
        assert s.selector_history == [".price", "#x"]

    asyncio.run(scenario())


def test_get_state_includes_selector_history():
    # get_state は履歴を同梱する（遷移後のバーが datalist 候補を失わない）。
    # token 不一致には履歴を漏らさない（利用者が入れた候補は返さない）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})
        s.selector_history = ["#x", ".price"]

        state = await s.get_state({"page": r}, token=s.token)
        assert state["history"] == ["#x", ".price"]

        denied = await s.get_state({"page": r}, token="wrong")
        assert denied["history"] == []

    asyncio.run(scenario())


def test_set_history_call_serializes_values():
    # 日本語/記号を含む値も JS 配列リテラルとして安全に埋め込む。E-3: 固定名でなく
    # 起動ごとのランダム名 ns の隠しオブジェクト window[ns].setHistory(...) を呼ぶ式を組む。
    call = badge.set_history_call("nabc", ["#main", ".一覧"])
    assert call.startswith('window["nabc"] && window["nabc"].setHistory && '
                           'window["nabc"].setHistory(')
    # json.dumps で ASCII 化（\uXXXX）され、二重引用符の配列になる。
    assert '"#main"' in call


def test_trigger_threaded_per_path():
    # 撮影契機が投入元 3 経路から CaptureRequest.trigger に載る（F-A1）:
    # on_shot="manual" / on_spa_changed="spa" / _shoot_if_changed="url" /
    # 記録開始(on_toggle)の即撮り="url"。
    from edge_auto_capture import GroupState

    async def scenario():
        # 今すぐ1枚 → manual（記録状態に関わらず撮る）
        r1 = _GroupPage("r1")
        s1 = _make_session([r1], roots={r1: GroupState(on=False, spa_on=False, selector="")})
        await s1.on_shot({"page": r1}, token=s1.token)
        assert s1.runner.triggers == ["manual"]

        # SPA変化 → spa
        r2 = _GroupPage("r2")
        s2 = _make_session([r2], roots={r2: GroupState(on=True, spa_on=True, selector="")})
        await s2.on_spa_changed({"page": r2}, token=s2.token)
        assert s2.runner.triggers == ["spa"]

        # URL変化 → url
        r3 = _GroupPage("r3", url="https://example.test/a")
        s3 = _make_session([r3], roots={r3: GroupState(on=True, spa_on=False, selector="")})
        await s3._shoot_if_changed(r3)
        assert s3.runner.triggers == ["url"]

        # 記録開始の即撮り → url
        r4 = _GroupPage("r4")
        s4 = _make_session([r4], roots={r4: GroupState(on=False, spa_on=False, selector="")})
        await s4.on_toggle({"page": r4}, token=s4.token)
        assert s4.runner.triggers == ["url"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# URL変化のイベント駆動化（B-1）: _shoot_if_changed / _on_navigated / _on_page_closed
#
# 旧・毎tickポーリングの run ループを廃し、framenavigated / close で撮る/掃除する。
# 実 Playwright を使わず、url を書き換えられるフェイクページで挙動を回帰から守る。
# --------------------------------------------------------------------------- #


def test_shoot_if_changed_only_on_real_url_change():
    # 記録ONのページは、URLが前回と変わったときだけ撮る。ハッシュのみの変化・
    # 同一URL・skip_urls・記録OFF では撮らない。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r", url="https://example.test/a")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="#x")})

        # 初回: seen 空 → 撮る。seen が現URLキーにそろう。
        await s._shoot_if_changed(r)
        assert [c[1] for c in s.runner.calls] == ["https://example.test/a"]

        # 同一URLで再度 → 撮らない（seen 一致）。
        await s._shoot_if_changed(r)
        assert len(s.runner.calls) == 1

        # ハッシュだけ変化（scroll-spy）→ 撮らない。
        r.url = "https://example.test/a#sec2"
        await s._shoot_if_changed(r)
        assert len(s.runner.calls) == 1

        # 本当のURL変化 → 撮る。selector はグループの値が渡る。
        r.url = "https://example.test/b"
        await s._shoot_if_changed(r)
        assert s.runner.calls[-1] == (r, "https://example.test/b", "#x")

    asyncio.run(scenario())


def test_shoot_if_changed_gated_by_recording_and_skip_urls():
    from edge_auto_capture import GroupState

    async def scenario():
        # 記録OFF → 撮らず seen も更新しない（ON にした瞬間の即撮りに委ねる）。
        off = _GroupPage("off", url="https://example.test/x")
        s = _make_session([off], roots={off: GroupState(on=False, spa_on=False, selector="")})
        await s._shoot_if_changed(off)
        assert s.runner.calls == []
        assert off not in s.seen

        # skip_urls に一致 → 撮らない。
        cfg = Config(skip_urls=("https://skip.test/",))
        skip = _GroupPage("skip", url="https://skip.test/")
        s2 = _make_session(
            [skip], roots={skip: GroupState(on=True, spa_on=False, selector="")}, config=cfg
        )
        await s2._shoot_if_changed(skip)
        assert s2.runner.calls == []

    asyncio.run(scenario())


def test_on_navigated_ignores_subframe_navigations():
    # 子フレーム（iframe 等）の遷移では撮らない。メインフレームの遷移だけ拾う。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r", url="https://example.test/a")
        r.main_frame = object()          # メインフレームの目印
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=False, selector="")})

        await s._on_navigated(r, frame=object())   # 別フレーム → 無視
        assert s.runner.calls == []

        await s._on_navigated(r, frame=r.main_frame)  # メインフレーム → 撮る
        assert [c[1] for c in s.runner.calls] == ["https://example.test/a"]

    asyncio.run(scenario())


def test_on_page_closed_prunes_state_and_gcs_group():
    # 閉じたページは seen / page_root / _tracked から消え、どの生存ページからも
    # 参照されなくなった root のグループも捨てられる。ポップアップが残る間は保持する。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        s = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        # popup を root グループへ合流させ、追跡・seen も載せておく。
        await s._resolve_group(popup)
        s._tracked.update({root, popup})
        s.seen[root] = "k1"
        s.seen[popup] = "k2"

        # root を閉じる: popup がまだ root を参照しているのでグループは残る。
        s._on_page_closed(root)
        assert root not in s.seen and root not in s.page_root and root not in s._tracked
        assert root in s.groups  # popup が参照中なので生存

        # popup も閉じる: root への参照が消え、グループが GC される。
        s._on_page_closed(popup)
        assert popup not in s.seen and popup not in s.page_root
        assert s.groups == {}

    asyncio.run(scenario())


def test_group_subdir_and_folder_name():
    # 採番済みは output_dir/lineage-<id>、未採番(空)は output_dir 直下。
    from pathlib import Path

    from capture import group_folder_name, group_subdir

    out = Path("/tmp/out")
    assert group_folder_name("20260814101105674") == "lineage-20260814101105674"
    assert group_subdir(out, "20260814101105674") == out / "lineage-20260814101105674"
    assert group_subdir(out, "") == out


def test_make_group_id_is_timestamp():
    # 新規グループの id は「作成時刻（ミリ秒まで）」の文字列（ログ/フォルダ識別用）。
    from lineage import make_group

    g = make_group(on=False, spa_on=False, selector="")
    # 例: 20260814101105674（YYYYMMDDHHMMSSmmm・区切りなし＝17桁）
    assert re.fullmatch(r"\d{17}", g.id)


def test_get_state_returns_group_state():
    # get_state は問い合わせ元ページのグループの状態を返す（token 一致時）。
    from edge_auto_capture import GroupState

    async def scenario():
        r = _GroupPage("r")
        s = _make_session([r], roots={r: GroupState(on=True, spa_on=True, selector="#c")})
        state = await s.get_state({"page": r}, token=s.token)
        # 撮影カウンタ（count）とセレクタ履歴（history）も同梱する（F-D3/F-D2。
        # 再描画されたバーの枚数復元・datalist 候補復元に使う）。
        assert state == {
            "recording": True, "spa": True, "selector": "#c", "count": 0, "history": [],
        }
        # token 不一致には既定（状態を漏らさない）。count は秘匿情報ではないので返す。
        assert await s.get_state({"page": r}, token="wrong") == {
            "recording": False, "spa": False, "selector": "", "count": 0, "history": [],
        }

    asyncio.run(scenario())


def test_prune_drops_group_when_all_pages_closed():
    # ページが全て閉じたグループは、状態が捨てられる（root が閉じても系譜が残れば保持）。
    from edge_auto_capture import GroupState

    async def scenario():
        root = _GroupPage("root")
        popup = _GroupPage("popup", opener=root)
        session = _make_session(
            [root, popup], roots={root: GroupState(on=True, spa_on=False, selector="")}
        )
        await session._resolve_group(popup)  # popup を root グループへ合流させる

        # root だけ閉じる（popup は生存）→ グループは残る
        session._on_page_closed(root)
        assert root in session.groups
        assert session.page_root.get(popup) is root

        # popup も閉じる → 参照ゼロでグループ破棄
        session._on_page_closed(popup)
        assert session.groups == {}
        assert session.page_root == {}

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# F-D3: 撮影カウンタ / 失敗表示（成否を JS へ通知する経路）
# --------------------------------------------------------------------------- #


def test_capture_end_call_encodes_success_and_failure():
    # done 有無を真偽値としてページ側 captureEnd へ渡す呼び出し式を組む（成功=赤/失敗=琥珀）。
    # E-3: 固定名でなく起動ごとのランダム名 ns の隠しオブジェクト window[ns].captureEnd(...) を呼ぶ。
    assert badge.capture_end_call("nabc", True) == (
        'window["nabc"] && window["nabc"].captureEnd && window["nabc"].captureEnd(true)'
    )
    assert badge.capture_end_call("nabc", False) == (
        'window["nabc"] && window["nabc"].captureEnd && window["nabc"].captureEnd(false)'
    )


def test_set_count_call_encodes_count():
    # 撮影カウンタ（本セッション枚数）をバーへ配る呼び出し式を組む（E-3: window[ns].setCount）。
    assert badge.set_count_call("nabc", 0) == (
        'window["nabc"] && window["nabc"].setCount && window["nabc"].setCount(0)'
    )
    assert badge.set_count_call("nabc", 7) == (
        'window["nabc"] && window["nabc"].setCount && window["nabc"].setCount(7)'
    )


def test_new_namespace_is_random_and_carries_no_hint():
    # E-3: 起動ごとに使い捨てるランダム名。先頭は英字（数値インデックス的扱いを避ける）で、
    # __eac のような固定の手掛かりを含まない（含めると全文一致でなくても勘付かれうる）。
    a, b = badge.new_namespace(), badge.new_namespace()
    assert a != b
    assert a[0].isalpha()
    assert "__eac" not in a and "eac" not in a


def test_build_badge_script_hides_fixed_globals():
    # E-3: 固定名（window.__eacApplyState 等）を生やさず、ランダム名 ns の非列挙プロパティへ収める。
    # ②のページ→Python バインディング固定名は退避後に window から削除する。
    script = badge.build_badge_script(token="t", ns="nXYZ")
    assert '"ns": "nXYZ"' in script                       # ns が $CONFIG に載る
    assert "window.__eacApplyState =" not in script       # 旧方式の固定名代入が残っていない
    assert "window.__eacSetCount =" not in script
    assert "Object.defineProperty(window, NS" in script   # ①を隠しプロパティで公開
    assert "delete window[n]" in script                   # ②の固定名を削除


class _EvalPage:
    """try_eval の宛先になる最小のページ代役。実行された JS を記録する。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.evals: list[str] = []

    async def evaluate(self, js, *args):
        self.evals.append(js)
        return None


def test_session_wires_runner_on_result():
    # CaptureSession は runner の成否通知を自分のカウンタ更新へ配線する（F-D3）。
    from edge_auto_capture import CaptureSession

    session = CaptureSession(_FakeContext([]), Config())
    assert session.runner.on_result == session._on_capture_result
    assert session.shots == 0


def test_on_capture_result_counts_only_success_and_pushes():
    # ok=True のときだけ枚数を増やし、その値を全ページの操作バーへ配る。ok=False は数えない。
    from edge_auto_capture import CaptureSession

    async def scenario():
        p1, p2 = _EvalPage("1"), _EvalPage("2")
        session = CaptureSession(_FakeContext([p1, p2]), Config())

        await session._on_capture_result(True)
        assert session.shots == 1
        # 成功のたびに現在の枚数を全ページへ配る（set_count_call の式が evaluate される）。
        # E-3: 呼び出し式はセッションのランダム名 ns（window[ns].setCount）を使う。
        assert p1.evals == [badge.set_count_call(session.ns, 1)]
        assert p2.evals == [badge.set_count_call(session.ns, 1)]

        await session._on_capture_result(False)   # 全滅は「撮れた1枚」に数えない
        assert session.shots == 1
        assert len(p1.evals) == 1                  # 配布もしない（枚数が変わらないため）

        await session._on_capture_result(True)
        assert session.shots == 2
        assert p1.evals[-1] == badge.set_count_call(session.ns, 2)

    asyncio.run(scenario())


def test_get_state_reports_current_shot_count():
    # get_state は現在の撮影カウンタを count で返す（再描画されたバーの枚数復元用）。
    # 記録状態を伏せる token 不一致の応答にも count は載る（枚数は秘匿情報ではない）。
    from edge_auto_capture import CaptureSession

    async def scenario():
        session = CaptureSession(_FakeContext([]), Config())
        await session._on_capture_result(True)
        await session._on_capture_result(True)
        state = await session.get_state(None, token="wrong")
        assert state["count"] == 2

    asyncio.run(scenario())


class _FakeCapturePage:
    """_capture を実 Edge 無しで通すための最小ページ代役（成功/失敗を切り替えられる）。"""

    def __init__(self, *, screenshot_ok: bool = True, text_ok: bool = True) -> None:
        self.screenshot_ok = screenshot_ok
        self.text_ok = text_ok
        self.eval_calls: list[str] = []

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def title(self):
        return "T"

    async def screenshot(self, path=None, full_page=None):
        if not self.screenshot_ok:
            raise RuntimeError("screenshot failed")
        Path(path).write_bytes(b"png")

    async def evaluate(self, js, *args):
        self.eval_calls.append(js)
        # 既定の CaptureRunner は ns="" なので、_capture は body_text_call("") を渡す（E-3）。
        if js == badge.body_text_call(""):
            if not self.text_ok:
                raise RuntimeError("no text")
            return "body text"
        return None


def test_capture_notifies_result_true_on_success(tmp_path):
    # png/txt が撮れたら captureEnd に ok=true を渡し、on_result にも True が届く（F-D3）。
    async def scenario():
        runner = CaptureRunner()
        got: list[bool] = []

        async def record(ok):
            got.append(ok)

        runner.on_result = record
        page = _FakeCapturePage(screenshot_ok=True, text_ok=True)
        cfg = Config(output_dir=tmp_path, settle_delay=0)
        await runner._capture(CaptureRequest(page, "https://ok.test/", cfg, trigger="manual"))

        assert got == [True]
        assert badge.capture_end_call(runner.ns, True) in page.eval_calls
        assert badge.capture_end_call(runner.ns, False) not in page.eval_calls

    asyncio.run(scenario())


def test_capture_notifies_result_false_on_total_failure(tmp_path):
    # 全滅（png も txt も失敗）なら captureEnd に ok=false を渡し、on_result にも False が届く。
    async def scenario():
        runner = CaptureRunner()
        got: list[bool] = []

        async def record(ok):
            got.append(ok)

        runner.on_result = record
        page = _FakeCapturePage(screenshot_ok=False, text_ok=False)
        cfg = Config(output_dir=tmp_path, settle_delay=0)
        await runner._capture(CaptureRequest(page, "https://ng.test/", cfg, trigger="manual"))

        assert got == [False]
        assert badge.capture_end_call(runner.ns, False) in page.eval_calls
        assert badge.capture_end_call(runner.ns, True) not in page.eval_calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "trigger, expect_settle_sleep",
    [
        ("manual", True),   # 手動：load 直後にまだ描画が動くので settle_delay を待つ
        ("url", True),      # URL遷移：同上
        ("spa", False),     # SPA：ページ側デバウンスで確定済み。二重待ちを避けて省く（B-4, #18）
    ],
)
def test_capture_skips_settle_sleep_only_for_spa(tmp_path, monkeypatch, trigger, expect_settle_sleep):
    # settle_delay 分の sleep が SPA 経由でだけ省かれることを、記録した sleep 秒数で確かめる。
    async def scenario():
        slept: list[float] = []

        async def fake_sleep(secs):
            slept.append(secs)

        monkeypatch.setattr(capture.asyncio, "sleep", fake_sleep)

        runner = CaptureRunner()
        page = _FakeCapturePage(screenshot_ok=True, text_ok=True)
        cfg = Config(output_dir=tmp_path, settle_delay=0.4)
        await runner._capture(CaptureRequest(page, "https://ok.test/", cfg, trigger=trigger))

        assert (0.4 in slept) is expect_settle_sleep

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 起動シーケンスのヘルパ（_prepare_output_dir / _prepare_profile_dir / _launch_browser）
#
# main() から切り出した各段。Playwright を起こさずに「保存先を用意できたか」「使い捨てと
# 再利用の分岐」「候補ブラウザのフォールバックと全滅時の通知」を回帰から守る。
# --------------------------------------------------------------------------- #


def test_prepare_output_dir_creates_and_reports_success(tmp_path):
    import edge_auto_capture as eac

    out = tmp_path / "session"
    assert eac._prepare_output_dir(Config(output_dir=out)) is True
    assert out.is_dir()


def test_prepare_output_dir_notifies_and_fails_when_unwritable(monkeypatch, tmp_path):
    # 作れないときは無言終了せず通知し、False で呼び出し側に中断させる。
    import edge_auto_capture as eac

    said: list[str] = []
    monkeypatch.setattr(eac, "notify_fatal", lambda msg: said.append(msg))

    def boom(*a, **kw):
        raise PermissionError("読み取り専用です")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert eac._prepare_output_dir(Config(output_dir=tmp_path / "x")) is False
    assert said and "保存先フォルダを作成できませんでした" in said[0]


def test_prepare_profile_dir_reuses_configured_dir(monkeypatch, tmp_path):
    # profile_dir 指定時は「そのフォルダ・使い捨てでない」。掃除は keep 付きで呼ぶ。
    import edge_auto_capture as eac

    kept: list = []
    monkeypatch.setattr(eac, "cleanup_old_profiles", lambda **kw: kept.append(kw.get("keep")))

    prof = tmp_path / "profile"
    user_data_dir, ephemeral = eac._prepare_profile_dir(Config(profile_dir=str(prof)))

    assert (user_data_dir, ephemeral) == (str(prof), False)
    assert prof.is_dir()                    # 無ければ作る
    assert kept == [prof]                   # 再利用プロファイル自身は掃除対象外


def test_prepare_profile_dir_makes_ephemeral_when_unset(monkeypatch):
    # 未指定なら使い捨ての一時プロファイル。掃除は除外指定なしで呼ぶ。
    import edge_auto_capture as eac

    calls: list = []
    monkeypatch.setattr(eac, "cleanup_old_profiles", lambda **kw: calls.append(kw))

    user_data_dir, ephemeral = eac._prepare_profile_dir(Config(profile_dir=""))
    try:
        assert ephemeral is True
        assert Path(user_data_dir).name.startswith("edge-debug-")
        assert calls == [{}]                # keep を渡さない＝全部が掃除対象
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)


class _FakeChromium:
    """launch_persistent_context を「指定回数だけ失敗してから成功する」代役。"""

    def __init__(self, fail_first: int) -> None:
        self.fail_first = fail_first
        self.channels: list[str] = []

    async def launch_persistent_context(self, **kwargs):
        self.channels.append(kwargs["channel"])
        if len(self.channels) <= self.fail_first:
            raise RuntimeError("未インストール")
        return _LaunchedContext()


class _LaunchedContext:
    browser = None       # version 取得は「不明」経路（起動を妨げないことの確認）


class _FakePlaywright:
    def __init__(self, chromium) -> None:
        self.chromium = chromium


def test_launch_browser_falls_back_to_next_candidate():
    # 既定（browser 未指定）は Edge→Chrome。Edge が起動できなければ Chrome へ回る。
    import edge_auto_capture as eac

    async def scenario():
        chromium = _FakeChromium(fail_first=1)
        context = await eac._launch_browser(_FakePlaywright(chromium), Config(), "/tmp/prof")

        assert isinstance(context, _LaunchedContext)
        assert chromium.channels == ["msedge", "chrome"]   # 優先順に試す

    asyncio.run(scenario())


def test_launch_browser_returns_none_and_notifies_when_all_fail(monkeypatch):
    # 全候補が失敗したら None を返し、試した順と理由を添えて通知する（無言終了にしない）。
    import edge_auto_capture as eac

    said: list[str] = []
    monkeypatch.setattr(eac, "notify_fatal", lambda msg: said.append(msg))

    async def scenario():
        chromium = _FakeChromium(fail_first=99)
        context = await eac._launch_browser(_FakePlaywright(chromium), Config(), "/tmp/prof")

        assert context is None
        assert said and "ブラウザを起動できませんでした" in said[0]
        assert "Edge / Chrome" in said[0]      # 試した候補が分かる
        assert "未インストール" in said[0]      # 各候補の失敗理由も添える

    asyncio.run(scenario())
