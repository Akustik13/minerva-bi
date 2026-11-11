/* 🎙 Наради — підготовка, сторінка наради, протоколи (meetings/templates/admin/meetings/meeting/*.html).
   - мова тексту згоди (de / en / uk), «📋 Копіювати», «＋ Ще хтось», клієнт за проєктом, confirm за data-confirm;
   - «🎙 Відкрити вікно запису»: після кліку кнопка 5 с неактивна (подвійний клік відкрив би дві вкладки);
   - ✓ Прийняти ▾ / ✕ Не те / ↺ Повернути — POST JSON, у відповідь свіжий фрагмент пункту;
   - пошук у розшифровці й #t=<секунда> в адресі;
   - опитування стану кожні 5 с: нове — перезавантаження, але не тоді, коли відкрито панель ✓ або курсор у полі
     (тоді смуга «🔄 Є нове — оновити сторінку»).
   Дані людини — лише через textContent; розшифровку браузер не зберігає (localStorage не використовується). */
(function () {
  'use strict';
  var root = document.getElementById('mt-page');
  if (!root || root.dataset.ready) return;
  root.dataset.ready = '1';

  function csrf() {
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    if (el && el.value) return el.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf(), 'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {ok: false, error: 'HTTP ' + r.status}; });
    }, function () { return {ok: false, error: 'Сервер не відповідає — спробуйте ще раз'}; });
  }

  function toast(text) {
    if (window.MVMenu && window.MVMenu.toast) { window.MVMenu.toast(text); return; }
    var el = document.createElement('div');
    el.className = 'mt-banner';
    el.setAttribute('role', 'status');
    el.textContent = text;
    root.insertBefore(el, root.firstChild);
    setTimeout(function () { el.remove(); }, 2500);
  }

  // ── підтвердження (текст — у data-confirm, mv_translate його перекладає) ──
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !root.contains(form)) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if (form.classList.contains('mt-rec-form')) {
      var btn = form.querySelector('button[type=submit]');
      if (btn) {
        setTimeout(function () { btn.disabled = true; }, 0);
        setTimeout(function () { btn.disabled = false; }, 5000);
      }
    }
  }, true);

  // ── мова тексту згоди й примітки для запрошення ──
  root.addEventListener('change', function (e) {
    var t = e.target;
    if (t.matches('[data-mt-langpick]')) {
      var box = t.closest('[data-mt-langbox]');
      if (!box) return;
      box.querySelectorAll('[data-mt-lang]').forEach(function (el) {
        el.hidden = el.getAttribute('data-mt-lang') !== t.value;
      });
    } else if (t.matches('[data-mt-project]')) {
      var form = t.form, opt = t.options[t.selectedIndex];
      var cust = form && form.querySelector('[data-mt-customer]');
      var id = opt ? opt.getAttribute('data-customer') : '';
      if (cust && id && cust.querySelector('option[value="' + id + '"]')) cust.value = id;
    }
  });

  root.addEventListener('click', function (e) {
    var t = e.target.closest('button, a');
    if (!t || !root.contains(t)) return;
    if (t.hasAttribute('data-mt-copy')) {
      var src = document.getElementById(t.getAttribute('data-mt-copy'));
      if (!src) return;
      var done = function () { toast('📋 Скопійовано'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(src.value).then(done, function () { src.select(); document.execCommand('copy'); done(); });
      } else { src.select(); document.execCommand('copy'); done(); }
    } else if (t.hasAttribute('data-mt-add-person')) {
      var tpl = document.getElementById('mt-person-tpl');
      var host = (t.closest('form') || root).querySelector('[data-mt-people-extra]');
      if (tpl && host) {
        host.appendChild(tpl.content.cloneNode(true));
        var inputs = host.querySelectorAll('input[name=x_name]');
        if (inputs.length) inputs[inputs.length - 1].focus();
      }
    } else if (t.hasAttribute('data-mt-open-dialog')) {
      var dlg = document.getElementById(t.getAttribute('data-mt-open-dialog'));
      if (dlg && dlg.showModal) dlg.showModal(); else if (dlg) dlg.setAttribute('open', '');
    } else if (t.hasAttribute('data-mt-close-dialog')) {
      var d = t.closest('dialog');
      if (d && d.close) d.close(); else if (d) d.removeAttribute('open');
    } else if (t.hasAttribute('data-mt-reload')) {
      location.reload();
    }
  });

  // ── ✓ / ✕ пункти ──
  var items = document.getElementById('mt-items');

  function panelTarget(item) {
    var r = item.querySelector('input[type=radio]:checked');
    return r ? r.value : '';
  }

  function showFor(item) {
    var target = panelTarget(item);
    item.querySelectorAll('[data-for]').forEach(function (el) {
      el.hidden = el.getAttribute('data-for').split(' ').indexOf(target) < 0;
    });
  }

  function setPending(n) {
    var el = document.getElementById('mt-pending-n');
    if (el && typeof n === 'number') el.textContent = String(n);
  }

  if (items) {
    items.querySelectorAll('.mt-item').forEach(showFor);
    items.addEventListener('change', function (e) {
      var item = e.target.closest('.mt-item');
      if (!item) return;
      if (e.target.type === 'radio') showFor(item);
      if (e.target.name === 'side') {
        var who = item.querySelector('select[name=assignee]');
        if (who) who.value = e.target.value === 'we' ? (item.dataset.me || '') : '';
      }
    });
    items.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      var item = btn && btn.closest('.mt-item');
      if (!item) return;
      var panel = item.querySelector('.mt-panel');
      if (btn.hasAttribute('data-mt-toggle')) {
        if (!panel) return;
        panel.hidden = !panel.hidden;
        item.querySelectorAll('[data-mt-toggle][aria-expanded]').forEach(function (b) {
          b.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
        });
        if (!panel.hidden) { showFor(item); var ti = panel.querySelector('input[name=title]'); if (ti) ti.focus(); }
        return;
      }
      var act = btn.getAttribute('data-mt-act');
      if (!act) return;
      var body = {action: act};
      if (act === 'accept') {
        body.target = panelTarget(item);
        if (!body.target) { showError(item, 'Виберіть, куди додати пункт'); return; }
        var val = function (name) { var f = item.querySelector('[name=' + name + ']'); return f ? f.value : ''; };
        body.title = val('title');
        if (body.target === 'moment') body.kind = val('kind');
        if (body.target === 'risk') { body.category = val('category'); if (val('owner')) body.owner = val('owner'); }
        if (body.target === 'step') { body.side = val('side'); body.assignee = val('assignee'); body.date = val('date'); }
        if (body.target === 'deadline') {
          body.date = val('date');
          if (!body.date) { showError(item, 'Виберіть дату дедлайну'); return; }
        }
      }
      btn.disabled = true;
      post(item.dataset.url, body).then(function (res) {
        btn.disabled = false;
        if (!res || !res.ok) { showError(item, (res && res.error) || 'Не вдалося'); return; }
        var wrap = document.createElement('div');
        wrap.innerHTML = res.html;              // фрагмент сервера: дані людини вже екрановано шаблоном
        var fresh = wrap.firstElementChild;
        if (fresh) { item.replaceWith(fresh); showFor(fresh); }
        setPending(res.pending);
        if (act === 'accept') toast('✓ Додано');
        else if (act === 'reject') toast('✕ Більше не пропонується');
      });
    });
  }

  function showError(item, text) {
    var el = item.querySelector('.mt-err');
    if (!el) {
      el = document.createElement('div');
      el.className = 'mt-err';
      el.setAttribute('role', 'alert');
      item.appendChild(el);
    }
    el.textContent = '⚠️ ' + text;
  }

  // ── розшифровка: пошук і #t=<секунда> ──
  var tr = document.getElementById('mt-tr');
  var q = document.getElementById('mt-tr-q');
  var count = document.getElementById('mt-tr-count');

  function highlight(span, needle) {
    var text = span.getAttribute('data-raw');
    if (text === null) { text = span.textContent; span.setAttribute('data-raw', text); }
    span.textContent = '';
    if (!needle) { span.textContent = text; return false; }
    var low = text.toLowerCase(), at = 0, i, found = false;
    while ((i = low.indexOf(needle, at)) >= 0) {
      span.appendChild(document.createTextNode(text.slice(at, i)));
      var mk = document.createElement('mark');
      mk.textContent = text.slice(i, i + needle.length);
      span.appendChild(mk);
      at = i + needle.length;
      found = true;
    }
    span.appendChild(document.createTextNode(text.slice(at)));
    return found;
  }

  if (tr && q) {
    var timer = null;
    q.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var needle = q.value.trim().toLowerCase(), hits = 0;
        tr.querySelectorAll('.mt-row').forEach(function (row) {
          var span = row.querySelector('.mt-text');
          if (!span) { row.hidden = !!needle; return; }
          var ok = highlight(span, needle);
          row.hidden = !!needle && !ok;
          if (ok) hits += 1;
        });
        if (count) count.textContent = needle ? ('Знайдено рядків: ' + hits) : '';
      }, 150);
    });
  }

  function jumpToTime() {
    var m = (location.hash || '').match(/^#t=(\d+)/);
    if (!m || !tr) return;
    var t = parseInt(m[1], 10), best = null;
    tr.querySelectorAll('.mt-seg').forEach(function (row) {
      if (parseInt(row.getAttribute('data-s'), 10) <= t) best = row;
    });
    best = best || tr.querySelector('.mt-seg');
    if (!best) return;
    if (q && q.value) { q.value = ''; q.dispatchEvent(new Event('input')); }
    tr.querySelectorAll('.mt-current').forEach(function (r) { r.classList.remove('mt-current'); });
    best.hidden = false;
    best.classList.add('mt-current');
    best.scrollIntoView({block: 'center', behavior: 'smooth'});
  }
  window.addEventListener('hashchange', jumpToTime);
  jumpToTime();

  // ── опитування стану ──
  var cfgEl = document.getElementById('mt-config');
  var cfg = null;
  try { cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { cfg = null; }
  if (!cfg || !cfg.poll || !cfg.status_url) return;

  var POLL = 5000, failures = 0, stopped = false;
  var banner = document.getElementById('mt-new');

  function busy() {
    if (document.querySelector('#mt-items .mt-panel:not([hidden])')) return true;
    if (document.querySelector('dialog[open]')) return true;
    if (document.querySelector('details.mt-edit[open]')) return true;
    var a = document.activeElement;
    return !!(a && root.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }

  function renderLines(lines) {
    var box = document.getElementById('mt-lines');
    if (!box || !Array.isArray(lines)) return;
    box.textContent = '';
    lines.forEach(function (l) {
      var div = document.createElement('div');
      div.className = 'mt-line-' + (l.tone || 'muted');
      div.textContent = l.text || '';
      box.appendChild(div);
    });
  }

  function changed(s) {
    return s.status !== cfg.status || !!s.transcript !== !!cfg.transcript || !!s.moments !== !!cfg.moments;
  }

  function tick() {
    if (stopped) return;
    if (document.hidden) { setTimeout(tick, POLL); return; }
    fetch(cfg.status_url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (s) {
        failures = 0;
        renderLines(s.lines);
        if (changed(s)) {
          if (!busy()) { location.reload(); return; }
          stopped = true;
          if (banner) banner.hidden = false;
          return;
        }
        setTimeout(tick, POLL);
      })
      .catch(function () {
        failures += 1;
        if (failures === 3) toast('Немає зв’язку з Minerva — спробую ще.');
        setTimeout(tick, failures > 3 ? POLL * 2 : POLL);
      });
  }
  setTimeout(tick, POLL);
})();
