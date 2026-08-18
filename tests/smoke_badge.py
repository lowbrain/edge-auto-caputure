"""操作バー JS（badge.js / badge.build_badge_script()）のスモークテスト。

実際の Edge を headless で起動し、add_init_script でバーを注入して
「操作バー（シャドウホスト＋中身）が実際に構築されるか」「Python から呼ぶ
ページ側ヘルパ（captureStart / captureEnd / bodyText / signature）が例外なく動くか」
「JS エラーが出ないか」を機械的に確認する。JS の構文/実行時エラーを、
実行前に自動検出することが狙い。

使い方:
    python tests/smoke_badge.py            # 手軽な確認（ブラウザ不在は SKIP=0）
    python tests/smoke_badge.py --strict   # CI 向け（ブラウザ不在は FAIL=1）
終了コード 0=成功 / 1=失敗。実アプリと同じく Edge を優先し、無ければ Chrome へ
フォールバックする（どちらも Chromium 系でバー JS の検証は等価）。

Edge も Chrome も起動できない環境では既定で SKIP（0）で抜ける。ただし CI では
「ブラウザが無いから何も検証していないのに緑」になるのを防ぐため、`--strict` を
付けるとブラウザ不在を FAIL（1）として扱う。
"""

import argparse
import sys
import time
from pathlib import Path

# 進捗ログに日本語/全角を含むため、標準出力を UTF-8 へ固定する。英語ロケール Windows
# （端末既定 cp1252 など）では print 時点で UnicodeEncodeError になるのを防ぐ（Issue #3）。
# reconfigure は Python 3.7+。差し替え済みで持たない場合もあるため hasattr で守る。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

# プロジェクト直下（このファイルの親の親）を import パスに入れて badge を読む。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright  # noqa: E402

import badge  # noqa: E402

BADGE_SEL = f'#{badge.BADGE_ID}'


# 実アプリ（edge_auto_capture）と同じく Edge を優先し、無ければ Chrome へ回す。
# 同じ Chromium 系なので、操作バー JS の構築・挙動の検証としては等価。
_CHANNELS = ("msedge", "chrome")


