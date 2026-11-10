/* 🗓 Шкала проєкту в браузері — «🧭 Пульт проєкту» картки й «🧭 Робочий стіл».

   Дані — json_script #pjt-data (projects/timeline.py → payload: доріжки, події, межі «усе / 3 / 6 / 12 міс»).
   Масштаб — Ctrl + коліщатко (чи щипок на тачпаді), ＋ / －, подвійний клік; прокрутка — тягнути мишею,
   Shift + коліщатко, ← →; доріжки вмикаються й вимикаються («Коротко» — моменти, фази й віхи, терміни).
   Позначки, яким бракує місця, групуються в «+N» — клік наближує це місце. Клацання по точках листів, ромбах
   подій і файлах обробляє pj.js (ті самі data-mails / data-cal / data-files, що й у SVG з сервера).
   Вікно (що видно) запам'ятовується для проєкту — localStorage. */
(function () {
  'use strict';
  var DAY = 864e5, LEFT = 156, RIGHT = 14, HEAD = 30, ROW_H = 18, PAD = 6, MAX_ROWS = 3;
  var MAIL_H = 36, HOURS_H = 42, CHAR_W = 6.2, MIN_SPAN = 7, MAX_SPAN = 3650, CLUSTER_PX = 18;
  var MONTHS = ['січ', 'лют', 'бер', 'кві', 'тра', 'чер', 'лип', 'сер', 'вер', 'жов', 'лис', 'гру'];
  var BRIEF = ['moments', 'plan', 'calendar'];
  var SHORT = {moments: '📌 Моменти', plan: '📋 Фази', mail: '✉️ Листи', docs: '📝 Документи', files: '📎 Файли',
               orders: '🛒 Замовлення', supply: '🏭 Закупівлі', calendar: '📅 Терміни', hours: '⏱ Години'};
  var RANGES = [['all', 'усе'], ['3', '3 міс'], ['6', '6 міс'], ['12', '12 міс']];
  // цілі речення — щоб «🌐 Переклади інтерфейсу» знаходили їх (змінна частина — {n})
  var EMPTY_TEXT = 'На шкалі поки порожньо: додайте листування, Angebot, замовлення чи закупівлю до проєкту — вони з’являться тут самі.';
  var NONE_TEXT = 'У цьому періоді на вибраних доріжках подій немає — віддаліть масштаб чи ввімкніть доріжки.';
  var CLUSTER_TEXT = '{n} подій тут — клік, щоб наблизити:';

  function dnum(iso) { return iso ? Math.round(Date.parse(String(iso).slice(0, 10) + 'T00:00:00Z') / DAY) : null; }
  function diso(n) { return new Date(Math.round(n) * DAY).toISOString().slice(0, 10); }
  function dshort(n) { var s = diso(n); return s.slice(8, 10) + '.' + s.slice(5, 7); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function n1(v) { return String(Math.round(v * 10) / 10); }
  function get(key) { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; } }
  function put(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* лише ця сторінка */ } }
  function place(rows, lo, hi, limit) {
    for (var i = 0; i < rows.length; i++) {
      if (lo >= rows[i] + 4) { rows[i] = hi; return i; }
    }
    if (rows.length < limit) { rows.push(hi); return rows.length - 1; }
    return null;
  }

  function Timeline(holder, data, key) {
    this.holder = holder;
    this.data = data || {};
    this.key = key;
    this.today = dnum(this.data.today) || Math.floor(Date.now() / DAY);
    this.events = (this.data.events || []).map(function (e) {
      var o = {};
      Object.keys(e).forEach(function (k) { o[k] = e[k]; });
      o.s = dnum(e.start);
      o.e = e.end ? dnum(e.end) : null;
      return o;
    });
    var present = {};
    this.events.forEach(function (e) { present[e.lane] = true; });
    this.lanes = (this.data.lanes || []).filter(function (l) { return present[l[0]]; });
    var saved = get(this.key) || {};
    this.off = saved.off || {};
    this.range = 'range' in saved ? saved.range : String(get('pj-tl-range') || 'all');
    var b = this.bounds(this.range);
    this.view = saved.from && saved.to && saved.range === null
      ? {from: dnum(saved.from), to: dnum(saved.to) + 1} : {from: b[0], to: b[1]};
    if (!(this.view.to - this.view.from >= MIN_SPAN)) this.view = {from: b[0], to: b[1]};
    this.build();
  }

  Timeline.prototype.bounds = function (key) {
    var b = (this.data.bounds || {})[key] || (this.data.bounds || {}).all;
    if (!b) return [this.today - 60, this.today + 21];
    return [dnum(b[0]), dnum(b[1]) + 1];
  };

  Timeline.prototype.save = function () {
    put(this.key, {off: this.off, range: this.range,
                   from: this.range === null ? diso(this.view.from) : '',
                   to: this.range === null ? diso(this.view.to - 1) : ''});
  };

  Timeline.prototype.build = function () {
    var h = this.holder, self = this;
    h.textContent = '';
    if (!this.events.length) {
      h.innerHTML = '<div class="pjt-empty">' + esc(EMPTY_TEXT) + '</div>';
      return;
    }
    var bar = document.createElement('div');
    bar.className = 'pjt-tools';
    var html = '<span class="pjc-ranges" role="group" aria-label="Період шкали">';
    RANGES.forEach(function (r) { html += '<button type="button" class="pjc-b" data-tl-range="' + r[0] + '">' + r[1] + '</button>'; });
    html += '</span><span class="pjc-ranges" role="group" aria-label="Масштаб">' +
      '<button type="button" class="pjc-b" data-tl-zoom="out" title="Віддалити (Ctrl + коліщатко)">－</button>' +
      '<button type="button" class="pjc-b" data-tl-zoom="in" title="Наблизити (Ctrl + коліщатко, подвійний клік)">＋</button>' +
      '<button type="button" class="pjc-b" data-tl-zoom="today" title="Сьогодні — посередині">⌖ сьогодні</button></span>' +
      '<span class="pjc-ranges pjt-lanes" role="group" aria-label="Доріжки шкали">' +
      '<button type="button" class="pjc-b" data-tl-lanes="brief" title="Лише важливі моменти, фази й віхи, терміни й зустрічі">Коротко</button>' +
      '<button type="button" class="pjc-b" data-tl-lanes="all" title="Усі доріжки">Усе</button>';
    this.lanes.forEach(function (l) {
      html += '<button type="button" class="pjc-b pjt-chip" data-tl-lane="' + l[0] + '" title="' + esc(l[1]) + '">' +
        esc(SHORT[l[0]] || l[1]) + '</button>';
    });
    html += '</span>';
    bar.innerHTML = html;
    var wrap = document.createElement('div');
    wrap.className = 'pjt-wrap';
    wrap.tabIndex = 0;
    wrap.setAttribute('aria-label', 'Шкала проєкту: Ctrl + коліщатко — масштаб, тягніть — прокрутка, ← → — зсунути');
    var hint = document.createElement('div');
    hint.className = 'pjc-hint pjt-hint';
    hint.textContent = 'Ctrl + коліщатко — масштаб · тягніть мишею — прокрутка · двічі клік — ближче · «+N» — кілька подій, клік — наблизити';
    h.appendChild(bar);
    h.appendChild(wrap);
    h.appendChild(hint);
    this.wrap = wrap;
    this.bar = bar;
    bar.addEventListener('click', function (e) {
      var t = e.target.closest('button');
      if (!t) return;
      e.preventDefault();
      if (t.dataset.tlRange) self.setRange(t.dataset.tlRange);
      else if (t.dataset.tlZoom === 'in') self.zoomAt(0.5, null);
      else if (t.dataset.tlZoom === 'out') self.zoomAt(2, null);
      else if (t.dataset.tlZoom === 'today') self.centerOn(self.today);
      else if (t.dataset.tlLanes) self.setLanes(t.dataset.tlLanes);
      else if (t.dataset.tlLane) {
        if (self.off[t.dataset.tlLane]) delete self.off[t.dataset.tlLane];
        else self.off[t.dataset.tlLane] = 1;
        self.save();
        self.render();
      }
    });
    this.bind();
    this.render();
  };

  Timeline.prototype.setRange = function (key) {
    this.range = key;
    put('pj-tl-range', key);
    var b = this.bounds(key);
    this.view = {from: b[0], to: b[1]};
    this.save();
    this.render();
  };

  Timeline.prototype.setLanes = function (mode) {
    var off = {};
    if (mode === 'brief') this.lanes.forEach(function (l) { if (BRIEF.indexOf(l[0]) < 0) off[l[0]] = 1; });
    this.off = off;
    this.save();
    this.render();
  };

  Timeline.prototype.width = function () { return Math.max(this.wrap.clientWidth, 560); };

  Timeline.prototype.setView = function (from, to) {
    var span = Math.min(Math.max(to - from, MIN_SPAN), MAX_SPAN);
    var mid = (from + to) / 2;
    if (to - from !== span) { from = mid - span / 2; to = mid + span / 2; }
    this.view = {from: from, to: to};
    this.range = null;
    this.schedule();
  };

  Timeline.prototype.zoomAt = function (factor, px) {
    var W = this.width(), v = this.view, span = v.to - v.from;
    var rel = px === null ? 0.5 : Math.min(Math.max((px - LEFT) / (W - LEFT - RIGHT), 0), 1);
    var at = v.from + rel * span, ns = Math.min(Math.max(span * factor, MIN_SPAN), MAX_SPAN);
    this.setView(at - rel * ns, at - rel * ns + ns);
  };

  Timeline.prototype.centerOn = function (d) {
    var span = this.view.to - this.view.from;
    this.setView(d - span / 2, d + span / 2);
  };

  Timeline.prototype.schedule = function () {
    var self = this;
    if (this.raf) return;
    this.raf = requestAnimationFrame(function () {
      self.raf = 0;
      self.render();
      clearTimeout(self.saveT);
      self.saveT = setTimeout(function () { self.save(); }, 400);
    });
  };

  Timeline.prototype.bind = function () {
    var self = this, wrap = this.wrap, pan = null;
    function px(e) { return e.clientX - wrap.getBoundingClientRect().left; }
    wrap.addEventListener('wheel', function (e) {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        self.zoomAt(Math.exp(Math.max(Math.min(e.deltaY, 200), -200) * 0.0022), px(e));
      } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        var d = (e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX) / self.ppd;
        self.setView(self.view.from + d, self.view.to + d);
      }
    }, {passive: false});
    wrap.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || e.target.closest('.pjt-ev, a')) return;
      pan = {x: e.clientX, from: self.view.from, to: self.view.to, moved: false};
      wrap.setPointerCapture(e.pointerId);
      wrap.classList.add('pjt-grab');
    });
    wrap.addEventListener('pointermove', function (e) {
      if (!pan) return;
      var dx = e.clientX - pan.x;
      if (Math.abs(dx) > 2) pan.moved = true;
      var d = -dx / self.ppd;
      self.setView(pan.from + d, pan.to + d);
    });
    function stop() {
      if (!pan) return;
      pan = null;
      wrap.classList.remove('pjt-grab');
    }
    wrap.addEventListener('pointerup', stop);
    wrap.addEventListener('pointercancel', stop);
    wrap.addEventListener('dblclick', function (e) {
      if (e.target.closest('.pjt-ev, a')) return;
      e.preventDefault();
      self.zoomAt(e.shiftKey ? 2 : 0.5, px(e));
    });
    wrap.addEventListener('click', function (e) {
      var c = e.target.closest('[data-tl-zoomto]');
      if (!c) return;
      e.preventDefault();
      e.stopPropagation();
      var p = c.getAttribute('data-tl-zoomto').split(',');
      self.setView(+p[0], +p[1]);
    });
    wrap.addEventListener('keydown', function (e) {
      if (e.target !== wrap) return;
      var span = self.view.to - self.view.from, step = span * 0.15;
      if (e.key === 'ArrowLeft') self.setView(self.view.from - step, self.view.to - step);
      else if (e.key === 'ArrowRight') self.setView(self.view.from + step, self.view.to + step);
      else if (e.key === '+' || e.key === '=') self.zoomAt(0.6, null);
      else if (e.key === '-' || e.key === '_') self.zoomAt(1 / 0.6, null);
      else if (e.key === '0') self.setRange('all');
      else return;
      e.preventDefault();
    });
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(function () { self.schedule(); });
      this.ro.observe(wrap);
    } else {
      window.addEventListener('resize', function () { self.schedule(); });
    }
  };

  Timeline.prototype.render = function () {
    if (!this.wrap || !this.wrap.offsetParent) return;          // вкладку сховано — намалює, коли покажуть
    var self = this, W = this.width(), v = this.view, span = v.to - v.from;
    var ppd = (W - LEFT - RIGHT) / span;
    this.ppd = ppd;
    function x(d) { return LEFT + (d - v.from) * ppd; }
    function xc(d) { return x(d) + ppd / 2; }
    function clip(a) { return Math.min(Math.max(a, LEFT), W - RIGHT); }
    var lo = v.from - 1, hi = v.to + 1, body = [], y = HEAD, idx = 0, shown = 0;
    this.lanes.forEach(function (l) {
      if (self.off[l[0]]) return;
      var items = self.events.filter(function (e) {
        return e.lane === l[0] && (e.e !== null ? e.e : e.s) >= lo && e.s <= hi;
      }).sort(function (a, b) { return a.s - b.s || ((a.e || a.s) - (b.e || b.s)); });
      if (!items.length) return;
      shown += items.length;
      var r = l[0] === 'mail' ? self.drawMail(items, y, xc)
        : (l[0] === 'hours' ? self.drawHours(items, y, x, W) : self.drawLane(items, y, x, xc, clip, W));
      if (idx % 2) body.push('<rect x="0" y="' + n1(y) + '" width="' + W + '" height="' + n1(r[0]) + '" class="pjt-alt"/>');
      body.push('<text x="8" y="' + n1(y + Math.min(r[0], 30) / 2 + 4) + '" class="pjt-lab">' + esc(l[1]) + '</text>');
      body.push(r[1]);
      y += r[0];
      idx++;
    });
    this.bar.querySelectorAll('[data-tl-range]').forEach(function (b) {
      var on = b.dataset.tlRange === self.range;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    this.bar.querySelectorAll('[data-tl-lane]').forEach(function (b) {
      var on = !self.off[b.dataset.tlLane];
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    var total = Math.max(y, HEAD + 40);
    var out = ['<svg class="pjt pjt-live" width="' + W + '" height="' + (total + 32) + '" viewBox="0 0 ' + W + ' ' +
               (total + 32) + '" role="img" aria-label="' + esc('Шкала проєкту ' + dshort(v.from) + ' – ' +
               dshort(v.to - 1) + ': подій ' + shown) + '">'];
    out.push(this.grid(x, ppd, total, W));
    if (!body.length) {
      out.push('<text x="' + (LEFT + 8) + '" y="' + (HEAD + 24) + '" class="pjt-lab">' + esc(NONE_TEXT) + '</text>');
    }
    out.push(body.join(''));
    out.push(this.marks(xc, total, W));
    out.push('</svg>');
    this.wrap.innerHTML = out.join('');
  };

  Timeline.prototype.grid = function (x, ppd, total, W) {
    var v = this.view, out = [];
    out.push('<line x1="' + (LEFT - 6) + '" y1="' + (HEAD - 4) + '" x2="' + (LEFT - 6) + '" y2="' + n1(total) + '" class="pjt-grid"/>');
    var first = Math.floor(v.from), last = Math.ceil(v.to);
    if (ppd >= 16) {                                    // дні
      for (var d = first; d <= last; d++) {
        var xd = x(d);
        if (xd < LEFT || xd > W - RIGHT) continue;
        var dow = new Date(d * DAY).getUTCDay();
        if (dow === 0 || dow === 6) {
          out.push('<rect x="' + n1(xd) + '" y="' + HEAD + '" width="' + n1(ppd) + '" height="' + n1(total - HEAD) + '" class="pjt-we"/>');
        }
        out.push('<text x="' + n1(xd + ppd / 2) + '" y="' + (HEAD - 7) + '" text-anchor="middle" class="pjt-wkt">' +
                 new Date(d * DAY).getUTCDate() + '</text>');
      }
    } else if (ppd * 7 >= 10) {                         // тижні — понеділки
      var m = first + ((8 - new Date(first * DAY).getUTCDay()) % 7);
      for (var w = m; w <= last; w += 7) {
        var xw = x(w);
        if (xw < LEFT || xw > W - RIGHT) continue;
        out.push('<line x1="' + n1(xw) + '" y1="' + (HEAD - 4) + '" x2="' + n1(xw) + '" y2="' + n1(total) + '" class="pjt-wk"/>');
        if (ppd * 7 >= 34) out.push('<text x="' + n1(xw + 2) + '" y="' + (HEAD - 7) + '" class="pjt-wkt">' + dshort(w) + '</text>');
      }
    }
    var dt = new Date(first * DAY), mo = Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), 1) / DAY;
    var monthPx = 30.4 * ppd;
    while (mo <= last) {
      var md = new Date(mo * DAY), next = Date.UTC(md.getUTCFullYear(), md.getUTCMonth() + 1, 1) / DAY;
      var xm = x(mo);
      if (xm >= LEFT && xm <= W - RIGHT) {
        out.push('<line x1="' + n1(xm) + '" y1="2" x2="' + n1(xm) + '" y2="' + n1(total) + '" class="pjt-mon"/>');
      }
      var lx = Math.max(xm, LEFT) + 4;
      if (monthPx >= 30 && x(next) - lx > 26) {
        var name = MONTHS[md.getUTCMonth()] + (md.getUTCMonth() === 0 || mo <= v.from ? ' ' + md.getUTCFullYear() : '');
        out.push('<text x="' + n1(lx) + '" y="12" class="pjt-mont">' + name + '</text>');
      } else if (monthPx < 30 && md.getUTCMonth() === 0 && xm >= LEFT) {
        out.push('<text x="' + n1(xm + 4) + '" y="12" class="pjt-mont">' + md.getUTCFullYear() + '</text>');
      }
      mo = next;
    }
    return out.join('');
  };

  Timeline.prototype.marks = function (xc, total, W) {
    var d = this.data, v = this.view, list = [], out = [], foot = [], used = [];
    if (d.start) list.push([dnum(d.start), 'pjt-start', 'старт ' + dshort(dnum(d.start))]);
    if (d.due) list.push([dnum(d.due), 'pjt-due', 'строк клієнту ' + dshort(dnum(d.due))]);
    list.push([this.today, 'pjt-today', 'сьогодні']);
    list.forEach(function (m) {
      if (m[0] < v.from - 0.5 || m[0] > v.to + 0.5) return;
      var xx = xc(m[0]);
      out.push('<line x1="' + n1(xx) + '" y1="' + (HEAD - 6) + '" x2="' + n1(xx) + '" y2="' + n1(total) + '" class="' + m[1] + '"/>');
      foot.push([xx, m[1], m[2]]);
    });
    foot.sort(function (a, b) { return a[0] - b[0]; }).forEach(function (f) {
      var w = f[2].length * CHAR_W, l = Math.min(Math.max(f[0] - w / 2, LEFT), W - RIGHT - w);
      var row = !used.length || l > used[used.length - 1] + 6 ? 0 : 1;
      if (!row) used.push(l + w);
      out.push('<text x="' + n1(l) + '" y="' + n1(total + 14 + row * 13) + '" class="pjt-ft ' + f[1] + '-t">' + esc(f[2]) + '</text>');
    });
    return out.join('');
  };

  function attrs(e) {
    var a = '';
    if (e.hid) a += ' data-hid="' + esc(e.hid) + '"';
    if (e.cal) a += ' data-cal="' + esc(JSON.stringify(e.cal)) + '" tabindex="0" role="button"';
    if (e.files) a += ' data-files="' + esc(JSON.stringify(e.files)) + '" tabindex="0" role="button"';
    if (e.mails && e.mails.length) a += ' data-mails="' + esc(JSON.stringify(e.mails)) + '"';
    return a;
  }

  function wrapEv(e, inner, extraCls) {
    var g = '<g class="pjt-ev pjt-' + (e.cls || 'def') + (extraCls || '') + '"' + attrs(e) + '><title>' +
      esc(e.tip || e.label) + '</title>' + inner + '</g>';
    return e.url ? '<a href="' + esc(e.url) + '" target="_blank" rel="noopener">' + g + '</a>' : g;
  }

  Timeline.prototype.drawLane = function (items, top, x, xc, clip, W) {
    var rows = [], placed = [], clusters = [];
    items.forEach(function (e) {
      var point = e.shape !== 'bar';
      var x1 = point ? clip(xc(e.s)) : clip(x(e.s));
      var x2 = point ? x1 : Math.max(clip(x((e.e !== null ? e.e : e.s) + 1)), x1 + 3);
      var label = e.label || '', lw = label.length * CHAR_W, anchor = 'start', lx = x2 + (point ? 9 : 5);
      var lo = x1 - 7, hi = lx + lw;
      if (label && hi > W - RIGHT) {
        anchor = 'end';
        lx = x1 - (point ? 9 : 5);
        lo = lx - lw;
        hi = x2 + 7;
        if (lo < LEFT) { label = ''; lo = x1 - 7; hi = x2 + 7; }
      }
      if (!label) { lo = x1 - 7; hi = x2 + 7; }
      var row = place(rows, lo, hi, MAX_ROWS);
      if (row === null && label) {
        label = '';
        lo = x1 - 7;
        hi = x2 + 7;
        row = place(rows, lo, hi, MAX_ROWS + 1);
      }
      if (row === null) {                                // місця немає — у групу «+N»
        var c = null;
        clusters.forEach(function (k) { if (Math.abs(k.x - x1) <= CLUSTER_PX) c = k; });
        if (!c) { c = {x: x1, items: []}; clusters.push(c); }
        c.items.push(e);
        return;
      }
      placed.push([e, row, x1, x2, label, anchor, lx]);
    });
    var nrows = Math.max(rows.length, 1) + (clusters.length ? 1 : 0);
    var height = PAD * 2 + nrows * ROW_H, parts = [];
    placed.forEach(function (p) {
      var e = p[0], cy = top + PAD + p[1] * ROW_H + ROW_H / 2, x1 = p[2], x2 = p[3], inner;
      if (e.shape === 'bar') {
        inner = '<rect x="' + n1(x1) + '" y="' + n1(cy - 5) + '" width="' + n1(x2 - x1) + '" height="10" rx="3" class="pjt-bar"/>';
      } else if (e.shape === 'diamond') {
        inner = '<polygon points="' + n1(x1) + ',' + n1(cy - 6) + ' ' + n1(x1 + 6) + ',' + n1(cy) + ' ' + n1(x1) + ',' +
          n1(cy + 6) + ' ' + n1(x1 - 6) + ',' + n1(cy) + '" class="pjt-dia"/>';
      } else if (e.shape === 'file') {
        inner = '<path d="M' + n1(x1 - 4.5) + ',' + n1(cy - 6) + ' h6 l3,3 v9 h-9 z" class="pjt-file"/>' +
          '<path d="M' + n1(x1 + 1.5) + ',' + n1(cy - 6) + ' v3 h3" class="pjt-fold"/>';
      } else if (e.shape === 'flag') {
        inner = '<line x1="' + n1(x1) + '" y1="' + n1(cy - 7) + '" x2="' + n1(x1) + '" y2="' + n1(cy + 7) + '" class="pjt-pole"/>' +
          '<polygon points="' + n1(x1) + ',' + n1(cy - 7) + ' ' + n1(x1 + 8) + ',' + n1(cy - 4) + ' ' + n1(x1) + ',' +
          n1(cy - 1) + '" class="pjt-flag"/>';
      } else {
        inner = '<circle cx="' + n1(x1) + '" cy="' + n1(cy) + '" r="4.5" class="pjt-pt"/>';
      }
      if (p[4]) {
        inner += '<text x="' + n1(p[6]) + '" y="' + n1(cy + 4) + '" class="pjt-t"' +
          (p[5] === 'end' ? ' text-anchor="end"' : '') + ' translate="no">' + esc(p[4]) + '</text>';
      }
      parts.push(wrapEv(e, inner));
    });
    var cy = top + PAD + (nrows - 1) * ROW_H + ROW_H / 2;
    clusters.forEach(function (c) {
      var s = Infinity, en = -Infinity;
      c.items.forEach(function (e) { s = Math.min(s, e.s); en = Math.max(en, e.e !== null ? e.e : e.s); });
      var tip = CLUSTER_TEXT.replace('{n}', c.items.length) + '\n' + c.items.slice(0, 8).map(function (e) {
        return dshort(e.s) + ' ' + (e.label || (e.tip || '').split('\n')[0]);
      }).join('\n') + (c.items.length > 8 ? '\n…' : '');
      var txt = '+' + c.items.length;
      var r = Math.max(8, txt.length * 3.4 + 3);
      parts.push('<g class="pjt-ev pjt-cl" data-tl-zoomto="' + (s - 2) + ',' + (en + 3) + '" tabindex="0" role="button">' +
        '<title>' + esc(tip) + '</title><rect x="' + n1(c.x - r) + '" y="' + n1(cy - 7.5) + '" width="' + n1(2 * r) +
        '" height="15" rx="7.5" class="pjt-clb"/><text x="' + n1(c.x) + '" y="' + n1(cy + 3.5) +
        '" text-anchor="middle" class="pjt-clt">' + txt + '</text></g>');
    });
    return [height, parts.join('')];
  };

  Timeline.prototype.drawMail = function (items, top, xc) {
    var buckets = {}, order = [];
    items.forEach(function (e) {                       // точки, що злилися б, — одна (сума листів)
      var k = e.cls + ':' + Math.round(xc(e.s) / 5);
      if (!buckets[k]) { buckets[k] = {e: e, n: 0, ring: false, mails: [], s: e.s, last: e.s, tips: []}; order.push(k); }
      var b = buckets[k];
      b.n += e.count || 1;
      b.ring = b.ring || !!e.ring;
      b.mails = b.mails.concat(e.mails || []).slice(0, 12);
      b.last = Math.max(b.last, e.s);
      b.tips.push(e.tip);
    });
    var parts = order.map(function (k) {
      var b = buckets[k], e = b.e, cy = top + (e.cls === 'in' ? 12 : 25), cx = xc(b.s);
      var r = Math.min(3 + (b.n - 1) * 0.8, 6.5);
      var inner = '<circle cx="' + n1(cx) + '" cy="' + n1(cy) + '" r="' + n1(r) + '" class="pjt-dot"/>';
      if (b.ring) inner += '<circle cx="' + n1(cx) + '" cy="' + n1(cy) + '" r="' + n1(r + 3) + '" class="pjt-ring"/>';
      var merged = b.tips.length > 1;
      var one = {cls: e.cls, mails: b.mails, url: merged ? '' : e.url,
                 tip: merged ? (e.cls === 'in' ? '📥 від клієнта' : '📤 наші') + ' · ' + dshort(b.s) + '–' + dshort(b.last) +
                   ' · листів ' + b.n + '\n' + b.tips.slice(0, 4).map(function (t) { return String(t).split('\n')[0]; }).join('\n')
                   : e.tip};
      return wrapEv(one, inner);
    });
    return [MAIL_H, parts.join('')];
  };

  Timeline.prototype.drawHours = function (items, top, x, W) {
    var max = 1, total = 0;
    items.forEach(function (e) { max = Math.max(max, e.minutes || 0); });
    var base = top + HOURS_H - 6, parts = [];
    items.forEach(function (e) {
      total += e.minutes || 0;
      var x1 = x(e.s) + 1.5, x2 = x(e.e + 1) - 1.5, h = Math.max((e.minutes || 0) / max * 24, 1.5);
      if (x2 < LEFT || x1 > W - RIGHT) return;
      x1 = Math.max(x1, LEFT);
      var inner = '<rect x="' + n1(x1) + '" y="' + n1(base - h) + '" width="' + n1(Math.max(x2 - x1, 1.5)) + '" height="' +
        n1(h) + '" rx="2" class="pjt-hrs"/>';
      if (x2 - x1 >= 18) {
        inner += '<text x="' + n1((x1 + x2) / 2) + '" y="' + n1(base - h - 3) + '" text-anchor="middle" class="pjt-ht">' +
          n1((e.minutes || 0) / 60).replace('.', ',') + '</text>';
      }
      parts.push('<g class="pjt-ev"><title>' + esc(e.tip) + '</title>' + inner + '</g>');
    });
    parts.push('<text x="' + (W - RIGHT) + '" y="' + n1(top + 12) + '" text-anchor="end" class="pjt-ht">Σ ' +
               n1(total / 60).replace('.', ',') + ' год</text>');
    return [HOURS_H, parts.join('')];
  };

  window.PJTimeline = {
    mount: function (holder, data, key) {
      if (!holder) return null;
      if (holder._pjt && holder._pjt.ro) holder._pjt.ro.disconnect();
      var t = new Timeline(holder, data, key || 'pj-tl-view');
      holder._pjt = t;
      return t;
    }
  };
})();
