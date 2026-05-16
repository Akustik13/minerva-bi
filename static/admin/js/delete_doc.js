function deleteDoc(pk) {
  if (!confirm('Видалити документ з сервера?')) return;
  const csrf = (document.cookie.split(';')
    .find(c => c.trim().startsWith('csrftoken=')) || '').split('=')[1] || '';
  fetch(`/documents/delete/${pk}/`, {
    method: 'POST',
    headers: {'X-CSRFToken': csrf},
  }).then(r => r.json()).then(d => {
    if (d.ok) location.reload();
    else alert('Помилка: ' + d.error);
  });
}
