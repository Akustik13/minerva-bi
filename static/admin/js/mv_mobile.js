/* 📱 Телефон (вузький екран ≤ 768 px, core/mobile_nav.py, CSS — static/admin/css/mv_mobile.css).
 *
 * Нижня панель: закріплені розділи (до 4) + «☰ Ще». «Ще» — меню знизу: усі доступні розділи плитками,
 * ☆ / ★ — закріпити внизу чи прибрати (зберігається в профілі, POST /core/mobile-nav/), «Усі розділи» —
 * повне бокове меню, акаунт (налаштування, мова, кошик, вийти). Списки адмінки — картками (підписи з заголовків
 * колонок, порожні поля сховано, понад 6 полів — «⋯ ще»), фільтри — кнопкою «Фільтри» знизу. На формах і під
 * час введення тексту нижньої панелі немає (кнопки «Зберегти» внизу). Ширший екран — усе як було.
 */
(function () {
  'use strict';
  if (window.MVMobile) return;
  var cfgEl = document.getElementById('mv-mobile-config');
  var cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null;
  var mq = window.matchMedia('(max-width: 768px)');
  var bar = null, sheet = null, filterBtn = null, note = null;
  var MAX_FIELDS = 6;
  var NOT_FILTERS = ['q', 'p', 'o', 'e', 'all', '_popup', '_to_field', '_changelist_filters'];

  function csrf() { return (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ''; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function item(key) { return cfg.items.filter(function (i) { return i.key === key; })[0]; }

  /* ── розділ, у якому людина зараз: найдовша адреса, з якої починається сторінка ── */
  function activeKey() {
    var path = location.pathname, best = '', len = 0;
    cfg.items.forEach(function (i) {
      var hit = i.href === '/admin/' ? path === '/admin/' : path.indexOf(i.href) === 0;
      if (hit && i.href.length > len) { best = i.key; len = i.href.length; }
    });
    return best;
  }

  /* ── нижня панель ── */
  function renderBar() {
    if (!bar) {
      bar = el('nav'); bar.id = 'mv-bnav'; bar.setAttribute('aria-label', 'Головні розділи');
      document.body.appendChild(bar);
      document.body.classList.add('mv-has-bnav');
    }
    bar.textContent = '';
    var active = activeKey();
    cfg.pinned.forEach(function (key) {
      var i = item(key);
      if (!i) return;
      var a = el('a', 'mvb-item' + (key === active ? ' on' : ''));
      a.href = i.href;
      if (key === active) a.setAttribute('aria-current', 'page');
      a.appendChild(el('span', 'mvb-ico', i.icon));
      a.appendChild(el('span', 'mvb-lbl', i.label));
      bar.appendChild(a);
    });
    var more = el('button', 'mvb-item mvb-more');
    more.type = 'button';
    more.setAttribute('aria-haspopup', 'dialog');
    more.appendChild(el('span', 'mvb-ico', '☰'));
    more.appendChild(el('span', 'mvb-lbl', 'Ще'));
    more.onclick = openSheet;
    bar.appendChild(more);
  }

  function savePinned(list) {
    cfg.pinned = list;
    renderBar(); renderTiles();
    fetch(cfg.saveUrl, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify({ pinned: list }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.pinned) { cfg.pinned = d.pinned; renderBar(); renderTiles(); } })
      .catch(function () { say('Не вдалося зберегти — перевірте зв’язок.'); });
  }
  function say(text) { if (note) note.textContent = text; }

  /* ── меню «Ще» ── */
  function buildSheet() {
    sheet = el('div'); sheet.id = 'mv-more'; sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true'); sheet.setAttribute('aria-label', 'Меню');
    var shade = el('div', 'mvm-shade'); shade.onclick = closeSheet;
    var box = el('div', 'mvm-box');
    var head = el('div', 'mvm-head');
    head.appendChild(el('div', 'mvm-title', 'Меню'));
    var x = el('button', 'mvm-x', '✕'); x.type = 'button'; x.setAttribute('aria-label', 'Закрити'); x.onclick = closeSheet;
    head.appendChild(x);
    box.appendChild(head);
    note = el('div', 'mvm-note', 'Натисніть ☆, щоб закріпити розділ унизу екрана (до ' + cfg.max + ').');
    box.appendChild(note);
    var grid = el('div', 'mvm-grid'); grid.id = 'mvm-grid';
    box.appendChild(grid);
    var row = el('div', 'mvm-row');
    var full = el('button', 'mvm-wide', '☰ Усі розділи — повне меню'); full.type = 'button';
    full.onclick = function () { closeSheet(); openDrawer(); };
    row.appendChild(full);
    var reset = el('button', 'mvm-wide mvm-soft', '↺ Як було'); reset.type = 'button';
    reset.title = 'Головна, Замовлення, Склад, Пошта';
    reset.onclick = function () {
      fetch(cfg.saveUrl, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify({ pinned: null }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.pinned) { cfg.pinned = d.pinned; renderBar(); renderTiles(); say('Нижня панель — як було.'); } })
        .catch(function () { say('Не вдалося зберегти — перевірте зв’язок.'); });
    };
    row.appendChild(reset);
    box.appendChild(row);
    box.appendChild(account());
    sheet.appendChild(shade); sheet.appendChild(box);
    document.body.appendChild(sheet);
    renderTiles();
  }
  function renderTiles() {
    var grid = document.getElementById('mvm-grid');
    if (!grid) return;
    grid.textContent = '';
    var active = activeKey();
    cfg.items.forEach(function (i) {
      var on = cfg.pinned.indexOf(i.key) >= 0;
      var tile = el('div', 'mvm-tile' + (i.key === active ? ' here' : ''));
      var a = el('a', 'mvm-link'); a.href = i.href;
      a.appendChild(el('span', 'mvm-ico', i.icon));
      a.appendChild(el('span', 'mvm-lbl', i.label));
      tile.appendChild(a);
      var star = el('button', 'mvm-star' + (on ? ' on' : ''), on ? '★' : '☆');
      star.type = 'button';
      star.setAttribute('aria-pressed', on ? 'true' : 'false');
      star.setAttribute('aria-label', (on ? 'Прибрати з нижньої панелі: ' : 'Закріпити внизу: ') + i.label);
      star.onclick = function () {
        if (on) { savePinned(cfg.pinned.filter(function (k) { return k !== i.key; })); say('«' + i.label + '» прибрано з нижньої панелі.'); return; }
        if (cfg.pinned.length >= cfg.max) { say('Унизу вже ' + cfg.max + ' розділи — спершу приберіть один (★).'); return; }
        savePinned(cfg.pinned.concat([i.key])); say('«' + i.label + '» — тепер унизу екрана.');
      };
      tile.appendChild(star);
      grid.appendChild(tile);
    });
  }
  /* акаунт: те, що на ПК у шапці праворуч (у телефоні шапка коротка) */
  function account() {
    var box = el('div', 'mvm-account');
    box.appendChild(el('div', 'mvm-sub', 'Акаунт'));
    var links = el('div', 'mvm-links');
    function add(href, text) { var a = el('a', 'mvm-chip', text); a.href = href; links.appendChild(a); }
    add('/core/my-settings/', '⚙ Мої налаштування');
    var cart = document.getElementById('mv-cart-btn');
    if (cart) add(cart.getAttribute('href'), '🛒 Кошик' + ((document.getElementById('mv-cart-badge') || {}).textContent ? ' (' + document.getElementById('mv-cart-badge').textContent + ')' : ''));
    [].forEach.call(document.querySelectorAll('#user-tools a[href*="password_change"]'), function (a) { add(a.getAttribute('href'), '🔑 ' + a.textContent.trim()); });
    [].forEach.call(document.querySelectorAll('#mv-lang-menu a'), function (a) { add(a.getAttribute('href'), '🌐 ' + a.textContent.trim()); });
    box.appendChild(links);
    var out = document.getElementById('logout-form');
    if (out) {
      var f = out.cloneNode(true); f.removeAttribute('id'); f.className = 'mvm-logout';
      var b = f.querySelector('button, input[type="submit"]');
      if (b) { b.className = 'mvm-chip'; b.textContent = '🚪 ' + (b.textContent || b.value || 'Вийти').trim(); }
      box.appendChild(f);
    }
    return box;
  }
  function openSheet() {
    if (!sheet) buildSheet();
    renderTiles();
    say('Натисніть ☆, щоб закріпити розділ унизу екрана (до ' + cfg.max + ').');
    document.documentElement.classList.add('mv-sheet-open');
    var first = sheet.querySelector('.mvm-x'); if (first) first.focus();
  }
  function closeSheet() { document.documentElement.classList.remove('mv-sheet-open'); }
  function openDrawer() {
    var side = document.getElementById('nav-sidebar'), shade = document.getElementById('sidebar-overlay');
    if (side) side.classList.add('mob-open');
    if (shade) shade.classList.add('mob-open');
  }

  /* ── список адмінки → картки ── */
  function labelOf(th) {
    var t = th.querySelector('.text') || th;
    return (t.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function cards() {
    var table = document.getElementById('result_list');
    if (!table || table.dataset.mvCards) return;
    table.dataset.mvCards = '1';
    document.documentElement.classList.add('mv-cards');
    var heads = [].map.call(table.querySelectorAll('thead th'), labelOf);
    [].forEach.call(table.querySelectorAll('tbody tr'), function (tr) {
      var shown = 0, extra = 0;
      [].forEach.call(tr.children, function (cell, i) {
        if (cell.classList.contains('action-checkbox')) return;
        if (heads[i]) cell.setAttribute('data-label', heads[i]);
        var txt = (cell.textContent || '').replace(/\s+/g, ' ').trim();
        if ((!txt || txt === '-' || txt === '—') && !cell.querySelector('img, input, select, button, svg')) { cell.classList.add('mvc-empty'); return; }
        if (cell.tagName === 'TH') { cell.classList.add('mvc-title'); return; }
        shown += 1;
        if (shown > MAX_FIELDS) { cell.classList.add('mvc-extra'); extra += 1; }
      });
      if (extra) {
        var td = el('td', 'mvc-more');
        var b = el('button', 'mvc-more-btn', '⋯ ще ' + extra); b.type = 'button';
        b.onclick = function (e) {
          e.stopPropagation();
          var open = tr.classList.toggle('mvc-open');
          b.textContent = open ? '▴ менше' : '⋯ ще ' + extra;
        };
        td.appendChild(b); tr.appendChild(td);
      }
    });
    /* дотик по картці (не по посиланню, полю чи кнопці) — картка запису */
    table.addEventListener('click', function (e) {
      if (!mq.matches || e.target.closest('a, button, input, select, textarea, label')) return;
      var tr = e.target.closest('tbody tr');
      var a = tr && (tr.querySelector('th a[href]') || tr.querySelector('a[href*="/change/"]'));
      if (a) location.href = a.href;
    });
  }

  /* ── фільтри списку — знизу, кнопкою ── */
  function filters() {
    var box = document.getElementById('changelist-filter');
    if (!box || filterBtn) return;
    var n = 0;
    new URLSearchParams(location.search).forEach(function (_v, k) { if (NOT_FILTERS.indexOf(k) < 0) n += 1; });
    filterBtn = el('button', 'mvf-btn', n ? '⚙ Фільтри · ' + n : '⚙ Фільтри');
    filterBtn.type = 'button';
    filterBtn.setAttribute('aria-expanded', 'false');
    filterBtn.onclick = function () { toggleFilters(true); };
    var host = document.getElementById('toolbar') || document.querySelector('#changelist .changelist-form-container') || document.getElementById('changelist');
    if (host) host.insertBefore(filterBtn, host.firstChild);
    var head = el('div', 'mvf-head');
    head.appendChild(el('b', '', 'Фільтри'));
    var x = el('button', 'mvm-x', '✕'); x.type = 'button'; x.setAttribute('aria-label', 'Закрити фільтри');
    x.onclick = function () { toggleFilters(false); };
    head.appendChild(x);
    box.insertBefore(head, box.firstChild);
    var shade = el('div', 'mvf-shade'); shade.onclick = function () { toggleFilters(false); };
    document.body.appendChild(shade);
  }
  function toggleFilters(on) {
    document.documentElement.classList.toggle('mv-filters-open', on);
    if (filterBtn) filterBtn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }

  /* ── введення тексту: клавіатура на пів екрана — нижню панель ховаємо ── */
  function typing(e) {
    var t = e.target;
    var on = e.type === 'focusin' && t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) &&
             !/^(checkbox|radio|button|submit|reset|file)$/.test(t.type || '');
    document.documentElement.classList.toggle('mv-typing', !!on);
  }

  /* ── форма: кнопки «Зберегти» внизу екрана — помічники стоять над ними ── */
  var submitSeen = false;
  function submitRow() {
    if (submitSeen || !document.body.classList.contains('change-form')) return;
    var rows = document.querySelectorAll('#content-main .submit-row');
    var row = rows[rows.length - 1];
    if (!row) return;
    submitSeen = true;
    /* один рядок: головна кнопка («Зберегти») + «⋯» — інші («і додати», «і продовжити», «Видалити») */
    var ctl = [].filter.call(row.querySelectorAll('input[type="submit"], button, a'), function (c) {
      return c.offsetParent !== null;
    });
    if (ctl.length > 2) {
      var main = row.querySelector('input.default, button.default') || ctl[0];
      ctl.forEach(function (c) { if (c !== main) c.classList.add('mv-sr-extra'); });
      var more = el('button', 'mv-sr-btn', '⋯'); more.type = 'button';
      more.setAttribute('aria-expanded', 'false');
      more.setAttribute('aria-label', 'Інші дії: ' + ctl.filter(function (c) { return c !== main; })
        .map(function (c) { return (c.value || c.textContent || '').trim(); }).join(', '));
      more.onclick = function () {
        var open = row.classList.toggle('mv-sr-open');
        more.setAttribute('aria-expanded', open ? 'true' : 'false');
        more.textContent = open ? '✕' : '⋯';
      };
      row.classList.add('mv-sr');
      main.insertAdjacentElement('afterend', more);
    }
    function set() { document.documentElement.style.setProperty('--mv-submit-h', row.offsetHeight + 'px'); }
    set();
    if (window.ResizeObserver) new ResizeObserver(set).observe(row);
  }

  function apply() {
    if (!mq.matches) { closeSheet(); toggleFilters(false); return; }
    if (cfg && cfg.items && cfg.items.length && !bar) renderBar();
    cards();
    filters();
    submitRow();
  }

  function start() {
    /* ☰ у шапці (на формах, де нижньої панелі немає) — те саме меню «Ще», а звідти — повне меню */
    var hb = document.getElementById('mob-menu-btn');
    if (hb && cfg && cfg.items && cfg.items.length) {
      var nb = hb.cloneNode(true);
      hb.parentNode.replaceChild(nb, hb);
      nb.addEventListener('click', openSheet);
    }
    apply();
    if (mq.addEventListener) mq.addEventListener('change', apply); else mq.addListener(apply);
    document.addEventListener('focusin', typing);
    document.addEventListener('focusout', typing);
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      closeSheet(); toggleFilters(false);
    });
  }
  window.MVMobile = { open: openSheet, close: closeSheet, filters: toggleFilters };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
