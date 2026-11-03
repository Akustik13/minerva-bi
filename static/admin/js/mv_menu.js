/* 🧭 Спільне меню Minerva — правий клік і кнопки ⚙ / ⋯ у панелі праворуч (mv_panel.js) і в боковому меню
 * (mv_desktop.js). Стилі — static/admin/css/mv_menu.css.
 *
 *   MVMenu.open(at, items, opts) → {el, close, refresh}
 *       at — елемент (меню під ним; opts.side: bottom / bottom-end / top / top-end / right / left), {x, y} або
 *       подія миші (меню в цій точці). Повторний виклик з тим самим елементом закриває меню (кнопка-перемикач);
 *       тоді el = null. Елемент з aria-haspopup отримує aria-expanded.
 *       opts: title, titleData (true — заголовок без перекладу; рядок — дані після заголовка), label (назва для
 *       читачів екрана), returnFocus (куди повернути фокус; false — нікуди), onClose(причина), className,
 *       focus (first / checked / none).
 *   MVMenu.close(), MVMenu.isOpen(), MVMenu.refresh() — перемалювати відкриті рівні (змінився порядок чи пункти).
 *   MVMenu.toast(text, {undo, action: {label, run}, ms}) → {close} — повідомлення внизу по центру, одне за раз;
 *       «↶ Скасувати» — ще й Ctrl Z; наведення чи фокус зупиняють відлік.
 *
 * Пункти (label, hint, icon, checked, disabled, title, hidden, href, items — значення або функція):
 *   {label, icon, hint, kbd, onSelect, disabled, danger, title, href, target, data, close} — виконати й закрити;
 *   {type: 'check', label, checked, onToggle(next)}, {type: 'radio', group, label, checked, onSelect} — меню
 *       лишається відкритим (close: true — закрити);
 *   {type: 'submenu', label, icon, hint, items | () => items};
 *   {type: 'title', label, data}, {type: 'separator'}, {type: 'hint', label};
 *   {type: 'row', label, buttons: [{label, aria, onClick, disabled, title}]} — кнопки ⬆ ⬇ у рядку; buttons можна
 *       дати й звичайному пункту, check чи radio. Після кнопки рівень перемальовується, фокус — на тій самій кнопці.
 *   data: true — текст без перекладу (дані людини); id — щоб фокус тримався пункту при перемальовуванні.
 *   Колбеки отримують ctx = {item, event, el, close, refresh} (onToggle — next, ctx).
 *
 * Розмітка: кожен рівень — div.mvm[role="menu"] у body (підменю — окремий div.mvm поруч), пункти — button.mvm-item з
 * role="menuitem" / "menuitemcheckbox" / "menuitemradio" і aria-checked, підменю — aria-haspopup="menu" і
 * aria-expanded. Клавіші: ↑ ↓ Home End; → Enter Пробіл — відкрити підменю; ← Esc — рівень назад; Tab — закрити;
 * перша літера (після емодзі) — до пункту. Миша: підменю — після 120 мс, рух по діагоналі до підменю — 300 мс
 * запасу. Закривається від натискання поза меню, втрати фокусу вікном, зміни розміру й прокрутки сторінки.
 * Тексти — лише textContent. Координати діляться на zoom тіла сторінки («Розмір тексту» 112–150 %).
 */