def run(strict: bool = False) -> int:
    errors: list[str] = []
    # E-3: ページ側ヘルパは固定名でなく、起動ごとのランダム名 ns の隠しオブジェクトに収まる。
    # 実アプリと同じく new_namespace() で採番し、build_badge_script と各 *_call へ通す。
    ns = badge.new_namespace()
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
            if strict:
                # CI（--strict）ではブラウザ不在を「検証できていない」＝失敗として扱う。
                print(f"FAIL(--strict): Edge/Chrome のいずれも起動できませんでした（{last_err}）")
                return 1
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
            # 完成スクリプトは必要時に build_badge_script() で組み立てる（R5a: import 時 I/O 回避）。
            # E-3: ns を渡し、ページ側ヘルパを window[ns] の隠しオブジェクトへ収める。
            page.add_init_script(badge.build_badge_script(ns=ns))
            page.goto("about:blank")

            # 1) シャドウホストが構築されるか（＝スクリプトがパース/実行できている）
            page.wait_for_selector(BADGE_SEL, state="attached", timeout=5000)

            # 2) 状態反映（apply）が例外なく動くか
            page.evaluate(badge.apply_state_call(ns, True, True, "body"))

            # 3) ページ側ヘルパが動くか
            sig = page.evaluate(badge.sig_call(ns), "body")
            body_text = page.evaluate(badge.body_text_call(ns))
            page.evaluate(badge.capture_start_call(ns))  # 撮影退避（Promise を待つ）
            page.evaluate(badge.capture_end_call(ns, True))    # シャッターフラッシュ＋バー復帰

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

            # 6b) F-D4: 「保存先」ボタンが存在し、押すと open_folder バインディングを呼ぶか。
            #     expose_binding を公開しないスモークでは、callBinding のフォールバック
            #     （退避が空なら実行時 window を見る）で後差しの関数が拾われる（SPA 検知と同じ）。
            open_folder = page.evaluate(
                "(() => { window.__openCalls = [];"
                " window.__eac_open_folder = (tok) => { window.__openCalls.push(tok); };"
                " const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                " const btn = sr && sr.querySelector('[data-eac=\"open\"]');"
                " if (!btn) return 'missing';"
                " btn.click();"
                " return window.__openCalls.length === 1 ? 'ok' : 'no-call'; })()"
            )
            if open_folder != "ok":
                errors.append(f"F-D4: 「保存先」ボタンが機能していません: {open_folder}")

            # 6c) F-D2: セレクタ入力欄の datalist に候補が入り、input が list 属性で紐づくか。
            #     setHistory で候補を配り、datalist の <option> と input.list が一致するか見る。
            history = page.evaluate(
                "(() => { " + badge.set_history_call(ns, ['#main', '.price']) + ";"
                " const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                " const dl = sr && sr.querySelector('[data-eac=\"history\"]');"
                " const inp = sr && sr.querySelector('[data-eac=\"selector\"]');"
                " if (!dl || !inp) return 'missing';"
                " const opts = Array.from(dl.querySelectorAll('option')).map((o) => o.value);"
                " const linked = inp.getAttribute('list') === dl.id && !!dl.id;"
                " return (linked && opts.join(',') === '#main,.price') ? 'ok'"
                "   : ('mismatch:' + opts.join(',') + '/' + linked); })()"
            )
            if history != "ok":
                errors.append(f"F-D2: セレクタ履歴（datalist）が機能していません: {history}")

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
            page.evaluate(badge.apply_state_call(ns, True, True, ""))  # 記録ON・SPA検知ON・既定ルート
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

            page.evaluate(badge.capture_end_call(ns, True))     # フラッシュを付ける（撮影直後の合図）
            had_flash = _has_flash()
            page.evaluate(badge.capture_start_call(ns))   # 次の退避（A-1: フラッシュを畳む）
            still_flash = _has_flash()
            page.evaluate(badge.capture_end_call(ns, True))     # 状態を戻す（capDepth を均衡させる）
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
            page.evaluate(badge.capture_start_call(ns))   # 退避（capturing=true, capDepth=1）
            page.evaluate(badge.capture_end_call(ns, True))     # 終了（capDepth=0、直後は capturing 継続）
            t0 = time.monotonic()
            page.evaluate(badge.capture_start_call(ns))   # 退避済みからの再退避（A-2: 即解決するはず）
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            page.evaluate(badge.capture_end_call(ns, True))     # 後始末（capDepth を均衡させる）
            # 修正あり: 数ms。修正なし: CAP_FALLBACK_MS(500ms) 近く待つ。間を取って 250ms で判定。
            if elapsed_ms >= 250:
                errors.append(
                    f"A-2: 退避済みからの captureStart が {elapsed_ms:.0f}ms 待ちました"
                    "（transitionend 不発で 500ms 無駄待ちする回帰）"
                )

            # 10) B-6 の回帰: MutationObserver を常時稼働から「必要なときだけ」に変えた。
            #     (a) バー再構築監視は body の childList のみ（subtree なし）に絞ったので、
            #         host を body 直下から消しても付け直されること。
            #     (b) SPA検知監視は spaActive の切り替わりで observe/disconnect する。
            #         SPA検知OFF（記録OFF）にした後は、DOM が変化しても通知が飛ばないこと。
            page.evaluate("document.querySelector(" + repr(BADGE_SEL) + ").remove()")
            try:
                page.wait_for_selector(BADGE_SEL, state="attached", timeout=3000)
            except Exception:
                errors.append("B-6(a): host を削除してもバーが再構築されませんでした")

            # (b) いったん SPA ON で監視が繋がっている状態から OFF へ落とし、以降の DOM 変化で
            #     通知（__eac_spa_changed）が増えないことを確認する（Observer が切断されている）。
            page.evaluate(badge.apply_state_call(ns, True, True, ""))   # 記録ON・SPA ON（接続）
            page.evaluate(badge.apply_state_call(ns, False, False, ""))  # 記録OFF・SPA OFF（切断）
            page.evaluate("window.__spaCalls = [];")
            for i in range(5):
                page.evaluate(
                    "document.body.appendChild(Object.assign("
                    "document.createElement('div'), { textContent: 'b6-" + str(i) + "-' + Date.now() }))"
                )
            # settleMs(300ms) を十分に超えて待ち、それでも通知ゼロなら切断できている。
            time.sleep(1.0)
            leaked_calls = page.evaluate("window.__spaCalls.length")
            if leaked_calls:
                errors.append(
                    f"B-6(b): SPA検知OFF後も DOM 変化で通知が来ました（{leaked_calls}件・"
                    "Observer が切断されていない）"
                )

            # 11) F-D3: 撮影カウンタと失敗フラッシュの色分け。
            #     (a) captureEnd(false)（保存全滅）は成功（赤）と別色（.fail）でフラッシュする。
            #         成功（引数なし＝赤）では .fail が付かないこと。frame は closed シャドウ内。
            def _frame_classes() -> str:
                return page.evaluate(
                    "(() => { const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                    " const f = sr && sr.querySelector('[data-eac=\"frame\"]');"
                    " return f ? f.className : ''; })()"
                )

            page.evaluate(badge.capture_end_call(ns, False))   # 失敗の合図
            fail_cls = _frame_classes()
            page.evaluate(badge.capture_start_call(ns))           # 後始末（フラッシュを畳む）
            page.evaluate(badge.capture_end_call(ns, True))    # 成功の合図
            ok_cls = _frame_classes()
            page.evaluate(badge.capture_start_call(ns))           # 後始末
            page.evaluate(badge.capture_end_call(ns, True))             # capDepth を均衡させる
            if "flash" not in fail_cls or "fail" not in fail_cls:
                errors.append(f"F-D3: 失敗フラッシュに .fail が付いていません（class='{fail_cls}'）")
            if "fail" in ok_cls:
                errors.append(f"F-D3: 成功フラッシュに .fail が付いています（class='{ok_cls}'）")

            #     (b) setCount(n) がバーの撮影カウンタ表示（本セッション N 枚）へ反映されること。
            shots_text = page.evaluate(
                "(() => { " + badge.set_count_call(ns, 3) + ";"
                " const sr = window.__eac_debugRoot && window.__eac_debugRoot();"
                " const s = sr && sr.querySelector('[data-eac=\"shots\"]');"
                " return s ? s.textContent : ''; })()"
            )
            if "3" not in shots_text:
                errors.append(f"F-D3: 撮影カウンタが更新されません（表示='{shots_text}'）")

            # 12) F-B2: hide_selectors 指定時、撮影中（captureStart 後）だけ該当要素が
            #     visibility:hidden になり、撮影後（captureEnd 後）に元へ戻ること。
            #     hide_selectors は build_badge_script に埋め込むので、別ページを用意して検証する。
            hp = context.new_page()
            hp.on("pageerror", lambda exc: errors.append(f"pageerror(F-B2): {exc}"))
            hp.add_init_script(badge.build_badge_script("", 300, ("#eac-hide-me",), ns))
            hp.goto("about:blank")
            hp.wait_for_selector(BADGE_SEL, state="attached", timeout=5000)
            hp.evaluate(
                "document.body.appendChild(Object.assign(document.createElement('div'),"
                " { id: 'eac-hide-me', textContent: 'banner' }))"
            )

            def _hide_vis() -> str:
                return hp.evaluate(
                    "(() => { const el = document.getElementById('eac-hide-me');"
                    " return el ? el.style.visibility : '(no-el)'; })()"
                )

            before = _hide_vis()
            hp.evaluate(badge.capture_start_call(ns))   # 撮影退避＋対象を隠す
            during = _hide_vis()
            hp.evaluate(badge.capture_end_call(ns, True))     # 撮影後＝元へ戻す
            after = _hide_vis()
            if during != "hidden":
                errors.append(f"F-B2: 撮影中に対象が隠れていません（visibility='{during}'）")
            if after == "hidden":
                errors.append("F-B2: 撮影後も対象が隠れたままです（元へ戻っていない）")
            if before == "hidden":
                errors.append("F-B2: 撮影前から対象が隠れています（テストの前提が崩れている）")

            # 13) E-3: 実運用ビルド（token 付き）で、サイトから固定名の存在検知ができないこと。
            #     これまでのステップは token 無しビルドで、②の固定名削除経路を通っていない。
            #     ここだけ実運用と同じく expose_binding を生やし、token/ns 付きで注入して確認する:
            #       (a) Python→ページのヘルパ固定名（__eacApplyState 等）が window に無い。
            #       (b) ページ→Python のバインディング固定名（__eac_toggle 等）が退避後に消えている。
            #       (c) ヘルパは列挙されないランダム名 ns の隠しオブジェクトからは使える（非列挙）。
            #       (d) 固定名を消しても機能は保たれる（badge.js が build 時に呼ぶ __eac_getstate が
            #           Python まで届く＝退避した本物参照が delete 後も生きている）。
            _BINDINGS = [
                "__eac_toggle", "__eac_shot", "__eac_open_folder", "__eac_spa_toggle",
                "__eac_set_selector", "__eac_commit_selector", "__eac_spa_changed",
                "__eac_getstate",
            ]
            getstate_hits: list[int] = []
            ep = context.new_page()
            ep.on("pageerror", lambda exc: errors.append(f"pageerror(E-3): {exc}"))
            # __eac_getstate だけ着火を数える。他はダミー（本物同様に window へ生やして削除対象にする）。
            ep.expose_binding("__eac_getstate", lambda source, *a: getstate_hits.append(1))
            for _bname in _BINDINGS:
                if _bname == "__eac_getstate":
                    continue
                ep.expose_binding(_bname, lambda source, *a: None)
            ns_prod = badge.new_namespace()
            ep.add_init_script(badge.build_badge_script("smoke-token", 300, (), ns_prod))
            ep.goto("about:blank")
            ep.wait_for_selector(BADGE_SEL, state="attached", timeout=5000)
            detect = ep.evaluate(
                "(() => ({"
                " applyFixed: ('__eacApplyState' in window),"
                " toggleFixed: ('__eac_toggle' in window),"
                " getstateFixed: ('__eac_getstate' in window),"
                " nsPresent: (" + repr(ns_prod) + " in window),"
                " nsEnum: Object.keys(window).includes(" + repr(ns_prod) + "),"
                " nsHasApply: !!(window[" + repr(ns_prod) + "]"
                "   && typeof window[" + repr(ns_prod) + "].applyState === 'function')"
                "}))()"
            )
            if detect["applyFixed"]:
                errors.append("E-3(a): __eacApplyState が window に残っています（ヘルパ固定名の検知が可能）")
            if detect["toggleFixed"] or detect["getstateFixed"]:
                errors.append(f"E-3(b): バインディング固定名が window に残っています（{detect}）")
            if not detect["nsPresent"] or not detect["nsHasApply"]:
                errors.append(f"E-3(c): ヘルパの隠しオブジェクト（ns）が使えません（{detect}）")
            if detect["nsEnum"]:
                errors.append("E-3(c): ns プロパティが列挙可能（enumerable）です（Object.keys に出る）")
            ep.wait_for_timeout(300)  # build 時の __eac_getstate 着火を待つ
            if not getstate_hits:
                errors.append(
                    "E-3(d): 固定名削除後に __eac_getstate が Python へ届きません"
                    "（退避参照が delete で失われた＝機能退行）"
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
        "保存先フォルダを開く(F-D4)・セレクタ履歴(F-D2)・"
        "SPA検知の通知・フラッシュ写り込み防止(A-1)・退避済み即解決(A-2)・"
        "Observer の必要時のみ稼働(B-6)・撮影カウンタ/失敗フラッシュ(F-D3)・"
        "撮影中バナー除去(F-B2)・固定名の存在検知不能化(E-3)・JSエラー無しを確認しました。"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="操作バー JS のスモークテスト")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="ブラウザ（Edge/Chrome）が起動できない場合を SKIP ではなく FAIL にする（CI 向け）",
    )
    args = parser.parse_args()
    sys.exit(run(strict=args.strict))
