"""
Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

このスクリプトが Edge の起動・監視・後始末までを一括で行う（Playwright が
毎回まっさらな一時プロファイルで Edge を起動し、終了時に自動で掃除する）。

事前準備:
  pip install -e .          （または pip install playwright）
  ※ システムにインストール済みの Edge をそのまま使うため、
    playwright install（ブラウザ同梱バイナリの取得）は不要。

起動方法:
  - scripts\\run.bat をダブルクリック、または
  - python edge_auto_capture.py
  最初に開くページ・保存先などは同じフォルダの config.ini で指定する
  （起動ページは start_url。空なら about:blank）。開いた Edge で普通に
  閲覧すると、URL/タブの変化ごとに output\\ へ自動保存される。

設定はソースではなく、同じフォルダの config.ini を編集して変更する。
停止は Ctrl + C、または Edge のウィンドウを閉じる。停止すると、この
スクリプトが起動した Edge の終了と一時プロファイルの削除まで行う。
"""

import asyncio
import configparser
import itertools
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Tuple

from playwright.async_api import Page, async_playwright

def _base_dir() -> Path:
    """設定・保存先の基準フォルダを返す。

    PyInstaller で exe 化した場合（sys.frozen）は exe のあるフォルダ、
    通常の Python 実行時はこのスクリプトのあるフォルダを基準にする。
    これにより、配布した exe の隣に置いた config.ini を読み、
    output\\ も exe の隣に作れる（＝第三者が config.ini を編集できる）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# 設定・出力の基準フォルダ（通常実行なら本ファイル、exe 実行なら exe と同じ場所）。
BASE_DIR = _base_dir()

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80

# 保存ファイル名の一意性を保証する通し番号（この起動中で連番）。
# next() は不可分なので、並行する capture() 同士でも番号は重複しない。
_seq_counter = itertools.count(1)

# 実行中の capture タスクへの強参照を保持する集合。
# これが無いとイベントループはタスクを弱参照でしか持たず、
# 実行途中で GC されて消える恐れがある（例外も握り潰される）。
_tasks: set = set()


@dataclass
class Config:
    """config.ini から読み込む設定値一式。

    各フィールドの初期値が「既定値」を兼ねる: config.ini にその項目行が
    無い場合、load_config() はここの値へフォールバックする。
    """

    start_url: str = "about:blank"          # Edge 起動時に最初に開くページ
    edge_path: str = ""                     # Edge 実行ファイルのパス（空なら channel="msedge"）
    output_dir: Path = Path("output")       # 保存先フォルダ（png も txt もここ）
    poll_interval: float = 1.0              # URL変化を確認する間隔（秒）
    settle_delay: float = 0.8               # 変化検知後、描画が落ち着くまで待つ秒数
    load_timeout: int = 5000                # ページ読み込み待ちの上限（ミリ秒）
    skip_urls: Tuple[str, ...] = ("about:blank", "")   # 撮らないURL
    target_selector: str = ""               # 一部抜き出しの CSS セレクタ（空ならスキップ）


def load_config() -> Config:
    """config.ini を読み込み、Config を返す。

    ファイルが無い / 値が不正な場合はメッセージを表示して終了する。

    既定値まわりの挙動（現状仕様）:
      - config.ini / [capture] セクションが無い    → メッセージ表示して終了。
      - 項目の「行そのものが無い」                 → Config の既定値を使う
        （sec.get / getfloat / getint の第2引数が既定値）。
      - 数値項目の値だけが空（例: poll_interval =）→ 変換に失敗し終了（ValueError）。
      - 文字列項目の値だけが空（output_dir / edge_path など）→ 空文字がそのまま入る。
        ※ output_dir が空だと Path('.') となりカレントフォルダへ保存されるので注意。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
    """
    if not CONFIG_PATH.exists():
        print(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        print("スクリプトと同じフォルダに config.ini を置いてください。")
        sys.exit(1)

    defaults = Config()
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
        sec = parser["capture"]
        # 第2引数は「その項目行が無い」ときのフォールバック既定値。
        # （項目行はあり値だけ空、の場合は空文字/変換エラー側になる点に注意）
        start_url = sec.get("start_url", defaults.start_url).strip() or "about:blank"
        edge_path = sec.get("edge_path", "").strip()
        # 相対パスは基準フォルダ基準に固定（exe 隣の output\ に確実に保存する）。
        # 絶対パス指定時はそのまま使う（config.ini で任意の保存先に変更可能）。
        output_dir = Path(sec.get("output_dir", str(defaults.output_dir)))
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir
        poll_interval = sec.getfloat("poll_interval", defaults.poll_interval)
        settle_delay = sec.getfloat("settle_delay", defaults.settle_delay)
        load_timeout = sec.getint("load_timeout", defaults.load_timeout)
        target_selector = sec.get("target_selector", "").strip()

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        raw = sec.get("skip_urls", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        skip_urls = tuple(urls) + ("",)
    except (configparser.Error, KeyError, ValueError) as e:
        print(f"config.ini の読み込みに失敗しました: {e}")
        print("[capture] セクションと各項目の値を確認してください。")
        sys.exit(1)

    return Config(
        start_url=start_url,
        edge_path=edge_path,
        output_dir=output_dir,
        poll_interval=poll_interval,
        settle_delay=settle_delay,
        load_timeout=load_timeout,
        skip_urls=skip_urls,
        target_selector=target_selector,
    )


def safe_name(url: str) -> str:
    """URL をファイル名に使える形へ変換（長すぎる場合は先頭 NAME_MAX_LEN 文字）。"""
    name = re.sub(r"[^\w\-]+", "_", url)[:NAME_MAX_LEN].strip("_")
    return name or "page"


@contextmanager
def _step(tag: str, url: str):
    """保存処理 1 ステップ分の共通ラッパ。

    例外が出ても [skip <tag>] を表示して握り、他ステップの続行を妨げない。
    （png / txt / part の 3 ステップで同じ try/except を書かないための共通化）
    """
    try:
        yield
    except Exception as e:
        print(f"[skip {tag}] {url}  ({e})")


async def capture(page: Page, url: str, config: Config) -> None:
    # 一意性は「ミリ秒付き日時 + 通し番号」で保証する。URL スラッグは
    # 読みやすさ用の装飾で、切り詰めや正規化で衝突しても実害はない。
    # seq / stem は await より前に確定させ、番号を予約してから保存する。
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    ms = now.strftime("%f")[:3]              # マイクロ秒の先頭3桁＝ミリ秒
    seq = next(_seq_counter)
    stem = f"{ts}_{ms}_{seq:04d}_{safe_name(url)}"   # 3ファイルで同じ接頭辞を共有

    # 読み込み完了を待つ（タイムアウトしても続行）
    with _step("load", url):
        await page.wait_for_load_state("load", timeout=config.load_timeout)
    await asyncio.sleep(config.settle_delay)

    # 1) フルページ スクリーンショット
    with _step("png", url):
        await page.screenshot(
            path=str(config.output_dir / f"{stem}.png"), full_page=True
        )

    # 2) ページ全文テキスト
    with _step("txt", url):
        text = await page.inner_text("body")
        (config.output_dir / f"{stem}.txt").write_text(
            f"URL: {url}\n\n{text}", encoding="utf-8"
        )

    # 3) 一部抜き出し（セレクタ設定時のみ）
    if config.target_selector:
        with _step("part", url):
            parts = await page.locator(config.target_selector).all_inner_texts()
            body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
            (config.output_dir / f"{stem}_part.txt").write_text(
                f"URL: {url}\nSELECTOR: {config.target_selector}\n\n{body}",
                encoding="utf-8",
            )

    print(f"[saved] {stem}.*  <- {url}")


def _spawn_capture(page: Page, url: str, config: Config) -> None:
    """capture() をバックグラウンドタスクとして起動し、参照を保持する。

    タスクを _tasks に入れて GC を防ぎ、完了時に取り除く。
    """
    task = asyncio.create_task(capture(page, url, config))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def cleanup_old_profiles() -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。
    """
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


