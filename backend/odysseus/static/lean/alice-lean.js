/* ===========================================================================
   Alice — lean local-AI chat app (classic script, single IIFE, ZERO imports).

   This is the entire app controller. It is deliberately flat: no ES modules,
   no import graph, no circular deps — just one IIFE that talks to the local
   backend over fetch (credentials:'same-origin' so the per-launch
   `alice_local_token` cookie authenticates every call).

   Depends only on globals that may or may not exist:
     - window.AliceMD   (our tiny markdown renderer; required)
     - window.hljs      (highlight.js; optional, for code highlighting)

   Endpoints (confirmed against alice_routes.py / live backend):
     GET  /alice/device        -> {label, accelerator, memory_gb}
     GET  /alice/recommend     -> {recommended:{id,display_name,...,state}, device}
     GET  /alice/models        -> {models:[{id,display_name,tagline,
                                   download_size_human, state, gate, context, active, is_moe}], device}
     GET  /alice/current       -> {current: <card|null>}
     POST /alice/ensure        -> SSE: data:{phase,fraction,downloaded_bytes,
                                   total_bytes,rate_bps,...} ... data:{phase:"done"|"error"} ... [DONE]
                                   BODY KEY IS `id` (not model_key).
     POST /alice/load          -> {id, current} | 409 {gate, needs_confirm|blocked}
                                   BODY: {id, confirm?, context_length?}
     POST /alice/context       -> set context. BODY: {id, context_length}
     GET/POST /alice/mode      -> {agent_mode, locked, ...}
     GET  /alice/earn/status   -> {state, gpu_earn, honesty:{credit_only}, download_url, ...}
     POST /alice/earn/open-miner -> {launched, fallback_url}
     POST /v1/chat/completions -> OpenAI-compatible SSE (delta.content; [DONE]).
   =========================================================================== */
