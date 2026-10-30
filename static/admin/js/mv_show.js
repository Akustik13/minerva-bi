/* Minerva — «📍 Показати» й «🧭 Провести по кроках» з відповідей гіда у віконці «Minerva» (guide/assistant.py).
 *
 * MVShow.go(адреса, текст елемента, пояснення) — відкрити сторінку (якщо інша) і підсвітити на ній кнопку / поле /
 * пункт меню / заголовок з таким текстом, поруч — пояснення. MVShow.tour([{url, target, say}, …]) — по кроках,
 * панель «← Назад · Далі →» внизу; перехід між сторінками переживає sessionStorage. Елемент шукається за видимим
 * текстом (і за перекладом, якщо інтерфейс англійською / німецькою — MVTranslate), згорнуті групи меню й <details>
 * розгортаються. Нічого не натискає й не змінює — лише показує.
 */
(function () {
  'use strict';
  if (window.MVShow) return;
  var KEY_SHOW = 'mv-show', KEY_TOUR = 'mv-tour', FRESH_MS = 120000;
  var SEL = 'a,button,input[type=submit],input[type=button],summary,label,th,h1,h2,h3,h4,legend,.button,' +
            '[role=button],[role=tab],.mg-link,.mg-header,.mg-link-head,option';
  var ring = null, bubble = null, current = null, timer = 0;

  function ss(k, v) {
    try {
      if (v === undefined) return JSON.parse(sessionStorage.getItem(k) || 'null');
      if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, JSON.stringify(v));
    } catch (e) { return null; }
  }
  function norm(s) {
    return String(s || '').replace(/[«»"„“”'’]/g, '')
      .replace(/^[\s\p{S}\p{Pd}\p{Zs}\p{Cf}·•|:;,*›‹‍️]+|[\s\p{S}\p{Pd}\p{Zs}\p{Cf}·•|:;,*›‹‍️]+$/gu, '')
      .replace(/\s+/g, ' ').toLowerCase();
  }
  function textOf(el) {
    if (el.tagName === 'INPUT') return el.value || '';
    var t = (el.innerText || el.textContent || '').trim();
    return t.length > 160 ? t.slice(0, 160) : t;
  }
  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function internal(path) { return typeof path === 'string' && /^\/(?!\/)/.test(path); }
  function samePage(path) {
    if (!path) return true;
    var u = new URL(path, location.origin);
    return u.pathname === location.pathname && (!u.search || u.search === location.search);
  }

  /* найкращий збіг: точний текст → починається з → містить; видимий і коротший — краще */
  function find(target) {
    var wants = [norm(target)];
    if (window.MVTranslate) { var t = window.MVTranslate.tr(String(target)); if (t) wants.push(norm(t)); }
    wants = wants.filter(Boolean);
    if (!wants.length) return null;
    var best = null, bestScore = 0;
    document.querySelectorAll(SEL).forEach(function (el) {
      if (el.closest('#rg-panel,#mv-show-bar,.mv-show-bubble')) return;
      var own = norm(textOf(el)), title = norm(el.getAttribute('title') || '');
      wants.forEach(function (w) {
        var s = 0;
        if (own === w || title === w) s = 30;
        else if (own.indexOf(w) === 0) s = 20;
        else if (own.indexOf(w) >= 0 || (title && title.indexOf(w) >= 0)) s = 10;
        if (!s) return;
        if (visible(el)) s += 5;
        s -= Math.min(own.length / 40, 4);
        if (s > bestScore) { bestScore = s; best = el; }
      });
    });
    return best;
  }
  function reveal(el) {
    for (var n = el; n; n = n.parentElement) {
      if (n.tagName === 'DETAILS' && !n.open) n.open = true;
      if (n.classList && n.classList.contains('mg-group') && n.classList.contains('collapsed')) n.classList.remove('collapsed');
      if (n.classList && n.classList.contains('mg-content') && n.style.display === 'none') n.style.display = '';
    }
    if (el.tagName === 'OPTION') el = el.closest('select') || el;
    return el;
  }

  function clear() {
    if (ring) ring.remove(); if (bubble) bubble.remove();
    ring = bubble = current = null;
    clearTimeout(timer);
    removeEventListener('scroll', place, true); removeEventListener('resize', place);
  }
  function place() {
    if (!current || !ring) return;
    var r = current.getBoundingClientRect();
    ring.style.left = (r.left - 6) + 'px'; ring.style.top = (r.top - 6) + 'px';
    ring.style.width = (r.width + 12) + 'px'; ring.style.height = (r.height + 12) + 'px';
    if (bubble) {
      var bw = bubble.offsetWidth, bh = bubble.offsetHeight;
      var top = r.bottom + 12 + bh < innerHeight ? r.bottom + 12 : Math.max(8, r.top - bh - 12);
      bubble.style.top = top + 'px';
      bubble.style.left = Math.max(8, Math.min(r.left, innerWidth - bw - 8)) + 'px';
    }
  }
  function toast(text) {
    var t = document.createElement('div');
    t.className = 'mv-show-toast'; t.textContent = text; document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 6000);
  }
  /* підсвітити: кільце навколо елемента + пояснення поруч */
  function highlight(target, say, opts) {
    opts = opts || {};
    clear();
    var el = find(target);
    if (!el) {
      toast('Не знайшов «' + target + '» на цій сторінці' + (opts.tour ? ' — перейдіть далі або завершіть' : '') + '.');
      return false;
    }
    el = reveal(el);
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
    current = el;
    ring = document.createElement('div'); ring.className = 'mv-show-ring'; document.body.appendChild(ring);
    bubble = document.createElement('div'); bubble.className = 'mv-show-bubble';
    var b = document.createElement('b'); b.textContent = '📍 ' + target; bubble.appendChild(b);
    if (say) { var p = document.createElement('div'); p.textContent = say; bubble.appendChild(p); }
    var x = document.createElement('button'); x.type = 'button'; x.textContent = '✕'; x.title = 'Закрити';
    x.onclick = clear; bubble.appendChild(x);
    document.body.appendChild(bubble);
    addEventListener('scroll', place, true); addEventListener('resize', place);
    setTimeout(place, 0); setTimeout(place, 400);
    if (!opts.tour) timer = setTimeout(clear, 12000);
    return true;
  }

  function go(path, target, say) {
    if (path && !internal(path)) return;
    if (path && !samePage(path)) {
      ss(KEY_SHOW, { target: target, say: say || '', at: Date.now() });
      location.href = path;
      return;
    }
    highlight(target, say);
  }

  /* ── по кроках ── */
  var bar = null;
  function tourState() { var s = ss(KEY_TOUR); return s && s.steps && s.steps.length ? s : null; }
  function renderBar(s) {
    if (!bar) { bar = document.createElement('div'); bar.id = 'mv-show-bar'; document.body.appendChild(bar); }
    bar.innerHTML = '';
    var step = s.steps[s.i];
    var head = document.createElement('b'); head.textContent = '🧭 Крок ' + (s.i + 1) + ' з ' + s.steps.length;
    var say = document.createElement('span'); say.textContent = step.say || step.target;
    function btn(text, fn, cls) {
      var b = document.createElement('button'); b.type = 'button'; b.textContent = text; b.onclick = fn;
      if (cls) b.className = cls; bar.appendChild(b); return b;
    }
    bar.appendChild(head); bar.appendChild(say);
    if (s.i > 0) btn('← Назад', function () { move(-1); });
    if (s.i < s.steps.length - 1) btn('Далі →', function () { move(1); }, 'next');
    else btn('✓ Готово', stopTour, 'next');
    btn('✕', stopTour).title = 'Завершити';
  }
  function runStep(s) {
    var step = s.steps[s.i];
    if (step.url && !samePage(step.url)) { ss(KEY_TOUR, s); location.href = step.url; return; }
    renderBar(s);
    if (step.target) highlight(step.target, step.say, { tour: true });
  }
  function move(d) {
    var s = tourState(); if (!s) return;
    s.i = Math.max(0, Math.min(s.steps.length - 1, s.i + d));
    ss(KEY_TOUR, s); runStep(s);
  }
  function stopTour() { ss(KEY_TOUR, null); clear(); if (bar) { bar.remove(); bar = null; } }
  function tour(steps) {
    steps = (steps || []).filter(function (x) { return x && (!x.url || internal(x.url)); }).slice(0, 12);
    if (!steps.length) return;
    var s = { steps: steps, i: 0, at: Date.now() };
    ss(KEY_TOUR, s); runStep(s);
  }

  function resume() {
    var pend = ss(KEY_SHOW);
    if (pend) {
      ss(KEY_SHOW, null);
      if (Date.now() - (pend.at || 0) < FRESH_MS) setTimeout(function () { highlight(pend.target, pend.say); }, 450);
    }
    var s = tourState();
    if (s) setTimeout(function () { runStep(s); }, 450);
  }

  window.MVShow = { go: go, tour: tour, find: find, highlight: highlight, stop: stopTour };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resume);
  else resume();
})();
