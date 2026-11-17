/* 🎤 Голосовий набір у Minerva (templates/admin/base_site.html): у полі, де пишуть текст — textarea, текстове поле
   від 180 px, редактор листа (contenteditable), — поки в ньому курсор, видно 🎤. Клік — говорите, текст пишеться туди,
   де курсор («кома», «крапка», «знак питання», «новий рядок» — розділові знаки й рядки); ще клік — стоп.

   Хто розпізнає (правий клік на 🎤, localStorage mvv-mode):
   - rag — RAG-ПК, наша модель Whisper (meetings/rag_pc/bench/dictate.py): звук з вибраного мікрофона (mvv-mic) пишеться
     в браузері, після «стоп» іде на <data-bench>/api/dictate, назад — текст; в інтернет не йде;
   - rag_clean — те саме + 🧹 DeepFilterNet на RAG-ПК (шумозаглушення браузера тоді вимкнено);
   - browser — як раніше: Web Speech API (Chrome — Google, Edge — Microsoft; processLocally, де вміє), мікрофон Windows
     за замовчуванням.
   За замовчуванням rag, коли адресу RAG-ПК передано (наради ввімкнено — core.context_processors.voice_bench), інакше
   browser. RAG-ПК недоступний (ping /api/dictate/ping) — автоматично браузер і про це сказано під полем.
   Мова (mvv-lang): «Автоматично» (RAG-ПК визначає сам; браузеру — мова інтерфейсу) або вибрана.
   Вимкнути — «Мої налаштування» → «🎤 Голосовий набір» (data-mv-voice="off" на <html>, core/ui_prefs.py);
   поле з data-voice="off" — без 🎤, з data-voice — завжди з ним. API: MVVoice {supported, start(el), stop()}. */
