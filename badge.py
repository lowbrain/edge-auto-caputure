"""操作バー（各ページ上部に表示）のページ側 JS を組み立てるモジュール。

- ページ側 JS の本体は隣の badge.js（実ファイル）に置く。実ファイルなので
  エディタ/リンタで構文検査でき、以前の「Python 文字列内 JS の構文エラーが
  実行するまで分からない」問題を避けられる。
- 表示文言（利用者に見える日本語など）は Python 側で定義し、_BADGE_CONFIG に
  まとめて 1 個の JSON（badge.js 中の $CONFIG）として渡す。追加時に置換の
  引数を並べ直す必要がなく、直し漏れが起きにくい。
- capture 側が page.evaluate で呼ぶヘルパ（バー隠し/本文取得/署名/保存フラッシュ）の
  呼び出し式もここに集約する。
"""

import json
import sys
from pathlib import Path

# 操作バーの識別子。バー全体をこの id のコンテナに閉じ込める（撮影時の写り込み除外が
# この id 前提）。
BADGE_ID = "__eac_rec_badge__"

# --- 表示文言（利用者に見える日本語）。ここを直せば UI 文言が変わる。 ---
_STATUS_ON = "記録中"
_STATUS_OFF = "待機中"
_LABEL_START = "記録開始"
_LABEL_STOP = "記録停止"
_LABEL_SHOT = "📸 今すぐ1枚"
# 保存先フォルダ（起動単位のセッションフォルダ）を OS のファイルマネージャで開くボタン（F-D4）。
# 撮影物・ダウンロード・log.txt が集まった場所をその場で開けるようにする受け渡しの導線。
_LABEL_OPEN = "📂 保存先"
_TITLE_OPEN = (
    "撮影物・ダウンロード・log.txt の保存先フォルダ（今回の起動ぶん）を開きます。\n"
    "そのまま「このフォルダを渡す」で受け渡しが済みます。"
)
# 撮影カウンタ（本セッションで保存できた枚数）の表示文言（F-D3）。{n} は枚数に置換される。
# 動作している実感と、暴走（意図しない連写）の早期発見のためにバーへ常時出す。
_LABEL_SHOTS = "本セッション {n} 枚"
# バーを半透明にして下に隠れた内容を確認するためのトグル（枠なしのアイコンボタン）。
# 文言ラベルは持たず、ホバー時の説明（title）だけ持つ。状態はアイコンの装飾で表す。
_TITLE_PEEK = (
    "操作パネルを半透明にして、下に隠れているページ内容を確認できるようにします。\n"
    "もう一度押すと元の表示に戻ります（記録状態や撮影には影響しません）。"
)
# SPA（URLが変わらず中身だけ変わるページ）向けトグルスイッチの横に出すラベル。
_LABEL_SPA = "SPA検知"
# E-1（a11y）: 支援技術（スクリーンリーダー）向けのアクセシブル名。
# 透過ボタンはアイコンのみで可視テキストが無く、名前が読み上げられないため aria-label を付ける。
# （可視テキストを持つボタン＝記録開始/停止・今すぐ1枚・保存先には付けない。二重読み上げになるため）
_ARIA_PEEK = "透過表示の切り替え"
# セレクタ入力欄。常時ラベルは置かず、プレースホルダ（透かし文字）で入力を促し、
# ホバー時の title で「Edge の開発者ツールでの調べ方」を手順で説明する（\n で改行表示）。
_PLACEHOLDER_SEL = "CSSセレクタを入力"
_TITLE_SEL = (
    "ページの一部だけをテキストとして抜き出す（_part.txt）対象の CSS セレクタを入力します。\n"
    "SPA検知が ON のときは、この要素の中身の変化を監視して自動保存します。\n"
    "\n"
    "調べ方（Edge の開発者ツール）:\n"
    "1. 監視したい部分を右クリック →「検証」（または F12）で開発者ツールを開く\n"
    "2. Elements パネルで、青く選択されている要素の id / class を確認する\n"
    "3. id があれば「#その値」、class があれば「.その値」をここに入力する\n"
    "   例) <div id=\"main\"> → #main ／ <ul class=\"list\"> → .list\n"
    "\n"
    "※右クリック → Copy → Copy selector でも取得できますが、長く壊れやすいので、\n"
    "  できるだけ短い #id や .class を指定するのがおすすめです。"
)
# SPA検知トグルのホバー説明。セレクタ未設定でも既定ルートを監視するので、常に操作できる。
_TITLE_SPA = (
    "SPA（URL が変わらず中身だけ変わるページ）向けの自動保存を ON/OFF します。\n"
    "左に CSS セレクタを入れると、その要素の中身の変化を監視します。\n"
    "未入力のときはページの主要部（main / article、無ければ本文全体）を自動で監視します。\n"
    "ON の間は、記録中にその中身が変わるたびに自動保存します。"
)

