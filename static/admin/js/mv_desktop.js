/* 🧭 Новий вигляд на ПК (html[data-mv-design="new"], ширше 768 px; core/main_menu.py, CSS — mv_desktop.css).
 *
 * Бокове меню: «Просте» (⭐ Мої + кілька груп, кожну можна згорнути) / «Повне» (усі групи base_site у тому самому
 * вигляді: значки, рядки 34 px, ＋ «Додати» при наведенні, без дублів, «⋯ Усі сторінки розділу», лічильники
 * «Вхідні» / «Сигнали», підсвічена поточна сторінка; відкриті групи лишаються відкритими або «Розкривати → Лише групу
 * поточної сторінки»). Клік у будь-якому місці назви групи — згорнути / розгорнути; огляд — перший пункт
 * «↗ Огляд розділу».
 * Лише значки (html.mvd-rail): «📐 Бокове меню на ПК» (core/ui_prefs.py: auto / wide / rail; ⇤ / ⇥ унизу меню);
 * «Само звужується» — вікно вужче за 1200 px або відкрита панель праворуч (подія mv:panel {open, width} від
 * mv_panel.js) і списку тісно. Підменю групи — наведення, клік, клавіатура (Enter / → — відкрити, ↑ ↓ — пункти,
 * Esc / ← — назад, Tab — далі), підказки з назвою. Унизу меню — тема, 🎨 вигляд, ❓ FAQ, ⇤ / ⇥.
 * Правий клік (window.MVMenu — mv_menu.js; Shift + правий клік — меню браузера): на пункті — ⭐ у Мої, вище / нижче,
 * свій значок, нова вкладка; на групі — огляд, згорнути всі, «Розкривати», ⬆ ⬇ групу (як ⠿, але без миші), свій
 * значок групи, лише значки.
 * Шапка: пошук усюди (Ctrl K — розділи меню й «шукати … у Замовленнях / Товарах / Клієнтах / Пошті»), кружок з
 * ініціалами — акаунт. Списки: фільтри кнопкою «⚙ Фільтри · N» у висувній панелі праворуч (або колонкою —
 * «📌 Збоку»); назва сторінки й кнопки над нею — одним рядком, що переноситься.
 * Вибір — у профілі (POST /core/menu/; ширина меню — POST /core/my-settings/ {ui: {sidebar}}). Класичний вигляд —
 * лише пігулка «✨ Новий вигляд» угорі меню.
 * Для інших: --mvd-side-w на <html> (ширина меню, CSS), подія mv:sidebar {rail, width} на document, коли меню
 * звузилось чи розширилось не через mv:panel, MVDesktop.reveal(el) / railOn() / sideWidth().
 */