async def main(config: Config) -> None:
    seen: dict = {}  # page オブジェクト -> 直近のURL

    # 保存先は起動時に一度だけ作成（親フォルダごと）。
    config.output_dir.mkdir(parents=True, exist_ok=True)

    cleanup_old_profiles()
    tmp = tempfile.mkdtemp(prefix="edge-debug-")  # 今回用の一時プロファイル

    # まっさらなプロファイルで起動するための Edge 起動オプション
    # （サインイン/同期ダイアログや初回セットアップ画面を回避する）
    edge_args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=msImplicitSignin",
    ]
    launch_kwargs = dict(
        user_data_dir=tmp,
        channel="msedge",
        headless=False,
        args=edge_args,
    )
    if config.edge_path:
        launch_kwargs["executable_path"] = config.edge_path

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            print(f"Edge を起動できませんでした: {e}")
            print("Edge がインストールされているか、config.ini の edge_path を確認してください。")
            shutil.rmtree(tmp, ignore_errors=True)
            return

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
                    print(f"[skip goto] {config.start_url}  ({e})")

            print("Edge を起動しました。URL/タブの変化を監視します（Ctrl+Cで停止）")

            while not closed.is_set():
                pages = list(context.pages)

                # 閉じられたページを管理から除去
                for pg in list(seen):
                    if pg not in pages:
                        del seen[pg]

                # URL変化 / 新規タブを検知して保存
                for pg in pages:
                    try:
                        url = pg.url
                    except Exception:
                        continue
                    if url in config.skip_urls:
                        continue
                    if seen.get(pg) != url:
                        seen[pg] = url
                        _spawn_capture(pg, url, config)

                await asyncio.sleep(config.poll_interval)
        finally:
            # Ctrl+C / ウィンドウを閉じた場合のどちらでもここが走る。
            # 起動した Edge を終了し、一時プロファイルを削除する。
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    config = load_config()
    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        print("\n停止しました。")
