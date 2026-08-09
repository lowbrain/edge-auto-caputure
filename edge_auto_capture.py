"""
Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

事前準備:
  1) pip install playwright
     playwright install
  2) 既存の Edge を一度すべて閉じてから、デバッグポート付きで起動:
     Windows:
       "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" ^
         --remote-debugging-port=9222 ^
         --user-data-dir="C:\\edge-debug" ^
         https://example.com
     ※ 末尾の URL が最初に開くページ。省略すると空白ページで起動する。
       ランチャ(start_edge_debug.ps1 / run.bat)で起動する場合は、この URL を
       config.ini の start_url で指定する（空なら about:blank）。
  3) その Edge で閲覧しながら実行:
       python edge_auto_capture.py

設定はソースではなく、同じフォルダの config.ini を編集して変更する。
停止は Ctrl + C。
"""

import asyncio
import configparser
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ==== 設定（値は config.ini から load_config() で読み込む）========
# 実際の値はスクリプトと同じフォルダの config.ini で調整する。
# 以下の各代入値は「既定値」を兼ねる: config.ini にその項目の
# 行そのものが無い場合、load_config() はここの値へフォールバックする。
# （項目行はあるが値だけ空の場合の扱いは load_config() のコメント参照）
CONFIG_PATH = Path(__file__).with_name("config.ini")

CDP_URL = "http://127.0.0.1:9222"   # Edge を起動したデバッグポート（IPv4を明示）
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
    global CDP_URL, OUTPUT_DIR, POLL_INTERVAL, SETTLE_DELAY
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
        CDP_URL = sec.get("cdp_url", CDP_URL)
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
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = f"{ts}_{safe_name(url)}"          # 3ファイルで同じ接頭辞を共有

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


async def main() -> None:
    seen: dict = {}  # page オブジェクト -> 直近のURL

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"Edge に接続できませんでした: {e}")
            print("Edge をデバッグポート付き(--remote-debugging-port=9222)で"
                  "起動しているか確認してください。")
            return

        print("Edge に接続しました。URL/タブの変化を監視します（Ctrl+Cで停止）")

        while True:
            pages = [pg for ctx in browser.contexts for pg in ctx.pages]

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


if __name__ == "__main__":
    load_config()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n停止しました。")