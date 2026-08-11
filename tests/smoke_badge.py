"""操作バー JS（badge.js / badge.BADGE_SCRIPT）のスモークテスト。

実際の Edge を headless で起動し、add_init_script でバーを注入して
「操作バー（シャドウホスト＋中身）が実際に構築されるか」「Python から呼ぶ
ページ側ヘルパ（captureStart / captureEnd / bodyText / signature）が例外なく動くか」
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

from playwright.sync_api import sync_playwright  # noqa: E402

import badge  # noqa: E402

BADGE_SEL = f'#{badge.BADGE_ID}'


def run() -> int:
    errors: list[str] = []
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
            page.evaluate(badge.CAPTURE_START_CALL)  # 撮影退避（Promise を待つ）
            page.evaluate(badge.CAPTURE_END_CALL)    # シャッターフラッシュ＋バー復帰

            # 署名は "<長さ>_<hash>" 形式の文字列を返す。
            if not (isinstance(sig, str) and "_" in sig):
                errors.append(f"signature の戻り値が不正: {sig!r}")
            if not isinstance(body_text, str):
                errors.append(f"bodyText の戻り値が不正: {type(body_text)}")

            # 4) シャドウホストと、その中身（バー本体）が構築されているか最終確認。
            #    バーはシャドウ内なので host.shadowRoot 経由で存在を見る。
            built = page.evaluate(
                "(() => { const h = document.querySelector(" + repr(BADGE_SEL) + ");"
                " return !!(h && h.shadowRoot"
                " && h.shadowRoot.querySelector('[data-eac=\"bar\"]')); })()"
            )
            if not built:
                errors.append("操作バーが構築されていません（build 失敗）")

            # 5) 透過トグル（枠なしアイコン）が存在し、押すたびに ON/OFF が切り替わるか。
            #    透過はローカル状態なので、クリック→バーの 'peek' とボタンの 'on' が付き、
            #    もう一度押すと外れることを確認する（アイコンの装飾切替の土台がこのクラス）。
            peek = page.evaluate(
                "(() => { const h = document.querySelector(" + repr(BADGE_SEL) + ");"
                " const sr = h && h.shadowRoot;"
                " const btn = sr && sr.querySelector('[data-eac=\"peek\"]');"
                " const bar = sr && sr.querySelector('[data-eac=\"bar\"]');"
                " if (!btn || !bar) return 'missing';"
                " btn.click();"
                " const on = bar.classList.contains('peek') && btn.classList.contains('on');"
                " btn.click();"
                " const off = !bar.classList.contains('peek') && !btn.classList.contains('on');"
                " return on && off ? 'ok' : 'toggle-failed'; })()"
            )
            if peek != "ok":
                errors.append(f"透過トグルが機能していません: {peek}")

            # 6) SPA検知のイベント駆動監視が通しで動くか。
            #    記録側の通知バインディング（__eac_spa_changed）をテスト用のコレクタに差し替え、
            #    SPA検知を ON（セレクタ空＝既定ルート監視）にしてから本文を書き換える。デバウンス
            #    確定後にコレクタが呼ばれれば、MutationObserver→落ち着き判定→通知の一連が動いている。
            page.evaluate(
                "window.__spaCalls = [];"
                "window.__eac_spa_changed = (tok, sig) => { window.__spaCalls.push(sig); };"
            )
            page.evaluate("window.__eacApplyState(true, true, '')")  # 記録ON・SPA検知ON・既定ルート
            page.evaluate(
                "document.body.appendChild(Object.assign("
                "document.createElement('div'), { textContent: 'spa-change-' + Date.now() }))"
            )
            try:
                # 既定 settleMs は 300ms。デバウンス確定を十分待つ。
                page.wait_for_function(
                    "window.__spaCalls && window.__spaCalls.length > 0", timeout=3000
                )
            except Exception:
                errors.append("SPA検知の変化通知（__eac_spa_changed）が発火しませんでした")
        finally:
            context.close()

    if errors:
        print("FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("PASS: 操作バーの構築・ヘルパ動作・透過トグル・JSエラー無しを確認しました。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
