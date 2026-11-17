/* 📈 Генератор ступенів цін — спільний для картки товару (change_form.html) і масового редактора
   (base_prices_editor.html), вставляється include-тегом усередину їхніх скриптів.
   Кількості + знижка на найбільшу кількість → знижка для кожної кількості:
   «плавно» — за логарифмом кількості (як ступені на DigiKey: на малих кількостях знижка росте швидше),
   «рівними кроками» — однаковий крок знижки між сусідніми ступенями. */
var TIER_PRESETS = [
  { label: 'Як на DigiKey', q: [1, 10, 25, 50, 100, 250, 500, 1000] },
  { label: '1 · 10 · 100 · 1000', q: [1, 10, 100, 1000] },
  { label: 'Малі партії', q: [1, 5, 10, 25, 50, 100] },
  { label: 'Великі партії', q: [1, 100, 500, 1000, 2500, 5000, 10000] }
];
var TIER_DK_MAX = 9;              // DigiKey Marketplace приймає до 9 ступенів

function tierQtys(text) {         // «1, 10, 25 …» → [1, 10, 25]: цілі від 1, без повторів, за зростанням, 1 — завжди
  var seen = { 1: true }, out = [1];
  String(text || '').split(/[^\d]+/).forEach(function (s) {
    var n = parseInt(s, 10);
    if (n >= 1 && n <= 10000000 && !seen[n]) { seen[n] = true; out.push(n); }
  });
  return out.sort(function (a, b) { return a - b; });
}

function tierPlan(qtys, base, maxDisc, curve, round) {
  /* → {rows: [{q, p, d}] з 1 шт., dropped: [кількості, де після округлення ціна та сама, що й у меншої]}.
     Ціна найбільшої кількості — рівно з maxDisc; знижка d — для показу, до 0,1 %. */
  var top = qtys[qtys.length - 1], rest = qtys.filter(function (q) { return q > 1; });
  var rows = [{ q: 1, p: round(base), d: 0 }], dropped = [];
  rest.forEach(function (q, i) {
    var k = curve === 'even' ? (i + 1) / rest.length : (top > 1 ? Math.log(q) / Math.log(top) : 1);
    var p = round(base * (100 - maxDisc * k) / 100), prev = rows[rows.length - 1];
    if (!(p > 0) || p >= prev.p) { dropped.push(q); return; }
    rows.push({ q: q, p: p, d: Math.round((1 - p / base) * 1000) / 10 });
  });
  return { rows: rows, dropped: dropped };
}

function tierPresetsHtml(extra) { // кнопки наборів кількостей (+ «як у шаблоні магазину», якщо він є)
  return TIER_PRESETS.concat(extra || []).map(function (p) {
    return '<button type="button" class="g-preset" data-q="' + p.q.join(', ') + '" title="' + p.q.join(' · ') + ' шт.">' + p.label + '</button>';
  }).join('');
}
