/* 🔍 Масштаб і прокрутка для SVG-діаграм проєктів (карта зв'язків портфеля, сторони й «Як рухалось» у пульті).
   Через viewBox — текст і лінії лишаються чіткими. Ctrl + коліщатко (щипок на тачпаді) — масштаб довкола курсора,
   тягнути порожнє місце — прокрутка, ＋ / − / ⟲ — кнопки в куті. Вузли, посилання й кнопки клацаються як раніше:
   після прокрутки svg.dataset.panned — час, щоб клік, що завершив перетягування, не вважався кліком.
   Початковий viewBox — data-vb0 (його бере експорт, pj_export.js). PJZoom.attach(svg) → {reset, zoom}. */
(function () {
  'use strict';
  var MIN_SHARE = 0.12;           // найбільше наближення: видно 12 % ширини
  var CLICKABLE = 'a, button, [data-n], [data-party], [data-href], g[tabindex], [role="button"]';

  function attach(svg) {
    if (!svg || svg.dataset.zoom) return svg && svg._pjz;
    var vb = svg.viewBox && svg.viewBox.baseVal;
    if (!vb || !vb.width) return null;
    svg.dataset.zoom = '1';
    var base = {x: vb.x, y: vb.y, w: vb.width, h: vb.height}, cur = {x: base.x, y: base.y, w: base.w, h: base.h};
    svg.setAttribute('data-vb0', [base.x, base.y, base.w, base.h].join(' '));

    function set(v) {
      var w = Math.min(Math.max(v.w, base.w * MIN_SHARE), base.w), h = w * base.h / base.w;
      var x = Math.min(Math.max(v.x, base.x), base.x + base.w - w), y = Math.min(Math.max(v.y, base.y), base.y + base.h - h);
      cur = {x: x, y: y, w: w, h: h};
      svg.setAttribute('viewBox', [x, y, w, h].map(function (n) { return Math.round(n * 100) / 100; }).join(' '));
      if (tools) tools.querySelector('[data-z="fit"]').disabled = w >= base.w - 0.5;
    }
    function zoom(f, cx, cy) {
      var r = svg.getBoundingClientRect();
      var px = r.width ? ((cx === undefined ? r.left + r.width / 2 : cx) - r.left) / r.width : 0.5;
      var py = r.height ? ((cy === undefined ? r.top + r.height / 2 : cy) - r.top) / r.height : 0.5;
      var w = Math.min(Math.max(cur.w / f, base.w * MIN_SHARE), base.w), h = w * base.h / base.w;
      set({x: cur.x + (cur.w - w) * px, y: cur.y + (cur.h - h) * py, w: w, h: h});
    }
    function reset() { set({x: base.x, y: base.y, w: base.w, h: base.h}); }

    svg.addEventListener('wheel', function (e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      zoom(Math.exp(-Math.max(Math.min(e.deltaY, 200), -200) * 0.0025), e.clientX, e.clientY);
    }, {passive: false});

    svg.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || (e.target.closest && e.target.closest(CLICKABLE))) return;
      var r = svg.getBoundingClientRect(), x0 = e.clientX, y0 = e.clientY, start = {x: cur.x, y: cur.y}, moved = false;
      function mv(e2) {
        var dx = e2.clientX - x0, dy = e2.clientY - y0;
        if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
        if (!moved) { moved = true; svg.classList.add('pjz-grab'); try { svg.setPointerCapture(e.pointerId); } catch (x) { /* ні */ } }
        set({x: start.x - dx * cur.w / r.width, y: start.y - dy * cur.h / r.height, w: cur.w, h: cur.h});
      }
      function up() {
        svg.removeEventListener('pointermove', mv);
        svg.removeEventListener('pointerup', up);
        svg.removeEventListener('pointercancel', up);
        svg.classList.remove('pjz-grab');
        if (moved) svg.dataset.panned = String(Date.now());
      }
      svg.addEventListener('pointermove', mv);
      svg.addEventListener('pointerup', up);
      svg.addEventListener('pointercancel', up);
    });

    var holder = svg.parentElement, tools = null;
    if (holder) {
      if (getComputedStyle(holder).position === 'static') holder.style.position = 'relative';
      tools = document.createElement('div');
      tools.className = 'pjz-tools';
      [['in', '＋', 'Наблизити (Ctrl + коліщатко)'], ['out', '－', 'Віддалити'], ['fit', '⟲', 'Усе на екрані']]
        .forEach(function (b) {
          var x = document.createElement('button');
          x.type = 'button';
          x.textContent = b[1];
          x.title = b[2];
          x.setAttribute('aria-label', b[2]);
          x.dataset.z = b[0];
          tools.appendChild(x);
        });
      tools.addEventListener('click', function (e) {
        var b = e.target.closest('[data-z]');
        if (!b) return;
        e.preventDefault();
        e.stopPropagation();
        if (b.dataset.z === 'in') zoom(1.5); else if (b.dataset.z === 'out') zoom(1 / 1.5); else reset();
      });
      holder.appendChild(tools);
      tools.querySelector('[data-z="fit"]').disabled = true;
    }
    svg._pjz = {reset: reset, zoom: zoom};
    return svg._pjz;
  }
  function justPanned(svg) {
    return !!(svg && svg.dataset.panned && Date.now() - (+svg.dataset.panned) < 350);
  }
  window.PJZoom = {attach: attach, justPanned: justPanned};
})();
