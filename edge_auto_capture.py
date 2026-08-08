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
         --user-data-dir="C:\\edge-debug"
  3) その Edge で閲覧しながら実行:
       python edge_auto_capture.py

停止は Ctrl + C。
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ==== 設定（ここだけ触れば調整できます）==========================
CDP_URL = "http://127.0.0.1:9222"   # Edge を起動したデバッグポート（IPv4を明示）
OUTPUT_DIR = Path("output")         # 保存先フォルダ（png も txt もここ）
POLL_INTERVAL = 1.0                 # URL変化を確認する間隔（秒）
SETTLE_DELAY = 0.8                  # 変化検知後、描画が落ち着くまで待つ秒数
LOAD_TIMEOUT = 5000                 # ページ読み込み待ちの上限（ミリ秒）
SKIP_URLS = ("about:blank", "")     # 撮らないURL

# --- 一部抜き出し設定 --------------------------------------------
# 抜きたい箇所の CSS セレクタをここに書く。空("")のままなら一部抜きはスキップ。
# 例: "h1"  /  "article"  /  ".price"  /  "#main .title"
TARGET_SELECTOR = ""
# ================================================================


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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n停止しました。")