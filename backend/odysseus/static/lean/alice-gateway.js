/* ===========================================================================
   Alice — gateway honest-render helpers (classic script, ZERO imports).

   This file holds the PURE, side-effect-free logic the lean app uses to render
   the remote Alice gateway's /v1/models tier status and chat verification
   receipts HONESTLY — the SAME rules the web portal (alice-website/chat.html)
   and alice-code already ship:

     - a non-ready tier (loading / capacity_available / no_capable_node) looks
       non-ready and is NON-selectable;
     - a 503 REASON_MODEL_TIER_LOADING / REASON_MODEL_TIER_NO_CAPABLE_NODE
       surfaces the gateway's OWN plaintext + a retry affordance, NEVER a
       fabricated answer or a generic swallow;
     - BACK-COMPAT: if /v1/models omits the new status fields, or a chat
       response carries no alice_receipt, nothing is invented — we fall back to
       prior behavior.

   It is deliberately framework-free and DOM-free so it can be unit-tested under
   node (see tests/test_gateway_normalize_js.py) AND consumed by the lean IIFE
   (which reads window.AliceGateway). Mirrors chat.html:244-742 verbatim in
   logic; only the React/JSX rendering lives in alice-lean.js.
   =========================================================================== */
(function (root) {
  'use strict';

  /* Per-status presentation. `ready` is the ONLY selectable/serving state
   * (#91 防吹牛: only a ready tier dispatches); every other status is
   * greyed-out + non-selectable and carries its own honest badge. The fallback
   * label is used when the gateway omits a human `status_text`. Mirrors
   * chat.html STATUS_PRESENTATION (chat.html:248-253). */
  var STATUS_PRESENTATION = {
    ready:              { label: 'LIVE',    cls: 'tier-live',    selectable: true,  fallbackText: 'Ready: a capable node is online and serving this model.' },
    loading:            { label: 'LOADING', cls: 'tier-loading', selectable: false, fallbackText: 'Loading: a capable node is spinning up this model; retry shortly.' },
    capacity_available: { label: 'COMING',  cls: 'tier-soon',    selectable: false, fallbackText: 'Coming: the fleet could run this size, but it is not yet a served tier.' },
    no_capable_node:    { label: 'OFFLINE', cls: 'tier-offline', selectable: false, fallbackText: 'No capable node online can run this model right now.' },
  };
  function statusPres(status) { return STATUS_PRESENTATION[status] || STATUS_PRESENTATION.capacity_available; }

  // Map a 4B/27B-style billions count into a compact param tag for display.
  function paramTag(billions) {
    if (billions == null || isNaN(billions)) return '';
    return billions >= 1 ? billions + 'B' : Math.round(billions * 1000) + 'M';
  }

  /* Normalize one OpenAI-shaped `GET /v1/models` object into the picker's row
   * shape. BACK-COMPAT: if the #91 status fields are ABSENT (today's pre-deploy
   * gateway), fall back to `served` (if present) else to a conservative
   * non-ready default — never inventing a ready tier. Mirrors
   * chat.html:266-288. */
  function normalizeModel(o) {
    if (!o || typeof o.id !== 'string') return null;
    var hasStatus = typeof o.status === 'string';
    var status;
    if (hasStatus) {
      status = STATUS_PRESENTATION[o.status] ? o.status : 'capacity_available';
    } else if (typeof o.served === 'boolean') {
      // Pre-#91 gateway that still exposed `served`: served -> ready, else coming.
      status = o.served ? 'ready' : 'capacity_available';
    } else {
      // Oldest shape (id + name only): be conservative — not serveable.
      status = 'capacity_available';
    }
    var name = o.display_name || o.description || o.name || o.id;
    var params = paramTag(o.parameter_billions);
    var note = (typeof o.status_text === 'string' && o.status_text) || statusPres(status).fallbackText;
    // `signalled`: the object carried SOME honest availability signal (#91
    // status or the older `served` bool). When NO object in the catalog is
    // signalled, the gateway predates both -> keep the static FALLBACK catalog
    // (behave exactly as today).
    var signalled = hasStatus || typeof o.served === 'boolean';
    return {
      id: o.id, name: name, params: params, status: status,
      live: status === 'ready', note: note,
      min_vram_gb: o.min_vram_gb, statusKnown: hasStatus, signalled: signalled,
    };
  }

  // The first SELECTABLE (ready) tier in a normalized list, or null.
  function firstSelectable(models) {
    for (var i = 0; i < models.length; i++) {
      if (statusPres(models[i].status).selectable) return models[i];
    }
    return null;
  }

  /* Normalize a whole `GET /v1/models` body (the proxy returns the gateway's
   * raw OpenAI shape: {object:'list', data:[...]}). Returns:
   *   { models, signalled, statusKnown }
   * `signalled === false` means the gateway carried NO availability signal on
   * ANY object (pre-deploy) -> the caller should keep its static fallback
   * catalog and NOT mark every tier dead (chat.html:540-551). */
  function normalizeCatalog(body) {
    var data = (body && Array.isArray(body.data)) ? body.data : [];
    var models = [];
    for (var i = 0; i < data.length; i++) {
      var n = normalizeModel(data[i]);
      if (n) models.push(n);
    }
    var signalled = false, statusKnown = false;
    for (var j = 0; j < models.length; j++) {
      if (models[j].signalled) signalled = true;
      if (models[j].statusKnown) statusKnown = true;
    }
    return { models: models, signalled: signalled, statusKnown: statusKnown };
  }

  /* The two #91 request-time tier-status reason codes the gateway returns on a
   * 503 for an OFFERED tier that is not `ready`. We surface the gateway's OWN
   * plaintext + a retry affordance HONESTLY — never a fabricated answer.
   * Mirrors chat.html:660-661. */
  var REASON_MODEL_TIER_LOADING = 'api_chat_model_tier_loading';
  var REASON_MODEL_TIER_NO_CAPABLE_NODE = 'api_chat_model_tier_no_capable_node';

  /* A structured tier-status error (chat.html:666-675). Carries the gateway's
   * plaintext message, reason code, live status, and Retry-After hint so the UI
   * can render an honest "retry"/"offline" affordance — not a flat failure or a
   * made-up completion. */
  function TierStatusError(opts) {
    var e = new Error(opts.message);
    e.name = 'TierStatusError';
    e.tier = true;
    e.code = opts.code;
    e.message = opts.message;
    e.status = opts.status || (opts.code === REASON_MODEL_TIER_LOADING ? 'loading' : 'no_capable_node');
    e.retryAfter = (opts.retryAfter == null) ? null : opts.retryAfter;
    return e;
  }

  /* Given a non-ok 503 response's body text + Retry-After header value, decide
   * whether it is an HONEST tier-status error we should surface verbatim. Returns
   * a TierStatusError on the two known reason codes, else null (caller throws a
   * generic transport error). Mirrors chat.html:697-711, but pure/DOM-free so it
   * is unit-testable. */
  function parseTierError(httpStatus, bodyText, retryAfterHeader) {
    if (httpStatus !== 503 || !bodyText) return null;
    var parsed = null;
    try { parsed = JSON.parse(bodyText); } catch (_) { return null; }
    var err = parsed && parsed.error;
    var code = err && err.code;
    if (code === REASON_MODEL_TIER_LOADING || code === REASON_MODEL_TIER_NO_CAPABLE_NODE) {
      var ra = parseInt(retryAfterHeader || '', 10);
      return TierStatusError({
        message: (err && err.message) || 'This model tier is not ready right now.',
        code: code,
        status: parsed.metadata && parsed.metadata.model_tier_status,
        retryAfter: isNaN(ra) ? null : ra,
      });
    }
    return null;
  }

  /* Pull an alice_receipt off a parsed SSE chunk wherever it rides (top-level or
   * nested under choices[0]). Mirrors chat.html:723-725. */
  function receiptFromChunk(obj) {
    if (!obj) return null;
    return obj.alice_receipt ||
      (obj.choices && obj.choices[0] && obj.choices[0].alice_receipt) || null;
  }

  /* Build the ordered [key, value] rows for the verification-receipt panel,
   * dropping empty values. `paid_acu` defaults to '0' — credit-only, no
   * real-money. Mirrors chat.html:569-581. */
  function receiptRows(r) {
    if (!r) return [];
    var raw = [
      ['spec_id', r.spec_id],
      ['miner_id', r.miner_id],
      ['decode_rule', r.decode_rule],
      ['prompt_token_ids_hash', r.prompt_token_ids_hash],
      ['output_token_ids_hash', r.output_token_ids_hash],
      ['output_cid', r.output_cid],
      ['request_id', r.request_id],
      ['nonce', r.nonce],
      ['signed', (r.signed === undefined ? undefined : String(r.signed))],
      ['token_ids_available', (r.token_ids_available === undefined ? undefined : String(r.token_ids_available))],
      ['paid_acu', r.paid_acu != null ? String(r.paid_acu) : '0'],
    ];
    var out = [];
    for (var i = 0; i < raw.length; i++) {
      var v = raw[i][1];
      if (v !== undefined && v !== null && v !== '') out.push([raw[i][0], v]);
    }
    return out;
  }

  /* Build the human-readable "Sign in with Alice" login challenge — IDENTICAL
   * text to alice-website/chat.html:388-399 so the signature is portable across
   * the portal and this desktop shell. The Alice ADDRESS is the session
   * identity. (No nonce server yet; timestamp + random make it fresh.) */
  function buildLoginChallenge(address, domain) {
    domain = domain || 'aliceprotocol.org';
    var nonce;
    if (root.crypto && root.crypto.getRandomValues) {
      var arr = new Uint8Array(16);
      root.crypto.getRandomValues(arr);
      nonce = Array.prototype.map.call(arr, function (b) {
        return ('0' + b.toString(16)).slice(-2);
      }).join('');
    } else {
      nonce = String(Math.random()).slice(2);
    }
    var issuedAt = new Date().toISOString();
    var challenge =
      'Sign in with Alice\n' +
      'domain: ' + domain + '\n' +
      'address: ' + address + '\n' +
      'nonce: ' + nonce + '\n' +
      'issued-at: ' + issuedAt + '\n' +
      'statement: Authenticate this address as my Alice chat session. This request will not trigger a transaction or cost any tokens.';
    return { challenge: challenge, nonce: nonce, issuedAt: issuedAt };
  }

  var NS = {
    STATUS_PRESENTATION: STATUS_PRESENTATION,
    statusPres: statusPres,
    paramTag: paramTag,
    normalizeModel: normalizeModel,
    normalizeCatalog: normalizeCatalog,
    firstSelectable: firstSelectable,
    REASON_MODEL_TIER_LOADING: REASON_MODEL_TIER_LOADING,
    REASON_MODEL_TIER_NO_CAPABLE_NODE: REASON_MODEL_TIER_NO_CAPABLE_NODE,
    TierStatusError: TierStatusError,
    parseTierError: parseTierError,
    receiptFromChunk: receiptFromChunk,
    receiptRows: receiptRows,
    buildLoginChallenge: buildLoginChallenge,
  };

  // Browser: expose as a runtime global (same pattern as window.AliceMD).
  if (root) root.AliceGateway = NS;
  // Node (tests): CommonJS export. No effect in the browser.
  if (typeof module !== 'undefined' && module.exports) module.exports = NS;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
