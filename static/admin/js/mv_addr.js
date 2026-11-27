/*
 * Підказки адрес у полях «Кому» / «Копія (CC)» / «Кому надіслати» усієї Minerva: групи з сервера
 * /email/address-suggest/ (email_assistant/address_suggest.py) — 👥 команда Minerva, 💬 учасники листування,
 * 🎯 ймовірно потрібні, 📇 інші. Фокус у порожньому місці поля — одразу підказки; введене слово (частина або
 * ціле) — пошук; ↑ ↓ — вибір, Enter / Tab — вставити, Esc — закрити. Уже вписані адреси й «Кому» не пропонуються.
 *
 * Поле: <input data-mv-addr …> — кілька адрес через кому; data-mv-addr="one" — одна адреса (вибір замінює поле);
 *   data-mv-addr="pick" — поле лише для пошуку: вибір шле подію «mv:addr-pick» {email, name} і очищає поле
 *   (сторінка сама додає людину, напр. учасника зустрічі); data-mv-addr-taken="a@x, b@y" — уже вибрані (не пропонуються).
 *   data-mv-addr-msg="<pk листа>" або data-mv-addr-msg-field="<name поля з pk листа>" — лист, з якого підказувати
 *   учасників листування; data-mv-addr-to="<name поля «Кому» тієї самої форми>" або "#<id поля>" — адресат.
 * Скрипт підключено в base_site.html для всіх сторінок; поля, що з'являються пізніше (вікна, прев'ю листа),
 * підхоплюються самі при першому фокусі. Замість prompt(): MVAddr.ask({title, value, msg}) → Promise<рядок|null>.
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
      'background:var(--bg-card,var(--body-bg,#fff));color:var(--text,var(--body-fg,#222));' +
      'border:1px solid var(--border-strong,var(--border-color,#ccd));box-shadow:0 8px 24px rgba(0,0,0,.25);' +
      'font-size:13px;text-align:left}' +
      '.mva-h{padding:6px 10px 3px;font-size:11px;font-weight:700;opacity:.75;text-transform:none}' +
      '.mva-i{padding:5px 10px 5px 14px;cursor:pointer;line-height:1.3}' +
      '.mva-i b{font-weight:600}.mva-i .mva-e{opacity:.85}.mva-i small{display:block;font-size:11px;opacity:.7}' +
      '.mva-i.on,.mva-i:hover{background:var(--selected-bg,rgba(80,130,255,.18))}' +
      '.mva-i mark{background:rgba(255,200,0,.35);color:inherit;padding:0;border-radius:2px}' +
      '.mva-none{padding:8px 12px;opacity:.7;font-size:12px}' +
      '.mva-dlg{border:1px solid var(--border-strong,#889);border-radius:10px;padding:16px 18px;min-width:min(520px,92vw);' +
      'background:var(--bg-card,var(--body-bg,#fff));color:var(--text,var(--body-fg,#222))}' +
      '.mva-dlg::backdrop{background:rgba(0,0,0,.45)}' +
      '.mva-dlg p{margin:0 0 10px;font-size:14px;line-height:1.4}' +
      '.mva-dlg input{width:100%;box-sizing:border-box;padding:7px 9px;font-size:13px}' +
      '.mva-dlg .mva-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}';
    document.head.appendChild(s);
  }

  function one(el) { return el.dataset.mvAddr === 'one' || el.dataset.mvAddr === 'pick'; }
  function pickMode(el) { return el.dataset.mvAddr === 'pick'; }
  function parts(v) { return v.split(/[,;\n]/); }
  function full(v) { return /^[^\s@,;<>]+@[^\s@,;<>]+\.[a-z]{2,}$/i.test(v); }
  // Останнє слово — запит, лише якщо його вписують зараз; готова адреса, якої ще не правили (поле з адресою з
  // листа, повернулись у поле), — не запит: підказки без пошуку, вибране додається після неї (одна — замінює).
  function last(el) {
    var p = parts(el.value), v = p[p.length - 1].trim();
    return full(v.replace(/^.*<([^>]+)>\s*$/, '$1')) && !el.dataset.mvAddrTyped ? '' : v;
  }
  function token(el) { return one(el) ? (full(el.value.trim()) && !el.dataset.mvAddrTyped ? '' : el.value.trim()) : last(el); }
  function kept(el) {                      // уже вписані адреси (рядками, як є) — без запиту
    var p = parts(el.value).map(function (x) { return x.trim(); });
    if (last(el)) p.pop();
    return p.filter(Boolean);
  }
  function entered(el) {
    if (one(el)) return [];
    return kept(el).map(function (x) { var m = x.match(/<([^>]+)>/); return (m ? m[1] : x).trim().toLowerCase(); });
  }
  function field(el, name) {
    if (!name) return null;
    if (name.charAt(0) === '#') return document.getElementById(name.slice(1));
    var f = el.form || el.closest('form, dialog, [role=dialog]') || document;
    return f.querySelector('[name="' + name.replace(/"/g, '') + '"]') ||
           document.querySelector('[name="' + name.replace(/"/g, '') + '"]');
  }
  function msgOf(el) {
    if (el.dataset.mvAddrMsg) return el.dataset.mvAddrMsg;
    var f = field(el, el.dataset.mvAddrMsgField);
    return f ? (f.value || '') : '';
  }
  function toOf(el) {                      // адресат + уже вибрані: їх не пропонуємо, з ними шукаємо «хто ще буває поруч»
    var f = field(el, el.dataset.mvAddrTo), v = f && f !== el ? f.value : '';
    return [v, el.dataset.mvAddrTaken || ''].filter(Boolean).join(', ');
  }

  function close() { if (box) { box.hidden = true; box.innerHTML = ''; } pick = -1; }
  function place() {
    if (!box || box.hidden || !cur) return;
    var r = cur.getBoundingClientRect(), below = window.innerHeight - r.bottom;
    box.style.left = Math.max(4, Math.min(r.left, window.innerWidth - 324)) + 'px';
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
    if (!box) { css(); box = document.createElement('div'); box.className = 'mva-box'; box.setAttribute('role', 'listbox'); }
    // вікно <dialog> лежить у верхньому шарі — підказки мусять бути всередині нього, інакше їх не видно
    var host = (cur && cur.closest('dialog[open]')) || document.body;
    if (box.parentNode !== host) host.appendChild(box);
    box.innerHTML = ''; pick = -1;
    var n = 0;
    groups.forEach(function (g) {
      var h = document.createElement('div'); h.className = 'mva-h'; h.textContent = g.label; box.appendChild(h);
      g.items.forEach(function (it) {
        var d = document.createElement('div'); d.className = 'mva-i'; d.setAttribute('role', 'option');
        d.dataset.email = it.email; d.dataset.name = it.name || '';
        if (it.name && it.name.toLowerCase() !== it.email) {   // ім'я й адреса — дані, не перекладаються
          var b = document.createElement('b'); b.setAttribute('translate', 'no'); mark(b, it.name, q);
          d.appendChild(b); d.appendChild(document.createTextNode('  '));
        }
        var e = document.createElement('span'); e.className = 'mva-e'; e.setAttribute('translate', 'no');
        mark(e, it.email, q); d.appendChild(e);
        if (it.why) { var s = document.createElement('small'); s.textContent = it.why; d.appendChild(s); }
        d.addEventListener('mousedown', function (ev) { ev.preventDefault(); take(it.email, it.name); });
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
        if (my !== seq || cur !== el || document.activeElement !== el) return;
        cache[key] = d.groups || [];
        render(cache[key], q);
      }).catch(function () {});
  }

  function take(email, name) {
    var el = cur; if (!el) return;
    if (pickMode(el)) {
      el.value = ''; delete el.dataset.mvAddrTyped; close();
      el.dispatchEvent(new CustomEvent('mv:addr-pick', {bubbles: true, detail: {email: email, name: name || ''}}));
      el.focus();
      return;
    }
    if (one(el)) {
      el.value = email; delete el.dataset.mvAddrTyped;
    } else {
      var p = kept(el);
      if (entered(el).indexOf(email.toLowerCase()) < 0) p.push(email);
      el.value = p.join(', ') + ', ';
      delete el.dataset.mvAddrTyped;
    }
    close();
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.focus();
    try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) { /* type=email — без виділення */ }
  }

  function items() { return box && !box.hidden ? box.querySelectorAll('.mva-i') : []; }
  function hl(list) {
    [].forEach.call(list, function (d, i) { d.classList.toggle('on', i === pick); });
    if (list[pick]) list[pick].scrollIntoView({block: 'nearest'});
  }

  function tidy(el) {                      // без «, » у кінці; сторінка, що стежить за полем (input), — дізнається
    if (one(el) || !/[,;]\s*$/.test(el.value)) return;
    el.value = el.value.replace(/[,;\s]+$/, '');
    el.dispatchEvent(new Event('input', {bubbles: true}));
  }
  function ready(el, now) {                // курсор у кінці після готових адрес — «, », щоб одразу писати наступну
    function check() {
      if (document.activeElement !== el || one(el) || !el.value.trim() || /[,;\n]\s*$/.test(el.value) || last(el)) return;
      var at = null;
      try { at = el.selectionStart; } catch (e) { return; }
      if (at !== el.value.length) return;
      el.value = el.value.replace(/\s+$/, '') + ', ';
      try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) { /* ні */ }
    }
    if (now) check(); else setTimeout(check, 0);   // фокус: курсор ще не на місці; клік — уже
  }
  function usable(el) { return !(el.readOnly || el.disabled); }
  function attach(el) {
    if (!el || el.dataset.mvAddrOn) return;
    el.dataset.mvAddrOn = '1';
    el.setAttribute('autocomplete', 'off');
    if (el.getAttribute('list')) el.removeAttribute('list');   // свій список браузера поверх наших підказок — ні
    el.addEventListener('focus', function () { if (!usable(el)) return; cur = el; ready(el); load(el); });
    el.addEventListener('click', function () {   // курсор ставиться після фокусу — «, » у кінці перевіряємо й тут
      if (!usable(el)) return;
      cur = el; ready(el, true);
      if (!box || box.hidden) load(el);
    });
    el.addEventListener('input', function (e) {
      if (!e.isTrusted || !usable(el)) return;
      el.dataset.mvAddrTyped = '1';
      cur = el; clearTimeout(timer); timer = setTimeout(function () { load(el); }, 180);
    });
    // фаза перехоплення: свої клавіші сторінки поля (Enter — додати, Esc — закрити вікно) не спрацьовують,
    // поки відкриті підказки
    el.addEventListener('keydown', function (e) {
      var list = items(), open = box && !box.hidden && cur === el;
      function stop() { e.preventDefault(); e.stopImmediatePropagation(); }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!open || !list.length) { if (usable(el)) { cur = el; load(el); } stop(); return; }
        stop();
        pick = (pick + (e.key === 'ArrowDown' ? 1 : list.length - 1) + (pick < 0 && e.key === 'ArrowUp' ? 1 : 0)) % list.length;
        hl(list);
      } else if (!open) {
        return;
      } else if ((e.key === 'Enter' || e.key === 'Tab') && list.length && pick >= 0) {
        stop(); take(list[pick].dataset.email, list[pick].dataset.name);
      } else if (e.key === 'Enter') {      // відкриті підказки: Enter не надсилає форму
        if (pickMode(el) && !list.length) { close(); return; }   // нічого не знайдено — сторінка додасть вписане
        stop();
        if (list.length && token(el)) take(list[0].dataset.email, list[0].dataset.name); else close();
      } else if (e.key === 'Escape') {
        stop(); close();
      }
    }, true);
    el.addEventListener('blur', function () {
      // одразу, до кліку на «Надіслати»: без «, » у кінці поля (сторінка читає значення в обробнику кліку)
      delete el.dataset.mvAddrTyped;
      tidy(el);
      setTimeout(function () { if (cur === el && document.activeElement !== el) close(); }, 150);
    });
  }

  function scan(root) {
    [].forEach.call((root || document).querySelectorAll('[data-mv-addr]'), attach);
  }

  // Замість prompt(): вікно з полем адрес і тими самими підказками. Promise → рядок (може бути порожній) або null.
  function ask(opts) {
    opts = opts || {};
    css();
    return new Promise(function (resolve) {
      var dlg = document.createElement('dialog');
      dlg.className = 'mva-dlg';
      var f = document.createElement('form'); f.method = 'dialog';
      var p = document.createElement('p'); p.textContent = opts.title || 'Кому надіслати? Адреси через кому:';
      var inp = document.createElement('input');
      inp.type = 'text'; inp.value = opts.value || ''; inp.setAttribute('translate', 'no');
      inp.setAttribute('data-mv-addr', opts.one ? 'one' : '');
      if (opts.msg) inp.setAttribute('data-mv-addr-msg', String(opts.msg));
      var btns = document.createElement('div'); btns.className = 'mva-btns';
      var no = document.createElement('button'); no.type = 'button'; no.className = 'button'; no.textContent = 'Скасувати';
      var ok = document.createElement('button'); ok.type = 'submit'; ok.className = 'button default';
      ok.textContent = opts.okText || 'OK';
      btns.appendChild(no); btns.appendChild(ok);
      f.appendChild(p); f.appendChild(inp); f.appendChild(btns); dlg.appendChild(f);
      document.body.appendChild(dlg);
      var answer = null;
      f.addEventListener('submit', function () { answer = inp.value.replace(/[,;\s]+$/, '').trim(); });
      no.addEventListener('click', function () { dlg.close(); });
      dlg.addEventListener('close', function () { close(); dlg.remove(); resolve(answer); });
      attach(inp);
      dlg.showModal();
      inp.focus();
      try { inp.setSelectionRange(inp.value.length, inp.value.length); } catch (e) { /* ні */ }
    });
  }

  // поля, що з'явились після завантаження сторінки (прев'ю листа, вікна): підхоплюємо при першому фокусі
  document.addEventListener('focusin', function (e) {
    var t = e.target;
    if (!t || !t.matches || !t.matches('[data-mv-addr]') || t.dataset.mvAddrOn) return;
    attach(t);
    if (usable(t)) { cur = t; ready(t); load(t); }
  }, true);
  document.addEventListener('submit', function (e) {   // Enter у полі надсилає форму: теж без «, » у кінці
    [].forEach.call(e.target.querySelectorAll ? e.target.querySelectorAll('[data-mv-addr]') : [], tidy);
  }, true);
  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
  window.MVAddr = {attach: attach, scan: scan, close: close, ask: ask,
                   isOpen: function () { return !!(box && !box.hidden); }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { scan(); });
  else scan();
})();
