"""設定（config.ini の読み込み）。

Config データクラスと load_config を提供する。基盤ユーティリティ（infra）だけに依存し、
Playwright には依存しないので、実 Edge 無しで設定読み込みの仕様を回帰テストできる。
"""

import configparser
import sys
from dataclasses import dataclass
from pathlib import Path

from infra import BASE_DIR, log, notify_fatal, resolve_writable_dir, set_log_dir

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"

# browser 設定で受け付ける値 → 正規化後のキー（大文字小文字・別名を吸収）。
# 空文字は「未指定（自動選択）」を表し、ここには含めない。
_BROWSER_ALIASES = {
    "edge": "edge",
    "msedge": "edge",
    "chrome": "chrome",
    "googlechrome": "chrome",
}


def _normalize_browser(raw: str) -> str:
    """browser 設定値を "edge" / "chrome" / "" に正規化する。

    空なら "" を返す（自動選択）。未対応の値は ValueError で弾く。
    """
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "")
    if key not in _BROWSER_ALIASES:
        raise ValueError(
            f"browser は edge か chrome を指定してください（現在: {raw}）"
        )
    return _BROWSER_ALIASES[key]


@dataclass
class Config:
    """config.ini から読み込む設定値一式。

    各フィールドの初期値が「既定値」を兼ねる: config.ini にその項目行が
    無い場合、load_config() はここの値へフォールバックする。
    """

    start_url: str = "about:blank"          # ブラウザ起動時に最初に開くページ
    browser: str = ""                       # 使うブラウザ（"edge"/"chrome"。空なら Edge→Chrome の順で自動選択）
    edge_path: str = ""                     # Edge 実行ファイルのパス（空なら自動検出＝channel="msedge"）
    chrome_path: str = ""                   # Chrome 実行ファイルのパス（空なら自動検出＝channel="chrome"）
    output_dir: Path = Path("output")       # 保存先フォルダ（png / txt / log.txt もここ）
    poll_interval: float = 1.0              # URL変化を確認する間隔（秒）
    settle_delay: float = 0.8               # 変化検知後、描画が落ち着くまで待つ秒数
    load_timeout: int = 5000                # ページ読み込み待ちの上限（ミリ秒）
    eval_timeout: int = 5000                # ページ側 JS 実行（本文取得・撮影合図）の上限（ミリ秒。E-6）
    skip_urls: tuple[str, ...] = ("about:blank", "")   # 撮らないURL
    target_selector: str = ""               # 一部抜き出しの CSS セレクタ（空ならスキップ）
    start_recording: bool = False           # 起動直後に記録を開始するか（False=待機状態で起動）
    profile_dir: str = ""                    # 再利用するブラウザプロファイルの場所（空なら毎回使い捨て）