(function () {
  'use strict';
  if (window.MVMenu) return;

  var OPEN_MS = 120, GRACE_MS = 300, TOAST_MS = 6000, GAP = 4, EDGE = 8, MAX_W = 380, MIN_W = 220;
  var ITEM = '.mvm-item, .mvm-btn';
  var LEAD = /^[^\p{L}\p{N}]+/u;                 // емодзі, значки й пробіли перед першою літерою
  var state = null, seq = 0, grace = null, graceT = 0, lastPt = null, listening = false;
  var memo = Object.create(null);                // текст → як його показано (після перекладу): без миготіння

  function noop() {}
  function zoom() { return parseFloat(getComputedStyle(document.body).zoom) || 1; }
  function val(v, it) {
    if (typeof v !== 'function') return v;
    try { return v(it); } catch (err) { if (window.console) console.error(err); return undefined; }
  }
  function str(v) { return v == null || v === false ? '' : String(v); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = String(text);
    return e;
  }
  function uid(p) { seq += 1; return 'mvm-' + p + seq; }
  function call(fn) {
    if (typeof fn !== 'function') return undefined;
    try { return fn.apply(null, [].slice.call(arguments, 1)); }
    catch (err) { if (window.console) console.error(err); }
    return undefined;
  }
  function focusOn(e) { if (e && typeof e.focus === 'function') e.focus({ preventScroll: true }); }
  function inside(t) {
    if (!state || !t || t.nodeType !== 1) return false;
    for (var i = 0; i < state.levels.length; i++) if (state.levels[i].el.contains(t)) return true;
    return false;
  }
  function ctx(lv, it, ev) {
    return { item: it, event: ev || null, el: lv ? lv.el : null, close: api.close, refresh: refresh };
  }

  /* ── тексти: показуємо вже перекладене, якщо цей напис був на сторінці (mv_translate) ── */
  function shown(raw) { var s = memo[raw]; return s === undefined ? raw : s; }
  function setTxt(lv, node, raw) {
    if (node._mvmRaw === raw) return;
    if (node._mvmRaw === undefined) lv.texts.push([node, '']);
    node._mvmRaw = raw;
    node.textContent = shown(raw);
  }
  function setAttr(lv, node, name, raw) {
    var k = '_mvmA' + name;
    if (node[k] === raw) return;
    if (node[k] === undefined) lv.texts.push([node, name]);
    node[k] = raw;
    if (raw) node.setAttribute(name, shown(raw)); else node.removeAttribute(name);
  }
  function harvest(lv) {
    lv.texts.forEach(function (t) {
      var n = t[0], raw = t[1] ? n['_mvmA' + t[1]] : n._mvmRaw;
      var now = t[1] ? n.getAttribute(t[1]) : n.textContent;
      if (raw && now && (now !== raw || memo[raw] === undefined)) memo[raw] = now;   // ще не перекладене — не затирає
    });
  }

  /* ── пункти ── */
  function norm(src, owner) {
    var raw = val(src, owner) || [], out = [];
    for (var i = 0; i < raw.length; i++) {
      var it = raw[i];
      if (!it || typeof it !== 'object' || val(it.hidden, it)) continue;
      if (it.type === 'separator' && (!out.length || out[out.length - 1].type === 'separator')) continue;
      out.push(it);
    }
    while (out.length && out[out.length - 1].type === 'separator') out.pop();
    return out;
  }
  function keyOf(it) {
    if (it.id != null) return 'id:' + it.id;
    return (it.type || 'item') + ':' + str(val(it.label, it)) + ':' + str(it.group);
  }
  function isToggle(it) { return it.type === 'check' || it.type === 'radio'; }
  function isSub(m) { return !m.btn && m.item.type === 'submenu'; }

  function level(st, depth, source, owner, trigger) {
    var box = el('div', 'mvm' + (st.opts.className ? ' ' + st.opts.className : ''));
    box.id = uid('m');
    box.setAttribute('role', 'menu');
    box.tabIndex = -1;
    var lv = { st: st, depth: depth, el: box, source: source, owner: owner, trigger: trigger,
               items: [], stops: [], els: [], texts: [], timer: 0, pend: null, titleId: '', quiet: 0 };
    box._mvmLevel = lv;
    box.addEventListener('keydown', onKey);
    box.addEventListener('keyup', onKeyUp);
    box.addEventListener('click', onClick);
    box.addEventListener('pointermove', onMove);
    box.addEventListener('pointerout', onOut);
    box.addEventListener('pointerenter', onEnter);
    box.addEventListener('contextmenu', onCtx);
    box.addEventListener('wheel', onWheel, { passive: false });
    st.levels.push(lv);
    return lv;
  }
  function render(lv) {
    var st = lv.st, box = lv.el, items = norm(lv.source, lv.owner);
    harvest(lv);
    lv.items = items; lv.stops = []; lv.els = []; lv.texts = []; lv.titleId = '';
    lv.quiet = Date.now() + 150;                       // своя прокрутка після перемальовування — не закриває підменю
    box.textContent = '';
    if (!lv.depth) {
      if (str(st.opts.title)) box.appendChild(titleEl(lv, st.opts.title, st.opts.titleData, true));
      if (str(st.opts.label)) { box.removeAttribute('aria-labelledby'); setAttr(lv, box, 'aria-label', str(st.opts.label)); }
      else if (lv.titleId) { box.removeAttribute('aria-label'); box.setAttribute('aria-labelledby', lv.titleId); }
      else setAttr(lv, box, 'aria-label', 'Меню');
    }
    var ind = items.some(isToggle);
    items.forEach(function (it) {
      var t = it.type || 'item';
      if (t === 'separator') {
        var s = el('div', 'mvm-sep'); s.setAttribute('role', 'separator'); box.appendChild(s);
      } else if (t === 'title') {
        box.appendChild(titleEl(lv, val(it.label, it), it.data, false));
      } else if (t === 'hint') {
        var h = el('div', 'mvm-hint'); h.setAttribute('role', 'none');
        if (it.data) h.setAttribute('translate', 'no');
        setTxt(lv, h, str(val(it.label, it)));
        box.appendChild(h);
      } else if (t === 'row' || (it.buttons && it.buttons.length)) {
        box.appendChild(rowEl(lv, it, t === 'row' ? null : itemEl(lv, it, ind)));
      } else {
        var b = itemEl(lv, it, ind);
        stop(lv, [b]);
        box.appendChild(b);
      }
    });
    if (!lv.stops.length && !items.some(function (it) { return it.type === 'hint' || it.type === 'title'; })) {
      var e = el('div', 'mvm-hint'); e.setAttribute('role', 'none');
      setTxt(lv, e, 'Порожньо');
      box.appendChild(e);
    }
  }
  function titleEl(lv, text, data, top) {
    var t = el('div', 'mvm-title'), lab = el('span');
    t.setAttribute('role', 'none');
    setTxt(lv, lab, str(text));
    t.appendChild(lab);
    if (data === true) t.setAttribute('translate', 'no');
    else if (str(data)) {
      var d = el('span', 'mvm-data');
      d.setAttribute('translate', 'no');
      d.textContent = (str(text) ? ' ' : '') + str(data);
      t.appendChild(d);
    }
    if (top) { t.id = uid('t'); lv.titleId = t.id; }
    return t;
  }
  function itemEl(lv, it, ind) {
    var t = it.type || 'item', href = t === 'item' ? str(val(it.href, it)) : '';
    var b = el(href ? 'a' : 'button', 'mvm-item');
    if (href) {
      b.href = href;
      if (it.target) { b.target = it.target; b.rel = 'noopener'; }
    } else b.type = 'button';
    b.tabIndex = -1;
    b.setAttribute('role', t === 'check' ? 'menuitemcheckbox' : t === 'radio' ? 'menuitemradio' : 'menuitem');
    if (t === 'check') b.classList.add('mvm-check');
    if (t === 'radio') b.classList.add('mvm-radio');
    if (t === 'submenu') {
      b.classList.add('mvm-sub');
      b.setAttribute('aria-haspopup', 'menu');
      b.setAttribute('aria-expanded', 'false');
    }
    if (it.danger) b.classList.add('mvm-danger');
    if (ind) { var mk = el('span', 'mvm-mark'); mk.setAttribute('aria-hidden', 'true'); b.appendChild(mk); }
    var icon = str(val(it.icon, it));
    if (icon) { var ic = el('span', 'mvm-icon', icon); ic.setAttribute('aria-hidden', 'true'); b.appendChild(ic); }
    var tx = el('span', 'mvm-text');
    tx.id = uid('l');
    if (it.data) tx.setAttribute('translate', 'no');
    b.appendChild(tx);
    var vl = el('span', 'mvm-val');
    b.appendChild(vl);
    var kbd = str(it.kbd);
    if (kbd) {
      var k = el('kbd', 'mvm-kbd', kbd); k.setAttribute('aria-hidden', 'true'); b.appendChild(k);
      if (/^[A-Za-z0-9]$/.test(kbd)) b.setAttribute('aria-keyshortcuts', kbd);
    }
    if (t === 'submenu') { var ar = el('span', 'mvm-arrow', '▸'); ar.setAttribute('aria-hidden', 'true'); b.appendChild(ar); }
    b._mvm = { item: it, lv: lv, key: keyOf(it), text: tx, val: vl, stop: 0, col: 0, off: undefined };
    paint(b);
    return b;
  }
  function rowEl(lv, it, main) {
    var r = el('div', 'mvm-row' + (main ? ' mvm-row-item' : '')), els = [], rk = keyOf(it);
    r.setAttribute('role', 'group');
    if (main) { r.appendChild(main); els.push(main); }
    else {
      var lb = el('span', 'mvm-rtext');
      lb.id = uid('r');
      if (it.data) lb.setAttribute('translate', 'no');
      setTxt(lv, lb, str(val(it.label, it)));
      r.appendChild(lb);
      r.setAttribute('aria-labelledby', lb.id);
    }
    (it.buttons || []).forEach(function (bt, j) {
      if (!bt) return;
      var b = el('button', 'mvm-btn');
      b.type = 'button'; b.tabIndex = -1;
      b.setAttribute('role', 'menuitem');
      b._mvm = { item: bt, lv: lv, key: rk + '#' + j, btn: true, row: it, stop: 0, col: 0, off: undefined };
      paintBtn(b);
      r.appendChild(b);
      els.push(b);
    });
    if (els.length) stop(lv, els);
    return r;
  }
  function stop(lv, els) {
    var s = lv.stops.length;
    els.forEach(function (e, c) { e._mvm.stop = s; e._mvm.col = c; lv.els.push(e); });
    lv.stops.push(els);
  }
  function setOff(b, m, off) {
    if (m.off === off) return;
    m.off = off;
    b.classList.toggle('is-disabled', off);
    if (off) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
  }
  function paint(b) {
    var m = b._mvm, it = m.item;
    setTxt(m.lv, m.text, str(val(it.label, it)));
    setTxt(m.lv, m.val, str(val(it.hint, it)));
    setAttr(m.lv, b, 'title', str(val(it.title, it)));
    if (isToggle(it)) b.setAttribute('aria-checked', val(it.checked, it) ? 'true' : 'false');
    setOff(b, m, !!val(it.disabled, it));
  }
  function paintBtn(b) {
    var m = b._mvm, bt = m.item, aria = str(val(bt.aria, bt));
    setTxt(m.lv, b, str(val(bt.label, bt)));
    setAttr(m.lv, b, 'aria-label', aria);
    setAttr(m.lv, b, 'title', str(val(bt.title, bt)) || aria);
    setOff(b, m, !!val(bt.disabled, bt));
  }
  function sync(st) {
    st.levels.forEach(function (lv) {
      lv.els.forEach(function (e) { if (e._mvm.btn) paintBtn(e); else paint(e); });
    });
  }
  function byKey(lv, k) {
    for (var i = 0; i < lv.els.length; i++) if (lv.els[i]._mvm.key === k) return lv.els[i];
    return null;
  }

  /* ── де показати: координати вікна → px сторінки (поділити на zoom) ── */
  function bounds(box, z) {
    var room = innerWidth - 2 * EDGE;
    box.style.maxHeight = Math.max(80, Math.floor(innerHeight * 0.7 / z)) + 'px';
    box.style.maxWidth = Math.floor(Math.min(MAX_W, room / z)) + 'px';
    box.style.minWidth = Math.floor(Math.min(MIN_W, room / z)) + 'px';
    box.style.left = '0px';
    box.style.top = '0px';
    return box.getBoundingClientRect();
  }
  function shrink(box, room, z) {
    box.style.maxHeight = Math.max(60, Math.floor(room / z)) + 'px';
    return box.getBoundingClientRect().height;
  }
  function put(box, x, y, w, h, z) {
    x = Math.max(EDGE, Math.min(x, innerWidth - EDGE - w));
    y = Math.max(EDGE, Math.min(y, innerHeight - EDGE - h));
    box.style.left = (x / z) + 'px';
    box.style.top = (y / z) + 'px';
  }
  function placeRoot(lv) {
    var st = lv.st, box = lv.el, z = zoom(), r = bounds(box, z), w = r.width, h = r.height;
    var vw = innerWidth, vh = innerHeight, side = st.opts.side || 'bottom', x, y, below, above, down;
    if (st.anchor) {
      var a = st.anchor.getBoundingClientRect();
      if (side === 'right' || side === 'left') {
        var R = a.right + GAP, L = a.left - GAP - w;
        if (side === 'right') x = R + w <= vw - EDGE || L < EDGE ? R : L;
        else x = L >= EDGE || R + w > vw - EDGE ? L : R;
        y = a.top;
      } else {
        var end = /-end$/.test(side), up = side.indexOf('top') === 0;
        x = end ? a.right - w : a.left;
        if (!end && x + w > vw - EDGE) x = a.right - w;
        if (end && x < EDGE) x = a.left;
        below = vh - EDGE - a.bottom - GAP;
        above = a.top - GAP - EDGE;
        down = up ? !(h <= above || above >= below) : (h <= below || below >= above);
        if (h > (down ? below : above)) h = shrink(box, down ? below : above, z);
        y = down ? a.bottom + GAP : a.top - GAP - h;
      }
    } else {
      x = st.at.x; y = st.at.y;
      if (x + w > vw - EDGE) x -= w;                // не вміщається праворуч — ліворуч від точки
      below = vh - EDGE - y;
      above = y - EDGE;
      if (h > below) {
        if (h <= above || above > below) { if (h > above) h = shrink(box, above, z); y -= h; }
        else h = shrink(box, below, z);
      }
    }
    put(box, x, y, w, h, z);
  }
  function placeSub(lv) {
    var box = lv.el, z = zoom(), r = bounds(box, z), w = r.width, h = r.height, vw = innerWidth;
    var t = lv.trigger.getBoundingClientRect(), p = lv.trigger._mvm.lv.el.getBoundingClientRect();
    var x = p.right - 2 * z, y = t.top - 5 * z;   // перший пункт — на рівні пункту, що відкрив підменю
    if (x + w > vw - EDGE) {                       // праворуч немає місця — ліворуч від меню
      var lx = p.left - w + 2 * z;
      if (lx >= EDGE || p.left > vw - p.right) x = lx;
    }
    if (h > innerHeight - 2 * EDGE) h = shrink(box, innerHeight - 2 * EDGE, z);
    put(box, x, y, w, h, z);
  }
  function refit(lv) {
    var r = lv.el.getBoundingClientRect();
    if (r.right > innerWidth - EDGE || r.bottom > innerHeight - EDGE || r.left < EDGE || r.top < EDGE)
      put(lv.el, r.left, r.top, r.width, r.height, zoom());
  }

  /* ── відкрити / закрити ── */
  function open(at, items, opts) {
    opts = opts || {};
    var anchor = at && at.nodeType === 1 ? at : null;
    if (state && anchor && state.anchor === anchor) {           // та сама кнопка вдруге — закрити
      closeAll('toggle');
      return { el: null, close: noop, refresh: noop };
    }
    var back = opts.returnFocus;
    if (back === undefined) back = anchor || ((state && inside(document.activeElement)) ? state.back : document.activeElement);
    if (state) closeAll('replace');
    var pt = null;
    if (!anchor) {
      if (at && at.clientX != null) pt = { x: +at.clientX, y: +at.clientY };
      else if (at && at.x != null) pt = { x: +at.x || 0, y: +at.y || 0 };
      else pt = { x: Math.max(EDGE, innerWidth / 2 - 120), y: 80 };
    }
    var st = { anchor: anchor, at: pt, opts: opts, back: back, levels: [], born: Date.now() };
    state = st;
    var lv = level(st, 0, items, undefined, null);
    render(lv);
    document.body.appendChild(lv.el);
    placeRoot(lv);
    listen(true);
    if (anchor && anchor.hasAttribute('aria-haspopup')) anchor.setAttribute('aria-expanded', 'true');
    first(lv, opts.focus);
    return { el: lv.el,
             close: function () { if (state === st) closeAll('api'); },
             refresh: function () { if (state === st) refresh(); } };
  }
  function unlink(lv) {
    var t = lv.trigger;
    if (t) { t.setAttribute('aria-expanded', 'false'); t.removeAttribute('aria-controls'); }
  }
  function drop(lv) { harvest(lv); clearPend(lv); unlink(lv); lv.el.remove(); }
  function closeAll(reason) {
    var st = state;
    if (!st) return;
    state = null;
    endGrace();
    listen(false);
    var back = st.back, act = document.activeElement;
    var mine = !act || act === document.body || st.levels.some(function (l) { return l.el.contains(act); });
    // спершу фокус назад (поки меню ще на сторінці — без проміжного фокусу на body), потім прибрати меню
    if (reason !== 'outside' && reason !== 'replace' && mine && back && back !== document.body && back.isConnected)
      focusOn(back);
    st.levels.forEach(drop);
    st.levels = [];
    if (st.anchor && st.anchor.hasAttribute('aria-haspopup')) st.anchor.setAttribute('aria-expanded', 'false');
    call(st.opts.onClose, reason);
  }
  function closeAfter(st, depth) {
    if (st.levels.length <= depth + 1) return;
    endGrace();
    var gone = st.levels.slice(depth + 1), act = document.activeElement, trig = gone[0].trigger;
    if (gone.some(function (l) { return l.el.contains(act); }) && trig && trig.isConnected) focusOn(trig);
    gone.forEach(drop);
    st.levels.length = depth + 1;
  }
  function first(lv, mode) {
    var t = null;
    if (mode === 'checked') t = lv.els.filter(function (e) { return e.getAttribute('aria-checked') === 'true'; })[0] || null;
    if (!t && mode !== 'none') t = lv.els.filter(function (e) { return !e._mvm.off; })[0] || lv.els[0] || null;
    if (t) go(t); else focusOn(lv.el);
  }
  function go(e) { if (e) { focusOn(e); visible(e); } }
  function visible(e) {
    var box = e._mvm.lv.el;
    if (box.scrollHeight <= box.clientHeight + 1) return;
    var top = 0;
    for (var n = e; n && n !== box; n = n.offsetParent) top += n.offsetTop;
    var bottom = top + e.offsetHeight, lv = e._mvm.lv;
    if (top < box.scrollTop + 4) { lv.quiet = Date.now() + 150; box.scrollTop = Math.max(0, top - 4); }
    else if (bottom > box.scrollTop + box.clientHeight - 4) {
      lv.quiet = Date.now() + 150;
      box.scrollTop = bottom - box.clientHeight + 4;
    }
  }
  function openSub(parent, trig, focusFirst) {
    var st = parent.st, it = trig._mvm.item;
    clearPend(parent);
    var cur = st.levels[parent.depth + 1];
    if (cur && cur.trigger === trig) { if (focusFirst) first(cur); return; }
    closeAfter(st, parent.depth);
    if (val(it.disabled, it)) return;
    var lv = level(st, parent.depth + 1, it.items, it, trig);
    lv.el.setAttribute('aria-labelledby', trig._mvm.text.id);
    render(lv);
    document.body.appendChild(lv.el);
    placeSub(lv);
    trig.setAttribute('aria-expanded', 'true');
    trig.setAttribute('aria-controls', lv.el.id);
    if (focusFirst) first(lv);
  }

  /* ── перемалювати: змінився порядок, з’явились чи зникли пункти ── */
  function rebuild(lv, focusKey) {
    var idx = lv.els.indexOf(document.activeElement), sc = lv.el.scrollTop;
    render(lv);
    lv.el.scrollTop = sc;
    refit(lv);
    var t = byKey(lv, focusKey) || lv.els[Math.max(0, Math.min(idx, lv.els.length - 1))];
    if (t) go(t); else focusOn(lv.el);
  }
  function refresh() {
    var st = state;
    if (!st) return;
    var a = document.activeElement, wasIn = inside(a);
    var am = a && a._mvm && st.levels.indexOf(a._mvm.lv) >= 0 ? a._mvm : null;
    var fDepth = am ? am.lv.depth : -1, fKey = am ? am.key : '', fIdx = am ? am.lv.els.indexOf(a) : 0;
    for (var i = 0; i < st.levels.length; i++) {
      var lv = st.levels[i];
      if (i > 0) {
        var trig = byKey(st.levels[i - 1], lv.trigger._mvm.key);
        if (!trig || !isSub(trig._mvm) || val(trig._mvm.item.disabled, trig._mvm.item)) { closeAfter(st, i - 1); break; }
        lv.trigger = trig; lv.owner = trig._mvm.item; lv.source = trig._mvm.item.items;
        trig.setAttribute('aria-expanded', 'true');
        trig.setAttribute('aria-controls', lv.el.id);
        lv.el.setAttribute('aria-labelledby', trig._mvm.text.id);
      }
      var sc = lv.el.scrollTop;
      render(lv);
      lv.el.scrollTop = sc;
      refit(lv);
    }
    if (!wasIn || state !== st || inside(document.activeElement)) return;
    if (fDepth < 0) { focusOn(st.levels[st.levels.length - 1].el); return; }
    var L = st.levels[Math.min(fDepth, st.levels.length - 1)];
    if (L.depth === fDepth) go(byKey(L, fKey) || L.els[Math.max(0, Math.min(fIdx, L.els.length - 1))] || null);
    if (!inside(document.activeElement)) first(L);
  }

  /* ── вибір пункту ── */
  function activate(b, keyboard, ev) {
    var m = b._mvm, lv = m.lv, st = lv.st, it = m.item, t = it.type || 'item';
    if (st !== state) return;
    clearPend(lv);
    if (m.btn) {                                        // кнопка в рядку (⬆ ⬇): меню лишається, рівень — наново
      if (val(it.disabled, it)) return;
      closeAfter(st, lv.depth);
      call(it.onClick, ctx(lv, it, ev));
      if (state === st && lv.el.isConnected) { rebuild(lv, m.key); sync(st); }
      return;
    }
    if (t === 'submenu') { openSub(lv, b, keyboard); return; }
    if (val(it.disabled, it)) return;
    closeAfter(st, lv.depth);
    if (t === 'check') {
      var next = !val(it.checked, it);
      if (typeof it.checked !== 'function') it.checked = next;
      call(it.onToggle, next, ctx(lv, it, ev));
      settle(st, it);
      return;
    }
    if (t === 'radio') {
      if (!val(it.checked, it)) {
        lv.items.forEach(function (o) {
          if (o !== it && o.type === 'radio' && str(o.group) === str(it.group) && typeof o.checked !== 'function') o.checked = false;
        });
        if (typeof it.checked !== 'function') it.checked = true;
        call(it.onSelect, ctx(lv, it, ev));
      }
      settle(st, it);
      return;
    }
    if (it.close === false) { call(it.onSelect, ctx(lv, it, ev)); if (state === st) sync(st); return; }
    var href = str(val(it.href, it)), target = it.target;
    closeAll('select');                                 // спершу закрити: onSelect може відкрити інше меню
    if (href && keyboard) {
      if (target && target !== '_self') window.open(href, target, 'noopener'); else location.assign(href);
    }
    call(it.onSelect, ctx(null, it, ev));
  }
  function settle(st, it) {
    if (state !== st) return;
    if (it.close === true) closeAll('select'); else sync(st);
  }

  /* ── клавіатура (на самому меню; оброблені клавіші далі не йдуть) ── */
  function onKey(e) {
    var lv = this._mvmLevel;
    if (lv.st !== state || e.isComposing || e.keyCode === 229) return;
    var k = e.key, a = document.activeElement, m = a && a._mvm && a._mvm.lv === lv ? a._mvm : null, done = true;
    if ((e.ctrlKey || e.metaKey || e.altKey) && k !== 'Escape' && k !== 'Tab') return;
    switch (k) {
      case 'ArrowDown': case 'Down': step(lv, m, 1); break;
      case 'ArrowUp': case 'Up': step(lv, m, -1); break;
      case 'Home': case 'PageUp': jump(lv, false); break;
      case 'End': case 'PageDown': jump(lv, true); break;
      case 'ArrowRight': case 'Right':
        if (m && !aside(lv, m, 1) && isSub(m)) openSub(lv, a, true);
        break;
      case 'ArrowLeft': case 'Left':
        if ((!m || !aside(lv, m, -1)) && lv.depth) closeAfter(lv.st, lv.depth - 1);
        break;
      case 'Enter': case ' ': case 'Spacebar':
        if (m) activate(a, true, e);
        break;
      case 'Escape': case 'Esc':
        if (lv.depth) closeAfter(lv.st, lv.depth - 1); else closeAll('escape');
        break;
      case 'Tab': closeAll('tab'); break;
      default:
        if (k && k.length === 1) typeahead(lv, m, k); else done = false;
    }
    if (done) { e.preventDefault(); e.stopPropagation(); }
  }
  function onKeyUp(e) {                                 // Пробіл уже оброблено на keydown — без другого «кліку»
    if (e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); e.stopPropagation(); }
  }
  function step(lv, m, d) {
    var n = lv.stops.length;
    if (!n) return;
    closeAfter(lv.st, lv.depth);
    var s = m ? (m.stop + d + n) % n : (d > 0 ? 0 : n - 1), els = lv.stops[s];
    go(els[m ? Math.min(m.col, els.length - 1) : 0]);
  }
  function jump(lv, last) {
    if (!lv.stops.length) return;
    closeAfter(lv.st, lv.depth);
    go(lv.stops[last ? lv.stops.length - 1 : 0][0]);
  }
  function aside(lv, m, d) {                             // ← → між кнопками одного рядка
    var els = lv.stops[m.stop], c = m.col + d;
    if (!els || els.length < 2 || c < 0 || c >= els.length) return false;
    closeAfter(lv.st, lv.depth);
    go(els[c]);
    return true;
  }
  function stopText(els) {
    var e = els[0];
    if (e._mvm.btn) {
      var r = e.parentNode && e.parentNode.querySelector('.mvm-rtext');
      return r ? r.textContent : '';
    }
    return e._mvm.text.textContent;
  }
  function initial(s) { return str(s).replace(LEAD, '').charAt(0).toLocaleLowerCase(); }
  function typeahead(lv, m, ch) {
    var n = lv.stops.length, from = m ? m.stop : -1;
    ch = ch.toLocaleLowerCase();
    for (var i = 1; i <= n; i++) {
      var s = (from + i + n) % n;
      if (initial(stopText(lv.stops[s])) === ch) { closeAfter(lv.st, lv.depth); go(lv.stops[s][0]); return; }
    }
  }

  /* ── миша ── */
  function itemAt(lv, t) {
    var b = t && t.closest ? t.closest(ITEM) : null;
    return b && b._mvm && b._mvm.lv === lv ? b : null;
  }
  function pend(lv, kind, target) {
    if (lv.pend && lv.pend.kind === kind && lv.pend.target === target) return;
    clearPend(lv);
    lv.pend = { kind: kind, target: target };
    lv.timer = setTimeout(function () {
      lv.pend = null; lv.timer = 0;
      if (lv.st !== state || !lv.el.isConnected) return;
      if (kind === 'open') { if (target.isConnected) openSub(lv, target, false); }
      else closeAfter(lv.st, lv.depth);
    }, OPEN_MS);
  }
  function clearPend(lv) { clearTimeout(lv.timer); lv.timer = 0; lv.pend = null; }
  function hover(lv, b) {
    if (!b || lv.st !== state) return;
    if (document.activeElement !== b) focusOn(b);
    var next = lv.st.levels[lv.depth + 1], m = b._mvm;
    if (isSub(m)) {
      if (next && next.trigger === b) clearPend(lv);
      else if (!m.off) pend(lv, 'open', b);
      else if (next) pend(lv, 'close', null);
    } else if (next) pend(lv, 'close', null);
    else clearPend(lv);
  }
  function onMove(e) {
    var lv = this._mvmLevel;
    if (lv.st !== state || (e.pointerType && e.pointerType !== 'mouse')) return;   // дотик — лише клік
    lastPt = { x: e.clientX, y: e.clientY };
    if (grace) {
      if (grace.lv === lv && Date.now() < grace.until && inPoly(e.clientX, e.clientY, grace.poly)) return;
      endGrace();
    }
    hover(lv, itemAt(lv, e.target));
  }
  function onOut(e) {
    var lv = this._mvmLevel, st = lv.st;
    if (st !== state || (e.pointerType && e.pointerType !== 'mouse')) return;
    var from = itemAt(lv, e.target), to = e.relatedTarget;
    if (!from || (to && from.contains(to))) return;
    if (lv.pend && lv.pend.kind === 'open' && lv.pend.target === from) clearPend(lv);   // пішли раніше за 120 мс
    var next = st.levels[lv.depth + 1];
    if (next && next.trigger === from) {
      if (!(to && next.el.contains(to))) startGrace(lv, next, e.clientX, e.clientY);
      return;
    }
    if (!itemAt(lv, to) && document.activeElement === from) focusOn(lv.el);   // жоден пункт не підсвічено
  }
  function onEnter() {
    var lv = this._mvmLevel;
    if (lv.st !== state) return;
    if (grace && grace.lv !== lv) endGrace();
    var parent = lv.st.levels[lv.depth - 1];
    if (parent) clearPend(parent);
  }
  // рух від пункту до його підменю навскіс через сусідні пункти: 300 мс вони не перехоплюють наведення
  function startGrace(lv, next, x, y) {
    var r = next.el.getBoundingClientRect(), right = r.left + r.width / 2 > x;
    var near = right ? r.left : r.right, far = right ? r.right : r.left;
    endGrace();
    grace = { lv: lv, until: Date.now() + GRACE_MS,
              poly: [[x + (right ? -5 : 5), y], [near, r.top], [far, r.top], [far, r.bottom], [near, r.bottom]] };
    graceT = setTimeout(function () {
      var g = grace;
      grace = null; graceT = 0;
      if (!g || g.lv.st !== state || !lastPt || !document.elementFromPoint) return;
      var t = document.elementFromPoint(lastPt.x, lastPt.y);
      if (t && g.lv.el.contains(t)) hover(g.lv, itemAt(g.lv, t));
    }, GRACE_MS);
  }
  function endGrace() { grace = null; clearTimeout(graceT); graceT = 0; }
  function inPoly(x, y, p) {
    var hit = false;
    for (var i = 0, j = p.length - 1; i < p.length; j = i++) {
      var xi = p[i][0], yi = p[i][1], xj = p[j][0], yj = p[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  }
  function onClick(e) {
    var lv = this._mvmLevel;
    if (lv.st !== state) return;
    var b = itemAt(lv, e.target);
    if (!b) return;
    if (b.tagName === 'A') {                            // посилання: перехід — браузер (Ctrl / середня — нова вкладка)
      var m = b._mvm, st = lv.st;
      if (m.off) { e.preventDefault(); return; }
      call(m.item.onSelect, ctx(lv, m.item, e));
      setTimeout(function () { if (state === st) closeAll('select'); }, 0);
      return;
    }
    activate(b, e.detail === 0, e);
  }
  function onCtx(e) { if (!e.shiftKey) e.preventDefault(); }
  function onWheel(e) {                                 // колесо над меню не прокручує сторінку
    var box = this, max = box.scrollHeight - box.clientHeight;
    if (max <= 0 || !e.deltaY || (e.deltaY < 0 && box.scrollTop <= 0) || (e.deltaY > 0 && box.scrollTop >= max - 1))
      e.preventDefault();
  }

  /* ── закрити, коли людина пішла з меню ── */
  function listen(on) {
    if (on === listening) return;
    listening = on;
    var m = on ? 'addEventListener' : 'removeEventListener';
    document[m]('pointerdown', onDown, true);
    document[m]('scroll', onScroll, true);
    window[m]('keydown', onEscOutside, true);
    window[m]('blur', onBlur);
    window[m]('resize', onResize);
  }
  function onDown(e) {
    var st = state;
    if (!st) return;
    var t = e.target;
    if (inside(t) || (st.anchor && t && t.nodeType === 1 && st.anchor.contains(t))) return;
    closeAll('outside');
  }
  function onScroll(e) {
    var st = state;
    if (!st) return;
    var t = e.target;
    for (var i = 0; i < st.levels.length; i++) {
      var lv = st.levels[i];
      if (t === lv.el) { if (Date.now() > lv.quiet) closeAfter(st, i); return; }   // прокрутили меню — підменю геть
      if (t && t.nodeType === 1 && lv.el.contains(t)) return;
    }
    if (Date.now() - st.born < 250) return;            // прокрутка, що почалась до меню (і плавна до кнопки)
    closeAll('scroll');
  }
  // меню відкрили мишею, фокус лишився на сторінці — Esc закриває саме меню (і далі не йде: не панель, не вікно)
  function onEscOutside(e) {
    if (!state || (e.key !== 'Escape' && e.key !== 'Esc') || inside(document.activeElement)) return;
    closeAll('escape');
    e.preventDefault();
    e.stopImmediatePropagation();
  }
  function onBlur() { if (state) closeAll('blur'); }
  function onResize() { if (state) closeAll('resize'); }

  /* ── повідомлення внизу: одне за раз ── */
  var tBox = null, tTimer = 0, tLeft = 0, tEnd = 0, tGen = 0, tUndo = null, tHover = false, tFocus = false, zOn = false;
  function toastBox() {
    if (tBox && tBox.isConnected) return tBox;
    tBox = el('div', 'mvm-toast');
    tBox.id = 'mvm-toast';
    tBox.setAttribute('role', 'status');
    tBox.setAttribute('aria-live', 'polite');
    tBox.setAttribute('aria-atomic', 'true');
    tBox.addEventListener('pointerenter', function () { tHover = true; tPause(); });
    tBox.addEventListener('pointerleave', function () { tHover = false; tResume(); });
    tBox.addEventListener('focusin', function () { tFocus = true; tPause(); });
    tBox.addEventListener('focusout', function (e) {
      if (!tBox.contains(e.relatedTarget)) { tFocus = false; tResume(); }
    });
    tBox.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); e.stopPropagation(); hideToast(); }
    });
    document.body.appendChild(tBox);
    return tBox;
  }
  function toast(text, o) {
    o = o || {};
    var fresh = !(tBox && tBox.isConnected), box = toastBox(), gen = ++tGen;
    var ms = o.ms == null ? TOAST_MS : (+o.ms || 0);
    function btn(label, fn) {
      var b = el('button', 'mvm-toast-btn', label);
      b.type = 'button';
      b.addEventListener('click', fn);
      box.appendChild(b);
      return b;
    }
    function fill() {
      if (gen !== tGen) return;
      var act = o.action && str(o.action.label) ? o.action : null, undo = typeof o.undo === 'function' ? o.undo : null;
      box.textContent = '';
      box.appendChild(el('span', 'mvm-toast-msg', str(text)));
      if (act) btn(str(act.label), function () { hideToast(); call(act.run); });
      if (undo) {
        var u = btn('↶ Скасувати', function () { hideToast(); call(undo); });
        u.title = 'Ctrl Z';
        u.setAttribute('aria-keyshortcuts', 'Control+Z');
      }
      if (act || undo || ms <= 0) {
        var x = btn('✕', hideToast);
        x.classList.add('mvm-toast-x');
        x.setAttribute('aria-label', 'Закрити');
        x.title = 'Закрити';
      }
      tUndo = undo;
      keyZ(!!undo);
      box.style.maxWidth = Math.floor(Math.min(560, (innerWidth - 32) / zoom())) + 'px';
      box.classList.add('on');
      clearTimeout(tTimer); tTimer = 0; tLeft = 0;
      if (ms > 0) { if (tHover || tFocus) tLeft = ms; else tRun(ms); }
    }
    // нове місце для повідомлень читач екрана помічає не одразу — текст трохи згодом
    if (fresh) setTimeout(fill, 50); else fill();
    return { close: function () { if (gen === tGen) hideToast(); } };
  }
  function tRun(ms) { clearTimeout(tTimer); tEnd = Date.now() + ms; tTimer = setTimeout(hideToast, ms); }
  function tPause() { if (tTimer) { clearTimeout(tTimer); tTimer = 0; tLeft = Math.max(1500, tEnd - Date.now()); } }
  function tResume() {
    if (tHover || tFocus || !tLeft || !tBox || !tBox.classList.contains('on')) return;
    var left = tLeft;
    tLeft = 0;
    tRun(left);
  }
  function hideToast() {
    clearTimeout(tTimer); tTimer = 0; tLeft = 0; tUndo = null;
    keyZ(false);
    if (!tBox) return;
    var gen = tGen, act = document.activeElement;
    if (act && tBox.contains(act) && act.blur) act.blur();
    tFocus = false;
    tBox.classList.remove('on');
    setTimeout(function () { if (gen === tGen && tBox && !tBox.classList.contains('on')) tBox.textContent = ''; }, 250);
  }
  function keyZ(on) {
    if (on === zOn) return;
    zOn = on;
    document[on ? 'addEventListener' : 'removeEventListener']('keydown', onKeyZ, true);
  }
  function onKeyZ(e) {                                  // Ctrl Z, поки видно «↶ Скасувати» (не в полі вводу)
    // за символом (німецька розкладка: Z — на клавіші KeyY); кирилиця дає «я» — тоді за клавішею
    var ch = e.key || '', isZ = /^[a-z]$/i.test(ch) ? ch.toLowerCase() === 'z' : e.code === 'KeyZ';
    if (!tUndo || !isZ || !(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;
    var t = e.target;
    if (t && t.nodeType === 1 && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    e.stopPropagation();
    var u = tUndo;
    hideToast();
    call(u);
  }

  var api = window.MVMenu = {
    open: open,
    close: function () { closeAll('api'); },
    isOpen: function () { return !!state; },
    refresh: refresh,
    toast: toast
  };
})();
