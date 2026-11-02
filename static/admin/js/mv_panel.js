/* 📌 Панель праворуч у списках — сторінка списку (сервер: core/side_panel.py, core/panel_edit.py, core/views.py).
 *
 * Клік по рядку (не по посиланню, полю, кнопці, етикетці) — подробиці запису праворуч без переходу; подвійний клік —
 * картка. Шапка: ‹ › сусідні записи, назва, статус, ⤢ картка, ⚙ «під себе», ✕. Під нею — швидкі дії (перші N —
 * кнопками, решта — «⋯»), підменю розділів (фішки з лічильником і тоном: «📜 Усе підряд» з підсвічуванням розділу
 * під час прокрутки або «🗂 По одному»), розділи (згортаються, важкі вантажаться, коли до них дійшли), низ (↻ ⌨ і
 * місце для помічників 🏛 і «Minerva»). Перехід до іншого запису лишає той самий розділ і місце в ньому; сусідній
 * запис вантажиться наперед. Швидкі зміни (статус, ⭐, нотатка…) — POST …/<pk>/edit/ (той самий шлях, що «Зберегти»
 * в картці), дії списку для одного запису — звичайна форма на адресу списку. Вибір людини (розділи, порядок, дії,
 * ширина, колонки, вигляд) — правий клік, ⚙ або «⚙ Усі налаштування панелі…»: діє одразу, зберігається за 400 мс
 * (POST …/prefs/ {set, shared, reset}), «↶ Скасувати» — 6 с.
 * Меню й повідомлення — window.MVMenu (static/admin/js/mv_menu.js); без нього меню немає, клік і клавіші — є.
 * Вікно вужче за 900 px (з «Розміром тексту») — панелі немає, рядки — як раніше (телефон — mv_mobile.js).
 * Події document: «mv:panel» {open, width, model, pk} — бокове меню (mv_desktop.js) звужується до значків одразу,
 * до читання --mvd-side-w; «mv:panel-edit» {model, pk, key, value}. API — window.MVPanel.
 * Дані записів — лише в HTML з сервера (екранований Django); усе своє — через textContent.
 */
