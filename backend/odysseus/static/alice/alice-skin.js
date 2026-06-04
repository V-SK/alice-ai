/* ============================================================================
   Alice AI — brand skin orchestration (M3).
   Loaded as a normal /static module (CSP script-src 'self'), runs at boot.

   Responsibilities (design 05 §9 + design 02 §5/§6/§9):
     1. FORCE the locked Alice theme into localStorage so odysseus's own
        head-script + theme.js paint Alice from the first frame, and LOCK it
        (a 小白 can't recolour away from brand).
     2. De-odysseus the DOM: title / favicon / manifest / loader / sidebar
        brand / welcome hero / chat-meta / composer placeholder → Alice mark.
     3. Simple/Advanced progressive disclosure (default Simple; gate odysseus's
        agents/tools/RAG/apps chrome behind `html.a-advanced`).
     4. Chat chrome to match the mockup: privacy pill, composer note + hint,
        starter chips, Alice mark on assistant turns.
     5. EN/中 i18n apply + a language toggle (titlebar/rail).
     6. The first-run model-download overlay + the model-picker popover
        (both transcribed from docs/design/mockup.html; the actual download
        wiring is M1/M4 — here they are the SCREENS, demo-progressed).

   NO emoji. Models shown ONLY as Alice / Alice Lite / Alice Pro / Alice RP.
   ============================================================================ */
