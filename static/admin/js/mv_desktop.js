/* 🧭 Новий вигляд на ПК (html[data-mv-design="new"], ширше 768 px; core/main_menu.py, CSS — mv_desktop.css).
 *
 * Бокове меню: «Просте» (⭐ Мої + кілька груп) / «Повне» (усі групи, як було). Правий клік на пункті — ⭐ у Мої /
 * прибрати, вище / нижче, свій значок, нова вкладка; на сторінці з повного меню — «⭐ У Мої». Шапка: пошук усюди
 * (Ctrl K — розділи меню й «шукати … у Замовленнях / Товарах / Клієнтах / Пошті»), кружок з ініціалами — акаунт.
 * Списки: фільтри кнопкою «⚙ Фільтри · N» у висувній панелі праворуч (або колонкою — «📌 Збоку»).
 * Вибір — у профілі (POST /core/menu/). Класичний вигляд — лише пігулка «✨ Новий вигляд» угорі меню.
 */
(function () {
  'use strict';
  if (window.MVDesktop) return;
  var root = document.documentElement;
  var cfgEl = document.getElementById('mv-menu-config');
  var cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null;
  if (!cfg) return;
  var NEW = root.getAttribute('data-mv-design') === 'new';
  var st = cfg.prefs;
  var side = null, snav = null, top = null, ctx = null, pal = null, fbtn = null, toastT = 0, mailCount = 0;
  var NOT_FILTERS = ['q', 'p', 'o', 'e', 'all', '_popup', '_to_field', '_changelist_filters'];

  function csrf() { return (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ''; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function wide() { return window.matchMedia('(min-width: 769px)').matches; }
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }
  function toast(text) {
    var t = document.getElementById('mvd-toast');
    if (!t) { t = el('div'); t.id = 'mvd-toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
    t.textContent = text; t.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(function () { t.classList.remove('on'); }, 3200);
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
    var path = href.replace(location.origin, '');
    var hit = cfg.items.filter(function (i) { return i.href === path; })[0];
    if (hit) return hit.key;
    return st.links['url:' + path] ? 'url:' + path : '';
  }
  function activeKey() {
    var here = location.pathname + location.search, best = '', len = 0;
    st.pinned.map(byKey).concat(cfg.items).forEach(function (i) {
      if (!i) return;
      var hit = i.href === '/admin/' ? location.pathname === '/admin/' :
        (i.href.indexOf('?') >= 0 ? here.indexOf(i.href) === 0 : location.pathname.indexOf(i.href) === 0);
      if (hit && i.href.length > len) { best = i.key; len = i.href.length; }
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

  /* ── бокове меню ── */
  function renderMenu() {
    if (!side) return;
    root.classList.toggle('mvd-simple', st.mode === 'simple');
    [].forEach.call(top.querySelectorAll('.mvd-seg button'), function (b) {
      b.setAttribute('aria-pressed', b.dataset.mode === st.mode ? 'true' : 'false');
    });
    snav.textContent = '';
    var active = activeKey();
    function item(it) {
      var a = el('a', 'mvd-item' + (it.key === active ? ' on' : ''));
      a.href = it.href; a.dataset.key = it.key;
      if (it.key === active) a.setAttribute('aria-current', 'page');
      a.appendChild(el('span', 'mvd-ico', it.icon));
      a.appendChild(el('span', 'mvd-lbl', it.label));
      if (it.key === 'mail' && mailCount) a.appendChild(el('span', 'mvd-cnt', String(mailCount)));
      return a;
    }
    function group(title, items, hint) {
      var g = el('div', 'mvd-group');
      g.appendChild(el('div', 'mvd-gtitle', title));
      items.forEach(function (it) { if (it) g.appendChild(item(it)); });
      if (!items.length && hint) g.appendChild(el('div', 'mvd-hint', hint));
      snav.appendChild(g);
    }
    group('⭐ Мої', st.pinned.map(byKey).filter(Boolean),
          'Правий клік на пункті меню → «⭐ У Мої».');
    cfg.groups.forEach(function (g) {
      var items = g.keys.filter(function (k) { return st.pinned.indexOf(k) < 0; }).map(byKey).filter(Boolean);
      if (items.length) group(g.title, items);
    });
    var all = el('button', 'mvd-item mvd-all');
    all.type = 'button';
    all.appendChild(el('span', 'mvd-ico', '☰'));
    all.appendChild(el('span', 'mvd-lbl', 'Усі розділи…'));
    all.onclick = function () { setMode('full'); };
    snav.appendChild(all);
  }
  function setMode(m) { st.mode = m; renderMenu(); save({ mode: m }); }
  function buildMenu() {
    side = document.getElementById('nav-sidebar');
    if (!side) return;
    top = el('div'); top.id = 'mvd-top';
    var seg = el('div', 'mvd-seg'); seg.setAttribute('role', 'group'); seg.setAttribute('aria-label', 'Меню');
    [['simple', 'Просте'], ['full', 'Повне']].forEach(function (m) {
      var b = el('button', '', m[1]); b.type = 'button'; b.dataset.mode = m[0];
      b.onclick = function () { setMode(m[0]); };
      seg.appendChild(b);
    });
    top.appendChild(seg);
    var look = el('button', 'mvd-look', '🎨'); look.type = 'button';
    look.title = 'Вигляд Minerva'; look.setAttribute('aria-label', 'Вигляд Minerva');
    look.onclick = function (e) { var r = look.getBoundingClientRect(); lookMenu(r.left, r.bottom + 4); e.stopPropagation(); };
    top.appendChild(look);
    snav = el('nav'); snav.id = 'mvd-snav'; snav.setAttribute('aria-label', 'Меню');
    side.insertBefore(snav, side.firstChild);
    side.insertBefore(top, side.firstChild);
    side.addEventListener('contextmenu', function (e) {
      if (e.shiftKey || !wide()) return;                     // Shift + правий клік — меню браузера
      var a = e.target.closest('a[href]');
      var inMenu = e.target.closest('#mvd-snav, #mvd-top');
      if (!a && !inMenu) return;
      e.preventDefault();
      openCtx(e.clientX, e.clientY, a);
    });
    renderMenu();
    fetch('/email/unread-count/', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { mailCount = (d && d.count) || 0; if (mailCount) renderMenu(); })
      .catch(function () {});
  }

  /* ── меню правого кліку ── */
  function hideCtx() { if (ctx) { ctx.remove(); ctx = null; } }
  function addItem(box, text, fn) {
    var b = el('button', '', text); b.type = 'button'; b.setAttribute('role', 'menuitem');
    b.onclick = function () { hideCtx(); fn(); };
    box.appendChild(b);
  }
  function sep(box, title) {
    if (title) box.appendChild(el('div', 't', title)); else box.appendChild(el('hr'));
  }
  function place(box, x, y) {
    document.body.appendChild(box);
    box.style.left = Math.max(8, Math.min(x, innerWidth - box.offsetWidth - 8)) + 'px';
    box.style.top = Math.max(8, Math.min(y, innerHeight - box.offsetHeight - 8)) + 'px';
    var first = box.querySelector('button, input'); if (first) first.focus();
  }
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
  function openCtx(x, y, a) {
    hideCtx();
    ctx = el('div', 'mvd-ctx'); ctx.setAttribute('role', 'menu');
    var key = a ? (a.dataset.key || keyForHref(a.href)) : '';
    var it = key ? byKey(key) : null;
    var label = it ? it.label : (a ? (a.childNodes[0] && a.childNodes[0].nodeType === 3 ? a.childNodes[0].textContent : a.textContent) : '');
    label = (label || '').replace(/\s+/g, ' ').trim().slice(0, 60);
    if (a) {
      sep(ctx, '«' + label + '»');
      var pinned = key && st.pinned.indexOf(key) >= 0;
      if (pinned) {
        var i = st.pinned.indexOf(key);
        if (i > 0) addItem(ctx, '⬆ Вище в «Мої»', function () { move(key, -1); });
        if (i < st.pinned.length - 1) addItem(ctx, '⬇ Нижче в «Мої»', function () { move(key, 1); });
        addItem(ctx, '☆ Прибрати з «Мої»', function () { unpin(key); });
      } else if (key && catalog(key)) {
        addItem(ctx, '⭐ У «Мої»', function () { pin(key); });
      } else {
        addItem(ctx, '⭐ У «Мої»', function () {
          if (st.pinned.length >= cfg.max) { toast('У «Мої» вже ' + cfg.max + ' пунктів — спершу приберіть один.'); return; }
          var u = new URL(a.href, location.href);
          save({ pin_link: { href: u.pathname + u.search, label: label } })
            .then(function (d) { if (d && d.ok) toast('«' + label + '» — у «Мої».'); });
        });
      }
      if (key && (catalog(key) || st.links[key])) {
        addItem(ctx, '🎨 Свій значок…', function () { iconPicker(key, x, y); });
        if (st.icons[key]) addItem(ctx, '↺ Значок як був', function () { var c = {}; c[key] = null; save({ icons: c }); });
      }
      addItem(ctx, '↗ Відкрити в новій вкладці', function () { window.open(a.href, '_blank', 'noopener'); });
      sep(ctx);
    }
    if (st.mode === 'simple') addItem(ctx, '☰ Повне меню — усі розділи', function () { setMode('full'); });
    else addItem(ctx, '✨ Просте меню', function () { setMode('simple'); });
    addItem(ctx, '↺ Меню як було (стандартні «Мої» й значки)', function () { save({ reset: true }); });
    place(ctx, x, y);
  }
  function iconPicker(key, x, y) {
    hideCtx();
    ctx = el('div', 'mvd-ctx mvd-icons'); ctx.setAttribute('role', 'dialog'); ctx.setAttribute('aria-label', 'Значок');
    var it = byKey(key);
    sep(ctx, 'Значок для «' + (it ? it.label : key) + '»');
    var grid = el('div', 'mvd-igrid');
    cfg.iconChoices.forEach(function (ic) {
      var b = el('button', it && it.icon === ic ? 'on' : '', ic); b.type = 'button'; b.title = ic;
      b.onclick = function () { var c = {}; c[key] = ic; hideCtx(); save({ icons: c }); };
      grid.appendChild(b);
    });
    ctx.appendChild(grid);
    var row = el('div', 'mvd-irow');
    var inp = el('input'); inp.type = 'text'; inp.maxLength = 8; inp.placeholder = 'або свій емодзі';
    inp.setAttribute('aria-label', 'Свій значок (емодзі)');
    var ok = el('button', '', '✓'); ok.type = 'button';
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
    place(ctx, x, y);
  }
  function lookMenu(x, y) {
    hideCtx();
    ctx = el('div', 'mvd-ctx'); ctx.setAttribute('role', 'menu');
    sep(ctx, 'Вигляд Minerva на ПК');
    addItem(ctx, (NEW ? '● ' : '○ ') + 'Новий вигляд', function () { design('new'); });
    addItem(ctx, (NEW ? '○ ' : '● ') + 'Класичний — як було', function () { design('classic'); });
    sep(ctx);
    addItem(ctx, '♿ Тема, шрифт, розмір тексту…', function () { location.href = '/core/my-settings/'; });
    place(ctx, x, y);
  }
  function design(v) {
    if ((v === 'new') === NEW) return;
    post('/core/my-settings/', { ui: { design: v } }).then(function () { location.reload(); })
      .catch(function () { toast('Не вдалося зберегти — перевірте зв’язок.'); });
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
    u.title = name; u.setAttribute('aria-label', 'Акаунт: ' + name); u.setAttribute('aria-haspopup', 'menu');
    u.onclick = function (e) {
      e.stopPropagation();
      hideCtx();
      ctx = el('div', 'mvd-ctx'); ctx.setAttribute('role', 'menu');
      if (name) sep(ctx, name);
      addItem(ctx, '⚙ Мої налаштування', function () { location.href = '/core/my-settings/'; });
      [].forEach.call(tools.querySelectorAll('a[href*="password_change"]'), function (a) {
        addItem(ctx, '🔑 ' + a.textContent.trim(), function () { location.href = a.href; });
      });
      addItem(ctx, '🎨 Вигляд: класичний / новий…', function () { var r = u.getBoundingClientRect(); lookMenu(r.right - 240, r.bottom + 4); });
      var out = document.getElementById('logout-form');
      if (out) {
        sep(ctx);
        var b = out.querySelector('button, input[type="submit"]');
        addItem(ctx, '🚪 ' + ((b && (b.textContent || b.value)) || 'Вийти').trim(), function () { out.submit(); });
      }
      var r = u.getBoundingClientRect();
      place(ctx, r.right - 240, r.bottom + 6);
    };
    tools.appendChild(u);
  }

  /* ── Ctrl K: розділи й пошук ── */
  function sources() {
    var seen = {}, out = [];
    function add(icon, label, href, where) {
      label = (label || '').replace(/\s+/g, ' ').trim();
      if (!label || !href || seen[href]) return;
      seen[href] = 1;
      out.push({ icon: icon, label: label, href: href, where: where || '' });
    }
    st.pinned.map(byKey).filter(Boolean).forEach(function (i) { add(i.icon, i.label, i.href, '⭐ Мої'); });
    cfg.items.forEach(function (i) { add(st.icons[i.key] || i.icon, i.label, i.href, 'Розділ'); });
    [].forEach.call(document.querySelectorAll('#nav-sidebar .mg-group a[href]'), function (a) {
      var g = a.closest('.mg-group'), head = g && g.querySelector('.mg-header-link');
      var text = a.childNodes[0] && a.childNodes[0].nodeType === 3 ? a.childNodes[0].textContent : a.textContent;
      add('•', text, a.getAttribute('href'), head ? head.textContent.trim() : '');
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
    root.classList.add('mvd-pal-open');
    var q = document.getElementById('mvd-pal-q');
    q.value = ''; results(''); q.focus();
  }
  function closePalette() { root.classList.remove('mvd-pal-open'); }
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
    var host = form ? form.querySelector('div') || form : document.querySelector('#changelist .changelist-form-container');
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
    b.title = 'Меню «Просте / Повне», пошук усюди (Ctrl K), списки й фільтри по-новому. Назад — 🎨 угорі меню.';
    b.onclick = function () { design('new'); };
    s.insertBefore(b, s.firstChild);
  }

  function start() {
    if (!NEW) { classicPill(); return; }
    buildMenu();
    buildHeader();
    filters();
    document.addEventListener('click', function (e) { if (ctx && !e.target.closest('.mvd-ctx')) hideCtx(); });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'k' || e.key === 'K')) {
        if (!wide()) return;
        e.preventDefault();
        if (root.classList.contains('mvd-pal-open')) closePalette(); else openPalette();
        return;
      }
      if (e.key === 'Escape') { hideCtx(); closePalette(); drawer(false); }
    });
    addEventListener('scroll', function () { if (ctx && !ctx.classList.contains('mvd-icons')) hideCtx(); }, true);
  }
  window.MVDesktop = { palette: openPalette, menu: function () { return st; } };
  // після бокового меню з base_site (воно будується на DOMContentLoaded і спершу все з нього прибирає)
  if (document.readyState === 'complete') start(); else document.addEventListener('DOMContentLoaded', start);
})();
