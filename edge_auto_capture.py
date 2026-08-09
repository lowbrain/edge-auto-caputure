"""
記録ONの間、Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

キャプチャのタイミングは利用者が操作する。各ページ上部の操作パネルで
「記録開始／停止」で記録期間を制御でき、「今すぐ1枚」で今のページを1回だけ撮れる。
既定は記録OFF（待機）で起動する（config.ini の start_recording で変更可）。

このスクリプトが Edge の起動・監視・後始末までを一括で行う（Playwright が
毎回まっさらな一時プロファイルで Edge を起動し、終了時に自動で掃除する）。

事前準備:
  pip install -e .          （または pip install playwright）
  ※ システムにインストール済みの Edge をそのまま使うため、
    playwright install（ブラウザ同梱バイナリの取得）は不要。

起動方法:
  - python edge_auto_capture.py（開発時）、または
  - ビルドした edge-auto-capture.exe をダブルクリック（配布時）
  最初に開くページ・保存先などは同じフォルダの config.ini で指定する
  （起動ページは start_url。空なら about:blank）。開いた Edge で普通に
  閲覧し、記録ONの間だけ URL/タブの変化ごとに output\\ へ自動保存される。

設定はソースではなく、同じフォルダの config.ini を編集して変更する。
停止は「Edge のウィンドウを閉じる」だけでよい（コンソール実行時は Ctrl + C
も使える）。停止すると、このスクリプトが起動した Edge の終了と一時プロファイル
の削除まで行う。動作ログは exe/スクリプトと同じフォルダの log.txt に残る。
各ページの上部には操作パネル（記録中/待機中の表示＋「記録開始/停止」＋「今すぐ1枚」）
を表示する（保存するスクリーンショットにも、抽出する txt / part テキストにも含めない）。
"""

import asyncio
import configparser
import itertools
import json
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Tuple

from playwright.async_api import Page, async_playwright

