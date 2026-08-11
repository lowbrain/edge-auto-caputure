"""CaptureSession の合言葉（token）照合のユニットテスト。

公開バインディング（__eac_* 群）は全ページの window に出るため、閲覧中サイトの
スクリプトからも呼べてしまう。各コールバックが token を照合し、一致しない呼び出しを
無視する（＝記録状態・セレクタを勝手に変えさせない・状態を漏らさない）ことを守る。

実 Edge は不要（コールバックを直接呼ぶ純粋なロジックの検証）。
"""

import asyncio

import pytest

from capture import Config
from edge_auto_capture import CaptureSession


def _session() -> CaptureSession:
    # context はここでは使わない（コールバックが token 照合で早期 return する経路のみ検証）。
    return CaptureSession(context=None, config=Config())


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
    s = _session()
    before = s.on
    asyncio.run(s.on_toggle(None, token="wrong"))
    assert s.on == before  # 記録状態は変わらない


def test_set_selector_ignored_without_token():
    s = _session()
    asyncio.run(s.on_set_selector(None, token="wrong", value=".evil"))
    assert s.selector == ""  # 外部から書き換えられない


@pytest.mark.parametrize("token", ["wrong", None, ""])
def test_get_state_hides_real_state_without_token(token):
    s = _session()
    s.on = True
    s.spa_on = True
    s.selector = ".secret"
    state = asyncio.run(s.get_state(None, token=token))
    # 不一致には実際の状態を返さず既定値を返す（外部への情報漏れを防ぐ）。
    assert state == {"recording": False, "spa": False, "selector": ""}


def test_get_state_returns_real_state_with_token():
    s = _session()
    s.on = True
    s.selector = ".ok"
    state = asyncio.run(s.get_state(None, token=s.token))
    assert state == {"recording": True, "spa": False, "selector": ".ok"}
