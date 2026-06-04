/* ============================================================================
   Alice AI — failure-mode UX (M8 · design 01 §failure-modes, F1–F11).

   Loaded as a normal /static module (CSP script-src 'self'), runs at boot.

   This is the single place that turns every real-world failure into a graceful,
   小白-friendly, RECOVERABLE state — never a crash, blank screen, stack trace,
   port number, or jargon. Two responsibilities:

     A. DOWNLOAD failures (F1, F2, F3, F5, F8) — map the backend's machine-
        readable REASON_* code (streamed on the /alice/ensure SSE 'error' event,
        or returned by /alice/load) to a clear title + plain body + ONE obvious
        recovery action (resume / retry / one-tap "Use Alice Lite"). Rendered in
        place inside the first-run download card (the breathing-mark overlay), so
        the 小白 never leaves the flow.

     B. BACKEND health (F6 first-run-not-up, F7 crash-mid-chat) — a lightweight
        heartbeat on /healthz. If the in-process engine dies mid-session (OOM, a
        native-lib fault), the shell restarts it (shell/alice_shell/__main__.py
        watchdog); meanwhile this shows a calm "Reconnecting to Alice…" overlay
        that AUTO-CLEARS the instant /healthz returns — so a backend bounce looks
        like a brief pause, not a dead window. If it never comes back, an honest
        "close and reopen Alice — your chats are saved" card (no stack trace).

   The backend already classifies the failure (downloader.py REASON_* +
   alice_routes ensure 'error' {reason, message}); this file is the honest UI
   contract over those codes. NO emoji. Reason codes are mapped, never shown raw.
   ============================================================================ */
