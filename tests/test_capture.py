"""1 ページ分の保存処理（capture.py）のユニットテスト。

ファイル名スラッグ（safe_name / page_label）・保存ステップの集約（_step, A-3）・
ページ側 JS のハング保護（try_eval, E-6）・撮影キューの合流（B-3）・撮影要求
（CaptureRequest）・索引 CSV（F-A1 / F-A4）を守る。実 Edge 不要。

実行:
    pip install -e ".[dev]"
    pytest
"""

import asyncio
import csv
import re
from pathlib import Path

import pytest

import capture
import infra
from capture import (
    INDEX_CSV_HEADER,
    CaptureRequest,
    CaptureRunner,
    page_label,
    safe_name,
    trigger_label,
)
from config import Config

# --------------------------------------------------------------------------- #
# safe_name
# --------------------------------------------------------------------------- #


def test_safe_name_keeps_japanese():
    # \w は Unicode 対応なので日本語はそのまま残る。
    assert safe_name("テスト") == "テスト"


def test_safe_name_replaces_symbols_and_spaces_with_dash():
    assert safe_name("a/b c") == "a-b-c"


def test_safe_name_empty_falls_back_to_page():
    assert safe_name("") == "page"


def test_safe_name_separator_only_falls_back_to_page():
    # 空白・記号のみは区切り文字（-）だけに潰れるが、前後ストリップで空になり
    # "page" へフォールバックする。
    assert safe_name("   ") == "page"
    assert safe_name("///") == "page"


def test_safe_name_keeps_interior_dashes():
    # 前後だけを落とし、内部の区切りは保持する。
    assert safe_name("-a/b-") == "a-b"


def test_safe_name_strips_leading_trailing_underscore():
    # strip("_") は前後のアンダースコアのみ落とす（内部は残す）。
    assert safe_name("_hello_") == "hello"


def test_safe_name_truncates_to_max_len():
    long = "a" * 200
    result = safe_name(long)
    assert len(result) == capture.NAME_MAX_LEN
    assert result == "a" * capture.NAME_MAX_LEN


# --------------------------------------------------------------------------- #
# _step（A-3: 全失敗でも [saved] と出さないための done 集約）
# --------------------------------------------------------------------------- #


def test_step_appends_tag_on_success():
    done: list[str] = []
    with capture._step("png", "http://x", done):
        pass
    assert done == ["png"]


def test_step_does_not_append_on_exception():
    done: list[str] = []
    with capture._step("png", "http://x", done):
        raise RuntimeError("boom")  # _step が握って [skip] ログを出す
    assert done == []  # 失敗したステップは積まれない


def test_step_without_done_still_swallows_exception():
    # done を渡さない従来の使い方（load / title）でも例外を握ることは変わらない。
    with capture._step("load", "http://x"):
        raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# try_eval のハング保護（E-6: 返ってこない evaluate で worker を止めない）
# --------------------------------------------------------------------------- #


class _HangingPage:
    """evaluate が永遠に返らないページ代役（ページのメインスレッド停止を模す）。"""

    def __init__(self) -> None:
        self.eval_started = False

    async def evaluate(self, js, *args):
        self.eval_started = True
        await asyncio.Event().wait()  # 誰も set しない＝永久に待つ


def test_try_eval_gives_up_after_timeout():
    # timeout を渡すと、返らない evaluate でも打ち切って戻る（例外も握る）。
    async def scenario():
        page = _HangingPage()
        # timeout が無ければここで永久にハングする。wait_for で全体を縛って検証。
        await asyncio.wait_for(
            capture.try_eval(page, "never()", timeout=0.05), timeout=1
        )
        assert page.eval_started  # 実際に evaluate へ入ったうえで打ち切ったこと

    asyncio.run(scenario())


def test_try_eval_without_timeout_would_hang():
    # timeout=None（既定）だと打ち切りが無いことの対比確認。0.1 秒待っても終わらない。
    async def scenario():
        page = _HangingPage()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(capture.try_eval(page, "never()"), timeout=0.1)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# page_label
# --------------------------------------------------------------------------- #


def test_page_label_prefers_title():
    assert page_label("My Page", "https://example.com") == "My-Page"


def test_page_label_falls_back_to_url_without_scheme_and_www():
    # タイトルが空なら URL から scheme と www. を落とした名前で代替。
    assert page_label("", "https://www.example.com/path") == "example-com-path"


