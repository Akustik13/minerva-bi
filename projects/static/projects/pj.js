/* «🧭 Пульт проєкту»: вкладки, план, класична діаграма Ганта, ризики, статус, AI — картка проєкту й «🧭 Робочий
   стіл». Пульт (#pj-cockpit) оновлюється без перезавантаження: JSON api/project/<pk>/cockpit/ і POST-дії. */
(function () {
  var root = document.getElementById('pj-cockpit');
  if (!root || root.dataset.ready) return;
  root.dataset.ready = '1';
  var KEY = 'pj-tl-range', TAB = 'pj-tab', curTab = '', dlg = null;
  function csrf() {
    var i = document.querySelector('[name=csrfmiddlewaretoken]');
    return i ? i.value : '';
  }
  function box() { return root.querySelector('.pjc'); }
  function range() { var b = box(); return b ? b.dataset.range : 'all'; }
  function toast(text) {
    if (window.MVMenu && window.MVMenu.toast) { window.MVMenu.toast(text); return; }
    var n = root.querySelector('.pjc-merr');
    if (n) n.textContent = text;
  }
  function put(html) { root.innerHTML = html; applyTab(curTab); renderFound(); applyGantt(); applyTimeline(); }
  // ── 🗓 Шкала в браузері (pj_timeline.js): масштаб, прокрутка, доріжки; вікно — для кожного проєкту окремо ──
  var tl = null;
  function applyTimeline() {
    var node = root.querySelector('#pjt-data'), holder = root.querySelector('[data-pjt-live]');
    if (!node || !holder || !window.PJTimeline) return;
    var d;
    try { d = JSON.parse(node.textContent); } catch (e) { return; }
    tl = window.PJTimeline.mount(holder, d, 'pj-tl-view-' + ((box() && box().dataset.code) || ''));
  }
  function done(j) {
    if (j && j.ok && j.html) { put(j.html); return true; }
    toast('⚠️ ' + ((j && j.error) || 'не вдалося'));
    return false;
  }

  // ── Вкладки «🗓 Шкала» / «📅 План» / «⚠️ Ризики» / «📝 Статус»: #pj-plan, #pj-risks, #pj-status в адресі → ця
  // вкладка, інакше — як востаннє ──
  var TABS = ['timeline', 'plan', 'risks', 'status'];
  var HASH = {'#pj-plan': 'plan', '#pj-risks': 'risks', '#pj-status': 'status'};
  function firstTab() {
    if (HASH[location.hash]) return HASH[location.hash];
    var saved = '';
    try { saved = localStorage.getItem(TAB) || ''; } catch (e) { saved = ''; }
    if (TABS.indexOf(saved) >= 0) return saved;
    var b = box();
    return b && b.dataset.hasPlan ? 'plan' : 'timeline';
  }
  function applyTab(key) {
    var b = box();
    if (!b) return;
    curTab = key || firstTab();
    b.querySelectorAll('.pjc-tab[data-pjc-tab]').forEach(function (t) {
      var on = t.dataset.pjcTab === curTab;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    b.querySelectorAll('[data-pjc-pane]').forEach(function (p) { p.hidden = p.dataset.pjcPane !== curTab; });
    if (curTab === 'timeline' && tl) tl.render();
  }
  applyTab();
  window.addEventListener('hashchange', function () { if (HASH[location.hash]) applyTab(HASH[location.hash]); });
  // ── 📋 Діаграми Ганта: «📊 Minerva» (SVG з сервера) і «📋 Класична» (малює браузер); «Коротко» — фази й віхи,
  // «Детально» — усі кроки (фазу можна згорнути); класична: Ctrl + коліщатко — масштаб, тягнути фон — прокрутка,
  // тягнути смугу — нові дати, край — тривалість; правий клік — меню кроку, фази чи дня (лише з правом змінювати) ──
  var GV = 'pj-gantt-view', GS = 'pj-gantt-scale', GP = 'pj-gantt-ppd', GD = 'pj-gantt-detail';
  var ROW = 26, GHEAD = 40, DAY = 864e5, PPD_MIN = 0.8, PPD_MAX = 60;
  var PPD = {day: 26, week: 9, month: 3.2};
  var GCOLS = [['n', '№', 46], ['title', 'Крок', 236], ['start', 'Початок', 56], ['end', 'Кінець', 56],
               ['days', 'Дн.', 34], ['who', 'Хто', 92], ['preds', 'Після', 64]];
  var SVGNS = 'http://www.w3.org/2000/svg';
  var zoomAnchor = null, drawRaf = 0, noClickUntil = 0;
  function pref(key, dflt) { try { return localStorage.getItem(key) || dflt; } catch (e) { return dflt; } }
  function setPref(key, v) { try { localStorage.setItem(key, v); } catch (e) { /* лише на цю сторінку */ } }
  function dnum(iso) { return iso ? Math.round(Date.parse(iso + 'T00:00:00Z') / DAY) : null; }
  function diso(n) { return new Date(n * DAY).toISOString().slice(0, 10); }
  function dshort(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : ''; }
  function sv(tag, attrs, text) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function cut(text, px) {
    var max = Math.max(Math.floor(px / 6.6), 3);
    text = String(text || '');
    return text.length > max ? text.slice(0, max - 1) + '…' : text;
  }
  function ganttPpd() {
    var p = parseFloat(pref(GP, ''));
    if (!(p > 0)) p = PPD[pref(GS, 'week')] || 9;
    return Math.min(Math.max(p, PPD_MIN), PPD_MAX);
  }
  function foldKey() { return 'pj-gantt-fold-' + ((box() && box().dataset.code) || ''); }
  function folds() { try { return JSON.parse(pref(foldKey(), '{}')) || {}; } catch (e) { return {}; } }
  function setFold(id, on) {
    var f = folds();
    if (on) f[id] = 1; else delete f[id];
    setPref(foldKey(), JSON.stringify(f));
  }
  function canEdit() { var b = box(); return !!(b && b.dataset.canChange); }
  function applyGantt() {
    var b = box();
    if (!b) return;
    var view = pref(GV, 'ours'), detail = pref(GD, 'full'), ppd = ganttPpd();
    b.querySelectorAll('[data-pjg-view]').forEach(function (x) {
      var on = x.dataset.pjgView === view;
      x.classList.toggle('on', on);
      x.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    b.querySelectorAll('[data-pjg-detail-btn]').forEach(function (x) {
      var on = x.dataset.pjgDetailBtn === detail;
      x.classList.toggle('on', on);
      x.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    b.querySelectorAll('[data-pjg-pane]').forEach(function (p) { p.hidden = p.dataset.pjgPane !== view; });
    b.querySelectorAll('[data-pjg-only]').forEach(function (p) { p.hidden = p.dataset.pjgOnly !== view; });
    b.querySelectorAll('[data-pjg-detail]').forEach(function (p) { p.hidden = p.dataset.pjgDetail !== detail; });
    b.querySelectorAll('[data-pjg-scale]').forEach(function (x) {
      var on = Math.abs(PPD[x.dataset.pjgScale] - ppd) < 0.05;
      x.classList.toggle('on', on);
      x.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (view === 'classic') drawClassic();
    else bindOurs();
  }
  function scheduleDraw() {
    if (drawRaf) return;
    drawRaf = requestAnimationFrame(function () { drawRaf = 0; applyGantt(); });
  }
  function classicRows(d) {
    var detail = pref(GD, 'full'), f = folds(), out = [], phase = null;
    (d.rows || []).forEach(function (r) {
      if (r.type === 'phase') { phase = r; out.push(r); return; }
      if (detail === 'brief' && r.kind !== 'milestone') return;
      if (detail !== 'brief' && phase && f[phase.id || 'none']) return;
      out.push(r);
    });
    return out;
  }
  function drawClassic() {
    var holder = root.querySelector('[data-pjg-pane="classic"]'), node = root.querySelector('#pjg-data');
    if (!holder || !node || holder.hidden) return;
    var d;
    try { d = JSON.parse(node.textContent); } catch (e) { return; }
    var rows = classicRows(d), ppd = ganttPpd(), detail = pref(GD, 'full'), f = folds();
    var canChange = canEdit();
    var pad = Math.max(3, Math.ceil(60 / ppd));
    var lo = dnum(d.start) - pad, hi = dnum(d.end) + pad + 4;
    var width = Math.max((hi - lo + 1) * ppd, 320), height = GHEAD + rows.length * ROW + 4;
    var leftW = GCOLS.reduce(function (s, c) { return s + c[2]; }, 0);
    var keep = holder.querySelector('.pjg2-chart');
    var scrollLeft = keep ? keep.scrollLeft : null;
    holder.textContent = '';

    // Таблиця кроків (SVG — рядки точно на одному рівні зі смугами)
    var grid = sv('svg', {width: leftW, height: height, 'class': 'pjg2-grid', role: 'table', 'aria-label': 'Кроки плану'});
    var x = 0;
    GCOLS.forEach(function (c) {
      grid.appendChild(sv('text', {x: x + 6, y: GHEAD - 13, 'class': 'pjg2-th'}, c[1]));
      x += c[2];
    });
    grid.appendChild(sv('line', {x1: 0, y1: GHEAD - 0.5, x2: leftW, y2: GHEAD - 0.5, 'class': 'pjg2-hl'}));
    rows.forEach(function (r, i) {
      var y = GHEAD + i * ROW, g = sv('g', {'class': 'pjg2-row pjg2-' + r.type});
      if (r.type === 'step') {
        g.setAttribute('data-cstep', r.id);
        if (canChange) { g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button'); }
      } else {
        g.setAttribute('data-cphase', r.id || 'none');
        g.setAttribute('tabindex', '0');
        g.setAttribute('role', 'button');
        g.setAttribute('aria-expanded', detail !== 'brief' && f[r.id || 'none'] ? 'false' : 'true');
      }
      g.appendChild(sv('rect', {x: 0, y: y, width: leftW, height: ROW, 'class': 'pjg2-rb' + (i % 2 ? ' pjg2-odd' : '')}));
      var fold = r.type === 'phase' ? (detail === 'brief' ? '·' : (f[r.id || 'none'] ? '▸' : '▾')) : '';
      var vals = {n: (fold ? fold + ' ' : '') + r.n, title: (r.kind === 'milestone' ? '◆ ' : '') + r.title,
                  start: r.kind === 'milestone' ? '' : dshort(r.start), end: dshort(r.end),
                  days: r.type === 'step' ? (r.kind === 'milestone' ? '0' : String(r.days || '')) :
                        (r.total ? r.done + '/' + r.total : ''),
                  who: r.type === 'step' ? (r.who || r.side || '') : '',
                  preds: (r.preds || []).map(function (p) { return p.n + (p.lag ? (p.lag > 0 ? '+' : '') + p.lag : ''); }).join(', ')};
      var cx = 0;
      GCOLS.forEach(function (c) {
        var indent = c[0] === 'title' && r.type === 'step' ? 12 : 0;
        var t = sv('text', {x: cx + 6 + indent, y: y + 17, 'class': 'pjg2-td pjg2-c-' + c[0]},
                   cut(vals[c[0]], c[2] - 10 - indent));
        if (c[0] === 'title' || c[0] === 'who') t.setAttribute('translate', 'no');
        g.appendChild(t);
        cx += c[2];
      });
      g.appendChild(sv('title', {}, r.n + ' ' + r.title + (r.type === 'phase' ? '\nКлік — згорнути чи розгорнути кроки фази' : '')
                       + (r.note ? '\n' + r.note : '')));
      grid.appendChild(g);
    });

    // Смуги
    var chart = document.createElement('div');
    chart.className = 'pjg2-chart';
    var svg = sv('svg', {width: width, height: height, 'class': 'pjg2-svg', role: 'img',
                         'aria-label': 'Класична діаграма Ганта'});
    var defs = sv('defs');
    var mk = sv('marker', {id: 'pjg2-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7,
                           orient: 'auto-start-reverse'});
    mk.appendChild(sv('path', {d: 'M0,0 L8,4 L0,8 z', 'class': 'pjg2-arrowhead'}));
    defs.appendChild(mk);
    svg.appendChild(defs);
    function X(n) { return (n - lo) * ppd; }
    var lang = document.documentElement.lang || 'uk', fmtM;
    try { fmtM = new Intl.DateTimeFormat(lang, {month: 'short', year: 'numeric', timeZone: 'UTC'}); }
    catch (e) { fmtM = new Intl.DateTimeFormat('uk', {month: 'short', year: 'numeric', timeZone: 'UTC'}); }
    var showDays = ppd >= 16, showWeeks = !showDays && ppd * 7 >= 30;
    for (var dn = lo; dn <= hi; dn++) {
      var dt = new Date(dn * DAY), dow = dt.getUTCDay();
      if (ppd >= 10 && (dow === 0 || dow === 6)) {
        svg.appendChild(sv('rect', {x: X(dn), y: GHEAD, width: ppd, height: height - GHEAD, 'class': 'pjg2-wkend'}));
      }
      if (dt.getUTCDate() === 1 || dn === lo) {
        svg.appendChild(sv('line', {x1: X(dn), y1: 0, x2: X(dn), y2: height, 'class': 'pjg2-mon'}));
        var days = new Date(Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth() + 1, 1)) / DAY - dn;
        if (days * ppd > 40) svg.appendChild(sv('text', {x: X(dn) + 4, y: 14, 'class': 'pjg2-mt'}, fmtM.format(dt)));
        else if (dt.getUTCMonth() === 0) svg.appendChild(sv('text', {x: X(dn) + 3, y: 14, 'class': 'pjg2-mt'}, String(dt.getUTCFullYear())));
      }
      if (showDays) {
        svg.appendChild(sv('text', {x: X(dn) + ppd / 2, y: GHEAD - 8, 'class': 'pjg2-dt', 'text-anchor': 'middle'},
                           String(dt.getUTCDate())));
      } else if (ppd * 7 >= 8 && dow === 1) {
        svg.appendChild(sv('line', {x1: X(dn), y1: 20, x2: X(dn), y2: height, 'class': 'pjg2-wk'}));
        if (showWeeks) svg.appendChild(sv('text', {x: X(dn) + 3, y: GHEAD - 8, 'class': 'pjg2-dt'}, dshort(diso(dn))));
      }
    }
    svg.appendChild(sv('line', {x1: 0, y1: GHEAD - 0.5, x2: width, y2: GHEAD - 0.5, 'class': 'pjg2-hl'}));
    rows.forEach(function (r, i) {
      if (i % 2) svg.appendChild(sv('rect', {x: 0, y: GHEAD + i * ROW, width: width, height: ROW, 'class': 'pjg2-odd'}));
    });
    var depLayer = sv('g', {'class': 'pjg2-deps'});      // стрілки — під смугами, над фоном
    svg.appendChild(depLayer);
    var pos = {};
    rows.forEach(function (r, i) {
      if (!r.start || !r.end) return;
      var y = GHEAD + i * ROW, s = dnum(r.start), e = dnum(r.end);
      var x1 = X(s), x2 = X(e + 1), g;
      if (r.type === 'phase') {
        g = sv('g', {'class': 'pjg2-sum pjg2-s-' + r.status, 'data-cphase': r.id || 'none'});
        g.appendChild(sv('rect', {x: x1, y: y + 9, width: Math.max(x2 - x1, 2), height: 6, 'class': 'pjg2-sumbar'}));
        g.appendChild(sv('path', {d: 'M' + x1 + ',' + (y + 15) + ' l5,0 l-5,6 z', 'class': 'pjg2-sumbar'}));
        g.appendChild(sv('path', {d: 'M' + x2 + ',' + (y + 15) + ' l-5,0 l5,6 z', 'class': 'pjg2-sumbar'}));
        g.appendChild(sv('title', {}, r.title + ' · ' + dshort(r.start) + ' – ' + dshort(r.end)));
        svg.appendChild(g);
        return;
      }
      g = sv('g', {'class': 'pjg2-task pjg2-' + r.status + (r.crit ? ' pjg2-crit' : ''), 'data-cstep': r.id});
      if (r.bstart && r.bend) {
        g.appendChild(sv('rect', {x: X(dnum(r.bstart)), y: y + 20, width: Math.max(X(dnum(r.bend) + 1) - X(dnum(r.bstart)), 2),
                                  height: 3, 'class': 'pjg2-base'}));
      }
      if (r.kind === 'milestone') {
        var mx = X(e) + ppd / 2;
        g.appendChild(sv('path', {d: 'M' + mx + ',' + (y + 5) + ' l8,8 l-8,8 l-8,-8 z', 'class': 'pjg2-mile'}));
        pos[r.id] = {x1: mx - 8, x2: mx + 8, y: y + 13};
      } else {
        g.appendChild(sv('rect', {x: x1, y: y + 5, width: Math.max(x2 - x1, 3), height: 15, rx: 3, 'class': 'pjg2-bar'}));
        if (canChange && r.status !== 'done' && r.status !== 'skipped') {
          g.appendChild(sv('rect', {x: Math.max(x2 - 6, x1), y: y + 5, width: 6, height: 15, 'class': 'pjg2-h'}));
        }
        pos[r.id] = {x1: x1, x2: x2, y: y + 12.5};
      }
      var label = r.who || r.side || '';
      if (label) {
        var lt = sv('text', {x: (r.kind === 'milestone' ? X(e) + ppd / 2 + 11 : x2 + 5), y: y + 17, 'class': 'pjg2-lab'}, label);
        lt.setAttribute('translate', 'no');
        g.appendChild(lt);
      }
      g.appendChild(sv('title', {}, r.n + ' ' + r.title + ' · ' + dshort(r.start) + ' – ' + dshort(r.end)
        + (r.crit ? ' · критичний шлях' : '') + (r.slip > 0 ? ' · +' + r.slip + ' дн. від базового плану' : '')
        + (r.note ? '\n' + r.note : '') + (canChange ? '\nТягніть — нові дати · правий клік — меню' : '')));
      if (canChange && r.status !== 'done' && r.status !== 'skipped') g.classList.add('pjg2-drag');
      svg.appendChild(g);
    });
    // Залежності: від кінця попереднього кроку до початку наступного
    rows.forEach(function (r) {
      (r.preds || []).forEach(function (p) {
        var a = pos[p.id], b2 = pos[r.id];
        if (!a || !b2) return;
        var sx = a.x2, sy = a.y, ex = b2.x1, ey = b2.y, mid = Math.max(sx + 6, ex - 6);
        var dpath = ex >= sx + 12 ? 'M' + sx + ',' + sy + ' H' + mid + ' V' + ey + ' H' + ex
          : 'M' + sx + ',' + sy + ' h6 V' + (ey - 9) + ' H' + (ex - 8) + ' V' + ey + ' H' + ex;
        depLayer.appendChild(sv('path', {d: dpath, 'class': 'pjg2-dep', 'marker-end': 'url(#pjg2-arrow)'}));
      });
    });
    var today = dnum(d.today);
    svg.appendChild(sv('line', {x1: X(today) + ppd / 2, y1: 18, x2: X(today) + ppd / 2, y2: height, 'class': 'pjg2-today'}));
    if (d.due) {
      var due = dnum(d.due);
      var dl = sv('line', {x1: X(due) + ppd / 2, y1: 18, x2: X(due) + ppd / 2, y2: height, 'class': 'pjg2-due'});
      dl.appendChild(sv('title', {}, 'Строк клієнту ' + dshort(d.due)));
      svg.appendChild(dl);
    }
    chart.appendChild(svg);
    holder.appendChild(grid);
    holder.appendChild(chart);
    if (zoomAnchor) {
      chart.scrollLeft = Math.max(0, (zoomAnchor.day - lo) * ppd - zoomAnchor.px);
      zoomAnchor = null;
    } else {
      chart.scrollLeft = scrollLeft !== null ? scrollLeft : Math.max(0, X(today) - chart.clientWidth / 3);
    }
    function dayAt(clientX) {
      return lo + Math.floor((clientX - chart.getBoundingClientRect().left + chart.scrollLeft) / ppd);
    }

    // Масштаб: Ctrl + коліщатко (щипок на тачпаді) — довкола курсора
    chart.addEventListener('wheel', function (ev) {
      if (!(ev.ctrlKey || ev.metaKey)) return;
      ev.preventDefault();
      var px = ev.clientX - chart.getBoundingClientRect().left;
      var np = Math.min(Math.max(ganttPpd() * Math.exp(-Math.max(Math.min(ev.deltaY, 200), -200) * 0.0022), PPD_MIN), PPD_MAX);
      if (!zoomAnchor) zoomAnchor = {day: lo + (chart.scrollLeft + px) / ppd, px: px};   // кілька подій за кадр — накопичуються
      setPref(GP, String(np));
      scheduleDraw();
    }, {passive: false});

    // Перетягування: смуга — зсув дат, край — тривалість; фон — прокрутка; без руху — вікно кроку
    var byId = {};
    rows.forEach(function (r) { if (r.type === 'step') byId[r.id] = r; });
    svg.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      var g = ev.target.closest('g.pjg2-drag');
      if (!g) {
        if (ev.target.closest('g[data-cstep],g[data-cphase]')) return;
        var p0 = {x: ev.clientX, left: chart.scrollLeft};              // фон — прокрутка
        chart.classList.add('pjg2-grab');
        svg.setPointerCapture(ev.pointerId);
        var mv = function (e2) { chart.scrollLeft = p0.left - (e2.clientX - p0.x); };
        var up = function () {
          chart.classList.remove('pjg2-grab');
          svg.removeEventListener('pointermove', mv);
          svg.removeEventListener('pointerup', up);
          svg.removeEventListener('pointercancel', up);
        };
        svg.addEventListener('pointermove', mv);
        svg.addEventListener('pointerup', up);
        svg.addEventListener('pointercancel', up);
        return;
      }
      var r = byId[g.getAttribute('data-cstep')];
      if (!r) return;
      var resize = ev.target.classList.contains('pjg2-h'), x0 = ev.clientX, s0 = dnum(r.start), e0 = dnum(r.end);
      var bar = g.querySelector('.pjg2-bar'), w0 = bar ? +bar.getAttribute('width') : 0, delta = 0;
      var tip = document.createElement('div');
      tip.className = 'pjg2-tip';
      chart.appendChild(tip);
      g.setPointerCapture(ev.pointerId);
      ev.preventDefault();
      function dates() {
        var ns = resize ? s0 : s0 + delta, ne = Math.max(e0 + delta, ns);
        if (r.kind === 'milestone') ns = ne = e0 + delta;
        return [ns, ne];
      }
      function onMove(e2) {
        delta = Math.round((e2.clientX - x0) / ppd);
        if (resize && bar) bar.setAttribute('width', Math.max(w0 + delta * ppd, ppd));
        else g.setAttribute('transform', 'translate(' + (delta * ppd) + ',0)');
        var nd = dates();
        tip.textContent = r.kind === 'milestone' ? '◆ ' + dshort(diso(nd[1]))
          : dshort(diso(nd[0])) + ' – ' + dshort(diso(nd[1])) + ' · ' + (nd[1] - nd[0] + 1) + ' дн.';
        tip.style.left = (e2.clientX - chart.getBoundingClientRect().left + chart.scrollLeft + 12) + 'px';
        tip.style.top = (e2.clientY - chart.getBoundingClientRect().top - 28) + 'px';
        tip.hidden = !delta;
      }
      function onUp() {
        g.removeEventListener('pointermove', onMove);
        g.removeEventListener('pointerup', onUp);
        g.removeEventListener('pointercancel', onUp);
        tip.remove();
        if (!delta) { openStep(r.id); return; }
        var nd = dates();
        moveStep(r.id, diso(nd[0]), diso(nd[1]), drawClassic);
      }
      g.addEventListener('pointermove', onMove);
      g.addEventListener('pointerup', onUp);
      g.addEventListener('pointercancel', onUp);
    });
    svg.addEventListener('click', function (ev) {          // виконаний крок не тягнеться — клік відкриває вікно
      var g = ev.target.closest('g[data-cstep]');
      if (g && canChange && !g.classList.contains('pjg2-drag')) openStep(g.getAttribute('data-cstep'));
    });
    svg.addEventListener('contextmenu', function (ev) {
      if (!canChange || ev.shiftKey || !window.MVMenu) return;
      ev.preventDefault();
      var g = ev.target.closest('g[data-cstep]'), ph = ev.target.closest('g[data-cphase]');
      if (g) stepMenu(g.getAttribute('data-cstep'), ev);
      else if (ph) phaseMenu(ph.getAttribute('data-cphase'), ev);
      else dayMenu(diso(dayAt(ev.clientX)), ev);
    });
    grid.addEventListener('click', function (ev) {
      var ph = ev.target.closest('g[data-cphase]');
      if (ph) { togglePhase(ph.getAttribute('data-cphase')); return; }
      var g = ev.target.closest('g[data-cstep]');
      if (g && canChange) openStep(g.getAttribute('data-cstep'));
    });
    grid.addEventListener('dblclick', function (ev) {
      var ph = ev.target.closest('g[data-cphase]');
      if (ph && canChange && ph.getAttribute('data-cphase') !== 'none') openPhase(ph.getAttribute('data-cphase'));
    });
    grid.addEventListener('contextmenu', function (ev) {
      if (!canChange || ev.shiftKey || !window.MVMenu) return;
      var g = ev.target.closest('g[data-cstep]'), ph = ev.target.closest('g[data-cphase]');
      if (!g && !ph) return;
      ev.preventDefault();
      if (g) stepMenu(g.getAttribute('data-cstep'), ev);
      else phaseMenu(ph.getAttribute('data-cphase'), ev);
    });
    grid.addEventListener('keydown', function (ev) {
      var g = ev.target.closest && ev.target.closest('g[data-cstep],g[data-cphase]');
      if (!g || (ev.key !== 'Enter' && ev.key !== ' ')) return;
      ev.preventDefault();
      if (g.hasAttribute('data-cphase')) togglePhase(g.getAttribute('data-cphase'));
      else if (canChange) openStep(g.getAttribute('data-cstep'));
    });
  }
  function togglePhase(id) {
    if (pref(GD, 'full') === 'brief') { setPref(GD, 'full'); setFold(id, false); applyGantt(); return; }
    setFold(id, !folds()[id]);
    drawClassic();
  }

  // ── «📊 Діаграма Minerva» (SVG з сервера): тягнути смугу чи ◆ — нові дати, край смуги — тривалість ──
  function classicById() {
    var node = root.querySelector('#pjg-data'), out = {};
    if (!node) return out;
    try { (JSON.parse(node.textContent).rows || []).forEach(function (r) { if (r.type === 'step') out[r.id] = r; }); }
    catch (e) { /* без даних — без перетягування */ }
    return out;
  }
  function bindOurs() {
    if (!canEdit()) return;
    root.querySelectorAll('[data-pjg-pane="ours"] svg.pjg').forEach(function (svg) {
      if (svg.dataset.bound) return;
      svg.dataset.bound = '1';
      var ppd = parseFloat(svg.getAttribute('data-ppd') || '0');
      if (!(ppd > 0)) return;
      svg.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0) return;
        var shape = ev.target.closest('.pjg-bar, .pjg-dia'), g = ev.target.closest('g[data-step]');
        if (!shape || !g || g.classList.contains('pjg-done') || g.classList.contains('pjg-skipped')) return;
        var r = classicById()[g.getAttribute('data-step')];
        if (!r || !r.end) return;
        var ctm = svg.getScreenCTM(), k = ctm ? ctm.a : 1;
        var box2 = shape.getBoundingClientRect(), resize = shape.classList.contains('pjg-bar') && ev.clientX > box2.right - 7;
        var x0 = ev.clientX, s0 = dnum(r.start || r.end), e0 = dnum(r.end), delta = 0;
        var w0 = shape.classList.contains('pjg-bar') ? +shape.getAttribute('width') : 0;
        shape.setPointerCapture(ev.pointerId);
        ev.preventDefault();
        svg.classList.add('pjg-dragging');
        function onMove(e2) {
          delta = Math.round((e2.clientX - x0) / k / ppd);
          if (resize) shape.setAttribute('width', Math.max(w0 + delta * ppd, ppd));
          else shape.setAttribute('transform', 'translate(' + (delta * ppd) + ',0)');
        }
        function onUp() {
          shape.removeEventListener('pointermove', onMove);
          shape.removeEventListener('pointerup', onUp);
          shape.removeEventListener('pointercancel', onUp);
          svg.classList.remove('pjg-dragging');
          if (!delta) return;                               // без руху — клік відкриє вікно кроку
          noClickUntil = Date.now() + 400;
          var ns = resize ? s0 : s0 + delta, ne = Math.max(e0 + delta, ns);
          if (r.kind === 'milestone') ns = ne = e0 + delta;
          moveStep(r.id, diso(ns), diso(ne), function () { shape.removeAttribute('transform'); if (w0) shape.setAttribute('width', w0); });
        }
        shape.addEventListener('pointermove', onMove);
        shape.addEventListener('pointerup', onUp);
        shape.addEventListener('pointercancel', onUp);
      });
    });
  }
  function moveStep(id, start, end, onFail) {
    send(box().dataset.planBase + 'step/' + id + '/dates/', {start: start, end: end})
      .then(function (j) { if (!done(j) && onFail) onFail(); });
  }

  // ── Правий клік на діаграмах: крок, фаза, день ──
  function stepOf(id) {
    var s = null;
    planData().steps.forEach(function (x) { if (String(x.id) === String(id)) s = x; });
    return s;
  }
  function stepBody(s, patch) {
    var b = {id: s.id, title: s.title, kind: s.kind, phase: s.phase, side: s.side, assignee: s.assignee,
             start: s.kind === 'milestone' ? '' : s.start, end: s.end, duration: s.duration, status: s.status,
             trigger: s.trigger, auto: s.auto, lag: s.lag, notes: s.notes, after: s.after, links: s.links,
             offers: s.offers};
    Object.keys(patch || {}).forEach(function (k) { b[k] = patch[k]; });
    return b;
  }
  function postStep(body) { return send(box().dataset.planStep, body).then(done); }
  function shiftStep(s, days) {
    var a = dnum(s.f_start || s.start || s.f_end || s.end), e = dnum(s.f_end || s.end || s.f_start || s.start);
    if (a === null || e === null) { toast('⚠️ У кроку ще немає дат — відкрийте вікно кроку'); return; }
    if (s.kind === 'milestone') a = e;
    moveStep(s.id, diso(a + days), diso(e + days));
  }
  function stepMenu(id, ev) {
    var s = stepOf(id), data = planData();
    if (!s) return;
    var done2 = s.status === 'done' || s.eff === 'done';
    var items = [
      {label: 'Змінити крок…', icon: '✏️', onSelect: function () { openStep(s.id); }},
      done2 ? {label: 'Повернути в роботу', icon: '↺', onSelect: function () { postStep(stepBody(s, {status: 'todo'})); }}
            : {label: 'Виконано сьогодні', icon: '✓', onSelect: function () { postStep(stepBody(s, {status: 'done'})); }},
      {label: 'Зараз у роботі', icon: '●', onSelect: function () { postStep(stepBody(s, {status: 'doing'})); }},
      {type: 'separator'},
      {label: 'Новий крок після цього…', icon: '➕', onSelect: function () { openStep('', {phase: s.phase, after: [s.id]}); }},
      {label: 'Віха після цього…', icon: '◆', onSelect: function () { openStep('', {phase: s.phase, after: [s.id], kind: 'milestone'}); }},
      {type: 'submenu', label: 'Посунути', icon: '↔', items: [
        {label: 'на 1 день раніше', icon: '◀', onSelect: function () { shiftStep(s, -1); }},
        {label: 'на 1 день пізніше', icon: '▶', onSelect: function () { shiftStep(s, 1); }},
        {label: 'на тиждень раніше', icon: '⏪', onSelect: function () { shiftStep(s, -7); }},
        {label: 'на тиждень пізніше', icon: '⏩', onSelect: function () { shiftStep(s, 7); }}]},
      {type: 'submenu', label: 'Після кроку', icon: '⛓', items: data.steps.filter(function (x) {
        return String(x.id) !== String(s.id);
      }).slice(0, 40).map(function (x) {
        var on = (s.after || []).map(String).indexOf(String(x.id)) >= 0;
        return {label: (on ? '✓ ' : '') + x.title, hint: phaseName(data, x.phase), data: true, onSelect: function () {
          var after = (s.after || []).filter(function (a) { return String(a) !== String(x.id); });
          if (!on) after.push(x.id);
          postStep(stepBody(s, {after: after}));
        }};
      })},
      {type: 'submenu', label: 'У фазу', icon: '📁', items: data.phases.map(function (p) {
        return {label: (String(p.id) === String(s.phase) ? '✓ ' : '') + ((p.icon || '') + ' ' + p.name).trim(), data: true,
                onSelect: function () { postStep(stepBody(s, {phase: p.id})); }};
      }).concat([{label: '— без фази', onSelect: function () { postStep(stepBody(s, {phase: ''})); }}])},
      {label: 'Дублювати', icon: '⧉', onSelect: function () { postStep(stepBody(s, {id: '', title: s.title + ' (копія)'})); }},
      {type: 'separator'},
      {label: 'Видалити крок', icon: '🗑', onSelect: function () {
        if (!confirm('Видалити крок «{t}»? Наступні кроки підуть після його попередників.'.replace('{t}', s.title))) return;
        send(box().dataset.planBase + 'step/' + s.id + '/delete/', {}).then(done);
      }}];
    if (s.after && s.after.length) {
      items.splice(8, 0, {label: 'Прибрати всі залежності', icon: '✂', onSelect: function () { postStep(stepBody(s, {after: []})); }});
    }
    window.MVMenu.open(ev, items, {title: s.title});
  }
  function phaseMenu(id, ev) {
    var items = [];
    if (id !== 'none') {
      items.push({label: 'Змінити фазу…', icon: '✏️', onSelect: function () { openPhase(id); }});
      items.push({label: 'Новий крок у цій фазі…', icon: '➕', onSelect: function () { openStep('', {phase: id}); }});
      items.push({label: 'Віха в цій фазі…', icon: '◆', onSelect: function () { openStep('', {phase: id, kind: 'milestone'}); }});
    }
    items.push({label: folds()[id] ? 'Розгорнути кроки' : 'Згорнути кроки', icon: folds()[id] ? '▾' : '▸',
                onSelect: function () { togglePhase(id); }});
    items.push({label: 'Нова фаза…', icon: '＋', onSelect: function () { openPhase(''); }});
    if (id !== 'none') {
      items.push({type: 'separator'});
      items.push({label: 'Видалити фазу', icon: '🗑', onSelect: function () {
        if (!confirm('Видалити фазу? Її кроки лишаться — без фази.')) return;
        send(box().dataset.planBase + 'phase/' + id + '/delete/', {}).then(done);
      }});
    }
    window.MVMenu.open(ev, items, {title: 'Фаза'});
  }
  function dayMenu(iso, ev) {
    var items = [
      {label: 'Новий крок з {d}…'.replace('{d}', dshort(iso)), icon: '➕', onSelect: function () { openStep('', {start: iso, after: []}); }},
      {label: 'Віха на {d}…'.replace('{d}', dshort(iso)), icon: '◆', onSelect: function () { openStep('', {kind: 'milestone', end: iso, after: []}); }},
      {label: 'Нова фаза…', icon: '＋', onSelect: function () { openPhase(''); }},
      {type: 'separator'},
      {label: 'Наблизити', icon: '🔍', onSelect: function () { setPref(GP, String(Math.min(ganttPpd() * 1.6, PPD_MAX))); applyGantt(); }},
      {label: 'Віддалити', icon: '🔭', onSelect: function () { setPref(GP, String(Math.max(ganttPpd() / 1.6, PPD_MIN))); applyGantt(); }}];
    window.MVMenu.open(ev, items, {title: dshort(iso)});
  }
  root.addEventListener('contextmenu', function (ev) {         // «📊 Діаграма Minerva»
    var g = ev.target.closest('[data-pjg-pane="ours"] g[data-step], [data-pjg-pane="ours"] g[data-phase]');
    if (!g || !canEdit() || ev.shiftKey || !window.MVMenu) return;
    ev.preventDefault();
    if (g.dataset.step) stepMenu(g.dataset.step, ev);
    else phaseMenu(g.dataset.phase, ev);
  });
  root.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-pjg-view],[data-pjg-scale],[data-pjg-detail-btn],[data-pjg-zoom]');
    if (!t || !root.contains(t)) return;
    ev.preventDefault();
    if (t.dataset.pjgView) setPref(GV, t.dataset.pjgView);
    if (t.dataset.pjgScale) { setPref(GS, t.dataset.pjgScale); setPref(GP, String(PPD[t.dataset.pjgScale])); }
    if (t.dataset.pjgDetailBtn) setPref(GD, t.dataset.pjgDetailBtn);
    if (t.dataset.pjgZoom) {
      var p = ganttPpd() * (t.dataset.pjgZoom === 'in' ? 1.6 : 1 / 1.6);
      setPref(GP, String(Math.min(Math.max(p, PPD_MIN), PPD_MAX)));
    }
    applyGantt();
  });
  applyGantt();

  // ── ⚙️ Поля проєкту: під пультом згорнуті; розгорнуті — коли форма з помилками або людина їх відкрила ──
  var FKEY = 'pj-fields-open';
  function fieldsBar() { var f = root.closest('form'); return f && f.querySelector('.pj-fields-bar'); }
  function setFields(open, scroll) {
    var form = root.closest('form'), bar = fieldsBar();
    if (!form || !bar) return;
    form.classList.toggle('pj-fields-collapsed', !open);
    bar.setAttribute('aria-expanded', open ? 'true' : 'false');
    bar.querySelector('b').textContent = (open ? '▾' : '▸') + ' ⚙️ Поля проєкту';
    try { localStorage.setItem(FKEY, open ? '1' : '0'); } catch (e) { /* лише на цю сторінку */ }
    if (open && scroll) bar.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function initFields() {                    // після DOMContentLoaded: поля форми йдуть після пульта
    var form = root.closest('form');
    if (!form || form.querySelector('.pj-fields-bar')) return;
    // поля й таблиці прив'язок згортаються; огляд AI, листування, Angebote й години лишаються на виду
    var parts = Array.prototype.filter.call(form.querySelectorAll('fieldset.module, .inline-group'), function (n) {
      if (root.contains(n)) return false;
      return n.classList.contains('inline-group') ||
        !!n.querySelector('.field-name, .field-code, .field-description, .field-created_by');
    });
    if (!parts.length) return;
    parts.forEach(function (n) { n.classList.add('pj-field-part'); });
    var first = parts[0];
    var bar = document.createElement('button');
    bar.type = 'button';
    bar.className = 'pj-fields-bar';
    bar.innerHTML = '<b></b> <span></span>';
    bar.querySelector('span').textContent = 'назва, клієнт, статус, строки, учасники, листування, замовлення й закупівлі';
    first.parentNode.insertBefore(bar, first);
    bar.addEventListener('click', function () { setFields(form.classList.contains('pj-fields-collapsed'), false); });
    var saved = '';
    try { saved = localStorage.getItem(FKEY) || ''; } catch (e) { saved = ''; }
    var errors = !!document.querySelector('.errornote, .errorlist');
    setFields(errors || saved === '1', false);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initFields);
  else initFields();
  root.addEventListener('click', function (e) {
    var t = e.target.closest('[data-pjc-fields]');
    if (!t || !root.contains(t)) return;
    e.preventDefault();
    setFields(true, true);
  });

  // ── 📅 Вікна кроку й фази: у <body>, поза формою картки (Enter не надсилає форму проєкту) ──
  function planData() {
    var n = root.querySelector('#pjp-data'), d = null;
    if (n) { try { d = JSON.parse(n.textContent); } catch (e) { d = null; } }
    d = d || {};
    ['steps', 'phases', 'users', 'links', 'offers', 'kinds', 'sides', 'statuses', 'triggers'].forEach(function (k) {
      d[k] = d[k] || [];
    });
    return d;
  }
  function q(d, attr, key) { return d.querySelector('[' + attr + '="' + key + '"]'); }
  function fillSelect(sel, items, value, empty) {
    sel.textContent = '';
    if (empty !== undefined) items = [['', empty]].concat(items);
    items.forEach(function (it) {
      var o = document.createElement('option');
      o.value = it[0];
      o.textContent = it[1];
      sel.appendChild(o);
    });
    sel.value = value === null || value === undefined ? '' : String(value);
    if (sel.selectedIndex < 0 && sel.options.length) sel.selectedIndex = 0;
  }
  function fillChecks(holder, items, chosen) {
    var want = (chosen || []).map(String);
    holder.textContent = '';
    items.forEach(function (it) {
      var l = document.createElement('label'), c = document.createElement('input');
      c.type = 'checkbox';
      c.value = it.id;
      c.checked = want.indexOf(String(it.id)) >= 0;
      l.appendChild(c);
      l.appendChild(document.createTextNode(it.label));
      holder.appendChild(l);
    });
    holder.hidden = !items.length;
  }
  function picked(holder) {
    return Array.prototype.map.call(holder.querySelectorAll('input:checked'), function (c) { return c.value; });
  }
  function phaseName(data, id) {
    var out = '';
    data.phases.forEach(function (p) { if (String(p.id) === String(id)) out = ((p.icon || '') + ' ' + p.name).trim(); });
    return out || 'без фази';
  }
  function defaultPhase() {
    var b = box(), li = b && (b.querySelector('.pjc-ph-active[data-pjc-phase]') || b.querySelector('.pjc-ph-planned[data-pjc-phase]'));
    if (!li && b) {
      var all = b.querySelectorAll('[data-pjc-phase]');
      li = all.length ? all[all.length - 1] : null;
    }
    return li ? li.dataset.pjcPhase : '';
  }
  function lastBefore(data, phase) {
    // Новий крок — після останнього кроку цієї фази (або попередніх фаз)
    var pos = {}, last = null, end = data.phases.length;
    data.phases.forEach(function (p, i) { pos[String(p.id)] = i; });
    var want = phase === '' || pos[String(phase)] === undefined ? end : pos[String(phase)];
    data.steps.forEach(function (x) {
      var p = x.phase === '' || pos[String(x.phase)] === undefined ? end : pos[String(x.phase)];
      if (p <= want) last = x;
    });
    return last;
  }
  function dialogs() {
    if (dlg) return dlg;
    var tpl = document.getElementById('pjp-dialogs');
    if (!tpl) return null;
    document.body.appendChild(tpl.content.cloneNode(true));
    dlg = {step: document.getElementById('pjp-step'), phase: document.getElementById('pjp-phase'),
           risk: document.getElementById('pjr-risk'), plan: document.getElementById('pja-plan'),
           agent: document.getElementById('pja-agent'), ask: document.getElementById('pja-ask')};
    wire(dlg.step, saveStep, deleteStep);
    wire(dlg.phase, savePhase, deletePhase);
    wire(dlg.risk, saveRisk, deleteRisk);
    wire(dlg.plan, applyPlanDraft, function () {}, false);
    wire(dlg.agent, startAgent, function () {}, false);
    wire(dlg.ask, askAiPlan, function () {}, false);
    dlg.step.querySelector('[data-pja-agent]').addEventListener('click', function () {
      var id = dlg.step.dataset.id;
      dlg.step.close();
      if (id) openAgent(id);
    });
    q(dlg.risk, 'data-r', 'probability').addEventListener('change', riskScore);
    q(dlg.risk, 'data-r', 'impact').addEventListener('change', riskScore);
    q(dlg.step, 'data-s', 'kind').addEventListener('change', syncStep);
    q(dlg.step, 'data-s', 'side').addEventListener('change', syncStep);
    // ⛓ кроки інших проєктів: пошук за кодом проєкту чи назвою кроку
    var xq = dlg.step.querySelector('[data-s-xq]');
    xq.addEventListener('input', function () {
      var t = xq.value.trim().toLowerCase();
      q(dlg.step, 'data-s-list', 'xafter').querySelectorAll('label').forEach(function (l) {
        l.hidden = !!t && l.textContent.toLowerCase().indexOf(t) < 0 && !l.querySelector('input').checked;
      });
    });
    // Початок + тривалість → кінець; початок і кінець → тривалість (дати ISO, рахуємо в UTC)
    var st = q(dlg.step, 'data-s', 'start'), en = q(dlg.step, 'data-s', 'end'), du = q(dlg.step, 'data-s', 'duration');
    du.addEventListener('input', function () {
      var n = parseInt(du.value, 10);
      if (st.value && n > 0) en.value = new Date(Date.parse(st.value) + (n - 1) * 864e5).toISOString().slice(0, 10);
    });
    [st, en].forEach(function (i) {
      i.addEventListener('change', function () {
        if (st.value && en.value && en.value >= st.value) du.value = Math.round((Date.parse(en.value) - Date.parse(st.value)) / 864e5) + 1;
      });
    });
    return dlg;
  }
  function wire(d, save, del, enter) {
    d.addEventListener('click', function (e) {
      var t = e.target.closest('[data-pjp-save],[data-pjp-close],[data-pjp-del]');
      if (!t || t.disabled) return;
      if (t.hasAttribute('data-pjp-save')) save();
      else if (t.hasAttribute('data-pjp-close')) d.close();
      else del();
    });
    if (enter === false) return;                    // вікна AI: Enter не запускає дію
    d.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'BUTTON') {
        e.preventDefault();
        save();
      }
    });
  }
  function post(d, url, body) {
    var btns = d.querySelectorAll('.pjp-foot button'), err = d.querySelector('[data-pjp-err]');
    btns.forEach(function (b) { b.disabled = true; });
    err.textContent = '';
    send(url, body).then(function (j) {
      btns.forEach(function (b) { b.disabled = false; });
      if (j && j.ok && j.html) { put(j.html); d.close(); return; }
      err.textContent = '⚠️ ' + ((j && j.error) || 'не вдалося');
    });
  }
  function syncStep() {
    var d = dlg.step, kind = q(d, 'data-s', 'kind').value, side = q(d, 'data-s', 'side').value;
    d.querySelectorAll('[data-only]').forEach(function (n) {
      var w = n.dataset.only;
      n.hidden = (w === 'task' && kind === 'milestone') || (w === 'milestone' && kind !== 'milestone') ||
        (w === 'person' && side !== 'we' && side !== 'colleague');
    });
  }
  function openStep(id, preset) {
    var ds = dialogs();
    if (!ds) return;
    var d = ds.step, data = planData(), s = null;
    data.steps.forEach(function (x) { if (String(x.id) === String(id)) s = x; });
    var isNew = !s;
    if (isNew) {
      var ph = preset && 'phase' in preset ? preset.phase : defaultPhase(), last = lastBefore(data, ph);
      s = {id: '', title: '', kind: 'task', phase: ph, side: 'we', assignee: '', start: '', end: '', duration: 1,
           status: 'todo', trigger: '', auto: true, after: last ? [last.id] : [], lag: 0, links: [], offers: [],
           notes: '', note: '', eff: ''};
      Object.keys(preset || {}).forEach(function (k) { s[k] = preset[k]; });   // правий клік: після кроку, з дня…
    }
    var f = function (k) { return q(d, 'data-s', k); };
    d.dataset.id = s.id;
    f('title').value = s.title;
    fillSelect(f('kind'), data.kinds, s.kind);
    fillSelect(f('phase'), data.phases.map(function (p) { return [p.id, ((p.icon || '') + ' ' + p.name).trim()]; }),
               s.phase, '— без фази');
    fillSelect(f('side'), data.sides, s.side);
    fillSelect(f('assignee'), data.users.map(function (u) { return [u.id, u.label]; }), s.assignee, '— не вказано');
    f('start').value = s.start || '';
    f('end').value = s.end || '';
    f('duration').value = s.duration || 1;
    fillSelect(f('status'), data.statuses, s.status);
    fillSelect(f('trigger'), data.triggers, s.trigger);
    f('auto').checked = s.auto !== false;
    f('lag').value = s.lag || 0;
    f('notes').value = s.notes || '';
    fillChecks(q(d, 'data-s-list', 'after'), data.steps.filter(function (x) { return String(x.id) !== String(s.id); })
      .map(function (x) { return {id: x.id, label: phaseName(data, x.phase) + ' · ' + x.title}; }), s.after);
    var xlist = q(d, 'data-s-list', 'xafter');
    fillChecks(xlist, data.others || [], s.after);
    d.querySelector('[data-s-xq]').value = '';
    var xn = xlist.querySelectorAll('input:checked').length;
    d.querySelector('[data-s-xcount]').textContent = xn ? '· ' + xn : '';
    d.querySelector('[data-s-x]').open = xn > 0;
    d.querySelector('[data-s-x]').hidden = !(data.others || []).length;
    fillChecks(q(d, 'data-s-list', 'links'), data.links, s.links);
    fillChecks(q(d, 'data-s-list', 'offers'), data.offers, s.offers);
    var note = d.querySelector('[data-s-note]'), eff = '';
    data.statuses.forEach(function (x) { if (x[0] === s.eff) eff = x[1]; });
    note.textContent = s.note ? '🔗 Зараз: ' + s.note + (s.eff && s.eff !== s.status ? ' · стан за ними: ' + eff : '') : '';
    note.hidden = !s.note;
    var ag = d.querySelector('[data-s-agent]'), agl = d.querySelector('[data-s-agent-tasks]');
    ag.hidden = isNew || !data.agent;
    agl.textContent = '';
    (s.agent || []).forEach(function (t) {
      var a = document.createElement('a');
      a.href = t.url;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = '🤖 ' + t.label;
      agl.appendChild(a);
    });
    d.querySelector('[data-pjp-del]').hidden = isNew;
    d.querySelector('[data-pjp-err]').textContent = '';
    d.querySelector('h3').textContent = isNew ? '📋 Новий крок плану' : '📋 Крок плану';
    syncStep();
    d.showModal();
    f('title').focus();
  }
  function saveStep() {
    var d = dlg.step, err = d.querySelector('[data-pjp-err]'), f = function (k) { return q(d, 'data-s', k); };
    var kind = f('kind').value, side = f('side').value;
    var body = {
      id: d.dataset.id || '', title: f('title').value, kind: kind, phase: f('phase').value, side: side,
      assignee: side === 'we' || side === 'colleague' ? f('assignee').value : '',
      start: kind === 'milestone' ? '' : f('start').value, end: f('end').value, duration: f('duration').value,
      status: f('status').value, trigger: f('trigger').value, auto: f('auto').checked, lag: f('lag').value,
      notes: f('notes').value,
      after: picked(q(d, 'data-s-list', 'after')).concat(picked(q(d, 'data-s-list', 'xafter'))),
      links: picked(q(d, 'data-s-list', 'links')), offers: picked(q(d, 'data-s-list', 'offers'))
    };
    if (!body.title.trim()) { err.textContent = 'Напишіть назву кроку'; f('title').focus(); return; }
    if (body.start && body.end && body.end < body.start) { err.textContent = 'Кінець раніше за початок'; return; }
    post(d, box().dataset.planStep, body);
  }
  function deleteStep() {
    var d = dlg.step;
    if (!d.dataset.id) return;
    if (!confirm('Видалити крок «' + q(d, 'data-s', 'title').value + '»? Наступні за ним кроки підуть після його попередників.')) return;
    post(d, box().dataset.planBase + 'step/' + d.dataset.id + '/delete/', {});
  }
  function openPhase(id) {
    var ds = dialogs();
    if (!ds) return;
    var d = ds.phase, data = planData(), p = null;
    data.phases.forEach(function (x) { if (String(x.id) === String(id)) p = x; });
    var isNew = !p, f = function (k) { return q(d, 'data-p', k); };
    p = p || {id: '', name: '', icon: '', gate: '', order: ''};
    d.dataset.id = p.id;
    f('icon').value = p.icon || '';
    f('name').value = p.name || '';
    f('gate').value = p.gate || '';
    f('order').value = p.order === '' || p.order === undefined ? '' : p.order;
    d.querySelector('[data-pjp-del]').hidden = isNew;
    d.querySelector('[data-pjp-err]').textContent = '';
    d.querySelector('h3').textContent = isNew ? '🗂 Нова фаза плану' : '🗂 Фаза плану';
    d.showModal();
    f('name').focus();
  }
  function savePhase() {
    var d = dlg.phase, f = function (k) { return q(d, 'data-p', k); };
    var body = {id: d.dataset.id || '', name: f('name').value, icon: f('icon').value, gate: f('gate').value,
                order: f('order').value};
    if (!body.name.trim()) { d.querySelector('[data-pjp-err]').textContent = 'Напишіть назву фази'; f('name').focus(); return; }
    post(d, box().dataset.planPhase, body);
  }
  function deletePhase() {
    var d = dlg.phase;
    if (!d.dataset.id) return;
    if (!confirm('Видалити фазу «' + q(d, 'data-p', 'name').value + '»? Її кроки лишаться — без фази.')) return;
    post(d, box().dataset.planBase + 'phase/' + d.dataset.id + '/delete/', {});
  }
  function applyTemplate(btn) {
    var b = box(), sel = b.querySelector('[data-pjc-tpl]'), st = b.querySelector('[data-pjc-tpl-start]');
    if (!sel || !sel.value) return;
    btn = btn || b.querySelector('[data-pjc-tpl-apply]');
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '🧩 Складаю план…';
    send(btn.dataset.url, {template: sel.value, start: st ? st.value : ''}).then(function (j) {
      if (!done(j)) { btn.disabled = false; btn.textContent = label; }
    });
  }
  function send(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({range: range()}, body || {}))
    }).then(function (r) {
      return r.json().catch(function () { return {ok: false, error: 'HTTP ' + r.status}; });
    }).catch(function () { return {ok: false, error: 'сервер не відповідає'}; });
  }
  function load(key) {
    var b = box();
    if (!b) return;
    fetch(b.dataset.cockpit + '?range=' + encodeURIComponent(key), {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.ok) put(j.html); })
      .catch(function () {});
  }
  applyTimeline();

  function addMoment(preset) {
    var f = root.querySelector('[data-pjc-mform]'), body = {}, err = f && f.querySelector('.pjc-merr');
    if (preset) {
      body = preset;
    } else if (f) {
      f.querySelectorAll('[data-f]').forEach(function (i) { body[i.dataset.f] = i.value; });
    }
    if (!String(body.title || '').trim()) {
      if (err) err.textContent = 'Напишіть, що сталося';
      return;
    }
    body.cockpit = true;
    send(box().dataset.highlight, body).then(function (j) {
      if (!done(j) && err) err.textContent = (j && j.error) || '';
    });
  }
  // 📅 Ромб терміну чи зустрічі: ✓ виконано / відбулась, зустріч чи термін, лист (календар — лише свій)
  function calPost(url, body) {
    return fetch(url, {method: 'POST', credentials: 'same-origin',
                       headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/json'},
                       body: JSON.stringify(body || {})})
      .then(function (r) { return r.ok; }).catch(function () { return false; });
  }
  function calAll(c, fn, text) {
    Promise.all(c.ids.map(fn)).then(function (res) {
      load(range());
      toast(res.every(Boolean) ? text : '⚠️ не вдалося змінити подію в календарі');
    });
  }
  function calMenu(g, e) {
    var c = {};
    try { c = JSON.parse(g.getAttribute('data-cal') || '{}'); } catch (x) { c = {}; }
    if (!c.ids || !c.ids.length) return;
    if (!window.MVMenu) {
      if (c.mail) window.open('/email/message/' + c.mail + '/', '_blank', 'noopener');
      return;
    }
    var items = [];
    if (!c.done) {
      items.push({label: c.meeting ? 'Відбулась' : 'Виконано', icon: '✓', onSelect: function () {
        calAll(c, function (id) { return calPost('/calendar/event/' + id + '/done/'); }, '✓ Позначено в календарі');
      }});
    } else {
      items.push({label: 'Ще не виконано', icon: '↺', onSelect: function () {
        calAll({ids: [c.ids[0]]}, function (id) { return calPost('/calendar/event/' + id + '/toggle/'); },
               '↺ Позначку знято');
      }});
    }
    items.push(c.meeting
      ? {label: 'Це термін, не зустріч', icon: '⏰', onSelect: function () {
          calAll(c, function (id) { return calPost('/calendar/event/' + id + '/set-type/', {event_type: 'deadline'}); },
                 '⏰ Тепер це термін');
        }}
      : {label: 'Це зустріч, не термін', icon: '🤝', onSelect: function () {
          calAll(c, function (id) { return calPost('/calendar/event/' + id + '/set-type/', {event_type: 'meeting'}); },
                 '🤝 Тепер це зустріч — минула не буде простроченою');
        }});
    if (c.mail) {
      items.push({type: 'separator'});
      items.push({label: 'Відкрити лист', icon: '✉️', href: '/email/message/' + c.mail + '/', target: '_blank'});
    }
    if (c.linked) {
      items.push({type: 'separator'});
      items.push({label: 'Прибрати з проєкту', icon: '✕', onSelect: function () {
        send(box().dataset.eventLink, {event: c.ids[0], link: false}).then(done);
      }});
    }
    window.MVMenu.open(e, items, {title: 'Подія в календарі'});
  }
  // ✓ «Що зараз» / «Ризики»: відповіли поза поштою (лист) або виконано / зустріч відбулась (подія календаря)
  var HOWS = [['phone', '📞', 'Відповіли телефоном'], ['meeting', '🤝', 'Обговорили на зустрічі'],
              ['other', '✉️', 'Відповіли іншим шляхом'], ['not_needed', '🙅', 'Відповідь не потрібна']];
  function handled(ids, how, undo) {
    send(box().dataset.mailHandled, {messages: ids, how: how || '', undo: !!undo}).then(function (j) {
      if (!j || !j.ok) { toast('⚠️ ' + ((j && j.error) || 'не вдалося')); return; }
      put(j.html);
      if (!undo && window.MVMenu && window.MVMenu.toast) {
        window.MVMenu.toast(j.notice || '✓', {undo: function () { handled(j.marked, '', true); }});
      }
    });
  }
  function actMenu(btn, e) {
    var a = {};
    try { a = JSON.parse(btn.getAttribute('data-pjc-act') || '{}'); } catch (x) { a = {}; }
    if (a.ev) {
      var g = document.createElement('i');
      g.setAttribute('data-cal', JSON.stringify({ids: a.ev, meeting: a.meeting, mail: a.mail}));
      calMenu(g, e);
      return;
    }
    if (!a.m || !window.MVMenu) return;
    var same = [];
    root.querySelectorAll('[data-pjc-act]').forEach(function (b) {
      var o = {};
      try { o = JSON.parse(b.getAttribute('data-pjc-act') || '{}'); } catch (x) { o = {}; }
      if (o.m && o.dir === a.dir && o.who && o.who === a.who) same.push(o.m);
    });
    var items = HOWS.map(function (h) {
      return {label: h[2], icon: h[1], onSelect: function () { handled([a.m], h[0]); }};
    });
    if (same.length > 1) {
      items.push({type: 'submenu', label: 'Усі листи цієї людини ({0})'.replace('{0}', same.length), icon: '✓',
                  items: HOWS.map(function (h) {
                    return {label: h[2], icon: h[1], onSelect: function () { handled(same, h[0]); }};
                  })});
    }
    items.push({type: 'separator'});
    items.push({label: 'Відкрити лист', icon: '✉️', href: '/email/message/' + a.m + '/', target: '_blank'});
    window.MVMenu.open(e, items, {title: a.dir === 'in' ? 'Як ми відповіли?' : 'Як отримали відповідь?'});
  }
  // 📎 Кілька файлів дня — меню зі списком (один файл відкривається одразу посиланням)
  function filesMenu(g, e) {
    var files = [];
    try { files = JSON.parse(g.getAttribute('data-files') || '[]'); } catch (x) { files = []; }
    if (!files.length) return;
    if (!window.MVMenu) { window.open(files[0].u, '_blank', 'noopener'); return; }
    var items = [];
    files.forEach(function (f) {
      items.push({label: f.n, icon: '📎', data: true, href: f.u, target: '_blank'});
    });
    items.push({type: 'separator'});
    items.push({label: 'Лист з першим файлом', icon: '✉️', href: files[0].m, target: '_blank'});
    window.MVMenu.open(e, items, {title: 'Файли дня'});
  }
  // 📅 ＋ Подія з календаря: власні події навколо сьогодні, ще не в проєкті → прив'язати
  function eventAdd(btn) {
    fetch(box().dataset.events, {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var list = (j && j.events) || [];
        if (!list.length) { toast('У вашому календарі навколо сьогодні немає подій поза проєктом'); return; }
        var items = list.map(function (ev) {
          return {label: ev.title, hint: ev.date, icon: '📅', data: true, onSelect: function () {
            send(box().dataset.eventLink, {event: ev.id, link: true}).then(done);
          }};
        });
        if (window.MVMenu) window.MVMenu.open(btn, items, {title: 'Подія до проєкту'});
      })
      .catch(function () { toast('⚠️ сервер не відповідає'); });
  }
  function mailMenu(g, e) {
    var mails = [];
    try { mails = JSON.parse(g.getAttribute('data-mails') || '[]'); } catch (x) { mails = []; }
    if (!mails.length) return;
    var canAdd = !!root.querySelector('[data-pjc-mtoggle]');
    if (!window.MVMenu) { window.open('/email/message/' + mails[0].pk + '/', '_blank', 'noopener'); return; }
    var items = [];
    mails.forEach(function (m, i) {
      if (i) items.push({type: 'separator'});
      items.push({label: m.s, icon: '✉️', data: true, href: '/email/message/' + m.pk + '/', target: '_blank'});
      if (canAdd) items.push({label: 'Позначити як важливий момент', icon: '📌', onSelect: function () {
        addMoment({title: m.s, date: m.d, message: m.pk, kind: 'other'});
      }});
    });
    window.MVMenu.open(e, items, {title: 'Листи дня'});
  }

  function openRow(t) {
    if (!box().dataset.canChange || Date.now() < noClickUntil) return;   // перегляд — підказка; щойно тягнули — ні
    if (t.dataset.step) openStep(t.dataset.step);
    else openPhase(t.dataset.phase || t.dataset.pjcPhase);
  }

  // ── ⚠️ Вікно ризику (те саме перенесення в <body>, що й вікна кроку) ──
  function riskData() {
    var n = root.querySelector('#pjr-data'), d = null;
    if (n) { try { d = JSON.parse(n.textContent); } catch (e) { d = null; } }
    d = d || {};
    ['risks', 'users', 'steps', 'categories', 'statuses', 'probabilities', 'impacts'].forEach(function (k) {
      d[k] = d[k] || [];
    });
    return d;
  }
  function riskScore() {
    var d = dlg.risk, p = +q(d, 'data-r', 'probability').value || 0, i = +q(d, 'data-r', 'impact').value || 0;
    var s = p * i, b = d.querySelector('[data-r-score]');
    b.textContent = 'бал ' + s;
    b.className = 'pjr-score pjr-' + (s >= 6 ? 'red' : (s >= 3 ? 'yellow' : 'green'));
  }
  function openRisk(id) {
    var ds = dialogs();
    if (!ds) return;
    var d = ds.risk, data = riskData(), r = null;
    data.risks.forEach(function (x) { if (String(x.id) === String(id)) r = x; });
    var isNew = !r, f = function (k) { return q(d, 'data-r', k); };
    r = r || {id: '', title: '', description: '', category: 'schedule', probability: 2, impact: 2, owner: data.owner,
              mitigation: '', status: 'open', review_on: data.review || '', source: 'manual', evidence: '', rule: '',
              steps: []};
    var rule = r.source === 'rule';
    d.dataset.id = r.id;
    f('title').value = r.title;
    f('title').readOnly = rule;
    f('description').value = r.description || '';
    fillSelect(f('category'), data.categories, r.category);
    fillSelect(f('probability'), data.probabilities, r.probability);
    fillSelect(f('impact'), data.impacts, r.impact);
    fillSelect(f('owner'), data.users.map(function (u) { return [u.id, u.label]; }), r.owner, '— без власника');
    fillSelect(f('status'), data.statuses, r.status);
    f('review_on').value = r.review_on || '';
    f('mitigation').value = r.mitigation || '';
    fillChecks(q(d, 'data-r-list', 'steps'), data.steps, r.steps);
    var ev = d.querySelector('[data-r-evidence]');
    ev.textContent = rule ? '⚙️ ' + (r.evidence || ('Правило «' + r.rule + '»')) + ' · '
      + 'Ризик відкрило правило — закриється сам, коли умова зникне.' : (r.evidence || '');
    ev.hidden = !ev.textContent;
    d.querySelector('[data-r-desc]').hidden = rule;
    d.querySelector('[data-pjp-del]').hidden = isNew || rule;
    d.querySelector('[data-pjp-err]').textContent = '';
    d.querySelector('h3').textContent = isNew ? '⚠️ Новий ризик' : '⚠️ Ризик';
    riskScore();
    d.showModal();
    (rule ? f('mitigation') : f('title')).focus();
  }
  function saveRisk() {
    var d = dlg.risk, f = function (k) { return q(d, 'data-r', k); };
    var body = {id: d.dataset.id || '', title: f('title').value, description: f('description').value,
                category: f('category').value, probability: f('probability').value, impact: f('impact').value,
                owner: f('owner').value, status: f('status').value, review_on: f('review_on').value,
                mitigation: f('mitigation').value, steps: picked(q(d, 'data-r-list', 'steps'))};
    if (!body.title.trim()) { d.querySelector('[data-pjp-err]').textContent = 'Напишіть, що може статися'; f('title').focus(); return; }
    post(d, box().dataset.risk, body);
  }
  function deleteRisk() {
    var d = dlg.risk;
    if (!d.dataset.id) return;
    if (!confirm('Видалити ризик «' + q(d, 'data-r', 'title').value + '»?')) return;
    post(d, box().dataset.risk + d.dataset.id + '/delete/', {});
  }

  // ── 📝 Тижневий статус ──
  function statusBox() { return root.querySelector('.pjs-form'); }
  function statusErr(text) {
    var n = root.querySelector('[data-pjs-err]');
    if (n) n.textContent = text || '';
  }
  function saveStatus(btn) {
    var f = statusBox();
    if (!f) return;
    var on = f.querySelector('[data-pjs-health].on'), text = f.querySelector('[data-pjs-text]');
    if (!on) { statusErr('Виберіть колір'); return; }
    if (!text.value.trim()) { statusErr('Напишіть, як справи на проєкті'); text.focus(); return; }
    btn.disabled = true;
    send(box().dataset.statusSave, {health: on.dataset.pjsHealth, text: text.value, ai: text.dataset.ai === '1'})
      .then(function (j) { if (!done(j)) { btn.disabled = false; statusErr((j && j.error) || ''); } });
  }
  function aiStatus(btn) {
    var f = statusBox();
    if (!f) return;
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '🧠 Пишу…';
    statusErr('');
    send(box().dataset.statusDraft, {}).then(function (j) {
      btn.disabled = false;
      btn.textContent = label;
      if (j && j.ok && j.text) {
        var t = f.querySelector('[data-pjs-text]');
        t.value = j.text;
        t.dataset.ai = '1';
        t.focus();
      } else {
        statusErr('⚠️ ' + ((j && j.error) || 'AI не відповів'));
      }
    });
  }

  // ── 🧠 AI-помічник: моменти й ризики з листів, забуті листи, чернетка плану, доручення агенту ──
  function aiErr(text) {
    var n = root.querySelector('[data-pja-err]');
    if (n) n.textContent = text || '';
    if (text) toast(text);
  }
  function busy(btn, text) {
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = text;
    return function () { btn.disabled = false; btn.textContent = label; };
  }
  // Довга дія AI у фоні: POST запускає, далі GET кожні poll_ms, доки не готово (проксі NAS чекає лише ≈ 60 с)
  var BG_POLLS = 100;
  function bgRun(url, onDone, onFail, body) {
    var polls = 0;
    function poll() {
      fetch(url + '?range=' + encodeURIComponent(range()), {credentials: 'same-origin'})
        .then(function (r) { return r.json().catch(function () { return {ok: true, pending: true}; }); })
        .catch(function () { return {ok: true, pending: true}; })
        .then(step);
    }
    function step(j) {
      if (j && j.ok && j.pending) {
        if (++polls > BG_POLLS) { onFail('AI довго не відповідає — спробуйте пізніше'); return; }
        setTimeout(poll, j.poll_ms || 3000);
        return;
      }
      if (!j || !j.ok) { onFail((j && j.error) || 'не вдалося'); return; }
      onDone(j);
    }
    send(url, body || {}).then(step);
  }
  function aiScan(btn) {
    var undo = busy(btn, '🧠 AI читає листи… до 2 хвилин');
    aiErr('');
    bgRun(box().dataset.aiScan, function (j) {
      if (j.html) { put(j.html); return; }
      undo();
      aiErr('⚠️ не вдалося');
    }, function (err) { undo(); aiErr('⚠️ ' + err); });
  }
  function aiDecide(btn) {
    btn.disabled = true;
    send(box().dataset.aiBase + btn.dataset.pjaDecide + '/', {accept: btn.dataset.accept === '1'}).then(function (j) {
      if (j && j.ok && j.html) { put(j.html); return; }
      btn.disabled = false;
      aiErr('⚠️ ' + ((j && j.error) || 'не вдалося'));
    });
  }

  // 🔎 Забуті листи: список живе в пам'яті сторінки й малюється знову після кожного оновлення пульта
  var found = null;
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function foundRow(r, ul) {
    var li = el('li', 'pja-f'), a = el('a', '', r.subject);
    a.href = r.url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.setAttribute('translate', 'no');
    li.appendChild(document.createTextNode('✉️ '));
    li.appendChild(a);
    var meta = [r.date, r.from].filter(Boolean).join(' · ');
    if (meta) {
      var m = el('span', 'pjc-sub', ' · ' + meta);
      m.setAttribute('translate', 'no');
      li.appendChild(m);
    }
    var yn = el('span', 'pjc-yn'), state = found.decided[r.thread];
    if (state) {
      yn.textContent = state;
    } else {
      [['1', '✓', 'Так — додати гілку до проєкту'], ['0', '✗', 'Ні — не стосується цього проєкту']].forEach(function (x) {
        var b = el('button', 'pjc-b', x[1]);
        b.type = 'button';
        b.title = x[2];
        b.dataset.pjaFdecide = r.thread;
        b.dataset.accept = x[0];
        yn.appendChild(b);
      });
    }
    li.appendChild(yn);
    if (r.reasons && r.reasons.length) li.appendChild(el('div', 'pjc-sub', r.reasons.join(' · ')));
    ul.appendChild(li);
  }
  function renderFound() {
    var holder = root.querySelector('[data-pja-found]');
    if (!holder) return;
    if (!found) { holder.hidden = true; holder.textContent = ''; return; }
    var seen = {}, rows = [];
    found.rows.concat(found.ragRows).forEach(function (r) {
      if (!seen[r.thread]) { seen[r.thread] = 1; rows.push(r); }
    });
    holder.hidden = false;
    holder.textContent = '';
    var h = el('div', 'pja-fh', '🔎 Забуті листи · ' + rows.length + ' '), x = el('button', 'pjc-x', '✕');
    x.type = 'button';
    x.title = 'Сховати';
    x.setAttribute('aria-label', 'Сховати');
    x.dataset.pjaFclose = '1';
    h.appendChild(x);
    holder.appendChild(h);
    if (rows.length) {
      var ul = el('ul', 'pjc-list');
      rows.forEach(function (r) { foundRow(r, ul); });
      holder.appendChild(ul);
    }
    var note = '';
    if (found.rag && !found.ragDone) note = '🔎 База знань шукає…';
    else if (found.rag && found.message) note = 'База знань: ' + found.message;
    if (!rows.length && (!found.rag || found.ragDone)) {
      note = (note ? note + ' · ' : '') + (found.rag
        ? 'Нічого не знайшлося: шукали за учасниками й клієнтом проєкту, номерами замовлень і закупівель, артикулами й у базі знань.'
        : 'Нічого не знайшлося: шукали за учасниками й клієнтом проєкту, номерами замовлень і закупівель та артикулами.');
    }
    if (note) holder.appendChild(el('div', 'pjc-sub', note));
  }
  function pollFound() {
    if (!found || !found.job) return;
    var job = found.job;
    fetch(box().dataset.forgotten + job + '/', {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!found || found.job !== job) return;
        if (!j || !j.ok) {
          found.ragDone = true;
          found.job = '';
          found.message = (j && j.error) || 'не відповіла';
        } else {
          found.ragRows = j.rag_rows || [];
          found.ragDone = !!j.done;
          found.message = j.message || '';
          found.job = j.job || '';
        }
        renderFound();
        if (found.job) setTimeout(pollFound, (j && j.poll_ms) || 1500);
      })
      .catch(function () {
        if (!found || found.job !== job) return;
        found.ragDone = true;
        found.job = '';
        found.message = 'сервер не відповідає';
        renderFound();
      });
  }
  function findForgotten(btn) {
    var undo = busy(btn, '🔎 Шукаю…');
    aiErr('');
    send(box().dataset.forgotten, {}).then(function (j) {
      undo();
      if (!j || !j.ok) { aiErr('⚠️ ' + ((j && j.error) || 'не вдалося')); return; }
      found = {rows: j.rows || [], rag: !!j.rag, ragRows: j.rag_rows || [], ragDone: !!j.done, message: j.message || '',
               job: j.job || '', decided: {}};
      renderFound();
      var holder = root.querySelector('[data-pja-found]');
      if (holder) holder.scrollIntoView({behavior: 'smooth', block: 'nearest'});
      if (found.job) setTimeout(pollFound, j.poll_ms || 1500);
    });
  }
  function forgottenDecide(btn) {
    var accept = btn.dataset.accept === '1', thread = btn.dataset.pjaFdecide;
    btn.disabled = true;
    send(box().dataset.forgottenDecide, {thread: thread, accept: accept}).then(function (j) {
      if (!j || !j.ok) { btn.disabled = false; aiErr('⚠️ ' + ((j && j.error) || 'не вдалося')); return; }
      if (found) found.decided[thread] = accept ? '✓ додано' : '✕ не стосується';
      if (j.notice) toast(j.notice);
      if (accept) load(range());
      else renderFound();
    });
  }

  // 🧠 Чернетка плану з листування: перегляд у вікні, галочки кроків, «✓ Застосувати план»
  var SIDE_LABELS = {we: 'ми', customer: 'клієнт', supplier: 'постачальник', colleague: 'колега'};
  // Спершу вікно: що врахувати / що вже відбулось, листи з якої дати, старт, замінити план — далі чернетка у фоні
  var askBtn = null;
  function aiPlan(btn) {
    var ds = dialogs();
    if (!ds || !ds.ask) return;
    var d = ds.ask, b = box(), n = parseInt(b.dataset.steps || '0', 10) || 0;
    askBtn = btn;
    q(d, 'data-pa', 'since').value = b.dataset.aiSince || '';
    q(d, 'data-pa', 'start').value = b.dataset.start || '';
    q(d, 'data-pa', 'replace').checked = false;
    d.querySelector('[data-pa-replace-row]').hidden = !b.dataset.hasPlan;
    d.querySelector('[data-pa-steps]').textContent = String(n);
    d.querySelector('[data-pjp-err]').textContent = '';
    d.showModal();
    q(d, 'data-pa', 'wish').focus();
  }
  function askAiPlan() {
    var d = dlg.ask, b = box(), replace = q(d, 'data-pa', 'replace').checked;
    if (b.dataset.hasPlan && !replace) {
      d.querySelector('[data-pjp-err]').textContent = 'У проєкті вже є план — позначте «Замінити поточний план».';
      return;
    }
    var body = {wish: q(d, 'data-pa', 'wish').value, since: q(d, 'data-pa', 'since').value,
                start: q(d, 'data-pa', 'start').value, replace: replace};
    d.close();
    var btn = askBtn && root.contains(askBtn) ? askBtn : root.querySelector('[data-pja-plan]');
    var undo = btn ? busy(btn, '🧠 AI складає план… 1–3 хвилини') : function () {};
    aiErr('');
    bgRun(b.dataset.aiPlan, function (j) { undo(); openPlanDraft(j.draft); },
          function (err) { undo(); aiErr('⚠️ ' + err); }, body);
  }
  function aiSettings(btn) {
    var b = box(), set = btn.closest('.pja-set');
    btn.disabled = true;
    send(b.dataset.aiSettings, {since: set.querySelector('[data-pja-since]').value,
                                start: set.querySelector('[data-pja-start]').value})
      .then(function (j) { if (!done(j)) btn.disabled = false; });
  }
  function openPlanDraft(d) {
    var ds = dialogs();
    if (!ds || !d) return;
    var dl = ds.plan, list = dl.querySelector('[data-pd-list]'), i = 0;
    q(dl, 'data-pd', 'start').value = d.start || '';
    dl.querySelector('[data-pd-info]').textContent = [d.template ? 'на основі шаблону «' + d.template + '»' : '',
      'кроків ' + d.steps, 'ваших листів прочитано ' + d.mails,
      d.since ? 'листи з {d}'.replace('{d}', d.since.split('-').reverse().join('.')) : '', d.wish ? 'з вашою підказкою' : '',
      d.model].filter(Boolean).join(' · ');
    var warn = dl.querySelector('[data-pd-replace]');
    if (warn) {
      warn.hidden = !d.replace;
      warn.textContent = d.replace ? '⚠️ «✓ Застосувати план» замінить поточний план (кроків: {n}): старі кроки, їхні дати й прив’язки зникнуть.'
        .replace('{n}', d.had || 0) : '';
    }
    var notes = dl.querySelector('[data-pd-notes]');
    notes.textContent = d.notes || '';
    notes.hidden = !d.notes;
    list.textContent = '';
    (d.phases || []).forEach(function (ph) {
      var head = el('div', 'pjd-ph', ((ph.icon || '') + ' ' + ph.name).trim());
      head.setAttribute('translate', 'no');
      if (ph.gate) head.appendChild(el('span', 'pjc-sub', ' · умова переходу: ' + ph.gate));
      list.appendChild(head);
      (ph.steps || []).forEach(function (st) {
        var row = el('label', 'pjd-st'), c = document.createElement('input');
        c.type = 'checkbox';
        c.checked = true;
        c.value = i++;
        row.appendChild(c);
        var body = el('span', 'pjd-body'), t = el('b', '', (st.kind === 'milestone' ? '◆ ' : '') + st.title);
        t.setAttribute('translate', 'no');
        body.appendChild(t);
        var meta = [st.kind === 'milestone' ? 'віха' : st.days + ' дн.', SIDE_LABELS[st.side] || st.side];
        if (st.history) meta.push('⏱ як у минулих проєктах: ' + st.history.days + ' дн. · кроків ' + st.history.n);
        if (st.done) meta.push('✓ уже зроблено ' + st.done.split('-').reverse().join('.') +
                               (st.ref && st.ref.note ? ' — за вашою підказкою' : ''));
        body.appendChild(el('small', 'pjc-sub', ' · ' + meta.join(' · ')));
        if (st.ref && st.ref.message) {
          var a = el('a', '', ' ✉️ #' + st.ref.n);
          a.href = '/email/message/' + st.ref.message + '/';
          a.target = '_blank';
          a.rel = 'noopener';
          body.appendChild(a);
        }
        if (st.why) {
          var w = el('div', 'pjc-sub', st.why);
          w.setAttribute('translate', 'no');
          body.appendChild(w);
        }
        row.appendChild(body);
        list.appendChild(row);
      });
    });
    dl.querySelector('[data-pjp-err]').textContent = '';
    dl.showModal();
  }
  function applyPlanDraft() {
    var dl = dlg.plan, skip = [];
    dl.querySelectorAll('[data-pd-list] input[type=checkbox]').forEach(function (c) { if (!c.checked) skip.push(+c.value); });
    post(dl, box().dataset.aiPlanApply, {skip: skip, start: q(dl, 'data-pd', 'start').value});
  }

  // 🤖 Доручити крок агенту: варіанти доручення з кроку, текст і адреси можна змінити, запуск — завдання агенту
  var agentStep = '';
  function fillAgent(d, p) {
    q(d, 'data-ag', 'instruction').value = p.instruction || '';
    q(d, 'data-ag', 'mail_to').value = p.mail_to || '';
  }
  function openAgent(stepId) {
    var ds = dialogs();
    if (!ds) return;
    var d = ds.agent, list = d.querySelector('[data-ag-presets]');
    agentStep = stepId;
    d.querySelector('[data-pjp-err]').textContent = '';
    d.querySelector('[data-ag-done]').hidden = true;
    d.querySelector('[data-ag-start]').disabled = true;
    list.textContent = '…';
    fillAgent(d, {});
    d.showModal();
    fetch(box().dataset.planBase + 'step/' + stepId + '/agent/', {credentials: 'same-origin'})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || !j.ok) { list.textContent = '⚠️ ' + ((j && j.error) || 'не вдалося'); return; }
        d.querySelector('[data-ag-step]').textContent = j.step.title;
        list.textContent = '';
        (j.presets || []).forEach(function (p, i) {
          var l = el('label'), r = document.createElement('input');
          r.type = 'radio';
          r.name = 'pja-preset';
          r.checked = i === 0;
          r.addEventListener('change', function () { fillAgent(d, p); });
          l.appendChild(r);
          l.appendChild(document.createTextNode(' ' + p.label));
          list.appendChild(l);
        });
        if ((j.presets || []).length) fillAgent(d, j.presets[0]);
        fillSelect(q(d, 'data-ag', 'model'), j.models || [], j.model);
        q(d, 'data-ag', 'budget').value = j.budget || '';
        var tl = d.querySelector('[data-ag-tasks]');
        tl.textContent = '';
        (j.tasks || []).forEach(function (t) {
          var a = el('a', '', '🤖 #' + t.id + ' ' + t.label + ' ');
          a.href = t.url;
          a.target = '_blank';
          a.rel = 'noopener';
          tl.appendChild(a);
        });
        tl.hidden = !(j.tasks || []).length;
        d.querySelector('[data-ag-start]').disabled = !j.can;
        if (!j.can) d.querySelector('[data-pjp-err]').textContent = 'Немає права давати завдання агенту';
        q(d, 'data-ag', 'instruction').focus();
      })
      .catch(function () { list.textContent = '⚠️ сервер не відповідає'; });
  }
  function startAgent() {
    var d = dlg.agent, f = function (k) { return q(d, 'data-ag', k); };
    var err = d.querySelector('[data-pjp-err]'), start = d.querySelector('[data-ag-start]');
    var body = {instruction: f('instruction').value, mail_to: f('mail_to').value, model: f('model').value,
                budget: f('budget').value};
    start.disabled = true;
    err.textContent = '';
    send(box().dataset.planBase + 'step/' + agentStep + '/agent/', body).then(function (j) {
      if (j && j.ok) {
        if (j.html) put(j.html);
        var ok = d.querySelector('[data-ag-done]');
        ok.querySelector('a').href = j.url;
        ok.hidden = false;
        return;
      }
      start.disabled = false;
      err.textContent = '⚠️ ' + ((j && j.error) || 'не вдалося');
    });
  }

  root.addEventListener('click', function (e) {
    var t = e.target.closest('[data-pja-scan],[data-pja-decide],[data-pja-forgotten],[data-pja-fdecide],' +
      '[data-pja-fclose],[data-pja-plan],[data-pja-settings]');
    if (!t || !root.contains(t) || t.disabled) return;
    e.preventDefault();
    if (t.hasAttribute('data-pja-scan')) aiScan(t);
    else if (t.dataset.pjaDecide) aiDecide(t);
    else if (t.hasAttribute('data-pja-forgotten')) findForgotten(t);
    else if (t.dataset.pjaFdecide) forgottenDecide(t);
    else if (t.dataset.pjaFclose) { found = null; renderFound(); }
    else if (t.hasAttribute('data-pja-plan')) aiPlan(t);
    else if (t.hasAttribute('data-pja-settings')) aiSettings(t);
  });

  root.addEventListener('click', function (e) {
    var t = e.target.closest('[data-pjc-range],[data-pjc-decide],[data-pjc-scan],[data-pjc-mtoggle],[data-pjc-madd],' +
      '[data-pjc-mdel],g[data-mails],g[data-cal],g[data-files],[data-pjc-event-add],[data-pjc-tab],[data-pjc-step-new],[data-pjc-phase-new],[data-pjc-post],' +
      '[data-pjc-save-tpl],[data-pjc-tpl-apply],[data-pjc-phase],g[data-step],g[data-phase],[data-pjc-risk],' +
      '[data-pjc-risk-new],[data-pjs-health],[data-pjs-save],[data-pjs-ai],[data-pjc-act],[data-pjc-unmark]');
    if (!t || !root.contains(t)) return;
    e.preventDefault();
    if (t.hasAttribute('data-pjc-act')) { actMenu(t, e); return; }
    if (t.hasAttribute('data-pjc-unmark')) { handled([+t.getAttribute('data-pjc-unmark')], '', true); return; }
    if (t.matches('g[data-mails]')) { mailMenu(t, e); return; }
    if (t.matches('g[data-cal]')) { calMenu(t, e); return; }
    if (t.matches('g[data-files]')) { filesMenu(t, e); return; }
    if (t.hasAttribute('data-pjc-event-add')) { eventAdd(t); return; }
    if (t.matches('g[data-step],g[data-phase],[data-pjc-phase]')) { openRow(t); return; }
    if (t.dataset.pjcRisk || t.hasAttribute('data-pjc-risk-new')) {
      if (box().dataset.canChange) openRisk(t.dataset.pjcRisk || '');
      return;
    }
    if (t.dataset.pjsHealth) {
      statusBox().querySelectorAll('[data-pjs-health]').forEach(function (b) {
        b.classList.toggle('on', b === t);
        b.setAttribute('aria-pressed', b === t ? 'true' : 'false');
      });
      return;
    }
    if (t.hasAttribute('data-pjs-save')) { saveStatus(t); return; }
    if (t.hasAttribute('data-pjs-ai')) { aiStatus(t); return; }
    if (t.dataset.pjcTab) {
      try { localStorage.setItem(TAB, t.dataset.pjcTab); } catch (x) { /* лише на цю сторінку */ }
      applyTab(t.dataset.pjcTab);
      if (HASH[location.hash] && HASH[location.hash] !== curTab) {   // #pj-risks в адресі, а вибрали іншу вкладку
        var want = '';
        Object.keys(HASH).forEach(function (h) { if (HASH[h] === curTab) want = h; });
        try { history.replaceState(null, '', location.pathname + location.search + want); } catch (x) { /* як є */ }
      }
      if (!t.classList.contains('pjc-tab')) {        // кнопка в шапці чи в картці — показати вкладку
        var sec = box().querySelector('.pjc-tl');
        if (sec) sec.scrollIntoView({behavior: 'smooth', block: 'start'});
      }
    } else if (t.hasAttribute('data-pjc-step-new')) {
      openStep('');
    } else if (t.hasAttribute('data-pjc-phase-new')) {
      openPhase('');
    } else if (t.hasAttribute('data-pjc-tpl-apply')) {
      applyTemplate(t);
    } else if (t.dataset.pjcPost) {
      if (t.dataset.confirm && !confirm(t.dataset.confirm)) return;
      t.disabled = true;
      send(t.dataset.pjcPost).then(function (j) { if (!done(j)) t.disabled = false; });
    } else if (t.hasAttribute('data-pjc-save-tpl')) {
      var name = prompt('Назва нового шаблону плану — він з’явиться у виборі шаблонів для інших проєктів:',
                        box().dataset.name || '');
      if (name === null || !name.trim()) return;
      send(box().dataset.planSaveTemplate, {name: name}).then(done);
    } else if (t.dataset.pjcRange) {
      try { localStorage.setItem(KEY, t.dataset.pjcRange); } catch (x) { /* лише на цю сторінку */ }
      load(t.dataset.pjcRange);
    } else if (t.dataset.pjcDecide) {
      t.disabled = true;
      send(t.dataset.pjcDecide, {accept: t.dataset.accept === '1'}).then(done);
    } else if (t.hasAttribute('data-pjc-scan')) {
      t.disabled = true;
      t.textContent = '🔎 Шукаю…';
      send(box().dataset.scan).then(done);
    } else if (t.hasAttribute('data-pjc-mtoggle')) {
      var f = root.querySelector('[data-pjc-mform]');
      f.hidden = !f.hidden;
      t.setAttribute('aria-expanded', String(!f.hidden));
      if (!f.hidden) f.querySelector('[data-f=title]').focus();
    } else if (t.hasAttribute('data-pjc-madd')) {
      addMoment();
    } else if (t.dataset.pjcMdel) {
      if (!confirm('Прибрати цей важливий момент?')) return;
      send(t.dataset.pjcMdel).then(done);
    }
  });
  // Enter у полях пульта не надсилає форму проєкту: «📌» — зберегти момент, шаблон — скласти план;
  // Enter / пробіл на рядку діаграми — вікно кроку чи фази
  root.addEventListener('keydown', function (e) {
    var mark = (e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('g[data-cal],g[data-files]');
    if (mark) {
      e.preventDefault();
      if (mark.matches('g[data-cal]')) calMenu(mark, mark); else filesMenu(mark, mark);   // меню біля позначки
      return;
    }
    var row = (e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('g[data-step],g[data-phase]');
    if (row) { e.preventDefault(); openRow(row); return; }
    var risk = (e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('tr[data-pjc-risk]');
    if (risk) { e.preventDefault(); openRisk(risk.dataset.pjcRisk); return; }
    if (e.key !== 'Enter' || e.isComposing || e.target.tagName === 'TEXTAREA' || !e.target.closest('input,select')) return;
    e.preventDefault();
    if (e.target.closest('[data-pjc-mform]')) addMoment();
    else if (e.target.closest('.pjc-empty')) applyTemplate();
    else if (e.target.closest('.pja-set')) aiSettings(e.target.closest('.pja-set').querySelector('[data-pja-settings]'));
  });
})();
