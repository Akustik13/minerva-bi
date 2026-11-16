/* ✏️ Редактор плану (projects/plan_editor.py): усі фази й кроки однією таблицею — назви, віхи, хто робить, дати й
   тривалості, «Після №», стан, порядок; нові й прибрані фази й кроки. Правки живуть у пам'яті вікна, «💾 Зберегти
   план» шле всю таблицю одним запитом. 🧠 «Що змінити в плані» — AI пропонує зміни, вони одразу в таблиці й
   підсвічені з поясненням; ↺ повертає рядок як було. Відкриває pj.js:
   PJEditor.open({data, urls: {save, revise}, ai, send, bgRun, put}). */
(function () {
  'use strict';
  var SIDES = [['we', 'ми'], ['customer', 'клієнт'], ['supplier', 'постачальник'], ['colleague', 'колега']];
  var STATUSES = [['todo', 'заплановано'], ['doing', 'в роботі'], ['waiting', 'чекаємо'], ['done', 'виконано'],
                  ['skipped', 'пропущено']];
  var PH = ['name', 'icon', 'gate'], ST = ['title', 'kind', 'side', 'start', 'end', 'duration', 'status', 'phase', 'after'];
  var dlg = null, S = null, O = null, seq = 0, parties = {}, sides = SIDES, statuses = STATUSES;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function addDays(d, n) {
    var t = Date.parse(d + 'T00:00:00Z');
    return isNaN(t) ? '' : new Date(t + n * 864e5).toISOString().slice(0, 10);
  }
  function diff(a, b) { return Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 864e5); }
  function dm(d) { return d ? d.slice(8, 10) + '.' + d.slice(5, 7) + '.' + d.slice(0, 4) : ''; }
  function label(list, key) {
    for (var i = 0; i < list.length; i++) if (list[i][0] === key) return list[i][1];
    return key || '';
  }

  // ── Стан таблиці ──
  function snap(r, keys) {
    var o = {};
    keys.forEach(function (k) {
      o[k] = k === 'after' ? r.after.map(function (a) { return {key: a.key, lag: a.lag}; }) : r[k];
    });
    return o;
  }
  function sig(list) { return (list || []).map(function (a) { return a.key + '+' + (a.lag || 0); }).sort().join(','); }
  function same(a, b, k) {
    if (k === 'after') return sig(a) === sig(b);
    return String(a == null ? '' : a) === String(b == null ? '' : b);
  }
  function init(data) {
    seq = 0;
    parties = {};
    (data.parties || []).forEach(function (p) { parties[p.id] = p.label; });
    sides = (data.sides && data.sides.length) ? data.sides : SIDES;
    statuses = (data.statuses && data.statuses.length) ? data.statuses : STATUSES;
    S = {phases: [], steps: [], info: null};
    var pidx = {};
    (data.phases || []).slice().sort(function (a, b) { return (a.order - b.order) || (a.id - b.id); })
      .forEach(function (p, i) {
        var r = {key: 'p' + p.id, id: p.id, name: p.name || '', icon: p.icon || '', gate: p.gate || ''};
        r.o = snap(r, PH);
        pidx[r.key] = i;
        S.phases.push(r);
      });
    var local = {};
    (data.steps || []).forEach(function (s) { local[s.id] = 1; });
    function pos(s) {
      var k = s.phase === '' || s.phase == null ? '' : 'p' + s.phase;
      return pidx[k] === undefined ? 1e6 : pidx[k];
    }
    (data.steps || []).slice().sort(function (a, b) {
      return (pos(a) - pos(b)) || ((a.order || 0) - (b.order || 0)) || (a.id - b.id);
    }).forEach(function (s) {
      var deps = s.deps || (s.after || []).map(function (p) { return [p, s.lag || 0]; }), after = [], ext = 0;
      deps.forEach(function (d) {
        if (local[d[0]]) after.push({key: 's' + d[0], lag: d[1] || 0});
        else ext++;
      });
      var ms = s.kind === 'milestone';
      var r = {key: 's' + s.id, id: s.id, phase: pidx['p' + s.phase] === undefined ? '' : 'p' + s.phase,
               title: s.title || '', kind: ms ? 'milestone' : 'task', side: s.side || 'we', party: s.party || '',
               start: ms ? '' : (s.start || ''), end: ms ? (s.end || s.start || '') : (s.end || ''),
               duration: ms ? 0 : (s.duration || 1), status: s.status || 'todo', eff: s.eff || '', note: s.note || '',
               f_start: s.f_start || '', f_end: s.f_end || '', after: after, ext: ext};
      r.o = snap(r, ST);
      S.steps.push(r);
    });
    S.order0 = S.steps.map(function (s) { return s.key; }).join(',');
    S.phases0 = S.phases.map(function (p) { return p.key; }).join(',');
  }
  function findStep(key) {
    for (var i = 0; i < S.steps.length; i++) if (S.steps[i].key === key) return S.steps[i];
    return null;
  }
  function findPhase(key) {
    for (var i = 0; i < S.phases.length; i++) if (S.phases[i].key === key) return S.phases[i];
    return null;
  }
  function livePhase(key) { var p = key && findPhase(key); return !!(p && !p.del); }
  function seqRows() {
    var out = [];
    S.phases.forEach(function (p) {
      out.push({p: p});
      S.steps.forEach(function (s) { if (s.phase === p.key) out.push({s: s}); });
    });
    var loose = S.steps.filter(function (s) { return !livePhase(s.phase) && !findPhase(s.phase); });
    if (loose.length || !S.phases.length) {
      out.push({loose: true});
      loose.forEach(function (s) { out.push({s: s}); });
    }
    return out;
  }
  function numbers() {
    var n = {}, i = 0;
    seqRows().forEach(function (r) { if (r.s && !r.s.del) n[r.s.key] = ++i; });
    return n;
  }
  function changes() {
    var n = 0;
    S.phases.forEach(function (p) {
      if (p.del ? p.id : (!p.id || PH.some(function (k) { return !same(p[k], p.o[k], k); }))) n++;
    });
    S.steps.forEach(function (s) {
      if (s.del ? s.id : (!s.id || ST.some(function (k) { return !same(s[k], s.o[k], k); }))) n++;
    });
    var order = S.steps.filter(function (s) { return s.id; }).map(function (s) { return s.key; }).join(',');
    var porder = S.phases.filter(function (p) { return p.id; }).map(function (p) { return p.key; }).join(',');
    if (!n && (order !== S.order0 || porder !== S.phases0)) n = 1;
    return n;
  }

  // ── Таблиця ──
  function input(type, value, attrs) {
    var i = document.createElement('input');
    i.type = type;
    if (type === 'checkbox') i.checked = !!value;
    else i.value = value == null ? '' : value;
    Object.keys(attrs || {}).forEach(function (k) { i.setAttribute(k, attrs[k]); });
    return i;
  }
  function select(list, value, f) {
    var s = el('select');
    list.forEach(function (o) {
      var opt = el('option', '', o[1]);
      opt.value = o[0];
      s.appendChild(opt);
    });
    s.value = value;
    s.setAttribute('data-f', f);
    return s;
  }
  function td(f, node, cls) {
    var c = el('td', 'pje-c' + (cls ? ' ' + cls : ''));
    if (f) c.setAttribute('data-cf', f);
    if (node) c.appendChild(node);
    return c;
  }
  function btn(act, text, title) {
    var b = el('button', '', text);
    b.type = 'button';
    b.setAttribute('data-pje-act', act);
    if (title) { b.title = title; b.setAttribute('aria-label', title); }
    return b;
  }
  function afterText(s, nums) {
    return s.after.filter(function (a) { return nums[a.key]; }).map(function (a) {
      return nums[a.key] + (a.lag ? (a.lag > 0 ? '+' : '') + a.lag : '');
    }).join(', ');
  }
  function stepRow(s, nums) {
    var tr = el('tr', 'pje-st');
    tr.setAttribute('data-key', s.key);
    if (s.del) tr.classList.add('pje-del');
    else if (s.ai && s.ai.add) tr.classList.add('pje-ai-new');
    else if (!s.id) tr.classList.add('pje-new');
    var ms = s.kind === 'milestone', dis = !!s.del;
    tr.appendChild(td('', null, 'pje-n')).textContent = s.del ? '—' : String(nums[s.key] || '');
    var t = el('td', 'pje-c pje-tt');
    t.setAttribute('data-cf', 'title');
    var ti = input('text', s.title, {maxlength: '200', 'data-f': 'title', 'aria-label': 'Назва кроку', translate: 'no'});
    ti.disabled = dis;
    t.appendChild(ti);
    var why = s.ai && s.ai.why ? s.ai.why : '';
    if (why || (s.ai && s.ai.remove)) {
      var w = el('div', 'pje-why', '🧠 ' + (s.ai.remove ? 'прибрати: ' : s.ai.add ? 'новий: ' : '') + why);
      w.setAttribute('translate', 'no');
      t.appendChild(w);
    }
    if (s.note && s.eff && s.eff !== s.status) {
      t.appendChild(el('div', 'pje-hint', '🔗 ' + s.note));
    }
    tr.appendChild(t);
    var cb = input('checkbox', ms, {'data-f': 'kind', 'aria-label': 'Віха', title: 'Віха — ключовий момент без тривалості'});
    cb.disabled = dis;
    tr.appendChild(td('kind', cb, 'pje-ms'));
    var side = select(sides, s.side, 'side');
    side.disabled = dis || !!s.party;
    if (s.party) side.title = 'За стороною «' + (parties[s.party] || '') + '» — змінюється у вікні кроку';
    tr.appendChild(td('side', side));
    var st = input('date', s.start, {'data-f': 'start', 'aria-label': 'Початок'});
    st.disabled = dis || ms;
    if (s.f_start && s.f_start !== s.start) st.title = 'прогноз: ' + dm(s.f_start);
    tr.appendChild(td('start', st));
    var en = input('date', s.end, {'data-f': 'end', 'aria-label': ms ? 'Дата віхи' : 'Кінець'});
    en.disabled = dis;
    if (s.f_end && s.f_end !== s.end) en.title = 'прогноз: ' + dm(s.f_end);
    tr.appendChild(td('end', en));
    var du = input('number', ms ? 0 : s.duration, {min: '1', max: '999', 'data-f': 'duration', 'aria-label': 'Днів'});
    du.disabled = dis || ms;
    tr.appendChild(td('duration', du, 'pje-du'));
    var af = input('text', afterText(s, nums), {'data-f': 'after', 'aria-label': 'Після рядків №',
      title: 'Номери рядків, після яких іде крок: «3, 5» або «5+2» — через 2 дні після 5-го', inputmode: 'numeric'});
    af.disabled = dis;
    var ac = td('after', af, 'pje-af');
    if (s.ext) {
      var x = el('span', 'pje-ext', '⛓' + s.ext);
      x.title = 'Ще після кроків інших проєктів: ' + s.ext + ' — змінюється у вікні кроку';
      ac.appendChild(x);
    }
    tr.appendChild(ac);
    var ss = select(statuses, s.status, 'status');
    ss.disabled = dis;
    tr.appendChild(td('status', ss));
    var acts = el('td', 'pje-acts');
    if (s.del) {
      acts.appendChild(btn('undo', '↺', s.ai && s.ai.remove ? 'Лишити крок (AI пропонував прибрати)' : 'Повернути крок'));
    } else {
      acts.appendChild(btn('up', '↑', 'Вище (Alt + ↑)'));
      acts.appendChild(btn('down', '↓', 'Нижче (Alt + ↓)'));
      if (s.ai || (s.id && changedStep(s))) {
        acts.appendChild(btn('undo', '↺', s.ai ? (s.ai.add ? 'Не додавати (пропозиція AI)' : 'Повернути як було до AI')
          : 'Повернути як було'));
      }
      acts.appendChild(btn('del', '🗑', 'Прибрати крок'));
    }
    tr.appendChild(acts);
    marks(tr, s);
    return tr;
  }
  function changedStep(s) { return ST.some(function (k) { return !same(s[k], s.o[k], k); }); }
  function changedPhase(p) { return PH.some(function (k) { return !same(p[k], p.o[k], k); }); }
  function fmt(k, v, nums) {
    if (k === 'after') return (v || []).map(function (a) { return nums[a.key] || '?'; }).join(', ') || '—';
    if (k === 'kind') return v === 'milestone' ? 'віха' : 'крок';
    if (k === 'side') return label(sides, v);
    if (k === 'status') return label(statuses, v);
    if (k === 'start' || k === 'end') return dm(v) || '—';
    if (k === 'phase') { var p = findPhase(v); return p ? p.name : 'без фази'; }
    return v === '' || v == null ? '—' : String(v);
  }
  function marks(tr, r) {
    var nums = null, isPh = !!r.name || r.name === '';
    tr.querySelectorAll('[data-cf]').forEach(function (c) {
      var k = c.getAttribute('data-cf'), chg = !!(r.id && r.o && !same(r[k], r.o[k], k));
      var ai = !!(r.ai && r.ai.fields && r.ai.fields.indexOf(k) >= 0);
      c.classList.toggle('pje-chg', chg && !ai);
      c.classList.toggle('pje-aic', ai);
      if (ai || chg) {
        nums = nums || numbers();
        var was = ai ? r.ai.before[k] : r.o[k];
        c.title = (ai ? '🧠 ' + (r.ai.why ? r.ai.why + ' · ' : '') : '✏️ ') + 'було: ' + fmt(k, was, nums);
      } else if (!isPh) {
        c.removeAttribute('title');
      }
    });
  }
  function phaseRow(p) {
    var tr = el('tr', 'pje-ph' + (p.del ? ' pje-del' : '') + (!p.id ? (p.ai && p.ai.add ? ' pje-ai-new' : ' pje-new') : ''));
    tr.setAttribute('data-key', p.key);
    var c = el('td');
    c.colSpan = 10;
    var line = el('div', 'pje-phl');
    var ic = input('text', p.icon, {maxlength: '8', 'data-f': 'icon', 'aria-label': 'Значок фази', class: 'pje-pic'});
    var nm = input('text', p.name, {maxlength: '120', 'data-f': 'name', 'aria-label': 'Назва фази', class: 'pje-pname',
                                    translate: 'no'});
    var gt = input('text', p.gate, {maxlength: '200', 'data-f': 'gate', 'aria-label': 'Умова переходу',
                                    placeholder: 'умова переходу до наступної фази', class: 'pje-pgate'});
    [ic, nm, gt].forEach(function (i) { i.disabled = !!p.del; });
    [['icon', ic], ['name', nm], ['gate', gt]].forEach(function (x) {
      var w = el('span', 'pje-c');
      w.setAttribute('data-cf', x[0]);
      w.appendChild(x[1]);
      line.appendChild(w);
    });
    var acts = el('span', 'pje-acts');
    if (p.del) {
      acts.appendChild(btn('pundo', '↺', 'Повернути фазу'));
    } else {
      acts.appendChild(btn('addstep', '＋ крок', 'Новий крок у кінці цієї фази'));
      acts.appendChild(btn('pup', '↑', 'Фазу вище'));
      acts.appendChild(btn('pdown', '↓', 'Фазу нижче'));
      if (p.ai || (p.id && changedPhase(p))) acts.appendChild(btn('pundo', '↺', p.ai ? 'Повернути як було до AI' : 'Повернути як було'));
      acts.appendChild(btn('pdel', '🗑', 'Прибрати фазу (її кроки лишаться без фази)'));
    }
    line.appendChild(acts);
    c.appendChild(line);
    if (p.ai && p.ai.why) {
      var w = el('div', 'pje-why', '🧠 ' + (p.ai.remove ? 'прибрати: ' : p.ai.add ? 'нова фаза: ' : '') + p.ai.why);
      w.setAttribute('translate', 'no');
      c.appendChild(w);
    }
    tr.appendChild(c);
    marks(tr, p);
    return tr;
  }
  function looseRow() {
    var tr = el('tr', 'pje-ph pje-loose');
    tr.setAttribute('data-key', '');
    var c = el('td');
    c.colSpan = 10;
    var line = el('div', 'pje-phl');
    line.appendChild(el('b', '', S.phases.length ? 'Без фази' : 'Кроки'));
    var acts = el('span', 'pje-acts');
    acts.appendChild(btn('addstep', '＋ крок', 'Новий крок'));
    line.appendChild(acts);
    c.appendChild(line);
    tr.appendChild(c);
    return tr;
  }
  function render(focusKey, focusField) {
    var body = dlg.querySelector('[data-pje-body]'), nums = numbers();
    body.textContent = '';
    seqRows().forEach(function (r) {
      body.appendChild(r.p ? phaseRow(r.p) : r.loose ? looseRow() : stepRow(r.s, nums));
    });
    if (!S.steps.length && !S.phases.length) {
      var tr = el('tr'), c = el('td', 'pje-empty', 'Плану ще немає — додайте фазу й кроки або напишіть AI, що скласти.');
      c.colSpan = 10;
      tr.appendChild(c);
      body.appendChild(tr);
    }
    count();
    info();
    if (focusKey !== undefined) {
      var i = body.querySelector('tr[data-key="' + focusKey + '"] [data-f="' + (focusField || 'title') + '"]');
      if (i) { i.focus(); if (i.select) i.select(); }
    }
  }
  function count() {
    var n = changes(), c = dlg.querySelector('[data-pje-count]'), save = dlg.querySelector('[data-pje-save]');
    c.textContent = n ? 'незбережених змін: ' + n : 'змін немає';
    save.textContent = n ? '💾 Зберегти план (' + n + ')' : '💾 Зберегти план';
  }
  function info() {
    var box = dlg.querySelector('[data-pje-sum]'), i = S.info;
    box.textContent = '';
    box.hidden = !i;
    if (!i) return;
    var c = i.cnt, parts = [];
    if (c.upd) parts.push('змінено ' + c.upd);
    if (c.add) parts.push('додано ' + c.add);
    if (c.rm) parts.push('прибрати ' + c.rm);
    var head = el('div', 'pje-sum-h', parts.length ? '🧠 AI пропонує: ' + parts.join(' · ') : '🧠 AI нічого не змінив');
    box.appendChild(head);
    if (i.summary) {
      var p = el('div', '', i.summary);
      p.setAttribute('translate', 'no');
      box.appendChild(p);
    }
    var sub = [];
    if (i.dropped) sub.push('незрозумілих пропозицій відкинуто: ' + i.dropped);
    if (i.model) sub.push(i.model);
    if (sub.length) box.appendChild(el('div', 'pjc-sub', sub.join(' · ')));
    if (parts.length) {
      box.appendChild(el('div', 'pjc-sub', 'Зміни вже в таблиці (фіолетовим) — наведіть на клітинку, щоб побачити, що було. ' +
        '↺ у рядку — повернути, «💾 Зберегти план» — записати.'));
      var b = btn('aiundo', '↺ Повернути все, що змінив AI');
      b.className = 'pjc-b';
      box.appendChild(b);
    }
  }

  // ── Правки ──
  function sync(tr, s) {
    var ms = s.kind === 'milestone';
    [['start', s.start], ['end', s.end], ['duration', ms ? 0 : s.duration]].forEach(function (x) {
      var i = tr.querySelector('[data-f="' + x[0] + '"]');
      if (i && i !== document.activeElement) i.value = x[1];
    });
    var st = tr.querySelector('[data-f="start"]'), du = tr.querySelector('[data-f="duration"]');
    if (st) st.disabled = ms;
    if (du) du.disabled = ms;
  }
  function dates(s, set) {
    if (s.kind === 'milestone') {
      s.duration = 0;
      s.end = s.end || s.start;
      s.start = '';
      return;
    }
    s.duration = Math.max(1, Math.min(parseInt(s.duration, 10) || 1, 999));
    if (set.start && set.end && s.start && s.end && s.end >= s.start) s.duration = diff(s.start, s.end) + 1;
    else if (s.start && (set.start || set.duration)) s.end = addDays(s.start, s.duration - 1);
    else if (set.end && s.start && s.end && s.end >= s.start) s.duration = diff(s.start, s.end) + 1;
  }
  function parseAfter(text, s) {
    var nums = numbers(), byNum = {}, out = [], bad = false;
    Object.keys(nums).forEach(function (k) { byNum[nums[k]] = k; });
    String(text || '').split(/[,;\s]+/).filter(Boolean).forEach(function (t) {
      var m = /^(\d+)\s*([+-]\s*\d+)?$/.exec(t);
      var key = m && byNum[+m[1]];
      if (!key || key === s.key) { bad = true; return; }
      if (out.some(function (a) { return a.key === key; })) return;
      var lag = m[2] ? parseInt(m[2].replace(/\s/g, ''), 10) : (s.after.filter(function (a) { return a.key === key; })[0] || {}).lag || 0;
      out.push({key: key, lag: Math.max(-365, Math.min(365, lag))});
    });
    return {list: out, bad: bad};
  }
  function onInput(e) {
    var i = e.target, f = i.getAttribute('data-f'), tr = i.closest('tr[data-key]');
    if (!f || !tr) return;
    var key = tr.getAttribute('data-key'), p = findPhase(key);
    if (p) { p[f] = i.value; marks(tr, p); count(); return; }
    var s = findStep(key);
    if (!s) return;
    if (f === 'after') {
      if (e.type !== 'change') return;
      var r = parseAfter(i.value, s);
      i.classList.toggle('pje-bad', r.bad);
      i.title = r.bad ? 'Номер рядка не знайдено: пишіть «3, 5» або «5+2»' : 'Номери рядків, після яких іде крок';
      s.after = r.list;
      if (!r.bad) i.value = afterText(s, numbers());
    } else if (f === 'kind') {
      s.kind = i.checked ? 'milestone' : 'task';
      if (s.kind === 'milestone') { s.end = s.end || s.start; s.start = ''; s.duration = 0; }
      else { s.start = s.end; s.duration = 1; }
      sync(tr, s);
    } else if (f === 'start' || f === 'end' || f === 'duration') {
      if (f === 'duration') {
        var n = parseInt(i.value, 10);
        if (!(n > 0)) return;
        s.duration = Math.min(n, 999);
      } else {
        s[f] = i.value;
      }
      var set = {};
      set[f] = true;
      dates(s, set);
      sync(tr, s);
      var bad = s.kind !== 'milestone' && s.start && s.end && s.end < s.start;
      tr.querySelector('[data-f="end"]').classList.toggle('pje-bad', !!bad);
    } else {
      s[f] = i.value;
    }
    marks(tr, s);
    count();
  }

  // ── Рядки: додати, перемістити, прибрати ──
  function newStep(phase) {
    return {key: 'n' + (++seq), id: null, phase: phase || '', title: '', kind: 'task', side: 'we', party: '',
            start: '', end: '', duration: 1, status: 'todo', eff: '', note: '', f_start: '', f_end: '', after: [],
            ext: 0, o: null};
  }
  function addStep(phase, afterStep) {
    var s = newStep(phase), list = S.steps;
    if (afterStep) {
      list.splice(list.indexOf(afterStep) + 1, 0, s);
      s.after = [{key: afterStep.key, lag: 0}];
      var base = afterStep.end || afterStep.f_end;
      if (base) { s.start = addDays(base, 1); s.end = s.start; }
    } else {
      var last = null;
      list.forEach(function (x) { if (x.phase === phase && !x.del) last = x; });
      if (last) return addStep(phase, last);
      list.push(s);
    }
    render(s.key);
  }
  function addPhase() {
    var p = {key: 'np' + (++seq), id: null, name: 'Нова фаза', icon: '', gate: '', o: null};
    S.phases.push(p);
    render(p.key, 'name');
  }
  function moveStep(s, dir) {
    var rows = seqRows(), idx = -1;
    rows.forEach(function (r, i) { if (r.s === s) idx = i; });
    var t = rows[idx + dir];
    if (!t) return;
    var list = S.steps;
    if (t.s) {
      var a = list.indexOf(s), b = list.indexOf(t.s);
      list[a] = t.s;
      list[b] = s;
    } else if (dir < 0) {                           // над заголовком своєї фази → у кінець попередньої
      var live = S.phases.filter(function (p) { return !p.del; }), cur = t.p ? live.indexOf(t.p) : live.length;
      var prev = live[cur - 1];
      if (!prev) return;
      s.phase = prev.key;
      list.splice(list.indexOf(s), 1);
      list.push(s);
    } else {                                        // під заголовок наступної фази → на її початок
      if (t.p && t.p.del) return;
      s.phase = t.p ? t.p.key : '';
      list.splice(list.indexOf(s), 1);
      list.unshift(s);
    }
    render(s.key);
  }
  function movePhase(p, dir) {
    var i = S.phases.indexOf(p), j = i + dir;
    if (j < 0 || j >= S.phases.length) return;
    S.phases[i] = S.phases[j];
    S.phases[j] = p;
    render(p.key, 'name');
  }
  function drop(s) {
    S.steps.splice(S.steps.indexOf(s), 1);
    S.steps.forEach(function (x) { x.after = x.after.filter(function (a) { return a.key !== s.key; }); });
  }
  function delStep(s) {
    if (!s.id) { drop(s); return; }
    var chain = [];                                 // як на сервері: наступні йдуть після його попередніх
    S.steps.forEach(function (x) {
      var i = -1;
      x.after.forEach(function (a, k) { if (a.key === s.key) i = k; });
      if (i < 0) return;
      chain.push({x: x, after: x.after.slice()});
      var lag = x.after[i].lag;
      x.after.splice(i, 1);
      s.after.forEach(function (a) {
        if (a.key !== x.key && !x.after.some(function (b) { return b.key === a.key; })) x.after.push({key: a.key, lag: lag});
      });
    });
    s.del = {chain: chain};
  }
  function undel(s) {
    (s.del.chain || []).forEach(function (c) { c.x.after = c.after; });
    s.del = false;
  }
  function delPhase(p) {
    var moved = S.steps.filter(function (s) { return s.phase === p.key; });
    moved.forEach(function (s) { s.phase = ''; });
    if (!p.id) { S.phases.splice(S.phases.indexOf(p), 1); return; }
    p.del = {steps: moved};
  }
  function undelPhase(p) {
    (p.del.steps || []).forEach(function (s) { if (!s.phase) s.phase = p.key; });
    p.del = false;
  }
  function revertStep(s) {
    if (s.ai) {
      if (s.ai.add) { drop(s); return; }
      if (s.del) undel(s);
      var b = s.ai.before;
      ST.forEach(function (k) { s[k] = k === 'after' ? b.after.map(function (a) { return {key: a.key, lag: a.lag}; }) : b[k]; });
      s.ai = null;
      return;
    }
    if (s.del) { undel(s); return; }
    ST.forEach(function (k) { s[k] = k === 'after' ? s.o.after.map(function (a) { return {key: a.key, lag: a.lag}; }) : s.o[k]; });
    if (!livePhase(s.phase)) s.phase = '';
  }
  function revertPhase(p) {
    if (p.ai) {
      if (p.ai.add) { delPhase(p); return; }
      if (p.del) undelPhase(p);
      PH.forEach(function (k) { p[k] = p.ai.before[k]; });
      p.ai = null;
      return;
    }
    if (p.del) { undelPhase(p); return; }
    PH.forEach(function (k) { p[k] = p.o[k]; });
  }
  function onClick(e) {
    var b = e.target.closest('[data-pje-act]');
    if (!b || b.disabled) return;
    e.preventDefault();
    var act = b.getAttribute('data-pje-act'), tr = b.closest('tr[data-key]'), key = tr ? tr.getAttribute('data-key') : null;
    var s = key ? findStep(key) : null, p = key ? findPhase(key) : null;
    if (act === 'addphase') { addPhase(); return; }
    if (act === 'aiundo') { revertAll(); return; }
    if (act === 'addstep') {
      if (tr) { addStep(key); return; }
      var live = S.phases.filter(function (x) { return !x.del; });
      addStep(live.length ? live[live.length - 1].key : '');
      return;
    }
    if (s && act === 'up') moveStep(s, -1);
    else if (s && act === 'down') moveStep(s, 1);
    else if (s && act === 'del') { delStep(s); render(); }
    else if (s && act === 'undo') { revertStep(s); render(); }
    else if (p && act === 'pup') movePhase(p, -1);
    else if (p && act === 'pdown') movePhase(p, 1);
    else if (p && act === 'pdel') { delPhase(p); render(); }
    else if (p && act === 'pundo') { revertPhase(p); render(); }
  }
  function onKey(e) {
    var i = e.target, tr = i.closest && i.closest('tr[data-key]');
    if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.code === 'KeyS')) { e.preventDefault(); save(); return; }
    if (!tr) return;
    var s = findStep(tr.getAttribute('data-key'));
    if (s && e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      if (i.getAttribute('data-f') === 'after') i.dispatchEvent(new Event('change', {bubbles: true}));
      moveStep(s, e.key === 'ArrowUp' ? -1 : 1);
      return;
    }
    if (e.key === 'Enter' && !e.isComposing && i.tagName === 'INPUT') {
      e.preventDefault();
      if (i.getAttribute('data-f') === 'after') i.dispatchEvent(new Event('change', {bubbles: true}));
      if (s && i.getAttribute('data-f') === 'title') addStep(s.phase, s);
      else if (!s && i.getAttribute('data-f') === 'name') addStep(tr.getAttribute('data-key'));
    }
  }

  // ── 🧠 Що змінити в плані ──
  function tablePayload() {
    var phases = S.phases.filter(function (p) { return !p.del; }).map(function (p) {
      return {key: p.key, name: p.name, icon: p.icon, gate: p.gate};
    });
    var steps = [];
    seqRows().forEach(function (r) {
      if (!r.s || r.s.del) return;
      var s = r.s;
      steps.push({key: s.key, phase: livePhase(s.phase) ? s.phase : '', title: s.title, kind: s.kind, side: s.side,
                  start: s.start, end: s.end, duration: s.duration, status: s.status,
                  after: s.after.map(function (a) { return a.key; }), f_start: s.f_start, f_end: s.f_end});
    });
    return {phases: phases, steps: steps};
  }
  function before(r, keys) {
    if (!r.ai) r.ai = {before: snap(r, keys), fields: [], why: ''};
    return r.ai;
  }
  function applyAI(res) {
    var map = {}, made = [], cnt = {upd: 0, add: 0, rm: 0}, list = res.changes || [];
    function k(ref) { return map[ref] || ref || ''; }
    list.forEach(function (ch) {
      if (ch.op !== 'add_phase') return;
      var p = {key: 'np' + (++seq), id: null, name: ch.set.name || 'Нова фаза', icon: ch.set.icon || '',
               gate: ch.set.gate || '', o: null, ai: {add: true, why: ch.why || '', fields: [], before: {}}};
      map[ch.ref] = p.key;
      var at = ch.place_after ? findPhase(k(ch.place_after)) : null;
      if (at) S.phases.splice(S.phases.indexOf(at) + 1, 0, p);
      else S.phases.unshift(p);
      cnt.add++;
    });
    list.forEach(function (ch) {
      if (ch.op !== 'add') return;
      var s = newStep('');
      s.ai = {add: true, why: ch.why || '', fields: [], before: {}};
      map[ch.ref] = s.key;
      made.push([s, ch]);
      cnt.add++;
    });
    function setFields(s, set) {
      Object.keys(set).forEach(function (f) {
        if (f === 'after') {
          s.after = set.after.map(function (a) {
            var key = k(a), old = s.after.filter(function (x) { return x.key === key; })[0];
            return {key: key, lag: old ? old.lag : 0};
          }).filter(function (a) { return findStep(a.key) || made.some(function (m) { return m[0].key === a.key; }); });
        } else if (f === 'phase') {
          s.phase = livePhase(k(set.phase)) ? k(set.phase) : '';
        } else {
          s[f] = set[f];
        }
      });
      dates(s, set);
    }
    made.forEach(function (m) {
      var s = m[0], ch = m[1], at = ch.place_after ? findStep(k(ch.place_after)) : null;
      if (at) S.steps.splice(S.steps.indexOf(at) + 1, 0, s);
      else S.steps.push(s);
    });
    made.forEach(function (m) {
      var s = m[0], ch = m[1], at = ch.place_after ? findStep(k(ch.place_after)) : null;
      setFields(s, ch.set);
      if (ch.set.phase === undefined) s.phase = at ? at.phase : '';
      if (s.kind !== 'milestone' && !s.start && s.after.length) {      // без дат — одразу після попередніх
        var base = '';
        s.after.forEach(function (a) {
          var p = findStep(a.key), e = p && (p.end || p.f_end);
          if (e && e > base) base = e;
        });
        if (base) {
          s.start = addDays(base, 1);
          if (!s.end || s.end < s.start) s.end = addDays(s.start, s.duration - 1);
        }
      }
      if (!ch.place_after && s.phase) {                 // «на початку фази»
        S.steps.splice(S.steps.indexOf(s), 1);
        var first = null;
        S.steps.forEach(function (x) { if (!first && x.phase === s.phase) first = x; });
        if (first) S.steps.splice(S.steps.indexOf(first), 0, s);
        else S.steps.push(s);
      }
    });
    list.forEach(function (ch) {
      var s, p, a;
      if (ch.op === 'update') {
        s = findStep(ch.id);
        if (!s || s.del) return;
        a = before(s, ST);
        setFields(s, ch.set);
        Object.keys(ch.set).forEach(function (f) { if (a.fields.indexOf(f) < 0) a.fields.push(f); });
        if (ch.set.start || ch.set.end || ch.set.duration !== undefined) {
          ['start', 'end', 'duration'].forEach(function (f) { if (a.fields.indexOf(f) < 0 && !same(s[f], a.before[f], f)) a.fields.push(f); });
        }
        a.why = ch.why || a.why;
        cnt.upd++;
      } else if (ch.op === 'remove') {
        s = findStep(ch.id);
        if (!s || s.del) return;
        a = before(s, ST);
        if (!s.id) { drop(s); cnt.rm++; return; }
        delStep(s);
        a.remove = true;
        a.why = ch.why || a.why;
        cnt.rm++;
      } else if (ch.op === 'update_phase') {
        p = findPhase(ch.id);
        if (!p || p.del) return;
        a = before(p, PH);
        Object.keys(ch.set).forEach(function (f) { p[f] = ch.set[f]; if (a.fields.indexOf(f) < 0) a.fields.push(f); });
        a.why = ch.why || a.why;
        cnt.upd++;
      } else if (ch.op === 'remove_phase') {
        p = findPhase(ch.id);
        if (!p || p.del) return;
        a = before(p, PH);
        a.remove = true;
        a.why = ch.why || a.why;
        if (!p.id) { delPhase(p); cnt.rm++; return; }
        delPhase(p);
        cnt.rm++;
      }
    });
    S.info = {summary: res.summary || '', cnt: cnt, dropped: res.dropped || 0, model: res.model || ''};
    render();
  }
  function revertAll() {
    S.steps.slice().forEach(function (s) { if (s.ai) revertStep(s); });
    S.phases.slice().forEach(function (p) { if (p.ai) revertPhase(p); });
    S.info = null;
    render();
  }
  function revise() {
    var w = dlg.querySelector('[data-pje-wish]'), b = dlg.querySelector('[data-pje-revise]');
    var err = dlg.querySelector('[data-pje-aierr]'), wish = w.value.trim();
    err.textContent = '';
    if (!wish) { err.textContent = 'Напишіть, що змінити в плані.'; w.focus(); return; }
    if (S.steps.some(function (s) { return s.ai; }) || S.phases.some(function (p) { return p.ai; })) {
      if (!window.confirm('Попередні пропозиції AI лишаться в таблиці — нові додадуться до них. Далі?')) return;
    }
    var label = b.textContent;
    b.disabled = true;
    b.textContent = '🧠 AI думає… до 2 хвилин';
    O.bgRun(O.urls.revise, function (j) {
      b.disabled = false;
      b.textContent = label;
      if (!S) return;
      applyAI(j.result || {});
    }, function (e) {
      b.disabled = false;
      b.textContent = label;
      err.textContent = '⚠️ ' + e;
    }, {wish: wish, table: tablePayload()});
  }

  // ── 💾 Зберегти ──
  function payload() {
    var t = tablePayload(), stepsByKey = {};
    S.steps.forEach(function (s) { stepsByKey[s.key] = s; });
    t.steps.forEach(function (x) {
      var s = stepsByKey[x.key];
      x.after = s.after.map(function (a) { return {key: a.key, lag: a.lag}; });
      delete x.f_start;
      delete x.f_end;
    });
    t.delete_steps = S.steps.filter(function (s) { return s.del && s.id; }).map(function (s) { return s.id; });
    t.delete_phases = S.phases.filter(function (p) { return p.del && p.id; }).map(function (p) { return p.id; });
    return t;
  }
  function validate() {
    var body = dlg.querySelector('[data-pje-body]'), bad = null;
    S.phases.forEach(function (p) {
      if (!bad && !p.del && !p.name.trim()) bad = {key: p.key, f: 'name', text: 'Напишіть назву фази.'};
    });
    var nums = numbers();
    S.steps.forEach(function (s) {
      if (bad || s.del) return;
      if (!s.title.trim()) bad = {key: s.key, f: 'title', text: 'Рядок ' + nums[s.key] + ': напишіть назву кроку.'};
      else if (s.kind !== 'milestone' && s.start && s.end && s.end < s.start) {
        bad = {key: s.key, f: 'end', text: 'Рядок ' + nums[s.key] + ': кінець раніше за початок.'};
      }
    });
    var bi = body.querySelector('.pje-bad');
    if (!bad && bi) bad = {node: bi, text: 'Виправте підсвічене червоним.'};
    if (!bad) return null;
    var n = bad.node || body.querySelector('tr[data-key="' + bad.key + '"] [data-f="' + bad.f + '"]');
    if (n) n.focus();
    return bad.text;
  }
  function save() {
    var err = dlg.querySelector('[data-pje-err]'), b = dlg.querySelector('[data-pje-save]');
    err.textContent = '';
    var a = document.activeElement;
    if (a && a.getAttribute && a.getAttribute('data-f') === 'after') a.dispatchEvent(new Event('change', {bubbles: true}));
    var bad = validate();
    if (bad) { err.textContent = '⚠️ ' + bad; return; }
    if (!changes()) { close(true); return; }
    b.disabled = true;
    O.send(O.urls.save, payload()).then(function (j) {
      b.disabled = false;
      if (j && j.ok && j.html) {
        S = null;
        dlg.close();
        O.put(j.html);
        return;
      }
      err.textContent = '⚠️ ' + ((j && j.error) || 'не вдалося');
    });
  }
  function close(force) {
    if (!force && S && changes() && !window.confirm('Закрити без збереження? Незбережені зміни (' + changes() + ') зникнуть.')) return;
    S = null;
    dlg.close();
  }

  function build() {
    dlg = el('dialog', 'pjp-dlg pje-dlg');
    dlg.id = 'pje';
    dlg.setAttribute('aria-labelledby', 'pje-h');
    dlg.innerHTML =
      '<div class="pje-head"><h3 id="pje-h">✏️ Редактор плану</h3><span class="pjc-sub" data-pje-count></span>' +
      '<button type="button" class="pjc-b pje-x" data-pje-close title="Закрити (Esc)" aria-label="Закрити">✕</button></div>' +
      '<div class="pje-ai" data-pje-ai hidden>' +
      '<label class="pje-ask"><span>🧠 Що змінити в плані? <small class="pjc-sub">пишіть як колезі — або продиктуйте 🎤</small></span>' +
      '<textarea rows="2" data-pje-wish data-voice></textarea></label>' +
      '<div class="pje-ai-row"><button type="button" class="pjc-b pri" data-pje-revise>🧠 Запропонувати виправлення</button>' +
      '<span class="pjc-sub" data-pje-aihint></span><span class="pjc-merr" role="alert" data-pje-aierr></span></div>' +
      '<div class="pje-sum" data-pje-sum hidden></div></div>' +
      '<div class="pje-wrap"><table class="pje-t"><thead><tr><th class="pje-n">№</th><th>Назва</th>' +
      '<th title="Віха — ключовий момент без тривалості">◆</th><th>Хто робить</th><th>Початок</th><th>Кінець</th>' +
      '<th title="Календарні дні">Днів</th><th title="Номери рядків, після яких іде крок: «3, 5» або «5+2» — через 2 дні після 5-го">' +
      'Після №</th><th>Стан</th><th></th></tr></thead><tbody data-pje-body></tbody></table></div>' +
      '<div class="pje-add"><button type="button" class="pjc-b" data-pje-act="addphase">＋ Фаза</button>' +
      '<button type="button" class="pjc-b" data-pje-act="addstep">＋ Крок</button>' +
      '<span class="pjc-sub" data-pje-keys></span></div>' +
      '<div class="pjp-foot"><button type="button" class="pjc-b pri" data-pje-save>💾 Зберегти план</button>' +
      '<button type="button" class="pjc-b" data-pje-close>Скасувати</button><span class="pjc-merr" role="alert" data-pje-err></span></div>';
    // довгі тексти — кожен одним рядком (🌐 переклад інтерфейсу шукає цілі фрази)
    dlg.querySelector('[data-pje-wish]').placeholder = 'Наприклад: після складання додай тестування прототипу на 2 тижні · підтягни все до строку клієнта 20.12 · прототип уже отримали 10.11 — познач виконаним';
    dlg.querySelector('[data-pje-aihint]').textContent = 'AI бачить таблицю такою, як зараз (і незбережене), ваші листи проєкту й тривалості минулих проєктів. Зміни з’являться в таблиці підсвіченими — у план нічого не запишеться, доки ви не натиснете «💾 Зберегти план».';
    dlg.querySelector('[data-pje-keys]').textContent = 'Enter у назві — новий крок нижче · Alt + ↑ ↓ — перемістити рядок · Ctrl + S — зберегти · змінене — жовта риска, від AI — фіолетовим · наведіть — що було';
    document.body.appendChild(dlg);
    dlg.addEventListener('input', onInput);
    dlg.addEventListener('change', onInput);
    dlg.addEventListener('click', function (e) {
      if (e.target.closest('[data-pje-close]')) { e.preventDefault(); close(false); return; }
      if (e.target.closest('[data-pje-save]')) { e.preventDefault(); save(); return; }
      if (e.target.closest('[data-pje-revise]')) { e.preventDefault(); revise(); return; }
      onClick(e);
    });
    dlg.addEventListener('keydown', onKey);
    dlg.querySelector('[data-pje-wish]').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); revise(); }
    });
    dlg.addEventListener('cancel', function (e) {                 // Esc
      e.preventDefault();
      close(false);
    });
  }
  function open(opts) {
    O = opts || {};
    if (!dlg) build();
    init(O.data || {});
    dlg.querySelector('[data-pje-ai]').hidden = !O.ai;
    dlg.querySelector('[data-pje-err]').textContent = '';
    dlg.querySelector('[data-pje-aierr]').textContent = '';
    render();
    dlg.showModal();
    var first = dlg.querySelector('[data-pje-body] [data-f="title"]');
    if (O.ai && !S.steps.length) dlg.querySelector('[data-pje-wish]').focus();
    else if (first) first.focus();
  }
  window.PJEditor = {open: open, isOpen: function () { return !!(dlg && dlg.open); }};
})();
