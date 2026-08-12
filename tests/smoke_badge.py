"""操作バー JS（badge.js / badge.BADGE_SCRIPT）のスモークテスト。

実際の Edge を headless で起動し、add_init_script でバーを注入して
「操作バー（シャドウホスト＋中身）が実際に構築されるか」「Python から呼ぶ
ページ側ヘルパ（captureStart / captureEnd / bodyText / signature）が例外なく動くか」
「JS エラーが出ないか」を機械的に確認する。JS の構文/実行時エラーを、
実行前に自動検出することが狙い。

使い方:
    python tests/smoke_badge.py
終了コード 0=成功 / 1=失敗。実アプリと同じく Edge を優先し、無ければ Chrome へ
フォールバックする（どちらも Chromium 系でバー JS の検証は等価）。Edge も Chrome も
起動できない環境では SKIP（0）で抜ける。
"""

import sys
import time
from pathlib import Path

# プロジェクト直下（このファイルの親の親）を import パスに入れて badge を読む。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

import badge  # noqa: E402

BADGE_SEL = f'#{badge.BADGE_ID}'


# 実アプリ（edge_auto_capture）と同じく Edge を優先し、無ければ Chrome へ回す。
# 同じ Chromium 系なので、操作バー JS の構築・挙動の検証としては等価。
_CHANNELS = ("msedge", "chrome")


def run() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        context = None
        launched = ""
        last_err: object = None
        for channel in _CHANNELS:
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir="",  # 一時プロファイル（空文字＝Playwrightが用意）
                    channel=channel,
                    headless=True,
                )
                launched = channel
                break
            except Exception as e:
                last_err = e
        if context is None:
            print(f"SKIP: Edge/Chrome のいずれも起動できませんでした（{last_err}）")
            return 0
        print(f"（起動ブラウザ: {launched}）")

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
            #    シャドウは closed なので host.shadowRoot からは入れない。テスト専用の
            #    __eac_debugRoot()（token 無しビルドでのみ公開される）経由で中を見る。
            built = page.evaluate(
                "(() => { const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                " return !!(sr && sr.querySelector('[data-eac=\"bar\"]')); })()"
            )
            if not built:
                errors.append("操作バーが構築されていません（build 失敗）")

            # 5) A-4 の回帰: シャドウが closed であること。
            #    open に戻ると、閲覧中サイトのスクリプトが
            #    document.getElementById(ID).shadowRoot.querySelector(...).click() で
            #    記録操作を起こせてしまい、token 照合が UI 経由で迂回される。
            leaked = page.evaluate(
                "(() => { const h = document.querySelector(" + repr(BADGE_SEL) + ");"
                " return !!(h && h.shadowRoot); })()"
            )
            if leaked:
                errors.append("シャドウが open です（closed であるべき: A-4 の回帰）")

            # 6) 透過トグル（枠なしアイコン）が存在し、押すたびに ON/OFF が切り替わるか。
            #    透過はローカル状態なので、クリック→バーの 'peek' とボタンの 'on' が付き、
            #    もう一度押すと外れることを確認する（アイコンの装飾切替の土台がこのクラス）。
            peek = page.evaluate(
                "(() => { const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
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

            # 7) SPA検知のイベント駆動監視が通しで動くか。
            #    記録側の通知バインディング（__eac_spa_changed）をテスト用のコレクタに差し替え、
            #    SPA検知を ON（セレクタ空＝既定ルート監視）にしてから本文を書き換える。デバウンス
            #    確定後にコレクタが呼ばれれば、MutationObserver→落ち着き→通知の一連が動いている。
            #
            #    ここで後から window へ差し込んだ関数が拾われるのは、badge.js の callBinding が
            #    「退避済み参照が無ければ実行時の window を見る」フォールバックを持つため。
            #    このテストは expose_binding を公開しないので退避側は空になる。
            #    フォールバックを外すとこのステップが動かなくなる（外す場合は注入順を変えること）。
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

            # 8) A-1 の回帰: captureEnd 直後に captureStart を呼ぶと、残っていたシャッター
            #    フラッシュ（.frame.flash）が畳まれ、次のスクショへ赤みが写り込まないこと。
            #    frame は closed シャドウ内なので __eac_debugRoot() 経由で確認する。
            def _has_flash() -> bool:
                return page.evaluate(
                    "(() => { const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                    " const f = sr && sr.querySelector('[data-eac=\"frame\"]');"
                    " return !!(f && f.classList.contains('flash')); })()"
                )

            page.evaluate(badge.CAPTURE_END_CALL)     # フラッシュを付ける（撮影直後の合図）
            had_flash = _has_flash()
            page.evaluate(badge.CAPTURE_START_CALL)   # 次の退避（A-1: フラッシュを畳む）
            still_flash = _has_flash()
            page.evaluate(badge.CAPTURE_END_CALL)     # 状態を戻す（capDepth を均衡させる）
            if not had_flash:
                errors.append("captureEnd 後にフラッシュが付いていません（A-1 テストの前提が崩れている）")
            if still_flash:
                errors.append("captureStart 後もフラッシュが残っています（A-1 の回帰）")

            # 9) A-2 の回帰: バーが既に退避済み（capturing）のまま次の captureStart が
            #    来たとき、transitionend を待たず即座に解決すること。修正前は
            #    classList.add('capturing') が no-op で transitionend が飛ばず、
            #    CAP_FALLBACK_MS(500ms) まで無駄に待っていた。
            #    captureEnd 直後（capturing を外す barTimer が発火する前）に captureStart を
            #    呼ぶと、バーは capturing のまま capDepth が 1 に戻り、この分岐に入る。
            page.evaluate(badge.CAPTURE_START_CALL)   # 退避（capturing=true, capDepth=1）
            page.evaluate(badge.CAPTURE_END_CALL)     # 終了（capDepth=0、直後は capturing 継続）
            t0 = time.monotonic()
            page.evaluate(badge.CAPTURE_START_CALL)   # 退避済みからの再退避（A-2: 即解決するはず）
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            page.evaluate(badge.CAPTURE_END_CALL)     # 後始末（capDepth を均衡させる）
            # 修正あり: 数ms。修正なし: CAP_FALLBACK_MS(500ms) 近く待つ。間を取って 250ms で判定。
            if elapsed_ms >= 250:
                errors.append(
                    f"A-2: 退避済みからの captureStart が {elapsed_ms:.0f}ms 待ちました"
                    "（transitionend 不発で 500ms 無駄待ちする回帰）"
                )
        finally:
            context.close()

    if errors:
        print("FAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(
        "PASS: 操作バーの構築・ヘルパ動作・シャドウ closed・透過トグル・"
        "SPA検知の通知・フラッシュ写り込み防止(A-1)・退避済み即解決(A-2)・"
        "JSエラー無しを確認しました。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
