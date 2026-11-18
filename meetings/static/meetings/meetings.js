/* 🎙 Наради — підготовка, сторінка наради, протоколи (meetings/templates/admin/meetings/meeting/*.html).
   - мова тексту згоди (de / en / uk), «📋 Копіювати», «＋ Ще хтось», клієнт за проєктом, confirm за data-confirm;
   - «🎙 Відкрити вікно запису»: після кліку кнопка 5 с неактивна (подвійний клік відкрив би дві вкладки);
   - «⚡ Швидкий запис» (data-mt-reload-after): вікно запису — у новій вкладці, ця сторінка оновлюється за 3 с;
   - ✓ Прийняти ▾ / 💬 Лише обговорення / ✕ Не те / ↺ Повернути — POST JSON, у відповідь свіжий фрагмент пункту
     (і проєкт пункту);
   - 📁 проєкти наради (додати / ⭐ головний / ✕) і 🏷 «звідси — про проєкт …» на рядках розшифровки — POST JSON,
     потім сторінка оновлюється;
   - пошук у розшифровці й #t=<секунда> в адресі;
   - 📋 агенда: додати, ＋ Підпункт, 📎 файли (вибір, перетягування, Ctrl+V скріншот), ☑ обговорили, ✏️ текст і «що вирішили», ⏭, ↑ ↓, ✕, 🧭 підказки з проєкту,
     📋 копіювати, ✉️ надіслати (вкладка листа відкривається одразу, адреса — після відповіді);
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
    // «⚡ Швидкий запис» у новій вкладці: нова нарада з’явиться в списку цієї сторінки сама
    if (form.hasAttribute('data-mt-reload-after') && !e.defaultPrevented) {
      setTimeout(function () { location.reload(); }, 3000);
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
      var val = function (name) { var f = item.querySelector('[name=' + name + ']'); return f ? f.value : ''; };
      if (act === 'note') {                     // 💬 одним кліком: теза наради, у проєкт нічого не йде
        body = {action: 'accept', target: 'note'};
        if (val('project')) body.project = val('project');
      } else if (act === 'accept') {
        body.target = panelTarget(item);
        if (!body.target) { showError(item, 'Виберіть, куди додати пункт'); return; }
        body.title = val('title');
        if (body.target === 'moment') body.kind = val('kind');
        if (body.target === 'risk') { body.category = val('category'); if (val('owner')) body.owner = val('owner'); }
        if (body.target === 'step') { body.side = val('side'); body.assignee = val('assignee'); body.date = val('date'); }
        if (body.target === 'deadline') {
          body.date = val('date');
          if (!body.date) { showError(item, 'Виберіть дату дедлайну'); return; }
        } else if (val('project')) {
          body.project = val('project');
        }
      }
      btn.disabled = true;
      post(item.dataset.url, body).then(function (res) {
        btn.disabled = false;
        if (!res || !res.ok) { showError(item, (res && res.error) || 'Не вдалося'); return; }
        var wrap = document.createElement('div');
        wrap.innerHTML = res.html;              // фрагмент сервера: дані людини вже екрановано шаблоном
        var fresh = wrap.firstElementChild;
        if (fresh) { item.replaceWith(fresh); showFor(fresh); syncPlan(fresh); }
        setPending(res.pending);
        if (act === 'note' || body.target === 'note') toast('💬 Позначено: лише обговорення');
        else if (act === 'accept') toast('✓ Додано');
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

  // ── 📁 проєкти наради й 🏷 частини запису (meetings/parts.py; адреси й проєкти — mt-config) ──
  var pcfg = null;
  try {
    var pcfgEl = document.getElementById('mt-config');
    pcfg = pcfgEl ? JSON.parse(pcfgEl.textContent) : null;
  } catch (err) { pcfg = null; }

  function pjPost(url, body, btn) {
    if (!url) return;
    if (btn) btn.disabled = true;
    post(url, body).then(function (res) {
      if (!res || !res.ok) {
        if (btn) btn.disabled = false;
        toast('⚠️ ' + ((res && res.error) || 'Не вдалося'));
        return;
      }
      location.reload();
    });
  }

  root.addEventListener('click', function (e) {
    var b = e.target.closest('[data-mt-pj], [data-mt-part]');
    if (!b || !root.contains(b) || !pcfg) return;
    if (b.getAttribute('data-confirm') && !window.confirm(b.getAttribute('data-confirm'))) return;
    if (b.hasAttribute('data-mt-pj')) {
      var act = b.getAttribute('data-mt-pj'), project = b.getAttribute('data-project') || '';
      if (act === 'add') {
        var pick = root.querySelector('[data-mt-pj-pick]');
        project = pick ? pick.value : '';
        if (!project) { toast('Виберіть проєкт у списку'); if (pick) pick.focus(); return; }
      }
      pjPost(pcfg.projects_url, {action: act, project: project}, b);
    } else if (b.getAttribute('data-mt-part') === 'remove') {
      pjPost(pcfg.part_url, {action: 'remove', part: b.getAttribute('data-part')}, b);
    } else {
      pjPost(pcfg.part_url, {action: 'set', second: b.getAttribute('data-second'),
                             project: b.getAttribute('data-project') || ''}, b);
    }
  });

  // 🏷 на рядку розшифровки: одна кнопка, що переходить за курсором (тисячі рядків — без тисяч кнопок), і правий
  // клік на рядку (з виділеним текстом або з Shift — звичайне меню браузера)
  var trBox = document.getElementById('mt-tr');
  if (trBox && pcfg && pcfg.can_parts && pcfg.pj && pcfg.pj.length && window.MVMenu) {
    var tagBtn = document.createElement('button');
    tagBtn.type = 'button';
    tagBtn.className = 'mt-tag-btn';
    tagBtn.textContent = '🏷';
    tagBtn.title = 'Звідси — про проєкт…';
    tagBtn.setAttribute('aria-haspopup', 'menu');
    var tagRow = null;

    var tagMenu = function (row, at) {
      var second = row.getAttribute('data-t') || row.getAttribute('data-s') || '0';
      var timeEl = row.querySelector('.mt-time');
      var set = function (project) {
        return function () { pjPost(pcfg.part_url, {action: 'set', second: second, project: project}); };
      };
      var list = pcfg.pj.map(function (p) { return {label: p.label, data: true, icon: '📁', onSelect: set(p.pk)}; });
      list.push({type: 'separator'});
      list.push({label: 'загальне, не про проєкт', icon: '💬', onSelect: set('')});
      window.MVMenu.open(at, list, {title: '🏷 Звідси — про проєкт', titleData: timeEl ? timeEl.textContent : true,
                                    label: 'Позначити частину наради'});
    };
    trBox.addEventListener('mouseover', function (e) {
      var row = e.target.closest('.mt-seg');
      if (!row || row === tagRow) return;
      tagRow = row;
      row.appendChild(tagBtn);
    });
    tagBtn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (tagRow) tagMenu(tagRow, tagBtn);
    });
    trBox.addEventListener('contextmenu', function (e) {
      if (e.shiftKey) return;
      var row = e.target.closest('.mt-seg');
      if (!row) return;
      if (window.getSelection && String(window.getSelection()).trim()) return;
      e.preventDefault();
      tagMenu(row, e);
    });
  }

  // проєкт пункту в «✓ Прийняти ▾»: у проєкті без плану «📅 Крок плану» недоступний
  function syncPlan(item) {
    var sel = item && item.querySelector('[data-mt-item-project]');
    if (!sel) return;
    var opt = sel.options[sel.selectedIndex];
    var noplan = !!(opt && opt.hasAttribute('data-noplan'));
    var step = item.querySelector('input[type=radio][value=step]');
    var moment = item.querySelector('input[type=radio][value=moment]');
    if (!step || !moment || moment.disabled) return;      // немає права чи проєкту — пояснення від сервера
    var label = step.closest('label');
    step.disabled = noplan;
    if (label) label.classList.toggle('is-off', noplan);
    var reason = label && label.nextElementSibling && label.nextElementSibling.classList.contains('mt-reason')
      ? label.nextElementSibling : null;
    if (noplan && !reason && label) {
      reason = document.createElement('div');
      reason.className = 'mt-reason';
      label.after(reason);
    }
    if (reason) {
      if (noplan) reason.textContent = sel.getAttribute('data-noplan-text') || ''; else reason.remove();
    }
    if (noplan && step.checked) { moment.checked = true; showFor(item); }
  }

  if (items) {
    items.querySelectorAll('.mt-item').forEach(syncPlan);
    items.addEventListener('change', function (e) {
      if (e.target.matches('[data-mt-item-project]')) syncPlan(e.target.closest('.mt-item'));
    });
  }

  // ── 🔄 Ключові моменти ще раз — Claude у Minerva (meetings/ai_moments.py): запуск і хід (status/ → ai_moments) ──
  var aiBox = document.getElementById('mt-ai');
  if (aiBox) {
    var aiState = document.getElementById('mt-ai-state');
    var aiCfgEl = document.getElementById('mt-config'), aiCfg = null;
    try { aiCfg = aiCfgEl ? JSON.parse(aiCfgEl.textContent) : null; } catch (err) { aiCfg = null; }
    var aiPoll = function () {
      if (!aiCfg || !aiCfg.status_url) return;
      fetch(aiCfg.status_url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
        .then(function (r) { return r.json(); })
        .then(function (s) {
          var st = s && s.ai_moments;
          if (!st || st.state === 'running') { setTimeout(aiPoll, 4000); return; }
          if (st.state === 'done') { location.reload(); return; }
          if (aiState) aiState.textContent = '⚠️ ' + (st.error || 'Не вдалося');
          var b = aiBox.querySelector('[data-mt-ai-start]');
          if (b) b.disabled = false;
        }, function () { setTimeout(aiPoll, 8000); });
    };
    if (aiState && aiState.getAttribute('data-state') === 'running') setTimeout(aiPoll, 4000);
    aiBox.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mt-ai-start]');
      if (!b) return;
      var picked = aiBox.querySelector('input[name=mt-ai-model]:checked');
      var consent = document.getElementById('mt-ai-consent');
      if (!consent || !consent.checked) {
        if (aiState) aiState.textContent = '⚠️ Поставте галочку згоди учасників на обробку в хмарі.';
        if (consent) consent.focus();
        return;
      }
      b.disabled = true;
      if (aiState) aiState.textContent = '⏳ Надсилаю розшифровку…';
      post(b.getAttribute('data-url'), {model: picked ? picked.value : '', consent: true}).then(function (res) {
        if (!res || !res.ok) {
          b.disabled = false;
          if (aiState) aiState.textContent = '⚠️ ' + ((res && res.error) || 'Не вдалося');
          return;
        }
        if (res.state && res.state.state === 'done') { location.reload(); return; }
        if (aiState) aiState.textContent = '⏳ ' + ((res.state && res.state.label) || 'Claude') + ' шукає ключові моменти… (зазвичай 1–3 хв; сторінку можна не тримати відкритою)';
        setTimeout(aiPoll, 4000);
      });
    });
  }

  // ── 📝 Підсумок наради й звірка з агендою — Claude у Minerva (meetings/ai_brief.py): запуск і хід (status/ → ai_brief) ──
  var briefBox = document.getElementById('mt-brief');
  if (briefBox) {
    var briefState = document.getElementById('mt-brief-state');
    var briefCfgEl = document.getElementById('mt-config'), briefCfg = null;
    try { briefCfg = briefCfgEl ? JSON.parse(briefCfgEl.textContent) : null; } catch (err) { briefCfg = null; }
    var briefPoll = function () {
      if (!briefCfg || !briefCfg.status_url) return;
      fetch(briefCfg.status_url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
        .then(function (r) { return r.json(); })
        .then(function (s) {
          var st = s && s.ai_brief;
          if (!st || st.state === 'running') { setTimeout(briefPoll, 4000); return; }
          if (st.state === 'done') { location.hash = 'brief'; location.reload(); return; }
          if (briefState) briefState.textContent = '⚠️ ' + (st.error || 'Не вдалося');
          var b = briefBox.querySelector('[data-mt-brief-start]');
          if (b) b.disabled = false;
        }, function () { setTimeout(briefPoll, 8000); });
    };
    if (briefState && briefState.getAttribute('data-state') === 'running') setTimeout(briefPoll, 4000);
    briefBox.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mt-brief-start]');
      if (!b) return;
      var picked = briefBox.querySelector('input[name=mt-brief-model]:checked');
      var lang = briefBox.querySelector('input[name=mt-brief-lang]:checked');
      var consent = document.getElementById('mt-brief-consent');
      if (!consent || !consent.checked) {
        if (briefState) briefState.textContent = '⚠️ Поставте галочку згоди учасників на обробку в хмарі.';
        if (consent) consent.focus();
        return;
      }
      b.disabled = true;
      if (briefState) briefState.textContent = '⏳ Надсилаю розшифровку й агенду…';
      post(b.getAttribute('data-url'), {model: picked ? picked.value : '', lang: lang ? lang.value : 'uk',
                                        consent: true}).then(function (res) {
        if (!res || !res.ok) {
          b.disabled = false;
          if (briefState) briefState.textContent = '⚠️ ' + ((res && res.error) || 'Не вдалося');
          return;
        }
        if (res.state && res.state.state === 'done') { location.hash = 'brief'; location.reload(); return; }
        if (briefState) briefState.textContent = '⏳ ' + ((res.state && res.state.label) || 'Claude') + ' складає підсумок… (зазвичай 1–2 хв; сторінку можна не тримати відкритою)';
        setTimeout(briefPoll, 4000);
      });
    });
  }

  // ── ✏️ Помилки розпізнавання — Claude у Minerva (meetings/ai_fix.py): запуск, хід (status/ → ai_fix), ✓ / ✕ / ↶ ──
  var fixBox = document.getElementById('mt-fix');
  if (fixBox) {
    var fixState = document.getElementById('mt-fix-state');
    var fixCfgEl = document.getElementById('mt-config'), fixCfg = null;
    try { fixCfg = fixCfgEl ? JSON.parse(fixCfgEl.textContent) : null; } catch (err) { fixCfg = null; }
    var fixPoll = function () {
      if (!fixCfg || !fixCfg.status_url) return;
      fetch(fixCfg.status_url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}})
        .then(function (r) { return r.json(); })
        .then(function (s) {
          var st = s && s.ai_fix;
          if (!st || st.state === 'running') { setTimeout(fixPoll, 4000); return; }
          if (st.state === 'done') { location.reload(); return; }
          if (fixState) fixState.textContent = '⚠️ ' + (st.error || 'Не вдалося');
          var b = fixBox.querySelector('[data-mt-fix-start]');
          if (b) b.disabled = false;
        }, function () { setTimeout(fixPoll, 8000); });
    };
    if (fixState && fixState.getAttribute('data-state') === 'running') setTimeout(fixPoll, 4000);
    fixBox.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mt-fix-start]');
      if (!b) return;
      var picked = fixBox.querySelector('input[name=mt-fix-model]:checked');
      var consent = document.getElementById('mt-fix-consent');
      if (!consent || !consent.checked) {
        if (fixState) fixState.textContent = '⚠️ Поставте галочку згоди учасників на обробку в хмарі.';
        if (consent) consent.focus();
        return;
      }
      b.disabled = true;
      if (fixState) fixState.textContent = '⏳ Надсилаю розшифровку…';
      post(b.getAttribute('data-url'), {model: picked ? picked.value : '', consent: true}).then(function (res) {
        if (!res || !res.ok) {
          b.disabled = false;
          if (fixState) fixState.textContent = '⚠️ ' + ((res && res.error) || 'Не вдалося');
          return;
        }
        if (res.state && res.state.state === 'done') { location.reload(); return; }
        if (fixState) fixState.textContent = '⏳ ' + ((res.state && res.state.label) || 'Claude') + ' перевіряє розшифровку… (зазвичай 1–3 хв; сторінку можна не тримати відкритою)';
        setTimeout(fixPoll, 4000);
      });
    });
  }

  function fixDecide(action, ids) {
    var panel = document.getElementById('mt-fixes');
    if (!panel || !panel.getAttribute('data-url')) return;
    post(panel.getAttribute('data-url'), {action: action, ids: ids === 'all' ? 'all' : [ids]}).then(function (res) {
      if (!res || !res.ok) { toast('⚠️ ' + ((res && res.error) || 'Не вдалося')); return; }
      var cur = document.getElementById('mt-fixes');
      if (cur && res.panel) {
        var wrap = document.createElement('div');
        wrap.innerHTML = res.panel.trim();
        if (wrap.firstElementChild) cur.replaceWith(wrap.firstElementChild);
      }
      Object.keys(res.rows || {}).forEach(function (n) {
        var span = document.querySelector('#t-' + n + ' .mt-text');
        if (!span) return;
        span.innerHTML = res.rows[n];             // готовий фрагмент сервера (екранований шаблоном)
        span.removeAttribute('data-raw');
        span.removeAttribute('data-html');
      });
      var msg = {accept: '✓ Виправлено', reject: '✕ Відхилено', reset: '↶ Повернуто'}[action] || '';
      if (res.changed > 1) msg += ': ' + res.changed;
      if (res.skipped) msg += ' · пропущено ' + res.skipped + ' (це місце вже змінила інша правка)';
      toast(msg);
    });
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-mt-fix]');
    if (!b) return;
    e.preventDefault();
    var action = b.getAttribute('data-mt-fix'), ids = b.getAttribute('data-ids');
    if (ids === 'all') {
      var n = document.getElementById('mt-fix-n');
      var ask = action === 'accept' ? 'Прийняти всі правки ({0})? Текст розшифровки зміниться — кожну можна повернути «↶».'
        : 'Відхилити всі правки ({0})?';
      if (!confirm(ask.replace('{0}', n ? n.textContent : ''))) return;
    }
    fixDecide(action, ids);
  });

  // позначка правки в тексті: ✓ / ✕ прямо з рядка
  var trFix = document.getElementById('mt-tr');
  if (trFix) {
    trFix.addEventListener('click', function (e) {
      var mk = e.target.closest('.mt-fx');
      if (!mk) return;
      var id = mk.getAttribute('data-fix');
      if (!window.MVMenu) {
        var row = document.getElementById('fx-' + id);
        if (row) row.scrollIntoView({block: 'center'});
        return;
      }
      window.MVMenu.open(e, [
        {label: '✓ Прийняти правку', onSelect: function () { fixDecide('accept', id); }},
        {label: '✕ Відхилити', onSelect: function () { fixDecide('reject', id); }}
      ], {title: mk.getAttribute('title') || '✏️ Правка', titleData: true, label: 'Правка розпізнавання'});
    });
  }

  // ── розшифровка: пошук і #t=<секунда> ──
  var tr = document.getElementById('mt-tr');
  var q = document.getElementById('mt-tr-q');
  var count = document.getElementById('mt-tr-count');

  function highlight(span, needle) {
    var text = span.getAttribute('data-raw');
    if (text === null) {
      text = span.textContent;
      span.setAttribute('data-raw', text);
      span.setAttribute('data-html', span.innerHTML);    // позначки правок — повернути, коли пошук очищено
    }
    if (!needle) {
      var html = span.getAttribute('data-html');
      if (html !== null) span.innerHTML = html; else span.textContent = text;
      return false;
    }
    span.textContent = '';
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

  // ── 📋 Агенда (meetings/agenda.py, _agenda.html): POST JSON на data-mt-agenda, у відповідь — свіжий блок ──
  function agendaBox() { return document.getElementById('agenda'); }
  function openSub(parent) {                     // «＋ Підпункт»: поле під останнім підпунктом цього пункту
    var row = document.querySelector('#agenda [data-ag-subadd="' + parent + '"]');
    if (!row) return;
    document.querySelectorAll('#agenda [data-ag-subadd]').forEach(function (r) { if (r !== row) r.hidden = true; });
    row.hidden = false;
    var input = row.querySelector('input[name=text]');
    if (input) input.focus();
  }
  // 🌐 переклад під пунктами: після змін (AUTO_TR) — одним запитом, поки людина пише далі; відповідь, старіша за вже
  // показану, не малюється; що людина саме вводить (поля, відкриті форми, фокус) — лишається після оновлення блоку
  var agSeq = 0, agShown = 0, trBusy = false, trAgain = false;
  var AUTO_TR = {add: 1, edit: 1, take: 1, take_all: 1, tr_lang: 1, ai_ok: 1, ai_all: 1};
  function agKey(el) {
    var row = el.closest('[data-ag]'), sub = el.closest('[data-ag-subadd]');
    var kind = el.closest('[data-ag-edit]') ? 'edit' : el.closest('[data-ag-send]') ? 'send' : sub ? 'sub'
      : el.closest('[data-ag-add]') ? 'add' : 'bar';
    return kind + ':' + (row ? row.getAttribute('data-ag') : sub ? sub.getAttribute('data-ag-subadd') : '') + ':'
      + (el.name || '') + ':' + (el.type === 'radio' ? el.value : '');
  }
  function agPanel(f) {
    if (f.hasAttribute('data-ag-subadd')) return 'sub:' + f.getAttribute('data-ag-subadd');
    if (f.hasAttribute('data-ag-send')) return 'send';
    var row = f.closest('[data-ag]');
    return 'edit:' + (row ? row.getAttribute('data-ag') : '');
  }
  function agSnapshot() {
    var box = agendaBox();
    if (!box) return null;
    var s = {vals: {}, open: {}, focus: '', sel: null};
    box.querySelectorAll('input[name], select[name], textarea[name]').forEach(function (el) {
      if (el.type === 'file') return;
      var mine = (el.type === 'checkbox' || el.type === 'radio') ? el.checked !== el.defaultChecked
        : el.value !== el.defaultValue;
      if (mine) s.vals[agKey(el)] = (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
    });
    box.querySelectorAll('[data-ag-edit], [data-ag-subadd], [data-ag-send]').forEach(function (f) {
      if (!f.hidden) s.open[agPanel(f)] = 1;
    });
    var a = document.activeElement;
    if (a && box.contains(a) && a.name) {
      s.focus = agKey(a);
      try { s.sel = [a.selectionStart, a.selectionEnd]; } catch (err) { s.sel = null; }
    }
    return s;
  }
  function agRestore(s) {
    var box = agendaBox();
    if (!box || !s) return;
    box.querySelectorAll('[data-ag-edit], [data-ag-subadd], [data-ag-send]').forEach(function (f) {
      if (s.open[agPanel(f)]) f.hidden = false;
    });
    box.querySelectorAll('input[name], select[name], textarea[name]').forEach(function (el) {
      var k = agKey(el);
      if (k in s.vals) {
        if (el.type === 'checkbox' || el.type === 'radio') el.checked = s.vals[k]; else el.value = s.vals[k];
      }
      if (s.focus && k === s.focus) {
        el.focus();
        if (s.sel) { try { el.setSelectionRange(s.sel[0], s.sel[1]); } catch (err) { /* select, checkbox */ } }
      }
    });
  }
  function trMark() {
    var b = document.querySelector('#agenda [data-ag-trbusy]');
    if (b) b.hidden = !trBusy;
  }
  function autoTranslate(force) {
    var box = agendaBox();
    if (!box || !box.getAttribute('data-ag-tr')) return;
    if (!force && !(+box.getAttribute('data-ag-tr-missing') > 0)) return;
    if (trBusy) { trAgain = true; return; }
    trBusy = true;
    trMark();
    agendaSend({action: 'translate'}).then(function () {
      trBusy = false;
      trMark();
      if (trAgain) { trAgain = false; autoTranslate(); }
    });
  }
  function agendaSend(body, focusAdd) {
    var box = agendaBox();
    if (!box) return Promise.resolve(null);
    var seq = ++agSeq, quiet = body.action === 'translate';
    if (!quiet) box.classList.add('mt-ag-busy');
    return post(box.getAttribute('data-mt-agenda'), body).then(function (j) {
      var cur = agendaBox();
      if (cur && !quiet) cur.classList.remove('mt-ag-busy');
      if (!j || !j.ok) { toast('⚠️ ' + (quiet ? '🌐 ' : '') + ((j && j.error) || 'Не вдалося')); return j; }
      if (seq < agShown) return j;              // новіша відповідь уже на сторінці
      agShown = seq;
      var keep = quiet ? agSnapshot() : null;
      if (cur) cur.outerHTML = j.html;
      if (keep) agRestore(keep);
      trMark();
      if (focusAdd && body.parent) {
        openSub(body.parent);                     // підпункти — один за одним, поле лишається відкритим
      } else if (focusAdd) {
        var input = document.querySelector('#agenda > [data-ag-add] input[name=text]');
        if (input) input.focus();
      }
      if (AUTO_TR[body.action]) autoTranslate();
      return j;
    });
  }
  // 📎 файли агенди: вибір на ПК (📎 у пункті чи «виберіть на ПК»), перетягування на агенду чи пункт, Ctrl+V скріншот
  function agendaUpload(list, item) {
    var box = agendaBox();
    if (!box || !list || !list.length) return;
    var fd = new FormData(), stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
    Array.prototype.forEach.call(list, function (f, k) {
      var name = f.name && !/^image\.(png|jpe?g|gif|webp)$/i.test(f.name) ? f.name
        : 'screenshot-' + stamp + (k ? '-' + (k + 1) : '') + '.' + ((f.type || 'image/png').split('/')[1] || 'png').replace('jpeg', 'jpg');
      fd.append('file', f, name);
    });
    fd.append('item', item || '');
    box.classList.add('mt-ag-busy');
    toast('📎 Завантажую…');
    fetch(box.getAttribute('data-ag-upload'), {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf(), 'Accept': 'application/json'}, body: fd})
      .then(function (r) { return r.json().catch(function () { return {ok: false, error: 'HTTP ' + r.status}; }); },
            function () { return {ok: false, error: 'Сервер не відповідає — спробуйте ще раз'}; })
      .then(function (j) {
        var cur = agendaBox();
        if (cur) cur.classList.remove('mt-ag-busy');
        if (j && j.html && cur) cur.outerHTML = j.html;
        if (!j || !j.ok || j.error) toast('⚠️ ' + ((j && j.error) || 'Не вдалося'));
        else toast('📎 Додано: ' + j.added);
      });
  }
  root.addEventListener('dragover', function (e) {
    var zone = e.target.closest && e.target.closest('#agenda [data-ag-drop], #agenda [data-ag]');
    if (!zone || !e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') < 0) return;
    e.preventDefault();
    zone.classList.add('mt-ag-over');
  });
  root.addEventListener('dragleave', function (e) {
    var zone = e.target.closest && e.target.closest('.mt-ag-over');
    if (zone) zone.classList.remove('mt-ag-over');
  });
  root.addEventListener('drop', function (e) {
    var zone = e.target.closest && e.target.closest('#agenda [data-ag-drop], #agenda [data-ag]');
    if (!zone || !e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    zone.classList.remove('mt-ag-over');
    agendaUpload(e.dataTransfer.files, zone.getAttribute('data-ag') || '');
  });
  document.addEventListener('paste', function (e) {
    var box = agendaBox(), data = e.clipboardData;
    if (!box || !box.querySelector('[data-ag-file]') || !data || !data.files || !data.files.length) return;
    var a = document.activeElement, inAgenda = a && box.contains(a);
    if (!inAgenda && a && a !== document.body && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;   // чуже поле
    if (!inAgenda && a && a.isContentEditable) return;
    e.preventDefault();
    var row = inAgenda && a.closest('[data-ag], [data-ag-subadd]');
    agendaUpload(data.files, row ? (row.getAttribute('data-ag') || row.getAttribute('data-ag-subadd')) : '');
  });
  root.addEventListener('change', function (e) {
    if (!e.target.matches || !e.target.matches('#agenda [data-ag-file]')) return;
    agendaUpload(e.target.files, e.target.getAttribute('data-item') || '');
    e.target.value = '';
  });
  function agendaMail(form) {                     // ✉️ лист з агендою: вкладка відкривається одразу (інакше блокує браузер)
    var to = Array.prototype.map.call(form.querySelectorAll('input[name=to]:checked'), function (c) { return c.value; });
    var lang = (form.querySelector('input[name=lang]:checked') || {}).value || 'de';
    var ai = form.elements.ai && form.elements.ai.checked;
    var tab = window.open('about:blank', '_blank');
    var btn = form.querySelector('button[type=submit]'), label = btn.textContent;
    btn.disabled = true;
    btn.textContent = ai ? '⏳ Перекладаю…' : '⏳ Готую лист…';
    var attach = form.elements.attach ? form.elements.attach.checked : false;
    post(agendaBox().getAttribute('data-mt-agenda'), {action: 'send', to: to, extra: form.elements.extra.value,
                                                       lang: lang, ai: ai ? 1 : 0, attach: attach ? 1 : 0}).then(function (j) {
      btn.disabled = false;
      btn.textContent = label;
      if (!j || !j.ok || !j.compose) {
        if (tab) tab.close();
        toast('⚠️ ' + ((j && j.error) || 'Не вдалося'));
        return;
      }
      if (tab) { tab.opener = null; tab.location.href = j.compose; } else { location.href = j.compose; }
      form.hidden = true;
      toast('✉️ Лист з агендою відкрито в новій вкладці — перевірте й надішліть');
    });
  }
  root.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.closest || !form.closest('#agenda')) return;
    e.preventDefault();
    if (form.hasAttribute('data-ag-add')) {
      var text = (form.elements.text.value || '').trim();
      var parent = form.getAttribute('data-parent') || '';
      if (text) agendaSend(parent ? {action: 'add', text: text, parent: parent} : {action: 'add', text: text}, true);
    } else if (form.hasAttribute('data-ag-send')) {
      agendaMail(form);
    } else if (form.hasAttribute('data-ag-edit')) {
      var row = form.closest('[data-ag]');
      agendaSend({action: 'edit', item: row.getAttribute('data-ag'), text: form.elements.text.value,
                  note: form.elements.note.value,
                  tr_text: form.elements.tr_text ? form.elements.tr_text.value : undefined,
                  tr_note: form.elements.tr_note ? form.elements.tr_note.value : undefined});
    }
  });
  root.addEventListener('change', function (e) {
    var t = e.target;
    if (t.matches && t.matches('#agenda [data-ag-send] input[name=lang]')) {
      // мова листа: уже перекладене нею — без AI; чого бракує — галочка «перекласти AI»
      var missing = +t.getAttribute('data-missing') || 0, f = t.form;
      if (f.elements.ai) f.elements.ai.checked = missing > 0;
      var lab = f.querySelector('[data-ag-ai]'), ready = f.querySelector('[data-ag-ready]');
      if (lab) lab.hidden = !missing;
      if (ready) ready.hidden = t.getAttribute('data-ready') !== '1';
      return;
    }
    if (t.matches && t.matches('#agenda [data-ag-trlang]')) {
      agendaSend({action: 'tr_lang', lang: t.value});
      return;
    }
    if (!t.matches || !t.matches('#agenda [data-ag-act=toggle]')) return;
    agendaSend({action: 'status', item: t.closest('[data-ag]').getAttribute('data-ag'), status: t.checked ? 'done' : 'open'});
  });
  root.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('#agenda button[data-ag-act]');
    if (!b) return;
    var act = b.getAttribute('data-ag-act'), row = b.closest('[data-ag]'), id = row ? row.getAttribute('data-ag') : '';
    if (act === 'edit' || act === 'cancel') {
      var form = row && row.querySelector('[data-ag-edit]');
      if (!form) return;
      form.hidden = act === 'cancel';
      if (!form.hidden) form.elements.note.focus();
    } else if (act === 'skip') {
      agendaSend({action: 'status', item: id, status: row.classList.contains('mt-ag-skipped') ? 'open' : 'skipped'});
    } else if (act === 'up' || act === 'down') {
      agendaSend({action: 'move', item: id, delta: act === 'up' ? -1 : 1});
    } else if (act === 'remove') {
      if (b.getAttribute('data-confirm') && !window.confirm(b.getAttribute('data-confirm'))) return;
      agendaSend({action: 'remove', item: id});
    } else if (act === 'sub') {
      openSub(b.getAttribute('data-parent'));
    } else if (act === 'attach') {
      var input = agendaBox().querySelector('[data-ag-file]');
      if (!input) return;
      input.setAttribute('data-item', id || '');
      input.click();
    } else if (act === 'file_remove') {
      agendaSend({action: 'file_remove', file: b.getAttribute('data-file')});
    } else if (act === 'subclose') {
      var sr = b.closest('[data-ag-subadd]');
      if (sr) sr.hidden = true;
    } else if (act === 'send' || act === 'sendclose') {
      var sf = agendaBox().querySelector('[data-ag-send]');
      if (sf) sf.hidden = act === 'sendclose' ? true : !sf.hidden;
      if (sf && !sf.hidden) sf.scrollIntoView({block: 'nearest'});
    } else if (act === 'suggest' || act === 'take_all') {
      agendaSend({action: act});
    } else if (act === 'take') {
      agendaSend({action: 'take', ref: b.getAttribute('data-ref')});
    } else if (act === 'hide') {
      var sug = b.closest('.mt-ag-sug');
      if (sug) sug.remove();
    } else if (act === 'translate') {
      autoTranslate(true);
    } else if (act === 'ai_ok' || act === 'ai_no') {           // 🤖 звірка з розшифровкою (meetings/ai_brief.py)
      agendaSend({action: act, item: id});
    } else if (act === 'ai_all') {
      agendaSend({action: 'ai_all'}).then(function (j) {
        if (j && j.ok) toast('✓ Пунктів агенди оновлено: ' + (j.changed || 0));
      });
    } else if (act === 'tr_suggest') {
      agendaSend({action: 'tr_lang', lang: b.getAttribute('data-lang')});
    } else if (act === 'copy' || act === 'copy_tr') {
      var text = agendaBox().getAttribute(act === 'copy' ? 'data-ag-copy' : 'data-ag-copy-tr') || '';
      var ok = function () { toast('📋 Агенду скопійовано — вставте в запрошення чи лист'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok, function () { window.prompt('Скопіюйте агенду:', text); });
      } else { window.prompt('Скопіюйте агенду:', text); }
    }
  });

  // ── опитування стану ──
  var cfgEl = document.getElementById('mt-config');
  var cfg = null;
  try { cfg = cfgEl ? JSON.parse(cfgEl.textContent) : null; } catch (err) { cfg = null; }
  if (!cfg || !cfg.poll || !cfg.status_url) return;

  var POLL = 5000, failures = 0, stopped = false;
  var banner = document.getElementById('mt-new');

  function busy() {
    if (trBusy) return true;
    if (document.querySelector('#mt-items .mt-panel:not([hidden])')) return true;
    if (document.querySelector('dialog[open]')) return true;
    if (document.querySelector('details.mt-edit[open]')) return true;
    if (document.querySelector('#agenda [data-ag-edit]:not([hidden]), #agenda .mt-ag-sug, #agenda [data-ag-send]:not([hidden]), '
                               + '#agenda [data-ag-subadd]:not([hidden])')) return true;
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
