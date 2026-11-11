"""
inventory/admin_weight.py — «⚖️ Вага з даташита»: запити для картки товару й сторінки відправлення.

POST …/product/<pk>/weight-check/ — шукає вагу в даташиті (inventory/services/datasheet_weight.check) → JSON;
POST …/product/<pk>/weight-save/  {grams, source datasheet|manual, note} → записує в товар → JSON.
Право — змінювати товари (`inventory.change_product`).
"""
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import Product
from .services import datasheet_weight


def _num(g):
    return f"{g.normalize():f}" if g is not None else ""


def state_json(product):
    s = datasheet_weight.status(product)
    return {"grams": _num(s["grams"]) if s["grams"] else "", "source": s["source"],
            "source_text": s["source_text"], "note": s["note"]}


def _refused(request, product=None):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Лише POST"}, status=405)
    if not request.user.has_perm("inventory.change_product"):
        return JsonResponse({"ok": False, "error": "Немає права змінювати товари"}, status=403)
    return None


def check_view(request, pk):
    refused = _refused(request)
    if refused:
        return refused
    product = get_object_or_404(Product, pk=pk)
    result = datasheet_weight.check(product)
    product.refresh_from_db()
    cands = [{"grams": _num(c["grams"]), "quote": c["quote"], "page": c["page"], "hits": c["hits"]}
             for c in result["candidates"]]
    return JsonResponse({"ok": True, "status": result["status"], "candidates": cands, "where": result["where"],
                         "error": result["error"], "state": state_json(product)})


def save_view(request, pk):
    refused = _refused(request)
    if refused:
        return refused
    product = get_object_or_404(Product, pk=pk)
    try:
        g = datasheet_weight.save(product, request.POST.get("grams", ""), request.POST.get("source", "manual"),
                                  request.POST.get("note", "").strip())
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)
    return JsonResponse({"ok": True, "grams": _num(g), "kg": f"{(g / 1000).normalize():f}",
                         "sku": product.sku, "state": state_json(product)})


def urls(admin_site):
    from django.urls import path
    return [
        path("<int:pk>/weight-check/", admin_site.admin_view(check_view), name="inventory_product_weight_check"),
        path("<int:pk>/weight-save/", admin_site.admin_view(save_view), name="inventory_product_weight_save"),
    ]


def panel(product, can_change):
    """Блок у картці товару (група «🛃 Митне оформлення»): стан ваги й «🔎 Знайти вагу в даташиті»."""
    if not product or not product.pk:
        return "—"
    s = datasheet_weight.status(product)
    head = (format_html("<b>{} г/шт</b> · {}", _num(s["grams"]), s["source_text"] or "звідки — невідомо")
            if s["grams"] else format_html('<span style="color:var(--warn,#ef6c00)">⚠️ Ваги немає</span>'))
    note = format_html('<div style="color:var(--text-muted,#90a4ae);font-size:12px;margin-top:2px">{}</div>',
                       s["note"]) if s["note"] else ""
    button = ""
    if can_change:
        button = format_html(
            '<div style="margin-top:6px"><button type="button" class="button" data-dsw-check="{}" '
            'data-dsw-save="{}" data-dsw-sku="{}">🔎 Знайти вагу в даташиті</button>'
            '<div class="dsw-out" style="margin-top:6px;font-size:12.5px"></div></div>',
            reverse("admin:inventory_product_weight_check", args=[product.pk]),
            reverse("admin:inventory_product_weight_save", args=[product.pk]), product.sku)
    return format_html('<div class="dsw-panel" translate="no">{}{}{}</div>{}', head, note, button,
                       SCRIPT if can_change else "")