(function () {
  'use strict';
  var cfgEl = document.getElementById('mv-panel-config');
  if (!cfgEl || window.MVPanel) return;
  var cfg;
  try { cfg = JSON.parse(cfgEl.textContent || '{}'); } catch (err) { return; }
  if (!cfg || !cfg.url || !cfg.model) return;

  /* ── сталі ── */
  var MIN_EFF = 900, OVERLAY_EFF = 1100, PUSH_LIST = 720, LIST_MIN = 480, SIDE_MIN = 440;
  var AUTO_MIN = 360, AUTO_MAX = 520, AUTO_SHARE = 0.28, BAR_MAX = 8;
  var W_MIN = (cfg.width && cfg.width.min) || 320, W_MAX = (cfg.width && cfg.width.max) || 960;
  var PRESETS = (cfg.width && cfg.width.presets) || [360, 440, 560, 720];
  var CACHE_MAX = 6, CACHE_TTL = 60000, NAV_DELAY = 120, HASH_DELAY = 300, SAVE_DELAY = 400, SPY_PAUSE = 600;
  var REOPEN_TTL = 120000, EDGE_MS = 1500, SNAP = 12, DRAG_MIN = 6;
  var ROWS = '#result_list tbody tr, .sv-card[data-pk], .crm-card[data-pk], [data-mvp-row-pk]';
  var NO_OPEN = 'a, button, input, select, textarea, label, [onclick], [data-no-panel], [data-svl], [contenteditable]';
  var NO_MENU = 'a[href], input, select, textarea, [contenteditable], [data-svl], [data-own-menu]';
  var LAYERS = '#rg-panel.open, .svl-modal, #svl-menu, .mvd-ctx';
  // фокус там — клавіші їхні (бокове меню, помічники, пошук усюди, фільтри, шапка сторінки)
  var KEY_SKIP = '#nav-sidebar, #mv-chat-panel, #rg-panel, #mvd-pal, #changelist-filter, #header, .mvm';
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([type="hidden"]), select, textarea, ' +
                  '[tabindex]:not([tabindex="-1"])';
  var COL = /^[a-z_][a-z0-9_]*$/, KEY = /^[A-Za-z][A-Za-z0-9_-]{0,47}$/, PK = /^\d+$/;
  var HASH = /^#mvp=(\d+)(?:\/([A-Za-z][A-Za-z0-9_-]{0,47}))?$/;
  var LEAD = /^\s*([^\s\p{L}\p{N}]+)\s+(\S[\s\S]*)$/u;           // «🏷 Етикетка» → значок + підпис
  var LIST_KEYS = ['on', 'blocks', 'collapsed', 'actions', 'bar', 'width', 'hide_cols', 'start'];
  var SHARED = cfg.shared || ['nav', 'view', 'chips', 'density', 'actions_at', 'place', 'helpers', 'empty'];
  var RESETS = { blocks: ['blocks', 'collapsed', 'start'], actions: ['actions', 'bar'],
                 layout: ['width', 'hide_cols'].concat(SHARED), list: LIST_KEYS, all: LIST_KEYS.concat(SHARED) };
  var RESET_MSG = { blocks: 'Розділи — як було', actions: 'Швидкі дії — як було',
                    layout: 'Вигляд для всіх списків — як було', list: 'Панель у цьому списку — як було',
                    all: 'Панель — як було' };
  var ON_TITLE = 'Клік по рядку відкриває подробиці праворуч. Натисніть, щоб вимкнути.';
  var OFF_TITLE = 'Увімкнути: клік по рядку — подробиці праворуч без переходу на картку.';
  var SAVE_FAIL = 'Не вдалося зберегти — перевірте зв’язок.';

  /* ── дрібниці ── */
  var root = document.documentElement;
  function zoom() { return parseFloat(getComputedStyle(document.body).zoom) || 1; }
  function eff() { return innerWidth / zoom(); }
  function wide() { return eff() >= MIN_EFF; }
  function now() { return Date.now(); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = String(text);
    return e;
  }
  function button(cls, text, label) {
    var b = el('button', cls, text);
    b.type = 'button';
    if (label) { b.setAttribute('aria-label', label); b.title = label; }
    return b;
  }
  function fmt(s) {                                         // цілий напис з {0}, {1} — перекладається цілим
    var a = arguments;
    return String(s).replace(/\{(\d+)\}/g, function (m, i) { var v = a[+i + 1]; return v == null ? '' : String(v); });
  }
  function clone(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function attr(n, name) { return (n && n.getAttribute && n.getAttribute(name)) || ''; }
  function parseJSON(s, dflt) { try { return s ? JSON.parse(s) : dflt; } catch (e) { return dflt; } }
  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; } }
    var i = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return i ? i.value : '';
  }
  function sGet(kind, k) { try { var s = window[kind]; return s ? s.getItem(k) : null; } catch (e) { return null; } }
  function sSet(kind, k, v) { try { var s = window[kind]; if (s) s.setItem(k, v); } catch (e) { /* без сховища — як є */ } }
  function sDel(kind, k) { try { var s = window[kind]; if (s) s.removeItem(k); } catch (e) { /* без сховища */ } }
  function reduced() {
    var m = root.getAttribute('data-mv-motion');
    if (m === 'reduce') return true;
    if (m === 'full') return false;
    return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function typing(t) {
    return !!(t && t.nodeType === 1 && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)));
  }
  function emit(name, detail) {
    try { document.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) { /* старий браузер */ }
  }
  function focusOn(x) {
    if (!x || !x.focus) return;
    try { x.focus({ preventScroll: true }); } catch (e) { try { x.focus(); } catch (y) { /* не фокусується */ } }
  }
  function setVar(node, name, v) { if (node && node.style.getPropertyValue(name) !== v) node.style.setProperty(name, v); }
  function menuOpen() { return !!(window.MVMenu && MVMenu.isOpen && MVMenu.isOpen()); }
  function menu(at, items, opts) {
    if (!window.MVMenu || !MVMenu.open) return null;
    return MVMenu.open(at, items, opts || {});
  }
  function toast(text, o) {
    if (window.MVMenu && MVMenu.toast) return MVMenu.toast(text, o || {});
    announce(text);
    return null;
  }
  function layerOpen() {
    return menuOpen() || root.classList.contains('mvd-pal-open') || root.classList.contains('mvd-fopen') ||
      !!document.querySelector(LAYERS);
  }
  function keyName(code) {
    code = String(code || '');
    if (/^Key[A-Z]$/.test(code)) return code.slice(3);
    if (/^Digit\d$/.test(code)) return code.slice(5);
    return ({ Slash: '/', Period: '.', BracketLeft: '[', BracketRight: ']', Comma: ',', Minus: '-', Equal: '=' })[code] ||
      code;
  }
  function parse(html, sel) {                              // HTML з ендпойнтів панелі (сервер, екранований)
    var t = document.createElement('template');
    t.innerHTML = String(html || '').trim();
    return t.content.querySelector(sel);
  }

  /* ── вибір людини й опис панелі з сервера ── */
  var prefs = clone(cfg.prefs) || {};
  var blockCfg = {}, actCfg = {}, editCfg = {}, adminCfg = {};
  (cfg.blocks || []).forEach(function (b) { blockCfg[b.key] = b; });
  (cfg.actions || []).forEach(function (a) { actCfg[a.key] = a; });
  (cfg.edits || []).forEach(function (e) { editCfg[e.key] = e; });
  (cfg.adminActions || []).forEach(function (a) { adminCfg[a.name] = a; });
  var columns = (cfg.columns || []).filter(function (c) { return c && COL.test(c.name); });
  function list(k) { return Array.isArray(prefs[k]) ? prefs[k].slice() : []; }
  function isShared(k) { return SHARED.indexOf(k) >= 0; }
  function barCount() {
    var b = prefs.bar;
    return typeof b === 'number' && isFinite(b) ? Math.max(0, Math.min(BAR_MAX, Math.round(b))) : 4;
  }
  function hideCols() {
    var h = Array.isArray(prefs.hide_cols) ? prefs.hide_cols : (cfg.hideDefault || []);
    return h.filter(function (n) { return typeof n === 'string' && COL.test(n); });
  }
  function viewMode() { return prefs.nav !== 'none' && prefs.view === 'tabs' ? 'tabs' : 'scroll'; }
  function navMode() {
    if (prefs.nav === 'none') return 'none';
    return prefs.nav === 'side' && (S.shown || wantWidth()) >= SIDE_MIN ? 'side' : 'top';
  }

  /* ── стан ── */
  var S = {
    inited: false, waiting: false, open: false, pk: '', row: null, shell: null,
    req: 0, ctrl: null, pre: null, navT: 0, dir: 1,
    sec: '', offset: 0, want: null, shown: 0, top: '', dock: '',
    spyPause: 0, spyIO: null, lazyIO: null, inView: {}, scrollRaf: 0, topRaf: 0, spyRaf: 0, fitRaf: 0,
    hashT: 0, sheet: null, editor: null, edgeDir: 0, edgeAt: 0, noClick: 0, snap: null,
    pinnedEls: [], detached: {}, afterLoad: null, railSig: '', rzT: 0, wT: 0, dragging: false, dockMO: null
  };
  var panel = null, wrap = null, grip = null, live = null, colStyle = null, pill = null, colNote = null;
  var cache = {}, cacheOrder = [];

  /* ── рядки списку ── */
  function rows() {
    var out = [], all = document.querySelectorAll(ROWS);
    for (var i = 0; i < all.length; i++) {
      var r = all[i];
      if (panel && panel.contains(r)) continue;
      if (!r.getClientRects().length) continue;              // сховане (таблиця при картках і навпаки)
      var up = r.parentElement && r.parentElement.closest(ROWS);
      if (up && !(panel && panel.contains(up))) continue;    // вкладене — рахується зовнішній рядок
      out.push(r);
    }
    return out;
  }
  function pkOf(row) {
    if (!row || !row.querySelector) return '';
    var cb = row.querySelector('input.action-select');
    var v = (cb && cb.value) || attr(row, 'data-pk') || attr(row, 'data-mvp-row-pk');
    if (!v) {
      var a = row.querySelector('a[href*="/change/"]'), m = a && /(\d+)\/change\//.exec(a.getAttribute('href') || '');
      v = m ? m[1] : '';
    }
    v = String(v || '').trim();
    return PK.test(v) ? v : '';
  }
  function rowOf(pk) {
    if (!pk) return null;
    var all = rows();
    for (var i = 0; i < all.length; i++) if (pkOf(all[i]) === String(pk)) return all[i];
    return null;
  }
  function tableRowOf(pk) {
    var cb = PK.test(String(pk)) ? document.querySelector('#result_list input.action-select[value="' + pk + '"]') : null;
    return cb ? cb.closest('tr') : null;
  }
  function changeLinkOf(row) { var a = row && row.querySelector('a[href*="/change/"]'); return a ? a.getAttribute('href') : ''; }
  function rowTitle(row) {
    // назва запису: колонка номера / коду (у замовленнях посилання — це ☆ «Важливе», а номер — простий текст),
    // далі колонка-посилання Django (th), далі будь-яке посилання з буквами чи цифрами
    var t = '', WORD = /[0-9A-Za-zА-Яа-яЁёЇїІіЄєҐґ]/;
    function txt(n) { return ((n && n.textContent) || '').replace(/\s+/g, ' ').trim(); }
    if (row) {
      var id = row.querySelector('.field-order_number, .field-number, .field-code, .field-sku');
      if (id && WORD.test(txt(id))) t = txt(id);
      var cands = [].slice.call(row.querySelectorAll('th a[href]')).concat([].slice.call(row.querySelectorAll('a[href]')));
      for (var i = 0; i < cands.length && !t; i++) if (WORD.test(txt(cands[i]))) t = txt(cands[i]);
      if (!t) t = txt(row);
    }
    return t.length > 80 ? t.slice(0, 79) + '…' : t;
  }
  function mark(row) {
    var old = document.querySelectorAll('.mvp-sel');
    for (var i = 0; i < old.length; i++) if (old[i] !== row) old[i].classList.remove('mvp-sel');
    S.row = row || null;
    if (row) {
      row.classList.add('mvp-sel');
      if (!row.hasAttribute('tabindex')) row.setAttribute('tabindex', '-1');
    }
  }
  function labelOf(pk) {
    if (S.shell && attr(S.shell, 'data-pk') === String(pk) && attr(S.shell, 'data-label')) return attr(S.shell, 'data-label');
    return rowTitle(rowOf(pk)) || ('#' + pk);
  }
  function cardHref(pk) {
    var a = changeLinkOf(rowOf(pk));
    if (a) return a;                                         // посилання списку — з _changelist_filters
    var base = S.shell && attr(S.shell, 'data-pk') === String(pk) ? attr(S.shell, 'data-change-url') : '';
    if (!base) return '';
    var q = location.search.replace(/^\?/, '');
    return q ? base + (base.indexOf('?') < 0 ? '?' : '&') + '_changelist_filters=' + encodeURIComponent(q) : base;
  }
  function openCard(newTab) {
    var url = cardHref(S.pk);
    if (!url) return;
    hashUpdate();
    if (newTab) window.open(url, '_blank', 'noopener'); else location.href = url;
  }

  /* ── кеш записів: 6 останніх на 60 с; після змін, вибору людини, дій і R — наново ── */
  function cacheGet(pk) {
    var c = cache[pk];
    if (!c) return null;
    if (now() - c.t > CACHE_TTL) { cacheDrop(pk); return null; }
    return c;
  }
  function cachePut(pk, entry) {
    if (typeof entry === 'string') entry = { t: now(), html: entry };
    cache[pk] = entry;
    var i = cacheOrder.indexOf(pk);
    if (i >= 0) cacheOrder.splice(i, 1);
    cacheOrder.push(pk);
    while (cacheOrder.length > CACHE_MAX) cacheDrop(cacheOrder[0]);
  }
  function cacheDrop(pk) {
    var c = cache[pk];
    if (c && c.ctrl && !c.adopted && c.html == null) { try { c.ctrl.abort(); } catch (e) { /* уже */ } }
    delete cache[pk];
    var i = cacheOrder.indexOf(pk);
    if (i >= 0) cacheOrder.splice(i, 1);
  }
  function cacheClear() { cacheOrder.slice().forEach(cacheDrop); cache = {}; cacheOrder = []; S.pre = null; }

  /* ── запити ── */
  function httpError(status, msg) { var e = new Error(msg || ('HTTP ' + status)); e.status = status; return e; }
  function get(url, signal) {
    return fetch(url, { credentials: 'same-origin', signal: signal || undefined,
                        headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) {
        if (r.redirected) throw httpError(401, 'login');     // сесія скінчилась — сторінка входу
        if (!r.ok) throw httpError(r.status);
        return r.text();
      });
  }
  function fetchShell(pk, sec, signal) {
    var url = cfg.url + pk + '/' + (sec && KEY.test(sec) ? '?open=' + encodeURIComponent(sec) : '');
    return get(url, signal).then(function (html) {
      if (html.indexOf('mvp-shell') < 0) throw httpError(0, 'bad');
      return html;
    });
  }
  function fetchBlock(pk, key) {
    return get(cfg.url + pk + '/block/' + encodeURIComponent(key) + '/').then(function (html) {
      if (html.indexOf('mvp-sec') < 0) throw httpError(0, 'bad');
      return html;
    });
  }
  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', body: JSON.stringify(data),
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(),
                                   'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) {
        return r.text().then(function (t) {
          return { status: r.redirected ? 401 : r.status, data: parseJSON(t, null) || {} };
        });
      });
  }

  /* ── читач екрана ── */
  function announce(parts) {
    if (!live) return;
    live.textContent = '';
    var items = typeof parts === 'string' ? [{ text: parts }] : parts;
    setTimeout(function () {
      items.forEach(function (p, i) {
        if (!p || !p.text) return;
        if (live.childNodes.length) live.appendChild(document.createTextNode(' · '));
        var s = el('span', '', p.text);
        if (p.data) s.setAttribute('translate', 'no');
        live.appendChild(s);
      });
    }, 40);
  }
  function statusText() {
    var h = S.shell && S.shell.querySelector('.mvp-hedit');
    return h ? h.textContent.replace(/▾/g, '').replace(/\s+/g, ' ').trim() : '';
  }
  function posText() {
    var all = rows(), i = S.row ? all.indexOf(S.row) : -1;
    return i < 0 ? '' : fmt('{0} з {1}', i + 1, all.length);
  }
  function announceLoaded() {
    announce([{ text: attr(S.shell, 'data-label'), data: true }, { text: statusText() }, { text: posText() }]);
  }

  /* ── відкрити запис ── */
  function open(pk, o) {
    o = o || {};
    pk = String(pk == null ? '' : pk);
    if (!S.inited || !PK.test(pk) || !wide()) return false;
    if (S.sheet) closeSheet(true);
    if (S.editor) closeEditor(true);
    var row = o.row !== undefined ? o.row : rowOf(pk);
    if (S.open && S.shell) rememberSection();
    var sec = o.sec || (S.open ? S.sec : '') || startSection();
    var offset = o.offset != null ? Math.max(0, +o.offset || 0) : (S.open && sec === S.sec ? S.offset : 0);
    S.want = { sec: sec, offset: offset };
    if (o.dir) S.dir = o.dir;
    var first = !S.open, top0 = first ? anchorTop(row) : null;
    mark(row);
    if (row && o.focus !== false) focusRow(row);
    var again = pk === S.pk && S.shell && attr(S.shell, 'data-pk') === pk && !o.force &&
      !S.shell.classList.contains('mvp-shell-err');            // після помилки клік по рядку — ще раз
    S.pk = pk;
    if (first) showPanel(row, top0);
    if (again) { S.want = null; navButtons(); posUpdate(); return true; }
    clearTimeout(S.navT);
    var hit = cacheGet(pk);
    if (o.delay && !(hit && hit.html != null)) {
      S.req++;
      abortMain();
      setBusy(true);
      navButtons();
      posUpdate();
      S.navT = setTimeout(function () { load(pk, sec); }, o.delay);
    } else load(pk, sec);
    return true;
  }
  function focusRow(row) { if (row && row.isConnected) focusOn(row); }
  function startSection() {
    var st = prefs.start || 'last';
    if (st === 'first') return '';
    if (st === 'last') return sGet('localStorage', 'mvp-sec:' + cfg.model) || '';
    return st;
  }
  function abortMain() { if (S.ctrl) { try { S.ctrl.abort(); } catch (e) { /* уже */ } S.ctrl = null; } }
  function setBusy(on) {
    if (!panel) return;
    panel.classList.toggle('mvp-busy', !!on);
    if (on) panel.setAttribute('aria-busy', 'true'); else panel.removeAttribute('aria-busy');
  }
  function load(pk, sec, fresh) {
    var my = ++S.req;
    abortMain();
    if (fresh) cacheDrop(pk);
    var hit = fresh ? null : cacheGet(pk);
    if (hit && hit.html != null) { inject(my, pk, hit.html); return; }
    setBusy(true);
    var p;
    if (hit && hit.promise) { hit.adopted = true; S.ctrl = hit.ctrl; p = hit.promise; }   // наперед — уже йде
    else {
      var ctrl = window.AbortController ? new AbortController() : null;
      S.ctrl = ctrl;
      p = fetchShell(pk, sec, ctrl && ctrl.signal);
    }
    p.then(function (html) {
      if (my !== S.req) return;
      S.ctrl = null;
      cachePut(pk, html);
      inject(my, pk, html);
    }, function (err) {
      if (my !== S.req || (err && err.name === 'AbortError')) return;
      S.ctrl = null;
      setBusy(false);
      showError(pk, err);
    });
  }
  function prefetchNext() {
    if (navigator.connection && navigator.connection.saveData) return;
    var all = rows(), i = S.row ? all.indexOf(S.row) : -1;
    if (i < 0) return;
    var next = all[i + (S.dir < 0 ? -1 : 1)], pk = pkOf(next);
    if (!pk || pk === S.pk || cacheGet(pk)) return;
    if (S.pre && !S.pre.adopted && S.pre.html == null && S.pre.ctrl) { try { S.pre.ctrl.abort(); } catch (e) { /* уже */ } }
    var ctrl = window.AbortController ? new AbortController() : null;
    var entry = { t: now(), ctrl: ctrl, adopted: false, html: null, promise: null };
    entry.promise = fetchShell(pk, S.sec, ctrl && ctrl.signal).then(function (html) {
      entry.html = html; entry.promise = null; entry.t = now();
      return html;
    }, function (err) {
      if (cache[pk] === entry) cacheDrop(pk);
      throw err;
    });
    entry.promise.catch(function () { /* наперед — не вдалося, то й нехай */ });
    cachePut(pk, entry);
    S.pre = entry;
  }
  function inject(my, pk, html) {
    if (my !== S.req || !S.open) return;
    setBusy(false);
    var keep = S.want || { sec: S.sec, offset: S.offset };
    S.want = null;
    var act = document.activeElement, fk = act && panel.contains(act) && act !== panel ? focusKey(act) : '';
    wrap.innerHTML = html;                                    // оболонка з сервера (Django екранує дані)
    var shell = wrap.querySelector('.mvp-shell');
    if (!shell) { showError(pk, httpError(0, 'bad')); return; }
    S.shell = shell;
    S.detached = {};
    decorate();
    restore(keep);
    if (fk) refocus(fk);
    posUpdate();
    hashUpdate();
    announceLoaded();
    var after = S.afterLoad;
    S.afterLoad = null;
    if (after) after();
    prefetchNext();
  }
  function focusKey(a) {
    var names = ['data-mvp-nav', 'data-act', 'data-edit', 'data-go'];
    for (var i = 0; i < names.length; i++) {
      var v = a.getAttribute && a.getAttribute(names[i]);
      if (v != null && /^[A-Za-z0-9:_-]*$/.test(v)) return '[' + names[i] + '="' + v + '"]';
    }
    var flags = ['data-mvp-open', 'data-mvp-menu', 'data-mvp-close', 'data-mvp-refresh', 'data-mvp-keys', 'data-mvp-more',
                 'data-mvp-pos'];
    for (var j = 0; j < flags.length; j++) if (a.hasAttribute && a.hasAttribute(flags[j])) return '[' + flags[j] + ']';
    return '';
  }
  function refocus(sel) {
    var x = S.shell && S.shell.querySelector(sel);
    if (x && !x.disabled && !x.hidden) focusOn(x); else focusOn(panel);
  }
  function placeholder() {
    wrap.textContent = '';
    var shell = el('div', 'mvp-shell mvp-ph'), head = el('header', 'mvp-head'), row = el('div', 'mvp-hrow');
    row.appendChild(el('div', 'mvp-ph-title'));
    var x = button('mvp-icon', '✕', 'Закрити (Esc)');
    x.setAttribute('data-mvp-close', '');
    row.appendChild(x);
    head.appendChild(row);
    shell.appendChild(head);
    var b = el('div', 'mvp-body'), sk = el('div', 'mvp-skel');
    sk.setAttribute('aria-hidden', 'true');
    b.appendChild(sk);
    shell.appendChild(b);
    wrap.appendChild(shell);
    S.shell = null;
  }
  function showError(pk, err) {
    var st = err && err.status;
    wrap.textContent = '';
    var shell = el('div', 'mvp-shell mvp-shell-err'), head = el('header', 'mvp-head'), row = el('div', 'mvp-hrow');
    shell.setAttribute('data-pk', pk);
    var h = el('h2', 'mvp-title', 'Не вдалося завантажити');
    h.id = 'mvp-title';
    h.tabIndex = -1;
    var again = button('mvp-icon', '↻', 'Спробувати ще (R)');
    again.setAttribute('data-mvp-refresh', '');
    var x = button('mvp-icon', '✕', 'Закрити (Esc)');
    x.setAttribute('data-mvp-close', '');
    row.appendChild(h); row.appendChild(again); row.appendChild(x);
    head.appendChild(row);
    head.appendChild(el('div', 'mvp-sub', st === 404 || st === 403 ? 'Запис не знайдено або він для вас закритий.' :
      st === 401 ? 'Сесія закінчилась — оновіть сторінку.' : 'Перевірте зв’язок і спробуйте ще.'));
    shell.appendChild(head);
    wrap.appendChild(shell);
    S.shell = shell;
    announce('Не вдалося завантажити');
  }
  function refresh() {
    if (!S.open || !S.pk) return;
    rememberSection();
    S.want = { sec: S.sec, offset: S.offset };
    load(S.pk, S.sec, true);
  }

  /* ── показати / сховати панель ── */
  function showPanel(row, top0) {
    if (!S.shell) placeholder();
    var w = wantWidth();
    S.open = true;
    document.body.classList.add('mvp-open');
    // спершу подія: бокове меню (mv_desktop.js) одразу звужується, потім читаємо його ширину; --mvp-w на <body>
    // пише layout() один раз (кожна зміна стилю <body> — нова перевірка контрасту всієї сторінки)
    emit('mv:panel', { open: true, width: w, model: cfg.model, pk: S.pk });
    layout();
    reanchor(row, top0);
    dockWatch();
    colNoteUpdate();
  }
  function close() {
    if (!S.open) return;
    var row = S.row, top0 = anchorTop(row), act = document.activeElement;
    var hadFocus = !!(act && panel.contains(act));
    if (menuOpen()) { try { MVMenu.close(); } catch (e) { /* уже */ } }
    if (S.editor) closeEditor(true);
    if (S.sheet) closeSheet(true);
    rememberSection();
    if (S.sec) sSet('localStorage', 'mvp-sec:' + cfg.model, S.sec);
    S.open = false;
    S.req++;
    abortMain();
    clearTimeout(S.navT);
    clearTimeout(S.hashT);
    setBusy(false);
    disconnect();
    document.body.classList.remove('mvp-open', 'mvp-push', 'mvp-overlay');
    emit('mv:panel', { open: false, width: 0, model: cfg.model, pk: S.pk });
    hashClear();
    wrap.textContent = '';
    S.shell = null;
    S.pk = '';
    mark(null);
    colNoteUpdate();
    reanchor(row, top0);
    act = document.activeElement;
    if (row && (hadFocus || !act || act === document.body || act === row)) focusRow(row);
  }
  function disconnect() {
    if (S.spyIO) { S.spyIO.disconnect(); S.spyIO = null; }
    if (S.lazyIO) { S.lazyIO.disconnect(); S.lazyIO = null; }
  }

  /* ── ширина, місце, список поруч ── */
  function autoWidth() { return Math.max(AUTO_MIN, Math.min(AUTO_MAX, Math.round(eff() * AUTO_SHARE))); }
  function wantWidth() {
    var w = prefs.width;
    return typeof w === 'number' && isFinite(w) && w >= W_MIN && w <= W_MAX ? Math.round(w) : autoWidth();
  }
  function sideW() {
    var v = parseFloat(getComputedStyle(root).getPropertyValue('--mvd-side-w'));
    if (isFinite(v) && v >= 0) {                                    // новий вигляд (mv_desktop.css)
      var sb = document.getElementById('nav-sidebar');
      return sb && sb.getClientRects().length ? v : 0;
    }
    var s = document.querySelector('.main.shifted > #nav-sidebar');   // класичний вигляд Django
    return s && s.getClientRects().length ? s.getBoundingClientRect().width / zoom() : 0;
  }
  function widthCap() { return Math.max(W_MIN, Math.min(W_MAX, Math.floor(eff() - sideW() - LIST_MIN))); }
  function layout() {
    if (!S.open || !panel) return;
    var e = eff(), sw = sideW(), w = wantWidth();
    var shown = Math.max(W_MIN, Math.min(w, Math.floor(e - sw - LIST_MIN)));   // лише показ, не зберігається
    S.shown = shown;
    setVar(document.body, '--mvp-w', shown + 'px');
    var push;
    if (e < OVERLAY_EFF) push = false;
    else if (prefs.place === 'push') push = true;
    else if (prefs.place === 'overlay') push = false;
    else push = e - sw - shown - 48 >= PUSH_LIST;
    var b = document.body.classList, beside = prefs.helpers === 'beside';
    b.toggle('mvp-push', push);
    b.toggle('mvp-overlay', !push);
    b.toggle('mvp-dock', !beside);
    b.toggle('mvp-beside', beside);
    if (grip) {
      grip.setAttribute('aria-valuemax', String(widthCap()));
      grip.setAttribute('aria-valuenow', String(shown));
      grip.setAttribute('aria-valuetext', fmt('{0} px', shown));
    }
    applyLook();
    var side = navMode() === 'side';
    if (S.shell && S.shell.classList.contains('mvp-nav-side') !== side) {
      S.shell.classList.toggle('mvp-nav-side', side);
      buildNav();
    }
    topUpdate();
    dockUpdate();
    fitSoon();
  }
  function anchorTop(row) { return row && row.isConnected ? row.getBoundingClientRect().top : null; }
  function reanchor(row, top0) {                    // вибраний рядок лишається там, де був
    if (top0 == null || !row) return;
    function fix() {
      if (!row.isConnected) return;
      var d = row.getBoundingClientRect().top - top0;
      if (Math.abs(d) > 1) window.scrollBy(0, d);
    }
    requestAnimationFrame(fix);
    setTimeout(fix, 220);
  }
  function relayout(change) {                       // зміна ширини / місця / меню ліворуч — з прив'язкою рядка
    var row = S.row, top0 = anchorTop(row);
    if (change) change();
    layout();
    reanchor(row, top0);
  }
  function topUpdate() {                            // --mvp-top — лише на самій панелі (не на <html> / <body>)
    if (!panel) return;
    var h = document.getElementById('header');
    var v = Math.round((h ? Math.max(0, h.getBoundingClientRect().bottom) : 0) / zoom()) + 'px';
    if (v !== S.top) { S.top = v; setVar(panel, '--mvp-top', v); }
  }
  function dockUpdate() {                           // місце в низу панелі під помічників, які зараз видно
    if (!panel) return;
    var n = 0;
    ['mv-fab', 'rg-pill'].forEach(function (id) {
      var x = document.getElementById(id);
      if (x && getComputedStyle(x).display !== 'none') n += 1;
    });
    var v = (46 * n + 8) + 'px';
    if (v !== S.dock) { S.dock = v; setVar(panel, '--mvp-dock', v); }
  }
  function dockWatch() {
    if (S.dockMO || !window.MutationObserver) return;
    S.dockMO = new MutationObserver(function () { if (S.open) dockUpdate(); });
    ['mv-fab', 'rg-pill'].forEach(function (id) {
      var x = document.getElementById(id);
      if (x) S.dockMO.observe(x, { attributes: true, attributeFilter: ['style', 'class', 'hidden'] });
    });
  }
  function fitSoon() {
    if (S.fitRaf) return;
    S.fitRaf = requestAnimationFrame(function () { S.fitRaf = 0; fitNav(); fitBar(); });
  }
  function applyLook() {
    if (!panel) return;
    var c = panel.classList, nav = navMode();
    ['top', 'side', 'none'].forEach(function (v) { c.toggle('mvp-nav-' + v, nav === v); });
    c.toggle('mvp-view-tabs', viewMode() === 'tabs');
    c.toggle('mvp-view-scroll', viewMode() !== 'tabs');
    c.toggle('mvp-d-compact', prefs.density === 'compact');
    c.toggle('mvp-empty-compact', prefs.empty !== 'show');
    c.toggle('mvp-empty-show', prefs.empty === 'show');
  }
  function previewWidth(w) {                        // під час перетягування / повзунка — без збереження
    var shown = Math.max(W_MIN, Math.min(w, widthCap()));
    S.shown = shown;
    setVar(document.body, '--mvp-w', shown + 'px');
    if (grip) { grip.setAttribute('aria-valuenow', String(shown)); grip.setAttribute('aria-valuetext', fmt('{0} px', shown)); }
    return shown;
  }
  function setWidth(w) {                            // null — авто (28 % екрана)
    var v = w == null ? null : Math.max(W_MIN, Math.min(W_MAX, Math.round(w)));
    if (v === prefs.width || (v == null && prefs.width == null)) { relayout(); return; }
    change({ width: v });
  }

  /* ── колонки списку, поки панель відкрита ── */
  function colExists(n) { return !columns.length || columns.some(function (c) { return c.name === n; }); }
  function hiddenCount() { return hideCols().filter(colExists).length; }
  function applyCols() {
    if (!colStyle) return;
    var css = hideCols().map(function (n) {
      return 'body.mvp-open #result_list .column-' + n + ', body.mvp-open #result_list .field-' + n;
    });
    colStyle.textContent = css.length ? css.join(',\n') + ' { display: none !important; }' : '';
    colNoteUpdate();
  }
  function colNoteUpdate() {
    if (!colNote) return;
    var n = hiddenCount(), tbl = document.getElementById('result_list');
    var on = S.open && n > 0 && !!tbl && tbl.getClientRects().length > 0;
    colNote.hidden = !on;
    if (on) colNote.firstChild.textContent = fmt('🙈 Сховано колонок: {0} — вони є в панелі', n);
  }
  function colName(th) { var m = /(?:^|\s)column-([a-z_][a-z0-9_]*)(?:\s|$)/.exec(th.className || ''); return m ? m[1] : ''; }
  function colLabel(n) {
    var c = columns.filter(function (x) { return x.name === n; })[0];
    return c ? c.label : n;
  }
  function setColHidden(name, on) {
    var h = hideCols(), i = h.indexOf(name);
    if (on && i < 0) h.push(name); else if (!on && i >= 0) h.splice(i, 1); else return;
    change({ hide_cols: h });
  }

  /* ── прикрасити оболонку з сервера ── */
  function bodyEl() { return S.shell ? S.shell.querySelector(':scope > .mvp-body') : null; }
  function navEl() { return S.shell ? S.shell.querySelector(':scope > .mvp-nav') : null; }
  function actbar() { return S.shell ? S.shell.querySelector('.mvp-actbar') : null; }
  function secs() { var b = bodyEl(); return b ? [].slice.call(b.querySelectorAll(':scope > .mvp-sec')) : []; }
  function secKey(s) { return attr(s, 'data-sec'); }
  function secFor(key) {
    if (!key || !S.shell || !KEY.test(key)) return null;
    var s = document.getElementById('mvp-sec-' + key);
    return s && S.shell.contains(s) ? s : null;
  }
  function decorate() {
    if (!S.shell) return;
    S.shell.classList.toggle('mvp-nav-side', navMode() === 'side');
    decorateHead();
    secs().forEach(decorateSec);
    placeActbar();
    buildNav();
    var b = bodyEl();
    if (b) b.addEventListener('scroll', onBodyScroll, { passive: true });
    observeSpy();
    observeLazy();
  }
  function decorateHead() {
    var head = S.shell && S.shell.querySelector('.mvp-head');
    if (!head) return;
    var h = head.querySelector('.mvp-title'), data = h && (h.querySelector('[translate="no"]') || h);
    if (data) data.title = h.textContent.replace(/\s+/g, ' ').trim();
    var a = head.querySelector('a[data-mvp-open]'), u = cardHref(S.pk);
    if (a && u) a.setAttribute('href', u);
    navButtons();
    layoutBar();
  }
  function decorateSec(sec) {
    var b = sec.querySelector('.mvp-sh-btn');
    if (b && !b.querySelector('.mvp-snone')) b.insertBefore(el('span', 'mvp-snone', '— немає'), b.querySelector('.mvp-chev'));
  }
  function navButtons() {
    if (!S.shell) return;
    var all = rows(), i = S.row ? all.indexOf(S.row) : -1;
    var prev = S.shell.querySelector('[data-mvp-nav="-1"]'), next = S.shell.querySelector('[data-mvp-nav="1"]');
    if (prev) prev.disabled = i <= 0;
    if (next) next.disabled = i < 0 || i >= all.length - 1;
  }
  function posUpdate() {
    var b = S.shell && S.shell.querySelector('[data-mvp-pos]');
    if (!b) return;
    var t = posText();
    b.hidden = !t;
    if (t) { b.textContent = t; b.title = 'Показати рядок у списку'; }
  }
  function placeActbar() {
    var sh = S.shell, bar = actbar();
    if (!sh || !bar) return;
    var head = sh.querySelector(':scope > .mvp-head'), foot = sh.querySelector(':scope > .mvp-foot');
    if (prefs.actions_at === 'bottom') {
      if (bar.parentNode !== sh) sh.insertBefore(bar, foot || null);
      bar.classList.add('mvp-actbar-bottom');
    } else if (bar.parentNode === sh && head) {
      head.insertBefore(bar, head.querySelector(':scope > .mvp-memo'));
      bar.classList.remove('mvp-actbar-bottom');
    }
    fitSoon();
  }
  function replaceHead(html) {
    var old = S.shell && S.shell.querySelector(':scope > .mvp-head'), fresh = parse(html, 'header.mvp-head');
    if (!old || !fresh) return;
    var moved = S.shell.querySelector(':scope > .mvp-actbar');
    if (moved) moved.remove();
    old.parentNode.replaceChild(fresh, old);
    decorateHead();
    placeActbar();
    posUpdate();
  }

  /* ── рядок швидких дій ── */
  function barEls() { var b = actbar(); return b ? [].slice.call(b.querySelectorAll(':scope > [data-act]')) : []; }
  function moreBtn() { var b = actbar(); return b ? b.querySelector(':scope > [data-mvp-more]') : null; }
  function actKey(it) { return attr(it, 'data-act'); }
  function splitLabel(it) {
    if (it._mvpSplit) return;
    it._mvpSplit = true;
    var text = (it.textContent || '').replace(/\s+/g, ' ').trim();
    it._mvpLabel = attr(it, 'aria-label') || text;
    if (it.classList.contains('mvp-tog') || it.hasAttribute('data-mvp-more')) return;
    var m = LEAD.exec(text);
    it.textContent = '';
    if (m) {
      it._mvpIcon = m[1];
      var i = el('span', 'mvp-ai', m[1]);
      i.setAttribute('aria-hidden', 'true');
      it.appendChild(i);
      it.appendChild(el('span', 'mvp-al', m[2]));
    } else {
      it.classList.add('no-icon');
      it.appendChild(el('span', 'mvp-al', text));
    }
  }
  function layoutBar() {
    var bar = actbar(), more = moreBtn();
    if (!bar) return;
    var order = list('actions'), items = barEls();
    items.forEach(splitLabel);
    var pinned = items.filter(function (it) { return order.indexOf(actKey(it)) >= 0; })
      .sort(function (a, b) { return order.indexOf(actKey(a)) - order.indexOf(actKey(b)); });
    var rest = items.filter(function (it) { return order.indexOf(actKey(it)) < 0; });
    pinned.concat(rest).forEach(function (it) { bar.insertBefore(it, more || null); });
    var n = barCount(), primary = false;
    pinned.forEach(function (it, i) {
      var on = i < n;
      it.hidden = !on;
      it._mvpFit = false;
      var pri = on && !primary && !it.classList.contains('mvp-tog') && !it.classList.contains('is-off') &&
        !it.hasAttribute('data-edit');
      it.classList.toggle('is-primary', pri);
      if (pri) primary = true;
    });
    rest.forEach(function (it) { it.hidden = true; it._mvpFit = false; it.classList.remove('is-primary'); });
    S.pinnedEls = pinned;
    fitBar();
  }
  function overflows(box) { return box.scrollWidth > box.clientWidth + 1; }
  function fitBar() {                               // не переноситься: спершу лише значки, потім — у «⋯»
    var bar = actbar();
    if (!bar || !S.open || !bar.getClientRects().length) return;
    var pinned = S.pinnedEls.filter(function (it) { return it.isConnected; });
    pinned.forEach(function (it) { if (it._mvpFit) { it._mvpFit = false; it.hidden = false; } });
    bar.classList.remove('mvp-icons');
    if (!overflows(bar)) return;
    bar.classList.add('mvp-icons');
    var vis = pinned.filter(function (it) { return !it.hidden; });
    for (var i = vis.length - 1; i > 0 && overflows(bar); i--) { vis[i].hidden = true; vis[i]._mvpFit = true; }
  }
  function overflowEls() { return S.pinnedEls.filter(function (it) { return it.isConnected && it.hidden; }); }
  function itemLabel(it) {
    var lab = it._mvpLabel || it.textContent.replace(/\s+/g, ' ').trim(), m = LEAD.exec(lab);
    return m ? { icon: m[1], label: m[2] } : { icon: it._mvpIcon || '', label: lab };
  }
  function itemFromEl(it) {                         // кнопка рядка дій → пункт меню «⋯»
    var l = itemLabel(it), t = { icon: l.icon, label: l.label, title: it.title || '' };
    if (it.classList.contains('is-off')) { t.disabled = true; return t; }
    if (it.hasAttribute('data-edit')) {
      if (attr(it, 'data-kind') === 'bool') {
        return { type: 'check', label: l.label, checked: attr(it, 'data-value') === '1', close: true,
                 onToggle: function () { editClick(it); } };
      }
      t.onSelect = function () { setTimeout(function () { editClick(it); }, 0); };
      return t;
    }
    if (it.hasAttribute('data-admin-action')) {
      t.onSelect = function () { runAdmin(attr(it, 'data-admin-action'), attr(it, 'data-confirm')); };
      return t;
    }
    if (it.hasAttribute('data-svl')) { t.onSelect = function () { it.click(); }; return t; }
    if (it.tagName === 'A' && it.getAttribute('href')) {
      t.href = it.getAttribute('href');
      if (it.target === '_blank') t.target = '_blank'; else t.onSelect = hashUpdate;   // «Назад» — знову панель
      return t;
    }
    t.disabled = true;
    return t;
  }

  /* ── підменю розділів ── */
  function chips() { var n = navEl(); return n ? [].slice.call(n.querySelectorAll('.mvp-ch')) : []; }
  function chipFor(key) {
    var n = navEl();
    return n && key && KEY.test(key) ? n.querySelector('.mvp-ch[data-go="' + key + '"]') : null;
  }
  function buildNav() {
    var nav = navEl();
    if (!nav) return;
    var mode = navMode(), tabs = viewMode() === 'tabs', all = secs(), act = document.activeElement;
    var fk = nav.contains(act) ? (attr(act, 'data-go') || (act.classList.contains('mvp-ch-more') ? '*' : '')) : '';
    nav.textContent = '';
    nav.hidden = mode === 'none' || !all.length;
    nav.setAttribute('aria-label', 'Розділи запису');
    if (tabs) {
      nav.setAttribute('role', 'tablist');
      nav.setAttribute('aria-orientation', mode === 'side' ? 'vertical' : 'horizontal');
    } else { nav.removeAttribute('role'); nav.removeAttribute('aria-orientation'); }
    all.forEach(function (sec, i) {
      var key = secKey(sec), title = attr(sec, 'data-title'), count = attr(sec, 'data-count'), tone = attr(sec, 'data-tone');
      var na = sec.hasAttribute('data-na'), empty = sec.hasAttribute('data-empty');
      var c = button('mvp-ch');
      c.tabIndex = -1;
      c.id = 'mvp-ch-' + key;
      c.setAttribute('data-go', key);
      if (/^(ok|warn|err|info)$/.test(tone)) c.classList.add('t-' + tone);
      if (na) c.classList.add('is-na');
      if (empty) c.classList.add('is-empty');
      var ic = el('span', 'mvp-ci', attr(sec, 'data-icon') || '•');
      ic.setAttribute('aria-hidden', 'true');
      c.appendChild(ic);
      c.appendChild(el('span', 'mvp-cl', attr(sec, 'data-short') || title));
      c.appendChild(el('span', 'mvp-cn', count));
      var aria = [title];
      if (count) aria.push(count);
      if (attr(sec, 'data-badge')) aria.push(attr(sec, 'data-badge'));
      if (na) aria.push('не стосується'); else if (empty) aria.push('немає');
      c.setAttribute('aria-label', aria.join(', '));
      var tip = i < 9 ? title + ' · ' + (i + 1) : title, brief = attr(sec, 'data-brief');
      c.title = brief ? tip + '\n' + brief : tip;
      if (tabs) {
        c.setAttribute('role', 'tab');
        c.setAttribute('aria-selected', 'false');
        c.setAttribute('aria-controls', sec.id);
        sec.setAttribute('role', 'tabpanel');
        sec.setAttribute('aria-labelledby', c.id);
      } else { sec.removeAttribute('role'); sec.removeAttribute('aria-labelledby'); }
      nav.appendChild(c);
    });
    var more = button('mvp-ch mvp-ch-more', '⋯', 'Ще розділи');
    more.tabIndex = -1;
    more.hidden = true;
    more.setAttribute('aria-haspopup', 'menu');
    nav.appendChild(more);
    if (!S.sec || !secFor(S.sec)) S.sec = all.length ? secKey(all[0]) : '';
    setActive(S.sec, { quiet: true });
    fitNav();
    if (fk) {
      var f = fk === '*' ? more : chipFor(fk);
      if (f && !f.hidden) focusOn(f);
    }
  }
  function fitNav() {                               // «Авто»: не вміщується — лише значки, далі — «⋯» у кінці
    var nav = navEl();
    if (!nav || nav.hidden || !S.open) return;
    var list_ = chips(), more = nav.querySelector('.mvp-ch-more');
    if (!more) return;
    list_.forEach(function (c) { if (c !== more) c.hidden = false; });
    more.hidden = true;
    if (navMode() !== 'top') { nav.classList.remove('mvp-nav-icons'); return; }
    nav.classList.toggle('mvp-nav-icons', prefs.chips === 'icons');
    if (!overflows(nav)) return;
    if (prefs.chips !== 'labels' && !nav.classList.contains('mvp-nav-icons')) {
      nav.classList.add('mvp-nav-icons');
      if (!overflows(nav)) return;
    }
    more.hidden = false;
    for (var i = list_.length - 1; i >= 0 && overflows(nav); i--) {
      var c = list_[i];
      if (c === more || c.classList.contains('is-active')) continue;
      c.hidden = true;
    }
    rove();
  }
  function rove() {                                 // одна фішка з tabindex=0: активна (або перша видима)
    var all = chips().filter(function (c) { return !c.hidden; });
    var on = all.filter(function (c) { return c.classList.contains('is-active'); })[0] || all[0];
    all.forEach(function (c) { c.tabIndex = c === on ? 0 : -1; });
  }
  function setActive(key, o) {
    o = o || {};
    if (!key) return;
    var changed = key !== S.sec, tabs = viewMode() === 'tabs';
    S.sec = key;
    chips().forEach(function (c) {
      if (c.classList.contains('mvp-ch-more')) return;
      var on = attr(c, 'data-go') === key;
      c.classList.toggle('is-active', on);
      if (on) c.setAttribute('aria-current', 'true'); else c.removeAttribute('aria-current');
      if (tabs) c.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    secs().forEach(function (s) { s.classList.toggle('is-active', secKey(s) === key); });
    var chip = chipFor(key);
    if (chip && chip.hidden) fitNav(); else rove();
    if (chip && navMode() === 'side') {             // збоку: активну фішку видно в стовпчику
      var nav = navEl(), top = chip.offsetTop, bottom = top + chip.offsetHeight;
      if (top < nav.scrollTop) nav.scrollTop = top - 4;
      else if (bottom > nav.scrollTop + nav.clientHeight) nav.scrollTop = bottom - nav.clientHeight + 4;
    }
    if (!o.quiet && changed) hashSoon();
  }
  function chipClick(c) {
    if (now() < S.noClick) return;
    if (c.classList.contains('mvp-ch-more')) { navMoreMenu(c); return; }
    goSec(attr(c, 'data-go'), { focus: true });
  }
  function scrollBody(top, smooth) {                // плавно — не довше 200 мс; «менше анімацій» — одразу
    var b = bodyEl();
    if (!b) return;
    top = Math.max(0, Math.min(top, b.scrollHeight - b.clientHeight));
    cancelAnimationFrame(S.scrollRaf);
    if (!smooth || reduced()) { b.scrollTop = top; return; }
    var from = b.scrollTop, d = top - from, t0 = performance.now(), dur = Math.min(200, 80 + Math.abs(d) / 6);
    if (Math.abs(d) < 2) { b.scrollTop = top; return; }
    function step(t) {
      var k = Math.min(1, (t - t0) / dur);
      b.scrollTop = from + d * (1 - Math.pow(1 - k, 3));
      if (k < 1) S.scrollRaf = requestAnimationFrame(step);
    }
    S.scrollRaf = requestAnimationFrame(step);
  }
  function secTop(sec) { return sec.previousElementSibling ? sec.offsetTop : 0; }
  function goSec(key, o) {
    o = o || {};
    var sec = secFor(key), b = bodyEl();
    if (!sec || !b) return;
    S.spyPause = now() + SPY_PAUSE;
    setActive(key);
    if (viewMode() === 'tabs') {
      b.scrollTop = 0;
      S.offset = 0;
      ensureLoaded(sec);
    } else {
      if (sec.hasAttribute('data-collapsed')) setCollapsed(sec, false);   // лише для цього запису
      ensureLoaded(sec);
      scrollBody(secTop(sec), !o.instant);
      if (o.focus) focusOn(sec.querySelector('.mvp-sh-btn'));
    }
  }
  function stepSec(d) {
    var all = secs(), i = all.map(secKey).indexOf(S.sec), j = i + d;
    if (j >= 0 && j < all.length) goSec(secKey(all[j]));
  }
  function goNth(n) { var all = secs(); if (all[n - 1]) goSec(secKey(all[n - 1])); }
  function intoSection(chip) {
    var key = attr(chip, 'data-go'), sec = secFor(key);
    if (!sec) return;
    goSec(key, { instant: true });
    var sb = sec.querySelector('.mvp-sb'), first = sb && sb.querySelector(FOCUSABLE);
    focusOn(first || (viewMode() === 'tabs' ? bodyEl() : sec.querySelector('.mvp-sh-btn')));
  }
  function onNavKey(e) {
    var chip = e.target && e.target.closest && e.target.closest('.mvp-nav .mvp-ch');
    if (!chip || e.altKey || e.ctrlKey || e.metaKey) return;
    var side = navMode() === 'side', all = chips().filter(function (c) { return !c.hidden; });
    var i = all.indexOf(chip), n = all.length, k = e.key;
    var prevK = side ? 'ArrowUp' : 'ArrowLeft', nextK = side ? 'ArrowDown' : 'ArrowRight', intoK = side ? 'ArrowRight' : 'ArrowDown';
    function to(c) { if (!c) return; all.forEach(function (x) { x.tabIndex = x === c ? 0 : -1; }); focusOn(c); }
    if (k === nextK) to(all[(i + 1) % n]);
    else if (k === prevK) to(all[(i - 1 + n) % n]);
    else if (k === 'Home') to(all[0]);
    else if (k === 'End') to(all[n - 1]);
    else if (k === intoK && !chip.classList.contains('mvp-ch-more')) intoSection(chip);
    else if (k === 'Enter' || k === ' ' || k === 'Spacebar') { e.stopPropagation(); return; }   // клік — сам браузер
    else return;
    e.preventDefault();
    e.stopPropagation();
  }

  /* ── прокрутка: який розділ зараз видно ── */
  function observeSpy() {
    if (S.spyIO) { S.spyIO.disconnect(); S.spyIO = null; }
    S.inView = {};
    var b = bodyEl();
    if (!b || viewMode() === 'tabs' || !('IntersectionObserver' in window)) return;
    S.spyIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { S.inView[secKey(en.target)] = en.isIntersecting; });
      spy();
    }, { root: b, rootMargin: '0px 0px -70% 0px', threshold: 0 });
    secs().forEach(function (s) { S.spyIO.observe(s); });
  }
  function spy() {
    if (!S.open || viewMode() === 'tabs' || now() < S.spyPause || S.sheet) return;
    var b = bodyEl(), all = secs();
    if (!b || !all.length) return;
    var key = '';
    if (b.scrollTop > 0 && b.scrollTop + b.clientHeight >= b.scrollHeight - 2) key = secKey(all[all.length - 1]);
    else {
      for (var i = 0; i < all.length; i++) if (S.inView[secKey(all[i])]) { key = secKey(all[i]); break; }
    }
    if (key && key !== S.sec) setActive(key);
  }
  function onBodyScroll() {
    if (S.spyRaf) return;
    S.spyRaf = requestAnimationFrame(function () { S.spyRaf = 0; spy(); });
  }
  function rememberSection() {
    var b = bodyEl(), sec = secFor(S.sec);
    if (!b) return;
    if (viewMode() === 'tabs' || !sec) { S.offset = viewMode() === 'tabs' ? b.scrollTop : 0; return; }
    S.offset = Math.max(0, Math.round(b.scrollTop - secTop(sec)));
  }
  function restore(want) {                          // той самий розділ і місце в ньому, що в попереднього запису
    var b = bodyEl(), all = secs();
    if (!b) return;
    var key = want && want.sec, sec = secFor(key), off = Math.max(0, (want && want.offset) || 0);
    if (!sec) { sec = all[0] || null; off = 0; }
    if (!sec) return;
    key = secKey(sec);
    S.spyPause = now() + SPY_PAUSE;
    setActive(key, { quiet: true });
    if (viewMode() === 'tabs') {
      b.scrollTop = off;
      ensureLoaded(sec);
    } else {
      b.scrollTop = secTop(sec) + Math.min(off, Math.max(0, sec.offsetHeight - 40));
    }
    S.offset = off;
  }

  /* ── розділи: згорнути, вміст окремим запитом ── */
  function setCollapsed(sec, on) {
    if (on) sec.setAttribute('data-collapsed', ''); else sec.removeAttribute('data-collapsed');
    var b = sec.querySelector('.mvp-sh-btn');
    if (b) b.setAttribute('aria-expanded', on ? 'false' : 'true');
  }
  function toggleSec(sec, save) {
    if (!sec) return;
    var key = secKey(sec), on = !sec.hasAttribute('data-collapsed');
    setCollapsed(sec, on);
    if (!on) ensureLoaded(sec);
    if (save) {
      var c = list('collapsed').filter(function (k) { return k !== key; });
      if (on) c.push(key);
      change({ collapsed: c }, { keepDom: true });
    }
  }
  function observeLazy() {
    if (S.lazyIO) { S.lazyIO.disconnect(); S.lazyIO = null; }
    var all = secs(), b = bodyEl();
    if (!b) return;
    if (viewMode() === 'tabs') { var a = secFor(S.sec); if (a) ensureLoaded(a); return; }
    var wait = all.filter(function (s) { return s.hasAttribute('data-deferred') && !s.hasAttribute('data-collapsed'); });
    if (!wait.length) return;
    if (!('IntersectionObserver' in window)) { wait.forEach(function (s) { ensureLoaded(s); }); return; }
    S.lazyIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting || en.target.hasAttribute('data-collapsed')) return;
        if (S.lazyIO) S.lazyIO.unobserve(en.target);
        ensureLoaded(en.target);
      });
    }, { root: b, rootMargin: '200px 0px' });
    wait.forEach(function (s) { S.lazyIO.observe(s); });
  }
  function ensureLoaded(sec, force) {
    if (!sec || !sec.isConnected) return Promise.resolve(null);
    if (!force && !sec.hasAttribute('data-deferred')) return Promise.resolve(sec);
    if (sec._mvpLoading) return sec._mvpLoading;
    var key = secKey(sec), pk = S.pk;
    sec.setAttribute('aria-busy', 'true');
    var p = fetchBlock(pk, key).then(function (html) {
      if (pk !== S.pk || !sec.isConnected) return null;
      return replaceSection(sec, html);
    }).catch(function () {
      if (pk === S.pk && sec.isConnected) showSecError(sec);
      return null;
    }).then(function (fresh) {
      sec._mvpLoading = null;
      sec.removeAttribute('aria-busy');
      return fresh;
    });
    sec._mvpLoading = p;
    return p;
  }
  function replaceSection(old, html, o) {
    o = o || {};
    var fresh = parse(html, 'section.mvp-sec');
    if (!fresh || !old.parentNode) return null;
    var b = bodyEl(), h0 = old.offsetHeight, above = !!b && old.offsetTop + h0 <= b.scrollTop;
    var collapsed = o.collapsed != null ? o.collapsed : old.hasAttribute('data-collapsed');
    setCollapsed(fresh, collapsed);                 // згорнутий / розгорнутий — як зараз на сторінці
    if (old.classList.contains('is-active')) fresh.classList.add('is-active');
    old.parentNode.replaceChild(fresh, old);
    decorateSec(fresh);
    if (above && b) b.scrollTop += fresh.offsetHeight - h0;
    if (!o.quiet) { buildNav(); observeSpy(); observeLazy(); }
    return fresh;
  }
  function showSecError(sec) {
    var sb = sec.querySelector('.mvp-sb');
    if (!sb) return;
    sec.removeAttribute('data-deferred');
    sec.setAttribute('data-error', '');
    sb.textContent = '';
    var box = el('div', 'mvp-empty mvp-err', 'Не вдалося показати цей блок. ');
    var r = button('', '↻ Спробувати ще');
    r.setAttribute('data-mvp-retry', secKey(sec));
    box.appendChild(r);
    sb.appendChild(box);
  }
  function newSection(key) {                        // місце для розділу, який людина щойно додала
    var b = blockCfg[key] || { title: key, short: key, icon: '•' };
    var s = el('section', 'mvp-sec');
    s.id = 'mvp-sec-' + key;
    s.setAttribute('data-sec', key);
    s.setAttribute('data-title', b.title);
    s.setAttribute('data-short', b.short || b.title);
    s.setAttribute('data-icon', b.icon || '•');
    s.setAttribute('data-deferred', '');
    var h = el('h3', 'mvp-sh'), hb = button('mvp-sh-btn');
    h.id = 'mvp-sh-' + key;
    hb.setAttribute('aria-expanded', 'true');
    hb.setAttribute('aria-controls', 'mvp-sb-' + key);
    var si = el('span', 'mvp-si', b.icon || '•');
    si.setAttribute('aria-hidden', 'true');
    hb.appendChild(si);
    hb.appendChild(el('span', 'mvp-st', b.title));
    hb.appendChild(el('span', 'mvp-sc'));
    var chev = el('span', 'mvp-chev');
    chev.setAttribute('aria-hidden', 'true');
    hb.appendChild(chev);
    h.appendChild(hb);
    s.appendChild(h);
    var sb = el('div', 'mvp-sb');
    sb.id = 'mvp-sb-' + key;
    sb.setAttribute('role', 'region');
    sb.setAttribute('aria-labelledby', 'mvp-sh-' + key);
    var sk = el('div', 'mvp-skel');
    sk.setAttribute('aria-hidden', 'true');
    sb.appendChild(sk);
    s.appendChild(sb);
    return s;
  }
  function syncSections() {                         // порядок, сховані й додані — на сторінці, без перезавантаження
    var b = bodyEl();
    if (!b) return;
    var want = list('blocks').filter(function (k) { return blockCfg[k] && KEY.test(k); });
    secs().forEach(function (s) {
      var k = secKey(s);
      if (want.indexOf(k) < 0) { S.detached[k] = s; s.remove(); }
    });
    var missing = want.filter(function (k) { return !secFor(k) && !S.detached[k]; });
    if (missing.length > 1) { reloadKeep(); return; }   // багато нового — один запит на весь запис
    var notice = b.querySelector(':scope > .mvp-empty');
    var prev = null;
    want.forEach(function (k) {
      var s = secFor(k) || S.detached[k], add = false;
      if (!s) { s = newSection(k); add = true; }
      delete S.detached[k];
      var at = prev ? prev.nextSibling : b.firstChild;
      if (s !== at) b.insertBefore(s, at);
      if (add) { decorateSec(s); setCollapsed(s, list('collapsed').indexOf(k) >= 0); ensureLoaded(s, true); }
      prev = s;
    });
    if (want.length && notice) notice.remove();
    if (!want.length && !notice) b.appendChild(el('div', 'mvp-empty', 'Розділів немає — ⚙ або правий клік → «📑 Розділи».'));
  }
  function syncCollapsed() {
    var c = list('collapsed');
    secs().forEach(function (s) { setCollapsed(s, c.indexOf(secKey(s)) >= 0); });
  }
  function reloadKeep() {
    if (!S.open || !S.pk) return;
    rememberSection();
    S.want = { sec: S.sec, offset: S.offset };
    load(S.pk, S.sec, true);
  }

  /* ── у розділах: копіювати, розгорнути текст, знайти рядок ── */
  function copyText(text) {
    text = String(text || '');
    if (!text) return;
    function done() { toast('Скопійовано'); }
    function fallback() {
      var act = document.activeElement, ta = el('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      focusOn(act);
      if (ok) done(); else toast('Не вдалося скопіювати');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }
  function filterRows(input) {
    var scope = input.closest('.mvp-sec') || input.parentElement, q = (input.value || '').trim().toLowerCase();
    if (!scope) return;
    var hits = 0, all = scope.querySelectorAll('[data-mvp-row]');
    for (var i = 0; i < all.length; i++) {
      var on = !q || all[i].textContent.toLowerCase().indexOf(q) >= 0;
      all[i].hidden = !on;
      if (on) hits += 1;
      // знайшлося в згорнутому «Порожні поля (N)» — відкрити, поки шукають; пошук стерли — закрити, як було
      var det = all[i].closest('details');
      if (det && scope.contains(det)) {
        if (q && on && !det.open) { det.open = true; det._mvpOpened = true; }
        else if (!q && det._mvpOpened) { det.open = false; det._mvpOpened = false; }
      }
    }
    var none = scope.querySelector('.mvp-nofind');
    if (q && !hits && all.length) {
      if (!none) { none = el('div', 'mvp-nofind', 'Нічого не знайшлося'); input.insertAdjacentElement('afterend', none); }
      none.hidden = false;
    } else if (none) none.hidden = true;
  }

  /* ── швидкі зміни ── */
  function editEl(key) { return S.shell && key ? S.shell.querySelector('[data-edit="' + key + '"]') : null; }
  function editLabel(key) { return (editCfg[key] && editCfg[key].label) || ''; }
  function editClick(x) {
    var kind = attr(x, 'data-kind'), key = attr(x, 'data-edit');
    if (!key || x.getAttribute('aria-busy') === 'true') return;
    if (switching()) { waitToast(); return; }
    if (kind === 'choice') { choiceMenu(x); return; }
    if (kind === 'bool') {
      var was = attr(x, 'data-value');
      flipToggle(x, !was);
      postEdit(key, !was, was, { el: x, toggle: true });
      return;
    }
    openEditor(x);
  }
  function flipToggle(x, on) {
    x.setAttribute('data-value', on ? '1' : '');
    x.setAttribute('aria-pressed', on ? 'true' : 'false');
    var icon = on ? attr(x, 'data-on') : attr(x, 'data-off');
    if (icon) x.textContent = icon;
  }
  function choices(x) { var c = parseJSON(attr(x, 'data-choices'), []); return Array.isArray(c) ? c : []; }
  function choiceItems(x, close) {
    var key = attr(x, 'data-edit'), cur = attr(x, 'data-value'), conf = parseJSON(attr(x, 'data-confirm'), {}) || {};
    return choices(x).map(function (c) {
      var v = String(c[0]);
      return { type: 'radio', group: 'edit:' + key, label: String(c[1]), checked: v === cur, close: close !== false,
               onSelect: function () {
                 setTimeout(function () {                        // після меню: спершу закрити, потім confirm
                   if (v === attr(x, 'data-value')) return;
                   if (conf[v] && !window.confirm(conf[v])) return;
                   postEdit(key, v, attr(x, 'data-value'), { el: x });
                 }, 0);
               } };
    });
  }
  function choiceMenu(x) {
    if (!window.MVMenu) return;
    menu(x, choiceItems(x), { title: editLabel(attr(x, 'data-edit')) || 'Змінити', focus: 'checked', side: 'bottom-end' });
  }
  function openEditor(x) {
    if (S.editor) closeEditor(true);
    var kind = attr(x, 'data-kind'), key = attr(x, 'data-edit'), cur = attr(x, 'data-value');
    var ml = parseInt(attr(x, 'data-maxlength'), 10) || 0, area = kind === 'textarea';
    var form = el('span', 'mvp-ed-form' + (area ? ' is-area' : ''));
    var input = el(area ? 'textarea' : 'input', 'mvp-ed-in');
    if (!area) input.type = kind === 'date' ? 'date' : kind === 'number' ? 'number' : 'text';
    if (kind === 'number') input.step = 'any';
    if (ml && kind !== 'date' && kind !== 'number') input.maxLength = ml;
    if (area) input.rows = 4;
    input.value = cur;
    input.setAttribute('aria-label', fmt('Змінити: {0}', editLabel(key) || key));
    var cnt = area && ml ? el('span', 'mvp-ed-cnt') : null;
    var ok = button('mvp-ed-ok', '✓', area ? 'Зберегти (Ctrl+Enter)' : 'Зберегти (Enter)');
    var no = button('mvp-ed-no', '✕', 'Скасувати (Esc)');
    form.appendChild(input);
    if (cnt) form.appendChild(cnt);
    form.appendChild(ok);
    form.appendChild(no);
    x.classList.add('is-editing');
    x.appendChild(form);
    var ed = { x: x, form: form, input: input, key: key, cur: cur, kind: kind };
    S.editor = ed;
    function count() { if (cnt) cnt.textContent = fmt('{0}/{1}', input.value.length, ml); }
    count();
    input.addEventListener('input', count);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); e.stopPropagation(); closeEditor(); return; }
      if (e.key === 'Enter' && (!area || e.ctrlKey || e.metaKey)) { e.preventDefault(); e.stopPropagation(); saveEditor(); }
    });
    ok.addEventListener('click', function (e) { e.stopPropagation(); saveEditor(); });
    no.addEventListener('click', function (e) { e.stopPropagation(); closeEditor(); });
    focusOn(input);
    try { if (!area && input.select) input.select(); } catch (e) { /* type=date / number */ }
  }
  function saveEditor() {
    var ed = S.editor;
    if (!ed) return;
    var v = ed.input.value;
    if (v === ed.cur) { closeEditor(); return; }
    ed.form.classList.add('is-busy');
    postEdit(ed.key, v, ed.cur, { editor: ed });
  }
  function closeEditor(silent) {
    var ed = S.editor;
    if (!ed) return;
    S.editor = null;
    ed.form.remove();
    ed.x.classList.remove('is-editing');
    if (!silent && ed.x.isConnected) focusOn(ed.x.querySelector('.mvp-ed-btn') || ed.x);
  }
  function triggerEdit(key) {
    if (switching()) { waitToast(); return; }
    var x = editEl(key);
    if (!x) { toast('Тут цього змінити не можна — відкрийте картку.'); return; }
    if (attr(x, 'data-kind') === 'choice' || attr(x, 'data-kind') === 'bool') { editClick(x); return; }
    if (x.closest('.mvp-sec')) {
      var sec = x.closest('.mvp-sec');
      if (viewMode() === 'tabs') goSec(secKey(sec)); else if (sec.hasAttribute('data-collapsed')) setCollapsed(sec, false);
      x.scrollIntoView({ block: 'nearest' });
    }
    openEditor(x);
  }
  /* запис, який зараз НАМАЛЬОВАНО в панелі; поки вантажиться наступний, S.pk — уже його, а кнопки — ще старого */
  function shownPk() { return S.shell ? attr(S.shell, 'data-pk') : ''; }
  function switching() { return !!S.pk && shownPk() !== S.pk; }
  function waitToast() { toast('Зачекайте — панель ще завантажує інший запис.'); }
  function postEdit(key, value, expect, o) {
    o = o || {};
    var ctl = o.el, own = ctl && ctl.closest ? ctl.closest('.mvp-shell') : null;
    // зміна належить запису, чиї кнопки натиснули; під час перемикання — не писати в інший запис
    if (!o.pk && (switching() || (own && attr(own, 'data-pk') !== S.pk))) { waitToast(); return Promise.resolve(null); }
    var pk = String(o.pk || shownPk() || S.pk || '');
    if (!PK.test(pk)) return Promise.resolve(null);
    if (ctl) ctl.setAttribute('aria-busy', 'true');
    return post(cfg.url + pk + '/edit/', { key: key, value: value, expect: expect }).then(function (res) {
      var d = res.data || {};
      if (res.status === 200 && d.ok) applyEdit(pk, key, d, o);
      else if (res.status === 409 && d.html) {
        cacheDrop(pk);
        if (pk === S.pk) { closeEditor(true); swapShell(d.html); }
        toast(d.message || 'Запис щойно змінив інший користувач — показано свіже.');
      } else editFailed(o, res.status, d);
    }, function () { editFailed(o, 0, {}); }).then(function () {
      if (ctl && ctl.isConnected) ctl.removeAttribute('aria-busy');
    });
  }
  function editFailed(o, status, d) {               // 400 / 403 / збій: повідомлення, значення — як було
    var msg = (d && d.message) || (status === 403 ? 'Немає права змінювати цей запис.' :
      status === 401 ? 'Сесія закінчилась — оновіть сторінку.' : SAVE_FAIL);
    if (o.toggle && o.el) flipToggle(o.el, !attr(o.el, 'data-value'));   // назад, як було
    if (o.editor && S.editor === o.editor) {
      o.editor.form.classList.remove('is-busy');
      // значення не підійшло — поле лишається, щоб виправити; інакше — як було
      if (status === 400 && d && d.error === 'invalid') focusOn(o.editor.input); else closeEditor();
    }
    toast(msg);                                      // повідомлення MVMenu — саме для читача екрана (role=status)
  }
  function applyEdit(pk, key, d, o) {
    cacheDrop(pk);
    if (o.editor && S.editor === o.editor) closeEditor(true);
    var fk = '';
    if (pk === S.pk && S.shell) {
      var act = document.activeElement;
      fk = act && panel.contains(act) ? focusKey(act) : '';
      var b = bodyEl(), top = b ? b.scrollTop : 0;
      if (d.head) replaceHead(d.head);
      var blocks = d.blocks || {};
      Object.keys(blocks).forEach(function (k) {
        var old = secFor(k);
        if (old) replaceSection(old, blocks[k], { quiet: true });
      });
      buildNav();
      observeSpy();
      observeLazy();
      if (b) b.scrollTop = top;
      if (fk) refocus(fk);
    }
    var stale = applyCells(pk, d.cells || {});
    var t = {};
    if (d.undo && d.undo.key) {
      var u = d.undo, now_ = d.value;
      t.undo = function () { postEdit(u.key, u.value, now_, { pk: pk }); };   // і коли вже відкрито інший запис
    }
    if (stale) t.action = { label: '↻ Оновіть список', run: function () { hashUpdate(); location.reload(); } };
    toast(d.message || 'Збережено', t);
    emit('mv:panel-edit', { model: cfg.model, pk: pk, key: key, value: d.value });
  }
  function applyCells(pk, cells) {                  // клітинки рядка списку — HTML з сервера (як малює Django)
    var names = Object.keys(cells || {}).filter(function (n) { return COL.test(n); });
    var tr = tableRowOf(pk), vis = rowOf(pk), stale = false;
    names.forEach(function (n) {
      var cell = tr && tr.querySelector('td.field-' + n + ', th.field-' + n);
      if (!cell) { stale = true; return; }
      var a = cell.children.length === 1 && cell.firstElementChild.tagName === 'A' &&
        cell.textContent.trim() === cell.firstElementChild.textContent.trim() ? cell.firstElementChild : null;
      (a || cell).innerHTML = cells[n];
    });
    if (vis && vis.tagName !== 'TR') stale = true;   // картки: оновляться разом зі списком
    if (stale && (vis || tr)) (vis || tr).classList.add('mvp-stale');
    return stale && !!(vis || tr);
  }
  function swapShell(html) {                        // 409: свіжа панель, той самий розділ і місце
    rememberSection();
    var keep = { sec: S.sec, offset: S.offset };
    wrap.innerHTML = html;                          // оболонка з сервера (Django екранує дані)
    var shell = wrap.querySelector('.mvp-shell');
    if (!shell) return;
    S.shell = shell;
    S.detached = {};
    decorate();
    restore(keep);
    posUpdate();
  }

  /* ── дії списку для одного запису: звичайна форма на адресу списку, як «Виконати» над списком ── */
  function runAdmin(name, confirmText, pk, label) {
    if (!pk && switching()) { waitToast(); return; }
    pk = String(pk || shownPk() || S.pk || '');
    if (!name || !PK.test(pk)) return;
    var a = adminCfg[name], text = a ? a.label : name;
    var msg = confirmText || fmt('Виконати «{0}» для {1}?', text, label || labelOf(pk));
    if (!window.confirm(msg)) return;
    var f = el('form');
    f.method = 'post';
    f.action = location.pathname + location.search;   // без #, без своїх параметрів
    f.style.display = 'none';
    [['csrfmiddlewaretoken', csrf()], ['action', name], ['index', '0'], ['select_across', '0'],
     ['_selected_action', pk]].forEach(function (p) {
      var i = el('input');
      i.type = 'hidden';
      i.name = p[0];
      i.value = p[1];
      f.appendChild(i);
    });
    if (pk === S.pk) rememberSection();
    sSet('sessionStorage', 'mvp-reopen', JSON.stringify({ model: cfg.model, pk: pk, sec: pk === S.pk ? S.sec : '',
                                                          offset: pk === S.pk ? S.offset : 0, t: now() }));
    cacheClear();
    document.body.appendChild(f);
    var left = false;
    addEventListener('pagehide', function () { left = true; }, { once: true });
    setTimeout(function () { if (!left) sDel('sessionStorage', 'mvp-reopen'); }, 30000);   // файл — сторінка лишилась
    f.submit();
  }
  function adminItems(pk, label) {
    return (cfg.adminActions || []).map(function (a) {
      var l = LEAD.exec(a.label);
      return { icon: l ? l[1] : '', label: l ? l[2] : a.label,
               onSelect: function () { runAdmin(a.name, '', pk, label); } };
    });
  }

  /* ── повернутись до панелі: #mvp=<pk>/<розділ> або після дії списку ── */
  function hashSoon() { clearTimeout(S.hashT); S.hashT = setTimeout(hashUpdate, HASH_DELAY); }
  function hashUpdate() {
    clearTimeout(S.hashT);
    if (!S.open || !S.pk) return;
    if (S.sec) sSet('localStorage', 'mvp-sec:' + cfg.model, S.sec);
    var h = location.hash;
    if (h && !/^#mvp=/.test(h)) return;              // чужий # на сторінці — не чіпати
    var want = '#mvp=' + S.pk + (S.sec ? '/' + S.sec : '');
    if (h === want) return;
    try { history.replaceState(history.state, '', location.pathname + location.search + want); } catch (e) { /* ні */ }
  }
  function hashClear() {
    if (!/^#mvp=/.test(location.hash)) return;
    try { history.replaceState(history.state, '', location.pathname + location.search); } catch (e) { /* ні */ }
  }
  function reopen() {
    if (!prefs.on || !wide()) { hashClear(); return; }
    var m = HASH.exec(location.hash || '');
    if (m) { open(m[1], { sec: m[2] || '', focus: false }); return; }
    var raw = sGet('sessionStorage', 'mvp-reopen'), d = parseJSON(raw, null);
    if (!d || d.model !== cfg.model) return;
    sDel('sessionStorage', 'mvp-reopen');
    if (!(now() - (+d.t || 0) < REOPEN_TTL)) return;
    var all = rows(), pk = String(d.pk || '');
    if (pk === 'first') pk = all.length ? pkOf(all[0]) : '';
    else if (pk === 'last') pk = all.length ? pkOf(all[all.length - 1]) : '';
    if (!PK.test(pk)) return;
    open(pk, { sec: KEY.test(d.sec || '') ? d.sec : '', offset: +d.offset || 0, focus: false });
  }
  function step(d, how) {
    var all = rows();
    if (!all.length) return;
    var i = S.row ? all.indexOf(S.row) : -1;
    if (i < 0 && S.pk) i = all.map(pkOf).indexOf(S.pk);
    var j = i < 0 ? (d > 0 ? 0 : all.length - 1) : i + d;
    if (j < 0 || j >= all.length) { edge(d); return; }
    S.edgeDir = 0;
    var row = all[j];
    row.scrollIntoView({ block: 'nearest' });
    open(pkOf(row), { row: row, dir: d, delay: how === 'key' ? NAV_DELAY : 0, focus: how === 'key' });
  }
  function edge(d) {                                // ↓ на останньому рядку: ще раз — наступна сторінка
    var pg = cfg.page, can = !!pg && (d > 0 ? pg.num < pg.pages : pg.num > 1), t = now();
    if (can && S.edgeDir === d && t - S.edgeAt < EDGE_MS) {
      S.edgeDir = 0;
      rememberSection();
      sSet('sessionStorage', 'mvp-reopen', JSON.stringify({ model: cfg.model, pk: d > 0 ? 'first' : 'last', sec: S.sec,
                                                            offset: S.offset, t: t }));
      var q = new URLSearchParams(location.search);
      q.set(pg.param || 'p', String(pg.num + d));
      location.href = location.pathname + '?' + q.toString();
      return;
    }
    S.edgeDir = d;
    S.edgeAt = t;
    if (d > 0) toast(can ? 'Остання на сторінці — ↓ ще раз: наступна сторінка' : 'Це останній запис у списку.');
    else toast(can ? 'Перша на сторінці — ↑ ще раз: попередня сторінка' : 'Це перший запис у списку.');
  }

  /* ── вибір людини: одразу на сторінці, на сервер — за 400 мс разом ── */
  var pending = null, saveT = 0, saveSeq = 0;
  function change(delta, o) {
    o = o || {};
    if (!pending) pending = { set: {}, shared: {}, before: {}, msg: '', noUndo: false };
    Object.keys(delta).forEach(function (k) {
      if (!(k in pending.before)) pending.before[k] = prefs[k] === undefined ? null : clone(prefs[k]);
      var v = delta[k] === undefined ? null : clone(delta[k]);
      if (v === null) delete prefs[k]; else prefs[k] = v;
      (isShared(k) ? pending.shared : pending.set)[k] = v;
    });
    if (o.msg) pending.msg = o.msg;
    if (o.undo === false) pending.noUndo = true;
    applyPrefs(Object.keys(delta), o);
    clearTimeout(saveT);
    saveT = setTimeout(flush, SAVE_DELAY);
  }
  function flush() {
    clearTimeout(saveT);
    saveT = 0;
    var p = pending;
    pending = null;
    if (!p) return Promise.resolve();
    var body = {}, my = ++saveSeq;
    if (Object.keys(p.set).length) body.set = p.set;
    if (Object.keys(p.shared).length) body.shared = p.shared;
    return post(cfg.prefsUrl, body).then(function (res) {
      if (res.status !== 200 || !res.data || !res.data.ok || !res.data.prefs) throw httpError(res.status);
      if (my === saveSeq) adopt(res.data.prefs);     // уже йде новіший запис — він і принесе свіже
      if (!p.noUndo) toast(p.msg || 'Збережено', { undo: function () { change(p.before, { msg: 'Повернуто як було', undo: false }); } });
      else toast(p.msg || 'Збережено');
    }).catch(function () {
      var keys = Object.keys(p.before);
      keys.forEach(function (k) { if (p.before[k] === null) delete prefs[k]; else prefs[k] = p.before[k]; });
      applyPrefs(keys, {});
      toast(SAVE_FAIL);
    });
  }
  function adopt(fresh) {                           // відповідь сервера — вибір, як він діє (очищений)
    var local = {};
    if (pending) Object.keys(pending.set).concat(Object.keys(pending.shared)).forEach(function (k) { local[k] = prefs[k]; });
    var keys = Object.keys(fresh).concat(Object.keys(prefs)).filter(function (k, i, a) {
      return a.indexOf(k) === i && !(k in local) && !same(fresh[k], prefs[k]);
    });
    prefs = clone(fresh);
    Object.keys(local).forEach(function (k) { if (local[k] === undefined) delete prefs[k]; else prefs[k] = local[k]; });
    cacheClear();
    if (keys.length) applyPrefs(keys, {});
  }
  function reset(what) {
    var keys = RESETS[what];
    if (!keys) return;
    var wait = pending ? flush() : Promise.resolve();   // спершу те, що ще не надіслано, потім «як було»
    var before = {};
    keys.forEach(function (k) { before[k] = prefs[k] === undefined ? null : clone(prefs[k]); });
    wait.then(function () {
      var my = ++saveSeq;
      return post(cfg.prefsUrl, { reset: what }).then(function (res) {
        if (res.status !== 200 || !res.data || !res.data.ok || !res.data.prefs) throw httpError(res.status);
        if (my === saveSeq) adopt(res.data.prefs);  // розділи, кнопки, вигляд — одразу на сторінці (applyPrefs)
        toast(RESET_MSG[what], { undo: function () { change(before, { msg: 'Повернуто як було', undo: false }); } });
      });
    }).catch(function () { toast(SAVE_FAIL); });
  }
  function applyPrefs(keys, o) {
    o = o || {};
    function has(k) { return keys.indexOf(k) >= 0; }
    if (has('on')) { syncOn(); if (!prefs.on) close(); }
    if (['nav', 'view', 'chips', 'density', 'empty'].some(has)) applyLook();
    if (S.open && S.shell) {
      if (has('blocks')) syncSections();
      if (has('collapsed') && !o.keepDom) syncCollapsed();
      if (['blocks', 'collapsed', 'nav', 'view', 'chips', 'empty'].some(has)) {
        if (has('nav')) S.shell.classList.toggle('mvp-nav-side', navMode() === 'side');
        buildNav();
        observeSpy();
        observeLazy();
        if (has('view')) restore({ sec: S.sec, offset: 0 });
      }
      if (has('actions') || has('bar')) layoutBar();
      if (has('actions_at')) placeActbar();
    }
    if (['width', 'place', 'helpers', 'nav'].some(has) && S.open) {
      relayout(function () {
        if (has('width')) emit('mv:panel', { open: true, width: wantWidth(), model: cfg.model, pk: S.pk });
      });
    }
    if (has('hide_cols')) applyCols();
    if (S.sheet && (!o.fromSheet || has('start'))) refreshSheet();
  }
  function syncOn() {
    var on = !!prefs.on, w = wide();
    document.body.classList.toggle('mvp-on', on && w);
    if (pill) {
      pill.hidden = !w;                                // вужче за 900 px — панелі немає, то й кнопки
      pill.setAttribute('aria-pressed', on ? 'true' : 'false');
      pill.title = on ? ON_TITLE : OFF_TITLE;
    }
  }
  function setOn(on) {
    change({ on: !!on }, { msg: on ? 'Панель увімкнено: клік по рядку — подробиці праворуч.' :
                                     'Панель вимкнено в цьому списку — «📌 Панель» над списком увімкне її знову.' });
    if (!on) close();
  }

  /* ── розділи й дії: порядок, показати, сховати ── */
  function visibleOrder() {                         // розділи, які людина бачить (права), у її порядку
    var on = list('blocks').filter(function (k) { return blockCfg[k]; });
    return on.concat((cfg.blocks || []).map(function (b) { return b.key; }).filter(function (k) { return on.indexOf(k) < 0; }));
  }
  function shownBlocks() { return list('blocks').filter(function (k) { return blockCfg[k]; }); }
  function setBlocks(vis) {                         // невидимі за правами ключі лишаються (у кінці)
    change({ blocks: vis.concat(list('blocks').filter(function (k) { return !blockCfg[k]; })) });
  }
  function setBlockShown(key, on, o) {
    var vis = shownBlocks(), i = vis.indexOf(key);
    if (on && i < 0) vis.push(key); else if (!on && i >= 0) vis.splice(i, 1); else return;
    setBlocks(vis);
    if (on && o && o.go) setTimeout(function () { var s = secFor(key); if (s) ensureLoaded(s).then(function () { goSec(key); }); }, 0);
  }
  function canMoveBlock(key, d) { var vis = shownBlocks(), i = vis.indexOf(key); return i >= 0 && i + d >= 0 && i + d < vis.length; }
  function moveBlock(key, d) {
    var vis = shownBlocks(), i = vis.indexOf(key), j = i + d;
    if (i < 0 || j < 0 || j >= vis.length) return;
    vis.splice(j, 0, vis.splice(i, 1)[0]);
    setBlocks(vis);
  }
  function moveBlockTo(key, j) {
    var vis = shownBlocks(), i = vis.indexOf(key);
    if (i < 0) return;
    vis.splice(i, 1);
    vis.splice(Math.max(0, Math.min(j, vis.length)), 0, key);
    setBlocks(vis);
  }
  function hiddenBlocks() { var on = shownBlocks(); return (cfg.blocks || []).filter(function (b) { return on.indexOf(b.key) < 0; }); }
  function actionLabel(key) {
    if (actCfg[key]) return { icon: actCfg[key].icon || '', label: actCfg[key].short || actCfg[key].label };
    if (/^admin:/.test(key)) {
      var a = adminCfg[key.slice(6)];
      if (a) { var m = LEAD.exec(a.label); return { icon: m ? m[1] : '⚡', label: m ? m[2] : a.label }; }
    }
    return null;
  }
  function knownAction(key) { return !!actionLabel(key); }
  function pinnedActions() { return list('actions').filter(knownAction); }
  function setActions(vis, extra) {                 // невідомі тут ключі лишаються (у кінці)
    var d = { actions: vis.concat(list('actions').filter(function (k) { return !knownAction(k); })) };
    if (extra) Object.keys(extra).forEach(function (k) { d[k] = extra[k]; });
    change(d);
  }
  function setActionPinned(key, on) {
    var vis = pinnedActions(), i = vis.indexOf(key);
    if (on && i < 0) vis.push(key); else if (!on && i >= 0) vis.splice(i, 1); else return;
    setActions(vis);
  }
  function moveAction(key, d) {
    var vis = pinnedActions(), i = vis.indexOf(key), j = i + d;
    if (i < 0 || j < 0 || j >= vis.length) return;
    vis.splice(j, 0, vis.splice(i, 1)[0]);
    setActions(vis);
  }
  function moveAcrossBar(key, toBar) {              // «📌 Завжди на панелі» / «⋯ Лише в меню»
    var vis = pinnedActions(), i = vis.indexOf(key), n = barCount();
    if (toBar) {
      if (i >= 0 && i < n) return;
      if (i >= 0) vis.splice(i, 1);
      vis.splice(Math.max(0, Math.min(n, BAR_MAX) - 1), 0, key);
      setActions(vis, n === 0 ? { bar: 1 } : null);
    } else {
      if (i < 0 || i >= n) return;
      vis.splice(i, 1);
      if (vis.length < n) { vis.push(key); setActions(vis, { bar: vis.length - 1 }); return; }
      vis.splice(n, 0, key);
      setActions(vis);
    }
  }
  function allActions() {                           // закріплені (у порядку людини), потім решта
    var pinned = pinnedActions();
    return pinned.concat((cfg.actions || []).map(function (a) { return a.key; }).filter(function (k) { return pinned.indexOf(k) < 0; }));
  }

  /* ── меню (MVMenu) ── */
  function secItems() {
    var items = visibleOrder().map(function (key) {
      var b = blockCfg[key];
      return { type: 'check', id: 'sec:' + key, icon: b.icon, label: b.title,
               checked: function () { return shownBlocks().indexOf(key) >= 0; },
               onToggle: function (next) { setBlockShown(key, next); },
               buttons: [{ label: '⬆', aria: fmt('Вище: {0}', b.title), disabled: function () { return !canMoveBlock(key, -1); },
                           onClick: function () { moveBlock(key, -1); } },
                         { label: '⬇', aria: fmt('Нижче: {0}', b.title), disabled: function () { return !canMoveBlock(key, 1); },
                           onClick: function () { moveBlock(key, 1); } }] };
    });
    items.push({ type: 'separator' });
    items.push({ icon: '⇕', label: 'Згорнути всі', onSelect: function () { change({ collapsed: shownBlocks() }); } });
    items.push({ icon: '⇕', label: 'Розгорнути всі', onSelect: function () { change({ collapsed: [] }); } });
    items.push({ type: 'submenu', icon: '📍', label: 'Відкривати на', hint: startHint, items: startItems });
    items.push({ icon: '↺', label: 'Розділи як було', onSelect: function () { reset('blocks'); } });
    return items;
  }
  function startHint() {
    var st = prefs.start || 'last';
    if (st === 'last') return 'Останньому розділі';
    if (st === 'first') return 'Початку';
    return blockCfg[st] ? blockCfg[st].title : '';
  }
  function startItems() {
    var st = prefs.start || 'last';
    var items = [
      { type: 'radio', group: 'start', label: 'Останньому розділі', checked: st === 'last',
        onSelect: function () { change({ start: 'last' }); } },
      { type: 'radio', group: 'start', label: 'Початку', checked: st === 'first',
        onSelect: function () { change({ start: 'first' }); } },
      { type: 'separator' }];
    shownBlocks().forEach(function (k) {
      items.push({ type: 'radio', group: 'start', icon: blockCfg[k].icon, label: blockCfg[k].title, checked: st === k,
                   onSelect: function () { change({ start: k }); } });
    });
    return items;
  }
  function actItems() {
    var items = allActions().map(function (key) {
      var l = actionLabel(key);
      return { type: 'check', id: 'act:' + key, icon: l.icon, label: l.label,
               checked: function () { return pinnedActions().indexOf(key) >= 0; },
               onToggle: function (next) { setActionPinned(key, next); },
               buttons: [{ label: '⬆', aria: fmt('Лівіше: {0}', l.label),
                           disabled: function () { var v = pinnedActions(), i = v.indexOf(key); return i <= 0; },
                           onClick: function () { moveAction(key, -1); } },
                         { label: '⬇', aria: fmt('Правіше: {0}', l.label),
                           disabled: function () { var v = pinnedActions(), i = v.indexOf(key); return i < 0 || i >= v.length - 1; },
                           onClick: function () { moveAction(key, 1); } }] };
    });
    items.push({ type: 'separator' });
    items.push({ type: 'submenu', label: 'Кнопок на панелі', hint: function () { return String(barCount()); },
                 items: function () {
                   return [2, 3, 4, 5, 6].map(function (n) {
                     return { type: 'radio', group: 'bar', label: String(n), checked: barCount() === n,
                              onSelect: function () { change({ bar: n }); } };
                   });
                 } });
    items.push({ type: 'submenu', label: 'Де кнопки', hint: function () { return prefs.actions_at === 'bottom' ? 'Унизу' : 'Угорі'; },
                 items: function () {
                   return [['top', 'Угорі'], ['bottom', 'Унизу']].map(function (o) {
                     return { type: 'radio', group: 'at', label: o[1], checked: (prefs.actions_at || 'top') === o[0],
                              onSelect: function () { change({ actions_at: o[0] }); } };
                   });
                 } });
    if ((cfg.adminActions || []).length) {
      items.push({ type: 'submenu', icon: '⚡', label: 'Дії списку на панель', items: function () {
        return cfg.adminActions.map(function (a) {
          var key = 'admin:' + a.name, m = LEAD.exec(a.label);
          return { type: 'check', icon: m ? m[1] : '', label: m ? m[2] : a.label,
                   checked: function () { return pinnedActions().indexOf(key) >= 0; },
                   onToggle: function (next) { setActionPinned(key, next); } };
        });
      } });
    }
    items.push({ icon: '↺', label: 'Дії як було', onSelect: function () { reset('actions'); } });
    return items;
  }
  function radioSet(group, key, opts) {
    return opts.map(function (o) {
      return { type: 'radio', group: group, label: o[1], checked: function () { return (prefs[key] || opts[0][0]) === o[0]; },
               onSelect: function () { var d = {}; d[key] = o[0]; change(d); } };
    });
  }
  var NAV_OPTS = [['top', 'Угорі'], ['side', 'Збоку значками'], ['none', 'Без підменю']];
  var VIEW_OPTS = [['scroll', '📜 Усе підряд'], ['tabs', '🗂 По одному']];
  var CHIP_OPTS = [['auto', 'Авто'], ['icons', 'Лише значки'], ['labels', 'Значки й назви']];
  var PLACE_OPTS = [['auto', 'Авто'], ['push', 'Поруч зі списком'], ['overlay', 'Поверх списку']];
  var HELP_OPTS = [['dock', 'У панелі'], ['beside', 'Ліворуч від панелі']];
  var DENS_OPTS = [['normal', 'Звичайна'], ['compact', 'Компактна']];
  var EMPTY_OPTS = [['compact', 'Одним рядком'], ['show', 'Повністю']];
  var SIDEBAR_OPTS = [['auto', 'Само звужується'], ['wide', 'Завжди повне'], ['rail', 'Завжди значки']];
  function optLabel(opts, v) { var o = opts.filter(function (x) { return x[0] === v; })[0]; return o ? o[1] : opts[0][1]; }
  function navItems() {
    var items = radioSet('nav', 'nav', NAV_OPTS);
    items[1].hint = function () { return (S.shown || wantWidth()) < SIDE_MIN ? 'від 440 px' : ''; };
    return items.concat([{ type: 'separator' }], radioSet('view', 'view', VIEW_OPTS), [
      { type: 'submenu', label: 'Підписи', hint: function () { return optLabel(CHIP_OPTS, prefs.chips); },
        items: function () { return radioSet('chips', 'chips', CHIP_OPTS); } }]);
  }
  function widthItems() {
    var w = prefs.width;
    var items = [[360, 'Вузька 360'], [440, 'Звичайна 440'], [560, 'Широка 560'], [720, 'Дуже широка 720']].map(function (o) {
      return { type: 'radio', group: 'width', label: o[1], checked: w === o[0], onSelect: function () { setWidth(o[0]); } };
    });
    items.push({ type: 'radio', group: 'width', label: 'Авто (28 % екрана)', checked: w == null,
                 onSelect: function () { setWidth(null); } });
    items.push({ type: 'hint', label: '💡 або потягніть лівий край' });
    return items;
  }
  function sidebarMode() { return root.getAttribute('data-mv-sidebar') || 'auto'; }
  function setSidebar(v) {
    relayout(function () { root.setAttribute('data-mv-sidebar', v); });
    post('/core/my-settings/', { ui: { sidebar: v } }).then(function (res) {
      toast(res.status === 200 && res.data && res.data.ok ? 'Збережено' : SAVE_FAIL);
    }, function () { toast(SAVE_FAIL); });
  }
  function placeItems() {
    var items = radioSet('place', 'place', PLACE_OPTS);
    if (root.getAttribute('data-mv-design') === 'new') {
      items.push({ type: 'submenu', label: 'Меню ліворуч', hint: function () { return optLabel(SIDEBAR_OPTS, sidebarMode()); },
                   items: function () {
                     return SIDEBAR_OPTS.map(function (o) {
                       return { type: 'radio', group: 'sidebar', label: o[1], checked: sidebarMode() === o[0],
                                onSelect: function () { setSidebar(o[0]); } };
                     });
                   } });
    }
    items.push({ type: 'submenu', label: 'Помічники 🏛 Minerva', hint: function () { return optLabel(HELP_OPTS, prefs.helpers); },
                 items: function () { return radioSet('helpers', 'helpers', HELP_OPTS); } });
    return items;
  }
  function lookItems() {
    return [{ type: 'title', label: 'Щільність' }].concat(radioSet('density', 'density', DENS_OPTS),
      [{ type: 'separator' }, { type: 'title', label: 'Порожні розділи' }], radioSet('empty', 'empty', EMPTY_OPTS));
  }
  function colItems() {
    var items = columns.map(function (c) {
      return { type: 'check', label: c.label, checked: function () { return hideCols().indexOf(c.name) < 0; },
               onToggle: function (next) { setColHidden(c.name, !next); } };
    });
    items.push({ type: 'separator' });
    items.push({ icon: '↺', label: 'Як у Minerva', close: false, onSelect: function () { change({ hide_cols: null }); } });
    items.push({ icon: '👁', label: 'Показувати всі', close: false, onSelect: function () { change({ hide_cols: [] }); } });
    return items;
  }
  function resetItems() {
    return [{ icon: '📑', label: 'Розділи', onSelect: function () { reset('blocks'); } },
            { icon: '⚡', label: 'Швидкі дії', onSelect: function () { reset('actions'); } },
            { icon: '🔠', label: 'Вигляд для всіх списків', onSelect: function () { reset('layout'); } },
            { icon: '↺', label: 'Усе в цьому списку', onSelect: function () { reset('list'); } }];
  }
  function gearItems() {
    return [
      { type: 'submenu', icon: '📑', label: 'Розділи', items: secItems,
        hint: function () { return fmt('{0} з {1}', shownBlocks().length, (cfg.blocks || []).length); } },
      { type: 'submenu', icon: '⚡', label: 'Швидкі дії', items: actItems,
        hint: function () { return fmt('{0} на панелі', Math.min(barCount(), pinnedActions().length)); } },
      { type: 'submenu', icon: '🧭', label: 'Підменю (для всіх списків)', items: navItems,
        hint: function () { return optLabel(NAV_OPTS, prefs.nav); } },
      { type: 'submenu', icon: '↔', label: 'Ширина', items: widthItems,
        hint: function () { return prefs.width == null ? fmt('Авто · {0} px', S.shown || wantWidth()) : fmt('{0} px', prefs.width); } },
      { type: 'submenu', icon: '🪟', label: 'Розміщення', items: placeItems,
        hint: function () { return optLabel(PLACE_OPTS, prefs.place); } },
      { type: 'submenu', icon: '🔠', label: 'Вигляд', items: lookItems,
        hint: function () { return optLabel(DENS_OPTS, prefs.density); } },
      { type: 'submenu', icon: '📋', label: 'Колонки списку, поки панель відкрита', items: colItems, hidden: !columns.length,
        hint: function () { var n = hiddenCount(); return n ? fmt('сховано {0}', n) : ''; } },
      { type: 'separator' },
      { icon: '⚙', label: 'Усі налаштування панелі…', onSelect: function () { withShell(function () { openSheet(); }); } },
      { type: 'submenu', icon: '↺', label: 'Як було', items: resetItems },
      { icon: '✕', label: 'Вимкнути панель у цьому списку', onSelect: function () { setOn(false); } },
      { icon: '⌨', label: 'Клавіші…', onSelect: function () { withShell(keysSheet); } }
    ];
  }
  function gearTitle() { return fmt('Панель «{0}» під себе', cfg.name || ''); }
  function gearMenu(at) { menu(at, gearItems(), { title: gearTitle(), side: 'bottom-end' }); }
  function withShell(fn) {                          // налаштування й клавіші — у відкритій панелі
    if (S.open && S.shell && bodyEl()) { fn(); return; }
    var all = rows(), row = S.row || all[0];
    if (!row || !prefs.on) { toast('Відкрийте запис у списку — налаштування з’являться в панелі.'); return; }
    S.afterLoad = fn;
    open(pkOf(row), { row: row, focus: false });
  }
  function secMenu(sec, at) {
    if (!sec) return;
    var key = secKey(sec), title = attr(sec, 'data-title'), vis = shownBlocks(), i = vis.indexOf(key);
    var folded = sec.hasAttribute('data-collapsed');
    menu(at, [
      { icon: '⬆', label: 'Вище', disabled: i <= 0, onSelect: function () { moveBlock(key, -1); } },
      { icon: '⬇', label: 'Нижче', disabled: i < 0 || i >= vis.length - 1, onSelect: function () { moveBlock(key, 1); } },
      { icon: '⤒', label: 'Першим', disabled: i <= 0, onSelect: function () { moveBlockTo(key, 0); } },
      { icon: '⇕', label: folded ? 'Розгорнути' : 'Згорнути', hidden: viewMode() === 'tabs',
        onSelect: function () { toggleSec(secFor(key), true); } },
      { icon: '📍', label: 'Відкривати панель на цьому розділі', hint: prefs.start === key ? '✓' : '',
        onSelect: function () { change({ start: key }); } },
      { icon: '🙈', label: 'Сховати розділ', onSelect: function () { setBlockShown(key, false); } },
      { type: 'separator' },
      { type: 'submenu', icon: '＋', label: 'Додати розділ', items: addItems, disabled: !hiddenBlocks().length },
      { icon: '⚙', label: 'Розділи…', onSelect: function () { openSheet('sections'); } }
    ], { title: fmt('Розділ «{0}»', title) });
  }
  function addItems() {
    var items = hiddenBlocks().map(function (b) {
      return { icon: b.icon, label: b.title, onSelect: function () { setBlockShown(b.key, true, { go: true }); } };
    });
    return items.length ? items : [{ type: 'hint', label: 'Усі розділи вже на панелі.' }];
  }
  function navMoreMenu(btn) {
    var items = chips().filter(function (c) { return c.hidden && !c.classList.contains('mvp-ch-more'); }).map(function (c) {
      var key = attr(c, 'data-go'), b = blockCfg[key] || {}, ci = c.querySelector('.mvp-ci'), cn = c.querySelector('.mvp-cn');
      return { icon: b.icon || (ci ? ci.textContent : ''), label: b.title || attr(c, 'aria-label'),
               hint: cn ? cn.textContent : '', onSelect: function () { goSec(key, { focus: true }); } };
    });
    items.push({ type: 'separator' });
    items.push({ type: 'submenu', icon: '＋', label: 'Додати розділ', items: addItems, disabled: !hiddenBlocks().length });
    items.push({ icon: '⚙', label: 'Розділи й підменю…', onSelect: function () { openSheet('sections'); } });
    menu(btn, items, { label: 'Ще розділи', side: 'bottom-end' });
  }
  function moreMenu(btn) {
    var items = overflowEls().map(itemFromEl), sh = S.shell;
    if (items.length) items.push({ type: 'separator' });
    var st = sh && sh.querySelector('.mvp-hedit [data-edit][data-kind="choice"]');
    if (st) items.push({ type: 'submenu', icon: '🔁', label: 'Змінити статус', items: function () { return choiceItems(st); } });
    if ((cfg.adminActions || []).length) {
      items.push({ type: 'submenu', icon: '⚡', label: 'Дії списку для цього запису', items: function () { return adminItems(S.pk); } });
    }
    items.push({ type: 'separator' });
    var label = attr(sh, 'data-label'), card = attr(sh, 'data-change-url'), hist = attr(sh, 'data-history-url');
    var del = attr(sh, 'data-delete-url'), q = location.search.replace(/^\?/, '');
    if (label) items.push({ icon: '📋', label: 'Копіювати № / назву', onSelect: function () { copyText(label); } });
    if (card) {
      items.push({ icon: '🔗', label: 'Копіювати посилання на картку',
                   onSelect: function () { copyText(new URL(card, location.href).href); } });
      items.push({ icon: '↗', label: 'Картка в новій вкладці', href: cardHref(S.pk) || card, target: '_blank' });
    }
    if (hist) items.push({ icon: '🕘', label: 'Історія змін', href: hist, onSelect: hashUpdate });
    if (cfg.canDelete && del) {
      items.push({ icon: '🗑', label: 'Видалити…', danger: true, onSelect: hashUpdate,
                   href: q ? del + (del.indexOf('?') < 0 ? '?' : '&') + '_changelist_filters=' + encodeURIComponent(q) : del });
    }
    items.push({ type: 'separator' });
    items.push({ icon: '⚙', label: 'Швидкі дії…', onSelect: function () { openSheet('actions'); } });
    menu(btn, items, { label: 'Ще дії', side: 'bottom-end' });
  }
  function actMenu(it, at) {
    var key = actKey(it), vis = pinnedActions(), i = vis.indexOf(key), n = barCount(), l = itemLabel(it);
    var onBar = i >= 0 && i < n;
    menu(at, [
      { icon: '⬅', label: 'Лівіше', disabled: i <= 0, onSelect: function () { moveAction(key, -1); } },
      { icon: '➡', label: 'Правіше', disabled: i < 0 || i >= vis.length - 1, onSelect: function () { moveAction(key, 1); } },
      onBar ? { icon: '⋯', label: 'Лише в меню', onSelect: function () { moveAcrossBar(key, false); } }
            : { icon: '📌', label: 'Завжди на панелі', onSelect: function () { moveAcrossBar(key, true); } },
      { icon: '🙈', label: 'Прибрати', disabled: i < 0, onSelect: function () { setActionPinned(key, false); } },
      { type: 'separator' },
      { icon: '⚙', label: 'Швидкі дії…', onSelect: function () { openSheet('actions'); } }
    ], { title: l.label });
  }
  function rowMenu(row, e) {
    var pk = pkOf(row), title = rowTitle(row), card = changeLinkOf(row);
    if (!pk) return;
    var items = [{ icon: '👁', label: 'Подробиці праворуч', onSelect: function () { open(pk, { row: row, dir: 1 }); } }];
    if (card) {
      items.push({ icon: '📋', label: 'Відкрити картку', href: card, onSelect: hashUpdate });
      items.push({ icon: '↗', label: 'Картка в новій вкладці', href: card, target: '_blank' });
    }
    if ((cfg.adminActions || []).length) {
      items.push({ type: 'submenu', icon: '⚡', label: 'Дія для цього запису', items: function () { return adminItems(pk, title); } });
    }
    items.push({ type: 'separator' });
    items.push({ type: 'submenu', icon: '⚙', label: 'Панель', items: gearItems });
    menu(e, items, { title: title, titleData: true });
  }
  function colMenu(th, e) {
    var name = colName(th), hidden = hideCols().indexOf(name) >= 0;
    menu(e, [
      hidden ? { icon: '👁', label: 'Показувати', onSelect: function () { setColHidden(name, false); } }
             : { icon: '🙈', label: 'Ховати, поки панель відкрита', onSelect: function () { setColHidden(name, true); } },
      { type: 'submenu', icon: '📋', label: 'Колонки списку, поки панель відкрита', items: colItems, hidden: !columns.length },
      { icon: '↺', label: 'Як у Minerva', onSelect: function () { change({ hide_cols: null }); } }
    ], { title: fmt('Колонка «{0}»', colLabel(name) || th.textContent.replace(/\s+/g, ' ').trim()) });
  }
  function pillMenu(at) {
    menu(at, [
      { type: 'check', label: 'Панель у цьому списку', checked: !!prefs.on, close: true, onToggle: function (next) { setOn(next); } },
      { type: 'submenu', icon: '⚙', label: 'Налаштування панелі', items: gearItems }
    ], { title: fmt('Панель «{0}»', cfg.name || '') });
  }
  function isEmptyArea(t) {
    return t === panel || (t.matches && t.matches('.mvp-wrap, .mvp-shell, .mvp-body, .mvp-sb, .mvp-sec, .mvp-nav, ' +
      '.mvp-actbar, .mvp-foot, .mvp-empty, .mvp-skel, .mvp-stamp'));
  }
  function panelMenu(t, e) {                         // true — показали своє меню
    var x;
    if ((x = t.closest('.mvp-ch'))) {
      if (x.classList.contains('mvp-ch-more')) navMoreMenu(e); else secMenu(secFor(attr(x, 'data-go')), e);
      return true;
    }
    if ((x = t.closest('.mvp-sh'))) { secMenu(x.closest('.mvp-sec'), e); return true; }
    if ((x = t.closest('.mvp-actbar [data-act]'))) { actMenu(x, e); return true; }
    if ((x = t.closest('.mvp-grip'))) { menu(e, widthItems(), { title: 'Ширина' }); return true; }
    if (t.closest('.mvp-sheet')) return false;
    // шапка (назва, підрядок, нотатка) і порожнє місце панелі — «⚙ під себе»
    if (t.closest('[data-mvp-more], [data-mvp-menu], .mvp-hrow, .mvp-head > .mvp-sub, .mvp-memo') || isEmptyArea(t)) {
      gearMenu(e);
      return true;
    }
    return false;
  }

  /* ── налаштування панелі в самій панелі (замість розділів) ── */
  function sheetGroup(box, title, note) {
    var g = el('section', 'mvp-sg');
    g.appendChild(el('h4', '', title));
    if (note) g.appendChild(el('p', 'mvp-sg-note', note));
    box.appendChild(g);
    return g;
  }
  function radioGroup(name, opts, cur, onPick, caption) {
    var box = el('div', 'mvp-sopts');
    box.setAttribute('role', 'radiogroup');
    if (caption) box.setAttribute('aria-label', caption);
    opts.forEach(function (o) {
      var lab = el('label', 'mvp-sopt'), inp = el('input');
      inp.type = 'radio';
      inp.name = 'mvp-s-' + name;
      inp.value = String(o[0]);
      inp.checked = String(cur) === String(o[0]);
      inp.setAttribute('data-fk', name + ':' + o[0]);
      inp.addEventListener('change', function () { if (inp.checked) onPick(o[0]); });
      lab.appendChild(inp);
      lab.appendChild(el('span', '', o[1]));
      box.appendChild(lab);
    });
    return box;
  }
  function checkBox(label, on, fk, onSet, cls) {
    var lab = el('label', cls || 'mvp-sopt'), inp = el('input');
    inp.type = 'checkbox';
    inp.checked = !!on;
    inp.setAttribute('data-fk', fk);
    inp.addEventListener('change', function () { onSet(inp.checked); });
    lab.appendChild(inp);
    lab.appendChild(el('span', cls === 'mvp-sl-main' ? 'mvp-sl-name' : '', label));
    return lab;
  }
  function smallBtn(text, aria, fk, fn, disabled) {
    var b = button('mvp-btn mvp-btn-sm', text, aria);
    b.setAttribute('data-fk', fk);
    b.disabled = !!disabled;
    b.addEventListener('click', fn);
    return b;
  }
  function sheetBtn(text, fk, fn, cls) {
    var b = button('mvp-btn' + (cls ? ' ' + cls : ''), text);
    b.setAttribute('data-fk', fk);
    b.addEventListener('click', fn);
    return b;
  }
  function sheetSections(box) {
    var g = sheetGroup(box, '📑 Розділи', 'Порядок — ⠿ або ⬆ ⬇; ☑ — показувати; «згорнутий» — відкривається заголовком.');
    var ul = el('ul', 'mvp-sl'), on = shownBlocks(), folded = list('collapsed');
    ul.setAttribute('data-list', 'blocks');
    visibleOrder().forEach(function (key) {
      var b = blockCfg[key], shown = on.indexOf(key) >= 0, i = on.indexOf(key);
      var li = el('li', 'mvp-sli' + (shown ? '' : ' is-off'));
      li.setAttribute('data-key', key);
      var h = el('span', 'mvp-drag', '⠿');
      h.setAttribute('aria-hidden', 'true');
      li.appendChild(h);
      li.appendChild(checkBox((b.icon ? b.icon + ' ' : '') + b.title, shown, 'sb:' + key,
        function (v) { setBlockShown(key, v); renderSheet(); }, 'mvp-sl-main'));
      var fold = checkBox('⇕ згорнутий', folded.indexOf(key) >= 0, 'sc:' + key, function (v) {
        var c = list('collapsed').filter(function (k) { return k !== key; });
        if (v) c.push(key);
        change({ collapsed: c }, { fromSheet: true });
      }, 'mvp-sl-opt');
      if (!shown) fold.querySelector('input').disabled = true;
      li.appendChild(fold);
      li.appendChild(smallBtn('⬆', fmt('Вище: {0}', b.title), 'su:' + key,
        function () { moveBlock(key, -1); renderSheet(); }, !shown || i <= 0));
      li.appendChild(smallBtn('⬇', fmt('Нижче: {0}', b.title), 'sd:' + key,
        function () { moveBlock(key, 1); renderSheet(); }, !shown || i >= on.length - 1));
      ul.appendChild(li);
    });
    g.appendChild(ul);
    dragList(ul, '.mvp-sli', 'y', function (item, idx) {
      var key = attr(item, 'data-key'), keys = [].slice.call(ul.querySelectorAll('.mvp-sli')).map(function (n) { return attr(n, 'data-key'); });
      var from = keys.indexOf(key);
      keys.splice(from, 1);
      keys.splice(idx > from ? idx - 1 : idx, 0, key);
      var shownNow = shownBlocks(), shownSet = {};
      if (shownNow.indexOf(key) < 0) shownNow.push(key);
      shownNow.forEach(function (k) { shownSet[k] = true; });
      setBlocks(keys.filter(function (k) { return shownSet[k]; }));
      renderSheet();
    });
    var row = el('div', 'mvp-srow');
    row.appendChild(sheetBtn('⇕ Згорнути всі', 'fold-all', function () { change({ collapsed: shownBlocks() }); renderSheet(); }));
    row.appendChild(sheetBtn('⇕ Розгорнути всі', 'unfold-all', function () { change({ collapsed: [] }); renderSheet(); }));
    g.appendChild(row);
    var st = prefs.start || 'last', opts = [['last', 'Останньому розділі'], ['first', 'Початку']];
    var p = el('div', 'mvp-sopts');
    p.appendChild(el('div', 'mvp-sopt-t', 'Відкривати панель на'));
    g.appendChild(p);
    var rg = radioGroup('start', opts.concat([['key', 'Розділі:']]), st === 'last' || st === 'first' ? st : 'key', function (v) {
      if (v === 'key') { var s = g.querySelector('select'); change({ start: (s && s.value) || shownBlocks()[0] || 'last' }, { fromSheet: true }); }
      else change({ start: v }, { fromSheet: true });
    }, 'Відкривати панель на');
    var sel = el('select');
    sel.setAttribute('data-fk', 'start-sel');
    sel.setAttribute('aria-label', 'Розділ, на якому відкривати панель');
    shownBlocks().forEach(function (k) {
      var o = el('option', '', blockCfg[k].title);
      o.value = k;
      if (st === k) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', function () { change({ start: sel.value }, { fromSheet: true }); renderSheet(); });
    rg.appendChild(sel);
    g.appendChild(rg);
    g.appendChild(sheetBtn('↺ Розділи як було', 'reset-blocks', function () { reset('blocks'); }));
    return g;
  }
  function sheetActions(box) {
    var g = sheetGroup(box, '⚡ Швидкі дії', 'Над межею «— далі в меню ⋯ —» — кнопки на панелі, під нею — у «⋯». Межу теж можна тягти.');
    var ul = el('ul', 'mvp-sl'), pinned = pinnedActions(), n = Math.min(barCount(), pinned.length);
    ul.setAttribute('data-list', 'actions');
    function row(key, on, i) {
      var l = actionLabel(key), li = el('li', 'mvp-sli' + (on ? '' : ' is-off'));
      li.setAttribute('data-key', key);
      var h = el('span', 'mvp-drag', '⠿');
      h.setAttribute('aria-hidden', 'true');
      li.appendChild(h);
      li.appendChild(checkBox((l.icon ? l.icon + ' ' : '') + l.label, on, 'ap:' + key,
        function (v) { setActionPinned(key, v); renderSheet(); }, 'mvp-sl-main'));
      li.appendChild(smallBtn('⬆', fmt('Лівіше: {0}', l.label), 'au:' + key,
        function () { moveAction(key, -1); renderSheet(); }, !on || i <= 0));
      li.appendChild(smallBtn('⬇', fmt('Правіше: {0}', l.label), 'ad:' + key,
        function () { moveAction(key, 1); renderSheet(); }, !on || i >= pinned.length - 1));
      return li;
    }
    pinned.forEach(function (key, i) {
      if (i === n) ul.appendChild(divider());
      ul.appendChild(row(key, true, i));
    });
    if (n >= pinned.length) ul.appendChild(divider());
    allActions().filter(function (k) { return pinned.indexOf(k) < 0; }).forEach(function (key) { ul.appendChild(row(key, false, -1)); });
    function divider() {
      var d = el('li', 'mvp-sdiv mvp-sli', '— далі в меню ⋯ —');
      d.setAttribute('data-key', '|');
      d.setAttribute('aria-hidden', 'true');
      return d;
    }
    g.appendChild(ul);
    dragList(ul, '.mvp-sli', 'y', function (item, idx) {
      var keys = [].slice.call(ul.querySelectorAll('.mvp-sli')).map(function (x) { return attr(x, 'data-key'); });
      var key = attr(item, 'data-key'), from = keys.indexOf(key);
      keys.splice(from, 1);
      keys.splice(idx > from ? idx - 1 : idx, 0, key);
      var cut = keys.indexOf('|'), order = [], bar = 0, pinnedNow = pinnedActions();
      if (key !== '|' && pinnedNow.indexOf(key) < 0) pinnedNow.push(key);
      keys.forEach(function (k, i) {
        if (k === '|' || pinnedNow.indexOf(k) < 0) return;
        order.push(k);
        if (i < cut) bar += 1;
      });
      setActions(order, { bar: Math.min(BAR_MAX, bar) });
      renderSheet();
    });
    var r = el('div', 'mvp-srow');
    var lab = el('label', 'mvp-sopt', 'Кнопок на панелі');
    var range = el('input', 'mvp-srange');
    range.type = 'range';
    range.min = '0';
    range.max = String(BAR_MAX);
    range.step = '1';
    range.value = String(barCount());
    range.setAttribute('data-fk', 'bar-range');
    var out = el('span', 'mvp-sout', String(barCount()));
    range.addEventListener('input', function () { out.textContent = range.value; });
    range.addEventListener('change', function () { change({ bar: +range.value }); renderSheet(); });
    lab.appendChild(range);
    r.appendChild(lab);
    r.appendChild(out);
    g.appendChild(r);
    g.appendChild(el('div', 'mvp-sopt-t', 'Де кнопки'));
    g.appendChild(radioGroup('at', [['top', 'Угорі'], ['bottom', 'Унизу']], prefs.actions_at || 'top',
      function (v) { change({ actions_at: v }, { fromSheet: true }); }, 'Де кнопки'));
    if ((cfg.adminActions || []).length) {
      g.appendChild(el('div', 'mvp-sopt-t', '⚡ Дії списку на панель'));
      var box2 = el('div', 'mvp-sopts');
      cfg.adminActions.forEach(function (a) {
        var key = 'admin:' + a.name;
        box2.appendChild(checkBox(a.label, pinnedActions().indexOf(key) >= 0, 'aa:' + a.name,
          function (v) { setActionPinned(key, v); renderSheet(); }));
      });
      g.appendChild(box2);
    }
    g.appendChild(sheetBtn('↺ Дії як було', 'reset-actions', function () { reset('actions'); }));
    return g;
  }
  function sheetLook(box) {
    var g = sheetGroup(box, '🧭 Підменю (для всіх списків)');
    g.appendChild(radioGroup('nav', NAV_OPTS, prefs.nav || 'top', function (v) { change({ nav: v }, { fromSheet: true }); }, 'Підменю'));
    if ((S.shown || wantWidth()) < SIDE_MIN) g.appendChild(el('p', 'mvp-sg-note', '«Збоку значками» — коли панель від 440 px.'));
    g.appendChild(radioGroup('view', VIEW_OPTS, prefs.view || 'scroll', function (v) { change({ view: v }, { fromSheet: true }); }, 'Як показувати розділи'));
    g.appendChild(el('div', 'mvp-sopt-t', 'Підписи'));
    g.appendChild(radioGroup('chips', CHIP_OPTS, prefs.chips || 'auto', function (v) { change({ chips: v }, { fromSheet: true }); }, 'Підписи'));

    var w = sheetGroup(box, '↔ Ширина', 'Або потягніть лівий край панелі; подвійний клік по ньому — авто.');
    var r = el('div', 'mvp-srow'), range = el('input', 'mvp-srange'), out = el('span', 'mvp-sout');
    range.type = 'range';
    range.min = String(W_MIN);
    range.max = String(widthCap());
    range.step = '10';
    range.value = String(S.shown || wantWidth());
    range.setAttribute('data-fk', 'width-range');
    range.setAttribute('aria-label', 'Ширина панелі');
    out.textContent = fmt('{0} px', range.value);
    range.addEventListener('input', function () { out.textContent = fmt('{0} px', previewWidth(+range.value)); });
    range.addEventListener('change', function () { setWidth(+range.value); });
    r.appendChild(range);
    r.appendChild(out);
    w.appendChild(r);
    var pr = el('div', 'mvp-srow');
    PRESETS.forEach(function (p) {
      var b = sheetBtn(String(p), 'wp:' + p, function () { setWidth(p); renderSheet(); });
      b.setAttribute('aria-pressed', prefs.width === p ? 'true' : 'false');
      pr.appendChild(b);
    });
    var auto = sheetBtn('Авто (28 % екрана)', 'wp:auto', function () { setWidth(null); renderSheet(); });
    auto.setAttribute('aria-pressed', prefs.width == null ? 'true' : 'false');
    pr.appendChild(auto);
    w.appendChild(pr);

    var pl = sheetGroup(box, '🪟 Розміщення');
    pl.appendChild(radioGroup('place', PLACE_OPTS, prefs.place || 'auto', function (v) { change({ place: v }, { fromSheet: true }); }, 'Розміщення'));
    if (root.getAttribute('data-mv-design') === 'new') {
      pl.appendChild(el('div', 'mvp-sopt-t', 'Меню ліворуч'));
      pl.appendChild(radioGroup('sidebar', SIDEBAR_OPTS, sidebarMode(), function (v) { setSidebar(v); }, 'Меню ліворуч'));
    }
    pl.appendChild(el('div', 'mvp-sopt-t', 'Помічники 🏛 Minerva'));
    pl.appendChild(radioGroup('helpers', HELP_OPTS, prefs.helpers || 'dock', function (v) { change({ helpers: v }, { fromSheet: true }); }, 'Помічники'));

    var lk = sheetGroup(box, '🔠 Вигляд');
    lk.appendChild(el('div', 'mvp-sopt-t', 'Щільність'));
    lk.appendChild(radioGroup('density', DENS_OPTS, prefs.density || 'normal', function (v) { change({ density: v }, { fromSheet: true }); }, 'Щільність'));
    lk.appendChild(el('div', 'mvp-sopt-t', 'Порожні розділи'));
    lk.appendChild(radioGroup('empty', EMPTY_OPTS, prefs.empty || 'compact', function (v) { change({ empty: v }, { fromSheet: true }); }, 'Порожні розділи'));
  }
  function sheetColumns(box) {
    if (!columns.length) return;
    var g = sheetGroup(box, '📋 Колонки списку, поки панель відкрита', '☑ — колонку видно; без галочки — сховано, поки панель відкрита (дані є в панелі).');
    var opts = el('div', 'mvp-sopts'), h = hideCols();
    columns.forEach(function (c) {
      opts.appendChild(checkBox(c.label, h.indexOf(c.name) < 0, 'col:' + c.name, function (v) { setColHidden(c.name, !v); }));
    });
    g.appendChild(opts);
    var r = el('div', 'mvp-srow');
    r.appendChild(sheetBtn('↺ Як у Minerva', 'cols-default', function () { change({ hide_cols: null }); renderSheet(); }));
    r.appendChild(sheetBtn('👁 Показувати всі', 'cols-all', function () { change({ hide_cols: [] }); renderSheet(); }));
    g.appendChild(r);
  }
  function sheetFoot(box) {
    var g = sheetGroup(box, '↺ Як було');
    var r = el('div', 'mvp-srow');
    r.appendChild(sheetBtn('📑 Розділи', 'r-blocks', function () { reset('blocks'); }));
    r.appendChild(sheetBtn('⚡ Швидкі дії', 'r-actions', function () { reset('actions'); }));
    r.appendChild(sheetBtn('🔠 Вигляд для всіх списків', 'r-layout', function () { reset('layout'); }));
    r.appendChild(sheetBtn('↺ Усе в цьому списку', 'r-list', function () { reset('list'); }));
    g.appendChild(r);
    var r2 = el('div', 'mvp-srow');
    r2.appendChild(sheetBtn('✕ Вимкнути панель у цьому списку', 'off', function () { setOn(false); }, 'is-danger'));
    r2.appendChild(sheetBtn('⌨ Клавіші…', 'keys', function () { keysSheet(); }));
    g.appendChild(r2);
  }
  function sheetShell(title, kind) {
    var sh = el('div', 'mvp-sheet');
    sh.setAttribute('data-kind', kind);
    sh.setAttribute('role', 'region');
    sh.setAttribute('aria-label', title);
    var head = el('div', 'mvp-sheet-h'), h = el('h3', '', title);
    h.tabIndex = -1;
    head.appendChild(h);
    var done = button('mvp-btn is-pri', '✓ Готово', 'Готово (Esc)');
    done.setAttribute('data-fk', 'done');
    done.addEventListener('click', function () { closeSheet(); });
    head.appendChild(done);
    sh.appendChild(head);
    return { el: sh, head: h, body: sh };
  }
  function openSheet(focusGroup) {
    if (!S.shell || !bodyEl()) { withShell(function () { openSheet(focusGroup); }); return; }
    if (S.editor) closeEditor(true);
    var back = S.sheet ? S.sheet.back : (rememberSection(), { sec: S.sec, offset: S.offset });
    if (S.sheet) closeSheet(true, true);
    var box = sheetShell(fmt('⚙ Панель «{0}» під себе', cfg.name || ''), 'settings');
    S.sheet = { el: box.el, back: back, kind: 'settings' };
    S.shell.insertBefore(box.el, bodyEl());
    S.shell.classList.add('mvp-sheet-on');
    fillSheet();
    var target = focusGroup === 'actions' ? box.el.querySelector('[data-list="actions"]') :
      focusGroup === 'sections' ? box.el.querySelector('[data-list="blocks"]') : null;
    if (target) target.closest('.mvp-sg').scrollIntoView({ block: 'start' });
    focusOn(box.head);
  }
  function fillSheet() {
    var sh = S.sheet && S.sheet.el;
    if (!sh || S.sheet.kind !== 'settings') return;
    while (sh.children.length > 1) sh.removeChild(sh.lastChild);
    sheetSections(sh);
    sheetActions(sh);
    sheetLook(sh);
    sheetColumns(sh);
    sheetFoot(sh);
  }
  function renderSheet() {                          // перебудувати, лишивши прокрутку й фокус
    var st = S.sheet;
    if (!st || st.kind !== 'settings') return;
    var top = st.el.scrollTop, act = document.activeElement, fk = act && st.el.contains(act) ? attr(act, 'data-fk') : '';
    fillSheet();
    st.el.scrollTop = top;
    if (fk) {
      var x = st.el.querySelector('[data-fk="' + fk.replace(/["\\]/g, '') + '"]');
      if (x && !x.disabled) focusOn(x); else focusOn(st.el.querySelector('.mvp-sheet-h h3'));
    }
  }
  function refreshSheet() { renderSheet(); }
  function keysSheet() {
    if (!S.shell || !bodyEl()) { withShell(keysSheet); return; }
    if (S.editor) closeEditor(true);
    var back = S.sheet ? S.sheet.back : (rememberSection(), { sec: S.sec, offset: S.offset });
    if (S.sheet) closeSheet(true, true);
    var box = sheetShell('⌨ Клавіші панелі', 'keys');
    S.sheet = { el: box.el, back: back, kind: 'keys' };
    var t = el('table', 'mvp-keys'), tb = el('tbody');
    function row(keys, what) {
      var tr = el('tr'), th = el('th');
      keys.forEach(function (k, i) {
        if (i) th.appendChild(document.createTextNode(' / '));
        th.appendChild(el('kbd', '', k));
      });
      tr.appendChild(th);
      tr.appendChild(el('td', '', what));
      tb.appendChild(tr);
    }
    row(['↓', 'J'], 'Наступний запис (на останньому — ще раз: наступна сторінка)');
    row(['↑', 'K'], 'Попередній запис');
    row(['Enter', 'O'], 'Відкрити картку');
    row(['Ctrl+Enter'], 'Картка в новій вкладці');
    row(['1…9'], 'Розділ за номером');
    row(['[', ']'], 'Попередній / наступний розділ');
    row(['R'], 'Оновити панель');
    row(['.'], 'До підменю розділів / назад до рядка');
    row(['?'], 'Ці підказки');
    row(['Esc'], 'Закрити: редагування, налаштування, панель');
    (cfg.edits || []).forEach(function (e) { if (e.hotkey) row([keyName(e.hotkey)], e.label); });
    (cfg.actions || []).forEach(function (a) {
      if (a.hotkey && !(cfg.edits || []).some(function (e) { return e.key === a.key; })) row([keyName(a.hotkey)], a.label);
    });
    t.appendChild(tb);
    box.el.appendChild(t);
    box.el.appendChild(el('p', 'mvp-sg-note', 'Літери працюють і на українській розкладці. Правий клік — меню; Shift + правий клік — меню браузера.'));
    S.shell.insertBefore(box.el, bodyEl());
    S.shell.classList.add('mvp-sheet-on');
    focusOn(box.head);
  }
  function closeSheet(silent, swap) {
    var st = S.sheet;
    if (!st) return;
    S.sheet = null;
    st.el.remove();
    if (!S.shell) return;
    S.shell.classList.remove('mvp-sheet-on');
    if (swap) return;
    buildNav();
    observeSpy();
    observeLazy();
    restore(st.back);                               // той самий розділ і місце
    fitSoon();
    if (!silent) focusOn(S.shell.querySelector('[data-mvp-menu]') || panel);
  }

  /* ── перетягування: фішки підменю, рядки налаштувань ── */
  function dragList(box, sel, axis, onDrop) {
    box.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || e.pointerType === 'touch') return;
      var item = e.target.closest && e.target.closest(sel);
      if (!item || !box.contains(item)) return;
      if (!item.classList.contains('mvp-ch') && !item.classList.contains('mvp-sdiv') && !e.target.closest('.mvp-drag')) return;
      if (item.classList.contains('mvp-ch-more')) return;
      var x0 = e.clientX, y0 = e.clientY, on = false, line = null, idx = -1, pid = e.pointerId;
      var ax = typeof axis === 'function' ? axis() : axis;          // фішки збоку — стовпчиком (y)
      function items() { return [].slice.call(box.querySelectorAll(sel)).filter(function (n) { return !n.hidden && !n.classList.contains('mvp-ch-more'); }); }
      function begin() {
        on = true;
        S.dragging = true;
        item.classList.add('is-dragging');
        line = el('div', 'mvp-dropline');
        box.appendChild(line);
        try { item.setPointerCapture(pid); } catch (x) { /* ні */ }
        addEventListener('keydown', esc, true);
      }
      function move(ev) {
        if (ev.pointerId !== pid) return;
        if (!on) {
          if (Math.abs(ev.clientX - x0) < DRAG_MIN && Math.abs(ev.clientY - y0) < DRAG_MIN) return;
          begin();
        }
        ev.preventDefault();
        var all = items(), p = ax === 'x' ? ev.clientX : ev.clientY, z = zoom(), br = box.getBoundingClientRect();
        if (!all.length) return;
        idx = all.length;
        for (var i = 0; i < all.length; i++) {
          var r = all[i].getBoundingClientRect(), mid = ax === 'x' ? r.left + r.width / 2 : r.top + r.height / 2;
          if (p < mid) { idx = i; break; }
        }
        var ref = all[Math.min(idx, all.length - 1)], rr = ref.getBoundingClientRect(), after = idx >= all.length;
        if (ax === 'x') {
          line.style.cssText = 'top:' + ((rr.top - br.top) / z + box.scrollTop + 4) + 'px;height:' + (rr.height / z - 8) +
            'px;width:2px;left:' + (((after ? rr.right : rr.left) - br.left) / z + box.scrollLeft - 1) + 'px';
        } else {
          line.style.cssText = 'left:' + ((rr.left - br.left) / z + 4) + 'px;width:' + (rr.width / z - 8) +
            'px;height:2px;top:' + (((after ? rr.bottom : rr.top) - br.top) / z + box.scrollTop - 1) + 'px';
        }
      }
      function end(cancel) {
        document.removeEventListener('pointermove', move, true);
        document.removeEventListener('pointerup', up, true);
        document.removeEventListener('pointercancel', cancelIt, true);
        removeEventListener('keydown', esc, true);
        if (!on) return;
        S.dragging = false;
        S.noClick = now() + 350;                    // клік після перетягування — не клік
        item.classList.remove('is-dragging');
        if (line) line.remove();
        try { item.releasePointerCapture(pid); } catch (x) { /* ні */ }
        if (!cancel && idx >= 0) {
          var all = items(), from = all.indexOf(item);
          if (idx !== from && idx !== from + 1) onDrop(item, idx);
        }
      }
      function up(ev) { if (ev.pointerId === pid) end(false); }
      function cancelIt(ev) { if (ev.pointerId === pid) end(true); }
      function esc(ev) { if (ev.key === 'Escape' || ev.key === 'Esc') { ev.preventDefault(); ev.stopPropagation(); end(true); } }
      document.addEventListener('pointermove', move, true);
      document.addEventListener('pointerup', up, true);
      document.addEventListener('pointercancel', cancelIt, true);
    });
  }
  function chipDrop(item, idx) {                    // фішку перенесли — розділи в тому самому порядку
    var keys = chips().filter(function (c) { return !c.classList.contains('mvp-ch-more'); }).map(function (c) { return attr(c, 'data-go'); });
    var key = attr(item, 'data-go'), from = keys.indexOf(key);
    if (from < 0) return;
    keys.splice(from, 1);
    keys.splice(idx > from ? idx - 1 : idx, 0, key);
    var shown = shownBlocks();
    var hiddenTail = shown.filter(function (k) { return keys.indexOf(k) < 0; });   // фішки в «⋯»
    setBlocks(keys.concat(hiddenTail));
  }

  /* ── ширина: ліва межа панелі ── */
  function wireGrip() {
    grip.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || !S.open) return;
      e.preventDefault();
      var z = zoom(), x0 = e.clientX, w0 = S.shown || wantWidth(), last = w0, pid = e.pointerId, moved = false;
      var row = S.row, top0 = anchorTop(row), label = el('div', 'mvp-wlabel', fmt('{0} px', w0));
      panel.appendChild(label);
      root.classList.add('mvp-resizing');
      try { grip.setPointerCapture(pid); } catch (x) { /* ні */ }
      function mv(ev) {
        if (ev.pointerId !== pid) return;
        var raw = w0 + (x0 - ev.clientX) / z, w = Math.round(raw / 10) * 10;
        PRESETS.forEach(function (p) { if (Math.abs(raw - p) <= SNAP) w = p; });
        w = Math.max(W_MIN, Math.min(w, widthCap()));
        if (w !== last) { last = w; moved = true; previewWidth(w); label.textContent = fmt('{0} px', w); }
      }
      function done(cancel) {
        document.removeEventListener('pointermove', mv, true);
        document.removeEventListener('pointerup', upH, true);
        document.removeEventListener('pointercancel', cancelH, true);
        removeEventListener('keydown', escH, true);
        root.classList.remove('mvp-resizing');
        label.remove();
        try { grip.releasePointerCapture(pid); } catch (x) { /* ні */ }
        if (cancel || !moved) { layout(); reanchor(row, top0); return; }
        setWidth(last);
        reanchor(row, top0);
      }
      function upH(ev) { if (ev.pointerId === pid) done(false); }
      function cancelH(ev) { if (ev.pointerId === pid) done(true); }
      function escH(ev) { if (ev.key === 'Escape' || ev.key === 'Esc') { ev.preventDefault(); ev.stopPropagation(); done(true); } }
      document.addEventListener('pointermove', mv, true);
      document.addEventListener('pointerup', upH, true);
      document.addEventListener('pointercancel', cancelH, true);
      addEventListener('keydown', escH, true);
    });
    grip.addEventListener('dblclick', function () { setWidth(null); toast(fmt('Ширина — авто: {0} px', autoWidth())); });
    grip.addEventListener('keydown', function (e) {
      var w = S.shown || wantWidth(), k = e.key, d = e.shiftKey ? 80 : 20, v = null;
      if (k === 'ArrowLeft' || k === 'Left') v = w + d;
      else if (k === 'ArrowRight' || k === 'Right') v = w - d;
      else if (k === 'Home') v = W_MIN;
      else if (k === 'End') v = widthCap();
      else if (k === 'Enter') { e.preventDefault(); e.stopPropagation(); setWidth(null); return; }
      else return;
      e.preventDefault();
      e.stopPropagation();
      setWidth(Math.max(W_MIN, Math.min(v, widthCap())));
    });
  }

  /* ── події сторінки ── */
  function onDocClick(e) {
    if (!S.inited) return;
    var t = e.target;
    if (!t || !t.closest || (panel && panel.contains(t))) return;
    if (!prefs.on || !wide() || e.defaultPrevented) return;
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var row = t.closest(ROWS);
    if (!row) return;
    var hit = t.closest(NO_OPEN);
    if (hit && row.contains(hit)) return;
    var sel = window.getSelection && getSelection();
    if (sel && !sel.isCollapsed && String(sel).trim()) return;   // виділяли текст
    var pk = pkOf(row);
    if (!pk) return;
    S.edgeDir = 0;
    open(pk, { row: row, dir: 1 });
  }
  function onDocDblClick(e) {
    if (!S.inited || !prefs.on || !wide()) return;
    var t = e.target;
    if (!t || !t.closest || (panel && panel.contains(t))) return;
    var row = t.closest(ROWS);
    if (!row || t.closest('a, button, input, select, textarea, label, [data-svl], [contenteditable], [data-no-panel]')) return;
    var u = changeLinkOf(row) || (S.pk === pkOf(row) ? cardHref(S.pk) : '');
    if (!u) return;
    hashUpdate();
    location.href = u;
  }
  function onPanelClick(e) {
    var t = e.target, x;
    if (!t || !t.closest || t.closest('.mvp-ed-form')) return;
    if ((x = t.closest('[data-mvp-close]'))) { close(); return; }
    if ((x = t.closest('[data-mvp-nav]'))) { if (!x.disabled) step(+attr(x, 'data-mvp-nav') || 1, 'click'); return; }
    if ((x = t.closest('[data-mvp-menu]'))) { gearMenu(x); return; }
    if ((x = t.closest('[data-mvp-refresh]'))) { refresh(); return; }
    if ((x = t.closest('[data-mvp-keys]'))) { keysSheet(); return; }
    if ((x = t.closest('[data-mvp-pos]'))) { if (S.row && S.row.isConnected) S.row.scrollIntoView({ block: 'center' }); return; }
    if ((x = t.closest('[data-mvp-retry]'))) { var s = x.closest('.mvp-sec'); if (s) ensureLoaded(s, true); return; }
    if ((x = t.closest('.mvp-nav .mvp-ch'))) { chipClick(x); return; }
    if ((x = t.closest('.mvp-sh-btn'))) {
      var sec = x.closest('.mvp-sec');                // порожній розділ «одним рядком» — нічого розгортати
      if (sec && !(sec.hasAttribute('data-empty') && prefs.empty !== 'show')) toggleSec(sec, true);
      return;
    }
    if ((x = t.closest('[data-mvp-more]'))) { moreMenu(x); return; }
    if ((x = t.closest('[data-copy]'))) { copyText(attr(x, 'data-copy')); return; }
    if ((x = t.closest('[data-edit]'))) {
      if (t.closest('a[href]') && !t.closest('.mvp-ed-btn')) return;
      var kind = attr(x, 'data-kind');
      if (kind !== 'choice' && kind !== 'bool' && !t.closest('.mvp-ed-btn')) {
        // значення поля (трек-номер, строк…) можна виділити й скопіювати — змінює ✏️; нотатку — клік будь-де
        var picked = window.getSelection && getSelection();
        if (!x.classList.contains('mvp-ed-memo') || (picked && !picked.isCollapsed && String(picked).trim())) return;
      }
      editClick(x);
      return;
    }
    if ((x = t.closest('[data-admin-action]'))) { runAdmin(attr(x, 'data-admin-action'), attr(x, 'data-confirm')); return; }
    if ((x = t.closest('[data-svl]'))) {
      if (!window.svLabel) { e.preventDefault(); var fb = attr(x, 'data-fallback'); if (fb) location.href = fb; }
      return;                                        // є svLabel — друк сам (labels/_label_actions.html)
    }
    if ((x = t.closest('.mvp-act.is-off'))) { toast(x.title || 'Зараз недоступно.'); return; }
    if ((x = t.closest('[data-mvp-clamp]'))) { if (!t.closest('a')) x.classList.toggle('is-open'); return; }
    if ((x = t.closest('a[href]'))) hashUpdate();    // перехід — «Назад» поверне панель на цей запис
  }
  function onContextMenu(e) {
    if (!S.inited || e.shiftKey || !window.MVMenu) return;
    var t = e.target;
    if (!t || !t.closest || t.closest('.mvm')) return;
    var sel = window.getSelection && getSelection();       // виділений текст — меню браузера («Копіювати»)
    if (sel && !sel.isCollapsed && String(sel).trim() && sel.containsNode && sel.containsNode(t, true)) return;
    // заголовок колонки, поки панель відкрита: своє меню й на посиланні сортування (Shift — меню браузера)
    var th = S.open && wide() ? t.closest('#result_list thead th[class*="column-"]') : null;
    if (th && colName(th) && !t.closest('input, select, textarea, [data-own-menu]')) {
      e.preventDefault(); colMenu(th, e); return;
    }
    if (t.closest(NO_MENU)) return;                  // посилання, поля, етикетки, свої меню — меню браузера / своє
    if (panel && panel.contains(t)) { if (panelMenu(t, e)) e.preventDefault(); return; }
    if (pill && pill.contains(t)) { e.preventDefault(); pillMenu(e); return; }
    if (!wide()) return;
    if (!prefs.on) return;
    var row = t.closest(ROWS);
    if (row && pkOf(row)) { e.preventDefault(); rowMenu(row, e); }
  }
  function snapKeys(e) {                             // стан шарів до того, як їхні обробники щось закрили
    S.snap = { t: e.timeStamp, key: e.key, layer: layerOpen() };
  }
  function enterTarget(t) {
    return !t || t === document.body || t === root || t === panel ||
      !!(t.matches && (t.matches(ROWS) || t.matches('.mvp-body, .mvp-shell, .mvp-wrap, .mvp-title, .mvp-sheet')));
  }
  function onKey(e) {
    if (!S.inited || e.isComposing) return;
    var snap = S.snap && S.snap.t === e.timeStamp && S.snap.key === e.key ? S.snap : { layer: layerOpen() };
    if (e.key === 'Escape' || e.key === 'Esc') { if (!e.defaultPrevented) onEsc(e, snap); return; }
    if (!S.open || snap.layer || e.defaultPrevented || S.dragging) return;
    var t = e.target, code = e.code || '';
    if (typing(t) || e.altKey || e.metaKey) return;
    if (t && t.closest && t.closest(KEY_SKIP)) return;
    if (e.ctrlKey) {
      if ((code === 'Enter' || code === 'NumpadEnter') && enterTarget(t) && !S.sheet) { e.preventDefault(); openCard(true); }
      return;
    }
    if (code === 'Slash' && e.shiftKey) { e.preventDefault(); if (S.sheet && S.sheet.kind === 'keys') closeSheet(); else keysSheet(); return; }
    if (S.sheet) return;                             // у налаштуваннях клавіші — звичайні
    if (t && t.closest && t.closest('.mvp-grip')) return;
    switch (code) {
      case 'ArrowDown': case 'KeyJ': e.preventDefault(); step(1, 'key'); return;
      case 'ArrowUp': case 'KeyK': e.preventDefault(); step(-1, 'key'); return;
      case 'Enter': case 'NumpadEnter': case 'KeyO':     // фокус на кнопці / посиланні — Enter натискає її
        if ((code === 'KeyO' && e.shiftKey) || !enterTarget(t)) return;
        e.preventDefault(); openCard(false); return;
      case 'BracketLeft': e.preventDefault(); stepSec(-1); return;
      case 'BracketRight': e.preventDefault(); stepSec(1); return;
      case 'KeyR': if (e.shiftKey) return; e.preventDefault(); refresh(); return;
      case 'Period': e.preventDefault(); periodKey(); return;
      default: break;
    }
    if (/^Digit[1-9]$/.test(code) && !e.shiftKey) { e.preventDefault(); goNth(+code.slice(5)); return; }
    if (e.shiftKey) return;
    var fn = hotkey(code);
    if (fn) { e.preventDefault(); fn(); }
  }
  function hotkey(code) {
    var eds = cfg.edits || [], acts = cfg.actions || [];
    for (var i = 0; i < eds.length; i++) {
      if (eds[i].hotkey === code) { var ek = eds[i].key; return function () { triggerEdit(ek); }; }
    }
    for (var j = 0; j < acts.length; j++) {
      if (acts[j].hotkey === code) { var ak = acts[j].key; return function () { triggerAction(ak); }; }
    }
    return null;
  }
  function triggerAction(key) {
    if (switching()) { waitToast(); return; }
    var b = actbar(), it = b && b.querySelector(':scope > [data-act="' + key + '"]');
    if (!it) { toast('Для цього запису ця дія недоступна.'); return; }
    if (it.classList.contains('is-off')) { toast(it.title || 'Зараз недоступно.'); return; }
    if (it.hasAttribute('data-edit')) { editClick(it); return; }
    if (it.hasAttribute('data-admin-action')) { runAdmin(attr(it, 'data-admin-action'), attr(it, 'data-confirm')); return; }
    if (it.tagName === 'A') hashUpdate();
    it.click();                                      // посилання — перехід; етикетка — svLabel або запасна адреса
  }
  function periodKey() {
    var a = document.activeElement;
    if (a && panel.contains(a) && a !== panel) { if (S.row && S.row.isConnected) focusRow(S.row); else focusOn(panel); return; }
    var c = navEl() && navEl().querySelector('.mvp-ch.is-active');
    focusOn(c && !c.hidden ? c : (S.shell && S.shell.querySelector('.mvp-title')) || panel);
  }
  function onEsc(e, snap) {                          // Esc — лише верхній шар
    if (snap.layer || S.dragging) return;
    var a = document.activeElement;
    if (S.editor) { e.preventDefault(); closeEditor(); return; }
    if (S.sheet) { e.preventDefault(); closeSheet(); return; }
    if (!S.open) return;
    if (typing(a) && !(panel && panel.contains(a))) return;   // поле на сторінці — не наше
    if (a && panel.contains(a) && a !== panel) {
      e.preventDefault();
      if (S.row && S.row.isConnected) focusRow(S.row); else focusOn(panel);
      return;
    }
    e.preventDefault();
    close();
  }
  function onResize() {
    clearTimeout(S.rzT);
    S.rzT = setTimeout(function () {
      syncOn();
      if (!wide()) { if (S.open) close(); return; }
      if (S.open) { relayout(); colNoteUpdate(); }
    }, 150);
  }
  function onRailChange() {                          // бокове меню звузилось / розширилось
    var sig = (root.classList.contains('mvd-rail') ? 'r' : 'w') + '|' + (root.getAttribute('data-mv-sidebar') || '');
    if (sig === S.railSig) return;
    S.railSig = sig;
    if (S.open) relayout();
  }

  /* ── побудова ── */
  // «📌 Панель» — у рядку пошуку (#changelist-search > div; CSS order 8, margin-left: auto — перед «⚙ Фільтри»),
  // інакше #toolbar, інакше свій рядок div.mvp-tbar над списком; ніколи не в #changelist (рядок flex — забрав би
  // ширину списку). Список без пошуку: mv_desktop.js ставить «⚙ Фільтри» у свій рядок .mvd-tbar — тоді туди ж
  // (adoptToolbar), щоб над таблицею не було двох рядків кнопок.
  function buildPill() {
    pill = button('mvp-pill-toggle', '📌 Панель');
    pill.id = 'mvp-toggle';
    pill.setAttribute('aria-pressed', prefs.on ? 'true' : 'false');
    var host = document.querySelector('#changelist-search > div') || document.getElementById('toolbar');
    if (!host) {
      var box = document.querySelector('#changelist .changelist-form-container') || document.getElementById('content-main');
      if (box) {
        host = box.querySelector(':scope > .mvd-tbar');
        if (host) host.classList.add('mvp-tbar');
        else { host = el('div', 'mvp-tbar'); box.insertBefore(host, box.firstChild); }
      }
    }
    if (!host) return;
    host.appendChild(pill);
    colNote = el('span', 'mvp-colnote');
    colNote.hidden = true;
    colNote.appendChild(el('span', 'mvp-colnote-t'));
    var dot = el('span', 'mvp-colnote-s', '·');
    dot.setAttribute('aria-hidden', 'true');
    colNote.appendChild(dot);
    var show = button('mvp-colnote-b', 'показати');
    show.title = 'Показувати всі колонки, поки панель відкрита';
    show.addEventListener('click', function () { change({ hide_cols: [] }); });
    colNote.appendChild(show);
    host.appendChild(colNote);
    pill.addEventListener('click', function () { setOn(!prefs.on); });
  }
  function adoptToolbar() {                         // після mv_desktop.js (DOMContentLoaded): один рядок кнопок
    var own = pill && pill.parentNode;
    if (!own || !own.classList || !own.classList.contains('mvp-tbar') || own.classList.contains('mvd-tbar')) return;
    var theirs = own.parentNode && own.parentNode.querySelector(':scope > .mvd-tbar');
    if (!theirs || theirs === own) return;
    theirs.classList.add('mvp-tbar');
    theirs.appendChild(pill);
    if (colNote) theirs.appendChild(colNote);
    if (!own.children.length) own.remove();
  }
  function init() {
    if (S.inited) return;
    S.inited = true;
    panel = el('aside');
    panel.id = 'mv-panel';
    panel.setAttribute('role', 'complementary');
    panel.setAttribute('aria-label', 'Подробиці запису');
    panel.setAttribute('aria-labelledby', 'mvp-title');
    panel.tabIndex = -1;
    grip = el('div', 'mvp-grip');
    grip.setAttribute('role', 'separator');
    grip.setAttribute('aria-orientation', 'vertical');
    grip.setAttribute('aria-label', 'Ширина панелі');
    grip.setAttribute('aria-valuemin', String(W_MIN));
    grip.setAttribute('aria-valuemax', String(W_MAX));
    grip.setAttribute('aria-valuenow', String(wantWidth()));
    grip.tabIndex = 0;
    grip.title = 'Потягніть, щоб змінити ширину · подвійний клік — авто · ← → з клавіатури';
    var load = el('div', 'mvp-load');
    load.setAttribute('aria-hidden', 'true');
    wrap = el('div', 'mvp-wrap');
    panel.appendChild(grip);
    panel.appendChild(load);
    panel.appendChild(wrap);
    document.body.appendChild(panel);
    live = el('div', 'mvp-vh');
    live.id = 'mvp-live';
    live.setAttribute('aria-live', 'polite');
    live.setAttribute('aria-atomic', 'true');
    document.body.appendChild(live);
    colStyle = el('style');
    colStyle.id = 'mvp-cols';
    document.head.appendChild(colStyle);
    buildPill();
    function later() { setTimeout(adoptToolbar, 0); }
    document.addEventListener('DOMContentLoaded', later);
    addEventListener('load', later);
    applyLook();
    applyCols();
    syncOn();
    panel.addEventListener('click', onPanelClick);
    panel.addEventListener('keydown', onNavKey);
    panel.addEventListener('input', function (e) {
      var f = e.target && e.target.closest && e.target.closest('input[data-mvp-filter]');
      if (f) filterRows(f);
    });
    dragList(panel, '.mvp-nav .mvp-ch', function () { return navMode() === 'side' ? 'y' : 'x'; },
             function (item, idx) { chipDrop(item, idx); });
    wireGrip();
    if (window.ResizeObserver) new ResizeObserver(function () { if (S.open) fitSoon(); }).observe(panel);
    addEventListener('resize', onResize);
    addEventListener('scroll', function () {
      if (!S.open || S.topRaf) return;
      S.topRaf = requestAnimationFrame(function () { S.topRaf = 0; topUpdate(); });
    }, { passive: true });
    document.addEventListener('mv:sidebar', function () { S.railSig = ''; onRailChange(); });
    if (window.MutationObserver) {
      S.railSig = (root.classList.contains('mvd-rail') ? 'r' : 'w') + '|' + (root.getAttribute('data-mv-sidebar') || '');
      new MutationObserver(onRailChange).observe(root, { attributes: true, attributeFilter: ['class', 'data-mv-sidebar'] });
    }
    addEventListener('pagehide', function () { if (S.open) hashUpdate(); });
    // «Назад» з картки, сторінку взято з кешу браузера: панель ще відкрита — показати свіже (запис могли змінити)
    addEventListener('pageshow', function (e) { if (e.persisted && S.open) { cacheClear(); refresh(); } });
    reopen();
  }
  function waitWide() { clearTimeout(S.wT); S.wT = setTimeout(start, 150); }
  function start() {
    if (S.inited || !document.body) return;
    if (!wide()) {                                  // вузько: рядки — як раніше; ширше — тоді й увімкнемо
      if (!S.waiting) { S.waiting = true; addEventListener('resize', waitWide); }
      return;
    }
    if (S.waiting) { S.waiting = false; removeEventListener('resize', waitWide); }
    init();
  }

  // обробники документа — одразу (раніше за mv_desktop.js), щоб Esc закривав лише верхній шар
  addEventListener('keydown', snapKeys, true);
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', onDocClick);
  document.addEventListener('dblclick', onDocDblClick);
  document.addEventListener('contextmenu', onContextMenu);

  window.MVPanel = {
    open: function (pk, opts) { return open(pk, opts || {}); },
    close: close,
    isOpen: function () { return !!S.open; },
    current: function () { return S.open && S.pk ? { model: cfg.model, pk: S.pk } : null; },
    refresh: refresh,
    config: function () { var c = clone(cfg); c.prefs = clone(prefs); return c; }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