(function () {
  'use strict';
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var doc = document.documentElement;
  var script = document.currentScript;
  var BENCH = ((script && script.getAttribute('data-bench')) || '').replace(/\/+$/, '');
  var CAN_REC = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (BENCH && !/^https?:\/\//i.test(BENCH)) BENCH = '';
  if (window.MVVoice || (!SR && !(BENCH && CAN_REC))) return;

  var LANGS = [['uk-UA', 'Українська'], ['de-DE', 'Deutsch'], ['en-US', 'English'], ['ru-RU', 'Русский'],
               ['pl-PL', 'Polski']];
  var MODES = {
    rag: '🖥 RAG-ПК — наша модель (Whisper)',
    rag_clean: '🖥 RAG-ПК + 🧹 чистка шуму',
    browser: '🌐 Браузер (Google / Microsoft)'
  };
  var KEY = 'mvv-lang', MODE_KEY = 'mvv-mode', MIC_KEY = 'mvv-mic', SEEN = 'mvv-seen', SIZE = 28, RESTARTS = 30;
  var PING_OK_MS = 60000, PING_FAIL_MS = 15000, PING_TIMEOUT = 3000, SEND_TIMEOUT = 180000, MAX_REC_S = 180;
  // Chrome «доступ до локальної мережі» (Local Network Access): сайт з інтернету -> RAG-ПК у мережі офісу лише з дозволу;
  // поки Chrome питає, запит чекає на відповідь людини
  var LNA_WAIT = 60000;
  // слова → розділові знаки (ціле слово; «новий рядок» у однорядковому полі — пробіл)
  var WORDS = {
    uk: [['новий абзац', '\n\n'], ['новий рядок', '\n'], ['знак питання', '?'], ['знак оклику', '!'],
         ['двокрапка', ':'], ['крапка з комою', ';'], ['кома', ','], ['крапка', '.']],
    ru: [['новый абзац', '\n\n'], ['новая строка', '\n'], ['вопросительный знак', '?'], ['восклицательный знак', '!'],
         ['двоеточие', ':'], ['точка с запятой', ';'], ['запятая', ','], ['точка', '.']],
    de: [['neuer absatz', '\n\n'], ['neue zeile', '\n'], ['fragezeichen', '?'], ['ausrufezeichen', '!'],
         ['doppelpunkt', ':'], ['semikolon', ';'], ['komma', ','], ['punkt', '.']],
    en: [['new paragraph', '\n\n'], ['new line', '\n'], ['question mark', '?'], ['exclamation mark', '!'],
         ['colon', ':'], ['semicolon', ';'], ['comma', ','], ['full stop', '.'], ['period', '.']],
    pl: [['nowy akapit', '\n\n'], ['nowa linia', '\n'], ['znak zapytania', '?'], ['wykrzyknik', '!'],
         ['dwukropek', ':'], ['średnik', ';'], ['przecinek', ','], ['kropka', '.']]
  };
  var NO_NAME = /(e-?mail|mail_to|url|href|link|phone|tel\b|iban|bic|sku|code|key|token|secret|passw|zip|postal|number|nr\b|qty|price|date|time|hs_|vat|tracking|mrn|json|html|css|script|regex|xml|sql)/i;

  var btn = null, tip = null, target = null, rec = null, listening = false, stopping = false, restarts = 0;
  var hideTimer = 0, tipTimer = 0, local = false, lastError = '';
  // RAG-ПК: стан зв'язку (ping) і поточний запис
  var rag = {state: '', why: '', ip: '', info: null, at: 0, pending: null};
  var take = null;             // {el, clean, stream, recorder, chunks, ctx, timer, meter, started, cancel}
  var fallback = false;        // цей запис — браузер, бо RAG-ПК недоступний
  var mics = null;             // [{id, label}] — після першого дозволу на мікрофон

  function off() { return doc.getAttribute('data-mv-voice') === 'off'; }
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* лише цього разу */ } }
  function mode() {
    var m = get(MODE_KEY);
    if (m === 'browser' && SR) return m;
    if ((m === 'rag' || m === 'rag_clean') && BENCH && CAN_REC) return m;
    return BENCH && CAN_REC ? 'rag' : 'browser';
  }
  function isRag(m) { return m === 'rag' || m === 'rag_clean'; }
  function langPick() {
    var l = get(KEY);
    if (l === 'auto' || (l && LANGS.some(function (x) { return x[0] === l; }))) return l;
    return 'auto';
  }
  function uiLang() {
    var ui = (doc.lang || 'uk').slice(0, 2).toLowerCase();
    for (var i = 0; i < LANGS.length; i++) if (LANGS[i][0].slice(0, 2) === ui) return LANGS[i][0];
    return 'uk-UA';
  }
  function lang() { var l = langPick(); return l === 'auto' ? uiLang() : l; }        // для браузера й «кома» / «крапка»
  function langName(l) {
    if (l === 'auto') return 'автоматично';
    for (var i = 0; i < LANGS.length; i++) if (LANGS[i][0] === l) return LANGS[i][1];
    return l;
  }

  // ── Де показувати 🎤 ──
  function ok(el) {
    if (!el || off() || el.disabled || el.readOnly) return false;
    var v = el.getAttribute && el.getAttribute('data-voice');
    if (v === 'off' || (el.closest && el.closest('[data-voice="off"]'))) return false;
    if (el.isContentEditable) {
      return !el.parentElement || !el.parentElement.isContentEditable;      // лише сам редактор, не його частини
    }
    if (v !== null && v !== undefined && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) return true;
    var name = [el.name, el.id, el.className].join(' ');
    if (el.tagName === 'TEXTAREA') {
      return !/(json|html|css|script|regex|xml|sql|secret|token|passw)/i.test(name) && !el.closest('.CodeMirror');
    }
    if (el.tagName !== 'INPUT') return false;
    var type = (el.getAttribute('type') || 'text').toLowerCase();
    if (type !== 'text' && type !== 'search') return false;
    if (el.inputMode === 'numeric' || el.inputMode === 'decimal' || el.autocomplete === 'one-time-code') return false;
    if (NO_NAME.test(name)) return false;
    return el.getBoundingClientRect().width >= 180;
  }

  // ── Кнопка й підказка ──
  function style() {
    var s = document.createElement('style');
    s.textContent =
      '#mvv-btn{position:fixed;z-index:2147483000;width:' + SIZE + 'px;height:' + SIZE + 'px;padding:0;border-radius:50%;' +
      'border:1px solid var(--border-strong,rgba(127,127,127,.55));background:var(--bg-card,var(--body-bg,#fff));' +
      'color:var(--text,var(--body-fg,#222));font:15px/1 system-ui,sans-serif;cursor:pointer;display:none;' +
      'box-shadow:0 1px 5px rgba(0,0,0,.28);opacity:.88;align-items:center;justify-content:center}' +
      '#mvv-btn.show{display:inline-flex}#mvv-btn:hover,#mvv-btn:focus-visible{opacity:1}' +
      '#mvv-btn.on{background:#c5221f;border-color:#c5221f;color:#fff;opacity:1;animation:mvv-pulse 1.3s ease-in-out infinite}' +
      '#mvv-btn.wait{background:#1f6fd1;border-color:#1f6fd1;color:#fff;opacity:1;cursor:progress}' +
      '#mvv-btn.fb{outline:3px solid #e8590c;outline-offset:1px}' +
      '@keyframes mvv-pulse{0%,100%{box-shadow:0 0 0 0 rgba(197,34,31,.55)}50%{box-shadow:0 0 0 7px rgba(197,34,31,0)}}' +
      'html[data-mv-motion=reduce] #mvv-btn.on{animation:none}' +
      '#mvv-tip{position:fixed;z-index:2147483000;max-width:min(440px,80vw);padding:5px 9px;border-radius:7px;' +
      'background:rgba(22,27,34,.94);color:#fff;font:12px/1.4 system-ui,sans-serif;display:none;pointer-events:none;' +
      'white-space:pre-wrap;box-shadow:0 2px 10px rgba(0,0,0,.3)}#mvv-tip.show{display:block}' +
      '#mvv-tip i{opacity:.75}#mvv-tip b{font-weight:600}';
    document.head.appendChild(s);
  }
  function build() {
    style();
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'mvv-btn';
    btn.setAttribute('translate', 'no');
    btn.setAttribute('data-mv-contrast', 'off');
    btn.textContent = '🎤';
    tip = document.createElement('div');
    tip.id = 'mvv-tip';
    tip.setAttribute('role', 'status');
    tip.setAttribute('aria-live', 'polite');
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });     // курсор лишається в полі
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (take && take.sending) return;
      if (listening) stop(); else start(target);
    });
    btn.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      menu(e);
    });
    title();
  }
  function title() {
    if (!btn) return;
    var m = mode();
    var t = listening ? 'Слухаю — клік: ' + (take ? 'розпізнати' : 'зупинити')
      : 'Голосовий набір — клік: говоріть, текст з’явиться там, де курсор';
    t += ' · ' + (isRag(m) ? (m === 'rag_clean' ? 'RAG-ПК + чистка' : 'RAG-ПК') : 'браузер') + ' · мова: ' +
      langName(langPick()) + ' (правий клік — налаштування)';
    btn.title = t;
    btn.setAttribute('aria-label', t);
    btn.setAttribute('aria-pressed', listening ? 'true' : 'false');
  }
  function host(el) { return (el && el.closest && el.closest('dialog[open]')) || document.body; }
  function place() {
    if (!btn || !target) return;
    if (!target.isConnected) { hide(true); return; }
    var r = target.getBoundingClientRect();
    if (!r.width || r.bottom < 0 || r.top > innerHeight) { btn.classList.remove('show'); tip.classList.remove('show'); return; }
    var x, y;
    if (target.tagName === 'INPUT') {
      x = r.right + 4 + SIZE <= innerWidth - 4 ? r.right + 4 : r.right - SIZE - 3;
      y = r.top + (r.height - SIZE) / 2;
    } else {
      x = r.right - SIZE - (target.tagName === 'TEXTAREA' ? 16 : 6);
      y = r.height < 2 * SIZE + 8 ? r.top + (r.height - SIZE) / 2 : r.bottom - SIZE - 6;
      if (r.bottom > innerHeight) y = Math.min(y, innerHeight - SIZE - 6);
    }
    btn.style.left = Math.max(4, x) + 'px';
    btn.style.top = Math.max(4, y) + 'px';
    btn.classList.add('show');
    if (tip.classList.contains('show')) {
      var below = r.bottom + 6, w = tip.offsetWidth, h = tip.offsetHeight;
      tip.style.left = Math.max(6, Math.min(r.right - w, innerWidth - w - 6)) + 'px';
      tip.style.top = (below + h > innerHeight - 4 ? Math.max(4, r.top - h - 6) : below) + 'px';
    }
  }
  function show(el) {
    if (!btn) build();
    clearTimeout(hideTimer);
    if (listening && el !== target) stop();
    if (take && take.sending) { target = el; place(); return; }   // текст іде в поле, де почали
    target = el;
    var h = host(el);
    if (btn.parentNode !== h) { h.appendChild(btn); h.appendChild(tip); }
    title();
    place();
  }
  function hide(force) {
    if (!btn || (listening && !force) || (take && take.sending)) return;
    if (listening) stop();
    btn.classList.remove('show');
    tip.classList.remove('show');
    target = null;
  }
  function say(parts, ms) {
    if (!tip) return;
    clearTimeout(tipTimer);
    tip.textContent = '';
    if (parts === null) { tip.classList.remove('show'); return; }
    parts.forEach(function (part) {
      var n = part[0] ? document.createElement(part[0]) : document.createTextNode(part[1]);
      if (part[0]) n.textContent = part[1];
      tip.appendChild(n);
    });
    tip.classList.add('show');
    place();
    if (ms) tipTimer = setTimeout(function () { tip.classList.remove('show'); }, ms);
  }

  // ── RAG-ПК: чи доступний ──
  function lnaState() {
    // '' — браузер такого дозволу не має (старіший Chrome, Firefox, Safari) або сторінка в тій самій мережі
    if (!navigator.permissions || !navigator.permissions.query) return Promise.resolve('');
    return navigator.permissions.query({name: 'local-network-access'})
      .catch(function () { return navigator.permissions.query({name: 'local-network'}); })
      .then(function (p) { return p && p.state || ''; }, function () { return ''; });
  }
  function ragCheck(force, onAsk) {
    if (!BENCH) return Promise.resolve(false);
    var age = Date.now() - rag.at;
    if (!force && rag.state === 'ok' && age < PING_OK_MS) return Promise.resolve(true);
    if (!force && rag.state === 'down' && rag.why !== 'lna_denied' && age < PING_FAIL_MS) return Promise.resolve(false);
    if (rag.pending) return rag.pending;
    rag.pending = lnaState().then(function (lna) {
      // «заборонено» блокує лише сайт з інтернету -> локальна мережа (Minerva за адресою в мережі офісу пускає),
      // тож спершу пробуємо; не вийшло й заборонено — ping скаже «дозвольте доступ до локальної мережі»
      rag.lna = lna;
      if (lna === 'prompt' && onAsk) onAsk();
      return ping(lna === 'prompt' ? LNA_WAIT : PING_TIMEOUT);
    }).then(function (good) { rag.at = Date.now(); rag.pending = null; return good; },
            function () { rag.at = Date.now(); rag.pending = null; return false; });
    return rag.pending;
  }
  function ping(wait) {
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, wait);
    return fetch(BENCH + '/api/dictate/ping', {mode: 'cors', credentials: 'omit', cache: 'no-store',
                                               signal: ctl ? ctl.signal : undefined})
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (r.ok && d.ok) { rag.state = 'ok'; rag.why = ''; rag.info = d; return true; }
          rag.state = 'down';
          rag.why = d.code === 'not_allowed' ? 'not_allowed' : 'error';
          rag.ip = d.ip || '';
          return false;
        });
      }, function () {
        rag.state = 'down';
        rag.why = rag.lna === 'prompt' ? 'lna_waiting' : 'unreachable';
        return lnaState().then(function (now) {             // людина могла саме натиснути «Блокувати»
          if (now === 'denied') rag.why = 'lna_denied';
          return false;
        });
      })
      .then(function (good) { clearTimeout(timer); return good; });
  }
  function warm() {
    // людина почала говорити — RAG-ПК вантажить модель уже зараз (після паузи це ~12 с), а не після «стоп»
    fetch(BENCH + '/api/dictate/ping?warm=1', {mode: 'cors', credentials: 'omit', cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) { rag.info = d; rag.state = 'ok'; rag.at = Date.now(); } })
      .catch(function () { /* скаже сам запит на розпізнавання */ });
  }
  function ragProblem() {
    if (rag.why === 'not_allowed') return 'цей ПК' + (rag.ip ? ' (' + rag.ip + ')' : '') + ' не в списку дозволених на RAG-ПК';
    if (rag.why === 'error') return 'RAG-ПК відповів помилкою';
    if (rag.why === 'lna_denied') {
      return 'браузер не дозволив Minerva доступ до локальної мережі, де RAG-ПК: 🔒 ліворуч від адреси сайту → ' +
        '«Доступ до локальної мережі» → Дозволити, потім оновіть сторінку';
    }
    if (rag.why === 'lna_waiting') return 'браузер так і не отримав дозволу на доступ до локальної мережі';
    return 'RAG-ПК не відповідає: вимкнений, ви не в мережі офісу або браузер ще не прийняв його сертифікат ' +
      '(правий клік на 🎤 → «🔓 Відкрити RAG-ПК»)';
  }
  function ragStatus() {
    if (!BENCH) return 'RAG-ПК не налаштовано («⚙️ Налаштування нарад»)';
    if (rag.state === 'ok') {
      var i = rag.info || {};
      return '✅ RAG-ПК доступний · ' + (i.model || 'Whisper') + (i.loaded ? (i.device === 'cuda' ? ' · відеокарта' : ' · процесор') : '');
    }
    if (rag.state === 'down') return '⚠️ ' + ragProblem();
    return '… перевіряю RAG-ПК';
  }

  // ── Меню правого кліку ──
  function micLabel() {
    var id = get(MIC_KEY) || '';
    if (!id) return 'за замовчуванням Windows';
    var m = (mics || []).filter(function (x) { return x.id === id; })[0];
    return m ? m.label : 'вибраний раніше';
  }
  function loadMics() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return Promise.resolve([]);
    return navigator.mediaDevices.enumerateDevices().then(function (list) {
      var n = 0;
      mics = list.filter(function (d) { return d.kind === 'audioinput' && d.deviceId && d.deviceId !== 'default' &&
                                                 d.deviceId !== 'communications'; })
        .map(function (d) { n += 1; return {id: d.deviceId, label: d.label || ('Мікрофон ' + n)}; });
      return mics;
    }).catch(function () { return []; });
  }
  function menu(e) {
    if (!(window.MVMenu && window.MVMenu.open)) {
      // без спільного меню Minerva — мова по колу
      var all = ['auto'].concat(LANGS.map(function (l) { return l[0]; }));
      var i = all.indexOf(langPick());
      set(KEY, all[(i + 1) % all.length]);
      title();
      say([['', '🎤 ' + langName(langPick())]], 1500);
      return;
    }
    var at = {x: e.clientX, y: e.clientY};
    // у вікні (<dialog>) — меню в самому вікні, інакше його не видно над вікном
    var opened = window.MVMenu.open(at, items(), {title: '🎤 Голосовий набір', returnFocus: false,
                                                 container: host(target)});
    var redraw = function () { if (opened && opened.refresh && window.MVMenu.isOpen()) opened.refresh(); };
    ragCheck(true).then(redraw);
    loadMics().then(redraw);
  }
  function items() {
    function pickMode(m) { set(MODE_KEY, m); title(); }
    function pickLang(v) { set(KEY, v); title(); if (listening && !take) { var el = target; stop(); start(el); } }
    var out = [{type: 'title', label: 'Хто розпізнає'}];
    ['rag', 'rag_clean', 'browser'].forEach(function (m) {
      var can = m === 'browser' ? !!SR : !!(BENCH && CAN_REC);
      out.push({type: 'radio', group: 'mvv-mode', label: MODES[m], checked: function () { return mode() === m; },
                disabled: !can, onSelect: function () { pickMode(m); }});
    });
    out.push({type: 'hint', label: function () {
      if (mode() === 'browser') return 'Звук іде до Google / Microsoft; мікрофон — з «🎙 Мікрофон» (старіший браузер бере мікрофон Windows за замовчуванням).';
      return ragStatus() + (rag.state === 'down' && SR ? ' — поки що розпізнає браузер' : '');
    }});
    if (BENCH) {
      out.push({label: '🔓 Відкрити RAG-ПК (прийняти сертифікат)', onSelect: function () {
        window.open(BENCH + '/#check', '_blank', 'noopener');
      }, hidden: function () { return !(rag.state === 'down' && rag.why === 'unreachable'); }});
    }
    out.push({type: 'separator'});
    out.push({type: 'submenu', label: '🌐 Мова', hint: function () { return langName(langPick()); }, items: function () {
      return [['auto', 'Автоматично']].concat(LANGS).map(function (x) {
        return {type: 'radio', group: 'mvv-lang', label: x[1], checked: function () { return langPick() === x[0]; },
                close: true, onSelect: function () { pickLang(x[0]); }};
      }).concat([{type: 'hint', label: 'Автоматично: RAG-ПК визначає мову сам; браузер — мовою інтерфейсу.'}]);
    }});
    out.push({type: 'submenu', label: '🎙 Мікрофон', hint: micLabel, disabled: function () { return !CAN_REC; },
              items: function () {
      var list = [{type: 'radio', group: 'mvv-mic', label: 'За замовчуванням Windows', checked: function () { return !(get(MIC_KEY) || ''); },
                   close: true, onSelect: function () { set(MIC_KEY, ''); title(); }}];
      (mics || []).forEach(function (m) {
        list.push({type: 'radio', group: 'mvv-mic', label: m.label, data: true, checked: function () { return get(MIC_KEY) === m.id; },
                   close: true, onSelect: function () { set(MIC_KEY, m.id); title(); }});
      });
      if (!(mics || []).some(function (m) { return !/^Мікрофон \d+$/.test(m.label); })) {
        list.push({type: 'hint', label: 'Назви мікрофонів з’являться після першого запису (дозвіл браузера на мікрофон).'});
      }
      return list;
    }});
    out.push({type: 'hint', label: 'Не чує? «🎙 Мікрофон» — той, у який ви говорите.',
              hidden: function () { return !CAN_REC || !!get(MIC_KEY); }});
    return out;
  }

  // ── Текст у поле ──
  function words(text, single, code) {
    var list = WORDS[(code || lang()).slice(0, 2)] || [];
    list.forEach(function (w) {
      var to = w[1], mark = /[.,;:?!]/.test(to);
      // RAG-ПК (Whisper) сам ставить знаки й ще пише слово: «день, кома, як» — знак поруч зі словом поглинається;
      // перед «новим рядком» знак лишається («Дякую. Новий рядок»)
      var re = new RegExp('[ \\t]*' + (mark ? '[.,;:?!]?[ \\t]*' : '') + '(?<![\\p{L}\\p{N}])' +
        w[0].replace(/ /g, '\\s+') + '(?![\\p{L}\\p{N}])[.,;:?!]?[ \\t]*', 'giu');
      if (single && to.indexOf('\n') >= 0) to = ' ';
      text = text.replace(re, function () { return mark ? to + ' ' : to; });
    });
    return text.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').replace(/[ \t]{2,}/g, ' ')
      .replace(/([.!?]\s+|\n)(\p{L})/gu, function (m, a, c) { return a + c.toUpperCase(); })
      .replace(/^[ \t]+/, '');
  }
  function before(el) {
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      var p = el.selectionStart == null ? el.value.length : el.selectionStart;
      return el.value.slice(Math.max(0, p - 2), p);
    }
    var s = window.getSelection();
    if (!s || !s.rangeCount || !el.contains(s.anchorNode)) return el.textContent.slice(-2);
    var r = s.getRangeAt(0).cloneRange();
    r.setStart(el, 0);
    return r.toString().slice(-2);
  }
  function caret(el) {
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return;
    var s = window.getSelection();
    if (s && s.rangeCount && el.contains(s.anchorNode)) return;
    var r = document.createRange();
    r.selectNodeContents(el);
    r.collapse(false);
    s.removeAllRanges();
    s.addRange(r);
  }
  function insert(el, raw, code) {
    if (!el || !el.isConnected) return;
    var single = el.tagName === 'INPUT', text = words(raw.trim(), single, code);
    if (!text.trim()) return;
    if (document.activeElement !== el) el.focus();
    caret(el);
    var prev = before(el);
    var start = !prev.trim() || /[.!?]\s*$/.test(prev) || /\n$/.test(prev);
    if (start) text = text.replace(/^(\s*)(\p{L})/u, function (m, sp, c) { return sp + c.toUpperCase(); });
    if (prev && !/\s$/.test(prev) && !/^[\s.,;:?!]/.test(text)) text = ' ' + text;
    var done = false;
    // execCommand пише в активне поле: лише коли це саме наше (фокус міг піти — RAG-ПК відповідає за секунди)
    if (document.activeElement === el) {
      try { done = document.execCommand('insertText', false, text); } catch (e) { done = false; }
    } else if (!(el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {
      var end = document.createRange();
      end.selectNodeContents(el);
      end.collapse(false);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(end);
    }
    if (!done) {
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        var a = el.selectionStart == null ? el.value.length : el.selectionStart, b = el.selectionEnd == null ? a : el.selectionEnd;
        el.setRangeText(text, a, b, 'end');
      } else {
        var s = window.getSelection(), r = s.getRangeAt(0);
        r.deleteContents();
        r.insertNode(document.createTextNode(text));
        r.collapse(false);
      }
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    place();
  }

  // ── Старт: за вибором людини, RAG-ПК недоступний — браузер ──
  function start(el) {
    if (!el || listening || (take && take.sending)) return;
    var m = mode();
    fallback = false;
    if (!isRag(m)) { browserStart(el); return; }
    btn.classList.add('wait');
    say([['', '🎤 Перевіряю RAG-ПК…']]);
    ragCheck(false, function () {
      say([['', '🔐 Браузер питає дозвіл на доступ до пристроїв у локальній мережі (там RAG-ПК) — ' +
                'натисніть «Дозволити» вгорі, біля адреси сайту.']]);
    }).then(function (good) {
      btn.classList.remove('wait');
      if (target !== el) { say(null); return; }                // людина вже в іншому полі
      if (good) { ragStart(el, m === 'rag_clean'); return; }
      if (SR) {
        fallback = true;
        browserStart(el, ['', '⚠️ ' + ragProblem() + ' — розпізнає браузер.\n']);
        return;
      }
      say([['', '⚠️ ' + ragProblem() + ', а цей браузер сам голос не розпізнає.']], 8000);
    });
  }
  function stop() {
    if (take) { ragStop(false); return; }
    stopping = true;
    if (rec) { try { rec.stop(); } catch (e) { /* уже зупинено */ } }
    listening = false;
    if (btn) { btn.classList.remove('on'); btn.classList.remove('fb'); title(); }
    say(null);
  }

  // ── RAG-ПК: запис у браузері → /api/dictate ──
  function pickType() {
    var types = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm', 'audio/mp4'];
    for (var i = 0; i < types.length; i++) {
      if (window.MediaRecorder.isTypeSupported && window.MediaRecorder.isTypeSupported(types[i])) return types[i];
    }
    return '';
  }
  function getStream(clean) {
    var id = get(MIC_KEY) || '';
    var audio = {echoCancellation: true, noiseSuppression: !clean, autoGainControl: true, channelCount: 1};
    if (id) audio.deviceId = {exact: id};
    return navigator.mediaDevices.getUserMedia({audio: audio}).catch(function (err) {
      if (id && err && (err.name === 'OverconstrainedError' || err.name === 'NotFoundError')) {
        set(MIC_KEY, '');                                    // вибраного мікрофона вже немає — за замовчуванням
        delete audio.deviceId;
        return navigator.mediaDevices.getUserMedia({audio: audio});
      }
      throw err;
    });
  }
  function clock(s) { s = Math.floor(s); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function bars(level) {
    var n = Math.max(0, Math.min(5, Math.round(level * 5)));
    return '▮▮▮▮▮'.slice(0, n) + '▯▯▯▯▯'.slice(0, 5 - n);
  }
  function ragStart(el, clean) {
    getStream(clean).then(function (stream) {
      if (!mics || !mics.some(function (x) { return !/^Мікрофон \d+$/.test(x.label); })) loadMics();
      var type = pickType(), recorder;
      try { recorder = type ? new MediaRecorder(stream, {mimeType: type}) : new MediaRecorder(stream); } catch (e) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        say([['', '⚠️ Браузер не вміє записувати звук для RAG-ПК.']], 6000);
        return;
      }
      var t = {el: el, clean: clean, stream: stream, recorder: recorder, chunks: [], started: Date.now(), peak: 0,
               level: 0, cancel: false, sending: false};
      take = t;
      warm();
      recorder.ondataavailable = function (ev) { if (ev.data && ev.data.size) t.chunks.push(ev.data); };
      recorder.onstop = function () { ragSend(t); };
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        t.ctx = new Ctx();
        var an = t.ctx.createAnalyser();
        an.fftSize = 1024;
        t.ctx.createMediaStreamSource(stream).connect(an);
        var buf = new Float32Array(an.fftSize);
        t.meter = function () {
          an.getFloatTimeDomainData(buf);
          var sum = 0;
          for (var i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
          var rms = Math.sqrt(sum / buf.length);
          t.level = Math.min(1, rms * 12);
          t.peak = Math.max(t.peak, rms);
        };
      } catch (e) { t.meter = null; }
      recorder.start(1000);
      listening = true;
      btn.classList.add('on');
      title();
      var track = stream.getAudioTracks()[0];
      var micName = track && track.label ? track.label : '';
      t.timer = setInterval(function () {
        if (t.meter) t.meter();
        var sec = (Date.now() - t.started) / 1000;
        if (sec >= MAX_REC_S) { ragStop(false); return; }
        var lines = [['', '🎤 Слухаю · RAG-ПК' + (clean ? ' + 🧹' : '') + ' · ' + clock(sec) + '  ' + bars(t.level) + '\n'],
                     ['i', (micName ? micName + ' · ' : '') + 'клік на 🎤 — розпізнати, Esc — скасувати']];
        if (sec > 4 && t.peak < 0.004) {
          lines.push(['', '\n⚠️ Мікрофон мовчить — правий клік на 🎤 → «🎙 Мікрофон».']);
        }
        say(lines);
      }, 200);
    }, function (err) {
      var name = err && err.name;
      say([['', '⚠️ ' + (name === 'NotAllowedError' ? 'Браузер не дозволив мікрофон — дозвольте його (значок 🔒 або 🎤 в адресному рядку).'
        : name === 'NotFoundError' ? 'Мікрофон не знайдено — під’єднайте його.'
          : 'Не вдалося ввімкнути мікрофон.')]], 7000);
    });
  }
  function ragStop(cancel) {
    var t = take;
    if (!t || t.sending) return;
    t.cancel = !!cancel;
    clearInterval(t.timer);
    listening = false;
    if (btn) btn.classList.remove('on');
    try { if (t.recorder.state !== 'inactive') t.recorder.stop(); else ragSend(t); } catch (e) { ragSend(t); }
  }
  function release(t) {
    try { t.stream.getTracks().forEach(function (x) { x.stop(); }); } catch (e) { /* уже */ }
    try { if (t.ctx) t.ctx.close(); } catch (e) { /* уже */ }
  }
  function ragSend(t) {
    if (t.sending || t.done) return;
    release(t);
    if (t.cancel || !t.chunks.length) {
      t.done = true;
      take = null;
      title();
      say(t.cancel ? [['', '🎤 Скасовано.']] : [['', '🎤 Нічого не записано.']], 2000);
      return;
    }
    t.sending = true;
    var blob = new Blob(t.chunks, {type: (t.recorder.mimeType || 'audio/webm')});
    var l = langPick();
    var url = BENCH + '/api/dictate?lang=' + encodeURIComponent(l === 'auto' ? 'auto' : l.slice(0, 2)) +
      '&clean=' + (t.clean ? '1' : '0');
    if (btn) { btn.classList.add('wait'); title(); }
    var cold = !!(rag.info && (rag.info.warming || !rag.info.loaded));
    say([['', '⏳ Розпізнаю на RAG-ПК' + (t.clean ? ' (спершу чистка шуму)' : '') + '…']].concat(cold
      ? [['', '\n'], ['i', 'модель ще завантажується — після паузи перший раз до 15 с']] : []));
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, SEND_TIMEOUT);
    fetch(url, {method: 'POST', mode: 'cors', credentials: 'omit', cache: 'no-store', body: blob,
                headers: {'X-Bench': '1', 'Content-Type': blob.type.split(';')[0] || 'audio/webm'},
                signal: ctl ? ctl.signal : undefined})
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) { return {ok: r.ok && d.ok, d: d}; });
      })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'RAG-ПК не розпізнав.');
        var d = res.d;
        if (d.text) insert(t.el, d.text, d.language);       // «кома» / «крапка» — мовою, яку почув RAG-ПК
        var where = (d.device === 'cuda' ? 'відеокарта' : 'процесор') + (d.cleaned ? ' · 🧹' : '');
        if (rag.info) { rag.info.loaded = true; rag.info.warming = false; rag.info.device = d.device; }
        var load = d.load_s ? ' (модель завантажено за ' + String(d.load_s).replace('.', ',') + ' с)' : '';
        say(d.text ? [['', '✅ RAG-ПК · ' + where + ' · ' + String(d.seconds).replace('.', ',') + ' с' + load]]
          : [['', '🎤 RAG-ПК нічого не почув — говоріть ближче до мікрофона.']], d.text ? 2500 : 6000);
      }, function (err) {
        rag.state = 'down';                                 // наступний клік — знову перевірка (і браузер, якщо треба)
        rag.why = 'unreachable';
        rag.at = Date.now();
        var aborted = err && err.name === 'AbortError';
        throw new Error(aborted ? 'RAG-ПК не відповів вчасно.' : 'Зв’язок з RAG-ПК перервався.');
      })
      .catch(function (err) {
        say([['', '⚠️ ' + (err && err.message ? err.message : 'RAG-ПК не розпізнав.') + ' Текст не вставлено — спробуйте ще раз.']], 8000);
      })
      .then(function () {
        clearTimeout(timer);
        t.done = true;
        t.sending = false;
        take = null;
        if (btn) { btn.classList.remove('wait'); title(); }
      });
  }

  // ── Браузер (Web Speech API) ──
  var ERRORS = {
    'not-allowed': 'Браузер не дозволив мікрофон — дозвольте його (значок 🔒 або 🎤 в адресному рядку) й клікніть 🎤 ще раз.',
    'service-not-allowed': 'Браузер не дозволив розпізнавання голосу на цій сторінці.',
    'audio-capture': 'Мікрофон не знайдено — під’єднайте його або виберіть у налаштуваннях Windows.',
    'network': 'Немає зв’язку із сервісом розпізнавання браузера — перевірте інтернет.',
    'language-not-supported': 'Браузер не розпізнає цю мову — правий клік на 🎤, щоб вибрати іншу.',
    'no-speech': 'Не почув — говоріть ближче або виберіть свій мікрофон: правий клік на 🎤 → «🎙 Мікрофон».'
  };
  function useLocal(r, l) {
    // Chrome: розпізнавання на самому ПК, якщо мовна модель уже є (інакше — сервіс браузера)
    local = false;
    if (!('processLocally' in r) || typeof SR.available !== 'function') return Promise.resolve();
    return Promise.resolve(SR.available({langs: [l], processLocally: true})).then(function (s) {
      if (s === 'available') { r.processLocally = true; local = true; }
    }).catch(function () { local = false; });
  }
  function browserStart(el, note) {
    if (!SR) { say([['', '⚠️ Цей браузер сам голос не розпізнає — виберіть RAG-ПК (правий клік на 🎤).']], 6000); return; }
    stopping = false;
    restarts = 0;
    lastError = '';
    var l = lang(), r = new SR();
    r.lang = l;
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.onresult = function (e) {
      var interim = '';
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var res = e.results[i];
        if (res.isFinal) insert(el, res[0].transcript);
        else interim += res[0].transcript;
      }
      var head = note ? [note] : [];
      say(head.concat(interim ? [['', '🎤 '], ['i', interim]] : [['', '🎤 Слухаю…']]));
    };
    r.onerror = function (e) {
      lastError = e.error || '';
      if (lastError === 'aborted') return;
      say((note ? [note] : []).concat([['', '⚠️ ' + (ERRORS[lastError] || 'Голос не розпізнано (' + lastError + ').')]]),
          9000);
    };
    var micStream = null, micTrack = null;
    function begin() {
      // новіші Chrome / Edge: розпізнавання з вибраного мікрофона (start(MediaStreamTrack)); старіші — мікрофон
      // Windows за замовчуванням
      if (micTrack && micTrack.readyState === 'live') {
        try { r.start(micTrack); return; } catch (x) { micTrack = null; }
      }
      r.start();
    }
    function freeMic() {
      if (micStream) { try { micStream.getTracks().forEach(function (x) { x.stop(); }); } catch (e) { /* уже */ } }
      micStream = micTrack = null;
    }
    r.onend = function () {
      // Chrome сам зупиняється після паузи — поки людина не натиснула «стоп», слухаємо далі
      if (!stopping && !lastError && listening && target === el && document.activeElement === el && restarts++ < RESTARTS) {
        try { begin(); return; } catch (x) { /* далі — зупинка */ }
      }
      freeMic();
      listening = false;
      rec = null;
      if (btn) { btn.classList.remove('on'); btn.classList.remove('fb'); title(); }
      if (!lastError) say(null);
    };
    rec = r;
    var chosen = CAN_REC && get(MIC_KEY) ? getStream(false).then(function (s) {
      micStream = s;
      micTrack = s.getAudioTracks()[0] || null;
    }, function () { micStream = micTrack = null; }) : Promise.resolve();
    Promise.all([useLocal(r, l), chosen]).then(function () {
      if (rec !== r) { freeMic(); return; }
      try {
        begin();
      } catch (x) {
        freeMic();
        rec = null;
        say([['', '⚠️ Не вдалося ввімкнути мікрофон.']], 5000);
        return;
      }
      listening = true;
      btn.classList.add('on');
      if (fallback) btn.classList.add('fb');
      title();
      var first = !get(SEEN);
      set(SEEN, '1');
      var head = note ? [note] : [];
      say(head.concat(first ? [['', '🎤 Слухаю (' + langName(l) + ')…\n'],
                               ['i', local ? '🔒 Розпізнає цей ПК — голос нікуди не йде.'
                                 : 'Розпізнає браузер: у Chrome — сервіс Google, в Edge — Microsoft. Не диктуйте паролі.']]
        : [['', '🎤 Слухаю (' + langName(l) + (local ? ' · 🔒 на цьому ПК' : '') + ')…']]));
    });
  }

  // ── Події сторінки ──
  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (el === btn) return;
    if (el && el.isContentEditable && !ok(el)) {                  // курсор у частині редактора листа
      while (el.parentElement && el.parentElement.isContentEditable) el = el.parentElement;
    }
    if (ok(el)) show(el);
    else if (!listening) hide();
  }, true);
  document.addEventListener('focusout', function (e) {
    if (e.target !== target || listening) return;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      var a = document.activeElement;
      if (!listening && a !== target && !(target && target.contains && target.contains(a))) hide();
    }, 120);
  }, true);
  window.addEventListener('scroll', place, true);
  window.addEventListener('resize', place);
  document.addEventListener('input', function (e) { if (e.target === target) place(); }, true);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !listening) return;
    e.stopPropagation();
    e.preventDefault();
    if (take) ragStop(true); else stop();
  }, true);
  document.addEventListener('close', function (e) {               // вікно (dialog) закрили — кнопка з ним
    if (target && e.target.contains && e.target.contains(target)) hide(true);
  }, true);

  window.MVVoice = {supported: true, start: function (el) { show(el); start(el); }, stop: stop,
                    isListening: function () { return listening; }, mode: mode,
                    rag: function () { return {bench: BENCH, state: rag.state, why: rag.why}; }};
})();
