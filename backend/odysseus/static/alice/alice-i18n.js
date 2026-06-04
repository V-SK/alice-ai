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
    // privacy-P2 (privacy-audit): chat history IS persisted to a LOCAL SQLite
    // DB (data/app.db) so you can scroll back — it never leaves the device, but
    // "no logging" read as "nothing is written down", which is inaccurate.
    // Honest copy: no telemetry, no cloud, chats saved only on this device.
    'chat.note':       { en: 'Alice runs <b>on this device</b>. No telemetry, no cloud — your chats are saved only on this device, no credit. <span class="a-pending">待发放</span> applies only to opt-in Earn.',
                         zh: 'Alice 在<b>本机运行</b>。无遥测、无云端——聊天记录只保存在本机、无计费。<span class="a-pending">待发放</span> 仅适用于自愿参与的「赚取」。' },
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

    // ---- Earn ALICE bridge (M7 — design 04). Credit-only, no $, no rate. ----
    'earn.nav':         { en: 'Earn ALICE', zh: '赚取 ALICE' },
    // entry card (chat empty-state)
    'earn.entry.title': { en: 'Earn ALICE with your computer', zh: '用你的电脑赚取 ALICE' },
    'earn.entry.sub':   { en: 'Run the Alice Miner alongside chat — rewards go to your Alice address.',
                          zh: '在聊天之外运行 Alice 矿工——奖励发放到你的 Alice 地址。' },
    'earn.entry.open':  { en: 'Open Alice Miner', zh: '打开 Alice 矿工' },
    'earn.entry.get':   { en: 'Get the Alice Miner', zh: '获取 Alice 矿工' },
    'earn.entry.learn': { en: 'Learn more', zh: '了解更多' },
    // reward address row
    'earn.addr.label':  { en: 'Rewards to', zh: '奖励发放至' },
    'earn.addr.copy':   { en: 'Copy address', zh: '复制地址' },
    'earn.addr.copied': { en: 'Copied', zh: '已复制' },
    'earn.addr.watch':  { en: 'watch-only', zh: '仅查看' },
    'earn.addr.none':   { en: 'Set a reward address in the Alice Miner or Wallet.',
                          zh: '请在 Alice 矿工或钱包中设置奖励地址。' },
    // per-state headlines/hints
    'earn.s.set.head':  { en: 'Almost there — set a reward address', zh: '就差一步——设置奖励地址' },
    'earn.s.set.hint':  { en: 'Open the Alice Miner; its setup creates your reward address.',
                          zh: '打开 Alice 矿工，其引导流程会创建你的奖励地址。' },
    'earn.s.ready.head':{ en: 'Earn ALICE by mining', zh: '通过挖矿赚取 ALICE' },
    'earn.s.haveaddr.head': { en: 'Get the Alice Miner to start earning', zh: '获取 Alice 矿工开始赚取' },
    // launch feedback / fallback
    'earn.open.ok':     { en: 'Alice Miner is opening…', zh: 'Alice 矿工正在打开…' },
    'earn.open.fail':   { en: "Couldn't open it — open the download page instead.",
                          zh: '无法打开——请改用下载页面。' },
    // honest credit-only footnote (NO $, NO rate)
    'earn.foot':        { en: 'Mining rewards are credited to your Alice address and shown as <span class="a-pending">pending · 待发放</span> until distributed. No cash, no fees here.',
                          zh: '挖矿奖励将记入你的 Alice 地址，在发放前显示为 <span class="a-pending">待发放 · pending</span>。此处无现金、无手续费。' },

    // ---- the Alice story (block 2, design 04 §4) — honest, no hype/numbers ----
    'story.eyebrow':    { en: 'Why Alice', zh: '关于 Alice' },
    'story.1.h':        { en: "Alice's own models", zh: 'Alice 自有模型' },
    'story.1.b':        { en: 'Alice ships its own model family — Alice, Alice Lite, Alice Pro — built for privacy. They run on your hardware, and chat never leaves this device.',
                          zh: 'Alice 拥有自己的模型家族——Alice、Alice Lite、Alice Pro——以隐私为先。它们在你的硬件上运行，聊天不会离开本机。' },
    'story.2.h':        { en: 'A network, not a company', zh: '一个网络，而非一家公司' },
    'story.2.b':        { en: 'Alice is a protocol: people contribute compute and earn for it. There is no central owner — the network belongs to the people who run it.',
                          zh: 'Alice 是一个协议：人们贡献算力并因此获得回报。没有中心化的拥有者——网络属于运行它的人。' },
    'story.3.h':        { en: 'Where you fit in', zh: '你的位置' },
    'story.3.b':        { en: 'Today you can mine with the Alice Miner (above). Soon you will be able to lend your idle GPU to run Alice for others — you are already running it locally.',
                          zh: '现在你可以用 Alice 矿工挖矿（见上）。很快你将能把空闲的 GPU 借给网络，为他人运行 Alice——而你本就已在本机运行它。' },

    // ---- phase-2 GPU contribute teaser (block 3, design 04 §6) — INERT ----
    'gpu.title':        { en: 'Contribute your GPU to Alice', zh: '把你的 GPU 贡献给 Alice' },
    'gpu.soon':         { en: 'coming soon', zh: '即将推出' },
    'gpu.body':         { en: 'Soon you will be able to share your idle GPU with Alice’s inference network and earn ALICE for verified work. We’re finishing the network and the fairness checks that make rewards trustworthy.',
                          zh: '很快你将能把空闲的 GPU 共享给 Alice 的推理网络，并因可验证的工作赚取 ALICE。我们正在完善网络与让奖励可信的公平性校验。' },
    'gpu.priv':         { en: 'Your own chats are never shared — contribution will be a separate, opt-in mode.',
                          zh: '你自己的聊天永远不会被共享——贡献将是一个独立、自愿开启的模式。' },

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
