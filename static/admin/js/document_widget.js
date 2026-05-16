/**
 * document_widget.js
 * Універсальний віджет генерації документів для Admin сторінок.
 */

window.DocumentWidget = {
  module:   null,
  objectPk: null,
  csrf:     null,

  init(module, objectPk) {
    this.module   = module;
    this.objectPk = objectPk;
    this.csrf     = (document.cookie.split(';')
      .find(c => c.trim().startsWith('csrftoken=')) || '').split('=')[1] || '';
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => this._loadAll());
    } else {
      this._loadAll();
    }
  },

  _loadAll() {
    this.loadTemplates('doc-templates-list');
    this.loadDocumentsList('docs-list');
  },

  async loadTemplates(containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    try {
      const r = await fetch(`/documents/templates/?module=${this.module}`);
      const d = await r.json();
      if (!d.templates || !d.templates.length) {
        el.innerHTML =
          '<span style="font-size:12px;color:var(--text-dim)">' +
          'Немає шаблонів. ' +
          '<a href="/admin/documents/documenttemplate/add/" ' +
          'style="color:var(--link-fg)">+ Додати шаблон</a></span>';
        return;
      }
      el.innerHTML = d.templates.map(t =>
        `<button type="button"
                 onclick="DocumentWidget.generate(${t.pk}, '${t.name.replace(/'/g,"\\\'")}')"
                 title="${(t.description||'').replace(/"/g,'&quot;')}"
                 style="padding:6px 14px;border-radius:6px;font-size:12px;
                        border:1px solid var(--border-strong);background:none;
                        color:var(--text);cursor:pointer">
           📄 ${t.name}
         </button>`
      ).join('');
    } catch(e) {
      el.innerHTML = '<span style="color:var(--err);font-size:12px">Помилка завантаження шаблонів</span>';
    }
  },

  async generate(templatePk, templateName) {
    const status = document.getElementById('doc-gen-status');
    if (status) status.innerHTML =
      `<span style="color:var(--text-dim)">⏳ Генерую "${templateName}"...</span>`;

    try {
      const url = this.module === 'sales'
        ? `/documents/order/${this.objectPk}/generate/${templatePk}/`
        : `/documents/order/${this.objectPk}/generate/${templatePk}/`;
      const r = await fetch(url, {
        method: 'POST',
        headers: {'X-CSRFToken': this.csrf},
      });
      const d = await r.json();
      if (d.ok) {
        if (status) {
          status.innerHTML =
            `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:4px">` +
            `<span style="color:var(--ok);font-size:13px">✓ ${templateName} (${d.file_size})</span>` +
            `<a href="${d.docx_url}" download
               style="padding:5px 14px;border-radius:6px;font-size:12px;
                      background:var(--link-fg);color:#fff;text-decoration:none">⬇ Word</a>` +
            (d.has_pdf
              ? `<a href="${d.pdf_url}" download
                    style="padding:5px 14px;border-radius:6px;font-size:12px;
                           background:var(--err);color:#fff;text-decoration:none">⬇ PDF</a>`
              : '') +
            `</div>`;
        }
        this.loadDocumentsList('docs-list');
      } else {
        if (status) status.innerHTML =
          `<span style="color:var(--err);font-size:13px">✗ ${d.error}</span>`;
      }
    } catch(e) {
      if (status) status.innerHTML =
        '<span style="color:var(--err);font-size:13px">✗ Помилка мережі</span>';
    }
  },

  async loadDocumentsList(containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    try {
      const r = await fetch(
        `/documents/list/?module=${this.module}&object_id=${this.objectPk}`);
      const d = await r.json();
      if (!d.documents || !d.documents.length) {
        el.innerHTML =
          '<div style="font-size:12px;color:var(--text-dim);padding:8px 0">' +
          'Немає збережених документів</div>';
        return;
      }
      el.innerHTML = d.documents.map(doc =>
        `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;
                     border-bottom:1px solid var(--border-strong);flex-wrap:wrap">
           <span style="font-size:12px;flex:1;min-width:0;
                        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
                        color:var(--text)">📄 ${doc.name}</span>
           <span style="font-size:11px;color:var(--text-dim)">${doc.size}</span>
           <span style="font-size:11px;color:var(--text-dim)">${doc.date}</span>
           <a href="${doc.docx_url}" download
              style="font-size:11px;color:var(--link-fg)">⬇ Word</a>
           ${doc.has_pdf
             ? `<a href="${doc.pdf_url}" download
                   style="font-size:11px;color:var(--err)">⬇ PDF</a>`
             : ''}
           <button type="button"
                   onclick="DocumentWidget.deleteDoc(${doc.id}, this)"
                   style="font-size:11px;padding:2px 6px;border-radius:4px;
                          border:1px solid var(--err);color:var(--err);
                          background:none;cursor:pointer">✕</button>
         </div>`
      ).join('');
    } catch(e) {}
  },

  async deleteDoc(docId, btn) {
    if (!confirm('Видалити документ з сервера?')) return;
    try {
      const r = await fetch(`/documents/delete/${docId}/`, {
        method: 'POST',
        headers: {'X-CSRFToken': this.csrf},
      });
      const d = await r.json();
      if (d.ok) {
        btn.closest('div[style]').remove();
      } else {
        alert('Помилка: ' + d.error);
      }
    } catch(e) {
      alert('Помилка мережі');
    }
  },
};
