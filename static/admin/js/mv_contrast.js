/* Minerva — читабельний текст у кожній темі (WCAG 2.2, 1.4.3 / 1.4.6).
 *
 * Багато сторінок мають вшиті кольори, зроблені під темну тему: у світлій темі світлий текст опинявся на
 * білому чи жовтому фоні. Скрипт після завантаження сторінки й після кожної зміни на ній (нові панелі,
 * відповіді AJAX, зміна теми) бере кожен текст, рахує його справжній фон (кольори предків, напівпрозорі шари,
 * градієнти) і контраст. Не вистачає — колір тексту стає темнішим або світлішим, відтінок той самий
 * (червоне лишається червоним). Межі: звичайний текст 4,5 : 1, великий 3 : 1; тема «♿ Контраст» — 7 : 1 і 4,5 : 1.
 *
 * Вимкнути для себе — «Мої налаштування» → «♿ Доступність» (html[data-mv-fix-contrast="0"]).
 * Частину сторінки не чіпати — атрибут data-mv-contrast="off" (зразки кольорів, прев'ю теми).
 * Перевірка сторінки в консолі: MVContrast.report() — що не читається (без виправлень), MVContrast.summary() —
 * ті самі місця, згруповані за парою «колір тексту / фон», MVContrast.show() — обвести виправлене.
 */
