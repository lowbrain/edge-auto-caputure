"""設定（config.ini の読み込み）。

Config データクラスと load_config を提供する。基盤ユーティリティ（infra）だけに依存し、
Playwright には依存しないので、実 Edge 無しで設定読み込みの仕様を回帰テストできる。

読み切れない設定は ConfigFatalError で返す（プロセスは落とさない）。終了の判断は入口
（edge_auto_capture.cli）の 1 か所に集める（#49）。
"""

import configparser
import fnmatch
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from infra import BASE_DIR, log, notify_fatal, resolve_writable_dir, set_log_dir

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"


class ConfigFatalError(Exception):
    """設定を読み切れず、起動を続けられないときに送出する（#49）。

    ライブラリ層（この config モジュール）はプロセスを落とさない。以前は notify_fatal →
    sys.exit(1) をここで直接呼んでいたため、「設定を返す」はずの load_config が失敗時には
    返らずプロセスを終わらせ、GUI 設定画面・設定検証コマンド・テストの別経路から再利用
    できなかった（テストも「どう失敗したか」ではなく「落ちたこと」しか見られなかった）。

    メッセージには利用者向けのダイアログ文面をそのまま載せる。通知（notify_fatal）と終了
    コードの決定は入口（edge_auto_capture.cli）の 1 か所に集める。
    """

# 自己修復（D-C3）で書き出す既定 config.ini の中身。配布する config.ini と同一
# （drift はテスト test_default_config_text_matches_bundled_ini で担保する）。
# 削除・破損しても notify_fatal→exit で起動不能にせず、これで作り直して既定値で起動する。
# 改行は LF で持ち、write_text がプラットフォームの改行へ変換する（Windows なら CRLF）。
DEFAULT_CONFIG_TEXT = """\
[capture]
# ブラウザを起動したときに最初に開くページ（空なら about:blank）
# 例: https://example.com  /  https://www.google.com
start_url = https://www.google.com

# 使うブラウザ（edge / chrome）。指定したブラウザだけを起動する（未インストールなら終了）。
# 空にすると Edge を優先し、無ければ Chrome を使う（どちらも無ければ終了）。
# インストール先は自動検出する（標準インストールなら追加設定は不要）。
browser =

# Edge 実行ファイルのパス（空なら自動検出。非標準インストール時のみ指定）
# 例: C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
edge_path =

# Chrome 実行ファイルのパス（空なら自動検出。非標準インストール時のみ指定）
# 例: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe
chrome_path =

# 保存先フォルダ（png も txt もここ）
output_dir = output

# 変化検知後、描画が落ち着くまで待つ秒数。0 以上（負数はエラー）
settle_delay = 0.8

# ページ読み込み待ちの上限（ミリ秒）。正の整数（0 や負数はエラー）
load_timeout = 5000

# ページ側 JS（本文テキスト取得・撮影の合図）の実行を待つ上限（ミリ秒）。正の整数。
# ページが重い処理で固まると本文取得が返らず、そのページの撮影が止まり続けるため、
# ここで打ち切って次へ進む。取りこぼしが増えるなら大きく、固まりを早く諦めたいなら小さく。
eval_timeout = 5000

# 撮らないURL（カンマ区切り。空URLは常に自動スキップ）
# 前方一致で判定するのでクエリ付き（?...）でも効く。* ? [ を含めると
# ワイルドカード（fnmatch）扱い。例: about:blank, https://ads.example.com, *://*/logout
skip_urls = about:blank

# 撮るURLをこれだけに絞る（カンマ区切り。空なら無効）。指定すると、ここに
# 合致しない URL はすべてスキップする（ホワイトリスト）。skip_urls も併用でき、
# 合致しても skip_urls に当たるものは撮らない。判定は skip_urls と同じ前方一致/ワイルドカード。
# 例: https://example.com/, https://*.example.com/*
allow_urls =

# 一部抜き出しの CSS セレクタ（空なら一部抜きはスキップ）
# 例: h1  /  article  /  .price  /  #main .title
target_selector =

# 撮影中だけ隠す要素の CSS セレクタ（カンマ区切り。空なら何も隠さない）
# 同意バナーや追従ヘッダなどが証跡（スクショ）に被るのを防ぐ。撮影の瞬間だけ
# visibility:hidden で隠し、撮影後に元へ戻す（ページの操作や記録には影響しない）。
# 例: #cookie-banner, .sticky-header
hide_selectors =

# 起動直後に記録を開始するか（false=待機状態で起動し、パネルの「記録開始」で撮り始める）
# true にすると起動時から記録ON（従来どおり URL/タブ変化で自動保存）になる
start_recording = false

# 再利用するブラウザプロファイルの場所（空なら毎回まっさらな使い捨て＝従来どおり）。
# パスを指定すると、そのフォルダにログイン状態などを保存して次回も引き継ぐ。
# 相対パスは exe（またはこのスクリプト）と同じ場所を基準にする。
# 例: profile_dir = profile
# 注意: 指定フォルダには Cookie や認証情報がディスク保存される。取り扱いに注意。
profile_dir =
"""

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
    settle_delay: float = 0.8               # 変化検知後、描画が落ち着くまで待つ秒数
    load_timeout: int = 5000                # ページ読み込み待ちの上限（ミリ秒）
    eval_timeout: int = 5000                # ページ側 JS 実行（本文取得・撮影合図）の上限（ミリ秒。E-6）
    skip_urls: tuple[str, ...] = ("about:blank", "")   # 撮らないURL（前方一致/fnmatch。B-5）
    allow_urls: tuple[str, ...] = ()        # 指定時はこれに合致する URL だけ撮る（他は全スキップ。F-C2）
    target_selector: str = ""               # 一部抜き出しの CSS セレクタ（空ならスキップ）
    hide_selectors: tuple[str, ...] = ()    # 撮影中だけ隠す CSS セレクタ（空なら何も隠さない。F-B2）
    start_recording: bool = False           # 起動直後に記録を開始するか（False=待機状態で起動）
    profile_dir: str = ""                    # 再利用するブラウザプロファイルの場所（空なら毎回使い捨て）

    @property
    def eval_timeout_sec(self) -> float:
        """eval_timeout を秒で返す（asyncio.wait_for / try_eval に渡す単位）。

        config.ini の単位はミリ秒（利用者に分かりやすい整数で書ける）、コード内の利用は秒
        （asyncio の単位）。この変換点をここ 1 か所に定める（#56）。以前は capture.py の
        _capture と _save_text が `config.eval_timeout / 1000` を別々に計算していて、
        片方だけ直る形の事故を招く並びだった。
        """
        return self.eval_timeout / 1000


