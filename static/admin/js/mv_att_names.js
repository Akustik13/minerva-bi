/* 📎 Які файли названо в тексті листа — «im Anhang: Gerber TM270402-04H _4L_7628_1x34x85.zip, BOM_….xlsx».
   Сторінка «Написати» («📂 З Minerva») і швидка відповідь у прев'ю («📎») додають їх у вкладення самі.

   MVAttNames.key(назва)            → без регістру й розділювачів («TM270402-04H _4L.zip» → «tm27040204h4lzip»);
   MVAttNames.mentioned(текст, opts) → ті opts ({name, …}), чию назву (або назву без розширення від 8 знаків) є в тексті;
     довша назва, що містить коротшу («PandP_<назва ZIP>.txt»), коротшої не додає; той самий файл у двох групах
     (файли запиту й вкладення гілки) — лише перший.
   MVAttNames.filter(поле)  — «🔎 Знайти файл за назвою…» у списку: решту файлів, порожні групи й проєкти сховано;
   MVAttNames.all(кнопка)   — «＋ Додати всі» групи: клік по кожному ще не доданому видимому файлу. */
(function () {
  'use strict';

  function key(s) {
    return String(s || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');
  }

  function mentioned(text, opts) {
    var t = key(text), found = [];
    (opts || []).forEach(function (o, i) {
      var name = String(o.name || '');
      [[key(name), 6], [key(name.replace(/\.[^.]{1,6}$/, '')), 8]].forEach(function (p) {
        if (p[0].length < p[1]) return;
        for (var at = t.indexOf(p[0]); at >= 0; at = t.indexOf(p[0], at + 1)) {
          found.push({i: i, a: at, b: at + p[0].length});
        }
      });
    });
    found.sort(function (x, y) { return (y.b - y.a) - (x.b - x.a) || x.i - y.i; });   // довші — першими
    var taken = [], hit = {};
    found.forEach(function (c) {
      if (taken.some(function (k) { return k.i !== c.i && k.a <= c.a && c.b <= k.b; })) return;   // усередині іншої
      taken.push(c);
      hit[c.i] = true;
    });
    return (opts || []).filter(function (o, i) { return hit[i]; });
  }

  function filter(input) {
    var q = key(input.value), box = input.parentNode;
    box.querySelectorAll('.em-att-opt[data-ref]').forEach(function (o) {
      o.hidden = !!q && key(o.dataset.name).indexOf(q) < 0;
    });
    box.querySelectorAll('.em-att-part, .em-att-grp, details.em-att-proj').forEach(function (g) {
      var any = g.querySelector('.em-att-opt[data-ref]:not([hidden])');
      g.hidden = !!q && !any;
      if (q && any && g.tagName === 'DETAILS') g.open = true;
    });
  }

  function all(btn) {
    btn.closest('.em-att-grp').querySelectorAll('.em-att-opt[data-ref]').forEach(function (o) {
      if (!o.hidden && !o.classList.contains('added')) o.click();
    });
  }

  window.MVAttNames = {key: key, mentioned: mentioned, filter: filter, all: all};
})();
