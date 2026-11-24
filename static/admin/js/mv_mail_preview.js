/* «👁 Приклад» листа в таблиці «Хто що кому шле» (картка магазину, shop/mail_map.py): кнопка .mm-pv[data-letter]
   → POST усієї форми картки (незбережені значення таблиці теж) на #mm-pv-root[data-url] (shop/mail_preview.py) →
   вікно: хто надсилає, від кого, кому, копія, прихована копія, відповіді, тема, вкладення, текст. Усе — textContent,
   лист Minerva (HTML) — в iframe sandbox без скриптів. Нічого не зберігає й не надсилає. */
(function () {
  'use strict';
  if (window.__mvMailPreview) return;
  window.__mvMailPreview = true;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function list(v, empty) { return v && v.length ? v.join(', ') : empty; }

  var dlg, head, body, state = {};

  function dialog() {
    if (dlg) return dlg;
    dlg = el('dialog');
    dlg.id = 'mm-pv-dlg';
    head = el('div', 'pv-head');
    body = el('div', 'pv-body');
    dlg.appendChild(head);
    dlg.appendChild(body);
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });   // клік поза вікном
    document.body.appendChild(dlg);
    return dlg;
  }

  function select(label, options, value, key) {
    var wrap = el('label');
    wrap.appendChild(document.createTextNode(label + ' '));
    var s = el('select');
    options.forEach(function (o) {
      var opt = new Option(o[1], o[0]);
      if (o[0] === value) opt.selected = true;
      s.appendChild(opt);
    });
    s.addEventListener('change', function () { state[key] = s.value; load(); });
    wrap.appendChild(s);
    return wrap;
  }

  function row(table, label, value, plain, extra) {
    var tr = el('tr');
    tr.appendChild(el('th', '', label));
    var td = el('td');
    var v = el('span', '', value);
    if (plain) v.setAttribute('translate', 'no');   // дані (адреси, тема) — без перекладу інтерфейсу
    td.appendChild(v);
    if (extra) td.appendChild(el('span', 'nt-when', ' ' + extra));
    tr.appendChild(td);
    table.appendChild(tr);
  }

  function show(d) {
    head.textContent = '';
    head.appendChild(el('b', '', '👁 ' + d.title));
    var chip = el('span', 'pv-chip ' + (d.active ? 'pv-on' : 'pv-off'));
    chip.appendChild(el('span', '', d.active ? '✓ надсилається' : '✗ зараз не надсилається'));
    if (!d.active && d.inactive_reason) chip.appendChild(el('span', '', ' · ' + d.inactive_reason));
    head.appendChild(chip);
    var x = el('button', 'button', '✕');
    x.type = 'button';
    x.title = 'Закрити';
    x.addEventListener('click', function () { dlg.close(); });
    head.appendChild(x);

    body.textContent = '';
    body.appendChild(el('div', 'nt-when', d.when));
    var tools = el('div', 'pv-tools');
    if (d.variants && d.variants.length) tools.appendChild(select('Лист:', d.variants, d.variant, 'variant'));
    if (d.langs && d.langs.length) tools.appendChild(select('Мова:', d.langs, d.lang, 'lang'));
    if (d.methods && d.methods.length) tools.appendChild(select('Спосіб оплати:', d.methods, d.method, 'method'));
    if (tools.childNodes.length) body.appendChild(tools);

    var t = el('table');
    row(t, 'Хто надсилає', d.who);
    row(t, 'Від', d.from, true);
    row(t, 'Кому', list(d.to, '—'), true, d.to_example && d.to && d.to.length ? '(приклад покупця)' : '');
    row(t, 'Копія (CC)', list(d.cc, 'без копії'), true);
    if (d.bcc_allowed) row(t, 'Прихована копія (BCC)', list(d.bcc, 'без прихованої копії'), true);
    row(t, 'Відповіді (Reply-To)', d.reply_to || 'на адресу відправника', true);
    if (d.telegram) row(t, '💬 Telegram', d.telegram, true);
    row(t, 'Тема', d.subject || '—', true);
    if (d.attachments && d.attachments.length) row(t, 'Вкладення', '📎 ' + d.attachments.join(', '), true);
    body.appendChild(t);
    (d.notes || []).forEach(function (n) { body.appendChild(el('div', 'pv-note', n)); });

    if (d.html) {
      var f = el('iframe');
      f.setAttribute('sandbox', '');   // без скриптів, форм і переходів
      f.setAttribute('title', 'Лист');
      f.srcdoc = d.html;
      body.appendChild(f);
      var det = el('details');
      det.appendChild(el('summary', '', 'Текстова версія листа'));
      var pre = el('pre', '', d.text);
      pre.setAttribute('translate', 'no');
      det.appendChild(pre);
      body.appendChild(det);
    } else if (d.text) {
      var p = el('pre', '', d.text);
      p.setAttribute('translate', 'no');
      body.appendChild(p);
    }
  }

  function load() {
    var root = document.getElementById('mm-pv-root');
    var form = root && root.closest('form');
    if (!root || !form) return;
    var fd = new FormData(form);
    fd.set('pv_letter', state.letter || '');
    fd.set('pv_lang', state.lang || '');
    fd.set('pv_method', state.method || '');
    fd.set('pv_variant', state.variant || '');
    dialog();
    if (!dlg.open) dlg.showModal();
    body.textContent = 'Завантаження…';
    fetch(root.getAttribute('data-url'), { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }); })
      .then(function (d) {
        if (!d.ok) { body.textContent = d.error || 'Не вдалося показати лист.'; return; }
        state.lang = d.lang; state.method = d.method; state.variant = d.variant;
        show(d);
      })
      .catch(function () { body.textContent = 'Немає відповіді від Minerva.'; });
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.mm-pv');
    if (!b) return;
    e.preventDefault();
    state = { letter: b.getAttribute('data-letter') || '' };
    load();
  });
})();
