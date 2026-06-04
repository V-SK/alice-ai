/* ============================================================================
   Alice AI — Model Manager client (M4).
   Loaded as a normal /static module (CSP script-src 'self').

   Bridges the M3 picker + first-run overlays to the real /alice/* backend
   (design 03 §9): list/recommend/gate/ensure(SSE)/load/context. Replaces the
   M3 demo data + demo progress with real catalog + a real, verified download.

   Display rule (hard): the backend already returns Alice-only names + an
   `id` key (lite/std/pro/rp/rp_lite) + a guarded `gate`/`context`; this file
   NEVER constructs a model name from anything but the served `display_name`.
   States: ready | downloadable | downloading | locked. NO emoji.

   Exposes window.AliceModels:
     available()                  -> Promise<bool>   (is the /alice API up?)
     list({rp})                   -> Promise<{models, device}>
     recommend()                  -> Promise<{recommended, device}>
     setContext(id, n)            -> Promise<obj>
     load(id, {confirm, context}) -> Promise<{ok, status, body}>
     ensure(id, onEvent)          -> Promise<obj>     (SSE progress -> onEvent)
   ============================================================================ */
(function () {
  'use strict';

  var BASE = '/alice';

  function _json(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
    });
  }

  var _availP = null;
  function available() {
    if (_availP) return _availP;
    _availP = fetch(BASE + '/device').then(function (r) { return r.ok; }).catch(function () { return false; });
    return _availP;
  }

  function list(opts) {
    opts = opts || {};
    var q = opts.rp ? '?rp=1' : '';
    return fetch(BASE + '/models' + q).then(function (r) { return r.json(); });
  }

  function recommend() {
    return fetch(BASE + '/recommend').then(function (r) { return r.json(); });
  }

  function current() {
    return fetch(BASE + '/current').then(function (r) { return r.json(); });
  }

  function setContext(id, n) {
    return _json(BASE + '/context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, context_length: n }),
    }).then(function (r) { return r.body; });
  }

  function load(id, opts) {
    opts = opts || {};
    var payload = { id: id, confirm: !!opts.confirm };
    if (opts.context != null) payload.context_length = opts.context;
    return _json(BASE + '/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }

  // Download + verify with SSE progress. onEvent({phase, fraction, ...}).
  function ensure(id, onEvent) {
    return new Promise(function (resolve, reject) {
      fetch(BASE + '/ensure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id }),
      }).then(function (resp) {
        if (!resp.ok || !resp.body) { reject(new Error('ensure failed: ' + resp.status)); return; }
        var reader = resp.body.getReader();
        var dec = new TextDecoder();
        var buf = '';
        var last = null;
        function pump() {
          return reader.read().then(function (res) {
            if (res.done) { resolve(last || { phase: 'done' }); return; }
            buf += dec.decode(res.value, { stream: true });
            var lines = buf.split('\n');
            buf = lines.pop();
            lines.forEach(function (line) {
              line = line.trim();
              if (!line.indexOf('data: ')) {
                var data = line.slice(6);
                if (data === '[DONE]') return;
                try {
                  var ev = JSON.parse(data);
                  last = ev;
                  if (onEvent) onEvent(ev);
                } catch (_) {}
              }
            });
            return pump();
          });
        }
        return pump();
      }).catch(reject);
    });
  }

  window.AliceModels = {
    available: available,
    list: list,
    recommend: recommend,
    current: current,
    setContext: setContext,
    load: load,
    ensure: ensure,
  };
})();