def session_stamp() -> str:
    """起動 1 回分（セッション）の保存先サブフォルダ名を返す（F-C3）。

    起動時刻を「YYYY-MM-DD_HHMMSS」で表す（例: 2026-08-11_143025）。output_dir の直下に
    この 1 段を挟むことで、撮影物・log.txt・index.csv・lineage-<id>/downloads がすべて
    「起動ごとのフォルダ」にまとまり、受け渡しが『このフォルダを渡す』で済む。
    秒までの粒度（同一秒に二重起動するとまれに同じフォルダを共有するが、mkdir の
    exist_ok と同じ許容範囲で、起動単位に切るという狙いは損なわない）。
    """
    return f"{datetime.now():%Y-%m-%d_%H%M%S}"


def _url_matches(url: str, pattern: str) -> bool:
    """URL が 1 つのパターンに合致するか（B-5）。

    ワイルドカード（`*` `?` `[`）を含むパターンは fnmatch で判定し、
    含まないパターンは前方一致で判定する（`https://skip.me` が
    `https://skip.me?ref=1` のようなクエリ付き URL にも効くように）。
    空パターン("")だけは特例で「空URL専用」にする（前方一致だと全URLに化けるため厳密一致に限定）。
    """
    if pattern == "":
        return url == ""
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(url, pattern)
    return url == pattern or url.startswith(pattern)


def should_capture(url: str, config: Config) -> bool:
    """この URL を撮るべきか（skip_urls / allow_urls の判定を 1 か所に集約。R3）。

    - allow_urls が指定されていれば、それに合致しない URL はすべてスキップ（ホワイトリスト。F-C2）。
    - 次に skip_urls に合致すれば（allow を通っていても）スキップ（ブラックリスト）。
    どちらのマッチも前方一致 or fnmatch（B-5）。url 取得に失敗した側（None）は
    呼び出し側で弾く前提で、ここは確定した文字列 url を受ける。
    """
    if config.allow_urls and not any(_url_matches(url, p) for p in config.allow_urls):
        return False
    if any(_url_matches(url, p) for p in config.skip_urls):
        return False
    return True


