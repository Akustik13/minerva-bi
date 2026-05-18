/**
 * document_template_check.js
 * Validation buttons for DocumentTemplate admin changelist.
 */

async function checkDocTemplate(pk) {
  const resultEl = document.getElementById('dtc-result-' + pk);
  if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-dim)">⏳</span>';

  try {
    const r = await fetch(`/documents/template/${pk}/check/`);
    const d = await r.json();

    if (!resultEl) return;

    if (!d.ok) {
      const icon   = d.syntax_error ? '⚠️' : '✗';
      const dlUrl  = `/documents/template/${pk}/check-download/`;
      const dlLink = d.syntax_error
        ? `<a href="${dlUrl}" download title="Завантажити .docx з підсвіченими помилками"
              style="padding:2px 7px;border-radius:4px;font-size:10px;margin-left:6px;
                     border:1px solid #ff9800;color:#ff9800;text-decoration:none">⬇ docx</a>`
        : '';
      resultEl.innerHTML =
        `<span style="color:var(--err);font-size:11px">${icon} ${d.error}</span>${dlLink}`;
      return;
    }

    if (!d.issues || !d.issues.length) {
      resultEl.innerHTML =
        '<span style="color:var(--ok);font-size:11px;font-weight:600">✓ OK</span>';
      return;
    }

    const dlUrl  = `/documents/template/${pk}/check-download/`;
    const badges = d.issues.slice(0, 4).map(i => {
      const arrow = (i.suggestion && i.suggestion !== i.var)
        ? ` <span style="color:var(--text-dim)">→ {{${i.suggestion}}}</span>`
        : '';
      return `<span style="background:rgba(244,67,54,.12);padding:1px 5px;border-radius:3px;
        color:var(--err);font-size:10px;white-space:nowrap">{{${i.var}}}${arrow}</span>`;
    }).join(' ');
    const more = d.issues.length > 4
      ? `<span style="color:var(--text-dim);font-size:10px"> +${d.issues.length - 4}</span>` : '';

    resultEl.innerHTML =
      `<span style="color:#ff9800;font-size:11px;font-weight:600">⚠️ ${d.issues.length}</span>
       <span style="display:inline-flex;flex-wrap:wrap;gap:3px;align-items:center;margin-left:4px">
         ${badges}${more}
         <a href="${dlUrl}" download title="Завантажити .docx з позначками"
            style="padding:2px 7px;border-radius:4px;font-size:10px;
                   border:1px solid #ff9800;color:#ff9800;text-decoration:none">⬇ docx</a>
       </span>`;
  } catch (e) {
    if (resultEl) resultEl.innerHTML =
      '<span style="color:var(--err);font-size:11px">✗ Помилка</span>';
  }
}
