/**
 * document_template_check.js
 * Validation buttons for DocumentTemplate admin changelist.
 */

async function checkDocTemplate(pk) {
  const resultEl = document.getElementById('dtc-result-' + pk);
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-dim)">⏳</span>';

  const dlUrl = `/documents/template/${pk}/check-download/`;

  try {
    const r = await fetch(`/documents/template/${pk}/check/`);
    const d = await r.json();

    if (!resultEl) return;

    const dlBtn = `<a href="${dlUrl}" download
      style="display:inline-block;margin-top:5px;padding:3px 10px;border-radius:5px;
             font-size:10px;border:1px solid #ff9800;color:#ff9800;
             text-decoration:none;white-space:nowrap">⬇ Завантажити з позначками</a>`;

    if (!d.ok) {
      const icon = d.syntax_error ? '⚠️' : '✗';
      resultEl.innerHTML =
        `<div style="font-size:11px;color:var(--err)">${icon} ${d.error}</div>` +
        (d.syntax_error ? dlBtn : '');
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
      dlBtn;

  } catch (e) {
    if (resultEl) resultEl.innerHTML =
      '<span style="color:var(--err);font-size:11px">✗ Помилка</span>';
  }
}