def summarize_config(config: Config) -> str:
    """採用された設定値の要点を1行に整形する（D-B2）。

    config.ini の「実際に効いた値」をログへ残す。既定へフォールバックしたのか、
    自動検出（空）なのか明示指定なのかが後から分かり、不具合の切り分けが速くなる
    （channel="msedge" 等が環境依存なので、採用値の記録が効く）。パス・セレクタは
    有無だけでなく実値も出す（保存先が想定と違う、といった事故に気づけるように）。
    """
    browser = config.browser or "自動(Edge→Chrome)"
    selector = config.target_selector or "(無)"
    hides = ",".join(config.hide_selectors) or "(無)"
    skips = ",".join(u for u in config.skip_urls if u) or "(無)"
    allows = ",".join(config.allow_urls) or "(無)"
    return (
        "[config] "
        f"browser={browser} "
        f"edge_path={config.edge_path or '自動'} "
        f"chrome_path={config.chrome_path or '自動'} "
        f"output_dir={config.output_dir} "
        f"start_url={config.start_url} "
        f"settle_delay={config.settle_delay} "
        f"load_timeout={config.load_timeout} "
        f"eval_timeout={config.eval_timeout} "
        f"start_recording={config.start_recording} "
        f"target_selector={selector} "
        f"hide_selectors={hides} "
        f"skip_urls={skips} "
        f"allow_urls={allows} "
        f"profile_dir={config.profile_dir or '使い捨て'}"
    )


def _write_default_config() -> bool:
    """既定の config.ini を CONFIG_PATH へ書き出す（自己修復。D-C3）。

    書けたら True。読み取り専用の場所などで書けなくても例外は投げず False を返し、
    呼び出し側がメモリ上の既定値で起動できるようにする（起動不能にしない）。
    """
    try:
        CONFIG_PATH.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        return True
    except Exception as e:
        log(f"[config] 既定 config.ini の書き出しに失敗しました: {e}")
        return False


def _csv_tuple(sec: configparser.SectionProxy, key: str) -> tuple[str, ...]:
    """カンマ区切りの設定値をタプル化する（各要素を strip し、空要素は捨てる）。

    skip_urls / allow_urls / hide_selectors は書式が同じなので、パースをここへ寄せる
    （同じ内包表記の 3 連コピーを避け、書式を変えるときの直し漏れを無くす）。
    項目行が無ければ空タプル。
    """
    return tuple(v.strip() for v in sec.get(key, "").split(",") if v.strip())


