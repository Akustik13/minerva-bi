/* 📌 Панель праворуч у списках (core/side_panel.py).
 *
 * Клік по рядку (не по посиланню / полю / кнопці) — подробиці запису праворуч, без переходу; подвійний клік — картка;
 * ↑ / ↓ — сусідній рядок, Esc — закрити. Правий клік на панелі або ⚙ — налаштування для себе: блоки (додати,
 * сховати, вище / нижче), швидкі дії, ширина, «як було», вимкнути панель. Вибір зберігається в профілі
 * (POST …/prefs/). Телефон (вузький екран) — як раніше: рядок відкриває картку.
 */
(function () {
  'use strict';
  var cfgEl = document.getElementById('mv-panel-config');
  if (!cfgEl || window.MVPanel) return;
  var cfg = JSON.parse(cfgEl.textContent), prefs = cfg.prefs;
  var MIN_WIDTH = 900, panel, toggle, current = null, menu = null, req = 0;

  function csrf() { return (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || ''; }
  function rows() {
    return [].slice.call(document.querySelectorAll('#result_list tbody tr, .sv-card[data-pk]'))
      .filter(function (r) { return r.offsetParent !== null; });
  }
  function pkOf(row) {
    var cb = row.querySelector('input.action-select');
    return (cb && cb.value) || row.dataset.pk || '';
  }
  function cardUrl(row) {
    var a = row.querySelector('a[href*="/change/"]');
    return a ? a.href : '';
  }
  function setWidth() {
    document.documentElement.style.setProperty('--mvp-w', prefs.width + 'px');
    var head = document.getElementById('header');
    document.documentElement.style.setProperty('--mvp-top', (head ? Math.max(0, head.getBoundingClientRect().bottom) : 0) + 'px');
  }

  function build() {
    panel = document.createElement('aside');
    panel.id = 'mv-panel'; panel.setAttribute('aria-label', 'Подробиці запису');
    panel.innerHTML = '<div class="mvp-body"><div class="mvp-empty">Клікніть рядок у списку.</div></div>';
    document.body.appendChild(panel);
    toggle = document.createElement('button');
    toggle.type = 'button'; toggle.id = 'mvp-toggle'; toggle.className = 'button';
    var host = document.getElementById('changelist') || document.getElementById('content-main');
    if (host) host.insertBefore(toggle, host.firstChild);
    toggle.onclick = function () { save({ on: !prefs.on }); if (!prefs.on) close(); };
    label();
    setWidth();
  }
  function label() {
    toggle.textContent = prefs.on ? '📌 Панель праворуч: увімкнено' : '📌 Панель праворуч: вимкнено';
    toggle.title = prefs.on ? 'Клік по рядку відкриває подробиці праворуч. Натисніть, щоб вимкнути.'
                            : 'Увімкнути: клік по рядку — подробиці праворуч без переходу на картку.';
  }

  function select(row) {
    var pk = pkOf(row);
    if (!pk) return;
    rows().forEach(function (r) { r.classList.toggle('mvp-sel', r === row); });
    current = { pk: pk, row: row };
    load();
  }
  function load() {
    if (!current) return;
    var mine = ++req;
    panel.classList.add('mvp-busy');
    document.body.classList.add('mvp-open'); setWidth();
    fetch(cfg.url + current.pk + '/', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw r.status; return r.text(); })
      .then(function (html) { if (mine === req) { panel.innerHTML = html; panel.classList.remove('mvp-busy'); } })
      .catch(function () {
        if (mine !== req) return;
        panel.classList.remove('mvp-busy');
        panel.innerHTML = '<div class="mvp-head"><div class="mvp-title">Не вдалося завантажити</div>' +
          '<button type="button" class="mvp-icon" data-mvp-close aria-label="Закрити">✕</button></div>';
      });
  }
  function close() {
    document.body.classList.remove('mvp-open');
    rows().forEach(function (r) { r.classList.remove('mvp-sel'); });
    current = null;
  }
  function save(change) {
    Object.keys(change).forEach(function (k) { prefs[k] = change[k]; });
    label(); setWidth();
    fetch(cfg.prefsUrl, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(prefs) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.prefs) { prefs = d.prefs; label(); setWidth(); } if (current) load(); })
      .catch(function () {});
  }

  /* ── меню налаштувань (правий клік на панелі або ⚙) ── */
  function hideMenu() { if (menu) { menu.remove(); menu = null; } }
  function item(box, text, fn) {
    var b = document.createElement('button'); b.type = 'button'; b.textContent = text;
    b.onclick = function () { hideMenu(); fn(); }; box.appendChild(b);
  }
  function sep(box, title) {
    if (title) { var t = document.createElement('div'); t.className = 't'; t.textContent = title; box.appendChild(t); }
    else box.appendChild(document.createElement('hr'));
  }
  function move(key, d) {
    var list = prefs.blocks.slice(), i = list.indexOf(key), j = i + d;
    if (i < 0 || j < 0 || j >= list.length) return;
    list.splice(j, 0, list.splice(i, 1)[0]);
    save({ blocks: list });
  }
  function openMenu(x, y, blockKey) {
    hideMenu();
    menu = document.createElement('div'); menu.className = 'mvp-ctx'; menu.setAttribute('role', 'menu');
    if (blockKey) {
      var title = (cfg.blocks.filter(function (b) { return b.key === blockKey; })[0] || {}).title || blockKey;
      sep(menu, 'Блок «' + title + '»');
      var i = prefs.blocks.indexOf(blockKey);
      if (i > 0) item(menu, '⬆ Вище', function () { move(blockKey, -1); });
      if (i < prefs.blocks.length - 1) item(menu, '⬇ Нижче', function () { move(blockKey, 1); });
      item(menu, '🙈 Сховати блок', function () { save({ blocks: prefs.blocks.filter(function (k) { return k !== blockKey; }) }); });
      sep(menu);
    }
    var hidden = cfg.blocks.filter(function (b) { return prefs.blocks.indexOf(b.key) < 0; });
    if (hidden.length) {
      sep(menu, '➕ Додати блок');
      hidden.forEach(function (b) { item(menu, '＋ ' + b.title, function () { save({ blocks: prefs.blocks.concat([b.key]) }); }); });
    }
    sep(menu, '⚡ Швидкі дії внизу панелі');
    cfg.actions.forEach(function (a) {
      var on = prefs.actions.indexOf(a.key) >= 0;
      item(menu, (on ? '☑ ' : '☐ ') + a.label, function () {
        save({ actions: on ? prefs.actions.filter(function (k) { return k !== a.key; }) : prefs.actions.concat([a.key]) });
      });
    });
    sep(menu);
    var wi = cfg.widths.indexOf(prefs.width);
    if (wi < cfg.widths.length - 1) item(menu, '↔ Ширше', function () { save({ width: cfg.widths[wi + 1] }); });
    if (wi > 0) item(menu, '↔ Вужче', function () { save({ width: cfg.widths[wi - 1] }); });
    item(menu, '↺ Як було (стандартні блоки й дії)', function () { save({ blocks: null, actions: null, width: null, on: true, _reset: true }); });
    item(menu, '✕ Вимкнути панель у цьому списку', function () { close(); save({ on: false }); });
    document.body.appendChild(menu);
    menu.style.left = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8)) + 'px';
    var first = menu.querySelector('button'); if (first) first.focus();
  }

  function start() {
    if (innerWidth < MIN_WIDTH) return;            // телефон / вузьке вікно — як раніше
    build();
    document.addEventListener('click', function (e) {
      if (menu && !e.target.closest('.mvp-ctx')) hideMenu();
      if (e.target.closest('[data-mvp-close]')) { close(); return; }
      var gear = e.target.closest('[data-mvp-menu]');
      if (gear) { var r = gear.getBoundingClientRect(); openMenu(r.left - 200, r.bottom + 4); return; }
      if (!prefs.on || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      var row = e.target.closest('#result_list tbody tr, .sv-card[data-pk]');
      if (!row || e.target.closest('a, button, input, select, textarea, label, [data-no-panel], [onclick]')) return;
      e.preventDefault();
      select(row);
    });
    document.addEventListener('dblclick', function (e) {
      var row = e.target.closest('#result_list tbody tr, .sv-card[data-pk]');
      if (row && !e.target.closest('a, button, input, select, textarea')) { var u = cardUrl(row); if (u) location.href = u; }
    });
    panel.addEventListener('contextmenu', function (e) {
      if (e.shiftKey || e.target.closest('a')) return;           // посилання / Shift — меню браузера
      e.preventDefault();
      var block = e.target.closest('[data-block]');
      openMenu(e.clientX, e.clientY, block ? block.dataset.block : '');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { if (menu) hideMenu(); else if (current) close(); return; }
      if (!current || /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || '')) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        var list = rows(), i = list.indexOf(current.row), next = list[i + (e.key === 'ArrowDown' ? 1 : -1)];
        if (next) { e.preventDefault(); select(next); next.scrollIntoView({ block: 'nearest' }); }
      }
    });
    addEventListener('scroll', function () { if (menu) hideMenu(); setWidth(); }, true);
    addEventListener('resize', function () { setWidth(); if (innerWidth < MIN_WIDTH) close(); });
  }
  window.MVPanel = { open: function (pk) { var r = rows().filter(function (x) { return pkOf(x) === String(pk); })[0]; if (r) select(r); },
                     close: close };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