def load_config() -> Config:
    """config.ini を読み込み、Config を返す。

    ファイルが無い / 値が不正な場合はメッセージを表示して終了する。

    既定値まわりの挙動（現状仕様）:
      - config.ini / [capture] セクションが無い    → メッセージ表示して終了。
      - 項目の「行そのものが無い」                 → Config の既定値を使う
        （sec.get / getfloat / getint の第2引数が既定値）。
      - 数値項目の値だけが空（例: poll_interval =）→ 変換に失敗し終了（ValueError）。
      - output_dir の値が空 → 既定値（output）へフォールバックする
        （空だと Path('.') でカレントへ保存してしまう事故を防ぐ）。
      - edge_path / chrome_path が空 → 自動検出（空が正常値）。値があれば
        起動する各ブラウザの実行ファイルとしてそのパスを使う。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
      - profile_dir が空 → 毎回まっさらな使い捨てプロファイル（既定・従来どおり）。
        値があれば、そのフォルダを再利用（相対パスは基準フォルダ基準に固定）。
      - browser が空 → 自動選択（Edge→Chrome）。edge/chrome 以外の値
        → メッセージ表示して終了（ValueError）。
      - 数値の範囲が不正（poll_interval<=0 / settle_delay<0 / load_timeout<=0
        / eval_timeout<=0）→ メッセージ表示して終了（暴走・無意味値を防ぐ）。
    """
    if not CONFIG_PATH.exists():
        notify_fatal(
            f"設定ファイルが見つかりません: {CONFIG_PATH}\n"
            "exe と同じフォルダに config.ini を置いてください。"
        )
        sys.exit(1)

    # 各 get の第2引数は「その項目行が無い」ときのフォールバック既定値
    # （項目行はあり値だけ空、の場合は空文字/変換エラー側になる点に注意）。
    defaults = Config()
    parser = configparser.ConfigParser()
    try:
        # utf-8-sig: BOM 有無どちらでも読む（A-6）。USAGE.txt が「メモ帳で編集」と
        # 案内しており、メモ帳保存で BOM が混入すると utf-8 では最初のセクション見出しが
        # 壊れ MissingSectionHeaderError になる。BOM を吸収して原因不明の起動失敗を防ぐ。
        parser.read(CONFIG_PATH, encoding="utf-8-sig")
        sec = parser["capture"]

        # 保存先。値が空ならカレントへ落ちないよう既定へ戻す（配布先で編集ミスが起きても安全側に）。
        raw_out = sec.get("output_dir", str(defaults.output_dir)).strip()
        if not raw_out:
            log("[config] output_dir が空のため既定値を使います。")
            raw_out = str(defaults.output_dir)
        # 相対パスは基準フォルダ基準に固定（exe 隣の output\ に確実に保存する）。
        # 絶対パス指定時はそのまま使う（config.ini で任意の保存先に変更可能）。
        output_dir = Path(raw_out)
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir

        # 書き込み可能なフォルダへ解決する（D-C1）。権限の無い場所へ展開されても
        # 無言終了せず、%LOCALAPPDATA% 等へ退避して動き続ける。どこにも書けなければ終了。
        resolved = resolve_writable_dir(output_dir)
        if resolved is None:
            notify_fatal(
                f"保存先フォルダに書き込めませんでした: {output_dir}\n"
                "書き込み可能な場所（例: ドキュメント配下）へ移して実行してください。"
            )
            sys.exit(1)

        # ログも PNG などと同じ保存先へ寄せる（保存先が確定したこの時点で切り替え）。
        # 先に set_log_dir しておくと、退避の通知が確実に書ける退避先ログへ残る。
        set_log_dir(resolved)

        # 退避が起きたら、どこへ保存されるのかをログとダイアログの両方で知らせる
        # （保存先が分からないほうが利用者は困るため）。
        if resolved != output_dir:
            notify_fatal(
                f"保存先 {output_dir} に書き込めないため、{resolved} へ退避して実行します。\n"
                "権限のある場所（例: ドキュメント配下）へ移すと元の設定で保存できます。"
            )
        output_dir = resolved

        # 数値項目。範囲を検証し、不正なら理由付き ValueError（下の except で通知＆終了）。
        poll_interval = sec.getfloat("poll_interval", defaults.poll_interval)
        settle_delay = sec.getfloat("settle_delay", defaults.settle_delay)
        load_timeout = sec.getint("load_timeout", defaults.load_timeout)
        eval_timeout = sec.getint("eval_timeout", defaults.eval_timeout)
        if poll_interval <= 0:
            raise ValueError(f"poll_interval は正の数にしてください（現在: {poll_interval}）")
        if settle_delay < 0:
            raise ValueError(f"settle_delay は 0 以上にしてください（現在: {settle_delay}）")
        if load_timeout <= 0:
            raise ValueError(f"load_timeout は正の整数にしてください（現在: {load_timeout}）")
        if eval_timeout <= 0:
            raise ValueError(f"eval_timeout は正の整数にしてください（現在: {eval_timeout}）")

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        urls = [u.strip() for u in sec.get("skip_urls", "").split(",") if u.strip()]

        # 再利用プロファイルの場所。空なら毎回使い捨て（既定の挙動を据え置く）。
        # 指定時のみ、相対パスは基準フォルダ基準に固定して絶対パス文字列で保持する
        # （output_dir と同じ扱い。書き込み可能な exe 隣を既定基準にできる）。
        raw_profile = sec.get("profile_dir", "").strip()
        if raw_profile:
            profile_path = Path(raw_profile)
            if not profile_path.is_absolute():
                profile_path = BASE_DIR / profile_path
            profile_dir = str(profile_path)
        else:
            profile_dir = ""

        return Config(
            start_url=sec.get("start_url", defaults.start_url).strip() or "about:blank",
            browser=_normalize_browser(sec.get("browser", defaults.browser)),
            edge_path=sec.get("edge_path", "").strip(),
            chrome_path=sec.get("chrome_path", "").strip(),
            output_dir=output_dir,
            poll_interval=poll_interval,
            settle_delay=settle_delay,
            load_timeout=load_timeout,
            eval_timeout=eval_timeout,
            skip_urls=tuple(urls) + ("",),
            target_selector=sec.get("target_selector", "").strip(),
            start_recording=sec.getboolean("start_recording", defaults.start_recording),
            profile_dir=profile_dir,
        )
    except (configparser.Error, KeyError, ValueError) as e:
        notify_fatal(
            f"config.ini の読み込みに失敗しました: {e}\n"
            "[capture] セクションと各項目の値を確認してください。"
        )
        sys.exit(1)
