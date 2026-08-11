"""操作バー JS（badge.js / badge.BADGE_SCRIPT）のスモークテスト。

実際の Edge を headless で起動し、add_init_script でバーを注入して
「操作バー（シャドウホスト＋中身）が実際に構築されるか」「Python から呼ぶ
ページ側ヘルパ（barDisplay / bodyText / signature）が例外なく動くか」
「JS エラーが出ないか」を機械的に確認する。JS の構文/実行時エラーを、
実行前に自動検出することが狙い。

使い方:
    python tests/smoke_badge.py
終了コード 0=成功 / 1=失敗。Edge を起動できない環境では SKIP（0）で抜ける。
"""

import sys
from pathlib import Path

# プロジェクト直下（このファイルの親の親）を import パスに入れて badge を読む。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import badge  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

BADGE_SEL = f'#{badge.BADGE_ID}'


def run() -> int:
    errors: "list[str]" = []
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir="",  # 一時プロファイル（空文字＝Playwrightが用意）
                channel="msedge",
                headless=True,
            )
        except Exception as e:
            print(f"SKIP: Edge を起動できませんでした（{e}）")
            return 0

        try:
            page = context.new_page()
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.on(
                "console",
                lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None,
            )
            # add_init_script は「以後の遷移」で走るので、スクリプト登録→遷移の順にする。
            page.add_init_script(badge.BADGE_SCRIPT)
            page.goto("about:blank")

            # 1) シャドウホストが構築されるか（＝スクリプトがパース/実行できている）
            page.wait_for_selector(BADGE_SEL, state="attached", timeout=5000)

            # 2) 状態反映（apply）が例外なく動くか
            page.evaluate("window.__eacApplyState(true, true, 'body')")

            # 3) ページ側ヘルパが動くか
            sig = page.evaluate(badge.SIG_CALL, "body")
            body_text = page.evaluate(badge.BODY_TEXT_CALL)
            page.evaluate(badge.BAR_HIDE)
            page.evaluate(badge.BAR_SHOW)

            # 署名は "<長さ>_<hash>" 形式の文字列を返す。
            if not (isinstance(sig, str) and "_" in sig):
                errors.append(f"signature の戻り値が不正: {sig!r}")
            if not isinstance(body_text, str):
                errors.append(f"bodyText の戻り値が不正: {type(body_text)}")

            # 4) シャドウホストと、その中身（バー本体）が構築されているか最終確認。
            #    バーはシャドウ内なので host.shadowRoot 経由で存在を見る。
            built = page.evaluate(
                "(() => { const h = document.querySelector(" + repr(BADGE_SEL) + ");"
                " return !!(h && h.shadowRoot && h.shadowRoot.querySelector('[data-eac=\"bar\"]')); })()"
            )
            if not built:
                errors.append("操作バーが構築されていません（build 失敗）")
        finally:
            context.close()

    if errors:
        print("FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("PASS: 操作バーの構築・ヘルパ動作・JSエラー無しを確認しました。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
