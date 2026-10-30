/* Minerva — інтерфейс іншою мовою за словником (core/ui_text, «🌐 Переклади інтерфейсу»).
 *
 * Словник window.MV_UI_DICT = {exact: {напис: переклад}, patterns: [[напис з {0}, переклад з {0}], …]} приходить
 * окремим файлом /core/ui-dict/<мова>.js?v=… (кешується браузером). Скрипт перекладає тексти сторінки, підказки
 * (title, placeholder, aria-label, alt), кнопки й повідомлення alert / confirm / prompt — і те, що з'являється
 * пізніше (MutationObserver). Не чіпає дані: поля вводу, textarea, редактори, [translate="no"], .notranslate,
 * листи й нотатки (SKIP). Порівнюється так само, як у core/ui_text/text.py: по краях знімаються емодзі, значки,
 * «:», тире, «·».
 */
(function () {
  'use strict';
  var root = document.documentElement;
  function done() { root.classList.remove('mv-tr-pending'); }
  var D = window.MV_UI_DICT;
  if (!D || (!D.patterns.length && !Object.keys(D.exact).length)) { done(); return; }

  var CYR = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/;
  /* суцільний український шматок: слова з кирилиці (і цифри всередині) через пробіли, коми, дужки */
  var RUN = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ'’-]*(?:[ ,()]+[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9][А-ЩЬЮЯЄІЇҐа-щьюяєіїґ0-9'’-]*)*\)?/g;
  var SKIP = 'script,style,textarea,code,pre,[translate="no"],.notranslate,[contenteditable],[contenteditable="true"],' +
             '.em-card-body,.em-card-body-text,.em-snippet,.em-subj,.mv-user-text,.ap-draft';
  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt', 'data-confirm', 'label'];
  var exact = D.exact, index = {}, starts = [];

  /* шаблони з {0}: індекс за першим словом, щоб не перебирати всі */
  D.patterns.forEach(function (p) {
    var lit = p[0].split(/\{\d+\}/)[0].trim(), word = (lit.split(/\s+/)[0] || '').toLowerCase();
    var entry = { src: p[0], dst: p[1], re: null };
    if (word) (index[word] = index[word] || []).push(entry); else starts.push(entry);
  });
  function compile(e) {
    if (!e.re) {
      var order = [];
      var body = e.src.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{(\d+)\}/g, function (_, n) {
        order.push(n); return '(.+?)';
      });
      e.re = new RegExp('^' + body + '$'); e.order = order;
    }
    return e.re;
  }
  function viaPattern(core) {
    var word = (core.split(/\s+/)[0] || '').toLowerCase();
    var list = (index[word] || []).concat(starts);
    for (var i = 0; i < list.length; i++) {
      var e = list[i], m = compile(e).exec(core);
      if (m) {
        var vals = {};
        e.order.forEach(function (n, k) { vals[n] = m[k + 1]; });
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
  function core(c, depth) {
    if (!c || !CYR.test(c)) return null;
    if (exact[c] !== undefined) return exact[c];
    var t = viaPattern(c);
    if (t !== null) return t;
    if (depth > 1) return null;
    /* число в кінці («Зберегти (2)», «Сторінок: 12») */
    var m = c.match(/^(.*?[^\s\d])(\s*[([]?[\d\s.,:/×x%+−-]+[)\]]?)$/);
    if (m && CYR.test(m[1])) { t = core(m[1], depth + 1); if (t !== null) return t + m[2]; }
    /* кілька фраз через « · », « — », « | », «: », «; » */
    var parts = c.split(/(\s[·—–|]\s|:\s|;\s)/);
    if (parts.length > 1) {
      var changed = false;
      for (var i = 0; i < parts.length; i += 2) {
        var e = edges(parts[i]), tt = core(e[1], depth + 1);
        if (tt !== null) { parts[i] = e[0] + tt + e[2]; changed = true; }
      }
      if (changed) return parts.join('');
    }
    /* англійське слово Django перед нашою назвою («By BOM (комплектація)», «Add Товар»): без перших слів */
    var words = c.split(' ');
    for (var k = 1; k <= 3 && k < words.length && !CYR.test(words[k - 1]); k++) {
      var rest = words.slice(k).join(' ');
      if (exact[rest] !== undefined) return words.slice(0, k).join(' ') + ' ' + exact[rest];
    }
    /* українські шматки всередині чужої фрази («Select Товар to change», «By Категорія», «5 Товари») */
    var runs = false, out = c.replace(RUN, function (run) {
      var e = edges(run), tt = e[1] === c ? null : core(e[1], depth + 1);
      if (tt === null) return run;
      runs = true; return e[0] + tt + e[2];
    });
    return runs ? out : null;
  }
  function tr(s) {
    if (!s || !CYR.test(s)) return null;
    var e = edges(s), t = core(e[1], 0);
    return t === null ? null : e[0] + t + e[2];
  }
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
