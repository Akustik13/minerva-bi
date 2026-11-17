/* 🤖 AI-заповнення картки з даташита, фото, посилань і тексту (inventory/services/ai_fill.py) — одна панель для
   картки товару, «🔷 На DigiKey зі складу» і картки лістингу DigiKey.

   MVAiFill.mount(host, cfg, adapter): cfg — з сервера (ai_fill.config: target, start_url, status_url, ready, …),
   adapter — сторінка: read(key) / readAttr(key) — що зараз у формі, apply({fields, attrs, files}, sources) →
   скільки вставлено, необов'язково: open (розгорнуто), key (запам'ятати останній результат), extra() →
   {product_id, listing_id}, files {pdf: 'назва поля', image: 'назва поля'} + canFile(kind), skip — ключі полів,
   яких на сторінці немає, applyLabel — напис кнопки.
   Нічого не зберігає: записує кнопка «Зберегти» форми. Поля панелі — без name (у форму не потрапляють). */
(function () {
  'use strict';
  var CONF = { high: '', medium: 'ймовірно', low: 'перевірте' };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function csrf() {               // токен форми сторінки, інакше саме cookie «csrftoken» (не «xx_csrftoken»)
    var i = document.querySelector('[name=csrfmiddlewaretoken]');
    if (i && i.value) return i.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }
  function norm(v) { return String(v == null ? '' : v).replace(/\s+/g, ' ').trim().toLowerCase(); }
  function store(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function sstore(k, v) { try { if (v == null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) {} }
  function sload(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function kb(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + ' МБ' : Math.max(1, Math.round(n / 1024)) + ' КБ'; }
  function money(n) { return '$' + (Number(n) || 0).toFixed(Number(n) < 0.1 ? 3 : 2); }
  function kindOf(f) {
    var t = (f.type || '').toLowerCase(), n = (f.name || '').toLowerCase();
    if (t === 'application/pdf' || /\.pdf$/.test(n)) return 'pdf';
    if (/^image\//.test(t) || /\.(jpe?g|png|gif|webp)$/.test(n)) return 'image';
    return 'other';
  }

  function mount(host, cfg, ad) {
    if (!host || !cfg) return null;
    ad = ad || {};
    var files = [], job = null, timer = null, ticker = null, result = null;
    var skey = ad.key ? 'mvaf:' + ad.key : '';
    var models = (cfg.models || []).map(function (m) {
      return '<option value="' + esc(m[0]) + '">' + esc(m[1]) + '</option>';
    }).join('');
    var root = document.createElement('details');
    root.className = 'mvaf';
    if (ad.open) root.open = true;
    root.innerHTML =
      '<summary><span class="mvaf-t">🤖 AI: заповнити з даташита, посилань і тексту</span>' +
        '<span class="mvaf-sub">— завантажте, що маєте; AI знайде решту в інтернеті й заповнить поля (перевірите перед «Зберегти»)</span></summary>' +
      '<div class="mvaf-body">' +
        (cfg.ready ? '' : '<div class="mvaf-warn">Немає ключа AI — додайте його в <a href="/admin/strategy/aisettings/">⚙️ Налаштування AI</a>.</div>') +
        '<div class="mvaf-grid">' +
          '<label class="mvaf-l">Партномер / артикул<input type="text" class="mvaf-pn" autocomplete="off" placeholder="напр. AN220213-01C або партномер виробника"></label>' +
          '<label class="mvaf-l">Посилання — по одному в рядку<textarea class="mvaf-links" rows="2" placeholder="сторінка виробника, даташит, DigiKey, Mouser…"></textarea></label>' +
        '</div>' +
        '<div class="mvaf-drop" tabindex="0" role="button" aria-label="Додати файли">' +
          '<b>📎 Перетягніть сюди</b> даташит (PDF), фото, таблицю (XLSX, CSV), DOCX чи TXT · або <u>виберіть файли</u> · Ctrl+V — вставити фото' +
          '<input type="file" class="mvaf-file" multiple hidden accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.txt,.csv,.tsv,.md,.html,.htm,.json,.xml,.docx,.xlsx,.xlsm">' +
        '</div>' +
        '<ul class="mvaf-files"></ul>' +
        '<label class="mvaf-l">Що знаєте про товар (необов\'язково)<textarea class="mvaf-text" rows="3" placeholder="Опис, параметри, текст з листа чи сайту, нотатки — як є, без форми"></textarea></label>' +
        '<div class="mvaf-opts">' +
          '<label><input type="checkbox" class="mvaf-web"> 🌐 Шукати й читати в інтернеті</label>' +
          (cfg.has_datasheet ? '<label><input type="checkbox" class="mvaf-ds" checked> 📄 Даташит із картки товару</label>' : '') +
          '<label>Модель <select class="mvaf-model">' + models + '</select></label>' +
          '<button type="button" class="mvaf-go">🤖 Згенерувати</button>' +
        '</div>' +
        '<div class="mvaf-progress" hidden></div>' +
        '<div class="mvaf-result" hidden></div>' +
      '</div>';
    host.appendChild(root);
    var $ = function (c) { return root.querySelector(c); };
    var web = $('.mvaf-web'), model = $('.mvaf-model'), go = $('.mvaf-go');
    web.checked = load('mvaf-web') !== '0';
    model.value = load('mvaf-model') || cfg.default_model || '';
    if (!model.value && model.options.length) model.selectedIndex = 0;
    web.addEventListener('change', function () { store('mvaf-web', web.checked ? '1' : '0'); });
    model.addEventListener('change', function () { store('mvaf-model', model.value); });
    if (!cfg.ready || cfg.can === false) go.disabled = true;

    // Enter у полях панелі не відправляє форму сторінки; у «Партномер» — запускає «🤖 Згенерувати»
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.target.tagName !== 'INPUT') return;
      e.preventDefault();
      if (e.target.classList.contains('mvaf-pn') && !go.disabled) startJob();
    });

    // ── Файли ──
    function addFiles(list) {
      Array.prototype.forEach.call(list || [], function (f) {
        if (files.length >= (cfg.files_max || 8)) return;
        if (f.size > (cfg.file_mb || 20) * 1048576) { alert('«' + f.name + '» більший за ' + cfg.file_mb + ' МБ'); return; }
        if (files.some(function (x) { return x.name === f.name && x.size === f.size; })) return;
        files.push(f);
      });
      drawFiles();
    }
    function drawFiles() {
      var ul = $('.mvaf-files');
      ul.innerHTML = files.map(function (f, i) {
        var k = kindOf(f);
        return '<li>' + (k === 'pdf' ? '📄' : k === 'image' ? '🖼' : '📃') + ' <span>' + esc(f.name) + '</span> <small>' +
          kb(f.size) + '</small> <button type="button" data-i="' + i + '" title="Прибрати">✕</button></li>';
      }).join('');
    }
    $('.mvaf-files').addEventListener('click', function (e) {
      var b = e.target.closest('button[data-i]');
      if (b) { files.splice(+b.dataset.i, 1); drawFiles(); }
    });
    var drop = $('.mvaf-drop'), input = $('.mvaf-file');
    drop.addEventListener('click', function (e) { if (e.target !== input) input.click(); });
    drop.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
    input.addEventListener('change', function () { addFiles(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach(function (t) {
      root.addEventListener(t, function (e) {
        if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) {
          e.preventDefault(); drop.classList.add('is-over');
        }
      });
    });
    ['dragleave', 'drop'].forEach(function (t) {
      root.addEventListener(t, function () { drop.classList.remove('is-over'); });
    });
    root.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); }
    });
    root.addEventListener('paste', function (e) {
      var items = (e.clipboardData && e.clipboardData.files) || [];
      if (items.length) { e.preventDefault(); addFiles(items); }
    });

    // ── Запуск і хід ──
    function progress(html) { var p = $('.mvaf-progress'); p.hidden = !html; p.innerHTML = html || ''; }
    function stopTimers() { clearTimeout(timer); clearInterval(ticker); timer = ticker = null; }
    function startJob() {
      var fd = new FormData(), extra = ad.extra ? ad.extra() || {} : {};
      files.forEach(function (f) { fd.append('files', f, f.name); });
      fd.append('pn', $('.mvaf-pn').value);
      fd.append('links', $('.mvaf-links').value);
      fd.append('text', $('.mvaf-text').value);
      fd.append('web', web.checked ? '1' : '0');
      fd.append('model', model.value);
      fd.append('datasheet', $('.mvaf-ds') && $('.mvaf-ds').checked ? '1' : '0');
      var pid = extra.product_id != null ? extra.product_id : cfg.product_id;
      if (pid) fd.append('product_id', pid);
      if (extra.listing_id) fd.append('listing_id', extra.listing_id);
      go.disabled = true;
      $('.mvaf-result').hidden = true;
      progress('<div class="mvaf-steps"><div>⏳ Надсилаю дані…</div></div>');
      fetch(cfg.start_url, { method: 'POST', body: fd, credentials: 'same-origin',
                             headers: { 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); })
        .then(function (d) {
          if (!d.ok) { go.disabled = false; progress('<div class="mvaf-err">❌ ' + esc(d.error || 'Помилка') + '</div>'); return; }
          follow(d);
        })
        .catch(function (e) { go.disabled = false; progress('<div class="mvaf-err">❌ ' + esc(e.message) + '</div>'); });
    }
    function url(id) { return cfg.status_url.replace('JOB', encodeURIComponent(id)); }
    function follow(d) {
      job = d.id;
      if (skey) sstore(skey, job);
      var t0 = Date.now() - (d.elapsed || 0) * 1000;
      stopTimers();
      ticker = setInterval(function () {
        var s = root.querySelector('.mvaf-elapsed');
        if (s) s.textContent = Math.round((Date.now() - t0) / 1000) + ' с';
      }, 1000);
      show(d);
    }
    function show(d) {
      if (d.status === 'running') {
        var steps = (d.steps || []).slice(-8).map(function (s) {
          return '<div><small>' + esc(s.at) + '</small> ' + esc(s.text) + '</div>';
        }).join('') || '<div>⏳ Починаю…</div>';
        progress('<div class="mvaf-steps">' + steps + '</div><div class="mvaf-run">⏳ ' + esc(d.model_label || 'AI') +
          ' працює · <span class="mvaf-elapsed"></span> · ' + money(d.cost_usd) +
          ' <button type="button" class="mvaf-stop">⏹ Зупинити</button></div>');
        timer = setTimeout(poll, 2000);
        return;
      }
      stopTimers();
      go.disabled = !cfg.ready;
      if (d.status === 'error') {
        progress('<div class="mvaf-err">❌ ' + esc(d.error) + '</div>' + costLine(d));
        if (skey) sstore(skey, null);
        return;
      }
      progress('');
      render(d);
    }
    function poll() {
      fetch(url(job), { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d.ok) show(d); else { stopTimers(); go.disabled = false; progress('<div class="mvaf-err">❌ ' + esc(d.error) + '</div>'); } })
        .catch(function () { timer = setTimeout(poll, 4000); });
    }
    root.addEventListener('click', function (e) {
      if (!e.target.closest('.mvaf-stop') || !job) return;
      var fd = new FormData(); fd.append('action', 'cancel');
      fetch(url(job), { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() } });
    });
    go.addEventListener('click', startJob);

    function costLine(d) {
      return '<div class="mvaf-cost">' + esc(d.model_label || '') + ' · ' + (d.searches || 0) + ' пошук. · ' +
        (d.fetches || 0) + ' сторін. · ' + money(d.cost_usd) + ' · ' + (d.elapsed || 0) + ' с</div>';
    }

    // ── Результат: «зараз → AI пропонує», галочки, «Вставити» ──
    function row(kind, item, now) {
      var same = norm(now) === norm(item.value), empty = !norm(now);
      var conf = CONF[item.confidence] ? ' <span class="mvaf-conf c-' + item.confidence + '">' + CONF[item.confidence] + '</span>' : '';
      return '<tr class="' + (same ? 'is-same' : '') + '"><td><input type="checkbox" data-kind="' + kind + '" data-key="' +
        esc(item.key) + '"' + (same ? ' disabled' : empty ? ' checked' : '') + '></td>' +
        '<td>' + esc(item.label) + (item.required ? ' <span class="mvaf-req">★</span>' : '') + '</td>' +
        '<td class="mvaf-now">' + (empty ? '<i>порожньо</i>' : esc(String(now).slice(0, 160))) + '</td>' +
        '<td class="mvaf-new">' + (same ? '= так само' : esc(item.display || item.value)) + conf + '</td>' +
        '<td class="mvaf-src">' + esc(item.source || '') + '</td></tr>';
    }
    function render(d, restoring) {
      var r = d.result || {}, box = $('.mvaf-result'), byKey = {}, skip = ad.skip || [];
      result = r;
      var fields = (r.fields || []).filter(function (f) { return !f.follow && skip.indexOf(f.key) < 0; });
      (r.fields || []).forEach(function (f) { byKey['f:' + f.key] = f; });
      (r.attributes || []).forEach(function (a) { byKey['a:' + a.key] = a; });
      var html = '<div class="mvaf-head"><b>🤖 AI пропонує</b> <span class="mvaf-muted">— галочка = вставити у форму; що вже заповнено, ' +
        'за замовчуванням не чіпаємо</span> <button type="button" class="mvaf-hide" title="Сховати">✕</button></div>';
      if (r.summary) html += '<p class="mvaf-sum">' + esc(r.summary) + '</p>';
      if (r.exists) html += '<div class="mvaf-warn">⚠ Товар з артикулом <b>' + esc(r.exists.sku) + '</b> уже є на складі — <a href="' +
        esc(r.exists.url) + '" target="_blank" rel="noopener">відкрити →</a></div>';
      if (fields.length) {
        html += '<table class="mvaf-table"><thead><tr><th></th><th>Поле</th><th>Зараз</th><th>AI пропонує</th><th>Звідки</th></tr></thead><tbody>' +
          fields.map(function (f) { return row('f', f, ad.read ? ad.read(f.key) : ''); }).join('') + '</tbody></table>';
      }
      if ((r.attributes || []).length) {
        html += '<div class="mvaf-sec">📡 Параметри (' + r.attributes.length + ')</div><table class="mvaf-table"><thead><tr><th></th>' +
          '<th>Параметр</th><th>Зараз</th><th>AI пропонує</th><th>Звідки</th></tr></thead><tbody>' +
          r.attributes.map(function (a) { return row('a', a, ad.readAttr ? ad.readAttr(a.key) : ''); }).join('') + '</tbody></table>';
      }
      var fileRows = [];
      if (ad.files) {
        ['pdf', 'image'].forEach(function (k) {
          var f = files.filter(function (x) { return kindOf(x) === k; })[0];
          if (f && ad.files[k] && (!ad.canFile || ad.canFile(k))) {
            fileRows.push('<label class="mvaf-fl"><input type="checkbox" data-kind="file" data-key="' + k + '" checked> 📎 «' +
              esc(f.name) + '» → поле «' + esc(ad.files[k]) + '»</label>');
          }
        });
      }
      if (fileRows.length) html += '<div class="mvaf-sec">📎 Файли в картку</div>' + fileRows.join('');
      if ((r.links || []).length) {
        html += '<div class="mvaf-sec">🔗 Прочитані сторінки</div><ul class="mvaf-links-l">' + r.links.map(function (l) {
          return /^https?:\/\//.test(l.url) ? '<li><a href="' + esc(l.url) + '" target="_blank" rel="noopener noreferrer">' + esc(l.title) + '</a></li>' : '';
        }).join('') + '</ul>';
      }
      var notes = (r.doubts || []).map(function (x) { return '❔ ' + esc(x); })
        .concat((r.dropped || []).map(function (x) { return '⛔ ' + esc(x); }))
        .concat((r.unread || []).map(function (x) { return '🔗 ' + esc(x.error); }));
      if (notes.length) html += '<div class="mvaf-sec">Варто перевірити</div><ul class="mvaf-notes">' + notes.map(function (n) { return '<li>' + n + '</li>'; }).join('') + '</ul>';
      if (!fields.length && !(r.attributes || []).length) html += '<p class="mvaf-muted">AI нічого не знайшов для полів — додайте даташит чи посилання.</p>';
      html += '<div class="mvaf-foot"><button type="button" class="mvaf-apply default">✓ ' + esc(ad.applyLabel || 'Вставити вибране у форму') + '</button>' +
        '<button type="button" class="mvaf-all">Усі</button><button type="button" class="mvaf-none">Нічого</button>' +
        '<span class="mvaf-done"></span>' + costLine(d) + '</div>';
      box.innerHTML = html;
      box.hidden = false;
      if (!restoring) root.open = true;
      box._byKey = byKey;
    }
    $('.mvaf-result').addEventListener('click', function (e) {
      var box = $('.mvaf-result');
      if (e.target.closest('.mvaf-hide')) { box.hidden = true; if (skey) sstore(skey, null); return; }
      if (e.target.closest('.mvaf-all') || e.target.closest('.mvaf-none')) {
        var on = !!e.target.closest('.mvaf-all');
        box.querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(function (c) { c.checked = on; });
        return;
      }
      if (!e.target.closest('.mvaf-apply')) return;
      var out = { fields: {}, attrs: {}, files: {} }, sources = {}, byKey = box._byKey || {};
      box.querySelectorAll('input[type=checkbox]:checked').forEach(function (c) {
        var k = c.dataset.key, kind = c.dataset.kind;
        if (kind === 'file') {
          out.files[k] = files.filter(function (x) { return kindOf(x) === k; })[0];
          return;
        }
        var item = byKey[kind + ':' + k];
        if (!item) return;
        (kind === 'f' ? out.fields : out.attrs)[k] = item.value;
        sources[kind + ':' + k] = item.source || '';
        (result.fields || []).forEach(function (f) {      // назва категорії йде разом з її ID
          if (kind === 'f' && f.follow === k) out.fields[f.key] = f.value;
        });
      });
      var n = ad.apply ? ad.apply(out, sources) : 0;
      var done = box.querySelector('.mvaf-done');
      if (done) done.textContent = n ? '✓ Вставлено: ' + n + ' — перевірте підсвічені поля й натисніть «Зберегти»' : 'Нічого не вибрано';
    });

    // ── Повернення після перезавантаження: останній результат цієї сторінки ──
    var saved = skey && sload(skey);
    if (saved) {
      job = saved;
      fetch(url(saved), { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { sstore(skey, null); return; }
          if (d.status === 'running') { go.disabled = true; root.open = true; follow(d); }
          else if (d.status === 'done') render(d, true);
        })
        .catch(function () {});
    }
    return { root: root };
  }

  /* Підсвітити поле, яке вставив AI. */
  function mark(el) {
    if (!el) return;
    var row = el.closest('.form-row') || el.closest('.fs-field') || el.closest('div') || el;
    row.classList.remove('mvaf-hit'); void row.offsetWidth; row.classList.add('mvaf-hit');
  }
  /* Значення в поле форми: select — додати варіант, якого немає; подія input / change — для залежних скриптів. */
  function setValue(el, value) {
    if (!el) return false;
    if (el.tagName === 'SELECT') {
      var has = Array.prototype.some.call(el.options, function (o) { return o.value === value; });
      if (!has) { var o = document.createElement('option'); o.value = value; o.textContent = value; el.appendChild(o); }
    }
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    mark(el);
    return true;
  }
  /* Файл з панелі — у поле файлу форми (DataTransfer). */
  function setFile(input, file) {
    if (!input || !file || typeof DataTransfer === 'undefined') return false;
    try {
      var dt = new DataTransfer(); dt.items.add(file); input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      mark(input);
      return true;
    } catch (e) { return false; }
  }

  window.MVAiFill = { mount: mount, setValue: setValue, setFile: setFile, mark: mark };
})();