(function () {
  'use strict';
  if (window.MVDesktop) return;
  var root = document.documentElement;
  var cfgEl = document.getElementById('mv-menu-config');
  var cfg = null;
  try { cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { cfg = null; }
  if (!cfg) return;
  var NEW = root.getAttribute('data-mv-design') === 'new';
  var st = cfg.prefs;
  var side = null, snav = null, top = null, foot = null, ctx = null, ctxBack = null, pal = null, palBack = null;
  var fbtn = null, toastT = 0;
  var NOT_FILTERS = ['q', 'p', 'o', 'e', 'all', '_popup', '_to_field', '_changelist_filters'];
  var SIDEBAR = ['auto', 'wide', 'rail'];
  var WIDE_W = 268;                                  // ширина повного меню (--mvd-side-w у mv_desktop.css)
  var GROUP_ICON = { digikey: '🔷' };                // у MV_GROUPS теж 🛒, як «Продажі»: у ряду значків — однакові
  var THEMES = [['dark', '🌙', 'Темна'], ['light', '☀️', 'Світла'], ['minerva', '⚜️', 'Minerva'],
                ['hc-dark', '◐', 'Контраст — темна (≥ 7 : 1)'], ['hc-light', '◑', 'Контраст — світла (≥ 7 : 1)'],
                ['custom', '🎨', 'Власна']];
  // емодзі на початку назви («📝 Angebote»); не \p{Emoji_Component} — там і цифри
  var EMO = /^(\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)\s*/u;
  var SEV = { warn: 1, crit: 2 };
  var G = {}, ICON_BY_HREF = {}, ORIG_ORDER = [];     // MV_GROUPS — ще в порядку з base_site (сортує він згодом)
  var counts = {};                                    // ключ «Простого» меню → {n, sev} — лічильники з base_site
  var activeGroup = null, activeTarget = null, opened = false, quiet = 0, groupObs = null, openIds = null, headPlace = null;
  var fly = null, flyT = 0, flyC = 0, flyCalm = 0;
  var tip = null, tipT = 0, tipOwner = null, tipPend = null;
  var panelOpen = false, panelW = 0, inPanelEvent = false;
  var lastMax = -1, smRaf = 0;
  var mqWide = window.matchMedia ? window.matchMedia('(min-width: 769px)') : null;

  (window.MV_GROUPS || []).forEach(function (g) {
    if (!g || !g.id) return;
    G[g.id] = g;
    ORIG_ORDER.push(g.id);
    (g.links || []).forEach(function (l) {
      var k = l && l.href ? hrefKey(l.href) : '';
      if (k && l.icon && !ICON_BY_HREF[k]) ICON_BY_HREF[k] = l.icon;
    });
  });

  /* ── дрібниці ── */
  function csrf() { return (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ''; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function wide() { return mqWide ? mqWide.matches : window.innerWidth > 768; }
  function zoom() { return (document.body && parseFloat(getComputedStyle(document.body).zoom)) || 1; }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (err) { return null; } }
  function lsSet(k, v) {
    try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (err) { /* приватне вікно */ }
  }
  function isEl(x) { return !!(x && x.nodeType === 1); }
  function shown(x) { return !!(x && x.getClientRects().length); }
  function squash(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function hasMenu() { return !!(window.MVMenu && typeof window.MVMenu.open === 'function'); }
  function menuOpen() { return !!(window.MVMenu && typeof window.MVMenu.isOpen === 'function' && window.MVMenu.isOpen()); }
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }
  function toast(text, opts) {
    if (window.MVMenu && typeof window.MVMenu.toast === 'function') { window.MVMenu.toast(text, opts || {}); return; }
    var t = document.getElementById('mvd-toast');
    if (!t) { t = el('div'); t.id = 'mvd-toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = text; t.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove('on'); }, 3200);
  }
  function parse(h) { try { return new URL(h, location.href); } catch (err) { return null; } }
  function pathOf(h) { var u = parse(h); return u && u.origin === location.origin ? u.pathname + u.search : ''; }
  function hrefKey(h) { var u = parse(h); return u && u.origin === location.origin ? u.pathname + u.search + u.hash : ''; }
  /* назва пункту — лише власний текст (без значка-емодзі й лічильника base_site) */
  function labelOf(a) {
    if (!isEl(a)) return '';
    var l = a.querySelector('.mvd-lbl');
    if (l) return squash(l.textContent);
    var t = '';
    [].forEach.call(a.childNodes, function (n) { if (n.nodeType === 3) t += n.nodeValue; });
    return squash(t) || squash(a.getAttribute('aria-label')) || squash(a.textContent);
  }
  function linkLabel(a) {
    if (a.classList.contains('addlink') || a.classList.contains('mvd-add')) {
      var al = squash(a.getAttribute('aria-label'));
      if (al) return al;
      var tr = a.closest('tr');
      return 'Додати: ' + labelOf(tr && tr.querySelector('th a'));
    }
    return labelOf(a);
  }

  /* ── розділи ── */
  function catalog(key) { return cfg.items.filter(function (i) { return i.key === key; })[0]; }
  function byKey(key) {
    var link = st.links[key];
    if (link) return { key: key, icon: st.icons[key] || '🔗', label: link.label, href: link.href, custom: true };
    var it = catalog(key);
    return it ? { key: key, icon: st.icons[key] || it.icon, label: it.label, href: it.href } : null;
  }
  function keyForHref(href) {
    var path = pathOf(href);
    if (!path) return '';
    var hit = cfg.items.filter(function (i) { return i.href === path; })[0];
    if (hit) return hit.key;
    return st.links['url:' + path] ? 'url:' + path : '';
  }
  /* значок пункту повного меню: свій (розділ / сторінка) → з MV_GROUPS → емодзі з назви (там, де викликають) */
  function iconFor(href) {
    var p = pathOf(href);
    if (!p) return '';
    var k = keyForHref(href);
    return (k && st.icons[k]) || st.icons['url:' + p] || ICON_BY_HREF[hrefKey(href)] || '';
  }
  /* наскільки адреса пункту — це поточна сторінка: довжина шляху × 10 + 5 за кожен збіглий параметр (у будь-якому
     порядку) + 1 за якір; /admin/ — лише сама /admin/ */
  function score(href) {
    var u = parse(href);
    if (!u || u.origin !== location.origin || (u.hash && u.hash !== location.hash)) return 0;
    var p = u.pathname, here = location.pathname;
    if (p === '/admin/' ? here !== p : here.indexOf(p) !== 0) return 0;
    var q = new URLSearchParams(location.search), n = 0, ok = true;
    u.searchParams.forEach(function (v, k) { if (q.get(k) === v) n += 1; else ok = false; });
    return ok ? p.length * 10 + n * 5 + (u.hash ? 1 : 0) : 0;
  }
  function activeKey() {
    var best = '', bs = 0;
    st.pinned.map(byKey).concat(cfg.items).forEach(function (i) {
      if (!i) return;
      var s = score(i.href);
      if (s > bs) { bs = s; best = i.key; }
    });
    return best;
  }
  function save(change) {
    return post(cfg.saveUrl, change).then(function (d) {
      if (d && d.prefs) { st = d.prefs; renderMenu(); }
      if (d && d.error) toast(d.error);
      return d;
    }).catch(function () { toast('Не вдалося зберегти — перевірте зв’язок.'); });
  }
  function iconsFull(key) { return !(key in st.icons) && Object.keys(st.icons).length >= (cfg.maxIcons || 60); }

  /* ── лічильники: з base_site (Вхідні, Сигнали) — атрибутами, малює CSS ── */
  function better(a, b) {
    if (!b || !b.n) return a;
    if (!a) return b;
    var ra = SEV[a.sev] || 0, rb = SEV[b.sev] || 0;
    return rb > ra || (rb === ra && b.n > a.n) ? b : a;
  }
  function setCount(node, c) {
    if (c && c.n) {
      node.setAttribute('data-mvd-count', String(c.n));
      if (c.sev) node.setAttribute('data-mvd-sev', c.sev); else node.removeAttribute('data-mvd-sev');
    } else {
      node.removeAttribute('data-mvd-count');
      node.removeAttribute('data-mvd-sev');
    }
  }
  function paintCounts() {
    if (!snav) return;
    [].forEach.call(snav.querySelectorAll('a.mvd-item[data-key]'), function (a) {
      var c = counts[a.dataset.key], s = a.querySelector(':scope > .mvd-cnt');
      if (c && c.n) {
        if (!s) { s = el('span', 'mvd-cnt'); a.appendChild(s); }
        s.textContent = String(c.n);
        if (c.sev) s.setAttribute('data-mvd-sev', c.sev); else s.removeAttribute('data-mvd-sev');
      } else if (s) s.remove();
    });
    [].forEach.call(snav.querySelectorAll('.mvd-group[data-sg]'), function (g) {
      var best = null, b = g.querySelector(':scope > .mvd-gbtn');
      [].forEach.call(g.querySelectorAll('a.mvd-item[data-key]'), function (a) { best = better(best, counts[a.dataset.key]); });
      if (b) setCount(b, best);
    });
  }
  function aggregate() {
    groupsList().forEach(function (g) {
      var head = g.querySelector(':scope > .mg-header'), best = null;
      if (!head) return;
      [].forEach.call(g.querySelectorAll('a.mg-link[data-mvd-count]'), function (a) {
        best = better(best, { n: +a.getAttribute('data-mvd-count') || 0, sev: a.getAttribute('data-mvd-sev') || '' });
      });
      setCount(head, best);
    });
  }
  /* base_site дописує лічильник у посилання <span style=…> (Вхідні — innerHTML, Сигнали — «N ❗» / «N ⚠» / «✓») */
  function takeBadge(span) {
    if (!isEl(span) || span.tagName !== 'SPAN' || !span.hasAttribute('style') || span.classList.contains('mvd-src')) return;
    var a = span.parentElement;
    if (!a || a.tagName !== 'A' || !a.classList.contains('mg-link')) return;
    span.classList.add('mvd-src');
    var t = squash(span.textContent), n = parseInt(t, 10) || 0;
    var c = n > 0 ? { n: n, sev: t.indexOf('❗') >= 0 ? 'crit' : t.indexOf('⚠') >= 0 ? 'warn' : '' } : null;
    setCount(a, c);
    var key = keyForHref(a.getAttribute('href'));
    if (key) counts[key] = c;
    aggregate();
    paintCounts();
  }
  function watchBadges() {
    if (!side) return;
    [].forEach.call(side.querySelectorAll('a.mg-link > span[style]'), takeBadge);
    if (!window.MutationObserver) return;
    new MutationObserver(function (recs) {
      recs.forEach(function (r) {
        [].forEach.call(r.addedNodes, function (n) {
          if (!isEl(n)) return;
          if (n.tagName === 'SPAN') takeBadge(n);
          else [].forEach.call(n.querySelectorAll('a.mg-link > span[style]'), takeBadge);
        });
      });
    }).observe(side, { childList: true, subtree: true });
  }

  /* ── «Просте» меню ── */
  function paintSeg() {
    if (!top) return;
    [].forEach.call(top.querySelectorAll('.mvd-seg button'), function (b) {
      b.setAttribute('aria-pressed', b.dataset.mode === st.mode ? 'true' : 'false');
    });
  }
  function ico(text) { var i = el('span', 'mvd-ico', text); i.setAttribute('aria-hidden', 'true'); return i; }
  function renderMenu() {
    if (!side || !snav) return;
    closeFly();
    root.classList.toggle('mvd-simple', st.mode === 'simple');
    paintSeg();
    snav.textContent = '';
    var active = activeKey();
    function item(it) {
      var a = el('a', 'mvd-item' + (it.key === active ? ' on' : ''));
      a.href = it.href; a.dataset.key = it.key;
      if (it.key === active) a.setAttribute('aria-current', 'page');
      a.appendChild(ico(it.icon));
      a.appendChild(el('span', 'mvd-lbl', it.label));
      return a;
    }
    var mine = el('div', 'mvd-group mvd-mine'), pinned = st.pinned.map(byKey).filter(Boolean);
    mine.appendChild(el('div', 'mvd-gtitle', '⭐ Мої'));
    pinned.forEach(function (it) { mine.appendChild(item(it)); });
    if (!pinned.length) mine.appendChild(el('div', 'mvd-hint', 'Правий клік на пункті меню → «⭐ У Мої».'));
    snav.appendChild(mine);
    cfg.groups.forEach(function (g) {
      var items = g.keys.filter(function (k) { return st.pinned.indexOf(k) < 0; }).map(byKey).filter(Boolean);
      if (!items.length) return;
      var id = g.id || squash(g.title), box = el('div', 'mvd-group'), gid = 'mvd-sg-' + id;
      // група з поточною сторінкою відкрита завжди (не зберігається)
      var shut = lsGet('mvd-sg-' + id) === '1' && !items.some(function (it) { return it.key === active; });
      box.dataset.sg = id;
      if (shut) box.classList.add('mvd-shut');
      var b = el('button', 'mvd-gtitle mvd-gbtn'); b.type = 'button';
      b.setAttribute('aria-controls', gid);
      b.setAttribute('aria-expanded', shut ? 'false' : 'true');
      b.appendChild(ico(st.icons['sg:' + id] || g.icon || '•'));
      b.appendChild(el('span', 'mvd-lbl', g.title));
      var ch = el('span', 'mvd-chev'); ch.setAttribute('aria-hidden', 'true');
      b.appendChild(ch);
      b.addEventListener('click', function () {            // у ряду значків клік перехоплює підменю (wireRail)
        var now = !box.classList.contains('mvd-shut');
        box.classList.toggle('mvd-shut', now);
        b.setAttribute('aria-expanded', now ? 'false' : 'true');
        lsSet('mvd-sg-' + id, now ? '1' : null);
      });
      box.appendChild(b);
      var list = el('div', 'mvd-gitems'); list.id = gid;
      var ft = el('div', 'mvd-fly-title', g.title); ft.setAttribute('aria-hidden', 'true');
      list.appendChild(ft);
      items.forEach(function (it) { list.appendChild(item(it)); });
      box.appendChild(list);
      snav.appendChild(box);
    });
    var all = el('button', 'mvd-item mvd-all'); all.type = 'button';
    all.appendChild(ico('☰'));
    all.appendChild(el('span', 'mvd-lbl', 'Усі розділи…'));
    all.onclick = function () { setMode('full'); };
    snav.appendChild(all);
    paintCounts();
    applyIcons();
    syncAria();
  }
  function setMode(m) { st.mode = m; renderMenu(); save({ mode: m }); }
  function buildMenu() {
    side = document.getElementById('nav-sidebar');
    if (!side) { root.classList.add('mvd-noside'); return; }
    top = el('div'); top.id = 'mvd-top';
    var seg = el('div', 'mvd-seg'); seg.setAttribute('role', 'group'); seg.setAttribute('aria-label', 'Меню');
    [['simple', 'Просте'], ['full', 'Повне']].forEach(function (m) {
      var b = el('button', '', m[1]); b.type = 'button'; b.dataset.mode = m[0];
      b.onclick = function () { setMode(m[0]); };
      seg.appendChild(b);
    });
    top.appendChild(seg);
    snav = el('nav'); snav.id = 'mvd-snav'; snav.setAttribute('aria-label', 'Меню');
    side.insertBefore(snav, side.firstChild);
    side.insertBefore(top, side.firstChild);
    side.addEventListener('contextmenu', onSideMenu);
    renderMenu();
  }

  /* ── «Повне» меню: групи base_site у вигляді «Простого» (DOM той самий і на телефоні — там видно як було) ── */
  function groupsList() { return side ? [].slice.call(side.querySelectorAll(':scope > .mg-group[data-group-id]')) : []; }
  function splitEmoji(a) {                         // «📝 Angebote» → <span class=mvd-emo>📝 </span>Angebote
    var t = a && a.firstChild;
    if (!t || t.nodeType !== 3) return '';
    var m = t.nodeValue.match(EMO);
    if (!m) return '';
    a.insertBefore(el('span', 'mvd-emo', m[0]), t);
    t.nodeValue = t.nodeValue.slice(m[0].length);
    return m[1];
  }
  function chev(host) { var c = el('span', 'mvd-chev'); c.setAttribute('aria-hidden', 'true'); host.appendChild(c); }
  function enhanceFull() {
    if (!side) return;
    if (window.MutationObserver && !groupObs) groupObs = new MutationObserver(function (recs) { onGroups(recs, false); });
    groupsList().forEach(function (g) {
      if (g.dataset.mvd) return;
      var head = g.querySelector(':scope > .mg-header'), content = g.querySelector(':scope > .mg-content');
      if (!head || !content) return;
      g.dataset.mvd = '1';
      var id = g.dataset.groupId;
      var link = head.querySelector('.mg-header-link'), btn = head.querySelector('.mg-arrow-btn');
      head._mvdEmo = link ? splitEmoji(link) : '';
      var label = labelOf(link) || id;
      content.id = 'mvd-gc-' + id;
      if (btn) {
        btn.type = 'button';
        btn.removeAttribute('title');
        btn.setAttribute('aria-label', label);
        btn.setAttribute('aria-controls', content.id);
      }
      var ft = el('div', 'mvd-fly-title', label); ft.setAttribute('aria-hidden', 'true');
      content.insertBefore(ft, content.firstChild);
      // пункти групи (MV_GROUPS)
      var curated = {}, nCurated = 0;
      [].forEach.call(content.querySelectorAll(':scope > a.mg-link'), function (a) {
        a._mvdEmo = splitEmoji(a);
        var p = pathOf(a.getAttribute('href'));
        if (p && !curated[p]) curated[p] = a;
        nCurated += 1;
      });
      // сторінка групи: «↗ Огляд розділу» першим пунктом, якщо її немає серед пунктів
      var navHref = link ? link.getAttribute('href') : '', navPath = pathOf(navHref);
      if (navPath && !curated[navPath]) {
        var ov = el('a', 'mg-link mvd-overview', 'Огляд розділу');
        ov.href = navHref;
        ov.setAttribute('data-mvd-icon', '↗');
        ov.dataset.mvdFixed = '1';
        content.insertBefore(ov, ft.nextSibling);
        g._mvdHead = ov;
      } else g._mvdHead = curated[navPath] || null;
      // «⋯ Усі сторінки розділу» base_site (група з more: true) — у тому самому вигляді
      var det = content.querySelector(':scope > details.mg-more'), sum = det && det.querySelector(':scope > summary');
      if (sum) {
        var t0 = sum.firstChild, mm = t0 && t0.nodeType === 3 ? t0.nodeValue.match(/^\s*⋯\s*/) : null;
        if (mm) { sum.insertBefore(el('span', 'mvd-emo', mm[0]), t0); t0.nodeValue = t0.nodeValue.slice(mm[0].length); }
        sum.setAttribute('data-mvd-icon', '⋯');
        sum.dataset.mvdFixed = '1';
        chev(sum);
      }
      // таблиці Django: значок, дубль пункту групи — сховати (його ＋ — біля пункту), ＋ з назвою моделі
      var live = 0, firstMod = null;
      [].forEach.call(content.querySelectorAll('.module'), function (mod) {
        if (!firstMod && mod.parentNode === content) firstMod = mod;
        var rows = 0;
        [].forEach.call(mod.querySelectorAll('tbody tr'), function (tr) {
          var a = tr.querySelector('th a'), add = tr.querySelector('td a.addlink');
          if (a) a._mvdEmo = splitEmoji(a);
          var name = a ? labelOf(a) : squash(tr.textContent);
          var twin = a ? curated[pathOf(a.getAttribute('href'))] : null;
          if (twin) {
            tr.classList.add('mvd-dup');
            tr._mvdTwin = twin;
            if (add && !twin._mvdAdd) {
              var row = el('div', 'mvd-row');
              twin.parentNode.insertBefore(row, twin);
              row.appendChild(twin);
              var plus = el('a', 'mvd-add', '＋');
              plus.href = add.getAttribute('href');
              plus.setAttribute('aria-label', 'Додати: ' + name);
              row.appendChild(plus);
              twin._mvdAdd = plus;
            }
          } else rows += 1;
          if (add) add.setAttribute('aria-label', 'Додати: ' + name);
        });
        if (!rows) mod.classList.add('mvd-empty');
        live += rows;
      });
      // багато пунктів групи — решту сторінок розділу (таблиці Django) згорнуто в «⋯ Усі сторінки розділу»
      if (!det && nCurated >= 5 && live > 0 && firstMod) {
        g.classList.add('mvd-mods');
        var mb = el('button', 'mg-link mvd-more', 'Усі сторінки розділу'); mb.type = 'button';
        mb.setAttribute('data-mvd-icon', '⋯');
        mb.setAttribute('data-mvd-count', String(live));
        mb.dataset.mvdFixed = '1';
        mb.setAttribute('aria-expanded', 'false');
        chev(mb);
        mb.addEventListener('click', function () { setMods(g, !g.classList.contains('mvd-mods-open'), true); });
        content.insertBefore(mb, firstMod);
        if (lsGet('mg-more-' + id) === '1') g.classList.add('mvd-mods-open');
      }
      if (groupObs) groupObs.observe(g, { attributes: true, attributeFilter: ['class'], attributeOldValue: true });
    });
    applyIcons();
    headLinks();
    syncAria();
  }
  function applyIcons() {
    groupsList().forEach(function (g) {
      if (!g.dataset.mvd) return;
      var id = g.dataset.groupId, head = g.querySelector(':scope > .mg-header');
      if (head) head.setAttribute('data-mvd-icon',
        st.icons['grp:' + id] || GROUP_ICON[id] || (G[id] && G[id].icon) || head._mvdEmo || '•');
      [].forEach.call(g.querySelectorAll('.mg-content a.mg-link[href], .mg-content .module th > a[href]'), function (a) {
        if (a.dataset.mvdFixed) return;
        var ic = iconFor(a.getAttribute('href')) || a._mvdEmo || '';
        if (ic) a.setAttribute('data-mvd-icon', ic); else a.removeAttribute('data-mvd-icon');
      });
    });
  }
  /* назва групи накрита кнопкою ▼ (уся назва згортає групу) — посилання не в Tab і не для читачів екрана (ПК) */
  function headLinks() {
    if (!side) return;
    var on = wide();
    [].forEach.call(side.querySelectorAll('.mg-group > .mg-header .mg-header-link'), function (a) {
      if (on) { a.tabIndex = -1; a.setAttribute('aria-hidden', 'true'); }
      else { a.removeAttribute('tabindex'); a.removeAttribute('aria-hidden'); }
    });
  }
  function modsOpen(g) {
    var det = g.querySelector(':scope > .mg-content > details.mg-more');
    return det ? det.open : g.classList.contains('mvd-mods-open');
  }
  function setMods(g, on, remember) {
    var det = g.querySelector(':scope > .mg-content > details.mg-more');
    if (det) { det.open = on; return; }                   // base_site сам пише mg-more-<id> (подія toggle)
    if (!g.classList.contains('mvd-mods')) return;
    g.classList.toggle('mvd-mods-open', on);
    var mb = g.querySelector('.mvd-more');
    if (mb) mb.setAttribute('aria-expanded', on ? 'true' : 'false');
    if (remember) lsSet('mg-more-' + g.dataset.groupId, on ? '1' : '0');
  }
  function syncAria(only) {
    if (!side) return;
    var rail = railOn();
    (only ? [only] : groupsList()).forEach(function (g) {
      var b = g.querySelector(':scope > .mg-header .mg-arrow-btn');
      if (b) b.setAttribute('aria-expanded', (rail ? fly === g : !g.classList.contains('collapsed')) ? 'true' : 'false');
      var mb = g.querySelector('.mvd-more');
      if (mb) mb.setAttribute('aria-expanded', g.classList.contains('mvd-mods-open') ? 'true' : 'false');
    });
    if (!only && snav) [].forEach.call(snav.querySelectorAll('.mvd-group[data-sg]'), function (g) {
      var b = g.querySelector(':scope > .mvd-gbtn');
      if (b) b.setAttribute('aria-expanded', (rail ? fly === g : !g.classList.contains('mvd-shut')) ? 'true' : 'false');
    });
  }

  /* ── поточна сторінка й відкриті групи ── */
  function quietly(fn) {                            // розгорнути, не вважаючи це вибором людини
    quiet += 1;
    try { fn(); } finally {
      if (groupObs) onGroups(groupObs.takeRecords(), true);
      quiet -= 1;
    }
  }
  function onGroups(recs, silent) {
    var changed = false;
    recs.forEach(function (r) {
      var g = r.target;
      if (!isEl(g) || !g.classList.contains('mg-group')) return;
      syncAria(g);
      var was = (' ' + (r.oldValue || '') + ' ').indexOf(' collapsed ') >= 0, now = g.classList.contains('collapsed');
      if (!silent && !quiet && was !== now) { rememberOpen(g.dataset.groupId, !now); changed = true; }
    });
    if (changed) saveOpen();
  }
  function loadOpen() {
    try {
      var v = JSON.parse(lsGet('mvd-open') || 'null');
      return Array.isArray(v) ? v.filter(function (x) { return typeof x === 'string'; }) : null;
    } catch (err) { return null; }
  }
  function rememberOpen(id, open) {
    if (!openIds) openIds = loadOpen() || [];
    var i = openIds.indexOf(id);
    if (open && i < 0) openIds.push(id);
    if (!open && i >= 0) openIds.splice(i, 1);
  }
  function saveOpen() { lsSet('mvd-open', JSON.stringify(openIds || [])); }
  function expandAround(x) {
    var g = x.closest('.mg-group');
    if (!g) return;
    g.classList.remove('collapsed');
    for (var n = x.parentElement; n && n !== g; n = n.parentElement) {
      if (n.tagName === 'DETAILS' && !n.open) n.open = true;
      if (n.classList.contains('module')) {
        n.classList.remove('mg-mod-collapsed');
        if (g.classList.contains('mvd-mods')) setMods(g, true, false);
      }
    }
  }
  function markActive() {
    if (!side) return;
    var best = null, bs = 0, bCur = false;
    [].forEach.call(side.querySelectorAll('.mg-group a[href]:not(.addlink):not(.mvd-add)'), function (a) {
      var s = score(a.getAttribute('href'));
      if (!s) return;
      var cur = !a.closest('.module');                // пункт групи кращий за рядок таблиці Django
      if (s > bs || (s === bs && cur && !bCur)) { best = a; bs = s; bCur = cur; }
    });
    [].forEach.call(side.querySelectorAll('.mvd-on'), function (x) { x.classList.remove('mvd-on'); });
    [].forEach.call(side.querySelectorAll('.mvd-here'), function (x) { x.classList.remove('mvd-here'); });
    [].forEach.call(side.querySelectorAll('.mg-group [aria-current]'), function (x) { x.removeAttribute('aria-current'); });
    activeGroup = null;
    activeTarget = null;
    if (!best) return;
    var g = best.closest('.mg-group');
    if (best.classList.contains('mg-header-link') && g && g._mvdHead) best = g._mvdHead;   // сторінка групи → «Огляд»
    var tr = best.closest('tr');
    if (tr && tr._mvdTwin) { best = tr._mvdTwin; tr = null; }                              // дубль → пункт групи
    g = best.closest('.mg-group');
    if (!g) return;
    var target = tr || best;
    target.classList.add('mvd-on');
    best.setAttribute('aria-current', 'page');
    g.classList.add('mvd-here');
    activeGroup = g;
    activeTarget = target;
  }
  /* розгорнути довкола поточної сторінки й відкрити групи, як людина лишила, — лише на ПК (на телефоні шухляда
     mv_mobile — як збудував base_site); вікно розширили чи повернули планшет — тоді, один раз */
  function openAround() {
    if (opened || !side || !wide()) return;
    opened = true;
    var t = activeTarget;
    if (t && t.isConnected) {
      quietly(function () { expandAround(t); });
      requestAnimationFrame(function () {           // лише прокрутка меню (scrollIntoView прокрутив би й сторінку)
        if (!side || !t.isConnected) return;
        var r = t.getBoundingClientRect(), s = side.getBoundingClientRect();
        if (!r.height || !s.height) return;
        if (r.top < s.top + 48 || r.bottom > s.bottom - 64) side.scrollTop += (r.top - s.top - s.height / 3) / zoom();
      });
    }
    restoreOpen();
  }
  function restoreOpen() {
    var list = groupsList();
    if (!list.length) return;
    openIds = loadOpen();
    if (!openIds) {                                  // уперше — як лишив base_site
      openIds = list.filter(function (g) { return !g.classList.contains('collapsed'); })
        .map(function (g) { return g.dataset.groupId; });
      saveOpen();
    }
    quietly(function () {
      list.forEach(function (g) {
        if (st.expand === 'current') { if (activeGroup) g.classList.toggle('collapsed', g !== activeGroup); }
        else g.classList.toggle('collapsed', !(g === activeGroup || openIds.indexOf(g.dataset.groupId) >= 0));
      });
    });
  }
  function setCollapsed(list, c) {                  // вибір людини: пам'ятається (mvd-open і mg-<id> base_site)
    list.forEach(function (g) {
      g.classList.toggle('collapsed', c);
      lsSet('mg-' + g.dataset.groupId, c ? '1' : '0');
    });
  }
  function setExpand(v) { st.expand = v; restoreOpen(); save({ expand: v }); }
  function moveGroup(g, d) {                         // те саме, що перетягування ⠿ (mg-order — формат base_site)
    var list = groupsList(), i = list.indexOf(g), j = i + d;
    if (i < 0 || j < 0 || j >= list.length) return;
    side.insertBefore(g, d < 0 ? list[j] : list[j].nextSibling);
    saveOrder();
    var b = flyBtn(g);
    if (b && (!document.activeElement || document.activeElement === document.body)) b.focus({ preventScroll: true });
  }
  function saveOrder() { lsSet('mg-order', JSON.stringify(groupsList().map(function (x) { return x.dataset.groupId; }))); }
  function resetOrder() {
    lsSet('mg-order', null);
    var list = groupsList();
    if (!list.length) return;
    var after = list[list.length - 1].nextSibling;
    list.sort(function (a, b) { return ORIG_ORDER.indexOf(a.dataset.groupId) - ORIG_ORDER.indexOf(b.dataset.groupId); });
    list.forEach(function (g) { side.insertBefore(g, after); });
  }

  /* ── низ меню: тема, 🎨, ❓ FAQ, ⇤ / ⇥ ── */
  function tool(tag, cls, icon, label) {
    var b = el(tag, 'mvd-tool' + (cls ? ' ' + cls : ''));
    if (tag === 'button') b.type = 'button';
    b.appendChild(ico(icon));
    if (label) b.appendChild(el('span', 'mvd-lbl', label));
    return b;
  }
  function themeIcon() {
    var t = root.getAttribute('data-theme') || 'dark';
    for (var i = 0; i < THEMES.length; i++) if (THEMES[i][0] === t) return THEMES[i][1];
    return '🌙';
  }
  function themeName() {
    var t = root.getAttribute('data-theme') || 'dark';
    for (var i = 0; i < THEMES.length; i++) if (THEMES[i][0] === t) return THEMES[i][2];
    return '';
  }
  function paintTheme() {
    var i = foot && foot.querySelector('.mvd-theme .mvd-ico');
    if (i) i.textContent = themeIcon();
  }
  function paintRailBtn() {
    var b = document.getElementById('mvd-railbtn');
    if (!b) return;
    var on = railOn(), i = b.querySelector('.mvd-ico');
    if (i) i.textContent = on ? '⇥' : '⇤';
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.setAttribute('aria-label', on ? 'Розгорнути меню' : 'Згорнути меню до значків');
  }
  function buildFoot() {
    if (!side || document.getElementById('mvd-foot')) return;
    foot = el('div'); foot.id = 'mvd-foot';
    var th = tool('button', 'mvd-theme', themeIcon());
    th.setAttribute('aria-label', 'Тема оформлення'); th.setAttribute('aria-haspopup', 'menu');
    th.addEventListener('click', function () { themeMenu(th); });
    var lk = tool('button', 'mvd-look', '🎨');
    lk.setAttribute('aria-label', 'Вигляд Minerva'); lk.setAttribute('aria-haspopup', 'menu');
    lk.addEventListener('click', function () { lookMenu(lk); });
    var faq = tool('a', 'mvd-faq', '❓', 'FAQ');
    faq.href = '/admin/faq/faqplaceholder/';
    faq.title = 'FAQ та підтримка · Послуги';
    var rb = tool('button', '', '⇤'); rb.id = 'mvd-railbtn';
    rb.addEventListener('click', function () { setSidebar(railOn() ? 'wide' : 'rail'); });
    [th, lk, faq, rb].forEach(function (x) { foot.appendChild(x); });
    side.appendChild(foot);
    paintRailBtn();
  }
  function setTheme(t) {                             // лише коли немає перемикача base_site (#mv-theme-sw)
    root.setAttribute('data-theme', t);
    lsSet('mv-theme', t);
    lsSet('theme', (t === 'light' || t === 'hc-light') ? 'light' : 'dark');
    post('/core/my-settings/', { ui: { theme: t } }).catch(function () {});
  }
  function themeItems() {
    var sw = document.getElementById('mv-theme-sw'), cur = root.getAttribute('data-theme') || 'dark', out = [];
    THEMES.forEach(function (t) {
      var b = sw ? sw.querySelector('button[data-t="' + t[0] + '"]') : null;
      if (!b && (sw || t[0] === 'custom')) return;    // «Власна» — лише коли вона є
      out.push({ type: 'radio', group: 'theme', icon: t[1], label: t[2], checked: cur === t[0],
                 onSelect: function () { if (b) b.click(); else setTheme(t[0]); } });   // base_site: тема + профіль
    });
    out.push({ type: 'separator' });
    out.push({ label: 'Шрифт, розмір тексту, контраст…', icon: '♿', href: '/core/my-settings/#a11y' });
    return out;
  }
  function menuSide(anchor) {
    if (!isEl(anchor)) return 'bottom';
    if (anchor.closest('#mvd-foot')) return railOn() ? 'right' : 'top';
    return anchor.closest('#header') ? 'bottom-end' : 'bottom';
  }
  function lookAnchor() {
    var b = document.querySelector('#mvd-foot .mvd-look');
    return shown(b) ? b : { x: Math.max(8, window.innerWidth / 2 - 140), y: 80 };
  }
  function themeMenu(anchor) {
    if (!hasMenu()) { location.href = '/core/my-settings/'; return; }
    window.MVMenu.open(anchor, themeItems(), { title: 'Тема оформлення', side: menuSide(anchor), focus: 'checked' });
  }
  function sidebarItems() {
    var sb = root.getAttribute('data-mv-sidebar') || 'auto';
    return [
      { type: 'radio', group: 'sidebar', label: 'Само звужується до значків', checked: sb === 'auto', close: true,
        onSelect: function () { setSidebar('auto'); } },
      { type: 'radio', group: 'sidebar', label: 'Завжди повне', checked: sb === 'wide', close: true,
        onSelect: function () { setSidebar('wide'); } },
      { type: 'radio', group: 'sidebar', label: 'Завжди значки', checked: sb === 'rail', close: true,
        onSelect: function () { setSidebar('rail'); } }
    ];
  }
  function lookMenu(anchor) {
    if (!hasMenu()) { location.href = '/core/my-settings/#a11y'; return; }
    var at = isEl(anchor) ? anchor : lookAnchor();
    var items = [
      { type: 'title', label: 'Меню' },
      { type: 'radio', group: 'mode', icon: '✨', label: 'Просте', checked: st.mode === 'simple',
        onSelect: function () { setMode('simple'); } },
      { type: 'radio', group: 'mode', icon: '☰', label: 'Повне', checked: st.mode !== 'simple',
        onSelect: function () { setMode('full'); } },
      { type: 'separator' },
      { type: 'title', label: 'Бокове меню' }
    ].concat(sidebarItems(), [
      { type: 'separator' },
      { type: 'submenu', icon: '🌗', label: 'Тема', hint: themeName, items: themeItems },
      { type: 'separator' },
      { type: 'title', label: 'Вигляд на ПК' },
      { type: 'radio', group: 'design', label: 'Новий', checked: NEW, onSelect: function () { design('new'); } },
      { type: 'radio', group: 'design', label: 'Класичний — як було', checked: !NEW,
        onSelect: function () { design('classic'); } },
      { type: 'separator' },
      { label: 'Шрифт, розмір тексту, контраст…', icon: '♿', href: '/core/my-settings/#a11y' }
    ]);
    window.MVMenu.open(at, items, { title: 'Вигляд Minerva', side: menuSide(at) });
  }
  function design(v) {
    if ((v === 'new') === NEW) return;
    post('/core/my-settings/', { ui: { design: v } }).then(function () { location.reload(); })
      .catch(function () { toast('Не вдалося зберегти — перевірте зв’язок.'); });
  }

  /* ── лише значки: html.mvd-rail ── */
  function railOn() { return root.classList.contains('mvd-rail'); }
  function sideWidth() {
    var s = side || document.getElementById('nav-sidebar');
    if (!s || !wide()) return 0;
    if (!NEW) {
      var c = document.querySelector('.main.shifted > #nav-sidebar');
      return c ? Math.round(c.getBoundingClientRect().width / zoom()) : 0;
    }
    return parseFloat(getComputedStyle(root).getPropertyValue('--mvd-side-w')) || 0;
  }
  function wantRail() {
    if (!NEW || !wide()) return { on: false, byPanel: false };
    var p = root.getAttribute('data-mv-sidebar') || 'auto', eff = window.innerWidth / zoom();
    if (SIDEBAR.indexOf(p) < 0) p = 'auto';
    var base = p === 'rail' || (p === 'auto' && eff < 1200);
    var byPanel = !base && p === 'auto' && panelOpen && eff - WIDE_W - panelW - 48 < 960;
    return { on: base || byPanel, byPanel: byPanel };
  }
  /* синхронно: mv_panel.js читає --mvd-side-w одразу після своєї події mv:panel */
  function applyRail() {
    if (!NEW) return;
    var w = wantRail();
    if (w.on !== railOn()) {
      closeFly();
      hideTip();
      root.classList.toggle('mvd-rail', w.on);
      syncAria();
      if (w.on && w.byPanel) railTip();
      if (!inPanelEvent) {
        try {
          document.dispatchEvent(new CustomEvent('mv:sidebar', { detail: { rail: w.on, width: sideWidth() } }));
        } catch (err) { /* старий браузер */ }
      }
    }
    paintRailBtn();
    sideMax();
  }
  function railTip() {                               // уперше, коли меню звузила панель праворуч
    if (lsGet('mvd-rail-tip')) return;
    lsSet('mvd-rail-tip', '1');
    toast('Меню згорнуто до значків, поки відкрита панель праворуч',
          { action: { label: 'Змінити', run: function () { lookMenu(); } } });
  }
  function setSidebar(v) {
    if (SIDEBAR.indexOf(v) < 0) return;
    root.setAttribute('data-mv-sidebar', v);
    applyRail();
    post('/core/my-settings/', { ui: { sidebar: v } }).catch(function () { toast('Не вдалося зберегти — перевірте зв’язок.'); });
  }
  function railItem() {
    return railOn() ? { label: 'Широке меню', icon: '⇥', onSelect: function () { setSidebar('wide'); } }
                    : { label: 'Лише значки', icon: '⇤', onSelect: function () { setSidebar('rail'); } };
  }
  /* підменю групи в ряду значків — сам .mg-content / .mvd-gitems групи (без копії), position: fixed */
  function flyBox(g) { return g ? g.querySelector(':scope > .mg-content, :scope > .mvd-gitems') : null; }
  function flyBtn(g) { return g ? g.querySelector(':scope > .mg-header .mg-arrow-btn, :scope > .mvd-gbtn') : null; }
  function focusables(box) { return [].filter.call(box.querySelectorAll('a[href], button, summary'), shown); }
  function focusIn(box, x) {                         // без прокрутки самого меню (воно закрило б підменю)
    x.focus({ preventScroll: true });
    var r = x.getBoundingClientRect(), b = box.getBoundingClientRect(), z = zoom();
    if (r.top < b.top) box.scrollTop -= (b.top - r.top) / z + 4;
    else if (r.bottom > b.bottom) box.scrollTop += (r.bottom - b.bottom) / z + 4;
  }
  function openFly(g, focusFirst) {
    if (!side || !g || !railOn()) return;
    var box = flyBox(g), b = flyBtn(g);
    if (!box || !b) return;
    clearTimeout(flyT); clearTimeout(flyC);
    if (fly && fly !== g) closeFly();
    fly = g;
    flyCalm = Date.now() + 300;
    g.classList.add('mvd-fly');                       // стан — на групі й меню, не на <html> (контраст — лише тут)
    side.classList.add('mvd-flying');
    b.setAttribute('aria-expanded', 'true');
    hideTip();
    placeFly();
    box.scrollTop = 0;
    if (focusFirst) { var f = focusables(box)[0]; if (f) focusIn(box, f); }
  }
  /* біля свого значка, у межах вікна (px ÷ zoom); прокрутили сторінку — значок зсунувся, підменю за ним */
  function placeFly() {
    var box = flyBox(fly), b = flyBtn(fly);
    if (!box || !b) return;
    var z = zoom(), r = b.getBoundingClientRect(), vh = window.innerHeight;
    var h = Math.min(box.scrollHeight * z, vh - 16), t = Math.max(8, Math.min(r.top, vh - h - 8));
    box.style.setProperty('--mvd-fly-top', t / z + 'px');
    box.style.setProperty('--mvd-fly-max', (vh - t - 8) / z + 'px');
  }
  function closeFly(back) {
    clearTimeout(flyT); clearTimeout(flyC);
    if (!fly) return;
    var g = fly;
    fly = null;
    g.classList.remove('mvd-fly');
    if (side) side.classList.remove('mvd-flying');
    var b = flyBtn(g);
    if (b) { b.setAttribute('aria-expanded', 'false'); if (back) b.focus({ preventScroll: true }); }
  }
  function closeFlyIdle() { if (!menuOpen() && !ctx) closeFly(); }   // з меню правого кліку підменю не зникає
  function onFlyOver(e) {
    if (!railOn() || e.pointerType !== 'mouse' || !isEl(e.target)) return;
    clearTimeout(flyC);
    var box = fly ? flyBox(fly) : null;
    if (box && box.contains(e.target)) { clearTimeout(flyT); return; }
    var h = e.target.closest('.mg-header, .mvd-gbtn'), g = h ? h.closest('.mg-group, .mvd-group') : null;
    clearTimeout(flyT);
    if (g && g === fly) return;
    if (g && flyBox(g)) { flyT = setTimeout(function () { openFly(g); }, fly ? 90 : 140); return; }
    if (fly) flyC = setTimeout(closeFlyIdle, 320);
  }
  function onFlyLeave(e) {
    if (e.pointerType && e.pointerType !== 'mouse') return;
    clearTimeout(flyT);
    if (fly) flyC = setTimeout(closeFlyIdle, 320);
  }
  function railControls() {
    return [].filter.call(side.querySelectorAll('.mg-group > .mg-header .mg-arrow-btn, #mvd-snav > .mvd-group > .mvd-item,' +
      ' #mvd-snav > .mvd-group > .mvd-gbtn, #mvd-snav > .mvd-all, #mvd-foot .mvd-tool'), shown);
  }
  function onRailKey(e) {
    if (!railOn() || e.altKey || e.ctrlKey || e.metaKey || !isEl(e.target)) return;
    var k = e.key, t = e.target, box = fly ? flyBox(fly) : null;
    if (box && box.contains(t)) {                     // у підменю: ↑ ↓ Home End, ← — назад до значка
      var l = focusables(box), i = l.indexOf(t);
      if (k === 'ArrowLeft' || k === 'Left') { e.preventDefault(); e.stopPropagation(); closeFly(true); }
      else if ((k === 'ArrowDown' || k === 'ArrowUp' || k === 'Down' || k === 'Up') && l.length) {
        e.preventDefault();
        focusIn(box, l[(i + (k === 'ArrowDown' || k === 'Down' ? 1 : -1) + l.length) % l.length]);
      } else if ((k === 'Home' || k === 'End') && l.length) {
        e.preventDefault();
        focusIn(box, l[k === 'Home' ? 0 : l.length - 1]);
      }
      return;
    }
    var b = t.closest('.mg-arrow-btn, .mvd-gbtn');
    if (b && (k === 'ArrowRight' || k === 'Right')) { e.preventDefault(); openFly(b.closest('.mg-group, .mvd-group'), true); return; }
    if (b && fly && (k === 'ArrowLeft' || k === 'Left')) { e.preventDefault(); closeFly(true); return; }
    if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'Down' || k === 'Up' || k === 'Home' || k === 'End') {
      var rc = railControls(), j = rc.indexOf(t);                       // уздовж ряду значків
      if (j < 0 || !rc.length) return;
      e.preventDefault();
      var down = k === 'ArrowDown' || k === 'Down';
      var n = k === 'Home' ? 0 : k === 'End' ? rc.length - 1 : (j + (down ? 1 : -1) + rc.length) % rc.length;
      if (fly) closeFly();
      rc[n].focus({ preventScroll: false });
    }
  }

  /* ── підказка біля значка (#mvd-tip): назва — уже перекладена (текст / aria-label) ── */
  function tipTarget(t) {
    if (!isEl(t) || !wide()) return null;
    var x = t.closest('#mvd-foot .mvd-tool');
    if (x) return x;
    x = t.closest('#nav-sidebar .mg-group > .mg-header, #mvd-snav > .mvd-group > .mvd-item,' +
                  ' #mvd-snav > .mvd-group > .mvd-gbtn, #mvd-snav > .mvd-all');
    if (!x) return null;
    if (railOn()) return x;
    var l = x.querySelector('.mvd-lbl, .mg-label');  // повне меню — лише для обрізаної назви
    return l && l.scrollWidth > l.clientWidth + 1 ? x : null;
  }
  function tipText(x) {
    var s = x.getAttribute('aria-label') || x._mvdTitle || x.getAttribute('title') || '';
    if (!s) { var hl = x.querySelector('.mg-header-link'); if (hl) s = labelOf(hl); }
    if (!s) { var l = x.querySelector('.mvd-lbl'); s = l ? l.textContent : ''; }
    return squash(s);
  }
  function showTip(x) {
    tipPend = null;
    if (!isEl(x) || !x.isConnected || !wide()) return;
    if (fly && x.closest('.mg-group, .mvd-group') === fly) return;   // його підменю вже відкрите
    var s = tipText(x);
    if (!s) return;
    if (!tip) {
      tip = el('div'); tip.id = 'mvd-tip';
      tip.setAttribute('aria-hidden', 'true');
      tip.setAttribute('translate', 'no');           // текст уже зі сторінки — перекладений
      document.body.appendChild(tip);
    }
    if (x.hasAttribute('title')) { x._mvdTitle = x.getAttribute('title'); x.removeAttribute('title'); }  // без другої
    tip.textContent = s;
    var z = zoom(), r = x.getBoundingClientRect(), up = !railOn() && !!x.closest('#mvd-foot');
    var left = up ? r.left + r.width / 2 : r.right + 10, topY = up ? r.top - 8 : r.top + r.height / 2;
    tip.classList.toggle('mvd-tip-up', up);
    tip.style.left = left / z + 'px';
    tip.style.top = topY / z + 'px';
    tip.classList.add('on');
    tipOwner = x;
    var tr = tip.getBoundingClientRect();
    if (tr.right > window.innerWidth - 8) tip.style.left = (left - (tr.right - window.innerWidth + 8)) / z + 'px';
    else if (tr.left < 8) tip.style.left = (left + 8 - tr.left) / z + 'px';
  }
  function hideTip() {
    clearTimeout(tipT);
    tipPend = null;
    if (tipOwner && tipOwner._mvdTitle) { tipOwner.setAttribute('title', tipOwner._mvdTitle); tipOwner._mvdTitle = ''; }
    tipOwner = null;
    if (tip) tip.classList.remove('on');
  }
  function onTipOver(e) {
    if (e.pointerType && e.pointerType !== 'mouse') return;
    var x = tipTarget(e.target);
    if (x && (x === tipOwner || x === tipPend)) return;
    hideTip();
    if (!x) return;
    tipPend = x;
    tipT = setTimeout(function () { showTip(x); }, 250);
  }
  function onTipOut(e) {
    var x = tipOwner || tipPend, to = e.relatedTarget;
    if (x && isEl(to) && x.contains(to)) return;
    hideTip();
  }
  function onTipFocus(e) {                           // з клавіатури — одразу
    var t = e.target, kb = false;
    try { kb = isEl(t) && t.matches(':focus-visible'); } catch (err) { kb = false; }
    if (!kb) return;
    hideTip();
    var x = tipTarget(t);
    if (x) showTip(x);
  }
  function wireRail() {
    if (!side) return;
    // фаза захоплення: у ряду значків ▼ групи відкриває підменю, а не згортає (обробник base_site не спрацьовує)
    side.addEventListener('click', function (e) {
      if (!railOn() || !isEl(e.target)) return;
      var b = e.target.closest('.mg-arrow-btn, .mvd-gbtn');
      if (!b || !side.contains(b)) return;
      e.stopPropagation();
      e.preventDefault();
      var g = b.closest('.mg-group, .mvd-group');
      if (fly === g) closeFly(); else openFly(g, e.detail === 0);       // detail 0 — Enter / Пробіл
    }, true);
    side.addEventListener('pointerover', onFlyOver);
    side.addEventListener('pointerleave', onFlyLeave);
    side.addEventListener('focusout', function (e) {
      var t = isEl(e.relatedTarget) ? e.relatedTarget : null;
      hideTip();
      if (fly && !(t && (fly.contains(t) || t.closest('.mvm, .mvd-ctx')))) closeFly();
    });
    side.addEventListener('scroll', function () { hideTip(); if (Date.now() > flyCalm) closeFly(); });
    side.addEventListener('keydown', onRailKey);
    side.addEventListener('pointerover', onTipOver);
    side.addEventListener('pointerout', onTipOut);
    side.addEventListener('focusin', onTipFocus);
  }
  /* висота меню — до низу вікна, тож низ меню завжди видно (px ÷ zoom: «Розмір тексту» 112–150 %) */
  function sideMax() {
    if (!side || !NEW) return;
    var m = document.getElementById('main'), z = zoom(), t = m ? Math.max(0, m.getBoundingClientRect().top) : 0;
    var v = Math.round((window.innerHeight - t) / z);
    if (v !== lastMax) { lastMax = v; side.style.setProperty('--mvd-sb-max', v + 'px'); }
  }
  /* MVDesktop.reveal — mv_show.js перед підсвічуванням пункту меню: у «Простому» тимчасово «Повне», у ряду
     значків — підменю групи */
  function reveal(x) {
    if (!NEW || !isEl(x) || !side || !side.contains(x)) return;
    flyCalm = Date.now() + 1500;                     // mv_show прокручує до пункту — підменю не закривати
    var g = x.closest('.mg-group'), sg = x.closest('#mvd-snav .mvd-group'), onHead = !!x.closest('.mg-header, .mvd-gbtn');
    if (g) {
      if (root.classList.contains('mvd-simple')) root.classList.remove('mvd-simple');   // не зберігається
      quietly(function () { if (onHead) g.classList.remove('collapsed'); else expandAround(x); });
      if (railOn() && !onHead) openFly(g, false);
    } else if (sg) {
      if (!root.classList.contains('mvd-simple')) root.classList.add('mvd-simple');
      if (!onHead) sg.classList.remove('mvd-shut');
      if (railOn() && !onHead && !sg.classList.contains('mvd-mine')) openFly(sg, false);
    }
    flyCalm = Date.now() + 1500;
  }

  /* ── меню правого кліку (MVMenu) ── */
  function move(key, d) {
    var list = st.pinned.slice(), i = list.indexOf(key), j = i + d;
    if (i < 0 || j < 0 || j >= list.length) return;
    list.splice(j, 0, list.splice(i, 1)[0]);
    st.pinned = list; renderMenu(); save({ pinned: list });
  }
  function pin(key) {
    if (st.pinned.length >= cfg.max) { toast('У «Мої» вже ' + cfg.max + ' пунктів — спершу приберіть один.'); return; }
    st.pinned = st.pinned.concat([key]); renderMenu(); save({ pinned: st.pinned });
  }
  function unpin(key) {
    st.pinned = st.pinned.filter(function (k) { return k !== key; }); renderMenu(); save({ pinned: st.pinned });
  }
  function pinLink(path, label) {
    if (st.pinned.length >= cfg.max) { toast('У «Мої» вже ' + cfg.max + ' пунктів — спершу приберіть один.'); return; }
    save({ pin_link: { href: path, label: label } }).then(function (d) { if (d && d.ok) toast('«' + label + '» — у «Мої».'); });
  }
  function resetIcon(key) { var c = {}; c[key] = null; save({ icons: c }); }
  function modeItems(items) {
    if (st.mode === 'simple') items.push({ label: 'Повне меню — усі розділи', icon: '☰', onSelect: function () { setMode('full'); } });
    else items.push({ label: 'Просте меню', icon: '✨', onSelect: function () { setMode('simple'); } });
    items.push({ label: 'Меню як було (стандартні «Мої» й значки)', icon: '↺', onSelect: function () { save({ reset: true }); } });
    return items;
  }
  function linkCtx(at, a) {
    var href = a.getAttribute('href'), path = pathOf(href);
    var key = a.dataset.key || keyForHref(href);
    if (!key && path && a.closest('.mg-group')) key = 'url:' + path;    // будь-яка сторінка повного меню
    var it = key ? byKey(key) : null, shownLabel = linkLabel(a).slice(0, 60);
    var label = ((it && it.label) || shownLabel).slice(0, 60);
    var items = [];
    var i = key ? st.pinned.indexOf(key) : -1;
    if (i >= 0) {
      items.push({ label: 'Вище в «Мої»', icon: '⬆', disabled: i <= 0, onSelect: function () { move(key, -1); } });
      items.push({ label: 'Нижче в «Мої»', icon: '⬇', disabled: i >= st.pinned.length - 1, onSelect: function () { move(key, 1); } });
      items.push({ label: 'Прибрати з «Мої»', icon: '☆', onSelect: function () { unpin(key); } });
    } else if (key && catalog(key)) {
      items.push({ label: 'У «Мої»', icon: '⭐', onSelect: function () { pin(key); } });
    } else if (path) {
      items.push({ label: 'У «Мої»', icon: '⭐', onSelect: function () { pinLink(path, shownLabel); } });
    }
    if (key && !a.classList.contains('addlink') && !a.classList.contains('mvd-add')) {   // у ＋ свого значка немає
      var cur = (it && it.icon) || a.getAttribute('data-mvd-icon') || '';
      items.push({ label: 'Свій значок…', icon: '🎨', onSelect: function () { iconPicker(key, shownLabel || label, cur, at); } });
      if (st.icons[key]) items.push({ label: 'Значок як був', icon: '↺', onSelect: function () { resetIcon(key); } });
    }
    if (href) items.push({ label: 'Відкрити в новій вкладці', icon: '↗', href: href, target: '_blank' });
    items.push({ type: 'separator' });
    modeItems(items);
    window.MVMenu.open(at, items, { title: '«' + shownLabel + '»', titleData: true });
  }
  function groupCtx(at, g) {
    var id = g.dataset.groupId, head = g.querySelector(':scope > .mg-header');
    var link = head && head.querySelector('.mg-header-link'), href = link ? link.getAttribute('href') : '';
    var label = labelOf(link) || id, rail = railOn(), list = groupsList(), i = list.indexOf(g);
    var mods = g.classList.contains('mvd-mods') || !!g.querySelector(':scope > .mg-content > details.mg-more');
    var items = [];
    if (href) {
      items.push({ label: 'Огляд розділу', icon: '↗', href: href });
      items.push({ label: 'У новій вкладці', icon: '↗', href: href, target: '_blank' });
      items.push({ type: 'separator' });
    }
    if (!rail) {
      items.push({ label: g.classList.contains('collapsed') ? 'Розгорнути' : 'Згорнути', icon: '⇕',
                   onSelect: function () { setCollapsed([g], !g.classList.contains('collapsed')); } });
      items.push({ label: 'Згорнути всі', onSelect: function () { setCollapsed(groupsList(), true); } });
      items.push({ label: 'Розгорнути всі', onSelect: function () { setCollapsed(groupsList(), false); } });
    }
    items.push({ type: 'submenu', label: 'Розкривати',
                 hint: function () { return st.expand === 'current' ? 'Лише групу поточної сторінки' : 'Як я лишив'; },
                 items: function () {
                   return [
                     { type: 'radio', group: 'expand', label: 'Як я лишив', checked: st.expand !== 'current',
                       onSelect: function () { setExpand('remember'); } },
                     { type: 'radio', group: 'expand', label: 'Лише групу поточної сторінки', checked: st.expand === 'current',
                       onSelect: function () { setExpand('current'); } }
                   ];
                 } });
    items.push({ type: 'separator' });
    items.push({ label: 'Групу вище', icon: '⬆', disabled: i <= 0, onSelect: function () { moveGroup(g, -1); } });
    items.push({ label: 'Групу нижче', icon: '⬇', disabled: i < 0 || i >= list.length - 1,
                 onSelect: function () { moveGroup(g, 1); } });
    if (lsGet('mg-order')) items.push({ label: 'Порядок груп як був', icon: '↺', onSelect: resetOrder });
    if (mods) {
      items.push({ type: 'separator' });
      items.push({ type: 'check', label: 'Показувати всі сторінки розділу', checked: modsOpen(g),
                   onToggle: function (on) { setMods(g, on, true); } });
    }
    items.push({ type: 'separator' });
    items.push({ label: 'Свій значок групи…', icon: '🎨',
                 onSelect: function () { iconPicker('grp:' + id, label, head ? head.getAttribute('data-mvd-icon') : '', at); } });
    if (st.icons['grp:' + id]) items.push({ label: 'Значок як був', icon: '↺', onSelect: function () { resetIcon('grp:' + id); } });
    items.push({ type: 'separator' });
    items.push(railItem());
    items.push({ label: 'Просте меню', icon: '✨', onSelect: function () { setMode('simple'); } });
    window.MVMenu.open(at, items, { title: 'Група', titleData: '«' + label + '»' });
  }
  function simpleGroupCtx(at, g) {
    var id = g.dataset.sg, b = g.querySelector(':scope > .mvd-gbtn'), label = labelOf(b), items = [];
    if (!railOn() && b) {
      items.push({ label: g.classList.contains('mvd-shut') ? 'Розгорнути' : 'Згорнути', icon: '⇕', onSelect: function () { b.click(); } });
      items.push({ type: 'separator' });
    }
    items.push({ label: 'Свій значок групи…', icon: '🎨', onSelect: function () {
      var i = b && b.querySelector('.mvd-ico');
      iconPicker('sg:' + id, label, i ? i.textContent : '', at);
    } });
    if (st.icons['sg:' + id]) items.push({ label: 'Значок як був', icon: '↺', onSelect: function () { resetIcon('sg:' + id); } });
    items.push({ type: 'separator' });
    items.push(railItem());
    modeItems(items);
    window.MVMenu.open(at, items, { title: 'Група', titleData: '«' + label + '»' });
  }
  function railCtx(at) { window.MVMenu.open(at, sidebarItems(), { title: 'Бокове меню', focus: 'checked' }); }
  function menuCtx(at) { window.MVMenu.open(at, modeItems([railItem(), { type: 'separator' }]), { title: 'Меню' }); }
  function onSideMenu(e) {
    if (e.shiftKey || !wide() || !hasMenu() || !isEl(e.target)) return;   // Shift + правий клік, телефон — меню браузера
    var t = e.target;
    if (t.closest('input, textarea, select')) return;
    // з клавіатури (клавіша меню) координат немає — меню біля елемента
    var at = (e.clientX || e.clientY) ? { x: e.clientX, y: e.clientY } : (t.closest('a, button, .mg-header') || t);
    var tb = t.closest('#mvd-foot .mvd-tool');
    if (tb && tb.id === 'mvd-railbtn') { e.preventDefault(); railCtx(at); return; }
    if (tb && tb.classList.contains('mvd-theme')) { e.preventDefault(); themeMenu(tb); return; }
    if (tb && tb.classList.contains('mvd-look')) { e.preventDefault(); lookMenu(tb); return; }
    var h = t.closest('.mg-header'), g = t.closest('.mg-group');
    if (h && g && side.contains(g)) { e.preventDefault(); groupCtx(at, g); return; }
    var sg = t.closest('#mvd-snav .mvd-gbtn');
    if (sg) { e.preventDefault(); simpleGroupCtx(at, sg.closest('.mvd-group')); return; }
    var a = t.closest('a[href]');
    if (a && side.contains(a)) { e.preventDefault(); linkCtx(at, a); return; }
    if (g && side.contains(g)) { e.preventDefault(); groupCtx(at, g); return; }
    if (t.closest('#mvd-snav, #mvd-top, #mvd-foot')) { e.preventDefault(); menuCtx(at); }
  }

  /* ── вибір значка (вікно) ── */
  function place(box, at) {
    document.body.appendChild(box);
    var z = zoom(), r = box.getBoundingClientRect(), x = 80, y = 80;
    if (isEl(at)) { var a = at.getBoundingClientRect(); x = a.left; y = a.bottom + 4; }
    else if (at && at.x != null) { x = at.x; y = at.y; }
    x = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
    y = Math.max(8, Math.min(y, window.innerHeight - r.height - 8));
    box.style.left = x / z + 'px';
    box.style.top = y / z + 'px';
    var first = box.querySelector('button, input');
    if (first) first.focus();
  }
  function hideCtx() {
    if (!ctx) return;
    var back = ctxBack, had = ctx.contains(document.activeElement);
    ctx.remove();
    ctx = null; ctxBack = null;
    if (had && back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }
  function iconPicker(key, label, current, at) {
    hideCtx();
    if (iconsFull(key)) {
      toast('Своїх значків уже ' + (cfg.maxIcons || 60) + ' — спершу поверніть якийсь: «↺ Значок як був».');
      return;
    }
    var act = document.activeElement;
    ctxBack = isEl(act) && act !== document.body ? act : null;
    ctx = el('div', 'mvd-ctx mvd-icons');
    ctx.setAttribute('role', 'dialog');
    ctx.setAttribute('aria-label', 'Значок');
    var head = el('div', 't', 'Значок для ');
    var nm = el('span', '', '«' + label + '»'); nm.setAttribute('translate', 'no');
    head.appendChild(nm);
    ctx.appendChild(head);
    var grid = el('div', 'mvd-igrid');
    cfg.iconChoices.forEach(function (ic) {
      var on = current === ic, b = el('button', on ? 'on' : '', ic);
      b.type = 'button'; b.title = ic;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.onclick = function () { var c = {}; c[key] = ic; hideCtx(); save({ icons: c }); };
      grid.appendChild(b);
    });
    ctx.appendChild(grid);
    var row = el('div', 'mvd-irow');
    var inp = el('input'); inp.type = 'text'; inp.maxLength = 8; inp.placeholder = 'або свій емодзі';
    inp.setAttribute('aria-label', 'Свій значок (емодзі)');
    var ok = el('button', '', '✓'); ok.type = 'button'; ok.setAttribute('aria-label', 'Зберегти значок');
    ok.onclick = function () {
      var v = inp.value.trim();
      if (!v) return;
      var c = {}; c[key] = v; hideCtx();
      save({ icons: c }).then(function (d) {
        if (d && d.prefs && !d.prefs.icons[key]) toast('Значок — лише емодзі чи символ, без літер і цифр.');
      });
    };
    inp.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); ok.click(); } };
    row.appendChild(inp); row.appendChild(ok);
    ctx.appendChild(row);
    place(ctx, at);
  }

  /* ── шапка: пошук усюди й акаунт ── */
  function buildHeader() {
    var header = document.getElementById('header'), brand = document.getElementById('branding');
    if (!header || !brand || document.getElementById('mvd-search')) return;
    var s = el('button'); s.id = 'mvd-search'; s.type = 'button';
    s.setAttribute('aria-label', 'Шукати всюди (Ctrl K)');
    s.appendChild(el('span', 'mvd-s-ico', '🔍'));
    s.appendChild(el('span', 'mvd-s-text', 'Шукати всюди: розділ, замовлення, товар, клієнт, лист'));
    s.appendChild(el('kbd', '', 'Ctrl K'));
    s.onclick = openPalette;
    brand.insertAdjacentElement('afterend', s);
    var tools = document.getElementById('user-tools');
    if (!tools) return;
    var nameEl = tools.querySelector('strong');
    var name = nameEl ? nameEl.textContent.trim() : '';
    var initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w.charAt(0); }).join('').toUpperCase() || '👤';
    var u = el('button'); u.id = 'mvd-user'; u.type = 'button'; u.textContent = initials;
    u.setAttribute('translate', 'no');
    u.title = name; u.setAttribute('aria-label', 'Акаунт: ' + name); u.setAttribute('aria-haspopup', 'menu');
    u.onclick = function () {
      if (!hasMenu()) { location.href = '/core/my-settings/'; return; }
      var items = [{ label: 'Мої налаштування', icon: '⚙', href: '/core/my-settings/' }];
      [].forEach.call(tools.querySelectorAll('a[href*="password_change"]'), function (a) {
        items.push({ label: squash(a.textContent), icon: '🔑', href: a.getAttribute('href') });
      });
      items.push({ label: 'Вигляд…', icon: '🎨', onSelect: function () { lookMenu(u); } });
      var out = document.getElementById('logout-form');
      if (out) {
        var b = out.querySelector('button, input[type="submit"]');
        items.push({ type: 'separator' });
        items.push({ label: squash((b && (b.textContent || b.value)) || 'Вийти'), icon: '🚪', onSelect: function () { out.submit(); } });
      }
      window.MVMenu.open(u, items, { title: name || 'Акаунт', titleData: !!name, side: 'bottom-end' });
    };
    tools.appendChild(u);
  }

  /* ── Ctrl K: розділи й пошук ── */
  function sources() {
    var seen = {}, out = [];
    function add(icon, label, href, where) {
      label = squash(label);
      if (!label || !href || seen[href]) return;
      seen[href] = 1;
      out.push({ icon: icon, label: label, href: href, where: where || '' });
    }
    st.pinned.map(byKey).filter(Boolean).forEach(function (i) { add(i.icon, i.label, i.href, '⭐ Мої'); });
    cfg.items.forEach(function (i) { add(st.icons[i.key] || i.icon, i.label, i.href, 'Розділ'); });
    [].forEach.call(document.querySelectorAll('#nav-sidebar .mg-group a[href]'), function (a) {
      var g = a.closest('.mg-group'), head = g && g.querySelector('.mg-header-link');
      var where = head ? labelOf(head) : '';
      if (a.classList.contains('addlink') || a.classList.contains('mvd-add')) {
        var tr = a.closest('tr'), row = a.closest('.mvd-row');
        var main = tr ? tr.querySelector('th a') : row ? row.querySelector('.mg-link') : null;
        add('＋', 'Додати: ' + labelOf(main), a.getAttribute('href'), where);
        return;
      }
      var hd = a.closest('.mg-header');               // назва групи — значок групи
      add(a.getAttribute('data-mvd-icon') || (hd && hd.getAttribute('data-mvd-icon')) || '•', labelOf(a),
          a.getAttribute('href'), where);
    });
    return out;
  }
  function openPalette() {
    if (!pal) {
      pal = el('div'); pal.id = 'mvd-pal'; pal.setAttribute('role', 'dialog'); pal.setAttribute('aria-label', 'Пошук усюди');
      var shade = el('div', 'mvd-pal-shade'); shade.onclick = closePalette;
      var box = el('div', 'mvd-pal-box');
      var inp = el('input'); inp.type = 'search'; inp.id = 'mvd-pal-q';
      inp.placeholder = 'Розділ або що шукати: № замовлення, артикул, клієнт…';
      inp.setAttribute('aria-label', 'Що шукати'); inp.autocomplete = 'off';
      var list = el('div', 'mvd-pal-list'); list.id = 'mvd-pal-list'; list.setAttribute('role', 'listbox');
      box.appendChild(inp); box.appendChild(list);
      box.appendChild(el('div', 'mvd-pal-foot', '↑ ↓ — вибрати · Enter — відкрити · Esc — закрити'));
      pal.appendChild(shade); pal.appendChild(box);
      document.body.appendChild(pal);
      inp.addEventListener('input', function () { results(inp.value); });
      inp.addEventListener('keydown', function (e) {
        var rows = [].slice.call(list.querySelectorAll('a')), cur = list.querySelector('a.on'), i = rows.indexOf(cur);
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          var n = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))];
          if (n) { if (cur) cur.classList.remove('on'); n.classList.add('on'); n.scrollIntoView({ block: 'nearest' }); }
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (cur) location.href = cur.href;
        }
      });
    }
    if (!root.classList.contains('mvd-pal-open')) palBack = document.activeElement;
    root.classList.add('mvd-pal-open');
    var q = document.getElementById('mvd-pal-q');
    q.value = ''; results(''); q.focus();
  }
  function closePalette() {
    if (!root.classList.contains('mvd-pal-open')) return;
    root.classList.remove('mvd-pal-open');
    var b = palBack;
    palBack = null;
    if (isEl(b) && b !== document.body && b.isConnected && typeof b.focus === 'function') b.focus({ preventScroll: true });
  }
  function results(q) {
    var list = document.getElementById('mvd-pal-list');
    list.textContent = '';
    var words = q.toLowerCase().split(/\s+/).filter(Boolean);
    var found = sources().filter(function (s) {
      var hay = (s.label + ' ' + s.where).toLowerCase();
      return words.every(function (w) { return hay.indexOf(w) >= 0; });
    });
    if (words.length) {
      var ql = words.join(' ');
      found.sort(function (a, b) {
        return (a.label.toLowerCase().indexOf(ql) === 0 ? 0 : 1) - (b.label.toLowerCase().indexOf(ql) === 0 ? 0 : 1);
      });
    }
    found = found.slice(0, words.length ? 8 : 10);
    function row(icon, label, where, href) {
      var a = el('a', 'mvd-pal-row'); a.href = href; a.setAttribute('role', 'option');
      a.appendChild(el('span', 'mvd-ico', icon));
      a.appendChild(el('span', 'mvd-lbl', label));
      if (where) a.appendChild(el('span', 'mvd-where', where));
      list.appendChild(a);
    }
    function searchRows() {
      if (!words.length || !cfg.search.length) return;
      list.appendChild(el('div', 'mvd-pal-h', 'Шукати «' + q.trim() + '»'));
      cfg.search.forEach(function (s) { row('🔎', 'у: ' + s.label, '', s.href + encodeURIComponent(q.trim())); });
    }
    function pageRows() {
      if (!found.length) return;
      list.appendChild(el('div', 'mvd-pal-h', words.length ? 'Розділи й сторінки' : '⭐ Мої й розділи'));
      found.forEach(function (s) { row(s.icon, s.label, s.where, s.href); });
    }
    // номер, артикул (є цифра) або нічого не знайшлося серед сторінок — спершу пошук; слово — спершу сторінки
    if (/\d/.test(q) || !found.length) { searchRows(); pageRows(); } else { pageRows(); searchRows(); }
    if (!list.querySelector('a')) list.appendChild(el('div', 'mvd-hint', 'Нічого не знайшлося.'));
    var first = list.querySelector('a'); if (first) first.classList.add('on');
  }

  /* ── назва сторінки й кнопки над нею — одним рядком, що переноситься (Django: float + margin-top −48 px) ── */
  function pageHead() {
    var content = document.getElementById('content');
    var h1 = content && content.querySelector(':scope > h1');
    var tools = document.querySelector('#content-main > ul.object-tools');
    if (!h1 || !tools) return;
    var wrap = el('div', 'mvd-pagehead');
    h1.parentNode.insertBefore(wrap, h1);
    wrap.appendChild(h1);
    var home = tools.parentNode, next = tools.nextSibling;
    headPlace = function () {                        // на телефоні — на своєму місці, як було
      if (wide()) { if (tools.parentNode !== wrap) wrap.appendChild(tools); }
      else if (tools.parentNode === wrap) home.insertBefore(tools, next && next.parentNode === home ? next : home.firstChild);
    };
    headPlace();
  }

  /* ── фільтри списку: кнопкою, у висувній панелі праворуч (або колонкою — «📌 Збоку») ── */
  function filters() {
    var box = document.getElementById('changelist-filter');
    if (!box || fbtn) return;
    var n = 0;
    new URLSearchParams(location.search).forEach(function (_v, k) { if (NOT_FILTERS.indexOf(k) < 0) n += 1; });
    fbtn = el('button', 'mvd-fbtn', n ? '⚙ Фільтри · ' + n : '⚙ Фільтри');
    fbtn.type = 'button'; fbtn.setAttribute('aria-expanded', 'false');
    fbtn.onclick = function () { drawer(!root.classList.contains('mvd-fopen')); };
    var form = document.getElementById('changelist-search');
    // список без пошуку — рядок над таблицею (той самий, що вже зробила панель праворуч для «📌 Панель»)
    var host = form ? (form.querySelector(':scope > div') || form) : document.querySelector('#content-main .mvp-tbar');
    if (!host) {
      var fc = document.querySelector('#changelist .changelist-form-container');
      if (fc) { host = el('div', 'mvd-tbar'); fc.insertBefore(host, fc.firstChild); }
    }
    if (host) host.appendChild(fbtn);
    var head = el('div', 'mvd-fhead');
    head.appendChild(el('b', '', 'Фільтри'));
    var side_ = el('button', 'mvd-fside'); side_.type = 'button';
    side_.onclick = function () {
      st.filters = st.filters === 'drawer' ? 'side' : 'drawer';
      drawer(false); applyFilters(); save({ filters: st.filters });
    };
    head.appendChild(side_);
    var x = el('button', 'mvd-fx', '✕'); x.type = 'button'; x.setAttribute('aria-label', 'Закрити фільтри');
    x.onclick = function () { drawer(false); };
    head.appendChild(x);
    box.insertBefore(head, box.firstChild);
    var shade = el('div', 'mvd-fshade'); shade.onclick = function () { drawer(false); };
    document.body.appendChild(shade);
    applyFilters();
  }
  function applyFilters() {
    var b = document.querySelector('.mvd-fside');
    root.classList.toggle('mvd-fdrawer', st.filters === 'drawer');
    if (b) {
      b.textContent = st.filters === 'drawer' ? '📌 Збоку' : '⇥ У кнопку';
      b.title = st.filters === 'drawer' ? 'Показувати фільтри колонкою праворуч від списку, як раніше'
                                        : 'Сховати фільтри в кнопку «⚙ Фільтри» над списком';
    }
  }
  function drawer(on) {
    root.classList.toggle('mvd-fopen', !!on);
    if (fbtn) fbtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }

  /* ── класичний вигляд: лише дорога назад ── */
  function classicPill() {
    var s = document.getElementById('nav-sidebar');
    if (!s || document.getElementById('mvd-try')) return;
    var b = el('button', '', '✨ Новий вигляд Minerva'); b.id = 'mvd-try'; b.type = 'button';
    b.title = 'Меню «Просте / Повне», пошук усюди (Ctrl K), списки й фільтри по-новому. Назад — 🎨 унизу меню.';
    b.onclick = function () { design('new'); };
    s.insertBefore(b, s.firstChild);
  }

  /* ── клавіші, клік поза вікнами, прокрутка, розмір вікна ── */
  function onEscape(e) {                             // Esc закриває лише верхній шар — далі подія не йде
    if (e.key !== 'Escape' && e.key !== 'Esc') return;
    hideTip();
    if (menuOpen()) return;                          // меню правого кліку закривається саме
    if (ctx) hideCtx();
    else if (root.classList.contains('mvd-pal-open')) closePalette();
    else if (fly) closeFly(!!(document.activeElement && fly.contains(document.activeElement)));
    else if (root.classList.contains('mvd-fopen')) drawer(false);
    else return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }
  function wireGlobal() {
    document.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      if (e.code !== 'KeyK' && e.key !== 'k' && e.key !== 'K') return;         // e.code — і на українській розкладці
      if (!wide()) return;
      e.preventDefault();
      if (root.classList.contains('mvd-pal-open')) closePalette(); else openPalette();
    });
    window.addEventListener('keydown', onEscape, true);
    document.addEventListener('pointerdown', function (e) {
      var t = isEl(e.target) ? e.target : null;
      hideTip();
      if (ctx && !(t && t.closest('.mvd-ctx'))) hideCtx();
      if (fly && !(t && (fly.contains(t) || t.closest('.mvm, .mvd-ctx')))) closeFly();
    }, true);
    window.addEventListener('scroll', function () {
      hideTip();
      if (!smRaf) smRaf = requestAnimationFrame(function () { smRaf = 0; sideMax(); if (fly) placeFly(); });
    }, { passive: true });
    window.addEventListener('resize', function () { closeFly(); hideTip(); applyRail(); sideMax(); });
    window.addEventListener('hashchange', markActive);   // «🧩 Заповнити пропуски» (#fill) — та сама сторінка
    if (mqWide) {
      var onWide = function () {
        headLinks();
        openAround();
        applyRail();
        if (headPlace) headPlace();
        if (!wide()) { closeFly(); hideTip(); }
      };
      if (mqWide.addEventListener) mqWide.addEventListener('change', onWide);
      else if (mqWide.addListener) mqWide.addListener(onWide);
    }
    if (window.MutationObserver) {                   // «Мої налаштування» показують вибір одразу; тема з меню
      new MutationObserver(function (recs) {
        var rail = false, theme = false;
        recs.forEach(function (r) { if (r.attributeName === 'data-theme') theme = true; else rail = true; });
        if (rail) applyRail();
        if (theme) paintTheme();
      }).observe(root, { attributes: true, attributeFilter: ['data-theme', 'data-mv-sidebar', 'data-mv-size'] });
    }
  }
  function fixShifted() {                            // без кнопки Django (її прибирає base_site) меню не сховати
    var m = document.getElementById('main');
    if (m && document.getElementById('nav-sidebar') && !m.classList.contains('shifted')) {
      m.classList.add('shifted');
      lsSet('django.admin.navSidebarIsOpen', 'true');
    }
  }

  function start() {
    if (!NEW) { classicPill(); return; }
    fixShifted();
    buildMenu();
    enhanceFull();
    watchBadges();
    markActive();
    openAround();                                    // розгорнути довкола поточної + restoreOpen (лише на ПК)
    buildFoot();
    applyRail();
    wireRail();
    sideMax();
    pageHead();
    buildHeader();
    filters();
    wireGlobal();
  }
  // панель праворуч (mv_panel.js) може відкритись ще до start() — подію слухаємо одразу
  document.addEventListener('mv:panel', function (e) {
    var d = (e && e.detail) || {};
    panelOpen = !!d.open;
    panelW = d.open ? Math.max(0, +d.width || 0) : 0;
    inPanelEvent = true;
    try { applyRail(); } finally { inPanelEvent = false; }
  });
  // mv_panel.js іде раніше й відкриває панель з адреси (#mvp=…) одразу — тоді його подія була ще до цього скрипту
  try {
    if (window.MVPanel && typeof window.MVPanel.isOpen === 'function' && window.MVPanel.isOpen()) {
      panelOpen = true;
      panelW = parseFloat(document.body ? document.body.style.getPropertyValue('--mvp-w') : '') || 0;
    }
  } catch (err) { /* без панелі */ }
  window.MVDesktop = { palette: openPalette, menu: function () { return st; },
                       reveal: reveal, railOn: railOn, sideWidth: sideWidth };
  // після бокового меню з base_site (воно будується на DOMContentLoaded і спершу все з нього прибирає)
  if (document.readyState === 'complete') start(); else document.addEventListener('DOMContentLoaded', start);
})();