def test_page_label_whitespace_title_uses_url():
    assert page_label("   ", "http://foo.com") == "foo-com"


# --------------------------------------------------------------------------- #
# CaptureRunner の合流（B-3: 撮影キュー無制限の防止）
#
# spawn は「ページごとに実行中1件＋保留1件（最新で置き換え）」に合流させる。
# 実 Edge を使わず、_capture をスタブ化して「実際に何件・どの params で走ったか」だけを
# 見ることで、合流ロジックそのものを速いユニットテストで回帰から守る。
# --------------------------------------------------------------------------- #


class _FakePage:
    """WeakKeyDictionary のキーになれる最小のページ代役（weakref 可能な実体）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<FakePage {self.name}>"


def _recording_runner(gate: "asyncio.Event | None" = None):
    """_capture を「呼び出し記録用スタブ」に差し替えた CaptureRunner を返す。

    gate を渡すと各撮影は gate がセットされるまで待つ（撮影を in-flight に保ち、
    その間に来た spawn が合流することを確かめるため）。戻り値の calls に
    (page, url, selector) が実行順で積まれる。
    """
    runner = CaptureRunner()
    calls: list[tuple] = []

    async def stub(req):
        calls.append((req.page, req.url, req.selector))
        if gate is not None:
            await gate.wait()

    runner._capture = stub  # インスタンス属性がクラスメソッドを上書きする
    return runner, calls


def test_spawn_coalesces_synchronous_burst():
    # worker が動き出す前に連続 spawn した分は、最新1件へ合流する。
    async def scenario():
        runner, calls = _recording_runner()
        page = _FakePage("p")
        cfg = Config()
        for i in range(5):
            runner.spawn(CaptureRequest(page, f"url-{i}", cfg))
        assert len(runner._tasks) == 1  # ページごとに worker は1つだけ
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert calls == [(page, "url-4", "")]  # 走るのは最後の1件だけ

    asyncio.run(scenario())


def test_spawn_coalesces_requests_during_capture():
    # 撮影中(in-flight)に来た複数要求は、最新1件だけに合流して次に走る。
    async def scenario():
        gate = asyncio.Event()
        runner, calls = _recording_runner(gate)
        page = _FakePage("p")
        cfg = Config()

        runner.spawn(CaptureRequest(page, "url-1", cfg))
        worker = runner._workers[page]
        for _ in range(5):  # 第1撮影を gate 待ちまで進める
            await asyncio.sleep(0)
        assert calls == [(page, "url-1", "")]  # 1件目が in-flight

        # 撮影中に3回要求 → _pending は最新(url-4, sel-4)で上書きされる
        runner.spawn(CaptureRequest(page, "url-2", cfg, "sel-2"))
        runner.spawn(CaptureRequest(page, "url-3", cfg, "sel-3"))
        runner.spawn(CaptureRequest(page, "url-4", cfg, "sel-4"))

        gate.set()  # 1件目を解放。worker がループして最新1件だけを撮る
        await asyncio.wait_for(worker, timeout=1)

        # 4回積んでも走ったのは2件（in-flight の1 + 合流後の最新1）。selector も最新。
        assert calls == [(page, "url-1", ""), (page, "url-4", "sel-4")]
        assert page not in runner._workers  # 空になったら worker は退場
        assert page not in runner._pending

    asyncio.run(scenario())


def test_spawn_different_pages_run_independently():
    # 別ページは別 worker。互いに合流せず、それぞれ撮られる。
    async def scenario():
        runner, calls = _recording_runner()
        p1, p2 = _FakePage("1"), _FakePage("2")
        cfg = Config()
        runner.spawn(CaptureRequest(p1, "a", cfg))
        runner.spawn(CaptureRequest(p2, "b", cfg))
        assert len(runner._tasks) == 2  # ページごとに worker
        await asyncio.wait_for(asyncio.gather(*list(runner._tasks)), timeout=1)
        assert sorted(c[1] for c in calls) == ["a", "b"]

    asyncio.run(scenario())


def test_spawn_restarts_worker_after_drain():
    # 保留を撃ち尽くして worker が退場した後の spawn は、新しい worker を立てて走る
    #（終了と再要求の競合で取りこぼさないことの確認）。
    async def scenario():
        runner, calls = _recording_runner()
        page = _FakePage("p")
        cfg = Config()
        runner.spawn(CaptureRequest(page, "first", cfg))
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert page not in runner._workers  # drain 後に退場

        runner.spawn(CaptureRequest(page, "second", cfg))  # 再度 spawn → 新 worker
        await asyncio.wait_for(runner._workers[page], timeout=1)
        assert [c[1] for c in calls] == ["first", "second"]

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# CaptureRequest（撮影 1 回分の要求オブジェクト）
#
# spawn→_pending→_worker→_capture を貫通する位置引数タプルを 1 オブジェクトへ集約した器。
# 既定値と、要求が _capture まで欠けずに届くこと（R1 の配線）を回帰から守る。
# --------------------------------------------------------------------------- #


def test_capture_request_defaults():
    # selector / group_id / trigger は省略時に空文字（未指定）になる。
    page = _FakePage("p")
    cfg = Config()
    req = CaptureRequest(page, "https://example.test/", cfg)
    assert req.page is page
    assert req.url == "https://example.test/"
    assert req.config is cfg
    assert req.selector == ""
    assert req.group_id == ""
    assert req.trigger == ""


def test_capture_request_holds_all_fields():
    # 全フィールドを与えると、その値がそのまま保持される。
    page = _FakePage("p")
    cfg = Config()
    req = CaptureRequest(
        page, "https://example.test/x", cfg, "#main", "20260814101105674", "spa"
    )
    assert (req.selector, req.group_id, req.trigger) == (
        "#main", "20260814101105674", "spa"
    )


def test_spawn_delivers_request_to_capture_unchanged():
    # spawn した CaptureRequest が、page/url/selector/group_id を欠かさず _capture へ届く。
    async def scenario():
        runner = CaptureRunner()
        received: list[CaptureRequest] = []

        async def stub(req):
            received.append(req)

        runner._capture = stub
        page = _FakePage("p")
        cfg = Config()
        req = CaptureRequest(page, "https://example.test/y", cfg, "#part", "20260814101105674")
        runner.spawn(req)
        await asyncio.wait_for(runner._workers[page], timeout=1)

        assert len(received) == 1
        got = received[0]
        assert got is req  # 同じオブジェクトがそのまま渡る
        assert (got.page, got.url, got.selector, got.group_id) == (
            page, "https://example.test/y", "#part", "20260814101105674"
        )

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 索引 CSV（F-A1）+ 撮影時刻（F-A4）
#
# 撮影ごとに output_dir/index.csv へ 1 行追記する。地雷 2 つ（BOM 付き utf-8-sig で書く／
# 時刻は ISO 8601 オフセット付き）と、追記時に BOM・見出しを重複させないことを回帰から守る。
# --------------------------------------------------------------------------- #


def test_iso_timestamp_is_offset_aware_iso8601():
    # F-A4: ISO 8601・ミリ秒・UTC オフセット付き（例: 2026-08-11T14:30:25.123+09:00）。
    ts = infra.iso_timestamp()
    from datetime import datetime

    parsed = datetime.fromisoformat(ts)   # 解釈不能なら例外で落ちる
    assert parsed.tzinfo is not None      # オフセット（tzinfo）を必ず持つ
    # ミリ秒精度: 小数第 3 位まで（マイクロ秒の 6 桁ではない）。
    assert re.search(r"T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$", ts)


def test_log_line_uses_offset_timestamp(monkeypatch, tmp_path):
    # log() の行頭時刻もオフセット付き ISO（索引と突き合わせられるよう表記をそろえる）。
    monkeypatch.setattr(infra, "LOG_PATH", tmp_path / "log.txt")
    infra.log("hello")
    line = (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()[0]
    stamp, _, msg = line.partition(" ")
    assert msg == "hello"
    from datetime import datetime

    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_trigger_label_maps_known_and_passes_through():
    assert trigger_label("manual") == "手動"
    assert trigger_label("url") == "URL変化"
    assert trigger_label("spa") == "SPA変化"
    assert trigger_label("") == ""          # 未設定はそのまま
    assert trigger_label("other") == "other"  # 未知値は素通し


def _read_index(path: Path) -> list[list[str]]:
    """index.csv を読み、ヘッダ込みの行リストを返す（BOM は utf-8-sig で剥がす）。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def test_append_index_writes_bom_header_and_row(tmp_path):
    # 新規作成時: 先頭 BOM＋見出し＋データ 1 行。列は仕様どおりの並び・値。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path)
    cfg.output_dir.mkdir(exist_ok=True)
    req = CaptureRequest(
        _FakePage("p"), "https://example.test/x", cfg, "#main", "20260811143025123", "spa"
    )
    runner._append_index(
        req, "2026-08-11T14:30:25.123+09:00",
        "タイトル,あり", "2026-08-11_14-30-25-123_stem", ["png", "txt"],
    )
    path = tmp_path / "index.csv"
    # 地雷1: BOM 付きで書く（BOM 無しだと Excel で文字化け）。
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _read_index(path)
    assert rows[0] == ["時刻", "URL", "タイトル", "ファイル名接頭辞", "撮影契機", "セレクタ", "成否"]
    assert rows[1] == [
        "2026-08-11T14:30:25.123+09:00", "https://example.test/x",
        "タイトル,あり",   # カンマ入りタイトルも csv が退避して 1 セルに収まる
        "2026-08-11_14-30-25-123_stem", "SPA変化", "#main", "成功",
    ]


