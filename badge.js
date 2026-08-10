// 各ページ上部に出す操作バー（記録状態＋記録開始/停止＋今すぐ1枚＋セレクタ入力＋SPA検知トグル）
// のページ側スクリプト。add_init_script でページ遷移・新規タブにも自動適用される。
//
// このファイルは badge.py が読み込み、$CONFIG を 1 個の JSON（表示文言などの設定）へ
// 置換してから add_init_script に渡す。実ファイルなのでエディタ/リンタで構文検査できる。
//
// Python から使う API:
//   - window.__eacApplyState(recording, spaOn, selector) : 見た目を現在状態へ更新
//   - window.__eacReact('busy'|'done'[, text])           : キャプチャの手応え表示
//   - window.__eac_getstate()（expose_binding）           : 描画前に現在状態を取得
//   - window.__eac_barDisplay(show)                       : 保存/抽出中にバーを隠す/戻す
//   - window.__eac_bodyText()                             : バー除外の本文 innerText
//   - window.__eac_signature(selector)                   : SPA検知用のコンテンツ署名
//   - ボタン類は window.__eac_toggle()/__eac_shot()/__eac_spa_toggle()/
//     __eac_set_selector(v)/__eac_commit_selector(v)（すべて expose_binding）を呼ぶ
(() => {
  // add_init_script は各 iframe にも注入される。最上位フレーム以外では
  // 何もしない（iframe の数だけパネルが重複表示されるのを防ぐ）。
  if (window.top !== window.self) return;
  // 表示文言などの設定は Python 側で定義し、$CONFIG（1個の JSON）でまとめて渡す。
  const C = $CONFIG;
  const ID = C.id;
  const S_ON = C.sOn, S_OFF = C.sOff, L_START = C.lStart, L_STOP = C.lStop, L_SHOT = C.lShot;
  const L_SPA = C.lSpa, PH_SEL = C.phSel, TITLE_SEL = C.titleSel, TITLE_SPA = C.titleSpa;
  const R_BUSY = C.rBusy, R_DONE = C.rDone;
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
      const s2 = (selector || '').trim();
      if (!s2) {
        selCount.textContent = '';
      } else {
        try {
          let n = 0;
          document.querySelectorAll(s2).forEach((el) => { if (!box.contains(el)) n++; });
          selCount.textContent = '一致 ' + n + '件';
          selCount.style.setProperty('color', n > 0 ? 'rgba(255,255,255,.85)' : '#ffd24d', 'important');
        } catch (e) {
          selCount.textContent = '無効な指定';
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
    // 中身の有無・長短でバーの幅が動かないよう、常に固定幅の枠を確保しておく
    //（空でも同じ幅を占有＝バー全体の長さが一定になる）。はみ出しは省略記号で丸める。
    const selCount = document.createElement('span');
    selCount.setAttribute('data-eac', 'sel-count');
    selCount.style.cssText =
      'flex:0 0 76px !important;width:76px !important;margin:0 !important;padding:0 !important;'
      + 'white-space:nowrap !important;overflow:hidden !important;text-overflow:ellipsis !important;'
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
    // 無効（セレクタ未設定）ではボタン自身のホバーで tooltip が出ないことがあるため、
    // ラッパ／ラベルにも同じ説明を付け、どこにホバーしても読めるようにする。
    spaWrap.title = TITLE_SPA;
    const spaLbl = document.createElement('span');
    spaLbl.setAttribute('data-eac', 'spa-label');
    spaLbl.textContent = L_SPA;
    spaLbl.title = TITLE_SPA;
    spaLbl.style.cssText =
      'flex:0 0 auto !important;margin:0 !important;padding:0 !important;white-space:nowrap !important;'
      + 'color:#fff !important;font-family:"Segoe UI",sans-serif !important;font-size:12px !important;font-weight:bold !important;line-height:1 !important;';
    const spa = document.createElement('button');
    spa.setAttribute('data-eac', 'spa');
    spa.setAttribute('role', 'switch');
    spa.title = TITLE_SPA;
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

  // ---- Python(capture) から page.evaluate 経由で呼ぶページ側ヘルパ ----
  // いずれも操作バー(#ID)を保存物へ写し込まないよう、内部で「隠す/戻す」を同期実行する。

  // 撮影/抽出の瞬間だけバーを隠す/戻す。復帰は flex（display を消すと gap が失われるため）。
  function barDisplay(show) {
    const b = document.getElementById(ID);
    if (b) b.style.setProperty('display', show ? 'flex' : 'none', 'important');
  }

  // ページ全文テキスト（バーを除外して取得）。隠す→読む→復帰を 1 回で同期実行。
  function bodyText() {
    const b = document.getElementById(ID);
    if (!b) return document.body ? document.body.innerText : '';
    const prev = b.style.getPropertyValue('display');
    const prio = b.style.getPropertyPriority('display');
    b.style.setProperty('display', 'none', 'important');
    const t = document.body ? document.body.innerText : '';
    if (prev) b.style.setProperty('display', prev, prio); else b.style.removeProperty('display');
    return t;
  }

  // SPA検知用のコンテンツ署名。セレクタ一致要素の innerText を連結して短いハッシュにする
  //（全文ではなくハッシュだけ返し、毎 tick の転送量を抑える）。バーは除外。
  // セレクタ不正/該当なしは長さ 0 のハッシュ（＝「変化なし」とみなせる固定値）。
  function signature(sel) {
    const b = document.getElementById(ID);
    let prev, prio;
    if (b) {
      prev = b.style.getPropertyValue('display');
      prio = b.style.getPropertyPriority('display');
      b.style.setProperty('display', 'none', 'important');
    }
    let text = '';
    try {
      if (sel) {
        const parts = [];
        document.querySelectorAll(sel).forEach((el) => { parts.push(el.innerText || ''); });
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

  window.__eacApplyState = apply;
  window.__eacReact = react;
  window.__eac_barDisplay = barDisplay;
  window.__eac_bodyText = bodyText;
  window.__eac_signature = signature;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
  // サイト側の再描画でパネルが消えても付け直す。
  // 注入直後は documentElement がまだ無いページ（about:blank 等）があるため、
  // 監視対象は documentElement → document の順でフォールバックする（常に有効な Node）。
  const _root = document.documentElement || document;
  new MutationObserver(() => { if (!document.getElementById(ID)) build(); })
    .observe(_root, { childList: true, subtree: true });
})();
