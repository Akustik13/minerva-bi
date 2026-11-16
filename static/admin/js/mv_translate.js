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
 * Тому {0} шаблону приймає лише значення (число, дата, код, ім'я, щось у «лапках» чи напис зі словника), а не
 * продовження речення: «на {0}» не перекладе «на шкалі проєкту», «чекають {0}» — «чекають ✓ /» (value). Шматки
 * (речення, фрази через « · », « — », «: ») без перекладу — лише значення після назви (pieces), українські шматки
 * всередині чужої фрази («Select Товар to change») — лише всі разом (viaRuns).
 */
(function () {
  'use strict';
  var root = document.documentElement;
  function done() { root.classList.remove('mv-tr-pending'); }
  var CYR = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/;
  /* суцільний український шматок: слова з кирилиці (і цифри всередині) через пробіли, коми, дужки */
  var RUN = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’-]*(?:[ ,()]+[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9'’-]*)*\)?/g;
  var LATIN = /[A-Za-z]/;
  var WORD = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’-]*/g;
  var WORD3 = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’]{3,}/g;      // «опорне» слово шаблону: від 3 літер
  var OPEN_Q = '«“„"\'‘', CLOSE_Q = '»”“"\'’', QUOTES = /[«»“”„"]/;
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
    /* {0} впритул до літери: перед літерою («{0}м», «{0}хв») — число, не кінець слова: «роз'єм» — не «{0}м» («…mo»);
       після літери («лістинг{0}», «невідом{0}») — закінчення слова (до 4 малих літер) або щось з не-літери */
    var LET = 'A-Za-z\\u00C0-\\u00D6\\u00D8-\\u00F6\\u00F8-\\u024F\\u0370-\\u03FF\\u0400-\\u04FF', NOL = '[^' + LET + ']';
    var ENDING = "[a-z\\u0430-\\u0449\\u044c\\u044e\\u044f\\u0454\\u0456\\u0457\\u0491'\u2019]{1,4}";
    var LET_END = new RegExp('[' + LET + ']$'), LET_START = new RegExp('^[' + LET + ']');
    function compile(e) {
      if (!e.re) {
        var order = [], loose = {}, quoted = {}, bits = e.src.split(/(\{\d+\})/), body = '';
        for (var i = 0; i < bits.length; i++) {
          var b = bits[i], m = /^\{(\d+)\}$/.exec(b);
          if (!m) { body += b.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&'); continue; }
          order.push(m[1]);
          /* значення в лапках: «{0}», "{0}" — дані (або назва з інтерфейсу) */
          var qb = (bits[i - 1] || '').slice(-1), qa = (bits[i + 1] || '').charAt(0);
          if (qb && qa && OPEN_Q.indexOf(qb) >= 0 && CLOSE_Q.indexOf(qa) >= 0) quoted[m[1]] = true;
          var before = LET_END.test(bits[i - 1] || ''), after = LET_START.test(bits[i + 1] || '');
          body += before && after ? '(' + ENDING + '|' + NOL + '(?:.*?' + NOL + ')?)' :
                  before ? '(' + ENDING + '|' + NOL + '.*?)' : after ? '(.*?' + NOL + ')' : '(.+?)';
          /* закінчення слова («лістинг{0}» — 'end') або {1}{2} впритул (де межа — невідомо) */
          if (before) loose[m[1]] = 'end';
          else if ((i > 1 && bits[i - 1] === '') || (i + 2 < bits.length && bits[i + 1] === '')) loose[m[1]] = true;
        }
        e.re = new RegExp('^' + body + '$'); e.order = order; e.loose = loose; e.quoted = quoted;
        /* опора — щонайменше два слова від 3 літер поза {0} («Ризик додано: {0}», «для клієнта {0}»);
           «на {0}», «Крок {0}», «{0} з {1}» — слабкі, і шаблон, що починається з {0} («{0} без змін»), теж:
           неперекладеного українського на місці {0} там не приймаємо («Ризик з Петром» ≠ «Ризик of Петром») */
        e.strong = (e.src.replace(/\{\d+\}/g, ' ').match(WORD3) || []).length >= 2;
        e.lead = /^\{\d+\}/.test(e.src);
      }
      return e.re;
    }
    /* {0} шаблону — дані (число, назва, артикул). Значення, що починається з малої кириличної літери, — це
       неперекладений текст інтерфейсу, а не дані: «Усі {0}» на «Усі проєкти» дало б «All проєкти», «{0} з {1}» на
       «План з листування» — «План of листування». Тоді шаблон не підходить; значення, яке є в словнику, — перекладається. */
    var LOWER = /^[^\p{L}\p{N}]*[а-щьюяєіїґ]/u, SEP = /\s[·—–|]\s|:\s|;\s/,
        CYR_START = /^[^\p{L}\p{N}]*[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/u;
    /* неперекладене українське значення — ім'я чи назва («ТОВ Антена», «Іван Петренко»): кожне слово з великої
       літери і не слово зі словника; «або», «шт.», «Отримати» — це вже речення інтерфейсу, а не дані */
    function nameLike(v) {
      return (v.match(WORD) || []).every(function (w) { return !/^[а-щьюяєіїґ]/.test(w) && exact[w] === undefined; });
    }
    function value(v, loose, e, n) {
      if (loose === 'end' && /^[а-щьюяєіїґ'’]{1,4}$/.test(v)) return '';   // «лістинги» → «listing», не «listingи»
      if (!/[\p{L}\p{N}]/u.test(v)) return null;           // лише значки («✓ /» з «чекають ✓ / ✕») — не значення
      if (!CYR.test(v)) return v;
      if (loose === true) {                                 // {1}{2} впритул: межа довільна — лише перекладене ціле
        var lq = edges(v), lt = exact[lq[1]];
        return lt === undefined ? null : lq[0] + lt + lq[2];
      }
      var quoted = e.quoted[n];
      if (quoted && QUOTES.test(v)) return null;            // «{0}» захопило кілька «…» одразу
      /* слабкий шаблон чи {0} на початку: українські слова без цифр — це решта речення, навіть якщо кожне є в
         словнику («Назва зони» ≠ «Name zone(s)»); значення з числом («5 хв» → «5 min») перекладається */
      var weak = !quoted && (!e.strong || e.lead);
      if (weak && !/\d/.test(v)) return null;
      var q = edges(v), t = core(q[1], 2);                // «59 дн.» → «59 d.» (словник і шаблони, без шматків)
      if (t !== null) return q[0] + t + q[2];
      if (weak || LOWER.test(v)) return null;              // «Таймаут 7» у «{0} днів» — не «Таймаут 7 Tage»
      /* неперекладені фрази через « · », «: » («План · 21») — не дані: хай їх перекладуть по частинах;
         перелік даних («PO-1 до 04.11; PO-2 …») лишається */
      if (SEP.test(v) && v.split(SEP).some(function (x) { return CYR_START.test(x); })) return null;
      return quoted || nameLike(v) ? v : null;
    }
    function viaPattern(core) {
      var word = (core.split(/\s+/)[0] || '').toLowerCase();
      var list = (index[word] || []).concat(starts);
      for (var i = 0; i < list.length; i++) {
        var e = list[i], m = compile(e).exec(core);
        if (m) {
          var vals = {}, ok = true;
          e.order.forEach(function (n, k) { var v = value(m[k + 1], e.loose[n], e, n); if (v === null) ok = false; vals[n] = v; });
          if (!ok) continue;
          return e.dst.replace(/\{(\d+)\}/g, function (_, n) { return vals[n] !== undefined ? vals[n] : ''; });
        }
      }
      return null;
    }
    function edges(s) {                              // як core/ui_text/text.py → edges
      var pre = '', suf = '', m;
      m = s.match(/^[\s\u00a0\u00b7\u2022|:;,*\u203a\u2039\u200d\ufe0f\u2066\u2069\p{S}\p{Pd}\p{Zs}\p{Cf}]+/u); if (m) pre = m[0];
      var rest = s.slice(pre.length);
      m = rest.match(/[\s\u00a0\u00b7\u2022|:;,*\u203a\u2039\u200d\ufe0f\u2066\u2069\p{S}\p{Pd}\p{Zs}\p{Cf}]+$/u); if (m) suf = m[0];
      var core = rest.slice(0, rest.length - suf.length);
      return [pre, core.replace(/\s+/g, ' '), suf];
    }
    /* Шматки напису (речення чи фрази) з роздільниками між ними: кілька шматків поспіль — один запис словника
       (найдовший), інакше кожен окремо. Без перекладу може лишитись лише значення після назви («Клієнт: ТОВ
       Антена» → «Customer: ТОВ Антена»); перший шматок чи не ім'я / назва (nameLike) без перекладу — це
       український інтерфейс, переклад не годиться («Строк — ціле число днів» не стане «Lead time — …»). */
    function pieces(list, depth) {
      var any = false, low = false, out = [];
      for (var i = 0; i < list.length;) {
        var got = null, upto = i;
        for (var k = list.length - 1; k > i && got === null; k -= 2) {
          var je = edges(list.slice(i, k + 1).join(''));
          if (exact[je[1]] !== undefined) { got = je[0] + exact[je[1]] + je[2]; upto = k; }
        }
        if (got === null) {
          var e = edges(list[i]), t = core(e[1], depth + 1);
          if (t !== null) got = e[0] + t + e[2];
          else if (CYR.test(e[1]) && (i === 0 || LOWER.test(e[1]) || !nameLike(e[1]))) low = true;
        }
        if (got !== null) any = true;
        out.push(got !== null ? got : list.slice(i, upto + 1).join(''));
        if (upto + 1 < list.length) out.push(list[upto + 1]);
        i = upto + 2;
      }
      return any && !low ? out.join('') : null;
    }
    function leftover(s) { var m = s.match(/[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/g); return m ? m.length : 0; }
    function core(c, depth) {
      if (!c || !CYR.test(c)) return null;
      if (exact[c] !== undefined) return exact[c];
      var t = viaPattern(c);
      if (t !== null) return t;
      if (depth > 1) return null;
      /* Запасні шляхи — речення окремо, фрази окремо, число в кінці; з тих, що вдалися, — той, після якого лишилось
         найменше української (речення «… клієнти. Складає лист клієнту — ви відкриваєте …» перекладається
         фразами, бо запис словника охоплює частини двох речень) */
      var best = null;
      function take(r) { if (r !== null && (best === null || leftover(r) < leftover(best))) best = r; }
      var sent = c.split(/(?<=[.!?…])(\s+)(?=[A-ZА-ЩЬЮЯЄІЇҐ«“"])/);
      if (sent.length > 1) take(pieces(sent, depth));
      var parts = c.split(/(\s[·—–|]\s|:\s|;\s)/);   // ті самі роздільники, що SEP
      if (parts.length > 1 && leftover(best || c)) take(pieces(parts, depth));
      /* число в кінці («Зберегти (2)», «Сторінок: 12») — хвіст із цифрою, не «✓ /» */
      var m = c.match(/^(.*?[^\s\d])(\s*[([]?[\d\s.,:/×x%+−-]+[)\]]?)$/);
      if (m && /\d/.test(m[2]) && CYR.test(m[1]) && leftover(best || c)) {
        var h = edges(m[1]);
        t = core(h[1], depth + 1);
        if (t !== null) take(h[0] + t + h[2] + m[2]);
      }
      if (best !== null) return best;
      /* англійське слово Django перед нашою назвою («By BOM (комплектація)», «Add Товар»): без перших слів */
      var words = c.split(' ');
      for (var k = 1; k <= 3 && k < words.length && !CYR.test(words[k - 1]); k++) {
        var rest = words.slice(k).join(' ');
        if (exact[rest] !== undefined) return words.slice(0, k).join(' ') + ' ' + exact[rest];
      }
      /* українські шматки всередині чужої фрази («Select Товар to change») — усі разом або нічого */
      return viaRuns(c, depth);
    }
    /* хоч один шматок без перекладу — нічого (не «Надіслати на DigiKey now»); без латиниці навколо (українська
       фраза, розірвана значками: «Товар / Товари») — лише назви з великої літери, не «чекають ✓» посеред речення */
    function viaRuns(c, depth) {
      var foreign = LATIN.test(c.replace(RUN, ' ')), bad = false, runs = false;
      var out = c.replace(RUN, function (run) {
        if (bad) return run;
        var e = edges(run);
        if (e[1] === c || (!foreign && LOWER.test(e[1]))) { bad = true; return run; }
        var tt = core(e[1], depth + 1);
        if (tt === null) { bad = true; return run; }
        runs = true; return e[0] + tt + e[2];
      });
      return runs && !bad ? out : null;
    }
    function tr(s) {
      if (!s || !CYR.test(s)) return null;
      var e = edges(s), t = core(e[1], 0);
      return t === null ? null : e[0] + t + e[2];
    }
    function exactOnly(s) {
      if (!s || !CYR.test(s)) return null;
      var e = edges(s), t = exact[e[1]];
      return t === undefined ? null : e[0] + t + e[2];
    }
    function cutTo(s, n) { return n > 1 && s.length > n ? s.slice(0, n - 1) + '\u2026' : s; }
    /* Назви, які людина могла взяти з шаблону (кроки й фази плану проєкту…), — [data-mv-tr="exact"]: лише повний
       збіг зі словником («Прототип» → «Prototype»), без шматків — свої назви лишаються, як написано. Показано
       скорочено — повна назва в data-mv-full, довжина показаного — data-mv-cut. */
    function forNode(node) {
      var el = node.parentElement, hold = el && el.closest ? el.closest('[data-mv-tr]') : null;
      if (!hold) return tr(node.nodeValue);
      if (hold.getAttribute('data-mv-tr') !== 'exact') return null;
      var full = hold.getAttribute('data-mv-full');
      if (!full) return exactOnly(node.nodeValue);
      var t = exactOnly(full);
      return t === null ? null : cutTo(t, +hold.getAttribute('data-mv-cut') || 0);
    }
    tr.exact = exactOnly;
    tr.node = forNode;
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
    var t = tr.node(n);
    if (t !== null && t !== n.nodeValue) { n.nodeValue = t; mine.set(n, t); }
  }
  function attrs(el) {
    /* текст поля — дані, а підказка в ньому (placeholder, title) — напис інтерфейсу */
    var field = el.tagName === 'TEXTAREA' && !el.matches('[translate="no"],.notranslate');
    if (field ? skipped(el.parentElement) : skipped(el)) return;
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
    if (scope.nodeType !== 1) return;
    if (skipped(scope)) { if (scope.tagName === 'TEXTAREA') attrs(scope); return; }
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
