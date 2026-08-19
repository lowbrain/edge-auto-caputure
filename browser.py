"""ブラウザ（Edge / Chrome）の起動に関する定義とオプション組み立て。

- 起動を試すブラウザの定義と自動フォールバック順（BROWSER_BY_KEY / AUTO_BROWSER_ORDER）
- config から起動候補を優先順で決める（browser_candidates）
- launch_persistent_context へ渡す起動オプションの組み立て（browser_launch_kwargs）

以前は edge_auto_capture.py 内のモジュール関数だったが、起動処理の定義を 1 か所へ寄せて
エントリを薄くするために切り出した。実際の起動ループ（候補を順に試す）は呼び出し側
（edge_auto_capture の main）が持ち、ここは「何を・どう起動するか」の定義だけを担う。
"""

from config import Config

# ブラウザの定義: config.browser のキー → (channel, 表示名, 実行パスの config 項目名)。
#   channel   … Playwright の channel 名（標準インストール先を自動検出。未インストールなら起動時に例外）。
#   path_attr … 実行ファイルパスを持つ config 項目名（空なら自動検出）。
BROWSER_BY_KEY = {
    "edge": ("msedge", "Edge", "edge_path"),
    "chrome": ("chrome", "Chrome", "chrome_path"),
}
# browser 未指定（自動選択）時に試す優先順。
AUTO_BROWSER_ORDER = ("edge", "chrome")


def browser_candidates(config: Config) -> list[tuple[str, str, str]]:
    """起動を試すブラウザの候補を (channel, 表示名, 実行パス) の優先順で返す。

    config.browser が指定されていれば、その1つだけ（無ければ起動失敗＝終了）。
    空なら Edge→Chrome の順で自動フォールバックする。実行パスは対応する
    config 項目（edge_path / chrome_path）が空でなければそれを使う。
    """
    keys = [config.browser] if config.browser else list(AUTO_BROWSER_ORDER)
    candidates = []
    for key in keys:
        channel, label, path_attr = BROWSER_BY_KEY[key]
        candidates.append((channel, label, getattr(config, path_attr, "")))
    return candidates


def browser_launch_kwargs(
    config: Config, user_data_dir: str, channel: str, executable_path: str = ""
) -> dict:
    """launch_persistent_context に渡すブラウザ起動オプションを組み立てる。

    channel は "msedge" / "chrome" のいずれか（Chromium系で共通の起動引数を使う）。
    executable_path が空でなければ、自動検出よりそのパスを優先する。
    """
    browser_args = [
        # まっさらなプロファイルで起動する（サインイン/同期/初回セットアップを回避）。
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=msImplicitSignin",
        # 既定で最大化して起動する。
        "--start-maximized",
    ]
    kwargs = dict(
        user_data_dir=user_data_dir,
        channel=channel,
        headless=False,
        args=browser_args,
        # Playwright は既定で --no-sandbox を付け、ブラウザが黄色い警告バナーを出す。
        # サンドボックスを有効化してバナーを消す（撮影画像への映り込みも防ぐ）。
        chromium_sandbox=True,
        # 固定ビューポートのエミュレーションを外し、ウィンドウサイズにページを
        # 追従させる（--start-maximized も no_viewport でないと効かない）。
        no_viewport=True,
        # ダウンロードを受理する（E-4）。既定でも真だが意図を明示する。
        # ただし accept_downloads / downloads_path だけでは足りない: Playwright は
        # どちらの場合もコンテキスト終了時にダウンロードを削除するため（実機で確認）、
        # 別途 download イベントで save_as して退避する（CaptureSession.on_download）。
        accept_downloads=True,
    )
    if executable_path:
        kwargs["executable_path"] = executable_path
    return kwargs
