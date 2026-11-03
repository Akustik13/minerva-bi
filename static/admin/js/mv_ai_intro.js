/* 🤖 Помічники внизу праворуч — що це за кружечки й що вони вміють.
 *
 * • Картка при наведенні на 🏛️ Minerva AI (#mv-fab) і «Minerva» з аватаром (#rg-pill): хто це, що вміє, приклад.
 * • Перший раз (localStorage `mv-ai-intro-v1`) — віконце «🤖 Помічники Minerva» над кнопками, кнопки пульсують;
 *   знову — «❓ Хто з помічників для чого» в картці або MVAiIntro.show().
 * Показує лише ті кнопки, що є на сторінці (🏛️ — не на /ai/, «Minerva» — коли є доступ). Нічого не відкриває саме.
 */
(function () {
  'use strict';
  var KEY = 'mv-ai-intro-v1';
  var GUIDE = '/guide/start/assistants/';
  var HELPERS = {
    fab: {
      id: 'mv-fab', icon: '🏛️', name: 'Minerva AI', what: 'питання про ваш бізнес',
      text: 'Відповідає з живих даних Minerva: замовлення, продажі, склад, клієнти. Складає лист клієнту — ' +
            'ви відкриваєте його в пошті й надсилаєте самі.',
      example: '«Які замовлення ще не відправлені й у яких минув строк?»'
    },
    pill: {
      id: 'rg-pill', icon: '', name: 'Minerva', what: 'як працювати в Minerva і товари',
      text: '📚 Гід: де кнопка, як налаштувати — може показати все на сторінці. 📦 Товари: характеристики, ' +
            'даташити, наявність.',
      example: '«Як надрукувати етикетку замовлення?»'
    }
  };

  function ls(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* приватне вікно */ }
    return null;
  }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function visible(node) {
    if (!node || !document.body.contains(node)) return false;
    var cs = getComputedStyle(node);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && node.getClientRects().length > 0;
  }
  function present() {
    return Object.keys(HELPERS).filter(function (k) { return visible(document.getElementById(HELPERS[k].id)); });
  }
  function avatarOf(key) {
    if (key !== 'pill') return null;
    var img = document.querySelector('#rg-pill img');
    return img && img.src ? img.src : null;
  }
  function iconNode(key) {
    var src = avatarOf(key);
    if (src) { var img = el('img', 'mvai-ava'); img.src = src; img.alt = ''; return img; }
    return el('span', 'mvai-ico', HELPERS[key].icon || '🤖');
  }
  function nameOf(key) {
    if (key === 'pill') {
      var n = document.querySelector('#rg-pill .rg-name');
      return (n && n.textContent.trim()) || HELPERS.pill.name;
    }
    return HELPERS[key].name;
  }

  // ── Картка при наведенні ────────────────────────────────────────────────
  var card = null, cardFor = null, showT = 0, hideT = 0;

  function buildCard(key) {
    var h = HELPERS[key];
    var c = el('div', 'mvai-card');
    c.setAttribute('role', 'tooltip');
    var head = el('div', 'mvai-card-head');
    head.appendChild(iconNode(key));
    var t = el('div');
    t.appendChild(el('b', null, nameOf(key)));
    t.appendChild(el('span', 'mvai-what', ' — ' + h.what));
    head.appendChild(t);
    c.appendChild(head);
    c.appendChild(el('div', 'mvai-text', h.text));
    c.appendChild(el('div', 'mvai-ex', 'Напр.: ' + h.example));
    var foot = el('div', 'mvai-foot');
    foot.appendChild(el('span', 'mvai-hint', 'Натисніть, щоб відкрити'));
    var more = el('button', 'mvai-link', '❓ Хто з помічників для чого');
    more.type = 'button';
    more.onclick = function (e) { e.stopPropagation(); hideCard(true); show(); };
    foot.appendChild(more);
    c.appendChild(foot);
    c.addEventListener('mouseenter', function () { clearTimeout(hideT); });
    c.addEventListener('mouseleave', function () { hideT = setTimeout(hideCard, 250); });
    return c;
  }
  function place(node, anchors) {
    var top = Infinity, right = 0;
    anchors.forEach(function (a) {
      var r = a.getBoundingClientRect();
      top = Math.min(top, r.top);
      right = Math.max(right, window.innerWidth - r.right);
    });
    node.style.bottom = Math.max(12, window.innerHeight - top + 12) + 'px';
    node.style.right = Math.max(12, Math.min(right, 24)) + 'px';
  }
  function showCard(key) {
    var anchor = document.getElementById(HELPERS[key].id);
    if (!visible(anchor) || intro) return;
    hideCard(true);
    card = buildCard(key);
    cardFor = key;
    document.body.appendChild(card);
    place(card, [anchor]);
    requestAnimationFrame(function () { if (card) card.classList.add('on'); });
  }
  function hideCard(now) {
    clearTimeout(showT);
    if (!card) return;
    var c = card;
    card = cardFor = null;
    if (now) { c.remove(); return; }
    c.classList.remove('on');
    setTimeout(function () { c.remove(); }, 160);
  }
  function hook(key) {
    var node = document.getElementById(HELPERS[key].id);
    if (!node || node.dataset.mvaiHooked) return;
    node.dataset.mvaiHooked = '1';
    if (node.title) { node.setAttribute('aria-label', nameOf(key) + ' — ' + HELPERS[key].what); node.removeAttribute('title'); }
    var hover = window.matchMedia && window.matchMedia('(hover: hover)').matches;
    if (!hover) return;                                   // телефон: дотик просто відкриває помічника
    node.addEventListener('mouseenter', function () {
      clearTimeout(hideT); clearTimeout(showT);
      showT = setTimeout(function () { showCard(key); }, 350);
    });
    node.addEventListener('mouseleave', function () { clearTimeout(showT); hideT = setTimeout(hideCard, 250); });
    node.addEventListener('focus', function () { showCard(key); });
    node.addEventListener('blur', function () { hideCard(); });
    node.addEventListener('click', function () { hideCard(true); if (intro) hide(); });
  }

  // ── Перше знайомство ────────────────────────────────────────────────────
  var intro = null;

  function show() {
    var keys = present();
    if (!keys.length) return false;
    hide(true);
    hideCard(true);
    intro = el('div', 'mvai-intro');
    intro.setAttribute('role', 'dialog');
    intro.setAttribute('aria-label', 'Помічники Minerva');
    var x = el('button', 'mvai-x', '✕');
    x.type = 'button'; x.title = 'Закрити'; x.onclick = function () { hide(); };
    intro.appendChild(x);
    intro.appendChild(el('div', 'mvai-title', '🤖 Помічники Minerva — тут, унизу праворуч'));
    keys.forEach(function (key) {
      var h = HELPERS[key];
      var row = el('div', 'mvai-row');
      row.appendChild(iconNode(key));
      var body = el('div');
      var t = el('div');
      t.appendChild(el('b', null, nameOf(key)));
      t.appendChild(el('span', 'mvai-what', ' — ' + h.what));
      body.appendChild(t);
      body.appendChild(el('div', 'mvai-text', h.text));
      body.appendChild(el('div', 'mvai-ex', 'Напр.: ' + h.example));
      row.appendChild(body);
      intro.appendChild(row);
    });
    intro.appendChild(el('div', 'mvai-more',
      '✨ AI є й на місці роботи: у 📥 Пошті — AI-відповідь і «📋 Підготувати відповідь», у картках клієнта, ' +
      'замовлення й товару — «🧭 Огляд» і «❓ Запитати», 🧩 Завдання агенту — «знайди й внеси», «підготуй відправлення».'));
    var foot = el('div', 'mvai-foot');
    var guide = el('a', 'mvai-link', '📚 Докладніше в гіді');
    guide.href = GUIDE;
    foot.appendChild(guide);
    var ok = el('button', 'mvai-ok', 'Зрозуміло');
    ok.type = 'button'; ok.onclick = function () { hide(); };
    foot.appendChild(ok);
    intro.appendChild(foot);
    document.body.appendChild(intro);
    var anchors = keys.map(function (k) { return document.getElementById(HELPERS[k].id); });
    place(intro, anchors);
    anchors.forEach(function (a) { a.classList.add('mvai-ring'); });
    requestAnimationFrame(function () { if (intro) intro.classList.add('on'); });
    document.addEventListener('keydown', onKey);
    return true;
  }
  function onKey(e) { if (e.key === 'Escape') hide(); }
  function hide(silent) {
    document.removeEventListener('keydown', onKey);
    Object.keys(HELPERS).forEach(function (k) {
      var n = document.getElementById(HELPERS[k].id);
      if (n) n.classList.remove('mvai-ring');
    });
    if (!intro) return;
    intro.remove();
    intro = null;
    if (!silent) ls(KEY, '1');
  }

  function start() {
    Object.keys(HELPERS).forEach(hook);
    // «Minerva» з'являється після запиту стану — дочікуємося й чіпляємо картку
    var tries = 0;
    var t = setInterval(function () {
      Object.keys(HELPERS).forEach(hook);
      if (++tries > 20) clearInterval(t);
    }, 500);
    if (!ls(KEY)) {
      setTimeout(function () {
        if (!ls(KEY) && !document.documentElement.classList.contains('mv-typing')) show();
      }, 2500);
    }
  }

  window.MVAiIntro = { show: show, hide: hide };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
