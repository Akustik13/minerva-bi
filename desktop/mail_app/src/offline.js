'use strict';
(function () {
  const q = new URLSearchParams(location.search);
  let url = '';
  try {
    const u = new URL(q.get('url') || '');
    if (u.protocol === 'https:' || u.protocol === 'http:') url = u.origin;
  } catch (e) { /* */ }
  document.getElementById('host').textContent = url ? new URL(url).host : '';
  document.getElementById('why').textContent = q.get('err') || '';
  const retry = document.getElementById('retry');
  if (url) retry.href = url + '/email/';
  setTimeout(() => { if (url) location.href = url + '/email/'; }, 30000);
})();
