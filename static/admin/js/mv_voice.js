/* 🎤 Голосовий набір у Minerva (templates/admin/base_site.html): у полі, де пишуть текст — textarea, текстове поле
   від 180 px, редактор листа (contenteditable), — поки в ньому курсор, видно 🎤. Клік — говорите, текст пишеться туди,
   де курсор («кома», «крапка», «знак питання», «новий рядок» — розділові знаки й рядки); ще клік — стоп.
   Розпізнає браузер (Web Speech API: Chrome, Edge, Safari; Firefox не вміє — 🎤 немає); де браузер уміє розпізнавати
   на самому ПК (processLocally), так і робить. Мова — як інтерфейс; правий клік на 🎤 — інша (localStorage mvv-lang).
   Вимкнути — «Мої налаштування» → «🎤 Голосовий набір» (data-mv-voice="off" на <html>, core/ui_prefs.py);
   поле з data-voice="off" — без 🎤, з data-voice — завжди з ним. API: MVVoice {supported, start(el), stop()}. */
(function () {
  'use strict';
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var doc = document.documentElement;
  if (!SR || window.MVVoice) return;

  var LANGS = [['uk-UA', 'Українська'], ['de-DE', 'Deutsch'], ['en-US', 'English'], ['ru-RU', 'Русский'],
               ['pl-PL', 'Polski']];
  var KEY = 'mvv-lang', SEEN = 'mvv-seen', SIZE = 28, RESTARTS = 30;
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

  function off() { return doc.getAttribute('data-mv-voice') === 'off'; }
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* лише цього разу */ } }
  function lang() {
    var l = get(KEY);
    if (l && LANGS.some(function (x) { return x[0] === l; })) return l;
    var ui = (doc.lang || 'uk').slice(0, 2).toLowerCase();
    for (var i = 0; i < LANGS.length; i++) if (LANGS[i][0].slice(0, 2) === ui) return LANGS[i][0];
    return 'uk-UA';
  }
  function langName(l) {
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
      '@keyframes mvv-pulse{0%,100%{box-shadow:0 0 0 0 rgba(197,34,31,.55)}50%{box-shadow:0 0 0 7px rgba(197,34,31,0)}}' +
      'html[data-mv-motion=reduce] #mvv-btn.on{animation:none}' +
      '#mvv-tip{position:fixed;z-index:2147483000;max-width:min(440px,80vw);padding:5px 9px;border-radius:7px;' +
      'background:rgba(22,27,34,.94);color:#fff;font:12px/1.4 system-ui,sans-serif;display:none;pointer-events:none;' +
      'white-space:pre-wrap;box-shadow:0 2px 10px rgba(0,0,0,.3)}#mvv-tip.show{display:block}' +
      '#mvv-tip i{opacity:.75}';
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
      if (listening) stop(); else start(target);
    });
    btn.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      e.stopPropagation();
      langMenu(e);
    });
    title();
  }
  function title() {
    if (!btn) return;
    var t = listening ? 'Слухаю — клік: зупинити' : 'Голосовий набір — клік: говоріть, текст з’явиться там, де курсор';
    t += ' · мова: ' + langName(lang()) + ' (правий клік — інша)';
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
    target = el;
    var h = host(el);
    if (btn.parentNode !== h) { h.appendChild(btn); h.appendChild(tip); }
    title();
    place();
  }
  function hide(force) {
    if (!btn || (listening && !force)) return;
    if (listening) stop();
    btn.classList.remove('show');
    tip.classList.remove('show');
    target = null;
  }
  function say(html, ms) {
    if (!tip) return;
    clearTimeout(tipTimer);
    tip.textContent = '';
    if (html === null) { tip.classList.remove('show'); return; }
    html.forEach(function (part) {
      var n = part[0] ? document.createElement(part[0]) : document.createTextNode(part[1]);
      if (part[0]) n.textContent = part[1];
      tip.appendChild(n);
    });
    tip.classList.add('show');
    place();
    if (ms) tipTimer = setTimeout(function () { tip.classList.remove('show'); }, ms);
  }
  function langMenu(e) {
    var cur = lang();
    function pick(l) { set(KEY, l); title(); if (listening) { var el = target; stop(); start(el); } }
    // меню Minerva — у <body>; над відкритим вікном (dialog) його не видно, тоді — наступна мова по колу
    if (window.MVMenu && window.MVMenu.open && host(target) === document.body) {
      window.MVMenu.open(e, LANGS.map(function (l) {
        return {type: 'radio', group: 'mvv', label: l[1], checked: l[0] === cur, close: true,
                onSelect: function () { pick(l[0]); }};
      }).concat([{type: 'separator'}, {type: 'hint', label: local ? '🔒 Розпізнає цей ПК — голос нікуди не йде'
        : 'Розпізнає браузер: у Chrome — сервіс Google, в Edge — Microsoft'}]),
        {title: '🎤 Мова голосового набору', returnFocus: false});
      return;
    }
    var i = 0;
    LANGS.forEach(function (l, k) { if (l[0] === cur) i = k; });
    pick(LANGS[(i + 1) % LANGS.length][0]);
    say([['', '🎤 ' + langName(lang())]], 1500);
  }

  // ── Текст у поле ──
  function words(text, single) {
    var list = WORDS[lang().slice(0, 2)] || [];
    list.forEach(function (w) {
      var re = new RegExp('[ \\t]*(?<![\\p{L}\\p{N}])' + w[0].replace(/ /g, '\\s+') + '(?![\\p{L}\\p{N}])[ \\t]*', 'giu');
      var to = w[1];
      if (single && to.indexOf('\n') >= 0) to = ' ';
      text = text.replace(re, function () { return /[.,;:?!]/.test(to) ? to + ' ' : to; });
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
  function insert(el, raw) {
    if (!el || !el.isConnected) return;
    var single = el.tagName === 'INPUT', text = words(raw.trim(), single);
    if (!text.trim()) return;
    if (document.activeElement !== el) el.focus();
    caret(el);
    var prev = before(el);
    var start = !prev.trim() || /[.!?]\s*$/.test(prev) || /\n$/.test(prev);
    if (start) text = text.replace(/^(\s*)(\p{L})/u, function (m, sp, c) { return sp + c.toUpperCase(); });
    if (prev && !/\s$/.test(prev) && !/^[\s.,;:?!]/.test(text)) text = ' ' + text;
    var done = false;
    try { done = document.execCommand('insertText', false, text); } catch (e) { done = false; }
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

  // ── Розпізнавання ──
  var ERRORS = {
    'not-allowed': 'Браузер не дозволив мікрофон — дозвольте його (значок 🔒 або 🎤 в адресному рядку) й клікніть 🎤 ще раз.',
    'service-not-allowed': 'Браузер не дозволив розпізнавання голосу на цій сторінці.',
    'audio-capture': 'Мікрофон не знайдено — під’єднайте його або виберіть у налаштуваннях Windows.',
    'network': 'Немає зв’язку із сервісом розпізнавання браузера — перевірте інтернет.',
    'language-not-supported': 'Браузер не розпізнає цю мову — правий клік на 🎤, щоб вибрати іншу.',
    'no-speech': 'Не почув — говоріть ближче до мікрофона й клікніть 🎤 ще раз.'
  };
  function useLocal(r, l) {
    // Chrome: розпізнавання на самому ПК, якщо мовна модель уже є (інакше — сервіс браузера)
    local = false;
    if (!('processLocally' in r) || typeof SR.available !== 'function') return Promise.resolve();
    return Promise.resolve(SR.available({langs: [l], processLocally: true})).then(function (s) {
      if (s === 'available') { r.processLocally = true; local = true; }
    }).catch(function () { local = false; });
  }
  function start(el) {
    if (!el || listening) return;
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
      say(interim ? [['', '🎤 '], ['i', interim]] : [['', '🎤 Слухаю…']]);
    };
    r.onerror = function (e) {
      lastError = e.error || '';
      if (lastError === 'aborted') return;
      say([['', '⚠️ ' + (ERRORS[lastError] || 'Голос не розпізнано (' + lastError + ').')]], 7000);
    };
    r.onend = function () {
      // Chrome сам зупиняється після паузи — поки людина не натиснула «стоп», слухаємо далі
      if (!stopping && !lastError && listening && target === el && document.activeElement === el && restarts++ < RESTARTS) {
        try { r.start(); return; } catch (x) { /* далі — зупинка */ }
      }
      listening = false;
      rec = null;
      if (btn) { btn.classList.remove('on'); title(); }
      if (!lastError) say(null);
    };
    rec = r;
    useLocal(r, l).then(function () {
      if (rec !== r) return;
      try {
        r.start();
      } catch (x) {
        rec = null;
        say([['', '⚠️ Не вдалося ввімкнути мікрофон.']], 5000);
        return;
      }
      listening = true;
      btn.classList.add('on');
      title();
      var first = !get(SEEN);
      set(SEEN, '1');
      say(first ? [['', '🎤 Слухаю (' + langName(l) + ')…\n'],
                   ['i', local ? '🔒 Розпізнає цей ПК — голос нікуди не йде.'
                     : 'Розпізнає браузер: у Chrome — сервіс Google, в Edge — Microsoft. Не диктуйте паролі.']]
        : [['', '🎤 Слухаю (' + langName(l) + (local ? ' · 🔒 на цьому ПК' : '') + ')…']]);
    });
  }
  function stop() {
    stopping = true;
    if (rec) { try { rec.stop(); } catch (e) { /* уже зупинено */ } }
    listening = false;
    if (btn) { btn.classList.remove('on'); title(); }
    say(null);
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
    if (e.key === 'Escape' && listening) { e.stopPropagation(); e.preventDefault(); stop(); }
  }, true);
  document.addEventListener('close', function (e) {               // вікно (dialog) закрили — кнопка з ним
    if (target && e.target.contains && e.target.contains(target)) hide(true);
  }, true);

  window.MVVoice = {supported: true, start: function (el) { show(el); start(el); }, stop: stop,
                    isListening: function () { return listening; }};
})();
