/* 📤 Діаграма проєкту як на екрані → PNG на білому тлі (для PDF, листа, завантаження).
   Стилі вбудовуються в копію SVG (getComputedStyle), кольори — світла палітра на час знімка (змінні теми
   перевизначено лише на контейнері діаграми), занадто світлий текст — темним.
   PJExport.png([svg, …]) → Promise<Blob> (кілька SVG — поруч зліва направо, як класична діаграма Ганта);
   PJExport.save(blob, name); PJExport.fileName(title, kind, ext). */
(function () {
  'use strict';
  var LIGHT = {                                  // світла тема Minerva (base_site), фон — білий
    '--body-fg': '#1c2128', '--text': '#1c2128', '--text-muted': '#4b5563', '--text-dim': '#5b636e',
    '--body-bg': '#ffffff', '--bg-card': '#ffffff', '--darkened-bg': '#eef1f5', '--body-quiet-color': '#57606a',
    '--border-strong': '#d0d7de', '--border-color': '#d0d7de', '--hairline-color': 'rgba(0,0,0,.08)',
    '--accent': '#0860c9', '--accent2-subtle': 'rgba(5,80,174,.1)', '--accent2-hover': 'rgba(5,80,174,.06)',
    '--primary': '#0969da', '--ok': '#1a7f37', '--warn': '#9a6700', '--err': '#cf222e'
  };
  var PAD = 28, FONT = '"Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif';
  var PROPS = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-opacity', 'opacity',
               'font-family', 'font-size', 'font-weight', 'font-style', 'text-anchor', 'dominant-baseline',
               'visibility', 'display', 'paint-order', 'stroke-linecap', 'stroke-linejoin'];
  var SCALE = 2, MAX_PX = 12000;

  function rgb(v) {
    var m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(v || '');
    return m ? [+m[1], +m[2], +m[3]] : null;
  }
  function light(v) {
    var c = rgb(v);
    if (!c) return false;
    var l = (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
    return l > 0.72;
  }
  function inline(src, dst) {
    if (src.nodeType !== 1) return;
    var cs = window.getComputedStyle(src), out = [];
    PROPS.forEach(function (p) {
      var v = cs.getPropertyValue(p);
      if (v) out.push(p + ':' + v);
    });
    var tag = src.tagName.toLowerCase();
    if ((tag === 'text' || tag === 'tspan') && light(cs.getPropertyValue('fill'))) out.push('fill:#1f2328');
    dst.setAttribute('style', out.join(';'));
    dst.removeAttribute('tabindex');
    for (var i = 0; i < src.children.length && i < dst.children.length; i++) inline(src.children[i], dst.children[i]);
  }
  function size(svg) {
    var vb0 = (svg.getAttribute('data-vb0') || '').split(' ').map(Number);     // масштаб (pj_zoom.js) — уся діаграма
    if (vb0.length === 4 && vb0[2] > 0) return {w: vb0[2], h: vb0[3], vb: vb0.join(' ')};
    var r = svg.getBoundingClientRect();
    return {w: parseFloat(svg.getAttribute('width')) || r.width || 600,
            h: parseFloat(svg.getAttribute('height')) || r.height || 300};
  }
  function load(xml) {
    return new Promise(function (res, rej) {
      var img = new Image();
      img.onload = function () { res(img); };
      img.onerror = function () { rej(new Error('Не вдалося намалювати діаграму')); };
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
    });
  }
  // Заголовок і легенда над діаграмою: opts {title, sub, legend: [[вид, колір, підпис]]};
  // вид — bar | soft | outline | dashbox | diamond | line | dash | base | text (сам текст — колір, напр. «+N»)
  function legendMark(ctx, kind, color, x, y) {
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    if (kind === 'diamond') {
      ctx.beginPath(); ctx.moveTo(x + 7, y - 7); ctx.lineTo(x + 14, y); ctx.lineTo(x + 7, y + 7); ctx.lineTo(x, y);
      ctx.closePath(); ctx.fill();
    } else if (kind === 'line' || kind === 'dash') {
      ctx.lineWidth = 2;
      if (kind === 'dash') ctx.setLineDash([5, 3]);
      ctx.beginPath(); ctx.moveTo(x + 7, y - 8); ctx.lineTo(x + 7, y + 8); ctx.stroke();
    } else if (kind === 'outline' || kind === 'dashbox') {
      ctx.lineWidth = kind === 'dashbox' ? 1.5 : 2;
      if (kind === 'dashbox') ctx.setLineDash([3, 2]);
      ctx.strokeRect(x, y - 6, 22, 12);
    } else if (kind === 'text') {
      ctx.font = '700 13px ' + FONT; ctx.fillText('+N', x, y + 5);
      ctx.restore();
      return 22;
    } else if (kind === 'base') {
      ctx.globalAlpha = .7; ctx.fillRect(x, y - 2, 22, 4);
    } else {
      ctx.globalAlpha = kind === 'soft' ? .45 : .85; ctx.fillRect(x, y - 6, 22, 12);
    }
    ctx.restore();
    return kind === 'diamond' || kind === 'line' || kind === 'dash' ? 16 : 24;
  }
  function headHeight(opts) {
    if (!opts || !opts.title) return PAD;
    return PAD + 26 + (opts.sub ? 22 : 0) + (opts.legend && opts.legend.length ? 30 : 0) + 16;
  }
  function drawHead(ctx, opts, W) {
    if (!opts || !opts.title) return;
    var y = PAD + 20;
    ctx.fillStyle = '#1c2128';
    ctx.font = '700 20px ' + FONT;
    ctx.fillText(opts.title, PAD, y);
    if (opts.sub) {
      y += 22;
      ctx.fillStyle = '#4b5563';
      ctx.font = '14px ' + FONT;
      ctx.fillText(opts.sub, PAD, y);
    }
    if (opts.legend && opts.legend.length) {
      y += 28;
      var x = PAD;
      ctx.font = '13px ' + FONT;
      opts.legend.forEach(function (it) {
        x += legendMark(ctx, it[0], it[1], x, y - 4) + 6;
        ctx.fillStyle = '#4b5563';
        ctx.fillText(it[2], x, y);
        x += ctx.measureText(it[2]).width + 18;
      });
    }
    ctx.strokeStyle = '#d0d7de';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD, y + 12.5); ctx.lineTo(W - PAD, y + 12.5); ctx.stroke();
  }
  function headWidth(opts) {
    if (!opts || !opts.title) return 0;
    var c = document.createElement('canvas').getContext('2d'), w = 0;
    c.font = '700 20px ' + FONT; w = Math.max(w, c.measureText(opts.title).width);
    c.font = '14px ' + FONT; if (opts.sub) w = Math.max(w, c.measureText(opts.sub).width);
    c.font = '13px ' + FONT;
    var lw = 0;
    (opts.legend || []).forEach(function (it) { lw += 30 + c.measureText(it[2]).width + 18; });
    return Math.max(w, lw) + PAD * 2;
  }
  // 🌐 Мова документа: підписи діаграми (не дані — [translate="no"]) перекладаються словником інтерфейсу Minerva
  // (core/ui_text, /core/ui-dict/<мова>.js) тим самим алгоритмом, що й сторінка (mv_translate.js → MVTranslateKit);
  // місяці класичної діаграми (data-md) — Intl цією мовою. Українська — як на екрані.
  var KIT_URL = '/static/admin/js/mv_translate.js?v=13', dicts = {};
  function kit() {
    if (window.MVTranslateKit) return Promise.resolve(window.MVTranslateKit);
    return new Promise(function (ok) {
      var s = document.createElement('script');
      s.src = KIT_URL;
      s.onload = function () { ok(window.MVTranslateKit || null); };
      s.onerror = function () { ok(null); };
      document.head.appendChild(s);
    });
  }
  function translator(lang) {
    if (!lang || lang === 'uk') return Promise.resolve(null);
    if (dicts[lang]) return dicts[lang];
    dicts[lang] = Promise.all([kit(), fetch('/core/ui-dict/' + encodeURIComponent(lang) + '.js', {credentials: 'same-origin'})
      .then(function (r) { return r.ok ? r.text() : ''; }).catch(function () { return ''; })])
      .then(function (res) {
        var k = res[0], text = res[1].trim().replace(/^window\.MV_UI_DICT=/, '').replace(/;$/, ''), d = null;
        try { d = JSON.parse(text); } catch (e) { d = null; }
        if (!k || !d) { delete dicts[lang]; return null; }     // не вдалось — наступний експорт спробує знову
        return k.make(d);
      });
    return dicts[lang];
  }
  var CYR = /[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]/;
  // 🌐 Назви кроків і фаз, перекладені AI (o.names {назва: переклад}, вікно експорту): [data-mv-tr="exact"] —
  // повна назва в data-mv-full (значок «◆ » попереду лишається), показано скорочено — data-mv-cut символів
  function applyNames(c, names) {
    c.querySelectorAll('[data-mv-tr="exact"]').forEach(function (el) {
      var full = (el.getAttribute('data-mv-full') || el.textContent || '').replace(/\s+/g, ' ').trim();
      var m = /^([^\p{L}\p{N}]*)([\s\S]*)$/u.exec(full), t = names[full] || names[m[2]];
      if (!t) return;
      var out = (names[full] ? '' : m[1]) + t, cut = +el.getAttribute('data-mv-cut') || 0;
      if (cut > 1 && out.length > cut) out = out.slice(0, cut - 1) + '\u2026';
      el.textContent = out;
      el.setAttribute('translate', 'no');       // уже перекладено — словник інтерфейсу не чіпає
    });
  }
  function localize(c, o) {
    if (o && o.names) applyNames(c, o.names);
    if (!o || !o.lang || o.lang === 'uk') return;
    var fm = null;
    try { fm = new Intl.DateTimeFormat(o.lang, {month: 'short', year: 'numeric', timeZone: 'UTC'}); } catch (e) { fm = null; }
    c.querySelectorAll('[data-md]').forEach(function (t) {
      var d = new Date(t.getAttribute('data-md') + 'T00:00:00Z');
      if (fm && !isNaN(d)) t.textContent = fm.format(d);
    });
    if (!o.tr) return;
    var w = document.createTreeWalker(c, NodeFilter.SHOW_TEXT, null), n, list = [];
    while ((n = w.nextNode())) list.push(n);
    list.forEach(function (node) {
      var el = node.parentNode;
      if (!CYR.test(node.nodeValue) || !el || (el.closest && el.closest('[translate="no"],title,style,script'))) return;
      var t = o.tr.node ? o.tr.node(node) : o.tr(node.nodeValue);   // назви кроків і фаз — лише повний збіг
      if (t !== null && t !== undefined) node.nodeValue = t;
    });
  }
  function png(svgs, opts) {
    var holder = (svgs[0] && svgs[0].closest('.pjc')) || document.documentElement;
    var saved = {}, keys = Object.keys(LIGHT).concat(['color']);
    keys.forEach(function (k) { saved[k] = holder.style.getPropertyValue(k); });
    Object.keys(LIGHT).forEach(function (k) { holder.style.setProperty(k, LIGHT[k]); });
    holder.style.setProperty('color', '#1f2328');
    var parts = [];
    try {
      svgs.forEach(function (svg) {
        var sz = size(svg), c = svg.cloneNode(true);
        inline(svg, c);
        localize(c, opts);
        c.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
        c.setAttribute('width', sz.w);
        c.setAttribute('height', sz.h);
        if (sz.vb) c.setAttribute('viewBox', sz.vb);
        else if (!c.getAttribute('viewBox')) c.setAttribute('viewBox', '0 0 ' + sz.w + ' ' + sz.h);
        parts.push({xml: new XMLSerializer().serializeToString(c), w: sz.w, h: sz.h});
      });
    } finally {
      keys.forEach(function (k) {
        if (saved[k]) holder.style.setProperty(k, saved[k]); else holder.style.removeProperty(k);
      });
    }
    var top = headHeight(opts);
    var W = Math.max(parts.reduce(function (s, p) { return s + p.w; }, 0) + PAD * 2, headWidth(opts));
    var H = parts.reduce(function (s, p) { return Math.max(s, p.h); }, 0) + top + PAD;
    var scale = Math.min(SCALE, MAX_PX / Math.max(W, H, 1));
    return Promise.all(parts.map(function (p) { return load(p.xml); })).then(function (imgs) {
      var cv = document.createElement('canvas');
      cv.width = Math.ceil(W * scale);
      cv.height = Math.ceil(H * scale);
      var ctx = cv.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.scale(scale, scale);
      drawHead(ctx, opts, W);
      var x = PAD;
      imgs.forEach(function (img, i) {
        ctx.drawImage(img, x, top, parts[i].w, parts[i].h);
        x += parts[i].w;
      });
      return new Promise(function (res, rej) {
        cv.toBlob(function (b) { if (b) res(b); else rej(new Error('Не вдалося зберегти картинку')); }, 'image/png');
      });
    });
  }
  function save(blob, name) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
  }
  function fileName(title, kind, ext) {
    var d = new Date(), day = d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
    var base = String(title || 'project').split(' · ')[0].replace(/[^\w.-]+/g, '_');
    var word = {plan: 'Projektplan', timeline: 'Projektverlauf', chain: 'Projektbeteiligte'}[kind] || kind;
    return base + '_' + word + '_' + day + '.' + ext;          // як projects/export.py file_name
  }
  window.PJExport = {png: png, save: save, fileName: fileName, translator: translator};
})();
