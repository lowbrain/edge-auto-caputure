"""CaptureSession の合言葉（token）照合のユニットテスト。

公開バインディング（__eac_* 群）は全ページの window に出るため、閲覧中サイトの
スクリプトからも呼べてしまう。各コールバックが token を照合し、一致しない呼び出しを
無視する（＝記録状態・セレクタを勝手に変えさせない・状態を漏らさない）ことを守る。

実 Edge は不要（コールバックを直接呼ぶ純粋なロジックの検証）。
"""

import asyncio

import pytest

from config import Config
from edge_auto_capture import CaptureSession, GroupState, _url_key


def _session() -> CaptureSession:
    # context はここでは使わない（コールバックが token 照合で早期 return する経路のみ検証）。
    return CaptureSession(context=None, config=Config())


class _Page:
    """opener() を持つ最小のページ代役（グループ解決・撮影経路の検証用）。"""

    url = "https://example.test/"

    async def opener(self):
        return None


class _RecRunner:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def spawn(self, page, url, config, selector="", group_id=""):
        self.calls.append((page, url, selector))


def _seeded_session(**state) -> tuple[CaptureSession, _Page]:
    """1 ページを root として seed 済みのセッションと、その root ページを返す。

    state は GroupState の初期値（on / spa_on / selector）。runner は記録用スタブへ差し替える。
    種入れは setup() と同じくレジストリの seed_root で行う（#48 以降、session.groups /
    session.page_root は読み取り専用ビューなので外から書けない）。
    """
    s = _session()
    s.runner = _RecRunner()
    page = _Page()
    s._lineage.seed_root(
        page,
        GroupState(
            on=state.get("on", False),
            spa_on=state.get("spa_on", False),
            selector=state.get("selector", ""),
        ),
    )
    return s, page


def test_token_is_random_per_session():
    a, b = _session(), _session()
    assert a.token and b.token
    assert a.token != b.token


def test_authorized_matches_only_exact_token():
    s = _session()
    assert s._authorized(s.token) is True
    assert s._authorized("wrong") is False
    assert s._authorized(None) is False
    assert s._authorized("") is False


def test_toggle_ignored_without_token():
    # 合言葉不一致では source も触らず早期 return する（グループを作りも変えもしない）。
    s = _session()
    asyncio.run(s.on_toggle(None, token="wrong"))
    assert s.groups == {}  # 何のグループ状態も生まれない


def test_set_selector_ignored_without_token():
    s = _session()
    asyncio.run(s.on_set_selector(None, token="wrong", value=".evil"))
    assert s.groups == {}  # 外部からセレクタを書き換えられない


def test_spa_changed_ignored_without_token():
    # 合言葉不一致（操作バー以外）からの変化通知は無視する（source を触らず早期 return）。
    s = _session()
    asyncio.run(s.on_spa_changed(None, token="wrong", sig="x"))  # 例外なく無視される
    assert s.groups == {}


def test_spa_changed_ignored_when_not_recording():
    # 正規 token でも、そのページのグループが記録OFF なら撮らない（記録ON がマスタースイッチ）。
    s, page = _seeded_session(on=False, spa_on=True)
    asyncio.run(s.on_spa_changed({"page": page}, token=s.token, sig="x"))
    assert s.runner.calls == []  # 記録OFF なので撮らない


@pytest.mark.parametrize("token", ["wrong", None, ""])
def test_get_state_hides_real_state_without_token(token):
    # 不一致には実際の状態を返さず既定値を返す（外部への情報漏れを防ぐ）。source も触らない。
    s, page = _seeded_session(on=True, spa_on=True, selector=".secret")
    state = asyncio.run(s.get_state(None, token=token))
    # 記録状態やセレクタは伏せるが、撮影カウンタ（count）は秘匿情報ではないので返す（F-D3）。
    # セレクタ履歴（F-D2）は利用者が入れた候補なので非正規呼び出しには返さない（空）。
    assert state == {
        "recording": False, "spa": False, "selector": "", "count": 0, "history": [],
    }


def test_get_state_returns_real_state_with_token():
    # 正規 token では、問い合わせ元ページが属するグループの実状態を返す（撮影カウンタも同梱）。
    # セレクタ履歴（F-D2）も同梱する（遷移後のバーが datalist 候補を失わない）。
    s, page = _seeded_session(on=True, spa_on=False, selector=".ok")
    state = asyncio.run(s.get_state({"page": page}, token=s.token))
    assert state == {
        "recording": True, "spa": False, "selector": ".ok", "count": 0, "history": [],
    }


# --------------------------------------------------------------------------- #
# _url_key … URL変化の「同じページか」判定キー（フラグメント #... を除く）
# --------------------------------------------------------------------------- #


def test_url_key_strips_fragment():
    # scroll-spy で付くハッシュ違いは同じページとみなす（Vuetify等の二重撮り防止）。
    base = "https://vuetifyjs.com/ja/getting-started/installation/"
    assert _url_key(base) == base
    assert _url_key(base + "#vite309") == base
    assert _url_key(base + "#section-624b") == base
    # ハッシュだけ違う2URLは同一キーになる（＝撮り直さない）。
    assert _url_key(base + "#nuxt") == _url_key(base + "#vite")


def test_url_key_keeps_path_and_query():
    # パスやクエリの違いは別ページとして残す（#以降だけを落とす）。
    assert _url_key("https://a.com/p?q=1#frag") == "https://a.com/p?q=1"
    assert _url_key("https://a.com/x") != _url_key("https://a.com/y")
    assert _url_key("about:blank") == "about:blank"