def _base_dir() -> Path:
    """設定・保存先の基準フォルダを返す。

    PyInstaller で exe 化した場合（sys.frozen）は exe のあるフォルダ、
    通常の Python 実行時はこのスクリプトのあるフォルダを基準にする。
    これにより、配布した exe の隣に置いた config.ini を読み、
    output\\ も exe の隣に作れる（＝第三者が config.ini を編集できる）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# 設定・出力の基準フォルダ（通常実行なら本ファイル、exe 実行なら exe と同じ場所）。
BASE_DIR = _base_dir()

# 設定ファイルのパス（基準フォルダ固定）。
CONFIG_PATH = BASE_DIR / "config.ini"

# safe_name() がファイル名スラッグを切り詰める最大長。
NAME_MAX_LEN = 80

# 実行ログの出力先（exe/スクリプトと同じフォルダ）。
# コンソール無し（windowed exe）で実行しても後から動作を追えるようにする。
LOG_PATH = BASE_DIR / "log.txt"

# 各ページ上部に出す操作パネルの識別子と表示文言。
# パネル全体をこの id のコンテナに閉じ込める（撮影時の写り込み除外がこの id 前提）。
_BADGE_ID = "__eac_rec_badge__"
# 状態ラベル（アイコンは絵文字ではなく CSS 描画の丸で表す。下の apply/build 参照）。
_STATUS_ON = "記録中"
_STATUS_OFF = "待機中"
_LABEL_START = "記録開始"
_LABEL_STOP = "記録停止"
_LABEL_SHOT = "📸 今すぐ1枚"

# キャプチャ時に、撮れたことを利用者へ知らせるリアクション文言。
# 待機中の単発ショットは「保存しました」、記録中（記録開始/URL移動の自動保存）は
# 「記録しました」を出し、消えると下地の「🔴 記録中」表示に戻る。
_REACT_BUSY = "⏳ 保存中…"
_REACT_DONE = "✓ 保存しました"
_REACT_REC = "✓ 記録しました"

# JS へ埋め込む値は json.dumps で安全にリテラル化（絵文字/日本語も \uXXXX へ）。
# 下の Template では $ID / $S_ON / … がこれらに置換される（JS 中に $ は他に無い）。
_ID_JS = json.dumps(_BADGE_ID)
_S_ON_JS = json.dumps(_STATUS_ON)
_S_OFF_JS = json.dumps(_STATUS_OFF)
_L_START_JS = json.dumps(_LABEL_START)
_L_STOP_JS = json.dumps(_LABEL_STOP)
_L_SHOT_JS = json.dumps(_LABEL_SHOT)
_R_BUSY_JS = json.dumps(_REACT_BUSY)
_R_DONE_JS = json.dumps(_REACT_DONE)

# 操作パネル本体を注入するスクリプト。add_init_script でページ遷移・新規タブにも自動適用される。
#   - window.__eacApplyState(recording) で見た目（記録中/待機中）を更新できる。
#   - window.__eacReact('busy'|'done'|'off') で「今すぐ1枚」の手応えを表示できる。
#   - ボタンは window.__eac_toggle() / window.__eac_shot()（Python 側 expose_binding）を呼ぶ。
_BADGE_SCRIPT = Template(r"""
(() => {
  // add_init_script は各 iframe にも注入される。最上位フレーム以外では
  // 何もしない（iframe の数だけパネルが重複表示されるのを防ぐ）。
  if (window.top !== window.self) return;
  const ID = $ID;
  const S_ON = $S_ON, S_OFF = $S_OFF, L_START = $L_START, L_STOP = $L_STOP, L_SHOT = $L_SHOT;
  const R_BUSY = $R_BUSY, R_DONE = $R_DONE;
  let recording = false;   // 直近に適用された記録状態（再描画時の復元に使う）
  let rxTimer = null;      // 「保存しました」を自動で消すためのタイマー

  function apply(r) {
    recording = !!r;
    const box = document.getElementById(ID);
    if (!box) return;
    const lbl = box.querySelector('[data-eac="label"]');
    const dot = box.querySelector('[data-eac="dot"]');
    const tg = box.querySelector('[data-eac="toggle"]');
    if (lbl) lbl.textContent = recording ? S_ON : S_OFF;
    if (dot) {
      // 記録中＝白い丸、待機中＝白い輪郭のみの丸（固定サイズ・同一質感）。
      if (recording) {
        dot.style.setProperty('background', '#fff', 'important');
        dot.style.setProperty('border', '0', 'important');
      } else {
        dot.style.setProperty('background', 'transparent', 'important');
        dot.style.setProperty('border', '2px solid rgba(255,255,255,.85)', 'important');
      }
    }
    if (tg) tg.textContent = recording ? L_STOP : L_START;
    box.style.setProperty('background', recording ? 'rgba(200,0,0,.92)' : 'rgba(90,90,90,.92)', 'important');
  }

  // 「今すぐ1枚」の手応え。パネルコンテナ内のオーバーレイに重ねるので、
  // 撮影中は他のパネル要素と一緒に隠れ、保存物（png/txt/part）には写り込まない。
  // position:absolute なのでパネル幅（ボタン位置）には影響しない。
  function react(kind, text) {
    const box = document.getElementById(ID);
    if (!box) return;
    const rx = box.querySelector('[data-eac="react"]');
    if (!rx) return;
    if (rxTimer) { clearTimeout(rxTimer); rxTimer = null; }
    if (kind === 'busy') {
      rx.textContent = text || R_BUSY;
      rx.style.setProperty('background', 'rgba(70,70,70,.96)', 'important');
      rx.style.setProperty('display', 'flex', 'important');
    } else if (kind === 'done') {
      rx.textContent = text || R_DONE;   // text 未指定なら「保存しました」
      rx.style.setProperty('background', 'rgba(0,150,60,.96)', 'important');
      rx.style.setProperty('display', 'flex', 'important');
      rxTimer = setTimeout(() => { rx.style.setProperty('display', 'none', 'important'); rxTimer = null; }, 1200);
    } else {
      rx.style.setProperty('display', 'none', 'important');
    }
  }

  function build() {
    if (!document.body || document.getElementById(ID)) return;
    // 遷移直後に誤った状態（待機中）を一瞬見せないよう、先に現在の記録状態を
    // Python へ問い合わせ、分かってからパネルを描画する。
    const render = (initOn) => {
    if (!document.body || document.getElementById(ID)) return;
    const box = document.createElement('div');
    box.id = ID;
    // コンテナ自体は pointer-events:none（下のページ操作を妨げない）。
    // ボタンだけ pointer-events:auto に戻してクリックできるようにする。
    // 高さ固定＋各プロパティを明示してサイト側 CSS の影響を受けないようにする
    // （ページごとにバーの高さ/幅が変わるのを防ぐ）。余白は gap/padding で確保。
    // 全プロパティを !important で明示し、サイト側 CSS（!important 含む）の影響を
    // 完全に遮断する。これでどのページでもバーの高さ・幅・余白が固定になる。
    box.style.cssText =
      'position:fixed !important;top:8px !important;left:50% !important;transform:translateX(-50%) !important;'
      + 'z-index:2147483647 !important;box-sizing:border-box !important;height:36px !important;'
      + 'display:flex !important;align-items:center !important;gap:14px !important;margin:0 !important;padding:0 16px !important;'
      + 'color:#fff !important;font-family:"Segoe UI",sans-serif !important;font-size:13px !important;font-weight:bold !important;line-height:1 !important;'
      + 'border-radius:8px !important;pointer-events:none !important;'
      + 'box-shadow:0 2px 8px rgba(0,0,0,.4) !important;white-space:nowrap !important;';

    const st = document.createElement('span');
    st.setAttribute('data-eac', 'status');
    // 幅固定でボタンが左右に動かないようにしつつ、高さ 36px の flex 中央寄せで
    // 上下均等に中央へ。中身は CSS 描画の丸（dot）＋ラベルで、状態間で見た目を統一。
    st.style.cssText =
      'box-sizing:border-box !important;flex:0 0 auto !important;width:84px !important;height:36px !important;'
      + 'display:inline-flex !important;align-items:center !important;justify-content:center !important;gap:6px !important;'
      + 'line-height:1 !important;margin:0 !important;padding:0 !important;'
      + 'color:#fff !important;font-family:"Segoe UI",sans-serif !important;font-size:13px !important;font-weight:bold !important;';
    // 状態インジケータ（CSS 描画の丸）。絵文字を使わないので記録中/待機中で
    // アイコンの質感・大きさが揃う。塗り/輪郭の違いは apply() で切り替える。
    const dot = document.createElement('span');
    dot.setAttribute('data-eac', 'dot');
    dot.style.cssText =
      'box-sizing:border-box !important;flex:0 0 auto !important;width:9px !important;height:9px !important;'
      + 'border-radius:50% !important;margin:0 !important;padding:0 !important;';
    const lbl = document.createElement('span');
    lbl.setAttribute('data-eac', 'label');
    lbl.style.cssText =
      'flex:0 0 auto !important;margin:0 !important;padding:0 !important;'
      + 'font-family:"Segoe UI",sans-serif !important;font-size:13px !important;font-weight:bold !important;line-height:1 !important;';
    st.appendChild(dot);
    st.appendChild(lbl);

    // ボタン共通スタイル。高さは固定するが幅は中身ぴったり（width 固定しない）。
    // 幅を固定すると絵文字/文字が枠からはみ出して隣との隙間を潰すことがあるため、
    // flex:0 0 auto で内容ぴったりにし、gap:14px が常に効くようにする。
    // appearance 等も !important で明示してサイト側 button スタイルを排除する。
    const bcss =
      'box-sizing:border-box !important;flex:0 0 auto !important;width:auto !important;'
      + 'min-width:0 !important;max-width:none !important;height:26px !important;'
      + 'display:inline-flex !important;align-items:center !important;justify-content:center !important;'
      + 'white-space:nowrap !important;overflow:visible !important;'
      + 'margin:0 !important;padding:0 12px !important;pointer-events:auto !important;cursor:pointer !important;'
      + 'border:0 !important;border-radius:5px !important;background:#fff !important;color:#b00 !important;'
      + 'font-family:"Segoe UI",sans-serif !important;font-size:12px !important;font-weight:bold !important;line-height:1 !important;'
      + 'appearance:none !important;-webkit-appearance:none !important;';
    const tg = document.createElement('button');
    tg.setAttribute('data-eac', 'toggle');
    tg.style.cssText = bcss;
    tg.addEventListener('click', () => { try { window.__eac_toggle(); } catch (e) {} });
    const sh = document.createElement('button');
    sh.setAttribute('data-eac', 'shot');
    sh.textContent = L_SHOT;
    sh.style.cssText = bcss;
    sh.addEventListener('click', () => { try { window.__eac_shot(); } catch (e) {} });

    // 手応え表示用オーバーレイ（既定は非表示）。コンテナ内に絶対配置で重ねる。
    const rx = document.createElement('div');
    rx.setAttribute('data-eac', 'react');
    rx.style.cssText =
      'position:absolute !important;inset:0 !important;display:none !important;box-sizing:border-box !important;'
      + 'align-items:center !important;justify-content:center !important;border-radius:8px !important;'
      + 'margin:0 !important;padding:0 !important;'
      + 'color:#fff !important;font-family:"Segoe UI",sans-serif !important;font-size:13px !important;font-weight:bold !important;'
      + 'pointer-events:none !important;';

    box.appendChild(st);
    box.appendChild(tg);
    box.appendChild(sh);
    box.appendChild(rx);
    document.body.appendChild(box);
    apply(!!initOn);   // 取得した現在の記録状態で最初から正しく描画する
    };
    if (window.__eac_getstate) {
      window.__eac_getstate().then(render).catch(() => render(recording));
    } else {
      render(recording);
    }
  }

  window.__eacApplyState = apply;
  window.__eacReact = react;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
  // サイト側の再描画でパネルが消えても付け直す。
  new MutationObserver(() => { if (!document.getElementById(ID)) build(); })
    .observe(document.documentElement, { childList: true, subtree: true });
})();
""").substitute(
    ID=_ID_JS, S_ON=_S_ON_JS, S_OFF=_S_OFF_JS,
    L_START=_L_START_JS, L_STOP=_L_STOP_JS, L_SHOT=_L_SHOT_JS,
    R_BUSY=_R_BUSY_JS, R_DONE=_R_DONE_JS,
)

# スクリーンショットにバッジを写し込まないための一時 非表示 / 復帰。
# 復帰は display を空にする（削除する）と flex レイアウトが失われ gap が効かなく
# なるため、必ず 'flex'（!important）へ戻す。
_BADGE_HIDE = f"document.getElementById({_ID_JS})?.style.setProperty('display','none','important')"
_BADGE_SHOW = f"document.getElementById({_ID_JS})?.style.setProperty('display','flex','important')"

# body 全文テキスト取得時にバッジを隠して innerText から除外する。
# innerText は display:none の要素を含まないため、隠す→取得→復帰で写り込みを防ぐ。
_BODY_TEXT_JS = Template(r"""
() => {
  const b = document.getElementById($ID);
  if (!b) return document.body ? document.body.innerText : '';
  const prev = b.style.getPropertyValue('display');
  const prio = b.style.getPropertyPriority('display');
  b.style.setProperty('display', 'none', 'important');
  const t = document.body ? document.body.innerText : '';
  if (prev) b.style.setProperty('display', prev, prio); else b.style.removeProperty('display');
  return t;
}
""").substitute(ID=_ID_JS)


def log(msg: str) -> None:
    """メッセージを log.txt へ追記し、可能ならコンソールにも出す。

    windowed exe（コンソール無し）では sys.stdout が None になり得るため、
    print は失敗しても無視する。ファイルへの記録を主とする。
    """
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def _message_box(msg: str, title: str = "edge-auto-capture") -> None:
    """致命的エラーを Windows のメッセージボックスで通知する（失敗しても無視）。

    コンソールを持たない windowed exe では print が見えないため、
    起動失敗などはダイアログで利用者に伝える。
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _notify_fatal(msg: str) -> None:
    """致命的メッセージをログとダイアログの両方へ出す（終了処理は呼び出し側）。"""
    log(msg)
    _message_box(msg)