# Спільний скрипт (картка товару й сторінка відправлення): кнопки з data-dsw-check / data-dsw-save.
# Подія document «dsw:saved» {sku, kg, grams} — сторінка відправлення підставляє вагу в митну декларацію.
SCRIPT = mark_safe(r"""<script>
(function(){
  if (window.__dsw) return; window.__dsw = true;
  function csrf(){ var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function post(url, data){
    var fd = new FormData(); for (var k in data) fd.append(k, data[k]);
    return fetch(url, {method:'POST', body:fd, headers:{'X-CSRFToken':csrf()}, credentials:'same-origin'})
      .then(function(r){ return r.json().then(function(j){ j._status = r.status; return j; }); });
  }
  function el(tag, text, css){ var e = document.createElement(tag); if (text) e.textContent = text; if (css) e.style.cssText = css; return e; }
  function saved(btn, out, j){
    out.textContent = '';
    out.appendChild(el('span', '✅ Записано в товар: ' + j.grams + ' г/шт · ' + j.state.source_text, 'color:var(--ok,#2e7d32);font-weight:600'));
    document.dispatchEvent(new CustomEvent('dsw:saved', {detail:{sku:j.sku, kg:j.kg, grams:j.grams}}));
    var row = btn.closest('[data-dsw-row]'); if (row) row.setAttribute('data-dsw-done', '1');
  }
  function manual(btn, out, hint){
    var box = el('div', '', 'margin-top:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap');
    if (hint) box.appendChild(el('span', hint, 'color:var(--text-muted)'));
    var inp = el('input', '', 'width:90px'); inp.type = 'number'; inp.step = 'any'; inp.min = '0'; inp.placeholder = 'г/шт';
    var b = el('button', '💾 Вписати вручну'); b.type = 'button'; b.className = 'button';
    b.onclick = function(){
      if (!inp.value) { inp.focus(); return; }
      post(btn.dataset.dswSave, {grams: inp.value, source: 'manual'}).then(function(j){
        if (!j.ok) { alert(j.error || 'Не вдалося'); return; } saved(btn, out, j);
      });
    };
    inp.addEventListener('keydown', function(e){ if (e.key === 'Enter') { e.preventDefault(); b.click(); } });
    box.appendChild(inp); box.appendChild(b); out.appendChild(box);
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-dsw-check]'); if (!btn) return;
    e.preventDefault();
    var out = btn.parentNode.querySelector('.dsw-out') || btn.closest('[data-dsw-row]').querySelector('.dsw-out');
    out.textContent = '⏳ Читаю даташит…'; btn.disabled = true;
    post(btn.dataset.dswCheck, {}).then(function(j){
      btn.disabled = false; out.textContent = '';
      if (!j.ok) { out.textContent = '❌ ' + (j.error || 'Помилка'); return; }
      if (j.status === 'found') {
        out.appendChild(el('div', '📄 Знайдено в даташиті — перевірте й внесіть:', 'color:var(--text-muted)'));
        j.candidates.forEach(function(c){
          var line = el('div', '', 'margin:4px 0;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap');
          var b = el('button', '💾 ' + c.grams + ' г/шт'); b.type = 'button'; b.className = 'button';
          b.onclick = function(){
            post(btn.dataset.dswSave, {grams: c.grams, source: 'datasheet', note: '«' + c.quote + '» (с. ' + c.page + ')'})
              .then(function(r){ if (!r.ok) { alert(r.error || 'Не вдалося'); return; } saved(btn, out, r); });
          };
          line.appendChild(b);
          line.appendChild(el('span', '«' + c.quote + '» · с. ' + c.page + (c.hits > 1 ? ' · ×' + c.hits : ''), 'color:var(--text-muted)'));
          out.appendChild(line);
        });
        manual(btn, out, 'або інше значення:');
      } else if (j.status === 'none') {
        out.appendChild(el('div', '📄 У даташиті ваги немає — позначено в товарі.', 'color:var(--warn,#ef6c00)'));
        manual(btn, out, '');
      } else {
        out.appendChild(el('div', '📄 Даташита немає або він не читається: ' + (j.error || ''), 'color:var(--warn,#ef6c00)'));
        manual(btn, out, '');
      }
    }).catch(function(){ btn.disabled = false; out.textContent = '❌ Немає зв’язку з сервером'; });
  });
})();
</script>""")