(function () {
  'use strict';

  var T = function (k) { return (window.AliceI18n ? window.AliceI18n.t(k) : k); };

  // ── reason-code → F-state map ────────────────────────────────────────────
  // Backend REASON_* (downloader.py) + the ensure/load 'reason' field. Each
  // resolves to {kind, h, b} where kind drives which recovery action shows.
  //   net    → resume (kept partials, HTTP Range) ......................... F1
  //   sha    → auto re-fetch happening (transient) / sha.fail after retry .. F2
  //   big    → too big for device → one-tap Alice Lite .................... F3
  //   load   → downloaded+verified but load failed → free memory / Lite ... F4
  //   disk   → not enough free space → free space + retry ................ F5
  //   dl     → unknown / generic interruption → retry .................... F8
  var REASON_KIND = {
    // network / interruption (resumable)
    'model_download_failed':      'net',
    'ensure_failed':              'net',
    // checksum / integrity (auto re-fetch first; this is the post-retry fail)
    'model_checksum_mismatch':    'sha',
    'model_file_size_mismatch':   'sha',
    // disk
    'model_disk_space_insufficient': 'disk',
    // gate (too big for the device) — load path
    'model_gate_refused':         'big',
    'model_gate_warn_unconfirmed':'big',
  };

  function _kindFor(reason) {
    if (!reason) return 'dl';
    return REASON_KIND[reason] || 'dl';
  }

  // Pull "X.Y GB" out of the backend's machine message ("need about 4.6 GB,
  // have 2.1 GB") so the UI can show the honest numbers without parsing locale.
  function _gb(message, which) {
    if (!message) return null;
    // disk message: "need about 4.6 GB free, have 2.1 GB"
    var m;
    if (which === 'need') {
      m = /need about ([\d.]+\s?GB)/i.exec(message) || /about ([\d.]+\s?GB)/i.exec(message);
    } else {
      m = /have ([\d.]+\s?GB)/i.exec(message);
    }
    return m ? m[1].replace(/\s+/g, ' ') : null;
  }

  // ── A. download-error renderer (rendered inside the first-run card) ───────
  // ctx: { onResume, onRetry, onUseLite, need, have }
  //   need/have: optional GB strings the caller already knows (gate path);
  //   otherwise parsed from ev.message.
  function renderDownloadError(ev, ctx) {
    ctx = ctx || {};
    var reason = ev && ev.reason;
    var kind = _kindFor(reason);
    var need = ctx.need || _gb(ev && ev.message, 'need');
    var have = ctx.have || _gb(ev && ev.message, 'have');

    var card = document.querySelector('#a-firstrun .fr-card');
    if (!card) return;

    // Stop the breathing/aura animation noise on an error: swap the live ring
    // for a calm static state. Keep the mark (brand), drop the % and live-rate.
    var h, b, actions = [];
    if (kind === 'net') {
      h = T('fail.net.h'); b = T('fail.net.b');
      actions = [_btn('primary', T('fail.resume'), ctx.onResume || ctx.onRetry)];
    } else if (kind === 'sha') {
      // After the backend's one automatic re-fetch already failed.
      h = T('fail.sha.fail.h'); b = T('fail.sha.fail.b');
      actions = [_btn('primary', T('download.retry'), ctx.onRetry)];
    } else if (kind === 'big') {
      h = T('fail.big.h');
      b = _fill(T('fail.big.b'), { need: need || '—', have: have || '—' });
      actions = [_btn('primary', T('fail.tryLite'), ctx.onUseLite),
                 _btn('ghost', T('fail.choose'), ctx.onChoose)];
    } else if (kind === 'disk') {
      h = T('fail.disk.h');
      b = need ? _fill(T('fail.disk.b'), { need: need }) : T('fail.disk.b.plain');
      actions = [_btn('primary', T('download.retry'), ctx.onRetry)];
    } else {
      h = T('fail.dl.h'); b = T('fail.dl.b');
      actions = [_btn('primary', T('download.retry'), ctx.onRetry)];
    }

    _renderErrorCard(card, { h: h, b: b, actions: actions, message: ev && ev.message });
  }

  // F4 — model load failure (downloaded + verified, but the engine couldn't
  // bring it up — usually low free RAM). Offers free-memory retry + Alice Lite.
  function renderLoadError(ctx) {
    ctx = ctx || {};
    var card = document.querySelector('#a-firstrun .fr-card');
    if (!card) {
      // No first-run card on screen (a mid-session switch): toast-style banner.
      _banner(T('fail.load.h'), T('fail.load.b'), [
        _btn('primary', T('download.retry'), ctx.onRetry),
        _btn('ghost', T('fail.tryLite'), ctx.onUseLite),
      ]);
      return;
    }
    _renderErrorCard(card, {
      h: T('fail.load.h'), b: T('fail.load.b'),
      actions: [_btn('primary', T('download.retry'), ctx.onRetry),
                _btn('ghost', T('fail.tryLite'), ctx.onUseLite)],
    });
  }

  function _renderErrorCard(card, o) {
    card.innerHTML =
      '<div class="fr-steps">' + _steps() + '</div>' +
      '<div class="dl-wrap a-fail"><div class="dl-track"></div>' +
        '<div class="dl-ring a-fail-ring" style="--p:1"></div>' +
        '<div class="dl-core"><span class="a-fail-glyph">' + _warnGlyph() + '</span></div></div>' +
      '<div class="fr-h">' + o.h + '</div>' +
      '<div class="fr-sub">' + o.b + '</div>' +
      '<div class="a-fail-actions">' + o.actions.join('') + '</div>' +
      (o.message ? '<a class="fr-ghost a-fail-copy" data-msg="' + _attr(o.message) + '">' + T('err.copy') + '</a>' : '');
    _wire(card);
  }

  function _banner(h, b, actions) {
    var prev = document.getElementById('a-fail-banner');
    if (prev) prev.remove();
    var el = document.createElement('div');
    el.id = 'a-fail-banner';
    el.className = 'a-fail-banner';
    el.innerHTML =
      '<div class="a-fail-banner-ic">' + _warnGlyph() + '</div>' +
      '<div class="a-fail-banner-txt"><div class="h">' + h + '</div><div class="b">' + b + '</div></div>' +
      '<div class="a-fail-banner-act">' + actions.join('') + '</div>' +
      '<button class="a-fail-banner-x" aria-label="close">&times;</button>';
    document.body.appendChild(el);
    el.querySelector('.a-fail-banner-x').addEventListener('click', function () { el.remove(); });
    _wire(el);
  }

  // ── shared bits ───────────────────────────────────────────────────────────
  function _steps() {
    // keep the 3-dot stepper at "download" so the error reads as part of setup
    return '<i class="done"></i><i class="on"></i><i></i>';
  }
  function _warnGlyph() {
    return '<svg viewBox="0 0 24 24" width="40" height="40" class="a-ic">' +
      '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';
  }
  function _btn(variant, label, fn) {
    var cls = variant === 'primary' ? 'fr-cta a-fail-btn' : 'fr-btn-ghost a-fail-btn';
    var id = 'afb' + (_btn._n = (_btn._n || 0) + 1);
    _btn._fns = _btn._fns || {};
    _btn._fns[id] = fn || function () {};
    return '<button class="' + cls + '" data-afb="' + id + '">' + label + '</button>';
  }
  function _wire(root) {
    root.querySelectorAll('[data-afb]').forEach(function (b) {
      var id = b.getAttribute('data-afb');
      b.addEventListener('click', function () {
        var fn = _btn._fns && _btn._fns[id];
        if (typeof fn === 'function') fn();
      });
    });
    root.querySelectorAll('.a-fail-copy').forEach(function (a) {
      a.addEventListener('click', function () {
        var msg = a.getAttribute('data-msg') || '';
        _copy(msg, a);
      });
    });
  }
  function _copy(text, el) {
    var ok = false;
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
      document.body.appendChild(ta); ta.focus(); ta.select();
      ok = document.execCommand && document.execCommand('copy'); ta.remove();
    } catch (_) {}
    if (el) { var o = el.textContent; el.textContent = T('earn.addr.copied') || 'Copied'; setTimeout(function () { el.textContent = o; }, 1400); }
  }
  function _fill(tpl, vars) {
    return String(tpl).replace(/\{(\w+)\}/g, function (_, k) { return vars[k] != null ? vars[k] : ''; });
  }
  function _attr(s) { return String(s).replace(/"/g, '&quot;').replace(/</g, '&lt;'); }

  /* =========================================================================
     B. Backend health heartbeat (F6 first-run-not-up, F7 crash-mid-chat).
     ========================================================================= */
  var HB = {
    timer: null, overlay: null, downSince: 0, wasUp: true, escalated: false,
    everUp: false,   // require ONE good /healthz before we ever show "reconnect"
    // give the shell time to restart the child before we declare it dead.
    GIVE_UP_MS: 45000,
    INTERVAL_MS: 2500,
  };

  function _healthOnce() {
    // Same-origin GET /healthz — open (no token needed), cheap. A network error
    // (ECONNREFUSED while the backend is restarting) rejects the promise.
    return fetch('/healthz', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok; })
      .catch(function () { return false; });
  }

  function _showReconnect() {
    if (HB.overlay) return;
    var ov = document.createElement('div');
    ov.id = 'a-reconnect';
    ov.className = 'a-reconnect';
    ov.innerHTML =
      '<div class="a-reconnect-card">' +
        '<div class="a-reconnect-mark">' + (window.AliceShell && window.AliceShell.markSVG ? window.AliceShell.markSVG(48) : _markFallback(48)) + '</div>' +
        '<div class="a-reconnect-spin"></div>' +
        '<div class="a-reconnect-h">' + T('fail.recon.h') + '</div>' +
        '<div class="a-reconnect-b">' + T('fail.recon.b') + '</div>' +
      '</div>';
    document.body.appendChild(ov);
    HB.overlay = ov;
  }
  function _markFallback(size) {
    // minimal mark if the skin's markSVG isn't exposed
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '"><circle cx="12" cy="12" r="9" fill="none" stroke="#F97316" stroke-width="2"/></svg>';
  }
  function _escalateReconnect() {
    if (!HB.overlay) _showReconnect();
    HB.escalated = true;
    var card = HB.overlay && HB.overlay.querySelector('.a-reconnect-card');
    if (!card) return;
    var spin = card.querySelector('.a-reconnect-spin'); if (spin) spin.style.display = 'none';
    var h = card.querySelector('.a-reconnect-h'); if (h) h.textContent = T('fail.recon.fail.h');
    var b = card.querySelector('.a-reconnect-b'); if (b) b.textContent = T('fail.recon.fail.b');
  }
  function _clearReconnect() {
    if (HB.overlay) { HB.overlay.remove(); HB.overlay = null; }
    HB.escalated = false;
  }

  function _tick() {
    _healthOnce().then(function (up) {
      if (up) {
        HB.everUp = true;
        if (!HB.wasUp) {
          // recovered → clear the overlay; if a fetch/SSE was in flight the
          // user can just retry — odysseus persists the session server-side.
          _clearReconnect();
        }
        HB.wasUp = true; HB.downSince = 0;
        return;
      }
      // down — but NEVER show the reconnect overlay until we've seen the backend
      // up at least once (avoids false positives in the standalone static
      // mockup, where /healthz 404s, and during the first boot race).
      if (!HB.everUp) { HB.wasUp = false; return; }
      var now = Date.now();
      if (HB.wasUp) { HB.wasUp = false; HB.downSince = now; }
      // Only surface the overlay once the app is past first-run — the first-run
      // flow owns the screen and shows its own (download) state.
      if (!document.getElementById('a-firstrun')) {
        if (now - HB.downSince > HB.GIVE_UP_MS) _escalateReconnect();
        else _showReconnect();
      }
    });
  }

  function startHeartbeat() {
    if (HB.timer) return;
    // Only run inside the real shell (the standalone static mockup has no
    // backend). Detect by a quick probe; start polling regardless once seen up.
    HB.timer = setInterval(_tick, HB.INTERVAL_MS);
    // expose a hook so a stream-failure can demand an immediate check
    window.AliceFailure.checkNow = _tick;
  }
  function stopHeartbeat() { if (HB.timer) { clearInterval(HB.timer); HB.timer = null; } }

  function relocalize() {
    if (HB.overlay) {
      var h = HB.overlay.querySelector('.a-reconnect-h');
      var b = HB.overlay.querySelector('.a-reconnect-b');
      // Re-apply the CURRENT state's copy (escalated "needs restart" vs the
      // normal "reconnecting"), so a language toggle mid-failure stays correct.
      if (h) h.textContent = T(HB.escalated ? 'fail.recon.fail.h' : 'fail.recon.h');
      if (b) b.textContent = T(HB.escalated ? 'fail.recon.fail.b' : 'fail.recon.b');
    }
  }

  window.AliceFailure = {
    renderDownloadError: renderDownloadError,
    renderLoadError: renderLoadError,
    startHeartbeat: startHeartbeat,
    stopHeartbeat: stopHeartbeat,
    relocalize: relocalize,
    // exposed for tests / manual triggers
    _kindFor: _kindFor,
    _gb: _gb,
    _showReconnect: _showReconnect,
    _escalateReconnect: _escalateReconnect,
    _clearReconnect: _clearReconnect,
  };

  // Auto-start the heartbeat once the page is interactive (real shell only).
  function _boot() {
    // Start polling; the first successful /healthz proves we're in the shell.
    // (In the static mockup /healthz 404s → wasUp stays false but a-firstrun
    //  guards the overlay, and the mockup has no chat, so nothing shows.)
    startHeartbeat();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot, { once: true });
  } else {
    _boot();
  }
})();
