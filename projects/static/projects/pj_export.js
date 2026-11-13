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
  window.PJExport = {png: png, save: save, fileName: fileName};
})();
