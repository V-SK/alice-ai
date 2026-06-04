/* ============================================================================
   Alice AI — Earn ALICE bridge (M7, design 04).
   Loaded as a normal /static module (CSP script-src 'self'); runs at boot.

   Three honest blocks (design 04 §0), all credit-only — NO `$`, NO rate, NO
   "profit":
     1. Earn with the Alice Miner — detect (per-OS, server-side) the already-
        built Miner, then "Open Alice Miner" (fixed safe launch via the
        token-guarded POST /alice/earn/open-miner) or "Get the Alice Miner"
        (open the download page). Shows the READ-ONLY ~/.alice address.
     2. The Alice story — why Alice has its own models + the network vision.
     3. Contribute your GPU (phase-2) — a visibly DISABLED "coming soon" teaser,
        inert behind ALICE_AI_GPU_EARN_ENABLED (default off, foundation-gated).

   Surfaces:
     - a compact ENTRY card in the chat empty-state (#welcome-screen),
     - a "Earn ALICE" sidebar entry that opens the full Earn panel (overlay).

   Security: every /alice/earn/* fetch is same-origin so it carries the
   per-launch local-token cookie (the Simple-mode guard). open-miner sends NO
   body — the server launches only the KNOWN Miner path. ~/.alice stays
   read-only (we only GET status). NO emoji. EN + 中.
   ============================================================================ */