(function () {
  'use strict';

  /* ---- the canonical Alice mark (the geometric "A", verbatim from the
       Miner brand SVG). `fill` is left to CSS so cores can tint it. ---- */
  var MARK_PATH = 'M5635 7050 c112 -184 252 -416 312 -515 60 -99 224 -369 365 -600 140 -231 373 -616 518 -855 144 -239 273 -452 286 -472 13 -20 193 -317 400 -660 207 -343 428 -707 490 -810 63 -103 114 -189 114 -192 0 -3 -525 -6 -1167 -6 l-1168 0 81 33 c91 37 197 109 262 177 261 272 330 674 181 1055 -43 110 -53 128 -315 550 -181 292 -334 542 -501 820 -201 336 -356 591 -367 602 -8 9 -29 -19 -87 -115 -95 -160 -269 -446 -431 -712 -69 -113 -168 -275 -220 -360 -52 -85 -141 -229 -198 -320 -277 -440 -323 -555 -336 -820 -7 -145 8 -250 57 -384 52 -145 181 -316 309 -412 47 -35 157 -90 197 -99 18 -3 35 -10 37 -13 4 -8 -2325 -5 -2332 3 -2 2 69 125 158 272 89 147 214 354 278 458 63 105 170 280 237 390 115 188 222 364 740 1220 117 193 276 456 355 585 170 280 516 850 833 1375 126 209 267 442 314 517 l85 138 155 -258 c86 -141 247 -408 358 -592z m-505 -1770 c0 -29 49 -159 83 -220 46 -84 123 -184 178 -234 109 -98 254 -173 384 -199 l70 -14 -73 -16 c-280 -63 -541 -316 -631 -612 -7 -22 -15 -47 -18 -55 -3 -8 -11 9 -19 38 -35 134 -126 286 -238 398 -119 118 -250 194 -401 231 l-66 16 88 22 c157 40 260 98 374 210 116 115 208 272 241 414 11 48 28 61 28 21z';
  function markSVG(size) {
    size = size || 24;
    return '<svg viewBox="0 0 1024 1024" width="' + size + '" height="' + size + '" aria-label="Alice">' +
      '<g transform="translate(0,1024) scale(0.1,-0.1)"><path d="' + MARK_PATH + '"/></g></svg>';
  }
  // monoline icon helper (no emoji) — returns inner svg markup
  function ic(inner, size) {
    size = size || 16;
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" class="a-ic">' + inner + '</svg>';
  }
  var ICONS = {
    lock:   "<rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 8 0v3'/>",
    plus:   "<path d='M12 5v14M5 12h14'/>",
    chevD:  "<path d='m6 9 6 6 6-6'/>",
    chevR:  "<path d='m9 6 6 6-6 6'/>",
    check:  "<path d='M20 6 9 17l-5-5'/>",
    arrowR: "<path d='M5 12h14M13 6l6 6-6 6'/>",
    sendUp: "<path d='M12 19V5M5 12l7-7 7 7'/>",
    chip:   "<rect x='6' y='6' width='12' height='12' rx='2'/><path d='M9 2v2M12 2v2M15 2v2M9 20v2M12 20v2M15 20v2M2 9h2M2 12h2M2 15h2M20 9h2M20 12h2M20 15h2'/>",
  };

  /* =========================================================================
     1. FORCE + LOCK the Alice theme (so odysseus paints Alice from the start)
     ========================================================================= */
  var ALICE_THEME = {
    name: 'alice',
    colors: {
      bg: '#050505', fg: '#FAFAFA', panel: '#161618', border: 'rgba(63,63,70,0.55)',
      red: '#F97316',
      advanced: {
        brandColor: '#F97316',
        sectionAccent: '#FDBA74',
        accentPrimary: '#F97316',
        accentError: '#EF4444',
        userBubbleBg: '#1E1E22',
        aiBubbleBg: 'transparent',
        bubbleBorder: 'rgba(63,63,70,0.55)',
        sidebarBg: '#0c0c0e',
        inputBg: '#08080A',
        inputBorder: 'rgba(82,82,91,0.65)',
        sendBtnBg: '#F97316',
        sendBtnHover: '#EA580C',
        codeBg: '#08080A',
        codeFg: '#d7d7db',
        toggleActive: '#F97316',
      },
    },
    font: 'sans',          // Inter UI everywhere (odysseus default was 'mono')
    density: 'comfortable',
    bgPattern: 'none',
  };
  function lockTheme() {
    try {
      // Pin (and overwrite any drifted) theme so brand is the only look.
      localStorage.setItem('odysseus-theme', JSON.stringify(ALICE_THEME));
    } catch (_) {}
  }
  // Run BEFORE odysseus's modules read localStorage on DOMContentLoaded.
  lockTheme();

  var T = function (k) { return (window.AliceI18n ? window.AliceI18n.t(k) : k); };

  /* =========================================================================
     2. De-odysseus the document chrome
     ========================================================================= */
  function deOdysseusHead() {
    try { document.title = 'Alice'; } catch (_) {}
    // favicon = the Alice mark in brand orange (overrides odysseus's boat)
    var fav = "data:image/svg+xml," + encodeURIComponent(
      "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1024 1024'>" +
      "<g transform='translate(0,1024) scale(0.1,-0.1)' fill='#F97316'><path d='" + MARK_PATH + "'/></g></svg>");
    var link = document.querySelector("link[rel='icon']");
    if (!link) { link = document.createElement('link'); link.rel = 'icon'; link.type = 'image/svg+xml'; document.head.appendChild(link); }
    link.href = fav;
    var apple = document.querySelector("link[rel='apple-touch-icon']");
    if (apple) apple.href = fav;
    var mtc = document.querySelector("meta[name='theme-color']");
    if (mtc) mtc.setAttribute('content', '#050505');
  }
  deOdysseusHead();
  // odysseus's per-route favicon script can re-set the boat; reassert shortly.
  setTimeout(deOdysseusHead, 0);

  function brandMark(size) {
    var s = document.createElement('span');
    s.className = 'a-brand-mark';
    s.innerHTML = markSVG(size || 15);
    return s;
  }

  function deOdysseusBody() {
    // sidebar brand: "Odysseus" → mark + "Alice"
    var bt = document.querySelector('.sidebar-brand-title');
    if (bt) {
      bt.textContent = 'Alice';
      var sb = bt.parentElement;
      if (sb && !sb.querySelector('.a-brand-mark')) sb.insertBefore(brandMark(15), bt);
    }
    // a11y h1
    var h1 = document.querySelector('h1.a11y-visually-hidden');
    if (h1) h1.textContent = 'Alice';
    // chat meta header
    var meta = document.getElementById('current-meta');
    if (meta) meta.textContent = T('chrome.meta');
    // composer placeholder
    var msg = document.getElementById('message');
    if (msg) msg.setAttribute('placeholder', T('chat.placeholder'));
    // welcome hero: hide the boat, inject the Alice mark + greeting + trust
    var wn = document.querySelector('.welcome-name');
    if (wn && !wn.querySelector('.a-hero-mark')) {
      // strip any stray odysseus boat + bare "Odysseus" text node so the 小白
      // never sees it, even before/without the source edit.
      wn.querySelectorAll('.welcome-boat').forEach(function (b) { b.remove(); });
      Array.prototype.slice.call(wn.childNodes).forEach(function (n) {
        if (n.nodeType === 3) n.textContent = ''; // text nodes (e.g. "Odysseus")
      });
      var hero = document.createElement('span'); hero.className = 'a-hero-mark'; hero.innerHTML = markSVG(64);
      wn.insertBefore(hero, wn.firstChild);
      var title = document.createElement('span'); title.className = 'a-hero-title'; title.textContent = T('chat.greeting');
      var trust = document.createElement('span'); trust.className = 'a-hero-trust'; trust.textContent = T('chat.trust');
      wn.appendChild(title); wn.appendChild(trust);
    }
    var wsub = document.getElementById('welcome-sub'); if (wsub) wsub.style.display = 'none';
  }

  /* =========================================================================
     4. Chat chrome (privacy pill, composer note + hint, starter chips)
     ========================================================================= */
  function injectPrivacyPill() {
    var bar = document.querySelector('.chat-top-bar');
    if (!bar || bar.querySelector('.a-priv-pill')) return;
    var pill = document.createElement('span');
    pill.className = 'a-priv-pill';
    pill.innerHTML = ic(ICONS.lock, 13) + '<span class="a-priv-txt">' + T('chrome.private') + '</span>';
    bar.appendChild(pill);
  }
  function injectComposerNote() {
    var bar = document.querySelector('.chat-input-bar');
    if (!bar || bar.querySelector('.a-composer-note')) return;
    var note = document.createElement('div');
    note.className = 'a-composer-note';
    note.innerHTML = T('chat.note');
    bar.appendChild(note);
  }
  function injectStarterChips() {
    var ws = document.getElementById('welcome-screen');
    if (!ws || ws.querySelector('.a-chips')) return;
    var keys = ['starter.email', 'starter.explain', 'starter.code', 'starter.plan'];
    var wrap = document.createElement('div'); wrap.className = 'a-chips';
    keys.forEach(function (k) {
      var c = document.createElement('button'); c.type = 'button'; c.className = 'a-chip'; c.textContent = T(k);
      c.addEventListener('click', function () {
        var msg = document.getElementById('message');
        if (msg) { msg.value = c.textContent; msg.focus();
          msg.dispatchEvent(new Event('input', { bubbles: true })); }
      });
      wrap.appendChild(c);
    });
    ws.appendChild(wrap);
  }

  /* =========================================================================
     3. Simple / Advanced progressive disclosure
     ========================================================================= */
  // odysseus chrome (selector) that 小白 never needs → tagged a-adv-only and
  // hidden by CSS unless html.a-advanced. Behavioural code is untouched.
  var ADV_SELECTORS = [
    '#sidebar-search-btn',
    '.incognito-indicator', '.incognito-btn',
    '#export-dropdown-wrap',
    '.chats-manage-btn', '#chats-library-btn',
    '#model-picker-add-models-btn',
    // odysseus app launchers / tool rails, if present in this DOM:
    '.tool-rail', '.app-launcher', '#tools-bar', '.agent-mode-toggle',
    '#research-toggle-btn', '#rag-toggle-btn', '.compare-btn', '#compare-toggle',
  ];
  function tagAdvanced() {
    ADV_SELECTORS.forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (el) { el.classList.add('a-adv-only'); });
    });
  }
  // HIGH-2 (deep-security-audit): the agent/tool surface is a SERVER-SIDE
  // boundary, not a CSS class. The server (GET /alice/mode → {agent_mode}) is
  // authoritative — the dangerous agent/tool/MCP/shell surface is blocked at
  // dispatch + unmounted unless the user has turned ON Agent mode (a
  // risk-acknowledged, server-persisted toggle; see alice-agent.js). So the UI
  // only REVEALS the advanced chrome when BOTH the server confirms Agent mode
  // is on AND the user opted in locally. `?adv=1`/localStorage alone can no
  // longer unlock anything security-relevant — at most it reveals chrome that
  // the server will still refuse, so we gate the reveal on the server too.
  var _serverAdvanced = false;  // until /alice/mode answers, assume Simple
  function _localAdvPref() {
    try { return localStorage.getItem('alice-advanced') === '1'; } catch (_) { return false; }
  }
  function advOn() {
    // Reveal the advanced chrome only when the server says Agent mode is on AND
    // the user opted in locally.
    return _serverAdvanced && _localAdvPref();
  }
  function applyAdvanced() {
    document.documentElement.classList.toggle('a-advanced', advOn());
  }
  function setAdvanced(on) {
    try { localStorage.setItem('alice-advanced', on ? '1' : '0'); } catch (_) {}
    applyAdvanced();
  }
  // Let alice-agent.js push the authoritative server Agent-mode state here when
  // the toggle flips (so the reveal updates in the same gesture, no refetch).
  function setServerAgentMode(on) { _serverAdvanced = !!on; applyAdvanced(); }
  // Ask the server whether Agent mode is actually enabled, then re-apply. Reads
  // the new `agent_mode` field (with `advanced` kept as a back-compat alias).
  function refreshServerMode() {
    try {
      fetch('/alice/mode', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          _serverAdvanced = !!(d && (d.agent_mode || d.advanced));
          applyAdvanced();
        })
        .catch(function () { _serverAdvanced = false; applyAdvanced(); });
    } catch (_) { _serverAdvanced = false; applyAdvanced(); }
  }
  window.AliceShell = window.AliceShell || {};
  window.AliceShell.setAdvanced = setAdvanced;
  window.AliceShell.setServerAgentMode = setServerAgentMode;
  window.AliceShell.advOn = advOn;
  window.AliceShell.markSVG = markSVG;   // reused by the reconnect overlay (M8)
  // exported so a host (or the language toggle) can re-localize live

  /* =========================================================================
     5. Language toggle + (re)apply i18n
     ========================================================================= */
  function applyI18n() {
    // re-run the body text swaps that carry copy
    var meta = document.getElementById('current-meta'); if (meta) meta.textContent = T('chrome.meta');
    var msg = document.getElementById('message'); if (msg) msg.setAttribute('placeholder', T('chat.placeholder'));
    var pillTxt = document.querySelector('.a-priv-pill .a-priv-txt'); if (pillTxt) pillTxt.textContent = T('chrome.private');
    var note = document.querySelector('.a-composer-note'); if (note) note.innerHTML = T('chat.note');
    var title = document.querySelector('.a-hero-title'); if (title) title.textContent = T('chat.greeting');
    var trust = document.querySelector('.a-hero-trust'); if (trust) trust.textContent = T('chat.trust');
    // starter chips
    var chips = document.querySelectorAll('.a-chips .a-chip');
    var ck = ['starter.email', 'starter.explain', 'starter.code', 'starter.plan'];
    chips.forEach(function (c, i) { if (ck[i]) c.textContent = T(ck[i]); });
    // any lang toggle labels
    document.querySelectorAll('.a-lang-toggle').forEach(function (el) {
      if (window.AliceI18n) el.textContent = window.AliceI18n.otherLabel();
    });
    // Earn surface (entry card / sidebar / open panel) re-localizes itself.
    if (window.AliceEarn && window.AliceEarn.relocalize) window.AliceEarn.relocalize();
    // Agent-mode surface (settings row / badge / open risk modal) re-localizes.
    if (window.AliceAgent && window.AliceAgent.relocalize) window.AliceAgent.relocalize();
    // Failure overlays (reconnect) re-localize live too.
    if (window.AliceFailure && window.AliceFailure.relocalize) window.AliceFailure.relocalize();
  }
  function injectLangToggle() {
    // a small EN/中 toggle near the chat top-bar (titlebar is native in the shell)
    var bar = document.querySelector('.chat-top-bar');
    if (!bar || bar.querySelector('.a-lang-toggle')) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'a-lang-toggle a-priv-pill';
    btn.style.cursor = 'pointer';
    btn.textContent = window.AliceI18n ? window.AliceI18n.otherLabel() : 'EN';
    btn.title = 'Language · 语言';
    btn.addEventListener('click', function () {
      if (!window.AliceI18n) return;
      window.AliceI18n.setLang(window.AliceI18n.other());
      applyI18n();
      // refresh the overlays if open
      if (window.AliceOverlays) window.AliceOverlays.refresh();
    });
    bar.appendChild(btn);
  }
  // expose live re-localize (used by the toggle + any host like the harness)
  window.AliceShell.applyI18n = applyI18n;

  /* =========================================================================
     6. Overlays — first-run download + model picker (mockup transcription)
     ========================================================================= */
  function railHTML() {
    return '<div class="a-ov-rail">' +
      '<span class="a-rail-logo">' + markSVG(26) + '</span>' +
      '<span class="grow"></span>' +
      '<span class="lang">' + (window.AliceI18n ? window.AliceI18n.label() : '中') + '</span>' +
      '<span class="ver">v0.1.0</span>' +
    '</div>';
  }
  function titlebarHTML(pillClass, pillDot, pillKey) {
    return '<div class="a-ov-titlebar">' +
      '<span class="a-ov-brand">' + brandMark(15).outerHTML + 'Alice AI</span>' +
      '<span class="a-ov-spacer"></span>' +
      '<span class="a-ov-pill ' + (pillClass || '') + '"><span class="dot ' + (pillDot || '') + '"></span>' + T(pillKey) + '</span>' +
      '<span class="a-ov-lang">' + (window.AliceI18n ? window.AliceI18n.otherLabel() : 'EN') + '</span>' +
    '</div>';
  }

  // --- model picker popover ---
  // M3 demo fallback (used only when the /alice Model Manager API is absent,
  // e.g. the static mockup demo). Real data comes from window.AliceModels (M4).
  var TIER_DOT = { lite: 'var(--tier-lite)', std: 'var(--tier-std)', pro: 'var(--tier-pro)', rp: 'var(--tier-rp)', rp_lite: 'var(--tier-rp)' };
  var DEMO_TIERS = [
    { id: 'lite', display_name: 'Alice Lite', state: 'ready',        active: true,  download_size_human: '2.4 GB', context: { min: 4096, max: 262144, chosen: 8192 } },
    { id: 'std',  display_name: 'Alice',      state: 'downloadable', active: false, download_size_human: '5.0 GB', context: { min: 4096, max: 262144, chosen: 8192 } },
    { id: 'pro',  display_name: 'Alice Pro',  state: 'downloadable', active: false, download_size_human: '29 GB',  context: { min: 4096, max: 131072, chosen: 8192 } },
  ];

  function _fmtCtx(n) {
    if (n >= 1024 && n % 1024 === 0) return (n / 1024) + 'k';
    return String(n);
  }

  // One picker row, driven by a real (or demo) card from the backend.
  function pickerItemHTML(m) {
    var ds = T('model.' + m.id + '.ds') || (m.tagline || '');
    var dot = TIER_DOT[m.id] || 'var(--tier-std)';
    var size = m.download_size_human || '';
    var right, badge = '';
    if (m.state === 'ready') {
      right = '<span class="got">' + ic(ICONS.check, 11) + T('model.ready') + '</span><br>' + size;
    } else if (m.state === 'downloading') {
      right = T('download.live') + '…<br>' + size;
    } else if (m.state === 'locked') {
      right = '<span class="a-lock">' + ic(ICONS.lock, 11) + '</span> ' + size;
    } else {
      right = T('model.download') + '<br>' + size;
    }
    if (m.recommended) badge = ' <span class="badge">' + T('download.recommended') + '</span>';
    var cur = m.active ? ' <span class="cur">' + T('model.current') + '</span>' : '';
    var cls = 'mp-item' + (m.active ? ' on' : '') + (m.state === 'locked' ? ' locked' : '');
    var reason = (m.gate && m.gate.level === 'refuse') ? ' title="' + (m.gate.reason || '') + '"' : '';
    return '<div class="' + cls + '" data-id="' + m.id + '"' + reason + '>' +
      '<span class="tdot" style="background:' + dot + '"></span>' +
      '<span class="mi"><span class="nm">' + m.display_name + cur + badge + '</span><span class="ds">' + ds + '</span></span>' +
      '<span class="sz">' + right + '</span>' +
    '</div>';
  }

  // The per-model context-size control (4k..max), rendered for the active model.
  function contextControlHTML(m) {
    if (!m || !m.context) return '';
    var c = m.context;
    var val = c.chosen || c.default || c.min;
    return '<div class="mp-ctx" data-id="' + m.id + '">' +
      '<div class="mp-ctx-row"><span class="mp-ctx-lbl">' + T('context.label') + '</span>' +
        '<span class="mp-ctx-val">' + _fmtCtx(val) + '</span></div>' +
      '<input type="range" class="mp-ctx-range" min="' + c.min + '" max="' + c.max + '" step="1024" value="' + val + '">' +
      '<div class="mp-ctx-hint">' + T('context.hint') + '</div>' +
    '</div>';
  }

  function _renderPickerBody(mp, data) {
    var models = (data && data.models) || DEMO_TIERS;
    var active = null;
    models.forEach(function (m) { if (m.active) active = m; });
    mp.innerHTML =
      '<div class="mp-h">' + T('picker.h') + '</div>' +
      models.map(pickerItemHTML).join('') +
      contextControlHTML(active) +
      '<div class="mp-foot">' + ic(ICONS.lock, 12) + T('picker.foot') + '</div>';
    _wirePickerRows(mp, models);
    _wireContextControl(mp);
  }

  function _wirePickerRows(mp, models) {
    mp.querySelectorAll('.mp-item:not(.locked)').forEach(function (it) {
      it.addEventListener('click', function () {
        var id = it.getAttribute('data-id');
        var m = null; models.forEach(function (x) { if (x.id === id) m = x; });
        if (!m || m.active) { closePicker(); return; }
        _selectModel(id, m);
      });
    });
  }

  function _wireContextControl(mp) {
    var range = mp.querySelector('.mp-ctx-range');
    if (!range || !window.AliceModels) return;
    var valEl = mp.querySelector('.mp-ctx-val');
    var id = mp.querySelector('.mp-ctx').getAttribute('data-id');
    range.addEventListener('input', function () { if (valEl) valEl.textContent = _fmtCtx(parseInt(range.value, 10)); });
    range.addEventListener('change', function () {
      window.AliceModels.setContext(id, parseInt(range.value, 10)).then(function (res) {
        if (res && res.context_length && valEl) valEl.textContent = _fmtCtx(res.context_length);
        // a tight gate after a big context bump → surface the honest hint
        if (res && res.gate && res.gate.level !== 'ok') {
          var hint = mp.querySelector('.mp-ctx-hint');
          if (hint) hint.textContent = T('context.warn');
        }
      });
    });
  }

  // Switch (ready) or download-then-switch (downloadable). Honest WARN confirm.
  function _selectModel(id, m) {
    if (!window.AliceModels) { closePicker(); return; }
    if (m.state === 'downloadable') {
      // route the download through the first-run-style ring (reused overlay).
      closePicker();
      _downloadThenLoad(id);
      return;
    }
    // ready → load/switch (with a WARN confirm if the gate needs it)
    window.AliceModels.load(id, { confirm: false }).then(function (r) {
      if (r.ok) { closePicker(); _refreshPickerLabel(); return; }
      if (r.status === 409 && r.body) {
        if (r.body.needs_confirm) {
          // honest WARN: ask before loading a model that's tight for the device
          if (window.confirm(T('gate.warn'))) {
            window.AliceModels.load(id, { confirm: true }).then(function () { closePicker(); _refreshPickerLabel(); });
          }
          return;
        }
        if (r.body.blocked) {
          // REFUSE (F3): too big for the device. Show the honest "this model
          // needs X GB; Alice Lite runs great" card with a one-tap fallback,
          // instead of a bare native alert.
          closePicker();
          if (window.AliceFailure) {
            // Reuse the download-error renderer's "big" path inside a first-run
            // card so the 小白 gets the honest "needs X GB → use Alice Lite" UI.
            openFirstRun(); FR_STATE.step = 1; renderFirstRun();
            window.AliceFailure.renderDownloadError(
              { phase: 'error', reason: 'model_gate_refused' },
              {
                have: _deviceMemHint(),
                onUseLite: _fallbackToLite,
                onChoose: function () { closeFirstRun(); openPicker(document.getElementById('model-picker-btn')); },
              });
          } else {
            window.alert(T('gate.refuse'));
          }
          return;
        }
      }
    });
  }

  function _refreshPickerLabel() {
    if (!window.AliceModels) return;
    window.AliceModels.current().then(function (c) {
      var lbl = document.getElementById('model-picker-label');
      if (lbl && c && c.current) lbl.textContent = c.current.display_name;
    });
  }

  function openPicker(anchorEl) {
    closePicker();
    var bd = document.createElement('div'); bd.className = 'a-mp-backdrop'; bd.id = 'a-mp-backdrop';
    var mp = document.createElement('div'); mp.className = 'mp';
    mp.innerHTML = '<div class="mp-h">' + T('picker.h') + '</div><div class="mp-loading">…</div>';
    bd.appendChild(mp);
    document.body.appendChild(bd);
    var r = anchorEl ? anchorEl.getBoundingClientRect() : { left: 70, bottom: 96 };
    var top = Math.min(r.bottom + 8, window.innerHeight - 360);
    var left = Math.min(r.left, window.innerWidth - 352);
    mp.style.top = Math.max(8, top) + 'px';
    mp.style.left = Math.max(8, left) + 'px';
    bd.addEventListener('click', function (e) { if (e.target === bd) closePicker(); });

    // Real data from the M4 Model Manager; fall back to the demo when absent.
    if (window.AliceModels) {
      window.AliceModels.available().then(function (up) {
        if (!up) { _renderPickerBody(mp, { models: DEMO_TIERS }); return; }
        window.AliceModels.list({ rp: advOn() }).then(function (data) { _renderPickerBody(mp, data); })
          .catch(function () { _renderPickerBody(mp, { models: DEMO_TIERS }); });
      });
    } else {
      _renderPickerBody(mp, { models: DEMO_TIERS });
    }
  }
  function closePicker() { var b = document.getElementById('a-mp-backdrop'); if (b) b.remove(); }

  // intercept odysseus's composer model-picker button → show the Alice picker
  function wirePickerButton() {
    var btn = document.getElementById('model-picker-btn');
    if (!btn || btn.dataset.aliceWired) return;
    btn.dataset.aliceWired = '1';
    btn.addEventListener('click', function (e) {
      // only hijack in Simple mode; Advanced uses odysseus's full picker
      if (advOn()) return;
      e.preventDefault(); e.stopImmediatePropagation();
      openPicker(btn);
    }, true);
    // brand the label to the Alice tier name
    var lbl = document.getElementById('model-picker-label');
    if (lbl) lbl.textContent = T('model.std');
  }

  // --- first-run overlay (Welcome → Download → Ready) ---
  var FR_STATE = { step: 0, pct: 62, demoTimer: null };  // step 0=welcome,1=download,2=ready
  function frStepsHTML(active) {
    var dots = '';
    for (var i = 0; i < 3; i++) dots += '<i class="' + (i < active ? 'done' : (i === active ? 'on' : '')) + '"></i>';
    return dots;
  }
  function frCardHTML() {
    if (FR_STATE.step === 0) {
      return '<div class="fr-card">' +
        '<div class="fr-steps">' + frStepsHTML(0) + '</div>' +
        '<div class="dl-wrap"><div class="dl-aura"></div><div class="dl-track"></div>' +
          '<div class="dl-core"><span class="mark">' + markSVG(62) + '</span></div></div>' +
        '<div class="fr-h">' + T('welcome.title') + '</div>' +
        '<div class="fr-sub">' + T('welcome.sub') + '</div>' +
        '<button class="fr-cta" data-fr="start">' + T('welcome.cta') + ' ' + ic(ICONS.arrowR, 16) + '</button>' +
        '<div class="fr-foot">' + T('download.foot') + '</div>' +
      '</div>';
    }
    if (FR_STATE.step === 2) {
      return '<div class="fr-card">' +
        '<div class="fr-steps">' + frStepsHTML(3) + '</div>' +
        '<div class="dl-wrap"><div class="dl-aura"></div><div class="dl-track"></div>' +
          '<div class="dl-ring" style="--p:1"></div>' +
          '<div class="dl-core"><span class="dl-check">' + ic(ICONS.check, 58) + '</span></div></div>' +
        '<div class="fr-h">' + T('ready.title') + '</div>' +
        '<div class="fr-sub">' + T('ready.sub') + '</div>' +
        '<button class="fr-cta" data-fr="chat">' + T('ready.cta') + ' ' + ic(ICONS.arrowR, 16) + '</button>' +
        '<div class="fr-foot">' + T('download.foot') + '</div>' +
      '</div>';
    }
    // step 1 = download
    var p = (FR_STATE.pct / 100).toFixed(2);
    var got = (FR_STATE.pct / 100 * 5.0).toFixed(1);
    return '<div class="fr-card">' +
      '<div class="fr-steps">' + frStepsHTML(1) + '</div>' +
      '<div style="text-align:center;margin-top:8px"><span class="eyebrow">' + T('download.eyebrow') + '</span></div>' +
      '<div class="dl-wrap"><div class="dl-aura"></div><div class="dl-track"></div>' +
        '<div class="dl-ring" style="--p:' + p + '"></div>' +
        '<div class="dl-core"><span class="mark">' + markSVG(62) + '</span></div>' +
        '<div class="dl-pct">' + FR_STATE.pct + '<small>%</small></div></div>' +
      '<div class="fr-h">' + T('download.h') + '</div>' +
      '<div class="fr-sub">' + T('download.sub') + '</div>' +
      '<div class="fr-detect">' +
        '<span class="di">' + ic(ICONS.chip, 20) + '</span>' +
        '<span class="dt"><span class="l">' + T('download.detected') + '</span>' +
          '<span class="v">Apple M2 Max <small>· 32 GB · Metal</small></span></span>' +
        '<span class="pick">' + T('download.change') + ' ' + ic(ICONS.chevD, 12) + '</span>' +
      '</div>' +
      '<div class="fr-modline"><span class="ml"><span class="tdot"></span>' + T('model.std') +
        ' <span class="badge">' + T('download.recommended') + '</span></span>' +
        '<span class="mr">' + got + ' / 5.0 GB</span></div>' +
      '<div class="fr-bar"><i style="width:' + FR_STATE.pct + '%"></i></div>' +
      '<div class="fr-meta"><span class="live"><span class="dot"></span>' + T('download.live') +
        ' · <span class="mono">14.2 MB/s</span></span><span>' + T('download.left') + '</span></div>' +
      '<a class="fr-ghost">' + T('download.choose') + ' ' + ic(ICONS.chevR, 13) + '</a>' +
      '<div class="fr-foot">' + T('download.foot') + '</div>' +
    '</div>';
  }
  function renderFirstRun() {
    var ov = document.getElementById('a-firstrun');
    if (!ov) return;
    var pillKey = FR_STATE.step === 2 ? 'status.ready' : 'status.setup';
    var pillDot = FR_STATE.step === 2 ? 'online' : 'checking';
    var pillClass = FR_STATE.step === 2 ? 'brand' : '';
    ov.innerHTML =
      titlebarHTML(pillClass, pillDot, pillKey) +
      '<div class="a-ov-body">' + railHTML() +
        '<div class="a-ov-content"><div class="fr">' + frCardHTML() + '</div></div>' +
      '</div>';
    // wire CTAs
    ov.querySelectorAll('[data-fr]').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-fr');
        if (act === 'start') { FR_STATE.step = 1; renderFirstRun(); startDownload(); }
        else if (act === 'chat') { closeFirstRun(); }
      });
    });
    ov.querySelector('.a-ov-lang') && ov.querySelector('.a-ov-lang').addEventListener('click', function () {
      if (!window.AliceI18n) return; window.AliceI18n.setLang(window.AliceI18n.other()); renderFirstRun(); applyI18n();
    });
    // On the welcome step, fill the detected-device + recommended model from
    // the real Model Manager (when the /alice API is up).
    if (FR_STATE.step === 0 || FR_STATE.step === 1) _hydrateFirstRunDevice();
  }

  // Pull the real device label + recommended tier into the first-run card.
  function _hydrateFirstRunDevice() {
    if (!window.AliceModels) return;
    window.AliceModels.available().then(function (up) {
      if (!up) return;  // keep the demo placeholder text in the static mockup
      window.AliceModels.recommend().then(function (data) {
        var dev = data.device || {};
        var rec = data.recommended || {};
        FR_STATE.recId = rec.id || 'lite';
        FR_STATE.recSize = rec.download_size_human || '';
        if (dev.memory_gb != null) FR_STATE.deviceMemHint = dev.memory_gb + ' GB';
        var v = document.querySelector('#a-firstrun .fr-detect .v');
        if (v && dev.label) {
          v.innerHTML = dev.label + ' <small>· ' + (dev.memory_gb || '?') + ' GB · ' + (dev.accelerator || '') + '</small>';
        }
        var ml = document.querySelector('#a-firstrun .fr-modline .ml');
        if (ml) ml.innerHTML = '<span class="tdot"></span>' + (rec.display_name || T('model.lite')) +
          ' <span class="badge">' + T('download.recommended') + '</span>';
        var mr = document.querySelector('#a-firstrun .fr-modline .mr');
        if (mr && FR_STATE.recSize) mr.textContent = '0.0 / ' + FR_STATE.recSize;
      }).catch(function () {});
    });
  }

  // Start the REAL download (verified, resumable) of the recommended tier; fall
  // back to the demo ring only when the /alice Model Manager API is absent.
  // On any failure, hand off to the M8 failure-mode renderer (F1/F2/F3/F5/F8)
  // with real recovery callbacks (resume/retry restarts the same flow; "Use
  // Alice Lite" downloads + loads the smallest safe tier instead).
  function startDownload() {
    if (!window.AliceModels) { startDemoProgress(); return; }
    window.AliceModels.available().then(function (up) {
      if (!up) { startDemoProgress(); return; }
      var id = FR_STATE.recId || 'lite';
      window.AliceModels.ensure(id, _onDownloadEvent).then(function (ev) {
        if (ev && ev.phase === 'error') { _showDownloadError(ev); return; }
        // verified → load it, then go to Ready. A load failure (F4) is its own
        // recoverable state (free memory / Alice Lite), distinct from download.
        window.AliceModels.load(id, { confirm: true }).then(function (r) {
          if (r && r.ok === false) { _showLoadError(id, r); return; }
          FR_STATE.step = 2; renderFirstRun(); _refreshPickerLabel();
        }).catch(function () { _showLoadError(id, null); });
      }).catch(function () {
        // The SSE channel itself failed (backend bounce mid-download). Treat as
        // a resumable network interruption, and nudge the health heartbeat.
        if (window.AliceFailure) {
          if (window.AliceFailure.checkNow) window.AliceFailure.checkNow();
          _showDownloadError({ phase: 'error', reason: 'model_download_failed' });
        } else { startDemoProgress(); }
      });
    });
  }

  // Download + load Alice Lite (the one-tap fallback for "too big" / load fail).
  function _fallbackToLite() {
    FR_STATE.recId = 'lite';
    FR_STATE.step = 1; renderFirstRun(); _hydrateFirstRunDevice();
    startDownload();
  }

  // The device's usable memory as a "NN GB" string (for the "too big" card's
  // honest "your computer has X" line). Cached from the last device probe.
  function _deviceMemHint() {
    return FR_STATE.deviceMemHint || null;
  }

  function _onDownloadEvent(ev) {
    // ev: {phase, fraction, downloaded_bytes, total_bytes, rate_bps, ...}
    var pct = Math.round((ev.fraction || 0) * 100);
    var ring = document.querySelector('#a-firstrun .dl-ring');
    var pe = document.querySelector('#a-firstrun .dl-pct');
    var bar = document.querySelector('#a-firstrun .fr-bar i');
    var mr = document.querySelector('#a-firstrun .fr-modline .mr');
    var meta = document.querySelector('#a-firstrun .fr-meta .live .mono');
    if (ring) ring.style.setProperty('--p', (pct / 100).toFixed(2));
    if (pe) pe.innerHTML = pct + '<small>%</small>';
    if (bar) bar.style.width = pct + '%';
    if (mr && ev.total_bytes) {
      var gb = (ev.downloaded_bytes / 1e9).toFixed(1), tot = (ev.total_bytes / 1e9).toFixed(1);
      mr.textContent = gb + ' / ' + tot + ' GB';
    }
    if (meta && ev.rate_bps) meta.textContent = (ev.rate_bps / 1e6).toFixed(1) + ' MB/s';
    // verifying / publishing phases: keep the ring full, swap the sub-line
    if (ev.phase === 'verifying') {
      var sub = document.querySelector('#a-firstrun .fr-h');
      if (sub) sub.textContent = T('download.verifying');
    }
  }

  // Map the backend's REASON_* (on the ensure SSE 'error' event) to a clear,
  // recoverable F-state (M8). Falls back to the inline paused-line if the
  // failure module isn't present (e.g. a stripped standalone mockup).
  function _showDownloadError(ev) {
    if (window.AliceFailure && document.querySelector('#a-firstrun .fr-card')) {
      var id = FR_STATE.recId || 'lite';
      window.AliceFailure.renderDownloadError(ev, {
        onResume: function () { FR_STATE.step = 1; renderFirstRun(); startDownload(); },
        onRetry:  function () { FR_STATE.step = 1; renderFirstRun(); startDownload(); },
        onUseLite: _fallbackToLite,
        onChoose: function () { closeFirstRun(); openPicker(document.getElementById('model-picker-btn')); },
      });
      return;
    }
    var h = document.querySelector('#a-firstrun .fr-h');
    var sub = document.querySelector('#a-firstrun .fr-sub');
    if (h) h.textContent = T('download.paused');
    if (sub) sub.textContent = (ev && ev.message) ? ev.message : T('err.generic');
  }

  // F4 — model load failure (downloaded + verified, engine couldn't bring it up).
  function _showLoadError(id, r) {
    if (window.AliceFailure) {
      window.AliceFailure.renderLoadError({
        onRetry: function () {
          window.AliceModels.load(id, { confirm: true }).then(function (rr) {
            if (rr && rr.ok === false) { _showLoadError(id, rr); return; }
            FR_STATE.step = 2; renderFirstRun(); _refreshPickerLabel();
          }).catch(function () { _showLoadError(id, null); });
        },
        onUseLite: _fallbackToLite,
      });
      return;
    }
    _showDownloadError({ phase: 'error', reason: 'ensure_failed', message: r && r.body && r.body.error });
  }

  // Picker → download a not-yet-installed tier (reuses the first-run ring), then
  // load it as the active model.
  function _downloadThenLoad(id) {
    FR_STATE.recId = id;
    openFirstRun();
    FR_STATE.step = 1; renderFirstRun();
    startDownload();
  }

  function startDemoProgress() {
    // Demo ring (mockup parity) — used ONLY when the /alice API is absent
    // (the standalone static mockup). Real downloads use startDownload().
    if (FR_STATE.demoTimer) return;
    FR_STATE.pct = 8;
    FR_STATE.demoTimer = setInterval(function () {
      FR_STATE.pct += Math.max(1, Math.round((100 - FR_STATE.pct) * 0.06));
      if (FR_STATE.pct >= 100) {
        FR_STATE.pct = 100; clearInterval(FR_STATE.demoTimer); FR_STATE.demoTimer = null;
        FR_STATE.step = 2; renderFirstRun(); return;
      }
      var ring = document.querySelector('#a-firstrun .dl-ring');
      var pct = document.querySelector('#a-firstrun .dl-pct');
      var bar = document.querySelector('#a-firstrun .fr-bar i');
      var mr = document.querySelector('#a-firstrun .fr-modline .mr');
      if (ring) ring.style.setProperty('--p', (FR_STATE.pct / 100).toFixed(2));
      if (pct) pct.innerHTML = FR_STATE.pct + '<small>%</small>';
      if (bar) bar.style.width = FR_STATE.pct + '%';
      if (mr) mr.textContent = (FR_STATE.pct / 100 * 5.0).toFixed(1) + ' / 5.0 GB';
    }, 600);
  }
  function openFirstRun() {
    if (document.getElementById('a-firstrun')) return;
    var ov = document.createElement('div'); ov.className = 'a-overlay'; ov.id = 'a-firstrun';
    document.body.appendChild(ov);
    FR_STATE.step = 0; renderFirstRun();
  }
  function closeFirstRun() {
    if (FR_STATE.demoTimer) { clearInterval(FR_STATE.demoTimer); FR_STATE.demoTimer = null; }
    var ov = document.getElementById('a-firstrun'); if (ov) ov.remove();
    try { localStorage.setItem('alice-firstrun-done', '1'); } catch (_) {}
  }

  window.AliceOverlays = {
    openFirstRun: openFirstRun, closeFirstRun: closeFirstRun,
    openPicker: openPicker, closePicker: closePicker,
    refresh: function () { if (document.getElementById('a-firstrun')) renderFirstRun(); },
  };

  /* =========================================================================
     Boot
     ========================================================================= */
  function boot() {
    if (window.AliceI18n) window.AliceI18n.setLang(window.AliceI18n.lang()); // sets <html lang>
    // ?adv=1/0 only records the LOCAL preference (cosmetic reveal). It can NOT
    // unlock the agent/tool surface — that is gated server-side (HIGH-2). The
    // reveal still requires the server to confirm Advanced via /alice/mode.
    var _qa = new URLSearchParams(location.search).get('adv');
    if (_qa === '1' || _qa === '0') { try { localStorage.setItem('alice-advanced', _qa); } catch (_) {} }
    deOdysseusBody();
    applyAdvanced();
    refreshServerMode();   // authoritative Simple/Advanced from the server
    tagAdvanced();
    injectPrivacyPill();
    injectComposerNote();
    injectStarterChips();
    injectLangToggle();
    wirePickerButton();
    applyI18n();

    // First-run: show the download SCREEN when the model isn't set up.
    // (Demo/standalone control: ?firstrun=1 forces it; ?firstrun=0 skips.
    //  ?frstep=N jumps to step N (0=welcome,1=download,2=ready) — for verify
    //  screenshots; ?picker=1 opens the model picker.)
    var qp = new URLSearchParams(location.search);
    var force = qp.get('firstrun');
    var frstep = qp.get('frstep');
    var done = false;
    try { done = localStorage.getItem('alice-firstrun-done') === '1'; } catch (_) {}
    if (frstep != null) {
      openFirstRun();
      FR_STATE.step = parseInt(frstep, 10) || 0;
      if (FR_STATE.step === 1) FR_STATE.pct = 62; // mockup parity (no live timer)
      renderFirstRun();
    } else if (force === '1' || (force !== '0' && !done)) {
      openFirstRun();
    }
    if (qp.get('picker') === '1') {
      setAdvanced(false);
      openPicker(document.getElementById('model-picker-btn'));
    }

    // Re-apply skin against odysseus's async DOM (it builds the chat lazily).
    var mo = new MutationObserver(function () {
      deOdysseusBody(); tagAdvanced(); injectPrivacyPill(); injectComposerNote();
      injectStarterChips(); injectLangToggle(); wirePickerButton();
    });
    try { mo.observe(document.body, { childList: true, subtree: true }); } catch (_) {}
    // stop watching after the app settles (keep it cheap)
    setTimeout(function () { try { mo.disconnect(); } catch (_) {} }, 8000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