(function () {
  'use strict';
  if (window.MVContrast) return;
  var root = document.documentElement;
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, CANVAS: 1, OPTION: 1, OPTGROUP: 1,
               IFRAME: 1, IMG: 1, VIDEO: 1, AUDIO: 1, OBJECT: 1, EMBED: 1, HEAD: 1, TITLE: 1 };
  var INPUTS = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=color])' +
               ':not([type=range]):not([type=file]):not([type=image]),textarea,select';
  var MAX = 12000;              // елементів за прохід — довгі списки перевіряються частинами
  var WHITE = [255, 255, 255, 1];

  /* ── кольори ── */
  function parse(c) {
    var m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/.exec(c || '');
    if (!m) return null;
    var a = m[4] === undefined ? 1 : parseFloat(m[4]) / (m[5] ? 100 : 1);
    return [+m[1], +m[2], +m[3], a];
  }
  function lin(x) { x /= 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }
  function lum(c) { return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]); }
  function ratio(a, b) {
    var x = lum(a), y = lum(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }
  function over(top, under) {           // шар top поверх непрозорого under
    var a = top[3];
    if (a >= 1) return [top[0], top[1], top[2], 1];
    return [top[0] * a + under[0] * (1 - a), top[1] * a + under[1] * (1 - a), top[2] * a + under[2] * (1 - a), 1];
  }
  function hex(c) {
    return '#' + c.slice(0, 3).map(function (v) {
      v = Math.round(Math.max(0, Math.min(255, v))).toString(16);
      return v.length < 2 ? '0' + v : v;
    }).join('');
  }
  function toHsl(c) {
    var r = c[0] / 255, g = c[1] / 255, b = c[2] / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, s = 0, d = mx - mn;
    if (d) {
      s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
      h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h /= 6;
    }
    return [h, s, l];
  }
  function fromHsl(h, s, l) {
    function k(p, q, t) {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    }
    if (!s) return [l * 255, l * 255, l * 255, 1];
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    return [k(p, q, h + 1 / 3) * 255, k(p, q, h) * 255, k(p, q, h - 1 / 3) * 255, 1];
  }
  function worst(fg, bgs) {
    var r = 99, w = bgs[0];
    bgs.forEach(function (b) { var x = ratio(over(fg, b), b); if (x < r) { r = x; w = b; } });
    return { ratio: r, bg: w };
  }
  /* Найближчий колір того самого відтінку, що дає потрібний контраст з усіма можливими фонами. */
  function adjust(fg, bgs, need) {
    var L = 0;
    bgs.forEach(function (b) { L += lum(b); });
    var darker = L / bgs.length > 0.179;            // на світлому фоні — темніше, на темному — світліше
    var hsl = toHsl(over(fg, bgs[0])), lo, hi, best = null;
    if (darker) { lo = 0; hi = hsl[2]; } else { lo = hsl[2]; hi = 1; }
    var end = fromHsl(hsl[0], hsl[1], darker ? 0 : 1);
    if (worst(end, bgs).ratio < need) return darker ? [0, 0, 0, 1] : [255, 255, 255, 1];
    for (var i = 0; i < 18; i++) {
      var mid = (lo + hi) / 2, c = fromHsl(hsl[0], hsl[1], mid);
      if (worst(c, bgs).ratio >= need) { best = c; if (darker) lo = mid; else hi = mid; }
      else if (darker) hi = mid; else lo = mid;
    }
    return best || end;
  }

  /* ── фон елемента: усі шари від нього до першого непрозорого предка ── */
  var bgMemo = new Map();
  function ownLayers(cs) {
    var out = [], img = cs.backgroundImage;
    if (cs.webkitBackgroundClip === 'text' || cs.backgroundClip === 'text') img = 'none';   // градієнт — колір тексту
    if (img && img !== 'none') {
      if (img.indexOf('gradient') < 0) return null;            // картинка — фон невідомий
      var stops = (img.match(/rgba?\([^)]*\)/g) || []).map(parse).filter(Boolean);
      if (stops.length) out.push(stops);
    }
    var bc = parse(cs.backgroundColor);
    if (bc && bc[3] > 0.02) out.push([bc]);
    return out;
  }
  function backgrounds(el) {
    if (!el || el.nodeType !== 1) return [WHITE];
    if (bgMemo.has(el)) return bgMemo.get(el);
    var cs = getComputedStyle(el), layers = ownLayers(cs), res;
    if (layers === null) res = null;
    else {
      var opaque = layers.some(function (l) { return l.every(function (c) { return c[3] >= 1; }); });
      var under = opaque ? [WHITE] : backgrounds(el.parentElement);
      if (under === null) res = null;
      else {
        res = under;
        for (var i = layers.length - 1; i >= 0; i--) {     // знизу вгору: колір фону, потім градієнт
          var next = [];
          layers[i].forEach(function (top) { res.forEach(function (b) { next.push(over(top, b)); }); });
          res = dedupe(next);
        }
      }
    }
    bgMemo.set(el, res);
    return res;
  }
  function dedupe(list) {
    var seen = {}, out = [];
    list.forEach(function (c) { var k = hex(c); if (!seen[k]) { seen[k] = 1; out.push(c); } });
    if (out.length > 6) {                                        // багато зупинок — найсвітліша й найтемніша
      out.sort(function (a, b) { return lum(a) - lum(b); });
      out = [out[0], out[out.length - 1]];
    }
    return out;
  }

  /* ── які елементи перевіряти ── */
  function hasHc() { return /^hc-/.test(root.getAttribute('data-theme') || ''); }
  function enabled() { return root.getAttribute('data-mv-fix-contrast') !== '0'; }
  var LETTER = /[\p{L}\p{N}]/u, EMOJI = /^[\p{Extended_Pictographic}\p{Emoji_Component}\uFE0F\u200D\s]+$/u;
  function ownText(el) {
    if (el.value !== undefined && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return String(el.value || '');
    var t = '';
    for (var n = el.firstChild; n; n = n.nextSibling) if (n.nodeType === 3) t += n.nodeValue;
    return t.trim();
  }
  /* Текст — 4,5 : 1 (великий 3 : 1); значок без літер і цифр (▼ ✓ ✕ ↻) — 3 : 1, як елемент керування
     (WCAG 1.4.11); тема «♿ Контраст» — 7 : 1 / 4,5 : 1 / 4,5 : 1. Емодзі мають власні кольори — не міряємо. */
  function need(cs, text) {
    var size = parseFloat(cs.fontSize) || 14, bold = (parseInt(cs.fontWeight, 10) || 400) >= 700;
    var large = size >= 24 || (bold && size >= 18.66);
    if (text && !LETTER.test(text)) return hasHc() ? 4.5 : 3;
    return hasHc() ? (large ? 4.5 : 7) : (large ? 3 : 4.5);
  }
  function skipped(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (SKIP[n.tagName] || n.namespaceURI === 'http://www.w3.org/2000/svg') return true;
      if (n.getAttribute('data-mv-contrast') === 'off') return true;
    }
    return false;
  }
  function dimmed(el) {                     // напівпрозоре (вимкнене, «неактивне») — WCAG не вимагає
    var o = 1;
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      o *= parseFloat(getComputedStyle(n).opacity);
      if (o < 0.6) return true;
    }
    return false;
  }
  function textElements(scope) {
    var out = [], seen = new Set();
    if (scope.nodeType !== 1) return out;
    var w = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
      acceptNode: function (t) { return /\S/.test(t.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; }
    });
    while (w.nextNode() && out.length < MAX) {
      var p = w.currentNode.parentElement;
      if (p && !seen.has(p)) { seen.add(p); out.push(p); }
    }
    var inputs = scope.matches && scope.matches(INPUTS) ? [scope] : [];
    if (scope.querySelectorAll) inputs = inputs.concat([].slice.call(scope.querySelectorAll(INPUTS)));
    inputs.forEach(function (i) { if (!seen.has(i)) { seen.add(i); out.push(i); } });
    return out;
  }

  /* ── виправлення: пам'ятаємо, що було в style, щоб повернути при зміні теми ── */
  var fixed = new Map();                    // елемент → [було, пріоритет, поставлено]
  function restore(el) {
    var o = fixed.get(el);
    if (!o) return;
    fixed.delete(el);
    if (el.style.getPropertyValue('color') !== o[2]) return;   // колір уже змінив хтось інший
    el.style.removeProperty('color');
    if (o[0]) el.style.setProperty('color', o[0], o[1]);
  }
  function check(el, fix, found) {
    if (fixed.has(el)) restore(el);
    if (skipped(el) || !el.getClientRects().length) return;
    var cs = getComputedStyle(el);
    if (cs.visibility === 'hidden') return;
    var fg = parse(cs.color), fill = parse(cs.webkitTextFillColor);
    if (fill && fill[3] < 0.05) return;                         // градієнт у тексті (назва, девіз) — колір не той
    if (fill) fg = fill;                                        // видно саме -webkit-text-fill-color
    if (!fg || fg[3] < 0.05) return;                            // прозорий текст
    var text = ownText(el);
    if (text && EMOJI.test(text)) return;                       // лише емодзі
    var bgs = backgrounds(el);
    if (!bgs || !bgs.length) return;
    var n = need(cs, text), w = worst(fg, bgs);
    if (w.ratio >= n - 0.01 || dimmed(el)) return;
    var to = adjust(fg, bgs, n);
    if (found) found.push({
      el: el, text: (text || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      color: hex(over(fg, w.bg)), background: hex(w.bg), ratio: Math.round(w.ratio * 100) / 100, need: n,
      inline: /(^|;)\s*color\s*:/i.test(el.getAttribute('style') || ''), better: hex(to),
    });
    if (fix) {
      var was = el.style.getPropertyValue('color'), pri = el.style.getPropertyPriority('color');
      var val = 'rgb(' + to.slice(0, 3).map(Math.round).join(', ') + ')';
      el.style.setProperty('color', val, 'important');
      fixed.set(el, [was, pri, el.style.getPropertyValue('color')]);
    }
  }

  /* ── проходи ── */
  var observer = null, pending = new Set(), timer = 0;
  var OBSERVE = { childList: true, subtree: true, characterData: true, attributes: true,
                  attributeFilter: ['class', 'style', 'hidden', 'open', 'data-theme', 'data-mv-fix-contrast'] };
  function run(scopes, fix, found) {
    if (observer) collect(observer.takeRecords());
    if (observer) observer.disconnect();
    bgMemo = new Map();
    try {
      scopes.forEach(function (s) {
        if (!s.isConnected) return;
        textElements(s).forEach(function (el) {
          try { check(el, fix, found); } catch (e) { /* один елемент не зупиняє решту */ }
        });
      });
    } finally {
      bgMemo = new Map();
      if (observer) observer.observe(root, OBSERVE);
    }
  }
  function restoreAll() {
    if (observer) observer.disconnect();
    Array.from(fixed.keys()).forEach(restore);
    if (observer) observer.observe(root, OBSERVE);
  }
  function full() {
    restoreAll();
    if (enabled() && document.body) run([document.body], true, null);
  }
  function collect(records) {
    records.forEach(function (m) {
      if (m.target === root) { pending.add('full'); return; }
      if (m.type === 'childList') {
        m.addedNodes.forEach(function (n) {
          if (n.nodeType === 1) pending.add(n);
          else if (n.parentElement) pending.add(n.parentElement);
        });
      } else if (m.type === 'characterData') {
        if (m.target.parentElement) pending.add(m.target.parentElement);
      } else pending.add(m.target);
    });
  }
  function flush() {
    timer = 0;
    if (pending.has('full')) { pending.clear(); full(); return; }
    var nodes = Array.from(pending).filter(function (n) { return n.isConnected; });
    pending.clear();
    if (!enabled() || !nodes.length) return;
    nodes = nodes.filter(function (n) {                       // предок уже в списку — досить його
      return !nodes.some(function (o) { return o !== n && o.contains(n); });
    });
    run(nodes, true, null);
  }
  function start() {
    full();
    observer = new MutationObserver(function (records) {
      collect(records);
      if (timer && !pending.has('full')) return;
      clearTimeout(timer);
      /* нова тема — після переходів кольорів (transition), інакше міряли б проміжні кольори */
      timer = setTimeout(flush, pending.has('full') ? 450 : 120);
    });
    observer.observe(root, OBSERVE);
    window.addEventListener('load', function () { pending.add('full'); if (!timer) timer = setTimeout(flush, 50); });
  }

  window.MVContrast = {
    /* Що на сторінці не читається — у поточній темі, без виправлень (виправлення повертаються після). */
    report: function () {
      var found = [];
      restoreAll();
      run([document.body], false, found);
      if (enabled()) run([document.body], true, null);
      return found;
    },
    summary: function () {
      var groups = {};
      this.report().forEach(function (f) {
        var k = f.color + ' на ' + f.background;
        var g = groups[k] || (groups[k] = { pair: k, ratio: f.ratio, count: 0, inline: 0, examples: [] });
        g.count++; if (f.inline) g.inline++;
        if (g.examples.length < 3 && f.text) g.examples.push(f.text);
      });
      return Object.keys(groups).map(function (k) { return groups[k]; })
        .sort(function (a, b) { return b.count - a.count; });
    },
    show: function () {
      fixed.forEach(function (o, el) { el.style.outline = '2px dashed #e5534b'; });
      return fixed.size;
    },
    refresh: full,
    ratio: function (a, b) { return ratio(parse(a), parse(b)); },
    get fixedCount() { return fixed.size; },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
