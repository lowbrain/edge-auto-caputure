"""
Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

このスクリプトが Edge の起動・監視・後始末までを一括で行う（Playwright が
毎回まっさらな一時プロファイルで Edge を起動し、終了時に自動で掃除する）。

事前準備:
  1) pip install playwright
     playwright install

起動方法:
  - run.bat をダブルクリック、または
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
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# 保存ファイル名の一意性を保証する通し番号（この起動中で連番）。
# next() は不可分なので、並行する capture() 同士でも番号は重複しない。
_seq_counter = itertools.count(1)

# ==== 設定（値は config.ini から load_config() で読み込む）========
# 実際の値はスクリプトと同じフォルダの config.ini で調整する。
# 以下の各代入値は「既定値」を兼ねる: config.ini にその項目の
# 行そのものが無い場合、load_config() はここの値へフォールバックする。
# （項目行はあるが値だけ空の場合の扱いは load_config() のコメント参照）
CONFIG_PATH = Path(__file__).with_name("config.ini")

START_URL = "about:blank"           # Edge 起動時に最初に開くページ（空なら about:blank）
EDGE_PATH = ""                      # Edge 実行ファイルのパス（空なら channel="msedge" に委ねる）
OUTPUT_DIR = Path("output")         # 保存先フォルダ（png も txt もここ）
POLL_INTERVAL = 1.0                 # URL変化を確認する間隔（秒）
SETTLE_DELAY = 0.8                  # 変化検知後、描画が落ち着くまで待つ秒数
LOAD_TIMEOUT = 5000                 # ページ読み込み待ちの上限（ミリ秒）
SKIP_URLS = ("about:blank", "")     # 撮らないURL
TARGET_SELECTOR = ""                # 一部抜き出しの CSS セレクタ（空ならスキップ）
# ================================================================


def load_config() -> None:
    """config.ini を読み込み、モジュールグローバルの設定値へ反映する。

    ファイルが無い / 値が不正な場合はメッセージを表示して終了する。

    既定値まわりの挙動（現状仕様）:
      - config.ini / [capture] セクションが無い    → メッセージ表示して終了。
      - 項目の「行そのものが無い」                 → モジュール冒頭の既定値を使う
        （sec.get / getfloat / getint の第2引数が既定値）。
      - 数値項目の値だけが空（例: poll_interval =）→ 変換に失敗し終了（ValueError）。
      - 文字列項目の値だけが空（cdp_url / output_dir）→ 空文字がそのまま入る。
        ※ output_dir が空だと Path('.') となりカレントフォルダへ保存されるので注意。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
    """
    global START_URL, EDGE_PATH, OUTPUT_DIR, POLL_INTERVAL, SETTLE_DELAY
    global LOAD_TIMEOUT, SKIP_URLS, TARGET_SELECTOR

    if not CONFIG_PATH.exists():
        print(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        print("スクリプトと同じフォルダに config.ini を置いてください。")
        sys.exit(1)

    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_PATH, encoding="utf-8")
        sec = parser["capture"]
        # 第2引数は「その項目行が無い」ときのフォールバック既定値。
        # （項目行はあり値だけ空、の場合は空文字/変換エラー側になる点に注意）
        START_URL = sec.get("start_url", START_URL).strip() or "about:blank"
        EDGE_PATH = sec.get("edge_path", "").strip()
        OUTPUT_DIR = Path(sec.get("output_dir", str(OUTPUT_DIR)))
        POLL_INTERVAL = sec.getfloat("poll_interval", POLL_INTERVAL)
        SETTLE_DELAY = sec.getfloat("settle_delay", SETTLE_DELAY)
        LOAD_TIMEOUT = sec.getint("load_timeout", LOAD_TIMEOUT)
        TARGET_SELECTOR = sec.get("target_selector", "").strip()

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        raw = sec.get("skip_urls", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        SKIP_URLS = tuple(urls) + ("",)
    except (configparser.Error, KeyError, ValueError) as e:
        print(f"config.ini の読み込みに失敗しました: {e}")
        print("[capture] セクションと各項目の値を確認してください。")
        sys.exit(1)


def safe_name(url: str) -> str:
    """URL をファイル名に使える形へ変換（長すぎる場合は先頭80文字）。"""
    name = re.sub(r"[^\w\-]+", "_", url)[:80].strip("_")
    return name or "page"


async def capture(page, url: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    # 一意性は「ミリ秒付き日時 + 通し番号」で保証する。URL スラッグは
    # 読みやすさ用の装飾で、切り詰めや正規化で衝突しても実害はない。
    # seq / stem は await より前に確定させ、番号を予約してから保存する。
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    ms = now.strftime("%f")[:3]              # マイクロ秒の先頭3桁＝ミリ秒
    seq = next(_seq_counter)
    stem = f"{ts}_{ms}_{seq:04d}_{safe_name(url)}"   # 3ファイルで同じ接頭辞を共有

    # 読み込み完了を待つ（タイムアウトしても続行）
    try:
        await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT)
    except Exception:
        pass
    await asyncio.sleep(SETTLE_DELAY)

    # 1) フルページ スクリーンショット
    try:
        await page.screenshot(path=str(OUTPUT_DIR / f"{stem}.png"), full_page=True)
    except Exception as e:
        print(f"[skip png]  {url}  ({e})")

    # 2) ページ全文テキスト
    try:
        text = await page.inner_text("body")
        (OUTPUT_DIR / f"{stem}.txt").write_text(
            f"URL: {url}\n\n{text}", encoding="utf-8"
        )
    except Exception as e:
        print(f"[skip txt]  {url}  ({e})")

    # 3) 一部抜き出し（セレクタ設定時のみ）
    if TARGET_SELECTOR:
        try:
            parts = await page.locator(TARGET_SELECTOR).all_inner_texts()
            body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
            (OUTPUT_DIR / f"{stem}_part.txt").write_text(
                f"URL: {url}\nSELECTOR: {TARGET_SELECTOR}\n\n{body}",
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[skip part] {url}  ({e})")

    print(f"[saved] {stem}.*  <- {url}")


def cleanup_old_profiles() -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。
    """
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


async def main() -> None:
    seen: dict = {}  # page オブジェクト -> 直近のURL

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
    if EDGE_PATH:
        launch_kwargs["executable_path"] = EDGE_PATH

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
            if START_URL and START_URL != "about:blank":
                try:
                    await page.goto(START_URL)
                except Exception as e:
                    print(f"[skip goto] {START_URL}  ({e})")

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
                    if url in SKIP_URLS:
                        continue
                    if seen.get(pg) != url:
                        seen[pg] = url
                        asyncio.create_task(capture(pg, url))

                await asyncio.sleep(POLL_INTERVAL)
        finally:
            # Ctrl+C / ウィンドウを閉じた場合のどちらでもここが走る。
            # 起動した Edge を終了し、一時プロファイルを削除する。
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    load_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n停止しました。")