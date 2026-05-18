/**
 * document_widget.js
 * Universальний віджет генерації документів для Admin сторінок.
 */

window.DocumentWidget = {
  module:      null,
  objectPk:    null,
  csrf:        null,
  sourceId:    '',
  orderNumber: '',
  sourceSlug:  'manual',

  init(module, objectPk, opts) {
    this.module   = module;
    this.objectPk = objectPk;
    this.csrf     = (document.cookie.split(';')
      .find(c => c.trim().startsWith('csrftoken=')) || '').split('=')[1] || '';
    if (opts) {
      this.sourceId    = opts.sourceId    || '';
      this.orderNumber = opts.orderNumber || '';
      this.sourceSlug  = opts.sourceSlug  || 'manual';
    }
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
      let url = `/documents/templates/?module=${this.module}`;
      if (this.sourceId) url += `&source_id=${this.sourceId}`;
      const r = await fetch(url);
      const d = await r.json();
      if (!d.templates || !d.templates.length) {
        el.innerHTML =
          '<span style="font-size:12px;color:var(--text-dim)">' +
          'Немає шаблонів. ' +
          '<a href="/admin/documents/documenttemplate/add/" ' +
          'style="color:var(--link-fg)">+ Додати шаблон</a></span>';
        return;
      }
      el.innerHTML = d.templates.map(t => {
        const safeName = t.name.replace(/'/g, "\\'");
        const desc     = (t.description || '').replace(/"/g, '&quot;');
        return (
          `<span style="display:inline-flex;align-items:stretch;gap:0">` +
          `<button type="button"
                   onclick="DocumentWidget.generate(${t.pk},'${safeName}')"
                   title="${desc}"
                   style="padding:6px 14px;border-radius:6px 0 0 6px;font-size:12px;
                          border:1px solid var(--border-strong);border-right:none;
                          background:none;color:var(--text);cursor:pointer">
             📄 ${t.name}
           </button>` +
          `<button type="button"
                   onclick="DocumentWidget.checkTemplate(${t.pk},'${safeName}')"
                   title="Перевірити шаблон"
                   style="padding:6px 8px;border-radius:0 6px 6px 0;font-size:12px;
                          border:1px solid var(--border-strong);background:none;
                          color:var(--text-dim);cursor:pointer">🔍</button>` +
          `</span>`
        );
      }).join('');
    } catch (e) {
      el.innerHTML = '<span style="color:var(--err);font-size:12px">Помилка завантаження шаблонів</span>';
    }
  },

  async generate(templatePk, templateName) {
    const status = document.getElementById('doc-gen-status');
    if (status) status.innerHTML =
      `<span style="color:var(--text-dim)">⏳ Генерую "${templateName}"...</span>`;

    try {
      const url = `/documents/order/${this.objectPk}/generate/${templatePk}/`;
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': this.csrf },
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
            `<span id="dw-local-status" style="font-size:11px;color:var(--text-dim)"></span>` +
            `</div>`;
        }

        // Refresh both doc lists
        this.loadDocumentsList('docs-list');
        if (typeof _refreshDocsPanel === 'function') {
          _refreshDocsPanel(this.objectPk);
        }

        // Local save via MinervaLocalSave (File System Access API)
        const savePairs = [];
        if (d.url && d.copy_filename) savePairs.push([d.url, d.copy_filename]);
        if (d.pdf_copy_url && d.pdf_copy_filename) savePairs.push([d.pdf_copy_url, d.pdf_copy_filename]);

        if (savePairs.length) {
          const lsEl = document.getElementById('dw-local-status');
          const subs = (d.source_slug && d.date_str && d.order_number)
            ? [d.source_slug, d.date_str, d.order_number] : null;

          if (!window.MinervaLocalSave || !MinervaLocalSave.supported) {
            if (lsEl) {
              lsEl.style.color = 'var(--text-dim)';
              lsEl.textContent = '— локальне збереження не підтримується (Firefox/Safari)';
            }
          } else {
            if (lsEl) lsEl.textContent = '💾 Збереження локально...';

            const doLocalSave = () =>
              Promise.all(savePairs.map(([fileUrl, fname]) =>
                MinervaLocalSave.saveUrlToFolder(fileUrl, fname, subs)
              )).then(results => {
                if (!lsEl) return;
                if (results.some(res => res.reason === 'no_handle')) {
                  lsEl.innerHTML =
                    '<span style="color:var(--text-dim)">📂 Папка не обрана — </span>' +
                    '<button type="button" onclick="DocumentWidget._pickAndSave()" ' +
                    'style="font-size:11px;padding:2px 8px;border-radius:4px;' +
                    'border:1px solid var(--border-strong);background:none;' +
                    'color:var(--link-fg);cursor:pointer">Вибрати папку</button>';
                } else {
                  const okCount = results.filter(res => res.ok).length;
                  if (okCount === results.length) {
                    lsEl.style.color = 'var(--ok)';
                    lsEl.innerHTML = `✅ Збережено локально (${okCount} файл${okCount > 1 ? 'и' : ''})`;
                  } else {
                    lsEl.style.color = '#ff9800';
                    lsEl.innerHTML = `⚠️ Локально: ${okCount}/${results.length}`;
                  }
                }
              });

            // Store pending save so _pickAndSave can retry after folder selection
            DocumentWidget._pendingSavePairs = savePairs;
            DocumentWidget._pendingSaveSubs  = subs;
            doLocalSave();
          }
        }

      } else {
        if (status) status.innerHTML =
          `<span style="color:var(--err);font-size:13px">✗ ${d.error}</span>`;
      }
    } catch (e) {
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
    } catch (e) {}
  },

  _pickAndSave() {
    const lsEl = document.getElementById('dw-local-status');
    if (!window.MinervaLocalSave) return;
    MinervaLocalSave.pickFolder().then(handle => {
      if (!handle) return;
      const pairs = this._pendingSavePairs || [];
      const subs  = this._pendingSaveSubs  || null;
      if (!pairs.length) return;
      if (lsEl) lsEl.textContent = '💾 Збереження...';
      Promise.all(pairs.map(([fileUrl, fname]) =>
        MinervaLocalSave.saveUrlToFolder(fileUrl, fname, subs)
      )).then(results => {
        if (!lsEl) return;
        const okCount = results.filter(r => r.ok).length;
        if (okCount === results.length) {
          lsEl.style.color = 'var(--ok)';
          lsEl.innerHTML = `✅ Збережено локально (${okCount} файл${okCount > 1 ? 'и' : ''})`;
        } else {
          lsEl.style.color = '#ff9800';
          lsEl.innerHTML = `⚠️ Локально: ${okCount}/${results.length}`;
        }
      });
    });
  },

  async checkTemplate(templatePk, templateName) {
    const status = document.getElementById('doc-gen-status');
    if (status) status.innerHTML =
      `<span style="color:var(--text-dim)">🔍 Перевіряю «${templateName}»...</span>`;

    try {
      const qs    = this.objectPk ? `?order_pk=${this.objectPk}` : '';
      const dlUrl = `/documents/template/${templatePk}/check-download/${qs}`;
      const r     = await fetch(`/documents/template/${templatePk}/check/${qs}`);
      const d     = await r.json();

      if (!d.ok) {
        const icon   = d.syntax_error ? '⚠️' : '✗';
        const dlLink = d.syntax_error
          ? `<a href="${dlUrl}" download="check_${templateName}.docx"
                style="display:inline-block;padding:5px 14px;border-radius:6px;font-size:12px;
                       background:rgba(255,152,0,.15);border:1px solid #ff9800;
                       color:#ff9800;text-decoration:none;margin-top:6px">
               ⬇ .docx з підсвіченими помилками
             </a>`
          : '';
        if (status) status.innerHTML =
          `<div style="margin-top:4px">
             <div style="color:var(--err);font-size:13px">${icon} ${d.error}</div>
             ${dlLink}
           </div>`;
        return;
      }

      if (!d.issues || !d.issues.length) {
        if (status) status.innerHTML =
          `<span style="color:var(--ok);font-size:13px">✓ Шаблон коректний — всі поля визначені</span>`;
        return;
      }

      // Build issues list
      const rows  = d.issues.map(i => {
        const varBadge = `<code style="background:rgba(244,67,54,.12);padding:1px 5px;
          border-radius:3px;color:var(--err);font-size:11px">{{${i.var}}}</code>`;
        const hint = i.suggestion && i.suggestion !== i.var
          ? `<span style="color:var(--text-dim);font-size:11px"> → {{${i.suggestion}}}</span>`
          : `<span style="color:var(--text-dim);font-size:11px"> — невідоме поле</span>`;
        return `<span style="display:inline-flex;align-items:center;gap:3px;flex-wrap:nowrap">
          ${varBadge}${hint}</span>`;
      }).join(' &nbsp; ');

      if (status) status.innerHTML =
        `<div style="margin-top:6px">
           <div style="font-size:12px;color:#ff9800;margin-bottom:6px">
             ⚠️ Знайдено ${d.issues.length} невідом${d.issues.length === 1 ? 'е поле' : 'их полів'}:
           </div>
           <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${rows}</div>
           <a href="${dlUrl}" download="check_${templateName}.docx"
              style="display:inline-block;padding:5px 14px;border-radius:6px;font-size:12px;
                     background:rgba(255,152,0,.15);border:1px solid #ff9800;
                     color:#ff9800;text-decoration:none">
             ⬇ Завантажити .docx з позначками
           </a>
         </div>`;
    } catch (e) {
      if (status) status.innerHTML =
        '<span style="color:var(--err)">✗ Помилка перевірки</span>';
    }
  },

  async deleteDoc(docId, btn) {
    if (!confirm('Видалити документ з сервера?')) return;
    try {
      const r = await fetch(`/documents/delete/${docId}/`, {
        method: 'POST',
        headers: { 'X-CSRFToken': this.csrf },
      });
      const d = await r.json();
      if (d.ok) {
        btn.closest('div[style]').remove();
        // Refresh "Завантажені документи" panel (media/orders/ folder)
        if (typeof _refreshDocsPanel === 'function') {
          _refreshDocsPanel(this.objectPk);
        }
      } else {
        alert('Помилка: ' + d.error);
      }
    } catch (e) {
      alert('Помилка мережі');
    }
  },
};