def test_append_index_appends_without_duplicate_bom_or_header(tmp_path):
    # 追記時: BOM も見出しも増やさず、行だけ足す。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path)
    cfg.output_dir.mkdir(exist_ok=True)
    runner._append_index(
        CaptureRequest(_FakePage("p1"), "u1", cfg, "", "", "manual"),
        "t1", "titleA", "stemA", ["png"],
    )
    runner._append_index(
        CaptureRequest(_FakePage("p2"), "u2", cfg, "#s", "", "url"),
        "t2", "titleB", "stemB", [],
    )
    raw = (tmp_path / "index.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\xef\xbb\xbf") == 1        # BOM は先頭 1 回だけ
    rows = _read_index(tmp_path / "index.csv")
    assert len(rows) == 3                          # 見出し + 2 行
    assert rows[1][4] == "手動" and rows[1][6] == "成功"
    assert rows[2][4] == "URL変化" and rows[2][6] == "失敗"  # done 空 → 失敗


def test_append_index_failure_does_not_raise(monkeypatch, tmp_path):
    # 索引の書き込み失敗は握って撮影を巻き込まない（ログに [skip index] を出すだけ）。
    runner = CaptureRunner()
    cfg = Config(output_dir=tmp_path / "missing")   # 親フォルダが無く open が失敗する
    logged: list[str] = []
    monkeypatch.setattr(capture, "log", lambda m: logged.append(m))
    req = CaptureRequest(_FakePage("p"), "u", cfg, "", "", "manual")
    runner._append_index(req, "t", "ti", "st", ["png"])  # 例外は出ない
    assert any("[skip index]" in m for m in logged)



