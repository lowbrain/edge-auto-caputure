"""操作バー（各ページ上部のパネル）のページ側 JS を組み立てるモジュール。

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

# 各ページ上部に出す操作パネルの識別子。
# パネル全体をこの id のコンテナに閉じ込める（撮影時の写り込み除外がこの id 前提）。
BADGE_ID = "__eac_rec_badge__"

# --- 表示文言（利用者に見える日本語）。ここを直せば UI 文言が変わる。 ---
_STATUS_ON = "記録中"
_STATUS_OFF = "待機中"
_LABEL_START = "記録開始"
_LABEL_STOP = "記録停止"
_LABEL_SHOT = "📸 今すぐ1枚"
# SPA（URLが変わらず中身だけ変わるページ）向けトグルスイッチの横に出すラベル。
_LABEL_SPA = "SPA検知"
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
# SPA検知トグルのホバー説明。無効（セレクタ未設定）でも読めるよう、ラベル/ラッパにも付ける。
_TITLE_SPA = (
    "SPA（URL が変わらず中身だけ変わるページ）向けの自動保存を ON/OFF します。\n"
    "左の CSS セレクタを設定すると操作できます（未設定のときは押せません）。\n"
    "ON の間は、記録中にその要素の中身が変わるたびに自動保存します。"
)

# badge.js の $CONFIG へ渡す設定（表示文言）。キー名は badge.js 内の C.* と対応する。
_BADGE_CONFIG = {
    "id": BADGE_ID,
    "sOn": _STATUS_ON,
    "sOff": _STATUS_OFF,
    "lStart": _LABEL_START,
    "lStop": _LABEL_STOP,
    "lShot": _LABEL_SHOT,
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


def _load_badge_script() -> str:
    """badge.js を読み込み、$CONFIG を設定 JSON で置換した完成スクリプトを返す。

    JS 中に他の `$` は使わない前提なので単純な文字列置換で足りる（絵文字/日本語も
    json.dumps で \\uXXXX に安全化される）。
    """
    src = _badge_js_path().read_text(encoding="utf-8")
    return src.replace("$CONFIG", json.dumps(_BADGE_CONFIG))


# 各ページへ注入する完成済みスクリプト（add_init_script に渡す）。
BADGE_SCRIPT = _load_badge_script()

# --- capture 側が page.evaluate で呼ぶ、ページ側ヘルパの呼び出し式 ---
# いずれも window.__eac_* が未定義でも落ちないよう、存在チェック付きの式にしてある。

# スクリーンショット/抽出の瞬間だけバーを隠す/戻す。
BAR_HIDE = "window.__eac_barDisplay && window.__eac_barDisplay(false)"
BAR_SHOW = "window.__eac_barDisplay && window.__eac_barDisplay(true)"
# 本文テキスト（バー除外）。未注入時は素の innerText にフォールバック。
BODY_TEXT_CALL = (
    "window.__eac_bodyText ? window.__eac_bodyText() "
    ": (document.body ? document.body.innerText : '')"
)
# SPA検知の署名。引数 sel を受け取る関数式（page.evaluate(SIG_CALL, selector) で使う）。
SIG_CALL = "(sel) => window.__eac_signature ? window.__eac_signature(sel) : '0_0'"

# 保存完了の合図（パネルを一瞬フラッシュ）。撮影後に page.evaluate で呼ぶ。
FLASH_CALL = "window.__eacFlash && window.__eacFlash()"
