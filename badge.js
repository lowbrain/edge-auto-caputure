// 各ページ上部に出す操作バー（記録状態＋記録開始/停止＋今すぐ1枚＋セレクタ入力＋SPA検知トグル）
// のページ側スクリプト。add_init_script でページ遷移・新規タブにも自動適用される。
//
// このファイルは badge.py が読み込み、$CONFIG を 1 個の JSON（表示文言などの設定）へ
// 置換してから add_init_script に渡す。実ファイルなのでエディタ/リンタで構文検査できる。
//
// バーは Shadow DOM の中に作る。サイト側 CSS はシャドウ境界を越えて中の要素に当たらない
// ため、隔離用の !important を各要素へ付ける必要がなく、見た目は下の <style> 1 枚に集約できる。
// また document.querySelectorAll はシャドウ境界を越えないので、SPA署名・_part.txt 抽出・
// 一致件数の計算にパネル要素が紛れ込まない（＝そのための「隠す/戻す」処理も不要）。
//
// 撮影の合図（captureStart/captureEnd）: スクショにはバーが写るので、撮影時は
// バーを上へスライドさせて画面外へ退避してから撮る（意図した動作に見せる）。撮影が
// 終わったら全画面の赤枠を一瞬フラッシュ（シャッター確定の合図）し、バーを元位置へ戻す。
//
// Python から使う API:
//   - window.__eacApplyState(recording, spaOn, selector) : 見た目を現在状態へ更新
//   - window.__eac_captureStart()                         : バーを退避し切るまで待つ（撮影直前）
//   - window.__eac_captureEnd()                           : 赤枠フラッシュ＋バー復帰（撮影直後）
//   - window.__eac_getstate()（expose_binding）           : 描画前に現在状態を取得
//   - window.__eac_barDisplay(show)                       : バーを隠す/戻す（本文取得時に使用）
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

  let recording = false;   // 直近に適用された記録状態（再描画時の復元に使う）
  let spaOn = false;       // 直近に適用された SPA 検知状態
  let selector = "";       // 直近に適用された SPA 検知対象セレクタ（入力欄の値）
  let capDepth = 0;        // 進行中の撮影数（重なっても最後の1つで復帰させるための入れ子カウント）
  let frameTimer = null;   // 赤枠フラッシュ（.flash クラス）を消すためのタイマー
  let barTimer = null;     // フラッシュ後にバー復帰を少し遅らせるためのタイマー

  let host = null;         // ページ側 DOM に置くシャドウホスト（本文取得時の非表示もこれを操作）
  let shadow = null;       // host.shadowRoot（フォーカス判定・要素取得に使う）
  let els = null;          // シャドウ内の主要要素をまとめた参照

  // シャドウホストの最小限のスタイル。画面上端いっぱいに敷いて重なり順と当たり判定の
  // 透過だけをインライン !important で固定する（サイト側 CSS に負けない）。見た目は
  // シャドウ内の <style> が受け持つ。中央寄せは内側の flex で行い、ホストには transform を
  // 使わない（transform を持つ要素は position:fixed の子の基準になってしまい、下の全画面
  // 赤枠がホスト内に閉じ込められてしまうため）。
  const HOST_CSS =
    'position:fixed !important;top:0 !important;left:0 !important;right:0 !important;'
    + 'z-index:2147483647 !important;margin:0 !important;padding:0 !important;border:0 !important;'
    + 'pointer-events:none !important;display:block !important;';

  // シャドウ内のスタイルと構造。境界で隔離されるので通常の CSS クラスで書ける。
  // 文言（ラベル/プレースホルダ/title）は textContent/属性で後から入れる（HTML へ
  // 直接埋め込まず、日本語・絵文字・引用符のエスケープを気にせずに済ませる）。
  const TEMPLATE =
    '<style>'
    + '.wrap{display:flex;justify-content:center;align-items:flex-start;padding-top:8px;pointer-events:none;}'
    + '.bar{box-sizing:border-box;height:36px;display:flex;align-items:center;gap:14px;'
    + 'margin:0;padding:0 16px;border-radius:8px;pointer-events:none;white-space:nowrap;'
    + 'color:#fff;font-family:"Segoe UI",sans-serif;font-size:13px;font-weight:bold;line-height:1;'
    + 'box-shadow:0 2px 8px rgba(0,0,0,.4);background:rgba(90,90,90,.92);'
    // 退避/復帰は同じ transition（＝隠す動きと戻る動きを対称に）。少しゆっくりの ease-out。
    + 'transition:transform .24s cubic-bezier(.22,.61,.36,1);}'
    + '.bar.rec{background:rgba(200,0,0,.92);}'
    + '.bar.idle{background:rgba(90,90,90,.92);}'
    // 撮影時: バーを上端の外まで退避（下の赤枠と重ならず、スクショにも写らない）。
    + '.bar.capturing{transform:translateY(-64px);}'
    + '.status{box-sizing:border-box;flex:0 0 auto;width:84px;height:36px;'
    + 'display:inline-flex;align-items:center;justify-content:center;gap:6px;}'
    + '.dot{box-sizing:border-box;flex:0 0 auto;width:9px;height:9px;border-radius:50%;}'
    + '.dot.rec{background:#fff;border:0;}'
    + '.dot.idle{background:transparent;border:2px solid rgba(255,255,255,.85);}'
    + '.label{flex:0 0 auto;}'
    + '.btn{box-sizing:border-box;flex:0 0 auto;height:26px;display:inline-flex;align-items:center;'
    + 'justify-content:center;white-space:nowrap;margin:0;padding:0 12px;pointer-events:auto;cursor:pointer;'
    + 'border:0;border-radius:5px;background:#fff;color:#b00;'
    + 'font-family:"Segoe UI",sans-serif;font-size:12px;font-weight:bold;line-height:1;'
    + 'appearance:none;-webkit-appearance:none;}'
    + '.sel-wrap{box-sizing:border-box;flex:0 0 auto;display:inline-flex;align-items:center;gap:8px;pointer-events:auto;}'
    + '.sel{box-sizing:border-box;flex:0 0 auto;width:180px;height:26px;margin:0;padding:0 8px;pointer-events:auto;'
    + 'border:1px solid rgba(255,255,255,.6);border-radius:5px;background:#fff;color:#111;'
    + 'font-family:"Segoe UI",sans-serif;font-size:12px;font-weight:normal;line-height:1;'
    + 'appearance:none;-webkit-appearance:none;}'
    + '.sel-count{flex:0 0 76px;width:76px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
    + 'color:rgba(255,255,255,.85);font-family:"Segoe UI",sans-serif;font-size:11px;font-weight:normal;line-height:1;}'
    + '.sel-count.warn{color:#ffd24d;}'
    + '.spa-wrap{box-sizing:border-box;flex:0 0 auto;display:inline-flex;align-items:center;gap:8px;pointer-events:auto;}'
    + '.spa-wrap.disabled{opacity:.45;}'
    + '.spa-label{flex:0 0 auto;white-space:nowrap;color:#fff;'
    + 'font-family:"Segoe UI",sans-serif;font-size:12px;font-weight:bold;line-height:1;}'
    + '.spa{box-sizing:border-box;position:relative;flex:0 0 auto;width:50px;height:24px;margin:0;padding:0;'
    + 'border:0;border-radius:12px;pointer-events:auto;cursor:pointer;background:rgba(255,255,255,.35);'
    + 'appearance:none;-webkit-appearance:none;transition:background .15s ease;}'
    + '.spa.on{background:#7cc243;}'
    + '.spa.off{background:rgba(255,255,255,.35);}'
    + '.spa:disabled{background:rgba(255,255,255,.2);cursor:not-allowed;}'
    + '.spa-text{position:absolute;top:0;height:24px;display:flex;align-items:center;'
    + 'color:#fff;font-family:"Segoe UI",sans-serif;font-size:10px;font-weight:bold;line-height:1;pointer-events:none;}'
    + '.spa.on .spa-text{left:8px;right:auto;}'
    + '.spa.off .spa-text{right:7px;left:auto;}'
    + '.spa-knob{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#fff;'
    + 'box-shadow:0 1px 2px rgba(0,0,0,.35);transition:left .15s ease;pointer-events:none;}'
    + '.spa.on .spa-knob{left:28px;}'
    + '.spa.off .spa-knob{left:2px;}'
    // 撮影完了の合図に使う全画面の赤いシャッターフラッシュ（既定は透明、.flash で発光）。
    // ホストに transform が無いので position:fixed はビューポート基準になり全画面に出る。
    // 枠は付けず、全画面の淡い赤み＋内側から広がる赤いグローだけ。パッと光って引く単発フラッシュ。
    + '.frame{position:fixed;inset:0;'
    + 'background:rgba(255,0,0,.10);box-shadow:inset 0 0 90px 14px rgba(255,0,0,.55);'
    + 'pointer-events:none;opacity:0;}'
    + '.frame.flash{animation:eac-shutter .5s ease-out;}'
    + '@keyframes eac-shutter{0%{opacity:0;}9%{opacity:1;}100%{opacity:0;}}'
    + '</style>'
    + '<div class="wrap"><div class="bar idle" data-eac="bar">'
    + '<span class="status"><span class="dot idle" data-eac="dot"></span><span class="label" data-eac="label"></span></span>'
    + '<button class="btn" data-eac="toggle"></button>'
    + '<button class="btn" data-eac="shot"></button>'
    + '<span class="sel-wrap"><input class="sel" type="text" data-eac="selector"><span class="sel-count" data-eac="sel-count"></span></span>'
    + '<span class="spa-wrap" data-eac="spa-wrap"><span class="spa-label" data-eac="spa-label"></span>'
    + '<button class="spa off" role="switch" data-eac="spa"><span class="spa-text" data-eac="spa-text"></span>'
    + '<span class="spa-knob"></span></button></span>'
    + '</div></div>'
    + '<div class="frame" data-eac="frame"></div>';

  // 現在状態（記録中/SPA検知/セレクタ）をバーの見た目へ反映する。
  // 見た目の切り替えはすべて CSS クラス（rec/idle・on/off・warn・disabled）の付け外しで行う。
  function apply(r, s, sel) {
    recording = !!r;
    spaOn = !!s;
    if (sel !== undefined && sel !== null) selector = String(sel);
    if (!els) return;
    const present = (selector || '').trim().length > 0;

    // 記録中＝赤バー・白丸／待機中＝灰バー・白輪郭の丸。
    els.bar.classList.toggle('rec', recording);
    els.bar.classList.toggle('idle', !recording);
    els.dot.classList.toggle('rec', recording);
    els.dot.classList.toggle('idle', !recording);
    els.label.textContent = recording ? S_ON : S_OFF;
    els.toggle.textContent = recording ? L_STOP : L_START;

    // セレクタ入力欄はフォーカス中は書き換えない（タイピングを壊さない）。
    // 非フォーカス時のみ現在値と差があれば同期（別タブでの変更を反映）。
    if (shadow.activeElement !== els.sel && els.sel.value !== selector) els.sel.value = selector;

    // 一致件数フィードバック。0件/不正はすぐ気づけるよう色を変える（warn クラス）。
    // パネルはシャドウ内なので querySelectorAll には紛れ込まない（除外処理は不要）。
    const s2 = (selector || '').trim();
    if (!s2) {
      els.selCount.textContent = '';
      els.selCount.classList.remove('warn');
    } else {
      try {
        const n = document.querySelectorAll(s2).length;
        els.selCount.textContent = '一致 ' + n + '件';
        els.selCount.classList.toggle('warn', n === 0);
      } catch (e) {
        els.selCount.textContent = '無効な指定';
        els.selCount.classList.add('warn');
      }
    }

    // SPA検知トグル: セレクタ未設定なら無効（灰色・押せない）。設定時のみ切替可。
    const active = present && spaOn;
    els.spa.disabled = !present;
    els.spaWrap.classList.toggle('disabled', !present);
    els.spa.classList.toggle('on', active);
    els.spa.classList.toggle('off', !active);
    els.spaText.textContent = active ? 'ON' : 'OFF';
  }

  // 撮影直前: バーを上へスライドさせて画面外へ退避する。退避し切ってから撮れるよう、
  // transition の完了（transitionend）で解決する Promise を返す（page.evaluate が待つ）。
  // 撮影が重なった場合は入れ子カウントで数え、最初の1回だけアニメーションする。
  function captureStart() {
    return new Promise((resolve) => {
      if (!els || !els.bar) { resolve(); return; }
      capDepth++;
      // 前回の「復帰待ち」が残っていれば取り消し、退避状態を維持する（撮影が重なっても
      // 途中でバーが降りてきて次のスクショに写り込まないように）。
      if (barTimer) { clearTimeout(barTimer); barTimer = null; }
      if (capDepth > 1) { resolve(); return; }   // 既に退避済み（別の撮影が進行中）
      const bar = els.bar;
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        bar.removeEventListener('transitionend', finish);
        resolve();
      };
      bar.addEventListener('transitionend', finish);
      setTimeout(finish, 500);   // transitionend が来ない場合の保険
      bar.classList.add('capturing');
    });
  }

  // 撮影直後: 進行中の撮影が無くなったら、まず全画面の赤いシャッターフラッシュを出し
  //（シャッター確定の合図）、少し遅らせてからバーを元位置へスライドで戻す。フラッシュと
  // 復帰の動きを時間差にすることで、隠すときと同じ「上から降りてくる」動きが単独で見える。
  // 撮影が重なっていれば最後の1つで戻す。
  function captureEnd() {
    if (!els || !els.bar) return;
    if (capDepth > 0) capDepth--;
    if (capDepth > 0) return;                     // まだ他の撮影が進行中
    // 1) シャッターフラッシュ（クラスを付け直して毎回アニメを頭から再生）。
    if (els.frame) {
      els.frame.classList.remove('flash');
      void els.frame.offsetWidth;                 // リフローでアニメーションを確実に再スタート
      els.frame.classList.add('flash');
      if (frameTimer) clearTimeout(frameTimer);
      frameTimer = setTimeout(() => {
        if (els && els.frame) els.frame.classList.remove('flash');
        frameTimer = null;
      }, 520);
    }
    // 2) フラッシュの直後にバーを降ろす（動きが競合せず、戻りがはっきり見える）。
    if (barTimer) clearTimeout(barTimer);
    barTimer = setTimeout(() => {
      if (els && els.bar) els.bar.classList.remove('capturing');
      barTimer = null;
    }, 170);
  }

  function build() {
    if (!document.body || document.getElementById(ID)) return;
    // 遷移直後に誤った状態（待機中）を一瞬見せないよう、先に現在の状態
    //（記録中/SPA検知/セレクタ）を Python へ問い合わせ、分かってから描画する。
    const finish = (state) => {
      if (!document.body || document.getElementById(ID)) return;
      host = document.createElement('div');
      host.id = ID;
      host.style.cssText = HOST_CSS;
      shadow = host.attachShadow({ mode: 'open' });
      shadow.innerHTML = TEMPLATE;

      const q = (name) => shadow.querySelector('[data-eac="' + name + '"]');
      els = {
        bar: q('bar'), dot: q('dot'), label: q('label'),
        toggle: q('toggle'), shot: q('shot'),
        sel: q('selector'), selCount: q('sel-count'),
        spaWrap: q('spa-wrap'), spaLabel: q('spa-label'), spa: q('spa'), spaText: q('spa-text'),
        frame: q('frame'),
      };

      // 静的な文言・ホバー説明を入れる。
      els.shot.textContent = L_SHOT;
      els.sel.placeholder = PH_SEL;
      els.sel.title = TITLE_SEL;
      els.spaLabel.textContent = L_SPA;
      // 無効（セレクタ未設定）でも読めるよう、ラッパ／ラベル／ボタンに同じ説明を付ける。
      els.spaLabel.title = TITLE_SPA;
      els.spaWrap.title = TITLE_SPA;
      els.spa.title = TITLE_SPA;

      // 操作はすべて expose_binding 経由で Python へ通知する。
      els.toggle.addEventListener('click', () => { try { window.__eac_toggle(); } catch (e) {} });
      els.shot.addEventListener('click', () => { try { window.__eac_shot(); } catch (e) {} });
      els.spa.addEventListener('click', () => { try { window.__eac_spa_toggle(); } catch (e) {} });
      // 入力のたびにローカルで即座に見た目（SPAボタンの有効/無効・一致件数）を反映しつつ、
      // Python 側へも値を通知する（入力欄はフォーカス中なので apply が上書きしない）。
      els.sel.addEventListener('input', () => {
        apply(recording, spaOn, els.sel.value);
        try { window.__eac_set_selector(els.sel.value); } catch (e) {}
      });
      // 確定時（blur / Enter）に最終値をログへ（入力毎の氾濫を避ける）。
      els.sel.addEventListener('change', () => { try { window.__eac_commit_selector(els.sel.value); } catch (e) {} });

      document.body.appendChild(host);
      // 取得した現在の状態で最初から正しく描画する。
      apply(!!(state && state.recording), !!(state && state.spa), (state && state.selector) || '');
    };
    const fallback = { recording: recording, spa: spaOn, selector: selector };
    if (window.__eac_getstate) {
      window.__eac_getstate().then(finish).catch(() => finish(fallback));
    } else {
      finish(fallback);
    }
  }

  // ---- Python(capture) から page.evaluate 経由で呼ぶページ側ヘルパ ----

  // バー（＝シャドウホスト）を隠す/戻す。本文取得時の一時退避に使う。
  function barDisplay(show) {
    if (host) host.style.setProperty('display', show ? 'block' : 'none', 'important');
  }

  // ページ全文テキスト。innerText はシャドウ内の描画も合成し得るため、
  // 念のためホストを隠してから読み、元へ戻す。
  function bodyText() {
    if (!host) return document.body ? document.body.innerText : '';
    const prev = host.style.getPropertyValue('display');
    const prio = host.style.getPropertyPriority('display');
    host.style.setProperty('display', 'none', 'important');
    const t = document.body ? document.body.innerText : '';
    if (prev) host.style.setProperty('display', prev, prio); else host.style.removeProperty('display');
    return t;
  }

  // SPA検知用のコンテンツ署名。セレクタ一致要素の innerText を連結して短いハッシュにする
  //（全文ではなくハッシュだけ返し、毎 tick の転送量を抑える）。パネルはシャドウ内なので
  // querySelectorAll には紛れ込まず、隠す/戻す処理は不要。
  // セレクタ不正/該当なしは長さ 0 のハッシュ（＝「変化なし」とみなせる固定値）。
  function signature(sel) {
    let text = '';
    try {
      if (sel) {
        const parts = [];
        document.querySelectorAll(sel).forEach((el) => { parts.push(el.innerText || ''); });
        text = parts.join('\n');
      }
    } catch (e) { text = ''; }
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
  window.__eac_captureStart = captureStart;
  window.__eac_captureEnd = captureEnd;
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