(function () {
  'use strict';

  var BASE = '/alice/earn';
  var T = function (k) { return (window.AliceI18n ? window.AliceI18n.t(k) : k); };

  /* ---- the Alice mark (verbatim from the brand SVG; fill left to CSS) ---- */
  var MARK_PATH = 'M5635 7050 c112 -184 252 -416 312 -515 60 -99 224 -369 365 -600 140 -231 373 -616 518 -855 144 -239 273 -452 286 -472 13 -20 193 -317 400 -660 207 -343 428 -707 490 -810 63 -103 114 -189 114 -192 0 -3 -525 -6 -1167 -6 l-1168 0 81 33 c91 37 197 109 262 177 261 272 330 674 181 1055 -43 110 -53 128 -315 550 -181 292 -334 542 -501 820 -201 336 -356 591 -367 602 -8 9 -29 -19 -87 -115 -95 -160 -269 -446 -431 -712 -69 -113 -168 -275 -220 -360 -52 -85 -141 -229 -198 -320 -277 -440 -323 -555 -336 -820 -7 -145 8 -250 57 -384 52 -145 181 -316 309 -412 47 -35 157 -90 197 -99 18 -3 35 -10 37 -13 4 -8 -2325 -5 -2332 3 -2 2 69 125 158 272 89 147 214 354 278 458 63 105 170 280 237 390 115 188 222 364 740 1220 117 193 276 456 355 585 170 280 516 850 833 1375 126 209 267 442 314 517 l85 138 155 -258 c86 -141 247 -408 358 -592z m-505 -1770 c0 -29 49 -159 83 -220 46 -84 123 -184 178 -234 109 -98 254 -173 384 -199 l70 -14 -73 -16 c-280 -63 -541 -316 -631 -612 -7 -22 -15 -47 -18 -55 -3 -8 -11 9 -19 38 -35 134 -126 286 -238 398 -119 118 -250 194 -401 231 l-66 16 88 22 c157 40 260 98 374 210 116 115 208 272 241 414 11 48 28 61 28 21z';
  function markSVG(size) {
    size = size || 24;
    return '<svg viewBox="0 0 1024 1024" width="' + size + '" height="' + size + '" aria-label="Alice">' +
      '<g transform="translate(0,1024) scale(0.1,-0.1)"><path d="' + MARK_PATH + '"/></g></svg>';
  }
  var ICONS = {
    pickaxe: "<path d='M14 7l3-3M9 9l6 6M5 21l4-9 1.5 1.5M21 5c-3 0-5-2-5-2M21 5c0 3-2 5-2 5'/>",
    arrowR:  "<path d='M5 12h14M13 6l6 6-6 6'/>",
    download:"<path d='M12 3v12M7 11l5 5 5-5M5 21h14'/>",
    copy:    "<rect x='9' y='9' width='12' height='12' rx='2'/><path d='M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1'/>",
    check:   "<path d='M20 6 9 17l-5-5'/>",
    chip:    "<rect x='6' y='6' width='12' height='12' rx='2'/><path d='M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2'/>",
    gpu:     "<rect x='2' y='6' width='18' height='12' rx='2'/><circle cx='8' cy='12' r='2.4'/><circle cx='14' cy='12' r='2.4'/><path d='M20 9h2v6h-2'/>",
    lock:    "<rect x='5' y='11' width='14' height='9' rx='2'/><path d='M8 11V8a4 4 0 0 1 8 0v3'/>",
    ext:     "<path d='M14 4h6v6M20 4l-9 9M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6'/>",
  };
  function ic(inner, size) {
    size = size || 16;
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" class="a-ic" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>';
  }

  /* =========================================================================
     window.AliceEarn — the /alice/earn/* client (token rides the cookie).
     ========================================================================= */
  var _statusP = null;
  function status(force) {
    if (_statusP && !force) return _statusP;
    _statusP = fetch(BASE + '/status', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
    return _statusP;
  }
  function openMiner() {
    // POST with NO body — the server launches only the KNOWN Miner path.
    return fetch(BASE + '/open-miner', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
      .catch(function () { return { ok: false, body: null }; });
  }
  function downloadUrl() {
    return fetch(BASE + '/download-url', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { return d && d.url; })
      .catch(function () { return null; });
  }
  function openDownloadPage() {
    downloadUrl().then(function (url) { if (url) window.open(url, '_blank', 'noopener'); });
  }
  window.AliceEarn = { status: status, openMiner: openMiner, downloadUrl: downloadUrl, openDownloadPage: openDownloadPage };

  /* =========================================================================
     Shared bits: reward-address row + launch/get buttons.
     ========================================================================= */
  function addrRowHTML(identity) {
    if (!identity || !identity.address) {
      return '<div class="a-earn-addr none">' + ic(ICONS.lock, 13) +
        '<span>' + T('earn.addr.none') + '</span></div>';
    }
    var watch = identity.watch_only ? ' <span class="a-earn-watch">' + T('earn.addr.watch') + '</span>' : '';
    var label = identity.label ? ' <span class="a-earn-lbl">' + escapeHTML(identity.label) + '</span>' : '';
    return '<div class="a-earn-addr">' +
      '<span class="l">' + T('earn.addr.label') + '</span>' +
      '<span class="v mono" title="' + escapeHTML(identity.address) + '">' + escapeHTML(identity.address_display || identity.address) + label + watch + '</span>' +
      '<button type="button" class="a-earn-copy" data-addr="' + escapeHTML(identity.address) + '" title="' + T('earn.addr.copy') + '">' + ic(ICONS.copy, 13) + '</button>' +
    '</div>';
  }
  function wireCopy(root) {
    root.querySelectorAll('.a-earn-copy').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var addr = btn.getAttribute('data-addr') || '';
        var done = function () {
          var prev = btn.innerHTML;
          btn.innerHTML = ic(ICONS.check, 13) + '<span class="a-earn-copied">' + T('earn.addr.copied') + '</span>';
          setTimeout(function () { btn.innerHTML = prev; }, 1400);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(addr).then(done, done);
        } else { done(); }
      });
    });
  }

  // Primary CTA depends on whether the Miner is installed.
  function ctaHTML(st) {
    var installed = st && st.miner && st.miner.installed;
    if (installed) {
      return '<button type="button" class="a-earn-cta" data-earn="open">' +
        ic(ICONS.pickaxe, 16) + '<span>' + T('earn.entry.open') + '</span></button>';
    }
    return '<button type="button" class="a-earn-cta" data-earn="get">' +
      ic(ICONS.download, 16) + '<span>' + T('earn.entry.get') + '</span></button>';
  }
  function headlineFor(state) {
    if (state === 'MINER_INSTALLED_WITH_IDENTITY') return T('earn.s.ready.head');
    if (state === 'MINER_INSTALLED_NO_IDENTITY')   return T('earn.s.set.head');
    if (state === 'MINER_NOT_INSTALLED_WITH_IDENTITY') return T('earn.s.haveaddr.head');
    return T('earn.entry.title');
  }

  function wireActions(root, st) {
    root.querySelectorAll('[data-earn]').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-earn');
        if (act === 'get') { openDownloadPage(); return; }
        if (act === 'learn') { openPanel(); return; }
        if (act === 'open') {
          setFeedback(root, '');
          openMiner().then(function (r) {
            if (r.ok && r.body && r.body.launched) {
              setFeedback(root, T('earn.open.ok'), 'ok');
            } else {
              setFeedback(root, T('earn.open.fail'), 'warn');
              // honest fallback: offer the download page link
              if (r.body && r.body.fallback_url) {
                var fb = root.querySelector('.a-earn-fb');
                if (fb) { fb.style.display = ''; fb.onclick = function () { window.open(r.body.fallback_url, '_blank', 'noopener'); }; }
              }
            }
          });
        }
      });
    });
  }
  function setFeedback(root, msg, kind) {
    var el = root.querySelector('.a-earn-feedback');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'a-earn-feedback' + (kind ? ' ' + kind : '');
  }

  /* =========================================================================
     Block 1 surface: the compact ENTRY card (chat empty-state).
     ========================================================================= */
  function entryCardHTML(st) {
    var state = (st && st.state) || 'MINER_NOT_INSTALLED_NO_IDENTITY';
    var identity = st && st.identity;
    return '' +
      '<div class="a-earn-card" role="group" aria-label="Earn ALICE">' +
        '<div class="a-earn-head">' +
          '<span class="a-earn-mark">' + markSVG(20) + '</span>' +
          '<div class="a-earn-htxt">' +
            '<div class="a-earn-title">' + headlineFor(state) + '</div>' +
            '<div class="a-earn-sub">' + T('earn.entry.sub') + '</div>' +
          '</div>' +
        '</div>' +
        addrRowHTML(identity) +
        '<div class="a-earn-actions">' +
          ctaHTML(st) +
          '<button type="button" class="a-earn-ghost" data-earn="learn">' + T('earn.entry.learn') + ' ' + ic(ICONS.arrowR, 14) + '</button>' +
        '</div>' +
        '<div class="a-earn-feedback" aria-live="polite"></div>' +
        '<a class="a-earn-fb" style="display:none">' + T('earn.entry.get') + ' ' + ic(ICONS.ext, 12) + '</a>' +
        '<div class="a-earn-foot">' + T('earn.foot') + '</div>' +
      '</div>';
  }

  function injectEntryCard() {
    var ws = document.getElementById('welcome-screen');
    if (!ws || ws.querySelector('.a-earn-entry')) return;
    var wrap = document.createElement('div');
    wrap.className = 'a-earn-entry';
    ws.appendChild(wrap);
    status().then(function (st) {
      if (!st) { wrap.remove(); return; }  // /alice/earn absent → no card (fail-soft)
      wrap.innerHTML = entryCardHTML(st);
      wireCopy(wrap); wireActions(wrap, st);
    });
  }

  /* =========================================================================
     Block 2 + 3: the full Earn panel (overlay) — story + GPU teaser.
     ========================================================================= */
  function storyHTML() {
    function sec(n) {
      return '<div class="a-story-sec">' +
        '<div class="a-story-h">' + T('story.' + n + '.h') + '</div>' +
        '<div class="a-story-b">' + T('story.' + n + '.b') + '</div>' +
      '</div>';
    }
    return '<div class="a-story">' +
      '<div class="a-story-eyebrow">' + ic(ICONS.chip, 13) + T('story.eyebrow') + '</div>' +
      sec(1) + sec(2) + sec(3) +
    '</div>';
  }
  function gpuTeaserHTML(st) {
    var g = (st && st.gpu_earn) || {};
    var zh = (window.AliceI18n && window.AliceI18n.lang() === 'zh');
    var reason = zh ? (g.reason_zh || '') : (g.reason_en || '');
    return '<div class="a-gpu-card disabled" aria-disabled="true">' +
      '<div class="a-gpu-head">' +
        '<span class="a-gpu-ico">' + ic(ICONS.gpu, 18) + '</span>' +
        '<span class="a-gpu-title">' + T('gpu.title') + '</span>' +
        '<span class="a-gpu-soon">' + T('gpu.soon') + '</span>' +
      '</div>' +
      '<div class="a-gpu-body">' + T('gpu.body') + '</div>' +
      (reason ? '<div class="a-gpu-reason">' + ic(ICONS.lock, 12) + '<span>' + escapeHTML(reason) + '</span></div>' : '') +
      '<div class="a-gpu-priv">' + T('gpu.priv') + '</div>' +
      '<button type="button" class="a-gpu-btn" disabled>' + T('gpu.soon') + '</button>' +
    '</div>';
  }

  function panelHTML(st) {
    return '' +
      '<div class="a-earn-panel-head">' +
        '<span class="a-earn-mark big">' + markSVG(30) + '</span>' +
        '<div><div class="a-earn-panel-title">' + T('earn.nav') + '</div>' +
          '<div class="a-earn-panel-sub">' + T('earn.entry.sub') + '</div></div>' +
        '<button type="button" class="a-earn-x" data-earn-close aria-label="Close">' +
          '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
        '</button>' +
      '</div>' +
      '<div class="a-earn-panel-scroll">' +
        '<div class="a-earn-block">' + entryCardHTML(st) + '</div>' +
        storyHTML() +
        gpuTeaserHTML(st) +
      '</div>';
  }

  function openPanel() {
    closePanel();
    var bd = document.createElement('div'); bd.className = 'a-earn-backdrop'; bd.id = 'a-earn-backdrop';
    var panel = document.createElement('div'); panel.className = 'a-earn-panel';
    panel.innerHTML = '<div class="a-earn-loading">…</div>';
    bd.appendChild(panel);
    document.body.appendChild(bd);
    bd.addEventListener('click', function (e) { if (e.target === bd) closePanel(); });
    status(true).then(function (st) {
      if (!st) { closePanel(); return; }
      panel.innerHTML = panelHTML(st);
      wireCopy(panel); wireActions(panel, st);
      panel.querySelectorAll('[data-earn-close]').forEach(function (b) {
        b.addEventListener('click', closePanel);
      });
    });
  }
  function closePanel() { var b = document.getElementById('a-earn-backdrop'); if (b) b.remove(); }

  /* =========================================================================
     Sidebar "Earn ALICE" entry.
     ========================================================================= */
  function injectSidebarEntry() {
    var nc = document.getElementById('sidebar-new-chat-btn');
    if (!nc || !nc.parentElement) return;
    if (document.getElementById('a-earn-nav')) return;
    var item = document.createElement('div');
    item.className = 'list-item a-earn-nav';
    item.id = 'a-earn-nav';
    item.title = T('earn.nav');
    item.innerHTML = '<span class="a-earn-nav-ico">' + ic(ICONS.pickaxe, 17) + '</span>' +
      '<span class="a-earn-nav-lbl">' + T('earn.nav') + '</span>';
    item.addEventListener('click', openPanel);
    // place it just under "New chat"
    nc.parentElement.insertBefore(item, nc.nextSibling);
  }

  /* ---- tiny HTML escaper for the few user-controlled strings (addr/label) -- */
  function escapeHTML(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  /* ---- re-localize open surfaces when the language toggles ---- */
  function relocalize() {
    var entry = document.querySelector('#welcome-screen .a-earn-entry');
    if (entry) { entry.innerHTML = ''; entry.classList.remove('a-earn-entry'); // force re-inject fresh
      entry.className = 'a-earn-entry'; status(true).then(function (st) { if (!st) return; entry.innerHTML = entryCardHTML(st); wireCopy(entry); wireActions(entry, st); }); }
    var navLbl = document.querySelector('#a-earn-nav .a-earn-nav-lbl'); if (navLbl) navLbl.textContent = T('earn.nav');
    if (document.getElementById('a-earn-backdrop')) openPanel();
  }
  window.AliceEarn.openPanel = openPanel;
  window.AliceEarn.relocalize = relocalize;

  /* =========================================================================
     Boot — inject the entry card + sidebar entry; re-assert on odysseus's
     async DOM (same pattern as alice-skin.js).
     ========================================================================= */
  function boot() {
    injectEntryCard();
    injectSidebarEntry();
    var mo = new MutationObserver(function () { injectEntryCard(); injectSidebarEntry(); });
    try { mo.observe(document.body, { childList: true, subtree: true }); } catch (_) {}
    setTimeout(function () { try { mo.disconnect(); } catch (_) {} }, 8000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else { boot(); }
})();
