/* ============================================================================
   Alice AI — Agent-mode toggle (HIGH-2 resolution · informed consent).
   Loaded as a normal /static module (CSP script-src 'self'); runs at boot.

   The app ships in the safe chat-only Simple mode. Agent mode — which lets the
   model run code, read/write files, and use tools on the user's computer — is a
   USER-CONTROLLED toggle, DEFAULT OFF, that only turns ON after an explicit
   RISK warning the user confirms. This replaces the old "Advanced requires an
   admin account" gate.

   What this module renders:
     1. An "Agent mode" toggle row in Settings (default OFF).
     2. A clear risk-warning modal (EN + 中, no emoji) shown ONLY when turning
        it ON — [Cancel] / [I understand — turn on Agent mode]. Only an explicit
        confirm enables it.
     3. A persistent "Agent mode on" badge/pill so the user always knows the
        powerful mode is active, with a one-click "turn off".

   The boundary is SERVER-SIDE, not CSS: the toggle calls
     GET  /alice/mode  -> { agent_mode, locked, network_guard_always_on,
                            restart_required_for_full }
     POST /alice/mode  { agent_mode: true, risk_acknowledged: true }
   and the backend persists the flag + reads it on every tool-dispatch / router
   gate. Every /alice/mode fetch is same-origin so it carries the per-launch
   local-token cookie (the always-on network guard). A foreign / DNS-rebound
   page can neither read the token nor flip the flag — and even if it somehow
   flipped it, it still can't drive the tools without the token. So the toggle
   ONLY controls "what you let Alice do", never the external-attack protections.
   NO emoji. EN + 中.
   ============================================================================ */
