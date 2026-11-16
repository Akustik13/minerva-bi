'use strict';
(function () {
  const api = window.minervaSetup;
  const $ = (id) => document.getElementById(id);
  const form = $('f'), go = $('go'), err = $('err'), cert = $('cert');

  function show(el, text) { el.hidden = !text; el.textContent = text || ''; }

  api.init().then((d) => {
    if (!d) return;
    if (d.server) { $('server').value = d.server; $('cancel').hidden = false; $('code').focus(); }
    else $('server').focus();
    $('ver').textContent = 'Версія ' + d.version + '.';
  });

  $('cancel').addEventListener('click', () => api.cancel());
  $('server').addEventListener('input', () => { cert.hidden = true; $('trust').checked = false; });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    go.disabled = true;
    go.textContent = 'Перевіряю…';
    show(err, '');
    api.connect({ server: $('server').value, code: $('code').value, trust: $('trust').checked })
      .then((r) => {
        if (r && r.ok) { go.textContent = '✓'; return; }
        go.disabled = false;
        go.textContent = 'Підключити';
        if (r && r.cert) {
          $('cert-text').textContent = 'Сертифікат: ' + (r.cert.subject || '') + ' · видав ' + (r.cert.issuer || '')
            + ' · відбиток ' + (r.cert.fingerprint || '');
          cert.hidden = false;
        }
        show(err, (r && r.error) || 'Не вдалося підключитися.');
      })
      .catch((x) => { go.disabled = false; go.textContent = 'Підключити'; show(err, String(x)); });
  });
})();
