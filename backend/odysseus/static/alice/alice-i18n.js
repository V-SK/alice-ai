/* ============================================================================
   Alice AI — EN / 中 i18n table (M3).
   Canonical strings from docs/design/02-xiaobai-ux.md §7.2 + the strings the
   first-run / picker / chat-chrome overlays need. 中 is the primary audience
   voice, EN first-class. Jargon-free (the §7.1 banned-words rule).

   Model TIER NAMES are brand names — NOT translated (Alice / Alice Lite /
   Alice Pro / Alice RP in both languages). Descriptors + chrome are localized.

   Persistence: localStorage['alice-lang'] = 'en' | 'zh'. Default follows the
   browser/OS locale → falls back to 中 then EN.

   Usage (global, no bundler):
     window.AliceI18n.lang()            -> 'en' | 'zh'
     window.AliceI18n.setLang('zh')     -> persist + return
     window.AliceI18n.t('welcome.title')-> the string in the current language
   ============================================================================ */
(function () {
  'use strict';

  var STR = {
    // ---- first-run flow ----
    'welcome.title':   { en: "Hi, I'm Alice", zh: '你好，我是 Alice' },
    'welcome.sub':     { en: 'An AI that runs on your own computer — fully private, free forever.',
                         zh: '在你自己的电脑上运行的 AI，完全私密、永久免费。' },
    'welcome.cta':     { en: 'Start', zh: '开始' },
    'detect.busy':     { en: 'Getting to know your computer…', zh: '正在了解你的电脑…' },
    'detect.ok':       { en: 'Great — your computer can run Alice smoothly.', zh: '很好，你的电脑可以流畅运行 Alice。' },
    'download.h':      { en: 'Getting Alice ready', zh: '正在准备你的 Alice' },
    'download.sub':    { en: 'We picked the model that fits your machine. This downloads <b>once</b> — after that, Alice works fully offline.',
                         zh: '我们挑选了适合你电脑的模型。<b>只需下载一次</b>，之后 Alice 完全离线运行。' },
    'download.eyebrow':{ en: 'Step 2 of 3 · download', zh: '第 2 步，共 3 步 · 下载' },
    'download.detected': { en: 'Detected', zh: '已检测到' },
    'download.recommended': { en: 'recommended', zh: '推荐' },
    'download.change': { en: 'change', zh: '更换' },
    'download.choose': { en: 'Choose a different model', zh: '选择其它模型' },
    'download.live':   { en: 'Downloading', zh: '下载中' },
    'download.left':   { en: 'about <span class="mono">2 min</span> left', zh: '剩余约 <span class="mono">2 分钟</span>' },
    'download.bg':     { en: 'Download in background', zh: '后台下载' },
    'download.cancel': { en: 'Cancel', zh: '取消' },
    'download.foot':   { en: 'Runs 100% on your device — <span class="a-pending">private &amp; offline</span>. No account, no cloud, no data leaves your machine.',
                         zh: '完全在你的设备上运行——<span class="a-pending">私密 &amp; 离线</span>。无需账号、无云端，数据不离开你的电脑。' },
    'ready.title':     { en: "You're all set.", zh: '准备好了！' },
    'ready.sub':       { en: 'Alice is ready on this device. Conversations stay private.', zh: 'Alice 已在本机就绪。对话完全私密。' },
    'ready.cta':       { en: 'Start chatting', zh: '开始聊天' },

    // ---- top-bar / chrome ----
    'status.ready':    { en: 'Local · ready', zh: '本机 · 就绪' },
    'status.setup':    { en: 'Setting up', zh: '正在准备' },
    'status.thinking': { en: 'Thinking…', zh: '思考中…' },
    'status.firstload':{ en: 'Loading for the first time…', zh: '首次加载中…' },
    'chrome.private':  { en: 'Private · stays on device', zh: '私密 · 保留在本机' },
    'chrome.meta':     { en: 'Alice', zh: 'Alice' },

    // ---- chat hero / empty state ----
    'chat.greeting':   { en: "Hi, I'm Alice.", zh: '你好，我是 Alice。' },
    'chat.trust':      { en: 'Runs on your computer · Fully private · Free forever',
                         zh: '在本机运行 · 完全私密 · 永久免费' },
    'chat.placeholder':{ en: 'Message Alice…', zh: '写点什么…（按 Enter 发送）' },
    'chat.note':       { en: 'Alice runs <b>on this device</b>. Conversations are private — no network, no logging, no credit. <span class="a-pending">待发放</span> applies only to opt-in Earn.',
                         zh: 'Alice 在<b>本机运行</b>。对话完全私密——不联网、不记录、无计费。<span class="a-pending">待发放</span> 仅适用于自愿参与的「赚取」。' },
    'chat.hint':       { en: 'Advanced tools, web &amp; agents live under <b>+</b>', zh: '高级工具、联网与智能体在 <b>+</b> 中' },

    // ---- starter chips (§7.2) ----
    'starter.email':   { en: 'Write a leave-request email', zh: '写一封请假邮件' },
    'starter.explain': { en: 'Explain blockchain simply', zh: '用大白话解释区块链' },
    'starter.code':    { en: 'Help me fix this code', zh: '帮我改这段代码' },
    'starter.plan':    { en: 'Plan a 3-day trip', zh: '规划一次三天的旅行' },

    // ---- model tiers (NAMES not translated; descriptors are) ----
    'model.lite':      { en: 'Alice Lite', zh: 'Alice Lite' },
    'model.lite.ds':   { en: 'Fastest · light on memory', zh: '最快 · 占用内存少' },
    'model.std':       { en: 'Alice', zh: 'Alice' },
    'model.std.ds':    { en: 'Balanced · best all-rounder', zh: '均衡 · 全能首选' },
    'model.pro':       { en: 'Alice Pro', zh: 'Alice Pro' },
    'model.pro.ds':    { en: 'Most capable · large memory', zh: '最强 · 需要大内存' },
    'model.rp':        { en: 'Alice RP', zh: 'Alice RP' },
    'model.rp.ds':     { en: 'Roleplay · opt-in', zh: '角色扮演 · 自愿开启' },
    'model.current':   { en: 'current', zh: '当前' },
    'model.ready':     { en: 'Ready', zh: '就绪' },
    'model.download':  { en: 'download', zh: '下载' },
    'picker.h':        { en: 'Choose a model · all run on this device', zh: '选择模型 · 全部在本机运行' },
    'picker.foot':     { en: 'Names are Alice tiers — your hardware picks the fit.', zh: '这些是 Alice 的型号——由你的硬件决定适配。' },

    // ---- per-model context-size control (4k..model max) ----
    'context.label':   { en: 'Context length', zh: '上下文长度' },
    'context.hint':    { en: 'Longer context remembers more — uses more memory.', zh: '上下文越长，记得越多——占用内存也越多。' },
    'context.warn':    { en: 'This length is tight for your memory — Alice may run slowly.', zh: '该长度对你的内存偏紧——Alice 可能会变慢。' },
    'download.verifying': { en: 'Verifying download…', zh: '正在校验下载…' },

    // ---- model gate (honest, before loading a model too big) ----
    'gate.warn':       { en: 'This model is tight for your computer and may run slowly. Continue?', zh: '该模型对你的电脑偏紧、可能较慢。仍要继续吗？' },
    'gate.refuse':     { en: 'This model needs more memory than your computer has. Try a smaller Alice.', zh: '该模型所需内存超过你的电脑。请选择更小的 Alice。' },

    // ---- settings (Simple) ----
    'settings.model':   { en: 'Model', zh: '模型' },
    'settings.change':  { en: 'Change', zh: '更换' },
    'settings.language':{ en: 'Language', zh: '语言' },
    'settings.advanced':{ en: 'Advanced', zh: '高级' },
    'settings.advanced.eyebrow': { en: 'for power users', zh: '面向高级用户' },
    'settings.earn':    { en: 'Earn ALICE', zh: '赚取 ALICE' },

    // ---- errors (§7.2) ----
    'err.generic':     { en: 'Something went wrong. Alice will try again.', zh: '出了点问题，Alice 会重试。' },
    'err.copy':        { en: 'Copy details', zh: '复制详情' },
    'download.retry':  { en: 'Retry', zh: '重试' },
    'download.paused': { en: 'Paused — check your internet.', zh: '已暂停，请检查网络连接。' },
  };

  function detectDefault() {
    try {
      var nav = (navigator.language || navigator.userLanguage || '').toLowerCase();
      if (nav.indexOf('zh') === 0) return 'zh';
      if (nav.indexOf('en') === 0) return 'en';
    } catch (_) {}
    return 'zh'; // §7.3: fallback 中 then EN; 中 is the primary audience
  }

  var _lang = null;
  function lang() {
    if (_lang) return _lang;
    try { _lang = localStorage.getItem('alice-lang'); } catch (_) {}
    if (_lang !== 'en' && _lang !== 'zh') _lang = detectDefault();
    return _lang;
  }
  function setLang(l) {
    _lang = (l === 'en') ? 'en' : 'zh';
    try { localStorage.setItem('alice-lang', _lang); } catch (_) {}
    try { document.documentElement.setAttribute('lang', _lang === 'zh' ? 'zh-CN' : 'en'); } catch (_) {}
    return _lang;
  }
  function t(key) {
    var e = STR[key];
    if (!e) return key;
    return e[lang()] != null ? e[lang()] : (e.en != null ? e.en : key);
  }
  function other() { return lang() === 'zh' ? 'en' : 'zh'; }
  function label() { return lang() === 'zh' ? '中' : 'EN'; }       // shown on the toggle now
  function otherLabel() { return lang() === 'zh' ? 'EN' : '中'; }   // what it switches to

  window.AliceI18n = { lang: lang, setLang: setLang, t: t, other: other, label: label, otherLabel: otherLabel, STR: STR };
})();
