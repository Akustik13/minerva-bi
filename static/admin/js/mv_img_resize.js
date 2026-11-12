/* 🖼 Розмір картинки в тексті листа (форма «Написати», відповідь під листом, сторінка гілки).
 *
 * Chrome / Edge у полі contenteditable не дають тягнути картинку за кут (лише Firefox), тож:
 *   - клік на картинці — рамка з чотирма кутами (тягнути — зменшити / збільшити, пропорції лишаються),
 *     розмір «640 × 360» і кнопки 25% · 50% · 75% · 100% ширини тексту та 1:1 — справжній розмір;
 *   - вставлена картинка, ширша за текст листа, одразу вписується в ширину;
 *   - розмір пишеться в саму картинку — атрибути width / height (їх розуміє Outlook) і style width,
 *     тож лист і збережена чернетка приходять з тим самим розміром.
 * Delete / Backspace — прибрати вибрану картинку, Esc або клік поза нею — зняти рамку.
 * Слухачі — на document: прев'ю листа в скриньці малюється пізніше, підключати окремо нічого не треба.
 */
(function () {
  'use strict';
  if (window.MVImgResize) return;

  var MIN = 24;
  var box = null, img = null, drag = null, raf = 0;

  function editorOf(el) {
    var ed = el && el.closest && el.closest('[contenteditable]');
    while (ed && ed.parentElement && ed.parentElement.closest && ed.parentElement.closest('[contenteditable="true"]')) {
      ed = ed.parentElement.closest('[contenteditable="true"]');
    }
    return ed;
  }
  function editable(el) {
    return !!(el && el.tagName === 'IMG' && el.isContentEditable);
  }
  // Ширина тексту листа: поле без відступів (картинка не ширша за лист)
  function maxWidth(el) {
    var ed = editorOf(el);
    if (!ed) return 2000;
    var cs = getComputedStyle(ed);
    var w = ed.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    return Math.max(MIN, Math.floor(w));
  }
  function ratioOf(el) {
    if (el.naturalWidth && el.naturalHeight) return el.naturalHeight / el.naturalWidth;
    var r = el.getBoundingClientRect();
    return r.width ? r.height / r.width : 1;
  }
  function apply(el, w) {
    w = Math.max(MIN, Math.round(w));
    var h = Math.max(1, Math.round(w * ratioOf(el)));
    el.style.width = w + 'px';
    el.style.height = 'auto';
    el.style.maxWidth = '100%';
    el.setAttribute('width', w);
    el.setAttribute('height', h);
  }
  function changed(el) {
    var ed = editorOf(el);
    if (ed) ed.dispatchEvent(new Event('input', {bubbles: true}));
  }

  function css() {
    if (document.getElementById('mvir-css')) return;
    var s = document.createElement('style');
    s.id = 'mvir-css';
    s.textContent =
      '#mvir{position:fixed;z-index:99990;pointer-events:none;border:2px solid var(--accent,#1a73e8);' +
      'box-sizing:border-box;display:none}' +
      '#mvir .mvir-h{position:absolute;width:12px;height:12px;background:#fff;border:2px solid var(--accent,#1a73e8);' +
      'border-radius:2px;pointer-events:auto;touch-action:none;box-sizing:border-box}' +
      '#mvir .nw{left:-7px;top:-7px;cursor:nwse-resize}#mvir .ne{right:-7px;top:-7px;cursor:nesw-resize}' +
      '#mvir .sw{left:-7px;bottom:-7px;cursor:nesw-resize}#mvir .se{right:-7px;bottom:-7px;cursor:nwse-resize}' +
      '#mvir .mvir-size{position:absolute;right:4px;bottom:4px;background:rgba(0,0,0,.65);color:#fff;font:11px/1.4 ' +
      'system-ui,sans-serif;padding:1px 6px;border-radius:4px;white-space:nowrap}' +
      '#mvir .mvir-bar{position:absolute;left:0;top:-34px;display:flex;gap:2px;pointer-events:auto;' +
      'background:var(--bg-card,#fff);border:1px solid var(--border-strong,#bbb);border-radius:6px;padding:2px;' +
      'box-shadow:0 2px 8px rgba(0,0,0,.2);white-space:nowrap}' +
      '#mvir.mvir-in .mvir-bar{top:4px;left:4px}' +
      '#mvir .mvir-bar button{border:none;background:none;color:var(--text,#222);font:12px/1 system-ui,sans-serif;' +
      'padding:5px 7px;border-radius:4px;cursor:pointer}' +
      '#mvir .mvir-bar button:hover{background:var(--bg-hover,rgba(128,128,128,.15))}' +
      'body.mvir-drag,body.mvir-drag *{cursor:nwse-resize!important;user-select:none!important}' +
      '[contenteditable] img{cursor:pointer}[contenteditable] img::selection{background:transparent}';   // рамка замість синього виділення
    document.head.appendChild(s);
  }

  function button(text, title, fn) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    b.title = title;
    b.addEventListener('pointerdown', function (e) { e.preventDefault(); e.stopPropagation(); });
    b.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      if (!img) return;
      fn(img);
      changed(img);
      place();
    });
    return b;
  }

  function build() {
    css();
    box = document.createElement('div');
    box.id = 'mvir';
    box.setAttribute('translate', 'no');
    ['nw', 'ne', 'sw', 'se'].forEach(function (c) {
      var h = document.createElement('span');
      h.className = 'mvir-h ' + c;
      h.dataset.c = c;
      h.addEventListener('pointerdown', startDrag);
      box.appendChild(h);
    });
    var bar = document.createElement('div');
    bar.className = 'mvir-bar';
    [25, 50, 75, 100].forEach(function (p) {
      bar.appendChild(button(p + '%', 'Ширина картинки — ' + p + '% ширини тексту листа', function (el) {
        apply(el, maxWidth(el) * p / 100);
      }));
    });
    bar.appendChild(button('1:1', 'Справжній розмір картинки (не ширше за текст листа)', function (el) {
      apply(el, Math.min(el.naturalWidth || maxWidth(el), maxWidth(el)));
    }));
    bar.appendChild(button('🗑', 'Прибрати картинку з листа', function (el) {
      var ed = editorOf(el);
      el.remove();
      if (ed) ed.dispatchEvent(new Event('input', {bubbles: true}));
      hide();
    }));
    box.appendChild(bar);
    var size = document.createElement('span');
    size.className = 'mvir-size';
    box.appendChild(size);
    document.body.appendChild(box);
  }

  function place() {
    if (!box || !img) return;
    if (!img.isConnected) { hide(); return; }
    var r = img.getBoundingClientRect();
    var ed = editorOf(img);
    var er = ed ? ed.getBoundingClientRect() : {top: 0, bottom: innerHeight, left: 0, right: innerWidth};
    var visible = r.bottom > er.top && r.top < er.bottom && r.right > er.left && r.left < er.right;
    box.style.display = visible ? 'block' : 'none';
    box.style.left = r.left + 'px';
    box.style.top = r.top + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
    box.classList.toggle('mvir-in', r.top < 40 || r.height < 60 || r.top - er.top < 36);
    box.querySelector('.mvir-size').textContent = Math.round(r.width) + ' × ' + Math.round(r.height);
  }
  function schedule() {
    if (raf || !img) return;
    raf = requestAnimationFrame(function () { raf = 0; place(); });
  }

  function select(el) {
    if (!box) build();
    img = el;
    place();
    try {   // Delete / Backspace і заміна текстом — як для звичайного виділення
      var range = document.createRange();
      range.selectNode(el);
      var sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* поле без виділення — рамка все одно діє */ }
  }
  function hide() {
    img = null;
    if (box) box.style.display = 'none';
  }

  function startDrag(e) {
    if (!img) return;
    e.preventDefault();
    e.stopPropagation();
    var c = e.currentTarget.dataset.c;
    drag = {sign: c === 'nw' || c === 'sw' ? -1 : 1, x: e.clientX, w: img.getBoundingClientRect().width,
            max: maxWidth(img), id: e.pointerId, el: e.currentTarget};
    try { drag.el.setPointerCapture(e.pointerId); } catch (err) { /* старий браузер */ }
    document.body.classList.add('mvir-drag');
  }
  function moveDrag(e) {
    if (!drag || !img) return;
    e.preventDefault();
    apply(img, Math.min(drag.max, drag.w + drag.sign * (e.clientX - drag.x)));
    place();
  }
  function endDrag() {
    if (!drag) return;
    try { drag.el.releasePointerCapture(drag.id); } catch (err) { /* уже відпущено */ }
    drag = null;
    document.body.classList.remove('mvir-drag');
    if (img) { changed(img); select(img); }
  }

  // Вставлена картинка ширша за текст листа — вписати в ширину (у листі теж: атрибут width)
  function fitNew(ed, before) {
    Array.prototype.forEach.call(ed.querySelectorAll('img'), function (el) {
      if (before.indexOf(el) >= 0 || el.getAttribute('width')) return;
      var fit = function () {
        var max = maxWidth(el);
        if (el.naturalWidth > max) { apply(el, max); changed(el); }
      };
      if (el.complete && el.naturalWidth) fit();
      else el.addEventListener('load', fit, {once: true});
    });
  }

  document.addEventListener('pointerdown', function (e) {
    if (box && box.contains(e.target)) return;
    if (editable(e.target)) { select(e.target); return; }
    if (img) hide();
  }, true);
  document.addEventListener('pointermove', moveDrag, true);
  document.addEventListener('pointerup', endDrag, true);
  document.addEventListener('pointercancel', endDrag, true);
  // клік на картинці не має ставити курсор поруч (і знімати виділення)
  document.addEventListener('click', function (e) {
    if (editable(e.target) && img === e.target) select(e.target);
  }, true);
  document.addEventListener('keydown', function (e) {
    if (!img) return;
    if (e.key === 'Escape') { hide(); return; }
    setTimeout(function () { if (img && !img.isConnected) hide(); else schedule(); }, 0);
  }, true);
  document.addEventListener('paste', function (e) {
    var ed = editorOf(e.target);
    if (!ed || !ed.isContentEditable) return;
    var before = Array.prototype.slice.call(ed.querySelectorAll('img'));
    setTimeout(function () { fitNew(ed, before); }, 0);
  }, true);
  document.addEventListener('input', schedule, true);
  window.addEventListener('scroll', schedule, true);
  window.addEventListener('resize', schedule);
  try { document.execCommand('enableObjectResizing', false, 'false'); } catch (e) { /* Firefox: свої кути — вимкнути */ }

  window.MVImgResize = {select: select, hide: hide, apply: apply, fitNew: fitNew};
})();