class _IndexPage:
    """_capture を実 Edge 無しで 1 周させるための最小ページ代役（索引 CSV の検証用）。"""

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def title(self):
        return "ページ題名"

    async def screenshot(self, path=None, full_page=None):
        Path(path).write_bytes(b"png")

    async def evaluate(self, js, *args):
        return "body text"

    def locator(self, selector):
        raise AssertionError("この回帰テストでは _part.txt は使わない")


def test_capture_writes_index_row_from_request(tmp_path):
    # _capture → _append_index の受け渡しが、CaptureRequest の各値を正しい列へ落とすこと。
    # trigger と selector は隣接する同型（str）で、取り違えても型検査では止まらない（#55）。
    # 値を区別できる形（"spa"→"SPA変化" と "#main"）で与え、列の入れ替わりを検知する。
    async def scenario():
        runner = CaptureRunner()
        cfg = Config(output_dir=tmp_path, settle_delay=0)
        req = CaptureRequest(
            _IndexPage(), "https://example.test/z", cfg,
            selector="", group_id="20260811143025123", trigger="spa",
        )
        await runner._capture(req)

        rows = _read_index(tmp_path / "index.csv")
        assert rows[0] == INDEX_CSV_HEADER
        assert len(rows) == 2
        row = rows[1]
        assert row[1] == "https://example.test/z"   # URL
        assert row[2] == "ページ題名"                 # タイトル（撮影中に確定＝引数のまま）
        assert row[3].endswith("_ページ題名")          # ファイル名接頭辞
        assert row[4] == "SPA変化"                   # 撮影契機（req.trigger 由来）
        assert row[5] == ""                          # セレクタ（req.selector 由来）
        assert row[6] == "成功"

    asyncio.run(scenario())
