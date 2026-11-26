/* ✉️ Кілька email-адрес (core/email_list.py, шаблон core/widgets/email_list.html).
   Адреси — чипами (видно всі), введене — Enter / кома / пробіл / Tab, вставка списку — усі адреси одразу,
   «＋ З акаунтів» і підказки під час введення — адреси акаунтів Minerva одним кліком (клік на ✓ — прибрати).
   Справжнє значення — textarea поруч («a@x.de, b@y.com»), сервер перевіряє адреси ще раз. */
(function () {
  'use strict';
  if (window.MVEmailList) return;

  var SPLIT = /[,;\s]+/;
  var OK = /^[^\s@<>()",;:]+@[^\s@<>()",;:]+\.[^\s@<>()",;:]{2,}$/;

  function clean(raw) {
    return String(raw || '').trim().replace(/^<+|>+$/g, '').trim();
  }

  function parse(text) {
    var out = [];
    String(text || '').split(SPLIT).forEach(function (part) {
      var e = clean(part);
      if (e && !has(out, e)) out.push(e);
    });
    return out;
  }

  function has(list, email) {
    var k = email.toLowerCase();
    for (var i = 0; i < list.length; i++) if (list[i].toLowerCase() === k) return true;
    return false;
  }

  function init(root) {
    if (root.dataset.mvelReady) return;
    root.dataset.mvelReady = '1';
    var raw = root.querySelector('textarea');
    var box = root.querySelector('[data-mvel-box]');
    var input = root.querySelector('[data-mvel-input]');
    var dd = root.querySelector('[data-mvel-dd]');
    var openBtn = root.querySelector('[data-mvel-open]');
    var none = root.querySelector('[data-mvel-none]');
    if (!raw || !box || !input) return;

    var locked = raw.disabled || raw.readOnly;
    var list = parse(raw.value);
    var opts = dd ? Array.prototype.slice.call(dd.querySelectorAll('[data-mvel-opt]')) : [];
    var labels = {};
    opts.forEach(function (o) { if (o.dataset.label) labels[o.dataset.email.toLowerCase()] = o.dataset.label; });
    var active = -1;

    root.classList.add('mvel-on');
    if (locked) { root.classList.add('mvel-locked'); input.hidden = true; if (openBtn) openBtn.hidden = true; }
    if (raw.id) {
      input.id = raw.id + '_input';
      var lab = document.querySelector('label[for="' + raw.id + '"]');
      if (lab) lab.htmlFor = input.id;
    }

    function sync() {
      raw.value = list.join(', ');
      raw.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function render() {
      Array.prototype.slice.call(box.querySelectorAll('.mvel-chip')).forEach(function (c) { c.remove(); });
      list.forEach(function (email, i) {
        var chip = document.createElement('span');
        chip.className = 'mvel-chip' + (OK.test(email) ? '' : ' mvel-bad');
        chip.setAttribute('translate', 'no');
        var name = labels[email.toLowerCase()];
        chip.title = OK.test(email) ? (name ? name + ' · ' + email : email) : 'Неправильна адреса — клік, щоб виправити';
        var txt = document.createElement('span');
        txt.className = 'mvel-chip-t';
        txt.textContent = email;
        chip.appendChild(txt);
        if (!locked) {
          txt.addEventListener('click', function () { edit(i); });
          var x = document.createElement('button');
          x.type = 'button';
          x.className = 'mvel-x';
          x.textContent = '✕';
          x.title = 'Прибрати';
          x.setAttribute('aria-label', 'Прибрати ' + email);
          x.addEventListener('click', function (ev) { ev.preventDefault(); remove(email); input.focus(); });
          chip.appendChild(x);
        }
        box.insertBefore(chip, input);
      });
      opts.forEach(function (o) {
        var on = has(list, o.dataset.email);
        o.classList.toggle('on', on);
        o.title = on ? 'Уже додано — клік прибере' : 'Додати';
      });
    }

    function add(text) {
      var changed = false;
      parse(text).forEach(function (e) {
        if (!has(list, e)) { list.push(e); changed = true; }
      });
      if (changed) { sync(); render(); }
      return changed;
    }

    function remove(email) {
      var k = email.toLowerCase();
      list = list.filter(function (e) { return e.toLowerCase() !== k; });
      sync();
      render();
      filter();
    }

    function edit(i) {
      var email = list[i];
      list.splice(i, 1);
      sync();
      render();
      if (input.value.trim()) add(input.value);
      input.value = email;
      input.focus();
      input.select();
      filter();
    }

    function commit() {
      var v = input.value;
      if (!v.trim()) { input.value = ''; return false; }
      input.value = '';
      add(v);
      filter();
      return true;
    }

    function visible() {
      return opts.filter(function (o) { return !o.hidden; });
    }

    function filter() {
      if (!dd) return;
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      opts.forEach(function (o) {
        var hit = !q || o.dataset.email.toLowerCase().indexOf(q) >= 0 ||
          (o.dataset.label || '').toLowerCase().indexOf(q) >= 0;
        o.hidden = !hit;
        if (hit) shown++;
      });
      Array.prototype.slice.call(dd.querySelectorAll('[data-mvel-grp]')).forEach(function (g) {
        g.hidden = !g.querySelector('[data-mvel-opt]:not([hidden])');
      });
      if (none) none.hidden = shown > 0;
      setActive(q ? 0 : -1);
    }

    function setActive(i) {
      var vis = visible();
      opts.forEach(function (o) { o.classList.remove('hi'); });
      active = vis.length ? Math.max(-1, Math.min(i, vis.length - 1)) : -1;
      if (active >= 0) {
        vis[active].classList.add('hi');
        vis[active].scrollIntoView({ block: 'nearest' });
      }
    }

    function open() {
      if (!dd || locked) return;
      dd.hidden = false;
      root.classList.add('mvel-open');
      if (openBtn) openBtn.setAttribute('aria-expanded', 'true');
      filter();
    }

    function close() {
      if (!dd || dd.hidden) return;
      dd.hidden = true;
      root.classList.remove('mvel-open');
      if (openBtn) openBtn.setAttribute('aria-expanded', 'false');
      active = -1;
    }

    function pick(o) {
      var email = o.dataset.email;
      if (has(list, email)) remove(email); else add(email);
      input.value = '';
      filter();
      input.focus();
    }

    render();
    if (locked) return;

    box.addEventListener('mousedown', function (ev) {
      if (ev.target === box) { ev.preventDefault(); input.focus(); }
    });
    input.addEventListener('focus', function () { if (input.value.trim()) open(); });
    input.addEventListener('input', function () {
      if (SPLIT.test(input.value) && /[,;\s]$/.test(input.value)) { commit(); close(); return; }
      if (input.value.trim()) open(); else filter();
    });
    input.addEventListener('paste', function (ev) {
      var text = (ev.clipboardData || window.clipboardData);
      text = text ? text.getData('text') : '';
      if (text && SPLIT.test(text.trim())) {
        ev.preventDefault();
        add(input.value + ' ' + text);
        input.value = '';
        filter();
      }
    });
    input.addEventListener('keydown', function (ev) {
      var vis = visible();
      var isOpen = dd && !dd.hidden;
      if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        if (!isOpen) open();
        setActive(active + 1);
      } else if (ev.key === 'ArrowUp') {
        if (isOpen) { ev.preventDefault(); setActive(active - 1); }
      } else if (ev.key === 'Enter') {
        ev.preventDefault();                       // форму не надсилати
        if (isOpen && active >= 0 && vis[active]) pick(vis[active]);
        else { commit(); close(); }
      } else if (ev.key === 'Tab') {
        if (input.value.trim()) { commit(); close(); }
      } else if (ev.key === 'Escape') {
        if (isOpen) { ev.preventDefault(); ev.stopPropagation(); close(); }
      } else if (ev.key === 'Backspace' && !input.value && list.length) {
        ev.preventDefault();
        edit(list.length - 1);
      }
    });
    input.addEventListener('blur', function () {
      setTimeout(function () {
        if (root.contains(document.activeElement)) return;
        commit();
        close();
      }, 150);
    });

    if (openBtn) {
      openBtn.addEventListener('click', function () {
        if (dd && !dd.hidden) { close(); return; }
        input.value = input.value.trim();
        open();
        input.focus();
      });
    }
    opts.forEach(function (o) {
      o.addEventListener('mousedown', function (ev) { ev.preventDefault(); });   // фокус лишається в полі
      o.addEventListener('click', function (ev) { ev.preventDefault(); pick(o); });
    });
    document.addEventListener('mousedown', function (ev) {
      if (!root.contains(ev.target)) close();
    });
    // перед «Зберегти» — недописана адреса теж іде у значення
    if (raw.form) raw.form.addEventListener('submit', function () { commit(); }, true);
  }

  function initAll(scope) {
    Array.prototype.slice.call((scope || document).querySelectorAll('[data-mvel]')).forEach(init);
  }

  window.MVEmailList = { init: init, initAll: initAll, parse: parse };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { initAll(); });
  else initAll();
})();