def _under_base_dir(raw: str) -> Path:
    """設定値のパスを絶対パスにする。相対パスは基準フォルダ（BASE_DIR）基準に固定する。

    exe の隣を基準にすることで、どこから起動しても保存先・プロファイルの場所がぶれない。
    絶対パス指定はそのまま使う（config.ini で任意の場所を指定できる）。output_dir と
    profile_dir が同じ規則なので共通化する。
    """
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _build_config(sec: configparser.SectionProxy, defaults: Config) -> Config:
    """[capture] セクションから Config を組み立てる（値の検証・既定フォールバック込み）。

    値の変換・検証だけを行い、ファイルアクセスやログ切り替え等の副作用は持たない
    （R5b: 副作用の分離）。数値項目の変換・範囲チェックに失敗すると ValueError を送出する
    （呼び出し側が「値の編集ミス」として通知し終了する）。output_dir は config.ini で指定された
    保存先（相対パスは基準フォルダ基準に固定）をそのまま載せる。書き込み可否の解決・
    セッション階層の挿入・set_log_dir といった副作用は _resolve_output_dir が担い、
    load_config などが本関数の直後に呼ぶ。
    """
    # 保存先。値が空ならカレントへ落ちないよう既定へ戻す（配布先で編集ミスが起きても安全側に）。
    raw_out = sec.get("output_dir", str(defaults.output_dir)).strip()
    if not raw_out:
        log("[config] output_dir が空のため既定値を使います。")
        raw_out = str(defaults.output_dir)
    # 相対パスは基準フォルダ基準に固定（exe 隣の output\ に確実に保存する）。
    # 絶対パス指定時はそのまま使う（config.ini で任意の保存先に変更可能）。
    output_dir = _under_base_dir(raw_out)

    # 数値項目。範囲を検証し、不正なら理由付き ValueError（呼び出し側で通知＆終了）。
    settle_delay = sec.getfloat("settle_delay", defaults.settle_delay)
    load_timeout = sec.getint("load_timeout", defaults.load_timeout)
    eval_timeout = sec.getint("eval_timeout", defaults.eval_timeout)
    if settle_delay < 0:
        raise ValueError(f"settle_delay は 0 以上にしてください（現在: {settle_delay}）")
    if load_timeout <= 0:
        raise ValueError(f"load_timeout は正の整数にしてください（現在: {load_timeout}）")
    if eval_timeout <= 0:
        raise ValueError(f"eval_timeout は正の整数にしてください（現在: {eval_timeout}）")

    # 撮らない URL。空URLは常にスキップ対象へ含める（下の Config で ("",) を足す）。
    skips = _csv_tuple(sec, "skip_urls")

    # 撮る URL を明示的に絞るホワイトリスト（指定時は他を全スキップ。F-C2）。
    # 空なら無効（従来どおり skip_urls だけで判定）。
    allows = _csv_tuple(sec, "allow_urls")

    # 撮影中だけ隠すセレクタ（F-B2）。
    hides = _csv_tuple(sec, "hide_selectors")

    # 再利用プロファイルの場所。空なら毎回使い捨て（既定の挙動を据え置く）。
    # 指定時のみ、相対パスは基準フォルダ基準に固定して絶対パス文字列で保持する
    # （output_dir と同じ扱い。書き込み可能な exe 隣を既定基準にできる）。
    raw_profile = sec.get("profile_dir", "").strip()
    profile_dir = str(_under_base_dir(raw_profile)) if raw_profile else ""

    return Config(
        start_url=sec.get("start_url", defaults.start_url).strip() or "about:blank",
        browser=_normalize_browser(sec.get("browser", defaults.browser)),
        edge_path=sec.get("edge_path", "").strip(),
        chrome_path=sec.get("chrome_path", "").strip(),
        # 指定された保存先そのまま（書き込み解決前）。_resolve_output_dir が
        # 書き込み可否の解決とセッション階層の挿入を行って確定させる。
        output_dir=output_dir,
        settle_delay=settle_delay,
        load_timeout=load_timeout,
        eval_timeout=eval_timeout,
        skip_urls=skips + ("",),
        allow_urls=allows,
        target_selector=sec.get("target_selector", "").strip(),
        hide_selectors=hides,
        start_recording=sec.getboolean("start_recording", defaults.start_recording),
        profile_dir=profile_dir,
    )


