/* 🧾 Кнопка «🧾 Rechnung» у картці замовлення (sales/admin_invoices.order_button): клік — за налаштуванням
 * «Клік «🧾 Rechnung» у картці замовлення» (⚡ одразу — POST /rechnungen/from-order/<pk>/, або форма),
 * правий клік — інші варіанти (window.MVMenu, static/admin/js/mv_menu.js). */
(function(){
  if (window._riOrderBtn) return; window._riOrderBtn = 1;
  function post(url, extra, target){
    var tok = (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value ||
              (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
    var f = document.createElement('form'); f.method = 'post'; f.action = url; f.style.display = 'none';
    if (target) f.target = target;
    var fields = Object.assign({csrfmiddlewaretoken: tok}, extra || {});
    Object.keys(fields).forEach(function(k){
      var i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = fields[k]; f.appendChild(i);
    });
    document.body.appendChild(f); f.submit(); f.remove();
  }
  function quick(a){
    if (a.dataset.busy) return;
    a.dataset.busy = '1'; a.textContent = '⏳ Формую рахунок…';
    post(a.dataset.riQuick);
  }
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('[data-ri-quick]');
    if (!a || e.button || e.ctrlKey || e.metaKey || e.shiftKey || a.dataset.riMode !== 'quick') return;
    e.preventDefault(); quick(a);
  });
  document.addEventListener('contextmenu', function(e){
    var a = e.target.closest && e.target.closest('[data-ri-menu]');
    if (!a || e.shiftKey || !window.MVMenu) return;
    e.preventDefault();
    var d = a.dataset, items = [];
    if (d.riQuick) {
      items.push({label: '⚡ Сформувати рахунок одразу', hint: d.riMode === 'quick' ? 'клік' : '',
                  onSelect: function(){ quick(a); }});
      items.push({label: '✏️ Відкрити форму рахунку', hint: d.riMode === 'form' ? 'клік' : '', href: d.riForm});
    } else {
      items.push({label: '✏️ Відкрити рахунок', href: d.riOpen});
      if (d.riPdf) items.push({label: '📄 Відкрити PDF', href: d.riPdf, target: '_blank'});
      if (d.riRegen) items.push({label: '♻ Перегенерувати й відкрити PDF',
                                 onSelect: function(){ post(d.riRegen, {open: 'pdf'}, '_blank'); }});
      if (d.riMail) items.push({label: '✉️ Надіслати покупцю', href: d.riMail, target: '_blank'});
      items.push({label: '＋ Ще один рахунок', href: d.riAgain});
    }
    items.push({type: 'separator'});
    items.push({label: '⚙️ Що робить клік…', href: '/admin/sales/invoicesettings/'});
    MVMenu.open(e, items, {title: '🧾 Рахунок'});
  });
})();
