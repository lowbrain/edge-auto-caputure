"""
記録ONの間、Edge の URL / タブが変わるたびに、以下を同じフォルダへ自動保存するスクリプト。
  - フルページのスクリーンショット  (.png)
  - ページ全文テキスト              (.txt)
  - ページ内の指定した一部だけ      (_part.txt)   ※セレクタ設定時のみ

キャプチャのタイミングは利用者が操作する。各ページ上部の操作パネルで
「記録開始／停止」で記録期間を制御でき、「今すぐ1枚」で今のページを1回だけ撮れる。
既定は記録OFF（待機）で起動する（config.ini の start_recording で変更可）。

SPA（URLが変わらず中身だけ変わるページ）向けに、パネルの入力欄へ CSS セレクタを
入れて「SPA検知」を ON にすると、記録ON中はそのセレクタ要素の中身が変わるたびに
自動保存する（同じ内容は署名比較で撮らない）。セレクタ入力が空だと SPA検知は使えない。
このセレクタは _part.txt の抜き出し対象も兼ねる（初期値は config.ini の target_selector）。

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
各ページの上部には操作パネル（記録中/待機中の表示＋「記録開始/停止」＋「今すぐ1枚」＋
セレクタ入力欄＋「SPA検知」トグル）を表示する
（保存するスクリーンショットにも、抽出する txt / part テキストにも含めない）。
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
# SPA（URLが変わらず中身だけ変わるページ）向けトグルスイッチの横に出すラベル。
# セレクタ入力に値がある時だけ操作可能。スイッチ内の ON/OFF 表示は JS 側の固定文言。
_LABEL_SPA = "SPA検知"
# セレクタ入力欄の意味づけ。常時ラベルは置かず、プレースホルダ（透かし文字）で入力を促し、
# ホバー時の title で具体例を含む詳しい説明を出す。
_PLACEHOLDER_SEL = "セレクタを入力"
_TITLE_SEL = (
    "SPA検知で変化を監視する CSS セレクタを入力します。指定した要素の中身が変わると自動保存"
    "（一部抜き出し _part.txt の対象も兼ねます）。例: #main / .results / article / .price"
)

# キャプチャ時に、撮れたことを利用者へ知らせるリアクション文言。
# 待機中の単発ショットは「保存しました」、記録中（記録開始/URL移動の自動保存）は
# 「記録しました」を出し、消えると下地の状態表示（記録中/待機中）に戻る。
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
_L_SPA_JS = json.dumps(_LABEL_SPA)
_PH_SEL_JS = json.dumps(_PLACEHOLDER_SEL)
_TITLE_SEL_JS = json.dumps(_TITLE_SEL)
_R_BUSY_JS = json.dumps(_REACT_BUSY)
_R_DONE_JS = json.dumps(_REACT_DONE)

# 操作パネル本体を注入するスクリプト。add_init_script でページ遷移・新規タブにも自動適用される。
#   - window.__eacApplyState(recording) で見た目（記録中/待機中）を更新できる。
#   - window.__eacReact('busy'|'done'[, text]) でキャプチャの手応え（保存中→…）を表示できる。
#   - 描画前に window.__eac_getstate() で現在の記録状態を取得する（遷移直後のちらつき防止）。
#   - ボタンは window.__eac_toggle() / window.__eac_shot()（Python 側 expose_binding）を呼ぶ。
_BADGE_SCRIPT = Template(r"""
(() => {
  // add_init_script は各 iframe にも注入される。最上位フレーム以外では
  // 何もしない（iframe の数だけパネルが重複表示されるのを防ぐ）。
  if (window.top !== window.self) return;
  const ID = $ID;
  const S_ON = $S_ON, S_OFF = $S_OFF, L_START = $L_START, L_STOP = $L_STOP, L_SHOT = $L_SHOT;
  const L_SPA = $L_SPA, PH_SEL = $PH_SEL, TITLE_SEL = $TITLE_SEL;
  const R_BUSY = $R_BUSY, R_DONE = $R_DONE;
  let recording = false;   // 直近に適用された記録状態（再描画時の復元に使う）
  let spaOn = false;       // 直近に適用された SPA 検知状態
  let selector = "";       // 直近に適用された SPA 検知対象セレクタ（入力欄の値）
  let rxTimer = null;      // 「保存しました」を自動で消すためのタイマー

  function apply(r, s, sel) {
    recording = !!r;
    spaOn = !!s;
    if (sel !== undefined && sel !== null) selector = String(sel);
    const box = document.getElementById(ID);
    if (!box) return;
    const lbl = box.querySelector('[data-eac="label"]');
    const dot = box.querySelector('[data-eac="dot"]');
    const tg = box.querySelector('[data-eac="toggle"]');
    const spa = box.querySelector('[data-eac="spa"]');        // トグルスイッチのトラック
    const spaKnob = box.querySelector('[data-eac="spa-knob"]');
    const spaText = box.querySelector('[data-eac="spa-text"]');
    const spaWrap = box.querySelector('[data-eac="spa-wrap"]');
    const inp = box.querySelector('[data-eac="selector"]');
    const selCount = box.querySelector('[data-eac="sel-count"]');
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
    // セレクタ入力欄はフォーカス中は書き換えない（タイピングを壊さない）。
    // 非フォーカス時のみ現在値と差があれば同期（別タブでの変更を反映）。
    if (inp && document.activeElement !== inp && inp.value !== selector) inp.value = selector;
    // 一致件数フィードバック。0件/不正はすぐ気づけるよう色を変える。
    // 操作バー自身の要素は件数から除外する（button/span 等を指定しても紛れないように）。
    if (selCount) {
      const s = (selector || '').trim();
      if (!s) {
        selCount.textContent = '';
      } else {
        try {
          let n = 0;
          document.querySelectorAll(s).forEach((el) => { if (!box.contains(el)) n++; });
          selCount.textContent = '一致 ' + n + '件';
          selCount.style.setProperty('color', n > 0 ? 'rgba(255,255,255,.85)' : '#ffd24d', 'important');
        } catch (e) {
          selCount.textContent = '無効なセレクタ';
          selCount.style.setProperty('color', '#ffd24d', 'important');
        }
      }
    }
    // SPA検知トグルスイッチ: セレクタ未設定なら無効（灰色・押せない）。設定時のみ切替可。
    // ON=緑・ノブ右・「ON」左寄せ / OFF=半透明白・ノブ左・「OFF」右寄せ。
    const present = (selector || '').trim().length > 0;
    if (spa) {
      spa.disabled = !present;
      if (!present) {
        spa.style.setProperty('background', 'rgba(255,255,255,.2)', 'important');
        spa.style.setProperty('cursor', 'not-allowed', 'important');
        if (spaWrap) spaWrap.style.setProperty('opacity', '.45', 'important');
      } else {
        spa.style.setProperty('background', spaOn ? '#7cc243' : 'rgba(255,255,255,.35)', 'important');
        spa.style.setProperty('cursor', 'pointer', 'important');
        if (spaWrap) spaWrap.style.setProperty('opacity', '1', 'important');
      }
      // ノブ位置（トラック50px・ノブ20px → 右端は left:28px）。
      if (spaKnob) spaKnob.style.setProperty('left', spaOn ? '28px' : '2px', 'important');
      // ON/OFF 文言はノブと反対側へ寄せる。
      if (spaText) {
        spaText.textContent = spaOn ? 'ON' : 'OFF';
        if (spaOn) {
          spaText.style.setProperty('left', '8px', 'important');
          spaText.style.setProperty('right', 'auto', 'important');
        } else {
          spaText.style.setProperty('right', '7px', 'important');
          spaText.style.setProperty('left', 'auto', 'important');
        }
      }
    }
  }

  // キャプチャの手応え（保存中→保存しました/記録しました）。パネルコンテナ内の
  // オーバーレイに重ねるので、撮影中は他のパネル要素と一緒に隠れ、保存物
  // （png/txt/part）には写り込まない。position:absolute なのでパネル幅（ボタン位置）
  // には影響しない。text を渡すと done の文言を差し替えられる（既定は「保存しました」）。
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
    // 遷移直後に誤った状態（待機中）を一瞬見せないよう、先に現在の状態
    //（記録中/SPA検知/セレクタ）を Python へ問い合わせ、分かってから描画する。
    // 引数名は下の status 用 span (const st) と衝突しないよう initState にする。
    const render = (initState) => {
    if (!document.body || document.getElementById(ID)) return;
    const box = document.createElement('div');
    box.id = ID;
    // コンテナは pointer-events:none（下のページ操作を妨げない。ボタンだけ後で
    // pointer-events:auto に戻す）。全プロパティを !important で明示して、サイト側
    // CSS（!important 含む）の影響を完全に遮断し、どのページでもバーの高さ・幅・
    // 余白を一定に保つ。要素間の余白は gap で確保する。
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

    // SPA検知の対象セレクタ入力欄。常時ラベルは置かず、透かし文字（placeholder）で入力を促し、
    // title でホバー時の詳しい説明（具体例つき）を出す。値がある時だけ SPA検知が押せる。
    const selWrap = document.createElement('span');
    selWrap.setAttribute('data-eac', 'sel-wrap');
    selWrap.style.cssText =
      'box-sizing:border-box !important;flex:0 0 auto !important;display:inline-flex !important;'
      + 'align-items:center !important;gap:8px !important;margin:0 !important;padding:0 !important;'
      + 'pointer-events:auto !important;';
    // サイト側 CSS の影響を排除するため主要プロパティを !important で明示する。
    const inp = document.createElement('input');
    inp.setAttribute('data-eac', 'selector');
    inp.type = 'text';
    inp.placeholder = PH_SEL;
    inp.title = TITLE_SEL;
    inp.style.cssText =
      'box-sizing:border-box !important;flex:0 0 auto !important;width:180px !important;height:26px !important;'
      + 'margin:0 !important;padding:0 8px !important;pointer-events:auto !important;'
      + 'border:1px solid rgba(255,255,255,.6) !important;border-radius:5px !important;'
      + 'background:#fff !important;color:#111 !important;'
      + 'font-family:"Segoe UI",sans-serif !important;font-size:12px !important;font-weight:normal !important;line-height:1 !important;'
      + 'appearance:none !important;-webkit-appearance:none !important;';
    // 入力のたびにローカルで即座に見た目（SPAボタンの有効/無効）を反映しつつ、
    // Python 側へも値を通知する（入力欄はフォーカス中なので apply が上書きしない）。
    inp.addEventListener('input', () => {
      apply(recording, spaOn, inp.value);
      try { window.__eac_set_selector(inp.value); } catch (e) {}
    });
    // 確定時（blur / Enter）に最終値をログへ（入力毎の氾濫を避ける）。
    inp.addEventListener('change', () => { try { window.__eac_commit_selector(inp.value); } catch (e) {} });
    // 一致件数の表示（入力欄の右）。文言・色は apply() が現在のセレクタから更新する。
    const selCount = document.createElement('span');
    selCount.setAttribute('data-eac', 'sel-count');
    selCount.style.cssText =
      'flex:0 0 auto !important;margin:0 !important;padding:0 !important;white-space:nowrap !important;'
      + 'color:rgba(255,255,255,.85) !important;font-family:"Segoe UI",sans-serif !important;font-size:11px !important;font-weight:normal !important;line-height:1 !important;';
    selWrap.appendChild(inp);
    selWrap.appendChild(selCount);

    // SPA検知トグル: 「SPA検知」ラベル＋ピル型スイッチ（ノブがスライドする ON/OFF）。
    // スイッチのトラック(button[data-eac=spa])がクリック対象。見た目は apply() が更新する。
    const spaWrap = document.createElement('span');
    spaWrap.setAttribute('data-eac', 'spa-wrap');
    spaWrap.style.cssText =
      'box-sizing:border-box !important;flex:0 0 auto !important;display:inline-flex !important;'
      + 'align-items:center !important;gap:8px !important;margin:0 !important;padding:0 !important;'
      + 'pointer-events:auto !important;';
    const spaLbl = document.createElement('span');
    spaLbl.setAttribute('data-eac', 'spa-label');
    spaLbl.textContent = L_SPA;
    spaLbl.style.cssText =
      'flex:0 0 auto !important;margin:0 !important;padding:0 !important;white-space:nowrap !important;'
      + 'color:#fff !important;font-family:"Segoe UI",sans-serif !important;font-size:12px !important;font-weight:bold !important;line-height:1 !important;';
    const spa = document.createElement('button');
    spa.setAttribute('data-eac', 'spa');
    spa.setAttribute('role', 'switch');
    spa.style.cssText =
      'box-sizing:border-box !important;position:relative !important;flex:0 0 auto !important;'
      + 'width:50px !important;height:24px !important;margin:0 !important;padding:0 !important;'
      + 'border:0 !important;border-radius:12px !important;pointer-events:auto !important;cursor:pointer !important;'
      + 'background:rgba(255,255,255,.35) !important;appearance:none !important;-webkit-appearance:none !important;'
      + 'transition:background .15s ease !important;';
    const spaText = document.createElement('span');
    spaText.setAttribute('data-eac', 'spa-text');
    spaText.style.cssText =
      'position:absolute !important;top:0 !important;height:24px !important;display:flex !important;'
      + 'align-items:center !important;margin:0 !important;padding:0 !important;'
      + 'font-family:"Segoe UI",sans-serif !important;font-size:10px !important;font-weight:bold !important;line-height:1 !important;'
      + 'color:#fff !important;pointer-events:none !important;';
    const spaKnob = document.createElement('span');
    spaKnob.setAttribute('data-eac', 'spa-knob');
    spaKnob.style.cssText =
      'position:absolute !important;top:2px !important;left:2px !important;width:20px !important;height:20px !important;'
      + 'border-radius:50% !important;background:#fff !important;margin:0 !important;padding:0 !important;'
      + 'box-shadow:0 1px 2px rgba(0,0,0,.35) !important;transition:left .15s ease !important;pointer-events:none !important;';
    spa.appendChild(spaText);
    spa.appendChild(spaKnob);
    spa.addEventListener('click', () => { try { window.__eac_spa_toggle(); } catch (e) {} });
    spaWrap.appendChild(spaLbl);
    spaWrap.appendChild(spa);

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
    box.appendChild(selWrap);
    box.appendChild(spaWrap);
    box.appendChild(rx);
    document.body.appendChild(box);
    // 取得した現在の状態（記録中/SPA検知/セレクタ）で最初から正しく描画する。
    apply(!!(initState && initState.recording), !!(initState && initState.spa),
          (initState && initState.selector) || '');
    };
    const fallback = { recording: recording, spa: spaOn, selector: selector };
    if (window.__eac_getstate) {
      window.__eac_getstate().then(render).catch(() => render(fallback));
    } else {
      render(fallback);
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
    L_SPA=_L_SPA_JS, PH_SEL=_PH_SEL_JS, TITLE_SEL=_TITLE_SEL_JS,
    R_BUSY=_R_BUSY_JS, R_DONE=_R_DONE_JS,
)

# スクリーンショットに操作パネルを写し込まないための一時 非表示 / 復帰。
# 復帰は display を空にする（削除する）と flex レイアウトが失われ gap が効かなく
# なるため、必ず 'flex'（!important）へ戻す。
_BADGE_HIDE = f"document.getElementById({_ID_JS})?.style.setProperty('display','none','important')"
_BADGE_SHOW = f"document.getElementById({_ID_JS})?.style.setProperty('display','flex','important')"

# body 全文テキスト取得時に操作パネルを隠して innerText から除外する。
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

# SPA検知用のコンテンツ署名を計算する JS。引数のセレクタに一致する要素群の
# innerText を連結し、短いハッシュ文字列にして返す（全文ではなくハッシュだけ返し
# 毎 tick の転送量を抑える）。操作パネルは _BODY_TEXT_JS と同様に「隠す→読む→復帰」を
# 1回の evaluate 内で同期実行し、パネル文言を署名へ混ぜない・画面をちらつかせない。
# セレクタ不正/該当なしは空扱い（'0_0'）にして「変化なし」とみなす。
_SIG_JS = Template(r"""
(selector) => {
  const b = document.getElementById($ID);
  let prev, prio;
  if (b) {
    prev = b.style.getPropertyValue('display');
    prio = b.style.getPropertyPriority('display');
    b.style.setProperty('display', 'none', 'important');
  }
  let text = '';
  try {
    if (selector) {
      const parts = [];
      document.querySelectorAll(selector).forEach((el) => { parts.push(el.innerText || ''); });
      text = parts.join('\n');
    }
  } catch (e) { text = ''; }
  if (b) { if (prev) b.style.setProperty('display', prev, prio); else b.style.removeProperty('display'); }
  // cyrb53 相当の簡易ハッシュ。衝突は実害小。長さ＋ハッシュで実質的に判別する。
  let h1 = 0xdeadbeef ^ text.length, h2 = 0x41c6ce57 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  const hash = (h2 >>> 0).toString(16) + (h1 >>> 0).toString(16);
  return text.length + '_' + hash;
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


async def capture(page: Page, url: str, config: Config, selector: str = "") -> None:
    # selector は「一部抜き出し(_part.txt)」の対象 CSS セレクタ。操作バーの入力欄で
    # 実行時に変えられるため、config 固定値ではなく呼び出し時の値を使う
    #（初期値は config.target_selector）。空なら _part.txt はスキップ。
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
    #    操作パネルは撮影の瞬間だけ隠し、保存画像へ写し込まない。
    with _step("png", url):
        await _try_eval(page, _BADGE_HIDE)
        try:
            await page.screenshot(
                path=str(config.output_dir / f"{stem}.png"), full_page=True
            )
        finally:
            await _try_eval(page, _BADGE_SHOW)

    # 2) ページ全文テキスト（操作パネルは除外して取得）
    with _step("txt", url):
        text = await page.evaluate(_BODY_TEXT_JS)
        (config.output_dir / f"{stem}.txt").write_text(
            f"URL: {url}\n\n{text}", encoding="utf-8"
        )

    # 3) 一部抜き出し（セレクタ設定時のみ）
    if selector:
        with _step("part", url):
            # 広いセレクタ（div / body / * など）だと操作パネルの文言を拾うことがある。
            # 抽出の間だけパネルを display:none にして innerText から除外する。
            await _try_eval(page, _BADGE_HIDE)
            try:
                parts = await page.locator(selector).all_inner_texts()
            finally:
                await _try_eval(page, _BADGE_SHOW)
            # 空文字（隠したパネル配下や該当なし要素）は落とす。
            parts = [p for p in parts if p.strip()]
            body = "\n---\n".join(parts) if parts else "(該当箇所が見つかりませんでした)"
            (config.output_dir / f"{stem}_part.txt").write_text(
                f"URL: {url}\nSELECTOR: {selector}\n\n{body}",
                encoding="utf-8",
            )

    log(f"[saved] {stem}.*  <- {url}")


def _spawn_capture(
    page: Page, url: str, config: Config, selector: str = "", done_text: str = _REACT_DONE
) -> None:
    """capture() をバックグラウンドタスクとして起動し、参照を保持する。

    撮影の前後にパネル上へ手応え（「保存中…」→ done_text）を表示する。
    done_text は待機中の単発ショットなら「保存しました」、記録中の保存なら
    「記録しました」を渡す。表示はパネル内オーバーレイなので保存物には写り込まない。
    selector は _part.txt 抜き出しの対象（実行時のバー入力値）を capture() へ渡す。
    タスクを _tasks に入れて GC を防ぎ、完了時に取り除く。
    """

    async def _run() -> None:
        await _try_eval(page, "window.__eacReact && window.__eacReact('busy')")
        try:
            await capture(page, url, config, selector)
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
    """実行時状態（JS ボタンのコールバックとループで共有）。

    - on:       記録中か（マスタースイッチ。自動保存は記録ON中のみ走る）
    - spa_on:   SPA検知（中身の変化を契機に自動保存）を有効にするか
    - selector: SPA検知の対象／_part.txt 抜き出しの対象 CSS セレクタ（バー入力の実行時値）
    不変条件: selector が空なら spa_on は必ず False（検知対象が無いため）。
    """

    on: bool = False
    spa_on: bool = False
    selector: str = ""


async def main(config: Config) -> None:
    seen: "dict[Page, str]" = {}  # page オブジェクト -> 直近のURL
    # SPA検知用の署名。sig_seen=最後に撮った署名 / sig_prev=前 tick の署名（落ち着き判定用）。
    sig_seen: "dict[Page, str]" = {}
    sig_prev: "dict[Page, str]" = {}
    state = _RecordingState(on=config.start_recording, selector=config.target_selector)

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

        async def _reseed_signatures() -> None:
            """全ページの SPA署名基準を「現在の内容」に取り直す。

            SPA検知を ON にした瞬間やセレクタを変えた直後に呼ぶ。基準を現状に
            合わせることで、開始/変更の直後に無駄撮りせず、以後の「変化」だけを契機にする。
            """
            for pg in list(context.pages):
                try:
                    sig = await pg.evaluate(_SIG_JS, state.selector)
                except Exception:
                    continue
                sig_seen[pg] = sig
                sig_prev[pg] = sig

        async def _refresh_all_panels() -> None:
            """開いている全ページの操作パネルへ現在の状態を反映する。

            記録中/SPA検知/セレクタの3つを送る。新規タブ（初期は待機表示で描画）や、
            サイト側の再描画で作り直されたパネルも、毎 tick これを呼ぶことで追従する。
            """
            flag = "true" if state.on else "false"
            spa_flag = "true" if state.spa_on else "false"
            sel = json.dumps(state.selector)  # 日本語/記号を含んでも安全に JS リテラル化
            for pg in list(context.pages):
                await _try_eval(
                    pg,
                    f"window.__eacApplyState && window.__eacApplyState({flag}, {spa_flag}, {sel})",
                )

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
                    _spawn_capture(pg, url, config, state.selector, done_text=_REACT_REC)

        async def _on_shot(source) -> None:
            """「今すぐ1枚」ボタン: 記録状態に関わらず、押したページを1回だけ撮る。

            seen は触らないので自動キャプチャの判定には影響しない（記録ON中でも
            同一 URL の「撮り直し」として別ファイルにもう1枚保存される）。

            手応え表示（保存中→保存しました/記録しました）は _spawn_capture が
            撮影の前後で行う（記録中は「記録しました」、待機中は「保存しました」）。
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
                pg, url, config, state.selector,
                done_text=_REACT_REC if state.on else _REACT_DONE,
            )

        async def _on_spa_toggle(source) -> None:
            """「SPA検知」ボタン: 中身の変化を契機にした自動保存を ON/OFF する。

            セレクタ未設定では検知対象が無いので no-op（UI 側でも無効化しているが二重防御）。
            ON にした瞬間は署名基準を現状に取り直し、開始直後の無駄撮りを避ける。
            """
            if not state.selector:
                return
            state.spa_on = not state.spa_on
            log(f"[SPA] {'ON' if state.spa_on else 'OFF'}")
            if state.spa_on:
                await _reseed_signatures()
            await _refresh_all_panels()

        async def _on_set_selector(source, value) -> None:
            """セレクタ入力欄の変更（入力のたびに呼ばれる）。実行時セレクタを更新する。

            空になったら SPA検知を OFF に落とす（検知対象が無いため）。SPA検知中に
            対象が変わったら署名基準を取り直す（旧セレクタの署名で誤検知しないため）。
            ログは氾濫を避けるためここでは出さず、確定時（_on_commit_selector）に出す。
            """
            new = (value or "").strip()
            if new == state.selector:
                return
            state.selector = new
            if not new:
                state.spa_on = False
            elif state.spa_on:
                await _reseed_signatures()
            await _refresh_all_panels()

        async def _on_commit_selector(source, value) -> None:
            """セレクタ入力の確定（blur / Enter）。最終値をログに残す。

            入力のたびに出すとログが氾濫するため、確定時にだけ実際に使う値を記録する。
            これにより「どのセレクタで動かしたか」がログと実態で一致する。
            """
            new = (value or "").strip()
            log(f"[セレクタ] {'クリア' if not new else repr(new)}")

        async def _get_state(source) -> dict:
            """パネルが描画前に現在の状態を問い合わせるためのバインディング。

            ページ遷移直後、新しいドキュメントのパネルはこれを見てから描画するので、
            記録ON中に別URLへ移動しても一瞬「待機中」を見せずに済む。SPA検知の
            ON/OFF・セレクタ値も同時に返し、遷移後も入力欄・ボタンを正しく初期化する。
            """
            return {"recording": state.on, "spa": state.spa_on, "selector": state.selector}

        # ページ内ボタン／パネルから呼び出す Python コールバックを公開する。
        # context 単位なので以後開く新規タブにも自動適用される（add_init_script より前に登録）。
        await context.expose_binding("__eac_toggle", _on_toggle)
        await context.expose_binding("__eac_shot", _on_shot)
        await context.expose_binding("__eac_spa_toggle", _on_spa_toggle)
        await context.expose_binding("__eac_set_selector", _on_set_selector)
        await context.expose_binding("__eac_commit_selector", _on_commit_selector)
        await context.expose_binding("__eac_getstate", _get_state)

        # 全ページ・全タブの上部に操作パネル（記録状態＋記録開始/停止＋今すぐ1枚＋
        # セレクタ入力＋SPA検知トグル）を表示する。
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
                "ページ上部のパネルで記録開始/停止・今すぐ1枚・SPA検知（セレクタ入力時）を"
                "操作できます（終了するには Edge のウィンドウを閉じてください）"
            )

            while not closed.is_set():
                pages = list(context.pages)

                # 閉じられたページを管理から除去（seen と SPA署名の両方）
                for pg in list(seen):
                    if pg not in pages:
                        del seen[pg]
                for pg in list(sig_prev):
                    if pg not in pages:
                        sig_prev.pop(pg, None)
                        sig_seen.pop(pg, None)

                # 記録状態を全パネルに反映（新規タブ・再描画にも毎 tick 追従）
                await _refresh_all_panels()

                # 記録ON の間だけ URL変化 / 新規タブ / (SPA検知ON なら)中身の変化を検知して保存。
                # OFF の間は seen を更新しないので、ON にした瞬間に現在ページが
                # 「変化」として検知され撮れる（_on_toggle でも即撮りするため通常は先回り）。
                if state.on:
                    spa_active = state.spa_on and bool(state.selector)
                    for pg in pages:
                        try:
                            url = pg.url
                        except Exception:
                            continue
                        if url in config.skip_urls:
                            continue

                        url_changed = seen.get(pg) != url
                        if url_changed:
                            seen[pg] = url
                            _spawn_capture(pg, url, config, state.selector, done_text=_REACT_REC)

                        # SPA検知: セレクタ要素の中身の変化を契機に保存。
                        # 「前回撮影時と署名が違う」かつ「前 tick から署名が不変（＝落ち着いた）」
                        # 時だけ撮る（描画途中の多段レンダを撮らない）。
                        if spa_active:
                            try:
                                sig = await pg.evaluate(_SIG_JS, state.selector)
                            except Exception:
                                sig = None
                            if sig is not None:
                                if url_changed:
                                    # 遷移直後は URL 側で撮ったので、その内容を基準にして二重撮り防止。
                                    sig_seen[pg] = sig
                                elif sig != sig_seen.get(pg) and sig == sig_prev.get(pg):
                                    sig_seen[pg] = sig
                                    _spawn_capture(
                                        pg, url, config, state.selector, done_text=_REACT_REC
                                    )
                                sig_prev[pg] = sig

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