def _resolve_output_dir(config: Config) -> Config:
    """保存先の書き込み可否を解決し、セッション階層を挟んで output_dir を確定させる。

    _build_config（純粋な値組み立て）から分離した副作用側（R5b）。組み立て済みの Config を
    受け取り、以下を行って output_dir 差し替え済みの Config を返す:
      - 書き込み可能なフォルダへの解決と %LOCALAPPDATA% 等への退避（D-C1）。
        どこにも書けなければ ConfigFatalError（通知と終了は入口が決める。#49）。
      - 起動 1 回分のセッションフォルダの挿入（F-C3）。
      - ログ出力先をその保存先へ切り替え（set_log_dir）。
      - 退避が起きた場合の通知。
    """
    output_dir = config.output_dir

    # 書き込み可能なフォルダへ解決する（D-C1）。権限の無い場所へ展開されても
    # 無言終了せず、%LOCALAPPDATA% 等へ退避して動き続ける。どこにも書けなければ終了。
    # （終了そのものは入口の仕事なので、ここでは ConfigFatalError を投げるだけにする）
    resolved = resolve_writable_dir(output_dir)
    if resolved is None:
        # 通知と終了は入口（cli）へ委ねる。文面はそのまま持たせて利用者から見た挙動を変えない。
        raise ConfigFatalError(
            f"保存先フォルダに書き込めませんでした: {output_dir}\n"
            "書き込み可能な場所（例: ドキュメント配下）へ移して実行してください。"
        )

    # F-C3: 起動 1 回分のセッションフォルダを 1 段挟む（例: output/2026-08-11_143025/）。
    # 撮影物・log.txt・index.csv・lineage-<id>/downloads はすべて output_dir からの相対で
    # 決まるので、ここで output_dir をセッションフォルダにすげ替えるだけで全保存物がその下へ
    # まとまる（起動単位で切り、受け渡しが「このフォルダを渡す」で済む）。書き込み可否の判定と
    # 退避は、セッション階層を挟む前の resolved（基準フォルダ）で済ませておく。プローブ対象を
    # 実在フォルダに保ち、退避先の名前が起動時刻に化けないようにするため。
    session_dir = resolved / session_stamp()

    # ログも PNG などと同じ保存先（セッションフォルダ）へ寄せる（保存先が確定したこの時点で
    # 切り替え）。先に set_log_dir しておくと、退避の通知が確実に書ける退避先ログへ残る。
    # set_log_dir がセッションフォルダを mkdir するので、直後の退避通知ログも取りこぼさない。
    set_log_dir(session_dir)

    # 退避が起きたら、どこへ保存されるのかをログとダイアログの両方で知らせる
    # （保存先が分からないほうが利用者は困るため）。判定・表示は基準フォルダ（resolved）で行い、
    # 利用者が config.ini で直す対象＝起動時刻サブ階層を含めない元のパスを示す。
    if resolved != output_dir:
        notify_fatal(
            f"保存先 {output_dir} に書き込めないため、{resolved} へ退避して実行します。\n"
            "権限のある場所（例: ドキュメント配下）へ移すと元の設定で保存できます。"
        )

    return replace(config, output_dir=session_dir)


def _config_from_section(sec: configparser.SectionProxy, defaults: Config) -> Config:
    """[capture] セクションから、保存先まで確定した Config を作る。

    値の組み立て（_build_config・純粋）と保存先の解決（_resolve_output_dir・副作用）は
    R5b で意図的に分けてあるが、使うときは必ずこの順で 2 段を揃える。以前は 3 経路
    （通常読み込み・破損からの復旧・既定テキストからの起動）がそれぞれ同じ 2 段重ねを
    書いていたため、間に処理が増えたときの直し漏れが 3 箇所ぶん起こりえた。組み合わせを
    ここへ 1 本化して、直す場所を 1 つにする。
    """
    return _resolve_output_dir(_build_config(sec, defaults))


def _config_with_defaults(defaults: Config) -> Config:
    """既定 config.ini（DEFAULT_CONFIG_TEXT）の中身から Config を作る。

    ファイルをどうしても書けない/読めないときでも、配布時と同じ既定値で起動するための
    最後の砦。空セクションではなく DEFAULT_CONFIG_TEXT を使うので、skip_urls などの
    「既定ファイルにある値」も取りこぼさない。output_dir 解決・set_log_dir も走る。
    """
    parser = configparser.ConfigParser()
    parser.read_string(DEFAULT_CONFIG_TEXT)
    return _config_from_section(parser["capture"], defaults)


def _recover_broken_config(error: Exception, defaults: Config) -> Config:
    """破損／[capture] 欠落の config.ini から回復して起動する（自己修復。D-C3）。

    壊れた元ファイルは消さず config.ini.invalid へ退避し（利用者が中身を確認できる）、
    既定 config.ini を書き直して読み直す。書けない/読めない場合は DEFAULT_CONFIG_TEXT の
    既定値で起動する。無言死ではなくダイアログで「壊れていた・作り直した」ことを伝える。
    """
    moved_to = None
    try:
        backup = CONFIG_PATH.with_name(CONFIG_PATH.name + ".invalid")
        if CONFIG_PATH.exists():
            CONFIG_PATH.replace(backup)  # 上書き移動（前回の .invalid があっても置き換える）
            moved_to = backup
    except Exception:
        moved_to = None

    wrote = _write_default_config()
    notify_fatal(
        "config.ini を読み込めませんでした（破損または [capture] セクション無し）。\n"
        + (f"元のファイルは {moved_to.name} へ退避しました。\n" if moved_to else "")
        + ("既定値で config.ini を作り直しました。\n" if wrote else "既定値で起動します。\n")
        + f"詳細: {error}"
    )

    # 作り直した既定ファイルを読む（書けていれば）。それも読めなければ、メモリ上の既定値で起動。
    if wrote:
        parser = configparser.ConfigParser()
        try:
            parser.read(CONFIG_PATH, encoding="utf-8-sig")
            return _config_from_section(parser["capture"], defaults)
        except (configparser.Error, KeyError):
            pass
    return _config_with_defaults(defaults)