# badge.js の $CONFIG へ渡す設定（表示文言）。キー名は badge.js 内の C.* と対応する。
_BADGE_CONFIG = {
    "id": BADGE_ID,
    "sOn": _STATUS_ON,
    "sOff": _STATUS_OFF,
    "lStart": _LABEL_START,
    "lStop": _LABEL_STOP,
    "lShot": _LABEL_SHOT,
    "lOpen": _LABEL_OPEN,
    "titleOpen": _TITLE_OPEN,
    "lShots": _LABEL_SHOTS,
    "titlePeek": _TITLE_PEEK,
    "ariaPeek": _ARIA_PEEK,
    "lSpa": _LABEL_SPA,
    "phSel": _PLACEHOLDER_SEL,
    "titleSel": _TITLE_SEL,
    "titleSpa": _TITLE_SPA,
}


def _badge_js_path() -> Path:
    """badge.js の場所を返す。

    PyInstaller で凍結（frozen）した場合は同梱データの展開先（sys._MEIPASS）、
    通常実行時はこのモジュールと同じフォルダを見る。build.ps1 が
    --add-data で badge.js を _MEIPASS 直下へ同梱する前提。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return base / "badge.js"


def build_badge_script(
    token: str = "", settle_ms: int = 300, hide_selectors: tuple[str, ...] = ()
) -> str:
    """badge.js を読み込み、$CONFIG を設定 JSON で置換した完成スクリプトを返す。

    token は、ページ側から expose_binding（__eac_* 群）を呼ぶときの「合言葉」。
    badge.js はこの token を各呼び出しの第1引数に付け、Python 側が照合する。閲覧中の
    サイトのスクリプトが勝手に記録操作・連写・セレクタ書き換えを行えないようにするため
    （token を知らない呼び出しは Python 側で無視される）。空文字（既定）は照合しない
    用途向け（スモークテストなど、バインディング自体を公開しない場面）。

    settle_ms は SPA検知のデバウンス時間（ミリ秒）。ページ側の MutationObserver が捉えた
    中身変化が「この時間だけ止まったら落ち着いた」とみなして署名を確定する。Config の
    settle_delay（秒）をミリ秒へ直して渡す（config.ini で調整可能）。

    hide_selectors は撮影中だけ隠す要素の CSS セレクタ群（F-B2）。captureStart で該当要素を
    visibility:hidden にして撮影後に戻す。同意バナー・追従ヘッダなどが証跡（スクショ）に
    被るのを防ぐ。空なら何も隠さない（既定）。

    置換対象は文字列 "$CONFIG" のみ。badge.js はテンプレートリテラル（バッククォート）を
    使うが、補間は `${...}` の形だけで、この JS では `${` を使わないため `$CONFIG` と
    衝突しない。よって単純な文字列置換で足りる（絵文字/日本語も json.dumps で
    \\uXXXX に安全化される）。
    """
    config = dict(_BADGE_CONFIG, tok=token, settleMs=settle_ms, hideSel=list(hide_selectors))
    src = _badge_js_path().read_text(encoding="utf-8")
    return src.replace("$CONFIG", json.dumps(config))


# 完成済みスクリプト（token 無し）は、以前ここで BADGE_SCRIPT = build_badge_script() として
# モジュール読み込み時に作っていたが、import しただけで badge.js の read_text（I/O）が走り、
# 凍結（PyInstaller）環境などで失敗経路を 1 つ抱えていた（R5a）。実運用では token 付きの
# build_badge_script(token) を都度呼ぶだけで、この完成済みスクリプトは使わない。スモークテスト
# など「バインディングを公開せず見た目だけ確認する」用途は、build_badge_script() を必要時に
# 呼ぶ（＝遅延化）。これで import 時 I/O を無くした。

# --- expose_binding で公開するバインディング名（ページ側 → Python の呼び出し口）---
# badge.js 内の window.__eac_* 呼び出し名と 1:1 で一致させること。言語境界をまたぐため
# 完全な一元化はできないが、Python 側の名前をここへ集約して「唯一の一覧」を持つ
# （綴りずれは JS 側 try/catch で無言失敗するので、実発火はスモークテストで確認している）。
BIND_TOGGLE = "__eac_toggle"                  # 記録開始/停止
BIND_SHOT = "__eac_shot"                       # 今すぐ1枚
BIND_OPEN_FOLDER = "__eac_open_folder"         # 保存先フォルダを開く（F-D4）
BIND_SPA_TOGGLE = "__eac_spa_toggle"           # SPA検知 ON/OFF
BIND_SET_SELECTOR = "__eac_set_selector"       # セレクタ入力（変更のたび）
BIND_COMMIT_SELECTOR = "__eac_commit_selector" # セレクタ確定（blur/Enter）
BIND_SPA_CHANGED = "__eac_spa_changed"         # SPA検知の変化通知
BIND_GETSTATE = "__eac_getstate"               # 描画前の状態問い合わせ

# --- capture 側が page.evaluate で呼ぶ、ページ側ヘルパの呼び出し式 ---
# いずれも window.__eac_* が未定義でも落ちないよう、存在チェック付きの式にしてある。

# 本文テキスト（バー除外）。未注入時は素の innerText にフォールバック。
BODY_TEXT_CALL = (
    "window.__eac_bodyText ? window.__eac_bodyText() "
    ": (document.body ? document.body.innerText : '')"
)
# SPA検知の署名。引数 sel を受け取る関数式（page.evaluate(SIG_CALL, selector) で使う）。
SIG_CALL = "(sel) => window.__eac_signature ? window.__eac_signature(sel) : '0_0'"

# 撮影の合図。撮影直前に captureStart（バーを退避し切るまで待つ・Promise を返す）、
# 撮影直後に captureEnd（全画面のシャッターフラッシュ＋バー復帰）を page.evaluate で呼ぶ。
# captureStart は未注入でも await できるよう、関数式で null を返す形にしておく。
CAPTURE_START_CALL = "(() => window.__eac_captureStart ? window.__eac_captureStart() : null)()"
# 引数なしの captureEnd は「成功（赤フラッシュ）」扱い。実運用は capture_end_call(ok) で
# 成否を渡す（スモークテストはこの無引数版を使う）。
CAPTURE_END_CALL = "window.__eac_captureEnd && window.__eac_captureEnd()"


def capture_end_call(ok: bool) -> str:
    """撮影直後の captureEnd 呼び出し式を組み立てる（F-D3）。

    ok は `_capture` の done 有無（1 種でも保存できたか）。ページ側の captureEnd へ真偽値で
    渡し、成功（赤）と失敗（琥珀）でシャッターフラッシュの色を分ける。現状 captureEnd は成否を
    受け取らなかったため、この引数付き呼び出し式を capture 側から使う経路を用意する。
    """
    flag = "true" if ok else "false"
    return f"window.__eac_captureEnd && window.__eac_captureEnd({flag})"


def set_count_call(count: int) -> str:
    """撮影カウンタ（本セッション枚数）をバーへ反映する呼び出し式を組み立てる（F-D3）。

    枚数は Python 側（監視セッション）が本体として持ち、成功のたびに全ページのバーへ配る。
    バーがサイト側の再描画で作り直されても __eac_getstate（count 同梱）で自己同期する。
    """
    return f"window.__eacSetCount && window.__eacSetCount({int(count)})"


def set_history_call(history: list[str]) -> str:
    """セレクタ候補（datalist の過去値）をバーへ反映する呼び出し式を組み立てる（F-D2）。

    候補は Python 側（監視セッション）が本体として持ち、セレクタ確定（blur/Enter）のたびに
    全ページのバーへ配る。バーがサイト側の再描画で作り直されても __eac_getstate（history
    同梱）で自己同期する。日本語/記号を含む値も json.dumps で安全に JS 配列リテラル化する。
    """
    return f"window.__eacSetHistory && window.__eacSetHistory({json.dumps(history)})"
