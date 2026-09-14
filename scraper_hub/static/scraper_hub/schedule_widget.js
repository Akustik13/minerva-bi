/* scraper_hub/static/scraper_hub/schedule_widget.js
   Показує/ховає поля розкладу та IMAP залежно від вибраного типу.
*/
(function () {
  'use strict';

  // ── helpers ────────────────────────────────────────────────────────────────
  function row(fieldName) {
    return document.querySelector('.field-' + fieldName);
  }
  function show(el) { if (el) el.style.display = ''; }
  function hide(el) { if (el) el.style.display = 'none'; }
  function val(id)  { var el = document.getElementById(id); return el ? el.value : ''; }

  // Fieldset title → DOM element
  function fieldset(titleText) {
    var all = document.querySelectorAll('.module h2');
    for (var i = 0; i < all.length; i++) {
      if (all[i].textContent.trim() === titleText) return all[i].closest('fieldset');
    }
    return null;
  }

  // ── main update ────────────────────────────────────────────────────────────
  function update() {
    var siteType  = val('id_site_name');
    var schedType = val('id_schedule_type');

    // schedule_time: show when not manual
    var timeRow = row('schedule_time');
    schedType === 'manual' ? hide(timeRow) : show(timeRow);

    // weekday checkboxes: show only for weekly
    var daysRow = row('schedule_weekdays_select');
    schedType === 'weekly' ? show(daysRow) : hide(daysRow);

    // IMAP fieldset: show only for email
    var imapFs = fieldset('Email / IMAP налаштування');
    if (imapFs) {
      if (siteType === 'email') {
        show(imapFs);
        imapFs.classList.remove('collapsed');
      } else {
        hide(imapFs);
      }
    }

    // Custom scraper fieldset: show only for custom
    var customFs = fieldset('Конфіг власного скрапера');
    if (customFs) {
      siteType === 'custom' ? show(customFs) : hide(customFs);
    }

    // Credentials label hint
    var usernameRow = row('username');
    if (usernameRow) {
      var label = usernameRow.querySelector('label');
      if (label) {
        if (siteType === 'email') {
          label.textContent = 'Email (IMAP логін):';
        } else {
          label.textContent = 'Логін / Email:';
        }
      }
    }
  }

  // ── init ───────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    var siteEl  = document.getElementById('id_site_name');
    var schedEl = document.getElementById('id_schedule_type');

    if (siteEl)  siteEl.addEventListener('change', update);
    if (schedEl) schedEl.addEventListener('change', update);

    // Run immediately
    update();
  });
})();
