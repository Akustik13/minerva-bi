/* Minerva — інтерфейс іншою мовою за словником (core/ui_text, «🌐 Переклади інтерфейсу»).
 *
 * Словник window.MV_UI_DICT = {exact: {напис: переклад}, patterns: [[напис з {0}, переклад з {0}], …]} приходить
 * окремим файлом /core/ui-dict/<мова>.js?v=… (кешується браузером). Скрипт перекладає тексти сторінки, підказки
 * (title, placeholder, aria-label, alt), кнопки й повідомлення alert / confirm / prompt — і те, що з'являється
 * пізніше (MutationObserver). Не чіпає дані: поля вводу, textarea, редактори, [translate="no"], .notranslate,
 * листи й нотатки (SKIP). Порівнюється так само, як у core/ui_text/text.py: по краях знімаються емодзі, значки,
 * «:», тире, «·».
 *
 * Фразу перекладаємо цілою або лишаємо українською — окремих слів посеред українського речення не міняємо.
 * Тому шаблон з {0} підходить, лише коли на місці {0} — значення (число, дата, код, ім'я, щось у «лапках»), а не
 * продовження речення: «на {0}» не перекладе «на шкалі проєкту», «Крок {0}» — «Крок плану» (valueOk). Українські
 * шматки всередині чужої фрази («Select Товар to change») міняються лише всі разом (viaRuns).
 */
