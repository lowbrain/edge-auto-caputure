"""設定（config.ini の読み込み）。

Config データクラスと load_config を提供する。基盤ユーティリティ（infra）だけに依存し、
Playwright には依存しないので、実 Edge 無しで設定読み込みの仕様を回帰テストできる。
"""

import configparser
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from infra import BASE_DIR, log, notify_fatal, set_log_dir

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"


@dataclass
class Config:
    """config.ini から読み込む設定値一式。

    各フィールドの初期値が「既定値」を兼ねる: config.ini にその項目行が
    無い場合、load_config() はここの値へフォールバックする。
    """

    start_url: str = "about:blank"          # Edge 起動時に最初に開くページ
    edge_path: str = ""                     # Edge 実行ファイルのパス（空なら channel="msedge"）
    output_dir: Path = Path("output")       # 保存先フォルダ（png / txt / log.txt もここ）
    poll_interval: float = 1.0              # URL変化を確認する間隔（秒）
    settle_delay: float = 0.8               # 変化検知後、描画が落ち着くまで待つ秒数
    load_timeout: int = 5000                # ページ読み込み待ちの上限（ミリ秒）
    skip_urls: Tuple[str, ...] = ("about:blank", "")   # 撮らないURL
    target_selector: str = ""               # 一部抜き出しの CSS セレクタ（空ならスキップ）
    start_recording: bool = False           # 起動直後に記録を開始するか（False=待機状態で起動）


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
      - edge_path など他の文字列項目が空 → 空文字がそのまま入る（空が正常値）。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
      - 数値の範囲が不正（poll_interval<=0 / settle_delay<0 / load_timeout<=0）
        → メッセージ表示して終了（暴走・無意味値を防ぐ）。
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
        parser.read(CONFIG_PATH, encoding="utf-8")
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

        # ログも PNG などと同じ保存先へ寄せる（保存先が確定したこの時点で切り替え）。
        set_log_dir(output_dir)

        # 数値項目。範囲を検証し、不正なら理由付き ValueError（下の except で通知＆終了）。
        poll_interval = sec.getfloat("poll_interval", defaults.poll_interval)
        settle_delay = sec.getfloat("settle_delay", defaults.settle_delay)
        load_timeout = sec.getint("load_timeout", defaults.load_timeout)
        if poll_interval <= 0:
            raise ValueError(f"poll_interval は正の数にしてください（現在: {poll_interval}）")
        if settle_delay < 0:
            raise ValueError(f"settle_delay は 0 以上にしてください（現在: {settle_delay}）")
        if load_timeout <= 0:
            raise ValueError(f"load_timeout は正の整数にしてください（現在: {load_timeout}）")

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        urls = [u.strip() for u in sec.get("skip_urls", "").split(",") if u.strip()]

        return Config(
            start_url=sec.get("start_url", defaults.start_url).strip() or "about:blank",
            edge_path=sec.get("edge_path", "").strip(),
            output_dir=output_dir,
            poll_interval=poll_interval,
            settle_delay=settle_delay,
            load_timeout=load_timeout,
            skip_urls=tuple(urls) + ("",),
            target_selector=sec.get("target_selector", "").strip(),
            start_recording=sec.getboolean("start_recording", defaults.start_recording),
        )
    except (configparser.Error, KeyError, ValueError) as e:
        notify_fatal(
            f"config.ini の読み込みに失敗しました: {e}\n"
            "[capture] セクションと各項目の値を確認してください。"
        )
        sys.exit(1)