def load_config() -> Config:
    """config.ini を読み込み、Config を返す。

    既定値まわりの挙動（現状仕様）:
      - config.ini が無い                          → 既定 config.ini を書き出し、既定値で
        起動する（D-C3 自己修復。書けなければメモリ上の既定値で起動）。
      - config.ini が破損／[capture] セクションが無い → 元ファイルを config.ini.invalid へ
        退避し、既定 config.ini を作り直して起動する（D-C3。ダイアログで通知）。
      - 項目の「行そのものが無い」                 → Config の既定値を使う
        （sec.get / getfloat / getint の第2引数が既定値）。
      - 数値項目の値だけが不正（例: settle_delay =／範囲外）→ ファイル自体は使えるので
        勝手に上書きせず、直し方を伝えて終了する（利用者の編集内容を失わせない）。
        「伝えて終了」の実体は ConfigFatalError の送出で、通知（notify_fatal）と終了コードは
        入口（edge_auto_capture.cli）が決める（#49）。保存先がどこにも書けないときも同じ。
      - output_dir の値が空 → 既定値（output）へフォールバックする
        （空だと Path('.') でカレントへ保存してしまう事故を防ぐ）。
      - 確定した output_dir の直下へ、起動時刻のセッションフォルダを 1 段挟む（F-C3。
        例: output/2026-08-11_143025/）。以後 log.txt・index.csv・撮影物・
        lineage-<id>/downloads はすべてこのフォルダ配下へまとまる。
      - edge_path / chrome_path が空 → 自動検出（空が正常値）。値があれば
        起動する各ブラウザの実行ファイルとしてそのパスを使う。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
      - profile_dir が空 → 毎回まっさらな使い捨てプロファイル（既定・従来どおり）。
        値があれば、そのフォルダを再利用（相対パスは基準フォルダ基準に固定）。
      - browser が空 → 自動選択（Edge→Chrome）。edge/chrome 以外の値 → 終了（ValueError）。
    """
    defaults = Config()

    # D-C3: 失われても起動不能にしない。既定 config.ini を書き出して既定値で起動する。
    if not CONFIG_PATH.exists():
        if _write_default_config():
            log(f"[config] config.ini が無いため既定値で作成しました: {CONFIG_PATH}")
            # 書けた → 下の通常読み込みで、今書いた既定ファイルを読む。
        else:
            log("[config] config.ini が無く作成もできないため、既定値で起動します。")
            return _config_with_defaults(defaults)

    parser = configparser.ConfigParser()
    try:
        # utf-8-sig: BOM 有無どちらでも読む（A-6）。USAGE.txt が「メモ帳で編集」と
        # 案内しており、メモ帳保存で BOM が混入すると utf-8 では最初のセクション見出しが
        # 壊れ MissingSectionHeaderError になる。BOM を吸収して原因不明の起動失敗を防ぐ。
        parser.read(CONFIG_PATH, encoding="utf-8-sig")
        sec = parser["capture"]
    except (configparser.Error, KeyError) as e:
        # 破損 or [capture] 欠落＝ファイルとして使えない → 退避して作り直す（D-C3）。
        return _recover_broken_config(e, defaults)

    try:
        return _config_from_section(sec, defaults)
    except ValueError as e:
        # 値だけが不正（利用者の編集ミス）。ファイル構造は正しく他の設定は生きているので、
        # 勝手に既定へ戻さず、直し方を伝えて終了する（編集内容を失わせない）。
        # 通知と終了は入口（cli）の仕事なので、文面を載せた ConfigFatalError に変換して返す。
        raise ConfigFatalError(
            f"config.ini の読み込みに失敗しました: {e}\n"
            "[capture] セクションと各項目の値を確認してください。"
        ) from e