(function () {
  'use strict';
  var root = document.documentElement;
  function done() { root.classList.remove('mv-tr-pending'); }
  var CYR = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/;
  /* суцільний український шматок: слова з кирилиці (і цифри всередині) через пробіли, коми, дужки */
  var RUN = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’-]*(?:[ ,()]+[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9'’-]*)*\)?/g;
  var LATIN = /[A-Za-z]/;
  var LOWER_START = /^[а-щьюяєіїґ]/;                 // з малої літери — продовження речення, не ім'я й не назва
  var WORD3 = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’]{3,}/g;      // «опорне» слово шаблону: від 3 літер
  var OPEN_Q = '«“„"\'‘', CLOSE_Q = '»”“"\'’', QUOTES = /[«»“”„"]/;
  var WORD = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’-]*/g;
  var SKIP = 'script,style,textarea,code,pre,[translate="no"],.notranslate,[contenteditable],[contenteditable="true"],' +
             '.em-card-body,.em-card-body-text,.em-snippet,.em-subj,.mv-user-text,.ap-draft';
  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt', 'data-confirm', 'label'];
  /* словник {exact, patterns} → tr(напис) | null. Тим самим алгоритмом перекладається й експорт діаграм проєкту
     іншою мовою (projects/pj_export.js → MVTranslateKit.make) — навіть коли інтерфейс український. */
  function make(D) {
    var exact = D.exact || {}, index = {}, starts = [];

    /* шаблони з {0}: індекс за першим словом, щоб не перебирати всі */
    (D.patterns || []).forEach(function (p) {
      var lit = p[0].split(/\{\d+\}/)[0].trim(), word = (lit.split(/\s+/)[0] || '').toLowerCase();
      var entry = { src: p[0], dst: p[1], re: null };
      if (word) (index[word] = index[word] || []).push(entry); else starts.push(entry);
    });
    function compile(e) {
      if (!e.re) {
        var order = [], quoted = [], src = e.src;
        var body = src.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{(\d+)\}/g, function (_, n) {
          order.push(n); return '(.+?)';
        });
        src.replace(/\{\d+\}/g, function (ph, at) {           // значення в лапках: «{0}», "{0}" — завжди дані
          var before = at > 0 ? src.charAt(at - 1) : '', after = src.charAt(at + ph.length);
          quoted.push(!!before && !!after && OPEN_Q.indexOf(before) >= 0 && CLOSE_Q.indexOf(after) >= 0);
          return ph;
        });
        e.re = new RegExp('^' + body + '$'); e.order = order; e.quoted = quoted;
        /* опора — щонайменше два слова від 3 літер поза {0} («Ризик додано: {0}», «для клієнта {0}»);
           «на {0}», «Крок {0}», «{0} з {1}» — слабкі: на їхньому місці українського тексту не приймаємо.
           Шаблон, що починається з {0} («{0} без змін»), — теж: фраза з українського слова — це інтерфейс */
        e.strong = (src.replace(/\{\d+\}/g, ' ').match(WORD3) || []).length >= 2;
        e.lead = /^\{\d+\}/.test(src);
      }
      return e.re;
    }
    /* українське значення — ім'я чи назва («ТОВ Антена», «Іван Петренко»): кожне слово з великої літери і не
       слово інтерфейсу; слово з малої («або», «шт.», «шкалі») чи напис зі словника («Отримати») — це вже
       речення інтерфейсу, а не дані */
    function nameLike(v) {
      return (v.match(WORD) || []).every(function (w) { return !LOWER_START.test(w) && exact[w] === undefined; });
    }
    /* те, що стало на місце {n}: значення (→ як є чи перекладене) або null — це шматок іншого українського речення */
    function value(v, k, e) {
      if (!/[\p{L}\p{N}]/u.test(v)) return null;           // лише значки («✓ /») — не значення
      if (!CYR.test(v)) return v;                           // число, дата, код, латиниця
      if (e.quoted[k]) {                                     // «{0}»: назва в лапках — дані або назва з інтерфейсу
        if (QUOTES.test(v)) return null;                    // захопило кілька «…» одразу
        var q = edges(v);
        if (exact[q[1]] !== undefined) return q[0] + exact[q[1]] + q[2];
        return LOWER_START.test(q[1]) ? null : v;           // «оплату отримано» — шматок речення
      }
      return e.strong && !e.lead && nameLike(v) ? v : null;
    }
    function viaPattern(core) {
      var word = (core.split(/\s+/)[0] || '').toLowerCase();
      var list = (index[word] || []).concat(starts);
      for (var i = 0; i < list.length; i++) {
        var e = list[i], m = compile(e).exec(core);
        if (!m) continue;
        var vals = {}, ok = true;
        e.order.forEach(function (n, k) {
          var v = ok ? value(m[k + 1], k, e) : null;
          if (v === null) ok = false; else vals[n] = v;
        });
        if (ok) return e.dst.replace(/\{(\d+)\}/g, function (_, n) { return vals[n] !== undefined ? vals[n] : ''; });
      }
      return null;
    }
    /* українські шматки всередині фрази — лише всі разом: «Select Товар to change» → «Select Product to change»;
       без латиниці навколо (українська фраза, розірвана значками: «Товар / Послуга») — лише назви з великої
       літери; хоч один шматок без перекладу — нічого (не «Надіслати на DigiKey now») */
    function viaRuns(c, depth) {
      var foreign = LATIN.test(c.replace(RUN, ' ')), bad = false, any = false;
      var out = c.replace(RUN, function (run) {
        if (bad) return run;
        var e = edges(run);
        if (e[1] === c || (!foreign && LOWER_START.test(e[1]))) { bad = true; return run; }
        var tt = core(e[1], depth + 1);
        if (tt === null) { bad = true; return run; }
        any = true; return e[0] + tt + e[2];
      });
      return bad || !any ? null : out;
    }
    function edges(s) {                              // як core/ui_text/text.py → edges
      var pre = '', suf = '', m;
      m = s.match(/^[\s\u00a0\u00b7\u2022|:;,*\u203a\u2039\u200d\ufe0f\u2066\u2069\p{S}\p{Pd}\p{Zs}\p{Cf}]+/u); if (m) pre = m[0];
      var rest = s.slice(pre.length);
      m = rest.match(/[\s\u00a0\u00b7\u2022|:;,*\u203a\u2039\u200d\ufe0f\u2066\u2069\p{S}\p{Pd}\p{Zs}\p{Cf}]+$/u); if (m) suf = m[0];
      var core = rest.slice(0, rest.length - suf.length);
      return [pre, core.replace(/\s+/g, ' '), suf];
    }
    function core(c, depth) {
      if (!c || !CYR.test(c)) return null;
      if (exact[c] !== undefined) return exact[c];
      var t = viaPattern(c);
      if (t !== null) return t;
      if (depth > 1) return null;
      /* число в кінці («Зберегти (2)», «Сторінок: 12») — хвіст із цифрою, не «✓ /» */
      var m = c.match(/^(.*?[^\s\d])(\s*[([]?[\d\s.,:/×x%+−-]+[)\]]?)$/);
      if (m && /\d/.test(m[2]) && CYR.test(m[1])) {
        var h = edges(m[1]);
        t = core(h[1], depth + 1);
        if (t !== null) return h[0] + t + h[2] + m[2];
      }
      /* кілька фраз через « · », « — », « | », «: », «; ». Без перекладу може лишитись лише значення після
         назви («Клієнт: ТОВ Антена» → «Customer: ТОВ Антена»); перша частина чи не схоже на ім'я / назву
         (nameLike) без перекладу — це український інтерфейс, тоді нічого («Строк — ціле число днів» не стане
         «Lead time — ціле число днів») */
      var parts = c.split(/(\s[·—–|]\s|:\s|;\s)/);
      if (parts.length > 1) {
        var changed = false, whole = true;
        for (var i = 0; i < parts.length && whole; i += 2) {
          var e = edges(parts[i]);
          if (!CYR.test(e[1])) continue;
          var tt = core(e[1], depth + 1);
          if (tt !== null) { parts[i] = e[0] + tt + e[2]; changed = true; }
          else if (i === 0 || !nameLike(e[1])) whole = false;
        }
        if (changed && whole) return parts.join('');
      }
      /* англійське слово Django перед нашою назвою («By BOM (комплектація)», «Add Товар»): без перших слів */
      var words = c.split(' ');
      for (var k = 1; k <= 3 && k < words.length && !CYR.test(words[k - 1]); k++) {
        var rest = words.slice(k).join(' ');
        if (exact[rest] !== undefined) return words.slice(0, k).join(' ') + ' ' + exact[rest];
      }
      /* українські шматки всередині чужої фрази («Select Товар to change») — усі разом або нічого */
      return viaRuns(c, depth);
    }
    function tr(s) {
      if (!s || !CYR.test(s)) return null;
      var e = edges(s), t = core(e[1], 0);
      return t === null ? null : e[0] + t + e[2];
    }
    return tr;
  }
  window.MVTranslateKit = { make: make };
  var D = window.MV_UI_DICT;
  if (!D || (!D.patterns.length && !Object.keys(D.exact).length)) { done(); return; }
  var tr = make(D);
  window.MVTranslate = { tr: tr };

  var mine = new WeakMap();                        // вузол → що ми туди записали (повторно не перекладаємо)
  function skipped(el) { return !el || (el.closest && el.closest(SKIP)); }
  function textNode(n) {
    if (mine.get(n) === n.nodeValue || skipped(n.parentElement)) return;
    var t = tr(n.nodeValue);
    if (t !== null && t !== n.nodeValue) { n.nodeValue = t; mine.set(n, t); }
  }
  function attrs(el) {
    if (skipped(el)) return;
    ATTRS.forEach(function (a) {
      var v = el.getAttribute(a);
      if (v && CYR.test(v)) { var t = tr(v); if (t !== null) el.setAttribute(a, t); }
    });
    if (el.tagName === 'INPUT' && /^(submit|button|reset)$/i.test(el.type) && CYR.test(el.value)) {
      var t = tr(el.value); if (t !== null) el.value = t;
    }
  }
  function walk(scope) {
    if (!scope) return;
    if (scope.nodeType === 3) { textNode(scope); return; }
    if (scope.nodeType !== 1 || skipped(scope)) return;
    var w = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, null), n;
    while ((n = w.nextNode())) if (CYR.test(n.nodeValue)) textNode(n);
    attrs(scope);
    scope.querySelectorAll('[title],[placeholder],[aria-label],[alt],[data-confirm],optgroup[label],input[type=submit],input[type=button],input[type=reset]')
      .forEach(attrs);
  }

  /* повідомлення браузера — тим самим словником, по рядках */
  ['alert', 'confirm', 'prompt'].forEach(function (name) {
    var orig = window[name];
    window[name] = function (msg) {
      var args = [].slice.call(arguments);
      if (typeof msg === 'string') args[0] = msg.split('\n').map(function (l) { var t = tr(l); return t === null ? l : t; }).join('\n');
      return orig.apply(window, args);
    };
  });

  var pending = new Set(), timer = 0;
  function flush() {
    timer = 0;
    var list = Array.from(pending); pending.clear();
    list.forEach(function (n) { if (n.isConnected) walk(n); });
  }
  function start() {
    walk(document.body);
    document.title = tr(document.title) || document.title;
    done();
    new MutationObserver(function (records) {
      records.forEach(function (m) {
        if (m.type === 'childList') m.addedNodes.forEach(function (n) { pending.add(n); });
        else if (m.type === 'characterData') pending.add(m.target);
        else pending.add(m.target);
      });
      if (!timer) timer = setTimeout(flush, 30);
    }).observe(document.body, { childList: true, subtree: true, characterData: true,
                                attributes: true, attributeFilter: ['title', 'placeholder', 'aria-label', 'value'] });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
