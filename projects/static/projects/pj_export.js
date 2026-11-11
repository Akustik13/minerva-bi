/* 📤 Діаграма проєкту як на екрані → PNG на білому тлі (для PDF, листа, завантаження).
   Стилі вбудовуються в копію SVG (getComputedStyle), кольори — світла палітра на час знімка (змінні теми
   перевизначено лише на контейнері діаграми), занадто світлий текст — темним.
   PJExport.png([svg, …]) → Promise<Blob> (кілька SVG — поруч зліва направо, як класична діаграма Ганта);
   PJExport.save(blob, name); PJExport.fileName(title, kind, ext). */
(function () {
  'use strict';
  var LIGHT = {
    '--body-fg': '#1f2328', '--text': '#1f2328', '--text-muted': '#57606a', '--text-dim': '#6e7781',
    '--body-bg': '#ffffff', '--bg-card': '#ffffff', '--darkened-bg': '#f6f8fa', '--body-quiet-color': '#57606a',
    '--border-strong': '#8c959f', '--border-color': '#d0d7de', '--hairline-color': '#d8dee4',
    '--accent': '#0969da', '--accent2-subtle': '#ddf4ff', '--accent2-hover': '#0550ae', '--primary': '#0969da',
    '--ok': '#1a7f37', '--warn': '#9a6700', '--err': '#cf222e'
  };
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
  function png(svgs) {
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
        if (!c.getAttribute('viewBox')) c.setAttribute('viewBox', '0 0 ' + sz.w + ' ' + sz.h);
        parts.push({xml: new XMLSerializer().serializeToString(c), w: sz.w, h: sz.h});
      });
    } finally {
      keys.forEach(function (k) {
        if (saved[k]) holder.style.setProperty(k, saved[k]); else holder.style.removeProperty(k);
      });
    }
    var W = parts.reduce(function (s, p) { return s + p.w; }, 0);
    var H = parts.reduce(function (s, p) { return Math.max(s, p.h); }, 0);
    var scale = Math.min(SCALE, MAX_PX / Math.max(W, H, 1));
    return Promise.all(parts.map(function (p) { return load(p.xml); })).then(function (imgs) {
      var cv = document.createElement('canvas');
      cv.width = Math.ceil(W * scale);
      cv.height = Math.ceil(H * scale);
      var ctx = cv.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.scale(scale, scale);
      var x = 0;
      imgs.forEach(function (img, i) {
        ctx.drawImage(img, x, 0, parts[i].w, parts[i].h);
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
