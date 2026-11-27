/*
 * Підказки адрес у полях «Копія (CC)» / «Кому» (кілька адрес через кому): групи з сервера
 * /email/address-suggest/ (email_assistant/address_suggest.py) — 👥 команда Minerva, 💬 учасники листування,
 * 🎯 ймовірно потрібні, 📇 інші. Фокус у порожньому місці поля — одразу підказки; введене слово (частина або
 * ціле) — пошук; ↑ ↓ — вибір, Enter / Tab — вставити, Esc — закрити. Уже вписані адреси й «Кому» не пропонуються.
 *
 * Поле: <input data-mv-addr data-mv-addr-msg="<pk листа>" data-mv-addr-to="<name поля «Кому» в тій самій формі>">
 * (або data-mv-addr-msg-field="<name поля з pk листа>"). Підключаються самі; нові поля — MVAddr.attach(el).
 */
(function () {
  'use strict';
  if (window.MVAddr) return;
  var URL_ = '/email/address-suggest/';
  var box = null, cur = null, pick = -1, timer = null, seq = 0, cache = {};

  function css() {
    if (document.getElementById('mva-css')) return;
    var s = document.createElement('style');
    s.id = 'mva-css';
    s.textContent =
      '.mva-box{position:fixed;z-index:2147483000;max-height:340px;overflow:auto;border-radius:8px;padding:4px 0;' +
      'background:var(--body-bg,#fff);color:var(--body-fg,#222);border:1px solid var(--border-color,#ccd);' +
      'box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:13px}' +
      '.mva-h{padding:6px 10px 3px;font-size:11px;font-weight:700;opacity:.75;text-transform:none}' +
      '.mva-i{padding:5px 10px 5px 14px;cursor:pointer;line-height:1.3}' +
      '.mva-i b{font-weight:600}.mva-i .mva-e{opacity:.85}.mva-i small{display:block;font-size:11px;opacity:.7}' +
      '.mva-i.on,.mva-i:hover{background:var(--selected-bg,rgba(80,130,255,.18))}' +
      '.mva-i mark{background:rgba(255,200,0,.35);color:inherit;padding:0;border-radius:2px}' +
      '.mva-none{padding:8px 12px;opacity:.7;font-size:12px}';
    document.head.appendChild(s);
  }

  function parts(v) { return v.split(/[,;\n]/); }
  function token(el) { var p = parts(el.value); return p[p.length - 1].trim(); }
  function entered(el) {
    var p = parts(el.value); p.pop();
    return p.map(function (x) { var m = x.match(/<([^>]+)>/); return (m ? m[1] : x).trim().toLowerCase(); })
            .filter(Boolean);
  }
  function field(el, name) {
    if (!name) return null;
    var f = el.form || document;
    return f.querySelector('[name="' + name.replace(/"/g, '') + '"]');
  }
  function msgOf(el) {
    if (el.dataset.mvAddrMsg) return el.dataset.mvAddrMsg;
    var f = field(el, el.dataset.mvAddrMsgField);
    return f ? (f.value || '') : '';
  }
  function toOf(el) { var f = field(el, el.dataset.mvAddrTo); return f && f !== el ? f.value : ''; }

  function close() { if (box) { box.hidden = true; box.innerHTML = ''; } pick = -1; }
  function place() {
    if (!box || box.hidden || !cur) return;
    var r = cur.getBoundingClientRect(), below = window.innerHeight - r.bottom;
    box.style.left = Math.max(4, r.left) + 'px';
    box.style.width = Math.max(320, Math.min(r.width, 640)) + 'px';
    if (below < 200 && r.top > below) { box.style.top = ''; box.style.bottom = (window.innerHeight - r.top + 2) + 'px';
      box.style.maxHeight = Math.min(340, r.top - 8) + 'px'; }
    else { box.style.bottom = ''; box.style.top = (r.bottom + 2) + 'px'; box.style.maxHeight = Math.min(340, below - 8) + 'px'; }
  }

  function mark(parent, text, q) {         // підсвічує слова запиту — лише текстові вузли, без HTML
    var words = (q || '').toLowerCase().split(/\s+/).filter(Boolean), low = text.toLowerCase(), at = 0;
    while (words.length) {
      var best = -1, len = 0;
      words.forEach(function (w) { var i = low.indexOf(w, at); if (i >= 0 && (best < 0 || i < best)) { best = i; len = w.length; } });
      if (best < 0) break;
      if (best > at) parent.appendChild(document.createTextNode(text.slice(at, best)));
      var m = document.createElement('mark'); m.textContent = text.slice(best, best + len); parent.appendChild(m);
      at = best + len;
    }
    if (at < text.length) parent.appendChild(document.createTextNode(text.slice(at)));
  }

  function render(groups, q) {
    if (!box) { css(); box = document.createElement('div'); box.className = 'mva-box'; box.setAttribute('role', 'listbox');
      document.body.appendChild(box); }
    box.innerHTML = ''; pick = -1;
    var n = 0;
    groups.forEach(function (g) {
      var h = document.createElement('div'); h.className = 'mva-h'; h.textContent = g.label; box.appendChild(h);
      g.items.forEach(function (it) {
        var d = document.createElement('div'); d.className = 'mva-i'; d.setAttribute('role', 'option');
        d.dataset.email = it.email;
        if (it.name && it.name.toLowerCase() !== it.email) {   // ім'я й адреса — дані, не перекладаються
          var b = document.createElement('b'); b.setAttribute('translate', 'no'); mark(b, it.name, q);
          d.appendChild(b); d.appendChild(document.createTextNode('  '));
        }
        var e = document.createElement('span'); e.className = 'mva-e'; e.setAttribute('translate', 'no');
        mark(e, it.email, q); d.appendChild(e);
        if (it.why) { var s = document.createElement('small'); s.textContent = it.why; d.appendChild(s); }
        d.addEventListener('mousedown', function (ev) { ev.preventDefault(); take(it.email); });
        box.appendChild(d); n++;
      });
    });
    if (!n) {
      if (!q) { close(); return; }
      var none = document.createElement('div'); none.className = 'mva-none';
      none.textContent = 'Нічого не знайдено — допишіть адресу повністю'; box.appendChild(none);
    }
    box.hidden = false; place();
  }

  function load(el) {
    var q = token(el), key = [msgOf(el), toOf(el), entered(el).join(','), q].join('|');
    if (cache[key]) { render(cache[key], q); return; }
    var my = ++seq;
    var url = URL_ + '?q=' + encodeURIComponent(q) + '&msg=' + encodeURIComponent(msgOf(el)) +
              '&to=' + encodeURIComponent(toOf(el)) + '&exclude=' + encodeURIComponent(entered(el).join(','));
    fetch(url, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : {groups: []}; })
      .then(function (d) {
        if (my !== seq || cur !== el) return;
        cache[key] = d.groups || [];
        render(cache[key], q);
      }).catch(function () {});
  }

  function take(email) {
    var el = cur; if (!el) return;
    var p = parts(el.value).map(function (x) { return x.trim(); }); p.pop();
    p = p.filter(Boolean);
    if (p.map(function (x) { return x.toLowerCase(); }).indexOf(email.toLowerCase()) < 0) p.push(email);
    el.value = p.join(', ') + ', ';
    close();
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.focus();
    try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) { /* textarea без виділення */ }
  }

  function items() { return box && !box.hidden ? box.querySelectorAll('.mva-i') : []; }
  function hl(list) {
    [].forEach.call(list, function (d, i) { d.classList.toggle('on', i === pick); });
    if (list[pick]) list[pick].scrollIntoView({block: 'nearest'});
  }

  function attach(el) {
    if (!el || el.dataset.mvAddrOn) return;
    el.dataset.mvAddrOn = '1';
    el.setAttribute('autocomplete', 'off');
    el.addEventListener('focus', function () { if (el.readOnly || el.disabled) return; cur = el; load(el); });
    el.addEventListener('click', function () { if (el.readOnly || el.disabled) return; cur = el; if (!box || box.hidden) load(el); });
    el.addEventListener('input', function (e) {
      if (!e.isTrusted || el.readOnly) return;
      cur = el; clearTimeout(timer); timer = setTimeout(function () { load(el); }, 180);
    });
    el.addEventListener('keydown', function (e) {
      var list = items();
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!list.length) { cur = el; load(el); e.preventDefault(); return; }
        e.preventDefault();
        pick = (pick + (e.key === 'ArrowDown' ? 1 : list.length - 1) + (pick < 0 && e.key === 'ArrowUp' ? 1 : 0)) % list.length;
        hl(list);
      } else if ((e.key === 'Enter' || e.key === 'Tab') && list.length && pick >= 0) {
        e.preventDefault(); take(list[pick].dataset.email);
      } else if (e.key === 'Enter' && box && !box.hidden) {   // відкриті підказки: Enter не надсилає форму
        e.preventDefault();
        if (list.length && token(el)) take(list[0].dataset.email); else close();
      } else if (e.key === 'Escape' && box && !box.hidden) {
        e.preventDefault(); e.stopPropagation(); close();
      }
    });
    el.addEventListener('blur', function () { setTimeout(function () { if (cur === el && document.activeElement !== el) close(); }, 150); });
  }

  function scan(root) {
    [].forEach.call((root || document).querySelectorAll('[data-mv-addr]'), attach);
  }
  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
  window.MVAddr = {attach: attach, scan: scan, close: close};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { scan(); });
  else scan();
})();