(function () {
  'use strict';

  /* ----------------------------- i18n ------------------------------------ *
   * HOW TO ADD A LANGUAGE: (1) add its code to LANGS below (code + short
   * native label for the switcher), then (2) add a `<code>: '…'` entry to
   * EVERY string in STR. The switcher, persistence and live re-render all key
   * off LANGS automatically — no other code changes. `en` is the fallback for
   * any string a language is missing, so a partial dict still renders.
   * (Brand rule: model display names — "Alice Lite/Alice/Alice Pro/Alice RP"
   * — and the product word "Alice" itself are NEVER translated.)
   * ----------------------------------------------------------------------- */
  var LANGS = [
    { code: 'en', label: 'EN' },
    { code: 'zh', label: '中' },
    // Future, e.g.:  { code: 'zh-Hant', label: '繁' },  { code: 'ja', label: '日' },  { code: 'ko', label: '한' }
  ];
  var DEFAULT_LANG = 'en';

  var STR = {
    'setup.title':   { en: "Hi, I'm Alice", zh: '你好，我是 Alice' },
    'setup.sub':     { en: 'A private AI that runs on your own computer — free, offline, yours.',
                       zh: '在你自己的电脑上运行的私密 AI——免费、离线、属于你。' },
    'setup.cta':     { en: 'Download & start', zh: '下载并开始' },
    'setup.starting':{ en: 'Preparing…', zh: '正在准备…' },
    'setup.recommended': { en: 'Recommended', zh: '推荐' },
    'setup.foot':    { en: 'Downloads once, then works fully offline. No account, no cloud — nothing leaves your computer.',
                       zh: '只需下载一次，之后完全离线运行。无需账号、无云端——数据不离开你的电脑。' },
    'setup.change':  { en: 'Choose a different model', zh: '选择其它模型' },
    'setup.loading': { en: 'Loading the model…', zh: '正在载入模型…' },
    'dl.preparing':  { en: 'Preparing', zh: '正在准备' },
    'dl.downloading':{ en: 'Downloading', zh: '下载中' },
    'dl.verifying':  { en: 'Verifying', zh: '正在校验' },
    'dl.loading':    { en: 'Loading', zh: '正在载入' },
    'dl.done':       { en: 'Done', zh: '完成' },
    'dl.failed':     { en: 'Download failed', zh: '下载失败' },
    'dl.retry':      { en: 'Try again', zh: '重试' },
    'chat.greeting': { en: "Hi, I'm Alice.", zh: '你好，我是 Alice。' },
    'chat.subtitle': { en: 'Runs on your computer · Fully private · Free forever',
                       zh: '在本机运行 · 完全私密 · 永久免费' },
    'chat.placeholder': { en: 'Message Alice…', zh: '写点什么…' },
    'chat.input.aria': { en: 'Message Alice', zh: '给 Alice 发消息' },
    'chat.send':     { en: 'Send', zh: '发送' },
    'chat.note':     { en: 'Alice runs on this device. No telemetry, no cloud — chats stay on your computer.',
                       zh: 'Alice 在本机运行。无遥测、无云端——聊天记录只保留在你的电脑上。' },
    'chat.private':  { en: 'Private · on-device', zh: '本地 · 私密' },
    'chat.thinking': { en: 'Thinking…', zh: '思考中…' },
    'chat.stop':     { en: 'Stop', zh: '停止' },
    'chat.newchat':  { en: 'New chat', zh: '新对话' },
    'chat.settings': { en: 'Settings', zh: '设置' },
    'chat.error':    { en: 'Something went wrong. Please try again.', zh: '出错了，请重试。' },
    'lang.label':    { en: 'Language', zh: '语言' },
    'lang.switch':   { en: 'Switch language', zh: '切换语言' },
    'common.close':  { en: 'Close', zh: '关闭' },
    'set.title':     { en: 'Settings', zh: '设置' },
    'set.model':     { en: 'Model', zh: '模型' },
    'set.model.rp':  { en: 'Roleplay', zh: '角色扮演' },
    'set.model.rp.desc': { en: 'Characters & story', zh: '角色与剧情' },
    'set.context':   { en: 'Context length', zh: '上下文长度' },
    'set.context.desc': { en: 'How much of the conversation Alice keeps in mind. Larger uses more memory.',
                          zh: 'Alice 能记住多长的对话。越大占用内存越多。' },
    'set.agent':     { en: 'Agent mode', zh: '智能体模式' },
    'set.agent.desc':{ en: 'Let Alice run code, read & write files, and use tools on your computer. Off by default — safe chat only.',
                       zh: '让 Alice 在你的电脑上运行代码、读写文件并使用工具。默认关闭——仅安全聊天。' },
    'set.agent.locked': { en: 'Disabled by this installation', zh: '此安装已禁用' },
    'set.agent.restart':{ en: 'Restart Alice to fully enable advanced tools.', zh: '重启 Alice 以完整启用高级工具。' },
    'set.earn':      { en: 'Earn', zh: '赚取' },
    'set.earn.desc': { en: 'Optional — contribute spare compute with the separate Alice Miner app.',
                       zh: '可选——使用独立的 Alice Miner 应用贡献闲置算力。' },
    'set.earn.open': { en: 'Open Alice Miner', zh: '打开 Alice Miner' },
    'set.earn.get':  { en: 'Get Alice Miner', zh: '获取 Alice Miner' },
    'set.earn.honest': { en: 'The miner is a separate app. Nothing mines inside this chat and no funds move here. Rewards are credit-only for now.',
                         zh: '矿工是独立应用。本聊天内不挖矿、不涉及任何资金转移。当前奖励仅为待发放积分。' },
    'set.done':      { en: 'Done', zh: '完成' },
    'risk.title':    { en: 'Turn on Agent mode?', zh: '开启智能体模式？' },
    'risk.body':     { en: 'Agent mode lets Alice run code, read & write files, and use tools on your computer. It is powerful but risky — a malicious web page or a document you paste could try to misuse it. Only turn this on if you understand and accept the risk.',
                       zh: '智能体模式让 Alice 在你的电脑上运行代码、读写文件并使用工具。它很强大但有风险——恶意网页或你粘贴的文档可能试图滥用它。仅在你理解并接受风险时开启。' },
    'risk.cancel':   { en: 'Cancel', zh: '取消' },
    'risk.confirm':  { en: 'I understand, turn it on', zh: '我已了解，开启' },
    'common.copy':   { en: 'Copy', zh: '复制' },
    'common.copied': { en: 'Copied', zh: '已复制' },
    // Agent mode (tool-using turns). Tool LABELS below are localized UI chrome;
    // they are NOT model/brand names so translating them is fine.
    'agent.badge':   { en: 'Agent', zh: '智能体' },
    'agent.indicator': { en: 'Agent mode · Alice can use tools', zh: '智能体模式 · Alice 可使用工具' },
    'agent.running': { en: 'Running…', zh: '正在运行…' },
    'agent.ok':      { en: 'Done', zh: '完成' },
    'agent.failed':  { en: 'Failed', zh: '失败' },
    'agent.working': { en: 'Working…', zh: '处理中…' },
    'agent.tool.bash':       { en: 'Shell', zh: '终端命令' },
    'agent.tool.python':     { en: 'Python', zh: 'Python' },
    'agent.tool.read_file':  { en: 'Read file', zh: '读取文件' },
    'agent.tool.write_file': { en: 'Write file', zh: '写入文件' },
    'agent.tool.web_search': { en: 'Web search', zh: '联网搜索' },
    'agent.tool.web_fetch':  { en: 'Open page', zh: '抓取网页' },
  };

  function isLang(code) {
    for (var i = 0; i < LANGS.length; i++) if (LANGS[i].code === code) return true;
    return false;
  }
  var _lang = (function () {
    try {
      var saved = localStorage.getItem('alice-lang');
      if (saved && isLang(saved)) return saved;       // user's explicit choice always wins
    } catch (_) {}
    var nav = (navigator.language || DEFAULT_LANG).toLowerCase();
    if (nav.indexOf('zh') === 0 && isLang('zh')) return 'zh';   // auto-detect on first run
    return DEFAULT_LANG;
  })();
  function t(key) {
    var e = STR[key];
    if (!e) return key;
    if (e[_lang] != null) return e[_lang];
    return e.en != null ? e.en : key;
  }
  function getLang() { return _lang; }
  function setLang(code) {
    if (!isLang(code) || code === _lang) return;
    _lang = code;
    try { localStorage.setItem('alice-lang', code); } catch (_) {}
    document.documentElement.setAttribute('lang', code);
    rerenderAll();
  }
  // Expose a minimal i18n handle for the OTHER classic IIFE (alice-md.js) to
  // localize the code-block "Copy" label — a runtime global lookup, same
  // pattern as window.AliceMD / window.hljs. No import graph is introduced.
  window.AliceI18N = { t: t, lang: getLang };

  // Re-render every piece of visible chrome in the current language. Cheap:
  // the setup card / chat shell / open settings sheet are all rebuilt from the
  // in-memory state, so switching language is instant and loses nothing.
  function rerenderAll() {
    window.AliceI18N.lang = getLang;

    var setupVisible = el('setup') && !el('setup').classList.contains('hidden');
    if (setupVisible && _lastSetupRec) {
      // (A download in progress holds its own DOM under #dlMount; we only
      //  rebuild the static setup card when no transfer is mid-flight.)
      if (!el('dlMount') || !el('dlMount').firstChild) renderSetup(_lastSetupRec);
    }

    var chatVisible = el('chat') && !el('chat').classList.contains('hidden');
    if (chatVisible) {
      // Preserve the in-progress composer draft across the rebuild.
      var prevTa = el('composerInput');
      var draft = prevTa ? prevTa.value : '';
      // A live stream writes into a detached bubble if we rebuild the thread;
      // stop it cleanly first (its partial text is already in state.messages).
      if (state.streaming) stopStream();
      buildChatShell();
      renderThread();
      updateModelPill();
      updateAgentIndicator();
      var ta = el('composerInput');
      if (ta) { ta.value = draft; autoGrow(); }
    }

    if (_settingsOpen) {
      // close the stale settings overlay and reopen it freshly localized
      var ov = document.querySelector('.overlay');
      if (ov) ov.remove();
      _settingsOpen = false;
      openSettings();
    }
  }

  /* --------------------------- API client -------------------------------- */
  var API = {
    opts: function (method, body) {
      var o = { method: method || 'GET', credentials: 'same-origin', headers: {} };
      if (body !== undefined) {
        o.headers['Content-Type'] = 'application/json';
        o.body = JSON.stringify(body);
      }
      return o;
    },
    getJSON: function (path) {
      return fetch(path, API.opts('GET')).then(function (r) { return r.json(); });
    },
    postJSON: function (path, body) {
      return fetch(path, API.opts('POST', body)).then(function (r) {
        return r.json().then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
      });
    },
    device: function () { return API.getJSON('/alice/device'); },
    recommend: function () { return API.getJSON('/alice/recommend'); },
    // ?rp=1 surfaces the dedicated roleplay line (Alice RP / Alice RP Lite)
    // alongside the general tiers, so the model picker can offer them.
    models: function () { return API.getJSON('/alice/models?rp=1'); },
    current: function () { return API.getJSON('/alice/current'); },
    mode: function () { return API.getJSON('/alice/mode'); },
    setMode: function (on, ack) { return API.postJSON('/alice/mode', { agent_mode: on, risk_acknowledged: ack }); },
    setContext: function (id, n) { return API.postJSON('/alice/context', { id: id, context_length: n }); },
    load: function (id, opts) {
      opts = opts || {};
      var b = { id: id, confirm: !!opts.confirm };
      if (opts.context != null) b.context_length = opts.context;
      return API.postJSON('/alice/load', b);
    },
    earnStatus: function () { return API.getJSON('/alice/earn/status'); },
    openMiner: function () { return API.postJSON('/alice/earn/open-miner', {}); },

    // SSE download with progress. onEvent({phase, fraction, ...}). Resolves on
    // terminal {phase:'done'} / [DONE]; rejects on {phase:'error'}.
    ensure: function (id, onEvent) {
      return new Promise(function (resolve, reject) {
        fetch('/alice/ensure', API.opts('POST', { id: id })).then(function (resp) {
          if (!resp.ok || !resp.body) { reject(new Error('ensure failed: ' + resp.status)); return; }
          var reader = resp.body.getReader();
          var dec = new TextDecoder();
          var buf = '';
          var last = null;
          (function pump() {
            return reader.read().then(function (res) {
              if (res.done) {
                if (last && last.phase === 'error') reject(new Error(last.message || 'download error'));
                else resolve(last || { phase: 'done' });
                return;
              }
              buf += dec.decode(res.value, { stream: true });
              var parts = buf.split('\n');
              buf = parts.pop();
              parts.forEach(function (line) {
                line = line.trim();
                if (line.indexOf('data:') !== 0) return;
                var data = line.slice(5).trim();
                if (data === '[DONE]') return;
                try {
                  var ev = JSON.parse(data);
                  last = ev;
                  if (onEvent) onEvent(ev);
                } catch (_) {}
              });
              return pump();
            });
          })().catch(reject);
        }).catch(reject);
      });
    },

    // OpenAI-compatible chat stream. onDelta(textChunk). signal -> AbortSignal.
    chat: function (model, messages, onDelta, signal) {
      return new Promise(function (resolve, reject) {
        fetch('/v1/chat/completions', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: model, messages: messages, stream: true }),
          signal: signal,
        }).then(function (resp) {
          if (!resp.ok || !resp.body) { reject(new Error('chat failed: ' + resp.status)); return; }
          var reader = resp.body.getReader();
          var dec = new TextDecoder();
          var buf = '';
          (function pump() {
            return reader.read().then(function (res) {
              if (res.done) { resolve(); return; }
              buf += dec.decode(res.value, { stream: true });
              var parts = buf.split('\n');
              buf = parts.pop();
              for (var i = 0; i < parts.length; i++) {
                var line = parts[i].trim();
                if (line.indexOf('data:') !== 0) continue;
                var data = line.slice(5).trim();
                if (data === '[DONE]') { resolve(); return; }
                try {
                  var json = JSON.parse(data);
                  var delta = json.choices && json.choices[0] && json.choices[0].delta;
                  if (delta && typeof delta.content === 'string' && delta.content) onDelta(delta.content);
                } catch (_) {}
              }
              return pump();
            });
          })().catch(function (err) {
            if (err && err.name === 'AbortError') resolve();
            else reject(err);
          });
        }).catch(function (err) {
          if (err && err.name === 'AbortError') resolve();
          else reject(err);
        });
      });
    },

    // Curated agent stream (code+files+web). POSTs the transcript to
    // /alice/agent_stream and dispatches each structured event to on[type]:
    //   delta{text} assistant_text{text} tool_start{tool,input}
    //   tool_output{tool,output,exit_code,ok} agent_step{round} done error{message}
    // Resolves on terminal [DONE]; rejects on 403 (agent mode off) / transport.
    agentStream: function (messages, on, signal) {
      return new Promise(function (resolve, reject) {
        fetch('/alice/agent_stream', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: messages }),
          signal: signal,
        }).then(function (resp) {
          if (resp.status === 403) { reject(new Error('AGENT_MODE_OFF')); return; }
          if (!resp.ok || !resp.body) { reject(new Error('agent failed: ' + resp.status)); return; }
          var reader = resp.body.getReader();
          var dec = new TextDecoder();
          var buf = '';
          (function pump() {
            return reader.read().then(function (res) {
              if (res.done) { resolve(); return; }
              buf += dec.decode(res.value, { stream: true });
              var parts = buf.split('\n');
              buf = parts.pop();
              for (var i = 0; i < parts.length; i++) {
                var line = parts[i].trim();
                if (line.indexOf('data:') !== 0) continue;
                var data = line.slice(5).trim();
                if (data === '[DONE]') { resolve(); return; }
                try {
                  var ev = JSON.parse(data);
                  if (ev && ev.type && on && on[ev.type]) on[ev.type](ev);
                } catch (_) {}
              }
              return pump();
            });
          })().catch(function (err) {
            if (err && err.name === 'AbortError') resolve();
            else reject(err);
          });
        }).catch(function (err) {
          if (err && err.name === 'AbortError') resolve();
          else reject(err);
        });
      });
    },
  };

  /* ------------------------------ helpers -------------------------------- */
  function el(id) { return document.getElementById(id); }
  function h(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === 'class') n.className = attrs[k];
      else if (k === 'text') n.textContent = attrs[k];
      else if (k === 'html') n.innerHTML = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    if (children) children.forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }
  function fmtBytes(b) {
    if (!b || b < 0) return '';
    var u = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return (b >= 10 || i === 0 ? Math.round(b) : b.toFixed(1)) + ' ' + u[i];
  }
  var MARK_SVG = "<svg viewBox='0 0 1024 1024' xmlns='http://www.w3.org/2000/svg' aria-hidden='true'><g transform='translate(0,1024) scale(0.1,-0.1)' fill='#F97316'><path d='M5635 7050 c112 -184 252 -416 312 -515 60 -99 224 -369 365 -600 140 -231 373 -616 518 -855 144 -239 273 -452 286 -472 13 -20 193 -317 400 -660 207 -343 428 -707 490 -810 63 -103 114 -189 114 -192 0 -3 -525 -6 -1167 -6 l-1168 0 81 33 c91 37 197 109 262 177 261 272 330 674 181 1055 -43 110 -53 128 -315 550 -181 292 -334 542 -501 820 -201 336 -356 591 -367 602 -8 9 -29 -19 -87 -115 -95 -160 -269 -446 -431 -712 -69 -113 -168 -275 -220 -360 -52 -85 -141 -229 -198 -320 -277 -440 -323 -555 -336 -820 -7 -145 8 -250 57 -384 52 -145 181 -316 309 -412 47 -35 157 -90 197 -99 18 -3 35 -10 37 -13 4 -8 -2325 -5 -2332 3 -2 2 69 125 158 272 89 147 214 354 278 458 63 105 170 280 237 390 115 188 222 364 740 1220 117 193 276 456 355 585 170 280 516 850 833 1375 126 209 267 442 314 517 l85 138 155 -258 c86 -141 247 -408 358 -592z m-505 -1770 c0 -29 49 -159 83 -220 46 -84 123 -184 178 -234 109 -98 254 -173 384 -199 l70 -14 -73 -16 c-280 -63 -541 -316 -631 -612 -7 -22 -15 -47 -18 -55 -3 -8 -11 9 -19 38 -35 134 -126 286 -238 398 -119 118 -250 194 -401 231 l-66 16 88 22 c157 40 260 98 374 210 116 115 208 272 241 414 11 48 28 61 28 21z'/></g></svg>";
  // small inline icons (stroke = currentColor), no emoji
  var IC = {
    send: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M5 12h14M13 6l6 6-6 6'/></svg>",
    stop: "<svg viewBox='0 0 24 24' fill='currentColor'><rect x='7' y='7' width='10' height='10' rx='2'/></svg>",
    plus: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round'><path d='M12 5v14M5 12h14'/></svg>",
    gear: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='3'/><path d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'/></svg>",
    lock: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='11' width='18' height='11' rx='2'/><path d='M7 11V7a5 5 0 0 1 10 0v4'/></svg>",
    close: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round'><path d='M18 6 6 18M6 6l12 12'/></svg>",
    check: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/></svg>",
    globe: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><circle cx='12' cy='12' r='9'/><path d='M3 12h18M12 3c2.5 2.5 2.5 15 0 18M12 3c-2.5 2.5-2.5 15 0 18'/></svg>",
    term: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><rect x='3' y='4' width='18' height='16' rx='2'/><path d='M7 9l3 3-3 3M13 15h4'/></svg>",
    code: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M8 9l-3 3 3 3M16 9l3 3-3 3M13 7l-2 10'/></svg>",
    file: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/></svg>",
  };

  function toast(msg) {
    var existing = document.querySelector('.toast');
    if (existing) existing.remove();
    var node = h('div', { class: 'toast', text: msg });
    document.body.appendChild(node);
    setTimeout(function () { node.remove(); }, 1800);
  }

  /* ------------------------------ state ---------------------------------- */
  var state = {
    device: null,
    models: [],
    current: null,     // active model card
    mode: null,        // /alice/mode payload
    messages: [],      // [{role, content}]
    streaming: false,
    abort: null,
  };
  // i18n re-render bookkeeping (set by the screens; read by rerenderAll()).
  var _lastSetupRec = null;   // the model card the setup screen last rendered
  var _settingsOpen = false;  // whether the settings sheet is currently open

  /* ----------------------- language switcher control --------------------- *
   * A compact on-brand "EN / 中" segmented toggle. Used in the chat header
   * (near the gear) AND inside the settings sheet. Clicking a segment calls
   * setLang(), which persists the choice and live re-renders all visible text.
   */
  function buildLangSwitch(extraClass) {
    var seg = h('div', { class: 'lang-seg' + (extraClass ? ' ' + extraClass : ''),
                         role: 'group', 'aria-label': t('lang.switch') });
    LANGS.forEach(function (L) {
      var on = L.code === _lang;
      var b = h('button', {
        class: 'lang-opt' + (on ? ' on' : ''),
        type: 'button',
        text: L.label,
        title: t('lang.switch'),
        'aria-pressed': on ? 'true' : 'false',
        'data-lang': L.code,
      });
      b.addEventListener('click', function () { setLang(L.code); });
      seg.appendChild(b);
    });
    return seg;
  }

  function activeModelKey() { return state.current ? state.current.id : 'lite'; }
  function chatModelName() {
    // /v1/chat/completions wants a model string; "alice-<id>" is what the
    // backend emitted in its own response (model:"alice-lite"). Use that form.
    return 'alice-' + activeModelKey();
  }
  function activeDisplayName() {
    if (state.current && state.current.display_name) return state.current.display_name;
    var m = findModel(activeModelKey());
    return m ? m.display_name : 'Alice';
  }
  function findModel(id) {
    // Prefer the active one; otherwise the recommended; otherwise first by id.
    var byId = state.models.filter(function (m) { return m.id === id; });
    if (!byId.length) return null;
    var active = byId.filter(function (m) { return m.active; })[0];
    if (active) return active;
    var rec = byId.filter(function (m) { return m.recommended; })[0];
    return rec || byId[0];
  }

  /* ============================ SETUP SCREEN ============================== */
  function renderSetup(rec) {
    _lastSetupRec = rec;
    var root = el('setup');
    root.innerHTML = '';
    var dev = state.device || {};
    var devLabel = [dev.label, dev.accelerator, (dev.memory_gb ? dev.memory_gb + ' GB' : '')]
      .filter(Boolean).join(' · ');

    // language switcher, pinned top-right of the setup viewport
    root.appendChild(buildLangSwitch('lang-seg-setup'));

    var card = h('div', { class: 'setup-card' });
    card.appendChild(h('div', { class: 'mark setup-mark', html: MARK_SVG }));
    card.appendChild(h('h1', { class: 'setup-title', text: t('setup.title') }));
    card.appendChild(h('p', { class: 'setup-sub', text: t('setup.sub') }));
    if (devLabel) {
      card.appendChild(h('div', { class: 'device-chip' }, [
        h('span', { class: 'dot' }),
        h('span', { text: devLabel }),
      ]));
    }

    // model card for the recommended tier
    var mc = h('div', { class: 'model-card' });
    var top = h('div', { class: 'mc-top' });
    var nameWrap = h('div', null, [
      h('span', { class: 'mc-name', text: rec.display_name || 'Alice Lite' }),
      h('span', { class: 'mc-badge', text: t('setup.recommended') }),
    ]);
    top.appendChild(nameWrap);
    top.appendChild(h('span', { class: 'mc-size', text: rec.download_size_human || '' }));
    mc.appendChild(top);
    if (rec.tagline) mc.appendChild(h('div', { class: 'mc-tag', text: rec.tagline }));
    card.appendChild(mc);

    // CTA + progress mount
    var cta = h('button', { class: 'btn btn-primary setup-cta', type: 'button', text: t('setup.cta') });
    card.appendChild(cta);
    var progMount = h('div', { id: 'dlMount' });
    card.appendChild(progMount);
    card.appendChild(h('p', { class: 'setup-foot', text: t('setup.foot') }));

    cta.addEventListener('click', function () { startDownload(rec, cta, progMount); });

    root.appendChild(card);
    root.classList.remove('hidden');
    el('chat').classList.add('hidden');
  }

  function startDownload(rec, cta, mount) {
    cta.disabled = true;
    cta.textContent = t('setup.starting');
    mount.innerHTML = '';

    var wrap = h('div', { class: 'dl-wrap' });
    var row = h('div', { class: 'dl-row' }, [
      h('span', { class: 'dl-phase', text: t('dl.preparing') }),
      h('span', { class: 'dl-pct', text: '' }),
    ]);
    var bar = h('div', { class: 'dl-bar' });
    var fill = h('div', { class: 'dl-fill indet' });
    bar.appendChild(fill);
    var meta = h('div', { class: 'dl-meta', text: '' });
    wrap.appendChild(row); wrap.appendChild(bar); wrap.appendChild(meta);
    mount.appendChild(wrap);

    var phaseEl = row.children[0], pctEl = row.children[1];

    function onEvent(ev) {
      var phaseKey = 'dl.' + (ev.phase || 'downloading');
      phaseEl.textContent = STR[phaseKey] ? t(phaseKey) : (ev.phase || t('dl.downloading'));
      var frac = typeof ev.fraction === 'number' ? ev.fraction : null;
      if (frac != null && frac >= 0) {
        fill.classList.remove('indet');
        var pct = Math.max(0, Math.min(100, Math.round(frac * 100)));
        fill.style.width = pct + '%';
        pctEl.textContent = pct + '%';
      } else {
        fill.classList.add('indet');
        pctEl.textContent = '';
      }
      var parts = [];
      if (ev.downloaded_bytes && ev.total_bytes) {
        parts.push(fmtBytes(ev.downloaded_bytes) + ' / ' + fmtBytes(ev.total_bytes));
      }
      if (ev.rate_bps) parts.push(fmtBytes(ev.rate_bps) + '/s');
      meta.textContent = parts.join('  ·  ');
    }

    API.ensure(rec.id, onEvent).then(function () {
      phaseEl.textContent = t('setup.loading');
      fill.classList.remove('indet');
      fill.style.width = '100%';
      pctEl.textContent = '';
      meta.textContent = '';
      return API.load(rec.id, { confirm: true });
    }).then(function (res) {
      if (res && res.ok && res.body && res.body.current) {
        state.current = res.body.current;
        enterChat();
      } else {
        // load returned a gate or error — still try to enter (model is downloaded)
        return API.current().then(function (c) {
          state.current = c.current;
          if (state.current) enterChat();
          else throw new Error((res && res.body && res.body.error) || 'load failed');
        });
      }
    }).catch(function (err) {
      fill.classList.remove('indet');
      fill.style.background = 'var(--danger)';
      phaseEl.textContent = t('dl.failed');
      meta.textContent = (err && err.message) ? err.message : '';
      cta.disabled = false;
      cta.textContent = t('dl.retry');
    });
  }

  /* ============================ CHAT SCREEN ============================== */
  function buildChatShell() {
    var chat = el('chat');
    chat.innerHTML = '';

    // top bar
    var bar = h('div', { class: 'topbar' });
    bar.appendChild(h('span', { class: 'mark tb-mark', html: MARK_SVG }));
    bar.appendChild(h('span', { class: 'tb-title', text: 'Alice' }));
    var pill = h('span', { class: 'model-pill', id: 'modelPill' }, [
      h('span', { class: 'pill-dot' }),
      h('span', { id: 'modelPillName', text: activeDisplayName() }),
    ]);
    bar.appendChild(pill);
    // Agent-mode badge — visible only while Agent mode is on, so the user always
    // knows when Alice can reach for tools. Toggled by updateAgentIndicator().
    var agentBadge = h('span', { class: 'agent-badge hidden', id: 'agentBadge', title: t('agent.indicator') }, [
      h('span', { class: 'mark', html: IC.term }),
      h('span', { text: t('agent.badge') }),
    ]);
    bar.appendChild(agentBadge);
    bar.appendChild(h('span', { class: 'tb-spacer' }));
    bar.appendChild(h('span', { class: 'privacy-tag' }, [
      h('span', { class: 'mark', html: IC.lock }),
      h('span', { text: t('chat.private') }),
    ]));
    bar.appendChild(buildLangSwitch('lang-seg-bar'));
    var newBtn = h('button', { class: 'icon-btn', type: 'button', title: t('chat.newchat'), 'aria-label': t('chat.newchat'), html: IC.plus });
    newBtn.addEventListener('click', newChat);
    bar.appendChild(newBtn);
    var setBtn = h('button', { class: 'icon-btn', type: 'button', title: t('chat.settings'), 'aria-label': t('chat.settings'), html: IC.gear });
    setBtn.addEventListener('click', openSettings);
    bar.appendChild(setBtn);
    chat.appendChild(bar);

    // scroll / thread
    var scroll = h('div', { class: 'scroll', id: 'scroll' });
    scroll.appendChild(h('div', { class: 'thread', id: 'thread' }));
    chat.appendChild(scroll);

    // composer
    var cwrap = h('div', { class: 'composer-wrap' });
    var comp = h('div', { class: 'composer' });
    var ta = h('textarea', { id: 'composerInput', rows: '1', placeholder: t('chat.placeholder'), 'aria-label': t('chat.input.aria') });
    var sendBtn = h('button', { class: 'send-btn', id: 'sendBtn', type: 'button', 'aria-label': t('chat.send'), html: IC.send });
    comp.appendChild(ta); comp.appendChild(sendBtn);
    cwrap.appendChild(comp);
    cwrap.appendChild(h('div', { class: 'composer-note', text: t('chat.note') }));
    chat.appendChild(cwrap);

    // wire composer
    ta.addEventListener('input', autoGrow);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        onSend();
      }
    });
    sendBtn.addEventListener('click', function () {
      if (state.streaming) stopStream(); else onSend();
    });

    // copy-button delegation for code blocks
    scroll.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-copy]');
      if (!btn) return;
      var pre = btn.closest('pre');
      var code = pre && pre.querySelector('code');
      if (!code) return;
      var text = code.textContent || '';
      copyText(text).then(function () {
        var orig = btn.textContent;
        btn.textContent = t('common.copied');
        setTimeout(function () { btn.textContent = orig; }, 1300);
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  }
  function fallbackCopy(text) {
    return new Promise(function (resolve) {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (_) {}
      ta.remove(); resolve();
    });
  }

  function autoGrow() {
    var ta = el('composerInput');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
    var send = el('sendBtn');
    if (!state.streaming) send.disabled = ta.value.trim().length === 0;
  }

  function renderThread() {
    var thread = el('thread');
    thread.innerHTML = '';
    if (!state.messages.length) {
      thread.appendChild(renderHero());
      return;
    }
    state.messages.forEach(function (m) {
      thread.appendChild(renderMessage(m));
    });
  }

  function renderHero() {
    var hero = h('div', { class: 'hero' });
    hero.appendChild(h('div', { class: 'mark hero-mark', html: MARK_SVG }));
    hero.appendChild(h('h2', { class: 'hero-h', text: t('chat.greeting') }));
    hero.appendChild(h('p', { class: 'hero-sub', text: t('chat.subtitle') }));
    return hero;
  }

  function renderMessage(m) {
    var role = m.role, content = m.content;
    var msg = h('div', { class: 'msg ' + role });
    if (role === 'assistant') {
      msg.appendChild(h('div', { class: 'avatar mark', html: MARK_SVG }));
      if (m.steps && m.steps.length) {
        // Agent turn: interleaved prose bubbles + tool cards (rebuilt from the
        // persisted step list, so restore + language-switch reproduce it).
        var wrap = h('div', { class: 'agent-turn' });
        m.steps.forEach(function (s) {
          var node = renderStep(s);
          if (node) wrap.appendChild(node);
        });
        msg.appendChild(wrap);
      } else {
        var bubble = h('div', { class: 'bubble md' });
        bubble.innerHTML = window.AliceMD ? window.AliceMD.render(content) : escapeText(content);
        highlightWithin(bubble);
        msg.appendChild(bubble);
      }
    } else {
      // user content as plain text (CSS preserves whitespace)
      msg.appendChild(h('div', { class: 'bubble', text: content }));
    }
    return msg;
  }

  // ---- agent-mode rendering (tool cards) --------------------------------- //
  function isAgentMode() { return !!(state.mode && state.mode.agent_mode); }
  function toolLabel(tool) {
    var k = 'agent.tool.' + tool;
    return STR[k] ? t(k) : tool;
  }
  function toolIcon(tool) {
    if (tool === 'bash') return IC.term;
    if (tool === 'python') return IC.code;
    if (tool === 'read_file' || tool === 'write_file') return IC.file;
    return IC.globe; // web_search / web_fetch
  }
  function renderStep(s) {
    if (!s) return null;
    if (s.kind === 'tool') return renderToolCard(s);
    var text = s.text || '';
    if (!text) return null;
    var bubble = h('div', { class: 'bubble md' });
    bubble.innerHTML = window.AliceMD ? window.AliceMD.render(text) : escapeText(text);
    highlightWithin(bubble);
    return bubble;
  }
  function renderToolCard(s) {
    var cls = 'tool-card ' + (s.running ? 'running' : (s.ok ? 'ok' : 'fail'));
    var card = h('div', { class: cls });
    var head = h('div', { class: 'tool-head' }, [
      h('span', { class: 'tool-ic mark', html: toolIcon(s.tool) }),
      h('span', { class: 'tool-name', text: toolLabel(s.tool) }),
      h('span', { class: 'tool-status', text: s.running ? t('agent.running') : (s.ok ? t('agent.ok') : t('agent.failed')) }),
    ]);
    card.appendChild(head);
    if (s.input) card.appendChild(h('div', { class: 'tool-input', text: s.input }));
    if (s.output != null && s.output !== '') {
      card.appendChild(h('pre', { class: 'tool-output' }, [ h('code', { text: s.output }) ]));
    }
    return card;
  }
  function escapeText(s) {
    var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML;
  }
  function highlightWithin(node) {
    if (!window.hljs || !window.hljs.highlightElement) return;
    // our renderer already highlights via hljs.highlight; nothing extra needed,
    // but if a code block came through un-highlighted, do it now.
    var blocks = node.querySelectorAll('pre code:not(.hljs-done)');
    for (var i = 0; i < blocks.length; i++) {
      blocks[i].classList.add('hljs-done');
    }
  }

  function scrollToBottom(force) {
    var s = el('scroll');
    if (!s) return;
    var nearBottom = s.scrollHeight - s.scrollTop - s.clientHeight < 120;
    if (force || nearBottom) s.scrollTop = s.scrollHeight;
  }

  function onSend() {
    var ta = el('composerInput');
    var text = ta.value.trim();
    if (!text || state.streaming) return;

    state.messages.push({ role: 'user', content: text });
    ta.value = '';
    autoGrow();
    persist();

    // append user bubble (rebuild hero -> messages on first send)
    var thread = el('thread');
    if (state.messages.length === 1) thread.innerHTML = '';
    thread.appendChild(renderMessage({ role: 'user', content: text }));
    scrollToBottom(true);

    if (isAgentMode()) sendAgentTurn(thread);
    else sendChatTurn(thread);
  }

  // Plain chat turn: a single streamed assistant bubble via /v1/chat/completions.
  function sendChatTurn(thread) {
    var assistant = { role: 'assistant', content: '' };
    state.messages.push(assistant);
    var msgNode = h('div', { class: 'msg assistant' }, [
      h('div', { class: 'avatar mark', html: MARK_SVG }),
    ]);
    var bubble = h('div', { class: 'bubble md caret' });
    msgNode.appendChild(bubble);
    thread.appendChild(msgNode);
    scrollToBottom(true);

    setStreaming(true);
    state.abort = new AbortController();

    var acc = '';
    var pending = false;
    function flush() {
      pending = false;
      assistant.content = acc;
      bubble.innerHTML = (window.AliceMD ? window.AliceMD.render(acc) : escapeText(acc));
      bubble.classList.add('caret');
      scrollToBottom(false);
    }
    function onDelta(chunk) {
      acc += chunk;
      if (!pending) { pending = true; requestAnimationFrame(flush); }
    }

    API.chat(chatModelName(), apiMessages(), onDelta, state.abort.signal)
      .then(function () {
        assistant.content = acc;
        bubble.classList.remove('caret');
        bubble.innerHTML = window.AliceMD ? window.AliceMD.render(acc || '') : escapeText(acc);
        if (!acc) bubble.appendChild(h('span', { class: 'hero-sub', text: '' }));
        finishStream();
      })
      .catch(function (err) {
        bubble.classList.remove('caret');
        if (!acc) {
          assistant.content = t('chat.error');
          bubble.innerHTML = '';
          bubble.appendChild(h('p', { class: 'dl-err', text: t('chat.error') + ' (' + (err && err.message || 'error') + ')' }));
        } else {
          assistant.content = acc;
        }
        finishStream();
      });
  }

  // Agent turn: interleaved prose bubbles + live tool cards via /alice/agent_stream.
  function sendAgentTurn(thread) {
    var assistant = { role: 'assistant', content: '', steps: [] };
    state.messages.push(assistant);
    var msgNode = h('div', { class: 'msg assistant' }, [
      h('div', { class: 'avatar mark', html: MARK_SVG }),
    ]);
    var wrap = h('div', { class: 'agent-turn' });
    msgNode.appendChild(wrap);
    var thinking = h('div', { class: 'agent-thinking', text: t('agent.working') });
    wrap.appendChild(thinking);
    thread.appendChild(msgNode);
    scrollToBottom(true);

    setStreaming(true);
    state.abort = new AbortController();

    var steps = assistant.steps;
    var curTextNode = null, curTextStep = null, liveRaw = '';
    var curToolNode = null, curToolStep = null;
    var pending = false;

    function clearThinking() {
      if (thinking && thinking.parentNode) thinking.parentNode.removeChild(thinking);
      thinking = null;
    }
    function ensureTextNode() {
      if (curTextNode) return;
      clearThinking();
      curTextStep = { kind: 'text', text: '' };
      steps.push(curTextStep);
      curTextNode = h('div', { class: 'bubble md caret' });
      wrap.appendChild(curTextNode);
    }
    function flushText() {
      pending = false;
      if (!curTextNode) return;
      curTextStep.text = liveRaw;
      curTextNode.innerHTML = window.AliceMD ? window.AliceMD.render(liveRaw) : escapeText(liveRaw);
      curTextNode.classList.add('caret');
      scrollToBottom(false);
    }
    function endRound() { curTextNode = null; curTextStep = null; liveRaw = ''; }

    var handlers = {
      delta: function (ev) {
        ensureTextNode();
        liveRaw += (ev.text || '');
        if (!pending) { pending = true; requestAnimationFrame(flushText); }
      },
      assistant_text: function (ev) {
        var clean = ev.text || '';
        if (clean) {
          ensureTextNode();
          curTextStep.text = clean;
          curTextNode.innerHTML = window.AliceMD ? window.AliceMD.render(clean) : escapeText(clean);
          curTextNode.classList.remove('caret');
          highlightWithin(curTextNode);
          assistant.content = clean;
        } else if (curTextNode) {
          // pure tool round, no prose — drop the empty text node + step
          if (curTextNode.parentNode) curTextNode.parentNode.removeChild(curTextNode);
          var idx = steps.indexOf(curTextStep);
          if (idx >= 0) steps.splice(idx, 1);
        }
        endRound();
      },
      tool_start: function (ev) {
        clearThinking();
        curToolStep = { kind: 'tool', tool: ev.tool, input: ev.input || '', output: '', running: true, ok: false };
        steps.push(curToolStep);
        curToolNode = renderToolCard(curToolStep);
        wrap.appendChild(curToolNode);
        scrollToBottom(false);
      },
      tool_output: function (ev) {
        if (!curToolStep) return;
        curToolStep.running = false;
        curToolStep.ok = !!ev.ok;
        curToolStep.output = ev.output || '';
        curToolStep.exit_code = ev.exit_code;
        var nc = renderToolCard(curToolStep);
        if (curToolNode && curToolNode.parentNode) curToolNode.parentNode.replaceChild(nc, curToolNode);
        curToolNode = nc; curToolStep = null;
        scrollToBottom(false);
      },
      agent_step: function () { endRound(); },
      error: function (ev) {
        clearThinking();
        wrap.appendChild(h('p', { class: 'dl-err', text: ev.message || t('chat.error') }));
      },
      done: function () {},
    };

    API.agentStream(apiMessages(), handlers, state.abort.signal)
      .then(function () {
        clearThinking();
        if (curTextNode) curTextNode.classList.remove('caret');
        finishStream();
      })
      .catch(function (err) {
        clearThinking();
        if (curTextNode) curTextNode.classList.remove('caret');
        var m = (err && err.message === 'AGENT_MODE_OFF')
          ? t('set.agent.desc')
          : (t('chat.error') + ' (' + ((err && err.message) || 'error') + ')');
        wrap.appendChild(h('p', { class: 'dl-err', text: m }));
        finishStream();
      });
  }

  function apiMessages() {
    // send the running transcript EXCEPT the trailing empty assistant placeholder
    return state.messages
      .filter(function (m, idx) { return !(idx === state.messages.length - 1 && m.role === 'assistant' && !m.content); })
      .map(function (m) { return { role: m.role, content: m.content }; });
  }

  function finishStream() {
    setStreaming(false);
    state.abort = null;
    persist();
    scrollToBottom(false);
  }
  function stopStream() {
    if (state.abort) { try { state.abort.abort(); } catch (_) {} }
  }
  function setStreaming(on) {
    state.streaming = on;
    var send = el('sendBtn');
    if (!send) return;
    send.innerHTML = on ? IC.stop : IC.send;
    send.setAttribute('aria-label', on ? t('chat.stop') : t('chat.send'));
    send.disabled = false;
    if (!on) autoGrow();
  }

  function newChat() {
    if (state.streaming) stopStream();
    state.messages = [];
    persist();
    renderThread();
    var ta = el('composerInput');
    if (ta) { ta.value = ''; autoGrow(); ta.focus(); }
  }

  function persist() {
    try { localStorage.setItem('alice-lean-chat', JSON.stringify(state.messages)); } catch (_) {}
  }
  function restore() {
    try {
      var raw = localStorage.getItem('alice-lean-chat');
      if (raw) {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) {
          // drop any trailing empty assistant turn
          while (arr.length && arr[arr.length - 1].role === 'assistant' && !arr[arr.length - 1].content) arr.pop();
          state.messages = arr;
        }
      }
    } catch (_) {}
  }

  function enterChat() {
    el('setup').classList.add('hidden');
    el('chat').classList.remove('hidden');
    buildChatShell();
    restore();
    renderThread();
    updateModelPill();
    updateAgentIndicator();
    var ta = el('composerInput');
    if (ta) { autoGrow(); ta.focus(); }
  }
  function updateModelPill() {
    var n = el('modelPillName');
    if (n) n.textContent = activeDisplayName();
  }
  function updateAgentIndicator() {
    var b = el('agentBadge');
    if (b) b.classList.toggle('hidden', !isAgentMode());
  }

  /* ============================ SETTINGS ================================== */
  function openSettings() {
    _settingsOpen = true;
    var overlay = h('div', { class: 'overlay', 'data-settings': '1' });
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeOverlay(overlay); });

    var sheet = h('div', { class: 'sheet' });
    var head = h('div', { class: 'sheet-head' }, [
      h('span', { class: 'sheet-title', text: t('set.title') }),
      (function () {
        var b = h('button', { class: 'icon-btn', type: 'button', 'aria-label': t('common.close'), html: IC.close });
        b.addEventListener('click', function () { closeOverlay(overlay); });
        return b;
      })(),
    ]);
    sheet.appendChild(head);

    // --- language ---
    var langSec = h('div', { class: 'sheet-section' });
    langSec.appendChild(h('div', { class: 'sheet-label', text: t('lang.label') }));
    var langRow = h('div', { class: 'toggle-row' });
    langRow.appendChild(h('div', { class: 'sheet-desc', text: t('lang.switch') }));
    langRow.appendChild(buildLangSwitch('lang-seg-sheet'));
    langSec.appendChild(langRow);
    sheet.appendChild(langSec);

    // --- model picker ---
    var modelSec = h('div', { class: 'sheet-section' });
    modelSec.appendChild(h('div', { class: 'sheet-label', text: t('set.model') }));
    var listMount = h('div', { id: 'modelList' });
    modelSec.appendChild(listMount);
    sheet.appendChild(modelSec);
    renderModelList(listMount);

    // --- context slider ---
    var ctxSec = h('div', { class: 'sheet-section', id: 'ctxSection' });
    sheet.appendChild(ctxSec);
    renderContextSlider(ctxSec);

    // --- agent mode ---
    var agentSec = h('div', { class: 'sheet-section' });
    agentSec.appendChild(h('div', { class: 'sheet-label', text: t('set.agent') }));
    var agentRow = h('div', { class: 'toggle-row' });
    agentRow.appendChild(h('div', { class: 'sheet-desc', text: t('set.agent.desc') }));
    var sw = h('label', { class: 'switch' });
    var swInput = h('input', { type: 'checkbox' });
    sw.appendChild(swInput);
    sw.appendChild(h('span', { class: 'track' }));
    agentRow.appendChild(sw);
    agentSec.appendChild(agentRow);
    var agentNote = h('div', { class: 'muted', text: '' });
    agentSec.appendChild(agentNote);
    sheet.appendChild(agentSec);

    var mode = state.mode || {};
    swInput.checked = !!mode.agent_mode;
    if (mode.locked) {
      swInput.disabled = true;
      agentNote.textContent = t('set.agent.locked');
    } else if (mode.agent_mode && mode.restart_required_for_full) {
      agentNote.textContent = t('set.agent.restart');
    }
    swInput.addEventListener('change', function () {
      if (swInput.checked) {
        // require risk modal before turning ON
        swInput.checked = false;
        showRiskModal(function () {
          API.setMode(true, true).then(function (res) {
            if (res.ok) {
              state.mode = res.body;
              swInput.checked = !!res.body.agent_mode;
              agentNote.textContent = res.body.restart_required_for_full ? t('set.agent.restart') : '';
              updateAgentIndicator();
            } else if (res.status === 409) {
              state.mode = res.body;
              agentNote.textContent = t('set.agent.locked');
            } else {
              toast(t('chat.error'));
            }
          });
        });
      } else {
        API.setMode(false, false).then(function (res) {
          if (res.ok) { state.mode = res.body; agentNote.textContent = ''; updateAgentIndicator(); }
        });
      }
    });

    // --- earn ---
    var earnSec = h('div', { class: 'sheet-section' });
    earnSec.appendChild(h('div', { class: 'sheet-label', text: t('set.earn') }));
    earnSec.appendChild(h('div', { class: 'sheet-desc', text: t('set.earn.desc') }));
    var earnBtnWrap = h('div', { style: 'margin-top:12px' });
    var earnBtn = h('button', { class: 'btn', type: 'button', text: t('set.earn.open') });
    earnBtnWrap.appendChild(earnBtn);
    earnSec.appendChild(earnBtnWrap);
    earnSec.appendChild(h('div', { class: 'earn-honest', text: t('set.earn.honest') }));
    sheet.appendChild(earnSec);

    API.earnStatus().then(function (st) {
      var installed = st && st.miner && st.miner.installed;
      earnBtn.textContent = installed ? t('set.earn.open') : t('set.earn.get');
      earnBtn.onclick = function () {
        if (installed) {
          API.openMiner().then(function (res) {
            var body = res.body || {};
            if (body.launched) { toast(t('set.earn.open')); }
            else if (body.fallback_url) { window.open(body.fallback_url, '_blank', 'noopener'); }
            else if (st.download_url) { window.open(st.download_url, '_blank', 'noopener'); }
          });
        } else if (st.download_url) {
          window.open(st.download_url, '_blank', 'noopener');
        }
      };
    }).catch(function () {
      earnBtn.onclick = function () { toast(t('chat.error')); };
    });

    // done
    var doneBtn = h('button', { class: 'btn btn-primary', type: 'button', style: 'width:100%', text: t('set.done') });
    doneBtn.addEventListener('click', function () { closeOverlay(overlay); });
    sheet.appendChild(doneBtn);

    overlay.appendChild(sheet);
    document.body.appendChild(overlay);
  }

  function closeOverlay(overlay) {
    if (overlay.getAttribute && overlay.getAttribute('data-settings') === '1') _settingsOpen = false;
    overlay.style.animation = 'fade .15s ease reverse forwards';
    setTimeout(function () { overlay.remove(); }, 140);
  }

  function renderModelList(mount) {
    mount.innerHTML = '';
    // de-dupe by id, preferring the active/recommended/ready entry per id.
    var seen = {};
    var order = [];
    state.models.forEach(function (m) {
      if (seen[m.id]) {
        // keep a better candidate: ready > recommended > existing
        var prev = seen[m.id];
        var better = (m.state === 'ready' && prev.state !== 'ready') ||
          (m.active && !prev.active);
        if (better) seen[m.id] = m;
        return;
      }
      seen[m.id] = m;
      order.push(m.id);
    });

    // Split the general tiers from the DEDICATED roleplay line (family ==
    // 'roleplay' → Alice RP / Alice RP Lite). RP is its own product line, so it
    // gets its own labeled section instead of being mixed in at the bottom.
    var general = [], roleplay = [];
    order.forEach(function (id) {
      (seen[id].family === 'roleplay' ? roleplay : general).push(seen[id]);
    });

    function appendRow(m) {
      var isActive = (state.current && state.current.id === m.id) || m.active;
      var row = h('div', { class: 'opt-row' + (isActive ? ' active' : '') });
      var main = h('div', { class: 'opt-main' }, [
        h('div', { class: 'opt-name', text: m.display_name }),
        m.tagline ? h('div', { class: 'opt-tag', text: m.tagline }) : null,
      ]);
      row.appendChild(main);
      if (isActive) {
        row.appendChild(h('span', { class: 'opt-check mark', html: IC.check }));
      } else if (m.state === 'ready') {
        row.appendChild(h('span', { class: 'opt-state ready', text: t('dl.done') }));
      } else {
        row.appendChild(h('span', { class: 'opt-state dl', text: m.download_size_human || '' }));
      }
      if (!isActive) {
        row.addEventListener('click', function () { switchModel(m, mount); });
      }
      mount.appendChild(row);
    }

    general.forEach(appendRow);

    if (roleplay.length) {
      // Dedicated roleplay divider, then the RP tiers (Alice RP / Alice RP Lite).
      mount.appendChild(h('div', { class: 'opt-group' }, [
        h('span', { class: 'opt-group-label', text: t('set.model.rp') }),
        h('span', { class: 'opt-group-desc', text: t('set.model.rp.desc') }),
      ]));
      roleplay.forEach(appendRow);
    }
  }

  function switchModel(m, mount) {
    var rows = mount.querySelectorAll('.opt-row');
    for (var i = 0; i < rows.length; i++) rows[i].classList.add('busy');

    function doLoad() {
      return API.load(m.id, { confirm: true }).then(function (res) {
        if (res.ok && res.body && res.body.current) {
          state.current = res.body.current;
          return refreshModels().then(function () {
            renderModelList(mount);
            updateModelPill();
            renderContextSlider(el('ctxSection'));
            toast(activeDisplayName());
          });
        }
        throw new Error((res.body && res.body.error) || 'load failed');
      });
    }

    if (m.state === 'ready') {
      doLoad().catch(function () {
        for (var i = 0; i < rows.length; i++) rows[i].classList.remove('busy');
        toast(t('chat.error'));
      });
    } else {
      // needs download — run ensure with a tiny inline progress on the row
      var stateEl = mount.querySelector('.opt-row.busy .opt-state') || null;
      API.ensure(m.id, function (ev) {
        var pct = typeof ev.fraction === 'number' && ev.fraction >= 0 ? Math.round(ev.fraction * 100) + '%' : t('dl.downloading');
        // reflect on the matching row's state cell
        var allRows = mount.querySelectorAll('.opt-row');
        for (var i = 0; i < allRows.length; i++) {
          var nameEl = allRows[i].querySelector('.opt-name');
          if (nameEl && nameEl.textContent === m.display_name) {
            var s = allRows[i].querySelector('.opt-state');
            if (s) { s.textContent = pct; s.className = 'opt-state dl'; }
          }
        }
      }).then(doLoad).catch(function (err) {
        for (var i = 0; i < rows.length; i++) rows[i].classList.remove('busy');
        toast((err && err.message) || t('chat.error'));
      });
    }
  }

  function renderContextSlider(sec) {
    if (!sec) return;
    sec.innerHTML = '';
    var m = findModel(activeModelKey());
    var ctx = (m && m.context) || (state.current && state.current.context);
    if (!ctx) return;
    sec.appendChild(h('div', { class: 'sheet-label', text: t('set.context') }));
    sec.appendChild(h('div', { class: 'sheet-desc', text: t('set.context.desc') }));

    var min = ctx.min || 2048, max = ctx.max || 8192;
    var cur = ctx.chosen || ctx.default || min;
    var rowWrap = h('div', { class: 'slider-row', style: 'margin-top:14px' });
    // step in powers-of-two-ish increments using the slider as a raw token value
    var slider = h('input', { type: 'range', min: String(min), max: String(max), step: '1024', value: String(clampStep(cur, min, max, 1024)) });
    var valEl = h('span', { class: 'slider-val', text: fmtCtx(slider.value) });
    rowWrap.appendChild(slider);
    rowWrap.appendChild(valEl);
    sec.appendChild(rowWrap);

    var commitTimer = null;
    slider.addEventListener('input', function () {
      valEl.textContent = fmtCtx(slider.value);
    });
    slider.addEventListener('change', function () {
      var n = parseInt(slider.value, 10);
      n = Math.max(min, Math.min(max, n));
      if (commitTimer) clearTimeout(commitTimer);
      commitTimer = setTimeout(function () {
        API.setContext(activeModelKey(), n).then(function (res) {
          if (res.ok) {
            // reflect chosen back into local model + current
            if (m && m.context) m.context.chosen = n;
            if (state.current && state.current.context) state.current.context.chosen = n;
          }
        });
      }, 250);
    });
  }
  function clampStep(v, min, max, step) {
    v = Math.round(v / step) * step;
    return Math.max(min, Math.min(max, v));
  }
  function fmtCtx(v) {
    var n = parseInt(v, 10);
    if (n >= 1024) return (n / 1024 % 1 === 0 ? (n / 1024) : (n / 1024).toFixed(1)) + 'K';
    return String(n);
  }

  /* ---- risk modal (shown before enabling Agent mode) -------------------- */
  function showRiskModal(onConfirm) {
    var overlay = h('div', { class: 'overlay', style: 'z-index:120' });
    var sheet = h('div', { class: 'sheet', style: 'max-width:420px' });
    sheet.appendChild(h('div', { class: 'sheet-head' }, [
      h('span', { class: 'sheet-title', text: t('risk.title') }),
    ]));
    sheet.appendChild(h('p', { class: 'sheet-desc', style: 'margin-bottom:20px', text: t('risk.body') }));
    var btns = h('div', { style: 'display:flex; gap:10px; justify-content:flex-end' });
    var cancel = h('button', { class: 'btn', type: 'button', text: t('risk.cancel') });
    var confirm = h('button', { class: 'btn btn-primary', type: 'button', text: t('risk.confirm') });
    cancel.addEventListener('click', function () { closeOverlay(overlay); });
    confirm.addEventListener('click', function () { closeOverlay(overlay); onConfirm(); });
    btns.appendChild(cancel); btns.appendChild(confirm);
    sheet.appendChild(btns);
    overlay.appendChild(sheet);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeOverlay(overlay); });
    document.body.appendChild(overlay);
  }

  /* ============================ BOOTSTRAP ================================ */
  function refreshModels() {
    return API.models().then(function (data) {
      state.models = (data && data.models) || [];
      if (data && data.device) state.device = data.device;
      return state.models;
    });
  }

  function boot() {
    // Reflect the resolved UI language onto <html lang> from the first paint.
    document.documentElement.setAttribute('lang', _lang);
    // Kick off the independent reads in parallel.
    var pDevice = API.device().catch(function () { return null; });
    var pModels = refreshModels().catch(function () { return []; });
    var pCurrent = API.current().catch(function () { return { current: null }; });
    var pMode = API.mode().catch(function () { return null; });
    var pRec = API.recommend().catch(function () { return null; });

    Promise.all([pDevice, pModels, pCurrent, pMode, pRec]).then(function (vals) {
      if (vals[0]) state.device = vals[0];
      state.current = (vals[2] && vals[2].current) || null;
      state.mode = vals[3];
      var rec = vals[4] && vals[4].recommended;
      if (vals[4] && vals[4].device && !state.device) state.device = vals[4].device;

      // Decide first-run vs chat:
      //   chat   if a model is already loaded (current != null)
      //   setup  otherwise (download the recommended tier, or lite as fallback)
      if (state.current) {
        enterChat();
        return;
      }

      // No current model — first run. Per the product spec the DEFAULT is Alice
      // Lite (small, instant start) regardless of device tier; bigger tiers (incl.
      // the device-recommended one) stay reachable via "Choose a different model".
      // And ALWAYS prefer an already-downloaded model so we never force a fresh
      // multi-GB download when something usable is already on disk.
      var readyModels = state.models.filter(function (m) { return m.state === 'ready'; });
      var target;
      if (readyModels.length) {
        target = readyModels.filter(function (m) { return m.id === 'lite'; })[0] || readyModels[0];
      } else {
        target = findModel('lite') || rec || pickFallbackRecommend();
      }
      var matching = findModel(target.id);
      if (matching && matching.state === 'ready') {
        // already downloaded — load and go (no download UI needed)
        API.load(target.id, { confirm: true }).then(function (res) {
          if (res.ok && res.body && res.body.current) {
            state.current = res.body.current;
            enterChat();
          } else {
            renderSetup(target);
          }
        }).catch(function () { renderSetup(target); });
      } else {
        renderSetup(target);
      }
    });
  }

  function pickFallbackRecommend() {
    // prefer "lite" if present, else the first model.
    var lite = findModel('lite');
    if (lite) return lite;
    return state.models[0] || { id: 'lite', display_name: 'Alice Lite', download_size_human: '2.2 GB', tagline: '' };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
