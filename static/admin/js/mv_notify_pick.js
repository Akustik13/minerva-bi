/* Таблиці «Хто що отримує» (dashboard/notify_widgets.py): адреси й Telegram-чати чипами, «＋ з Minerva»,
   своя адреса; прапорці. Поле з data-sync пише значення в поле картки з таким id і читає його назад.
   [data-nt-go] — перейти до поля на сторінці (розгорнути згорнутий блок, підсвітити). */
(function () {
  'use strict';
  var EMAIL = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;
  var CHAT = /^(-?\d{3,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$/;
  var BLOCK = /^(@[^\s@,;<>]+\.[^\s@,;<>]+|[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+)$/;
  var RULE = { email: EMAIL, tg: CHAT, block: BLOCK };
  var opts = { email: [], tg: [] };
  try { opts = JSON.parse(document.getElementById('nt-opts').textContent) || opts; } catch (e) { /* без списків */ }

  function split(v) {
    var out = [];
    String(v || '').split(/[,;\s]+/).forEach(function (x) {
      x = x.trim();
      if (x && out.map(function (o) { return o.toLowerCase(); }).indexOf(x.toLowerCase()) < 0) out.push(x);
    });
    return out;
  }
  function labelOf(kind, v) {
    var list = opts[kind] || [];
    for (var i = 0; i < list.length; i++) if (String(list[i][0]).toLowerCase() === String(v).toLowerCase()) return list[i][1];
    return '';
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function target(node) {
    var id = node.getAttribute('data-sync');
    return id ? document.getElementById(id) : null;
  }

  function setupPick(box) {
    var input = box.querySelector('.nt-in');
    var kind = RULE[box.getAttribute('data-kind')] ? box.getAttribute('data-kind') : 'email';
    var real = target(input);
    if (input.hasAttribute('data-sync') && !real) return;   // поля картки немає — лишається вимкненим
    input.disabled = false;
    if (real) input.value = real.value;
    input.style.display = 'none';
    var chips = el('div', 'nt-chips');
    var tools = el('div', 'nt-tools');
    var sel = el('select');
    sel.appendChild(new Option(kind === 'tg' ? '＋ чат з Minerva…' : '＋ адреса з Minerva…', ''));
    (opts[kind] || []).forEach(function (o) { sel.appendChild(new Option(o[1] + ' — ' + o[0], o[0])); });
    var own = el('input');
    own.type = 'text';
    own.placeholder = kind === 'tg' ? '＋ свій Chat ID, Enter' : (kind === 'block' ? '＋ адреса або @домен, Enter' : '＋ своя адреса, Enter');
    own.setAttribute('autocomplete', 'off');
    if (sel.options.length > 1) tools.appendChild(sel);
    tools.appendChild(own);
    box.insertBefore(chips, input.nextSibling);
    box.insertBefore(tools, chips.nextSibling);

    function save(list) {
      input.value = list.join(', ');
      if (real) {
        real.value = input.value;
        real.dispatchEvent(new Event('input', { bubbles: true }));
        real.dispatchEvent(new Event('change', { bubbles: true }));
      }
      draw();
    }
    function draw() {
      chips.textContent = '';
      var list = split(input.value);
      if (!list.length) {
        chips.appendChild(el('span', 'nt-empty', box.getAttribute('data-empty') || '—'));
        return;
      }
      list.forEach(function (v) {
        var chip = el('span', 'nt-chip');
        var name = labelOf(kind, v);
        if (name) { chip.appendChild(el('span', '', name)); chip.appendChild(el('small', '', v)); }
        else chip.appendChild(el('span', '', v));
        chip.setAttribute('translate', 'no');
        var x = el('button', '', '✕');
        x.type = 'button';
        x.title = 'Прибрати';
        x.addEventListener('click', function () {
          save(split(input.value).filter(function (o) { return o.toLowerCase() !== v.toLowerCase(); }));
        });
        chip.appendChild(x);
        chips.appendChild(chip);
      });
    }
    function add(v) {
      v = String(v || '').trim().replace(/^<|>$/g, '');
      if (!v) return true;
      if (!RULE[kind].test(v)) return false;
      var list = split(input.value);
      if (list.map(function (o) { return o.toLowerCase(); }).indexOf(v.toLowerCase()) < 0) list.push(v);
      save(list);
      return true;
    }
    sel.addEventListener('change', function () { add(sel.value); sel.value = ''; });
    function commit() {
      var bad = split(own.value).filter(function (v) { return !add(v); });
      own.value = bad.join(', ');
      own.classList.toggle('nt-bad', bad.length > 0);
      own.title = bad.length ? (kind === 'tg' ? 'Не схоже на Chat ID: ' : 'Не схоже на email: ') + bad.join(', ') : '';
    }
    own.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ',' || e.key === ';') { e.preventDefault(); commit(); }
    });
    own.addEventListener('blur', commit);
    if (real) real.addEventListener('input', function () { if (input.value !== real.value) { input.value = real.value; draw(); } });
    draw();
  }

  function setupCheck(cb) {
    var real = target(cb);
    if (!real) return;
    cb.disabled = false;
    cb.checked = real.checked;
    cb.addEventListener('change', function () {
      real.checked = cb.checked;
      real.dispatchEvent(new Event('change', { bubbles: true }));
      sync();
    });
    real.addEventListener('change', function () { sync(); });
  }
  function sync() {   // кілька прапорців таблиці на одне поле (звіти) і притушені рядки
    document.querySelectorAll('.nt-chk input[data-sync]').forEach(function (cb) {
      var real = target(cb);
      if (real && cb.checked !== real.checked) cb.checked = real.checked;
    });
    document.querySelectorAll('tr[data-nt-row]').forEach(function (tr) {   // вимкнено або жодного каналу
      function none(sel) {
        var all = tr.querySelectorAll(sel);
        return all.length > 0 && tr.querySelectorAll(sel.replace(/,/g, ':checked,') + ':checked').length === 0;
      }
      tr.classList.toggle('nt-off', none('.nt-chk input[data-nt-main]') ||
        none('.nt-chk input:not([data-nt-main]):not([data-nt-aux])'));
    });
  }

  function go(name) {
    var r = document.querySelector('.fieldBox.field-' + name + ', .form-row.field-' + name);
    if (!r) return;
    for (var d = r.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
    var fs = r.closest('fieldset.collapsed');
    if (fs) fs.classList.remove('collapsed');
    r.scrollIntoView({ block: 'center', behavior: 'smooth' });
    r.classList.add('nt-flash');
    setTimeout(function () { r.classList.remove('nt-flash'); }, 2200);
  }

  function init() {
    document.querySelectorAll('.nt-pick').forEach(setupPick);
    document.querySelectorAll('.nt-chk input[data-sync]').forEach(setupCheck);
    document.addEventListener('change', function (e) {
      if (e.target.closest && e.target.closest('.nt-chk')) sync();
    });
    document.querySelectorAll('[data-nt-go]').forEach(function (a) {
      a.addEventListener('click', function (e) { e.preventDefault(); go(a.getAttribute('data-nt-go')); });
    });
    document.querySelectorAll('[data-nt-hide]').forEach(function (n) {   // поля, якими тепер керує таблиця
      n.getAttribute('data-nt-hide').split(' ').forEach(function (name) {
        var r = document.querySelector('.form-row.field-' + name);
        if (r) r.style.display = 'none';
      });
    });
    sync();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