(function () {
  'use strict';

  var T = function (k) { return (window.AliceI18n ? window.AliceI18n.t(k) : k); };

  var ICONS = {
    bolt:  "<path d='M13 2 4 14h6l-1 8 9-12h-6l1-8z'/>",
    warn:  "<path d='M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z'/><path d='M12 9v4M12 17h.01'/>",
    shield:"<path d='M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6l8-3z'/>",
    x:     "<path d='M18 6 6 18M6 6l12 12'/>",
    check: "<path d='M20 6 9 17l-5-5'/>",
  };
  function ic(inner, size) {
    size = size || 16;
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size +
      '" class="a-ic" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>';
  }

  /* =========================================================================
     window.AliceAgent — the /alice/mode client (token rides the same-origin
     cookie). The server is the single source of truth for the boundary.
     ========================================================================= */
  var _state = { agent_mode: false, locked: false,
                 network_guard_always_on: true, restart_required_for_full: false };
  var _listeners = [];

  function _notify() { _listeners.forEach(function (fn) { try { fn(_state); } catch (_) {} }); }

  function refresh() {
    return fetch('/alice/mode', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d) {
          _state = {
            agent_mode: !!d.agent_mode,
            locked: !!d.locked,
            network_guard_always_on: d.network_guard_always_on !== false,
            restart_required_for_full: !!d.restart_required_for_full,
          };
        }
        _notify();
        return _state;
      })
      .catch(function () { _notify(); return _state; });
  }

  // POST the new state. enabling REQUIRES risk_acknowledged:true (the server
  // also enforces this); disabling never needs it. Returns the server's
  // post-change state (authoritative).
  function setAgentMode(on, riskAck) {
    return fetch('/alice/mode', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_mode: !!on, risk_acknowledged: !!riskAck }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        var d = res.d || {};
        _state = {
          agent_mode: !!d.agent_mode,
          locked: !!d.locked,
          network_guard_always_on: d.network_guard_always_on !== false,
          restart_required_for_full: !!d.restart_required_for_full,
        };
        _notify();
        return res;
      });
  }

  window.AliceAgent = {
    refresh: refresh,
    setAgentMode: setAgentMode,
    state: function () { return _state; },
    onChange: function (fn) { if (typeof fn === 'function') _listeners.push(fn); },
    // exposed so the skin's CSS-reveal can follow the server (HIGH-2): the
    // a-advanced chrome is only revealed when agent_mode is truly on.
    isOn: function () { return !!_state.agent_mode; },
  };

  /* =========================================================================
     1. The risk-warning modal (shown ONLY when turning Agent mode ON).
     ========================================================================= */
  function closeModal() {
    var b = document.getElementById('a-agent-backdrop');
    if (b) b.remove();
    document.removeEventListener('keydown', _escClose);
  }
  function _escClose(e) { if (e.key === 'Escape') closeModal(); }

  function openRiskModal(onConfirmed) {
    closeModal();
    var bd = document.createElement('div');
    bd.className = 'a-agent-backdrop';
    bd.id = 'a-agent-backdrop';
    bd.setAttribute('role', 'dialog');
    bd.setAttribute('aria-modal', 'true');
    bd.setAttribute('aria-labelledby', 'a-agent-risk-title');

    bd.innerHTML =
      '<div class="a-agent-modal">' +
        '<div class="a-agent-modal-head">' +
          '<span class="a-agent-warn-ico">' + ic(ICONS.warn, 22) + '</span>' +
          '<div class="a-agent-modal-htext">' +
            '<div class="a-agent-modal-title" id="a-agent-risk-title">' + T('agent.risk.title') + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="a-agent-modal-body">' + T('agent.risk.body') + '</div>' +
        '<div class="a-agent-modal-note">' +
          '<span class="a-agent-note-ico">' + ic(ICONS.shield, 15) + '</span>' +
          '<span>' + T('agent.risk.note') + '</span>' +
        '</div>' +
        '<div class="a-agent-modal-actions">' +
          '<button type="button" class="a-agent-btn a-agent-btn-ghost" data-agent-cancel>' + T('agent.risk.cancel') + '</button>' +
          '<button type="button" class="a-agent-btn a-agent-btn-danger" data-agent-confirm>' + T('agent.risk.confirm') + '</button>' +
        '</div>' +
      '</div>';

    document.body.appendChild(bd);
    bd.addEventListener('click', function (e) { if (e.target === bd) closeModal(); });
    document.addEventListener('keydown', _escClose);
    bd.querySelector('[data-agent-cancel]').addEventListener('click', closeModal);
    bd.querySelector('[data-agent-confirm]').addEventListener('click', function () {
      var btn = bd.querySelector('[data-agent-confirm]');
      btn.disabled = true;
      // Only an explicit confirm sends risk_acknowledged:true → enables.
      setAgentMode(true, true).then(function (res) {
        closeModal();
        renderAll();
        if (res && res.ok && typeof onConfirmed === 'function') onConfirmed(_state);
      }).catch(function () { btn.disabled = false; });
    });
    // focus the confirm so keyboard users land on the explicit choice
    try { bd.querySelector('[data-agent-confirm]').focus(); } catch (_) {}
  }

  /* =========================================================================
     2. The Settings "Agent mode" toggle row.
     The Simple Settings live in odysseus's settings surface; we inject a small
     Alice-branded row at the top of whatever settings container is open, and
     also expose it in the model-picker footer area as a stable home. To keep it
     robust against odysseus's async DOM we (re)inject on mutation.
     ========================================================================= */
  function toggleRowHTML() {
    var on = _state.agent_mode;
    var sub = T('agent.settings.desc');
    if (_state.locked) sub = T('agent.settings.locked');
    else if (on && _state.restart_required_for_full) sub = T('agent.settings.restart');
    return '' +
      '<div class="a-agent-row-main">' +
        '<span class="a-agent-row-ico">' + ic(ICONS.bolt, 17) + '</span>' +
        '<div class="a-agent-row-text">' +
          '<div class="a-agent-row-title">' + T('agent.settings.title') + '</div>' +
          '<div class="a-agent-row-sub">' + sub + '</div>' +
        '</div>' +
      '</div>' +
      '<button type="button" class="a-agent-switch' + (on ? ' on' : '') + (_state.locked ? ' locked' : '') + '" ' +
        'role="switch" aria-checked="' + (on ? 'true' : 'false') + '" ' +
        (_state.locked ? 'disabled ' : '') +
        'aria-label="' + T('agent.settings.title') + '" data-agent-switch>' +
        '<span class="a-agent-knob"></span>' +
      '</button>';
  }

  function wireRow(row) {
    var sw = row.querySelector('[data-agent-switch]');
    if (!sw) return;
    sw.addEventListener('click', function () {
      if (_state.locked) return;
      if (_state.agent_mode) {
        // turning OFF needs no confirmation (easy reverse)
        setAgentMode(false, false).then(renderAll);
      } else {
        // turning ON → the risk modal; only an explicit confirm enables.
        openRiskModal();
      }
    });
  }

  function injectSettingsRow() {
    // odysseus's Settings modal body is `.settings-modal-content` (panels live
    // under it as [data-settings-panel="…"]). Land the Agent-mode row at the
    // very top of that body so it's the first thing the user sees in Settings.
    // Fall back to the panels/content/modal-content containers across builds.
    if (document.getElementById('a-agent-setting')) return;
    var host = document.querySelector('#settings-modal .settings-modal-content')
      || document.querySelector('.settings-modal-content')
      || document.querySelector('#settings-modal .settings-panels')
      || document.querySelector('#settings-modal .modal-content')
      || document.querySelector('#settings-panel, .settings-content, #settings-content');
    if (!host) return;
    var row = document.createElement('div');
    row.className = 'a-agent-row';
    row.id = 'a-agent-setting';
    row.innerHTML = toggleRowHTML();
    // place it at the very top of Settings so it's the first thing seen
    host.insertBefore(row, host.firstChild);
    wireRow(row);
  }

  /* =========================================================================
     3. The persistent "Agent mode on" badge/pill (always visible while on).
     Lives in the chat top bar next to the privacy pill; clicking it turns
     Agent mode back off (after a lightweight confirm is unnecessary — off is
     the safe direction). The user can ALWAYS see + leave the powerful mode.
     ========================================================================= */
  function removeBadge() {
    var b = document.getElementById('a-agent-badge');
    if (b) b.remove();
  }
  function injectBadge() {
    if (!_state.agent_mode) { removeBadge(); return; }
    var bar = document.querySelector('.chat-top-bar');
    if (!bar) return;
    var existing = document.getElementById('a-agent-badge');
    if (existing) {
      // keep label localized on a language switch
      var lbl = existing.querySelector('.a-agent-badge-lbl');
      if (lbl) lbl.textContent = T('agent.badge');
      existing.title = T('agent.badge.title');
      return;
    }
    var pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'a-agent-badge';
    pill.id = 'a-agent-badge';
    pill.title = T('agent.badge.title');
    pill.innerHTML =
      '<span class="a-agent-badge-dot"></span>' +
      ic(ICONS.bolt, 13) +
      '<span class="a-agent-badge-lbl">' + T('agent.badge') + '</span>' +
      '<span class="a-agent-badge-off">' + ic(ICONS.x, 12) + '</span>';
    pill.addEventListener('click', function () {
      setAgentMode(false, false).then(renderAll);
    });
    bar.appendChild(pill);
  }

  /* =========================================================================
     Render everything from the current server state + keep the skin's CSS
     reveal in sync (so a-advanced chrome only shows when agent_mode is on).
     ========================================================================= */
  function renderAll() {
    injectSettingsRow();
    // refresh the settings row contents if it already exists (state changed)
    var row = document.getElementById('a-agent-setting');
    if (row) { row.innerHTML = toggleRowHTML(); wireRow(row); }
    injectBadge();
    // mirror to the skin's progressive-disclosure reveal (HIGH-2): the skin
    // gates a-advanced on BOTH the server flag AND the local pref; keep its
    // server view fresh +, when the user turns Agent mode on, opt the local
    // reveal in so the advanced chrome appears in the same gesture.
    try {
      if (window.AliceShell) {
        if (typeof window.AliceShell.setServerAgentMode === 'function') {
          window.AliceShell.setServerAgentMode(_state.agent_mode);
        }
        if (_state.agent_mode && typeof window.AliceShell.setAdvanced === 'function') {
          window.AliceShell.setAdvanced(true);
        } else if (!_state.agent_mode && typeof window.AliceShell.setAdvanced === 'function') {
          window.AliceShell.setAdvanced(false);
        }
      }
    } catch (_) {}
  }

  // re-localize open surfaces when the language toggles
  function relocalize() {
    var row = document.getElementById('a-agent-setting');
    if (row) { row.innerHTML = toggleRowHTML(); wireRow(row); }
    var badge = document.getElementById('a-agent-badge');
    if (badge) {
      var lbl = badge.querySelector('.a-agent-badge-lbl');
      if (lbl) lbl.textContent = T('agent.badge');
      badge.title = T('agent.badge.title');
    }
    if (document.getElementById('a-agent-backdrop')) {
      // re-open the modal so its copy follows the language
      openRiskModal();
    }
  }
  window.AliceAgent.relocalize = relocalize;
  window.AliceAgent.openRiskModal = openRiskModal;

  /* =========================================================================
     Boot — fetch the server state, render, and re-assert on odysseus's async
     DOM (same MutationObserver pattern as alice-skin.js / alice-earn.js).
     ========================================================================= */
  function boot() {
    refresh().then(renderAll);
    var mo = new MutationObserver(function () { injectSettingsRow(); injectBadge(); });
    try { mo.observe(document.body, { childList: true, subtree: true }); } catch (_) {}
    // keep watching a bit longer than the skin (settings open on demand)
    setTimeout(function () { try { mo.disconnect(); } catch (_) {} }, 12000);
    // The Settings modal is built lazily and can be opened long after boot
    // (after the observer disconnects). A delegated click re-attempts injection
    // — cheap: injectSettingsRow early-returns if the row exists or no host is
    // present. A short defer lets odysseus build the panel body first.
    document.addEventListener('click', function () {
      setTimeout(function () { injectSettingsRow(); injectBadge(); }, 60);
    }, true);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else { boot(); }
})();
