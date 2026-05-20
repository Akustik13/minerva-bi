/**
 * document_template_check.js
 * Validation buttons for DocumentTemplate admin changelist.
 */

async function checkDocTemplateDetail(pk) {
  const resultEl = document.getElementById('dtcf-result-' + pk);
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-dim)">⏳ Перевіряємо…</span>';

  const dlUrl  = `/documents/template/${pk}/check-download/`;
  const fixUrl = `/documents/template/${pk}/auto-fix/`;

  try {
    const r = await fetch(`/documents/template/${pk}/check/`);
    const d = await r.json();
    if (!resultEl) return;

    const dlBtn = `<a href="${dlUrl}" download
      style="display:inline-block;padding:5px 12px;border-radius:5px;
             font-size:12px;border:1px solid #ff9800;color:#ff9800;
             text-decoration:none;white-space:nowrap">⬇ Перевірити і завантажити</a>`;
    const fixBtn = `<a href="${fixUrl}" download
      style="display:inline-block;padding:5px 12px;border-radius:5px;font-size:12px;
             border:1px solid var(--ok);color:var(--ok);
             text-decoration:none;white-space:nowrap">🔧 Виправити і завантажити</a>`;

    if (!d.ok) {
      resultEl.innerHTML =
        `<div style="color:var(--err);font-weight:600;margin-bottom:6px">`+
          `${d.syntax_error ? '⚠️ Синтаксична помилка' : '✗ Помилка'}</div>`+
        `<div style="margin-bottom:8px">${d.error}</div>`+
        `<div style="display:flex;gap:8px;flex-wrap:wrap">${dlBtn}${fixBtn}</div>`;
      return;
    }

    if (!d.issues || !d.issues.length) {
      resultEl.innerHTML =
        '<span style="color:var(--ok);font-weight:600">✓ Шаблон коректний — помилок не знайдено</span>';
      return;
    }

    const rows = d.issues.map(i => {
      const fix = (i.suggestion && i.suggestion !== i.var)
        ? `<span style="color:var(--ok)"> → {{${i.suggestion}}}</span>`
        : `<span style="color:var(--text-dim)"> — невідоме поле</span>`;
      return `<div style="padding:2px 0">
        <code style="background:rgba(244,67,54,.12);padding:1px 5px;
          border-radius:3px;color:var(--err)">{{${i.var}}}</code>${fix}</div>`;
    }).join('');

    resultEl.innerHTML =
      `<div style="color:#ff9800;font-weight:600;margin-bottom:6px">`+
        `⚠️ Знайдено ${d.issues.length} невідом${d.issues.length===1?'е поле':'их полів'}</div>`+
      `<div style="margin-bottom:8px">${rows}</div>`+
      `<div style="display:flex;gap:8px;flex-wrap:wrap">${dlBtn}${fixBtn}</div>`;

  } catch (e) {
    if (resultEl) resultEl.innerHTML =
      '<span style="color:var(--err)">✗ Помилка з\'єднання</span>';
  }
}

async function checkDocTemplate(pk) {
  const resultEl = document.getElementById('dtc-result-' + pk);
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-dim)">⏳</span>';

  const dlUrl  = `/documents/template/${pk}/check-download/`;
  const fixUrl = `/documents/template/${pk}/auto-fix/`;

  try {
    const r = await fetch(`/documents/template/${pk}/check/`);
    const d = await r.json();

    if (!resultEl) return;

    const dlBtn = `<a href="${dlUrl}" download
      style="display:inline-block;margin-top:5px;padding:3px 10px;border-radius:5px;
             font-size:10px;border:1px solid #ff9800;color:#ff9800;
             text-decoration:none;white-space:nowrap">⬇ З позначками</a>`;

    const fixBtn = `<a href="${fixUrl}" download
      style="display:inline-block;margin-top:5px;margin-left:5px;padding:3px 10px;border-radius:5px;
             font-size:10px;border:1px solid var(--ok);color:var(--ok);
             text-decoration:none;white-space:nowrap">🔧 Виправлений системою</a>`;

    if (!d.ok) {
      const icon = d.syntax_error ? '⚠️' : '✗';
      resultEl.innerHTML =
        `<div style="font-size:11px;color:var(--err)">${icon} ${d.error}</div>` +
        (d.syntax_error ? `<div style="margin-top:4px">${dlBtn}${fixBtn}</div>` : '');
      return;
    }

    if (!d.issues || !d.issues.length) {
      resultEl.innerHTML =
        '<span style="color:var(--ok);font-size:11px;font-weight:600">✓ OK</span>';
      return;
    }

    const badges = d.issues.slice(0, 3).map(i => {
      const arrow = (i.suggestion && i.suggestion !== i.var)
        ? ` <span style="color:var(--text-dim)">→ {{${i.suggestion}}}</span>`
        : '';
      return `<span style="background:rgba(244,67,54,.12);padding:1px 5px;border-radius:3px;
        color:var(--err);font-size:10px;white-space:nowrap">{{${i.var}}}${arrow}</span>`;
    }).join(' ');
    const more = d.issues.length > 3
      ? `<span style="color:var(--text-dim);font-size:10px"> +${d.issues.length - 3} ще</span>`
      : '';

    resultEl.innerHTML =
      `<div style="font-size:11px;color:#ff9800;font-weight:600;margin-bottom:3px">` +
        `⚠️ ${d.issues.length} невідом${d.issues.length === 1 ? 'е поле' : 'их полів'}` +
      `</div>` +
      `<div style="display:flex;flex-wrap:wrap;gap:3px;margin-bottom:4px">${badges}${more}</div>` +
      `<div>${dlBtn}${fixBtn}</div>`;

  } catch (e) {
    if (resultEl) resultEl.innerHTML =
      '<span style="color:var(--err);font-size:11px">✗ Помилка</span>';
  }
}