async def _try_eval(page: Page, js: str) -> None:
    """ページ側 JS を実行。失敗しても無視する（バッジの表示/非表示など副次処理用）。"""
    try:
        await page.evaluate(js)
    except Exception:
        pass

# 保存ファイル名の一意性を保証する通し番号（この起動中で連番）。
# next() は不可分なので、並行する capture() 同士でも番号は重複しない。
_seq_counter = itertools.count(1)

# 実行中の capture タスクへの強参照を保持する集合。
# これが無いとイベントループはタスクを弱参照でしか持たず、
# 実行途中で GC されて消える恐れがある（例外も握り潰される）。
_tasks: "set[asyncio.Task]" = set()


@dataclass
class Config:
    """config.ini から読み込む設定値一式。

    各フィールドの初期値が「既定値」を兼ねる: config.ini にその項目行が
    無い場合、load_config() はここの値へフォールバックする。
    """

    start_url: str = "about:blank"          # Edge 起動時に最初に開くページ
    edge_path: str = ""                     # Edge 実行ファイルのパス（空なら channel="msedge"）
    output_dir: Path = Path("output")       # 保存先フォルダ（png も txt もここ）
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
      - 文字列項目の値だけが空（output_dir / edge_path など）→ 空文字がそのまま入る。
        ※ output_dir が空だと Path('.') となりカレントフォルダへ保存されるので注意。
      - target_selector が空 → 一部抜き出しをスキップ（空が正常値）。
    """
    if not CONFIG_PATH.exists():
        _notify_fatal(
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

        # 相対パスは基準フォルダ基準に固定（exe 隣の output\ に確実に保存する）。
        # 絶対パス指定時はそのまま使う（config.ini で任意の保存先に変更可能）。
        output_dir = Path(sec.get("output_dir", str(defaults.output_dir)))
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir

        # カンマ区切りをタプル化。空URLは常にスキップ対象へ含める。
        urls = [u.strip() for u in sec.get("skip_urls", "").split(",") if u.strip()]

        return Config(
            start_url=sec.get("start_url", defaults.start_url).strip() or "about:blank",
            edge_path=sec.get("edge_path", "").strip(),
            output_dir=output_dir,
            poll_interval=sec.getfloat("poll_interval", defaults.poll_interval),
            settle_delay=sec.getfloat("settle_delay", defaults.settle_delay),
            load_timeout=sec.getint("load_timeout", defaults.load_timeout),
            skip_urls=tuple(urls) + ("",),
            target_selector=sec.get("target_selector", "").strip(),
            start_recording=sec.getboolean("start_recording", defaults.start_recording),
        )
    except (configparser.Error, KeyError, ValueError) as e:
        _notify_fatal(
            f"config.ini の読み込みに失敗しました: {e}\n"
            "[capture] セクションと各項目の値を確認してください。"
        )
        sys.exit(1)


def safe_name(url: str) -> str:
    """URL をファイル名に使える形へ変換（長すぎる場合は先頭 NAME_MAX_LEN 文字）。"""
    name = re.sub(r"[^\w\-]+", "_", url)[:NAME_MAX_LEN].strip("_")
    return name or "page"


@contextmanager
def _step(tag: str, url: str):
    """保存処理 1 ステップ分の共通ラッパ。

    例外が出ても [skip <tag>] を表示して握り、他ステップの続行を妨げない。
    （png / txt / part の 3 ステップで同じ try/except を書かないための共通化）
    """
    try:
        yield
    except Exception as e:
        log(f"[skip {tag}] {url}  ({e})")


async def capture(page: Page, url: str, config: Config) -> None:
    # 一意性は「ミリ秒付き日時 + 通し番号」で保証する。URL スラッグは
    # 読みやすさ用の装飾で、切り詰めや正規化で衝突しても実害はない。
    # seq / stem は await より前に確定させ、番号を予約してから保存する。
    now = datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S")
    ms = now.strftime("%f")[:3]              # マイクロ秒の先頭3桁＝ミリ秒
    seq = next(_seq_counter)
    stem = f"{ts}_{ms}_{seq:04d}_{safe_name(url)}"   # 3ファイルで同じ接頭辞を共有

    # 読み込み完了を待つ（タイムアウトしても続行）
    with _step("load", url):
        await page.wait_for_load_state("load", timeout=config.load_timeout)
    await asyncio.sleep(config.settle_delay)

    # 1) フルページ スクリーンショット
    #    「キャプチャ中」バッジは撮影の瞬間だけ隠し、保存画像へ写し込まない。
    with _step("png", url):
        await _try_eval(page, _BADGE_HIDE)
        try:
            await page.screenshot(
                path=str(config.output_dir / f"{stem}.png"), full_page=True
            )
        finally:
            await _try_eval(page, _BADGE_SHOW)

    # 2) ページ全文テキスト（キャプチャ中バッジは除外して取得）
    with _step("txt", url):
        text = await page.evaluate(_BODY_TEXT_JS)
        (config.output_dir / f"{stem}.txt").write_text(
            f"URL: {url}\n\n{text}", encoding="utf-8"
        )

    # 3) 一部抜き出し（セレクタ設定時のみ）
    if config.target_selector:
        with _step("part", url):
            # 広いセレクタ（div / body / * など）だと操作パネルの文言を拾うことがある。
            # 抽出の間だけパネルを display:none にして innerText から除外する。
            await _try_eval(page, _BADGE_HIDE)
            try:
                parts = await page.locator(config.target_selector).all_inner_texts()
            finally:
                await _try_eval(page, _BADGE_SHOW)
            # 空文字（隠したパネル配下や該当なし要素）は落とす。
            parts = [p for p in parts if p.strip()]
            body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
            (config.output_dir / f"{stem}_part.txt").write_text(
                f"URL: {url}\nSELECTOR: {config.target_selector}\n\n{body}",
                encoding="utf-8",
            )

    log(f"[saved] {stem}.*  <- {url}")


def _spawn_capture(page: Page, url: str, config: Config, done_text: str = _REACT_DONE) -> None:
    """capture() をバックグラウンドタスクとして起動し、参照を保持する。

    撮影の前後にパネル上へ手応え（「保存中…」→ done_text）を表示する。
    done_text は待機中の単発ショットなら「保存しました」、記録中の保存なら
    「記録しました」を渡す。表示はパネル内オーバーレイなので保存物には写り込まない。
    タスクを _tasks に入れて GC を防ぎ、完了時に取り除く。
    """

    async def _run() -> None:
        await _try_eval(page, "window.__eacReact && window.__eacReact('busy')")
        try:
            await capture(page, url, config)
        finally:
            # 完了表示（約1.2秒で自動的に消え、下地の状態表示に戻る）。
            js = "window.__eacReact && window.__eacReact('done', " + json.dumps(done_text) + ")"
            await _try_eval(page, js)

    task = asyncio.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def cleanup_old_profiles() -> None:
    """前回までに残った一時プロファイル（edge-debug-*）を掃除する。

    使用中のフォルダは削除に失敗しても無視する（ignore_errors=True）。
    """
    base = Path(tempfile.gettempdir())
    for d in base.glob("edge-debug-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


@dataclass
class _RecordingState:
    """記録中かどうかの実行時状態（JS ボタンのコールバックとループで共有）。"""

    on: bool = False


async def main(config: Config) -> None:
    seen: "dict[Page, str]" = {}  # page オブジェクト -> 直近のURL
    state = _RecordingState(on=config.start_recording)

    config.output_dir.mkdir(parents=True, exist_ok=True)

    cleanup_old_profiles()
    tmp = tempfile.mkdtemp(prefix="edge-debug-")  # 今回用の一時プロファイル

    # まっさらなプロファイルで起動するための Edge 起動オプション
    # （サインイン/同期ダイアログや初回セットアップ画面を回避する）
    edge_args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=msImplicitSignin",
        # 既定で最大化して起動する。
        "--start-maximized",
    ]
    launch_kwargs = dict(
        user_data_dir=tmp,
        channel="msedge",
        headless=False,
        args=edge_args,
        # Playwright は既定で --no-sandbox を付け、Edge が黄色い警告バナーを出す。
        # サンドボックスを有効化してバナーを消す（キャプチャ画像への映り込みも防ぐ）。
        chromium_sandbox=True,
        # 固定ビューポートのエミュレーションを外し、ウィンドウサイズにページを
        # 追従させる（--start-maximized も no_viewport でないと効かない）。
        no_viewport=True,
    )
    if config.edge_path:
        launch_kwargs["executable_path"] = config.edge_path

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            _notify_fatal(
                f"Edge を起動できませんでした: {e}\n"
                "Edge がインストールされているか、config.ini の edge_path を確認してください。"
            )
            shutil.rmtree(tmp, ignore_errors=True)
            return

        async def _refresh_all_panels() -> None:
            """開いている全ページの操作パネルへ現在の記録状態を反映する。

            新規タブ（初期は待機表示で描画）や、サイト側の再描画で作り直された
            パネルも、毎 tick これを呼ぶことで現在の状態に追従する。
            """
            flag = "true" if state.on else "false"
            for pg in list(context.pages):
                await _try_eval(pg, f"window.__eacApplyState && window.__eacApplyState({flag})")

        async def _on_toggle(source) -> None:
            """「記録開始／停止」ボタン: 記録状態を反転する。

            ON にした瞬間は現在開いている全ページを即キャプチャし、seen を現在 URL に
            そろえる（撮り始めの体感を良くしつつ、直後のループでの二重取りも防ぐ）。
            """
            state.on = not state.on
            log(f"[記録] {'開始' if state.on else '停止'}")
            await _refresh_all_panels()
            if state.on:
                for pg in list(context.pages):
                    try:
                        url = pg.url
                    except Exception:
                        continue
                    if url in config.skip_urls:
                        continue
                    seen[pg] = url
                    _spawn_capture(pg, url, config, done_text=_REACT_REC)

        async def _on_shot(source) -> None:
            """「今すぐ1枚」ボタン: 記録状態に関わらず、押したページを1回だけ撮る。

            seen は触らないので自動キャプチャの判定には影響しない（記録ON中でも
            同一 URL の「撮り直し」として別ファイルにもう1枚保存される）。

            撮れたことが分かるよう、押下直後に「保存中…」、保存完了後に
            「✓ 保存しました」をパネル上に表示する（保存物には写り込まない）。
            背景タスクにせず await するのは、保存完了に合わせて手応えを出すため。
            """
            pg = source["page"]
            try:
                url = pg.url
            except Exception:
                return
            if url in config.skip_urls:
                return
            log(f"[手動] {url}")
            # 記録中の手動ショットは「記録しました」、待機中は「保存しました」を表示。
            _spawn_capture(
                pg, url, config,
                done_text=_REACT_REC if state.on else _REACT_DONE,
            )

        async def _get_state(source) -> bool:
            """パネルが描画前に現在の記録状態を問い合わせるためのバインディング。

            ページ遷移直後、新しいドキュメントのパネルはこれを見てから描画するので、
            記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。
            """
            return state.on

        # ページ内ボタン／パネルから呼び出す Python コールバックを公開する。
        # context 単位なので以後開く新規タブにも自動適用される（add_init_script より前に登録）。
        await context.expose_binding("__eac_toggle", _on_toggle)
        await context.expose_binding("__eac_shot", _on_shot)
        await context.expose_binding("__eac_getstate", _get_state)

        # 全ページ・全タブの上部に操作パネル（記録状態＋記録開始/停止＋今すぐ1枚）を表示する。
        # add_init_script は以後開くページ／新規タブにも自動適用される。
        await context.add_init_script(_BADGE_SCRIPT)

        # Edge のウィンドウを閉じたら監視ループを抜けるためのフラグ
        closed = asyncio.Event()
        context.on("close", lambda: closed.set())

        try:
            # 最初のページで start_url を開く（about:blank ならそのまま）
            page = context.pages[0] if context.pages else await context.new_page()
            if config.start_url and config.start_url != "about:blank":
                try:
                    await page.goto(config.start_url)
                except Exception as e:
                    log(f"[skip goto] {config.start_url}  ({e})")

            log(
                f"Edge を起動しました（記録は{'ON' if state.on else 'OFF（待機）'}で開始）。"
                "ページ上部のパネルで記録開始/停止・今すぐ1枚を操作できます"
                "（終了するには Edge のウィンドウを閉じてください）"
            )

            while not closed.is_set():
                pages = list(context.pages)

                # 閉じられたページを管理から除去
                for pg in list(seen):
                    if pg not in pages:
                        del seen[pg]

                # 記録状態を全パネルに反映（新規タブ・再描画にも毎 tick 追従）
                await _refresh_all_panels()

                # 記録ON の間だけ URL変化 / 新規タブを検知して保存。
                # OFF の間は seen を更新しないので、ON にした瞬間に現在ページが
                # 「変化」として検知され撮れる（_on_toggle でも即撮りするため通常は先回り）。
                if state.on:
                    for pg in pages:
                        try:
                            url = pg.url
                        except Exception:
                            continue
                        if url in config.skip_urls:
                            continue
                        if seen.get(pg) != url:
                            seen[pg] = url
                            _spawn_capture(pg, url, config, done_text=_REACT_REC)

                await asyncio.sleep(config.poll_interval)
        finally:
            # Ctrl+C / ウィンドウを閉じた場合のどちらでもここが走る。
            # 起動した Edge を終了し、一時プロファイルを削除する。
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # ログは追記のみ（既存があればそのまま末尾へ足す。削除・作り直しはしない）。
    log("=== edge-auto-capture 起動 ===")

    config = load_config()
    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        # コンソール実行時のみ届く保険的な停止経路。
        log("停止しました。")
    log("=== 終了 ===")
