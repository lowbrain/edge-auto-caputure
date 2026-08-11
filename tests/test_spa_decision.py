"""spa_capture_decision（SPA検知の落ち着き判定）のユニットテスト。

監視ループ（CaptureSession.run）から切り出した純粋関数を、実 Edge 無しで検証する。
「多段レンダの途中を撮らない」「落ち着いたら1回だけ撮る」「URL変化 tick では撮らず
基準を取り直す」という、最も間違えやすい仕様を回帰から守るのが狙い。
"""

from capture import SpaDecision, spa_capture_decision

# --------------------------------------------------------------------------- #
# 単発の判定
# --------------------------------------------------------------------------- #


def test_url_changed_never_captures_and_reseeds_baseline():
    # URL変化 tick では撮らない（URL側で撮影済み）。ただし現署名を基準へ取り直す。
    d = spa_capture_decision("NEW", url_changed=True, sig_seen="OLD", sig_prev="OLD")
    assert d == SpaDecision(capture=False, sig_seen="NEW")


def test_settled_change_captures():
    # 前回撮影時(A)と違い、かつ前 tick と同じ（落ち着いた）→ 撮る。sig_seen を更新。
    d = spa_capture_decision("B", url_changed=False, sig_seen="A", sig_prev="B")
    assert d == SpaDecision(capture=True, sig_seen="B")


def test_unsettled_change_waits():
    # 変わったが前 tick と署名が違う（＝まだ描画途中）→ 撮らず sig_seen を据え置く。
    d = spa_capture_decision("B", url_changed=False, sig_seen="A", sig_prev="MID")
    assert d == SpaDecision(capture=False, sig_seen="A")


def test_no_change_does_not_capture():
    # 前回撮影時と同じ署名 → 撮らない。
    d = spa_capture_decision("A", url_changed=False, sig_seen="A", sig_prev="A")
    assert d == SpaDecision(capture=False, sig_seen="A")


def test_none_baseline_new_page_does_not_capture():
    # reseed 前の新規ページ（基準が未設定）では撮らず、基準も未設定のまま。
    d = spa_capture_decision("A", url_changed=False, sig_seen=None, sig_prev=None)
    assert d == SpaDecision(capture=False, sig_seen=None)


# --------------------------------------------------------------------------- #
# tick 列のシミュレーション（ループと同じ状態遷移を回して挙動を確認）
# --------------------------------------------------------------------------- #


def _run_ticks(baseline, sigs, url_changed_at=()):
    """SPA検知 ON 直後（sig_seen=sig_prev=baseline）から tick 列を流し、
    撮影が発生した tick の index リストを返す。CaptureSession.run と同じ更新順を再現する。
    """
    sig_seen = baseline
    sig_prev = baseline
    captured_at = []
    for i, sig in enumerate(sigs):
        d = spa_capture_decision(sig, i in url_changed_at, sig_seen, sig_prev)
        sig_seen = d.sig_seen
        if d.capture:
            captured_at.append(i)
        sig_prev = sig  # sig_prev は撮影可否に関わらず毎 tick 更新（呼び出し側の責務）
    return captured_at


def test_multiframe_render_captures_once_after_settle():
    # baseline A → 中間フレーム MID を経て B に落ち着く。撮影は「落ち着いた最初の tick」で1回だけ。
    captured = _run_ticks("A", ["A", "MID", "B", "B", "B"])
    assert captured == [3]


def test_instant_change_needs_one_stable_tick():
    # 中間フレーム無しでも、落ち着き確認のため次 tick で1回だけ撮る。
    captured = _run_ticks("A", ["B", "B"])
    assert captured == [1]


def test_two_distinct_changes_each_captured_once():
    # A→B→C と2回変化。各変化につき落ち着いてから1回ずつ撮る。
    captured = _run_ticks("A", ["B", "B", "C", "C"])
    assert captured == [1, 3]


def test_url_change_midstream_resets_and_avoids_double_capture():
    # tick2 で URL変化（新内容 P）。その tick では撮らず基準を P に取り直すので、
    # 直後に P が続いても「変化」とはみなさず二重撮りしない。
    captured = _run_ticks("A", ["A", "A", "P", "P"], url_changed_at={2})
    assert captured == []
