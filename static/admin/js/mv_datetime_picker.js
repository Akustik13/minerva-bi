/**
 * MvDatePicker — custom datetime picker for Minerva.
 * Usage: new MvDatePicker(inputEl, { onChange: fn, minNow: bool })
 * inputEl must be <input type="datetime-local">
 */
(function (global) {
  'use strict';

  /* ── Inject CSS once ───────────────────────────────────────────── */
  if (!document.getElementById('mv-dtp-style')) {
    var s = document.createElement('style');
    s.id = 'mv-dtp-style';
    s.textContent = [
      '.mv-dt-trigger{display:inline-flex;align-items:center;gap:8px;',
        'padding:8px 16px;border-radius:7px;',
        'border:1px solid var(--accent,#58a6ff);',
        'background:var(--bg-card,#161b22);',
        'color:var(--text,#e6edf3);font-size:13px;cursor:pointer;',
        'transition:background .15s,border-color .15s;white-space:nowrap;}',
      '.mv-dt-trigger:hover{background:var(--bg-hover,#1e2d40);}',
      '.mv-dt-trigger .mv-dt-icon{font-size:15px;}',
      '.mv-dt-trigger .mv-dt-val{color:var(--accent,#58a6ff);font-weight:600;}',

      '.mv-dt-panel{position:absolute;z-index:99999;',
        'background:var(--bg-card,#161b22);',
        'border:1px solid var(--border-strong,#2a3f52);',
        'border-radius:12px;',
        'box-shadow:0 12px 40px rgba(0,0,0,.6);',
        'width:310px;overflow:hidden;display:none;flex-direction:column;}',

      /* presets */
      '.mv-dt-presets{padding:10px 12px;',
        'background:var(--bg-input,#111c26);',
        'border-bottom:1px solid var(--border,rgba(255,255,255,.07));',
        'display:flex;flex-wrap:wrap;gap:5px;align-items:center;}',
      '.mv-dt-presets-lbl{font-size:11px;color:var(--text-dim,#607d8b);',
        'width:100%;margin-bottom:2px;}',
      '.mv-dt-preset{padding:4px 10px;border-radius:5px;border:1px solid var(--border-strong,#2a3f52);',
        'background:none;color:var(--text-muted,#9aafbe);font-size:12px;cursor:pointer;',
        'transition:background .1s,color .1s,border-color .1s;}',
      '.mv-dt-preset:hover{background:var(--accent-subtle,rgba(88,166,255,.15));',
        'border-color:var(--accent,#58a6ff);color:var(--accent,#58a6ff);}',

      /* calendar nav */
      '.mv-dt-cal-nav{display:flex;align-items:center;justify-content:space-between;',
        'padding:10px 14px 6px;}',
      '.mv-dt-nav{background:none;border:1px solid var(--border-strong,#2a3f52);',
        'border-radius:5px;color:var(--text-muted,#9aafbe);cursor:pointer;',
        'font-size:16px;width:28px;height:28px;display:flex;align-items:center;',
        'justify-content:center;transition:background .1s;}',
      '.mv-dt-nav:hover{background:var(--bg-hover,#1e2d40);color:var(--text,#e6edf3);}',
      '.mv-dt-month-lbl{font-size:13px;font-weight:700;color:var(--text,#e6edf3);}',

      /* weekday header */
      '.mv-dt-wd{display:grid;grid-template-columns:repeat(7,1fr);',
        'padding:0 10px;margin-bottom:2px;}',
      '.mv-dt-wd span{text-align:center;font-size:11px;font-weight:600;',
        'color:var(--text-dim,#607d8b);padding:4px 0;}',

      /* day grid */
      '.mv-dt-grid{display:grid;grid-template-columns:repeat(7,1fr);',
        'padding:0 10px 8px;gap:1px;}',
      '.mv-dt-day{text-align:center;font-size:13px;padding:5px 2px;',
        'border-radius:5px;cursor:pointer;color:var(--text,#e6edf3);',
        'transition:background .1s;}',
      '.mv-dt-day:hover{background:var(--bg-hover,#1e2d40);}',
      '.mv-dt-day.today{color:var(--accent,#58a6ff);font-weight:700;}',
      '.mv-dt-day.sel{background:var(--accent,#58a6ff)!important;',
        'color:#fff!important;font-weight:700;}',
      '.mv-dt-day.past{color:var(--text-dim,#607d8b);}',
      '.mv-dt-day.past:hover{background:none;cursor:default;}',
      '.mv-dt-empty{pointer-events:none;}',

      /* time row */
      '.mv-dt-time{display:flex;align-items:center;gap:6px;',
        'padding:10px 14px;border-top:1px solid var(--border,rgba(255,255,255,.07));',
        'background:var(--bg-input,#111c26);}',
      '.mv-dt-time-lbl{font-size:12px;color:var(--text-muted,#9aafbe);margin-right:4px;}',
      '.mv-dt-t-spin{display:flex;flex-direction:column;gap:2px;}',
      '.mv-dt-t-btn{background:none;border:1px solid var(--border-strong,#2a3f52);',
        'border-radius:4px;color:var(--text-muted,#9aafbe);cursor:pointer;',
        'font-size:10px;width:22px;height:18px;line-height:1;',
        'display:flex;align-items:center;justify-content:center;',
        'transition:background .1s;}',
      '.mv-dt-t-btn:hover{background:var(--bg-hover,#1e2d40);color:var(--text,#e6edf3);}',
      '.mv-dt-t-inp{width:36px;text-align:center;',
        'background:var(--bg-card,#161b22);',
        'border:1px solid var(--border-strong,#2a3f52);border-radius:5px;',
        'color:var(--text,#e6edf3);font-size:16px;font-weight:700;',
        'padding:4px 0;outline:none;}',
      '.mv-dt-t-inp:focus{border-color:var(--accent,#58a6ff);}',
      '.mv-dt-colon{font-size:18px;font-weight:700;color:var(--text-muted,#9aafbe);',
        'margin:0 2px;line-height:1;}',

      /* confirm */
      '.mv-dt-foot{display:flex;gap:8px;padding:10px 14px;',
        'border-top:1px solid var(--border,rgba(255,255,255,.07));}',
      '.mv-dt-ok{flex:1;padding:8px;border-radius:6px;',
        'background:linear-gradient(135deg,#1565c0,#1a73e8);',
        'color:#fff;border:none;font-size:13px;font-weight:600;cursor:pointer;',
        'transition:opacity .15s;}',
      '.mv-dt-ok:hover{opacity:.88;}',
      '.mv-dt-cancel{padding:8px 14px;border-radius:6px;',
        'border:1px solid var(--border-strong,#2a3f52);background:none;',
        'color:var(--text-muted,#9aafbe);font-size:13px;cursor:pointer;',
        'transition:background .1s;}',
      '.mv-dt-cancel:hover{background:var(--bg-hover,#1e2d40);}',
    ].join('');
    document.head.appendChild(s);
  }

  /* ── Constants ───────────────────────────────────────────────────── */
  var MONTHS = ['Січень','Лютий','Березень','Квітень','Травень','Червень',
                'Липень','Серпень','Вересень','Жовтень','Листопад','Грудень'];
  var WD     = ['Пн','Вт','Ср','Чт','Пт','Сб','Нд'];

  /* ── Class ───────────────────────────────────────────────────────── */
  function MvDatePicker(inputEl, opts) {
    this.inp   = inputEl;
    this.opts  = opts || {};
    this._sel  = null;   // selected Date (date portion only)
    this._h    = 9;
    this._m    = 0;
    this._vy   = 0;      // view year
    this._vm   = 0;      // view month
    this._panel = null;
    this._trigger = null;
    this._init();
  }

  MvDatePicker.prototype._init = function () {
    var self = this;
    // Parse existing value
    if (this.inp.value) {
      var d = new Date(this.inp.value);
      if (!isNaN(d)) {
        this._sel = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        this._h   = d.getHours();
        this._m   = d.getMinutes();
      }
    }
    this._vy = (this._sel || new Date()).getFullYear();
    this._vm = (this._sel || new Date()).getMonth();

    // Build trigger
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mv-dt-trigger';
    btn.onclick = function (e) { e.stopPropagation(); self._toggle(); };
    this._trigger = btn;
    this._refreshTrigger();

    // Hide original input, insert trigger before it
    this.inp.style.display = 'none';
    this.inp.parentNode.insertBefore(btn, this.inp);
  };

  MvDatePicker.prototype._refreshTrigger = function () {
    if (this._sel) {
      var d = this._sel;
      var dd = _p(d.getDate()), mm = _p(d.getMonth() + 1), yy = d.getFullYear();
      var hh = _p(this._h), mn = _p(this._m);
      this._trigger.innerHTML =
        '<span class="mv-dt-icon">📅</span>' +
        '<span class="mv-dt-val">' + dd + '.' + mm + '.' + yy + '</span>' +
        '<span class="mv-dt-icon" style="margin-left:4px">⏰</span>' +
        '<span class="mv-dt-val">' + hh + ':' + mn + '</span>';
    } else {
      this._trigger.innerHTML =
        '<span class="mv-dt-icon">📅</span>' +
        '<span style="color:var(--text-muted,#9aafbe)">Вибрати дату та час</span>';
    }
  };

  MvDatePicker.prototype._toggle = function () {
    if (!this._panel) this._build();
    var p = this._panel;
    if (p.style.display === 'flex') { p.style.display = 'none'; return; }
    this._renderCal();
    p.style.display = 'flex';
    // Position
    var rect = this._trigger.getBoundingClientRect();
    var spaceBelow = window.innerHeight - rect.bottom;
    if (spaceBelow < 360 && rect.top > 360) {
      p.style.top  = (rect.top + window.scrollY - p.offsetHeight - 4) + 'px';
    } else {
      p.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
    }
    var left = rect.left;
    if (left + 314 > window.innerWidth) left = window.innerWidth - 318;
    p.style.left = Math.max(4, left) + 'px';
  };

  MvDatePicker.prototype._build = function () {
    var self = this;
    var p = document.createElement('div');
    p.className = 'mv-dt-panel';

    var presets = [
      {k:'+1h',  l:'+1 год'},
      {k:'+2h',  l:'+2 год'},
      {k:'+4h',  l:'+4 год'},
      {k:'t18',  l:'Сьогодні 18:00'},
      {k:'t0',   l:'Завтра 9:00'},
      {k:'t12',  l:'Завтра 12:00'},
      {k:'t+7',  l:'+7 днів 9:00'},
    ];
    var pHtml = '<div class="mv-dt-presets"><div class="mv-dt-presets-lbl">⚡ Швидко:</div>' +
      presets.map(function(x){
        return '<button type="button" class="mv-dt-preset" data-p="'+x.k+'">'+x.l+'</button>';
      }).join('') + '</div>';

    var calHtml =
      '<div class="mv-dt-cal-nav">' +
        '<button type="button" class="mv-dt-nav" id="mvdtp-prev">‹</button>' +
        '<span class="mv-dt-month-lbl" id="mvdtp-ml"></span>' +
        '<button type="button" class="mv-dt-nav" id="mvdtp-next">›</button>' +
      '</div>' +
      '<div class="mv-dt-wd">' + WD.map(function(d){ return '<span>'+d+'</span>'; }).join('') + '</div>' +
      '<div class="mv-dt-grid" id="mvdtp-grid"></div>';

    var timeHtml =
      '<div class="mv-dt-time">' +
        '<span class="mv-dt-time-lbl">⏰ Час</span>' +
        '<div class="mv-dt-t-spin">' +
          '<button type="button" class="mv-dt-t-btn" id="mvdtp-hu">▲</button>' +
          '<button type="button" class="mv-dt-t-btn" id="mvdtp-hd">▼</button>' +
        '</div>' +
        '<input type="number" class="mv-dt-t-inp" id="mvdtp-h" min="0" max="23">' +
        '<span class="mv-dt-colon">:</span>' +
        '<div class="mv-dt-t-spin">' +
          '<button type="button" class="mv-dt-t-btn" id="mvdtp-mu">▲</button>' +
          '<button type="button" class="mv-dt-t-btn" id="mvdtp-md">▼</button>' +
        '</div>' +
        '<input type="number" class="mv-dt-t-inp" id="mvdtp-m" min="0" max="59">' +
      '</div>';

    var footHtml =
      '<div class="mv-dt-foot">' +
        '<button type="button" class="mv-dt-ok" id="mvdtp-ok">✓ Підтвердити</button>' +
        '<button type="button" class="mv-dt-cancel" id="mvdtp-cancel">Скасувати</button>' +
      '</div>';

    p.innerHTML = pHtml + calHtml + timeHtml + footHtml;
    document.body.appendChild(p);
    this._panel = p;

    // Set current time inputs
    p.querySelector('#mvdtp-h').value = this._h;
    p.querySelector('#mvdtp-m').value = this._m;

    // Nav
    p.querySelector('#mvdtp-prev').onclick   = function(){ self._vy--||0; if(--self._vm<0){self._vm=11;self._vy--;} self._renderCal(); };
    p.querySelector('#mvdtp-next').onclick   = function(){ if(++self._vm>11){self._vm=0;self._vy++;} self._renderCal(); };

    // Time spinners
    var hInp = p.querySelector('#mvdtp-h');
    var mInp = p.querySelector('#mvdtp-m');
    p.querySelector('#mvdtp-hu').onclick = function(){ hInp.value = (((parseInt(hInp.value)||0)+1)%24); };
    p.querySelector('#mvdtp-hd').onclick = function(){ hInp.value = (((parseInt(hInp.value)||0)+23)%24); };
    p.querySelector('#mvdtp-mu').onclick = function(){ var v=parseInt(mInp.value)||0; mInp.value = Math.min(55, Math.ceil((v+1)/5)*5); };
    p.querySelector('#mvdtp-md').onclick = function(){ var v=parseInt(mInp.value)||0; mInp.value = Math.max(0, Math.floor((v-1)/5)*5); };
    hInp.onchange = function(){ var v=parseInt(this.value); if(isNaN(v)) this.value=0; else this.value=Math.min(23,Math.max(0,v)); };
    mInp.onchange = function(){ var v=parseInt(this.value); if(isNaN(v)) this.value=0; else this.value=Math.min(59,Math.max(0,v)); };

    // Presets
    p.querySelectorAll('.mv-dt-preset').forEach(function(btn){
      btn.onclick = function(){ self._preset(btn.getAttribute('data-p')); };
    });

    // Confirm / cancel
    p.querySelector('#mvdtp-ok').onclick     = function(){ self._confirm(); };
    p.querySelector('#mvdtp-cancel').onclick = function(){ p.style.display='none'; };

    // Close on outside click
    document.addEventListener('click', function(e){
      if (p.style.display === 'flex' && !p.contains(e.target) && e.target !== self._trigger)
        p.style.display = 'none';
    });
  };

  MvDatePicker.prototype._preset = function (k) {
    var now = new Date(), d = new Date(now), h = 9, m = 0;
    if      (k === '+1h')  { d = new Date(now.getTime()+3600000); h=d.getHours(); m=Math.ceil(d.getMinutes()/5)*5%60; }
    else if (k === '+2h')  { d = new Date(now.getTime()+7200000); h=d.getHours(); m=Math.ceil(d.getMinutes()/5)*5%60; }
    else if (k === '+4h')  { d = new Date(now.getTime()+14400000);h=d.getHours(); m=Math.ceil(d.getMinutes()/5)*5%60; }
    else if (k === 't18')  { h=18; m=0; }
    else if (k === 't0')   { d=new Date(now); d.setDate(d.getDate()+1); h=9;  m=0; }
    else if (k === 't12')  { d=new Date(now); d.setDate(d.getDate()+1); h=12; m=0; }
    else if (k === 't+7')  { d=new Date(now); d.setDate(d.getDate()+7); h=9;  m=0; }
    this._sel = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    this._vy  = this._sel.getFullYear();
    this._vm  = this._sel.getMonth();
    this._h   = h; this._m = m;
    if (this._panel) {
      this._panel.querySelector('#mvdtp-h').value = h;
      this._panel.querySelector('#mvdtp-m').value = m;
    }
    this._confirm();
  };

  MvDatePicker.prototype._renderCal = function () {
    if (!this._panel) return;
    var grid = this._panel.querySelector('#mvdtp-grid');
    var lbl  = this._panel.querySelector('#mvdtp-ml');
    lbl.textContent = MONTHS[this._vm] + ' ' + this._vy;

    var first    = new Date(this._vy, this._vm, 1);
    var startDow = (first.getDay() + 6) % 7;
    var days     = new Date(this._vy, this._vm + 1, 0).getDate();
    var today    = new Date();
    var selD = this._sel ? this._sel.getDate() : -1;
    var selM = this._sel ? this._sel.getMonth() : -1;
    var selY = this._sel ? this._sel.getFullYear() : -1;
    var self = this;

    var html = '';
    for (var i = 0; i < startDow; i++) html += '<span class="mv-dt-empty"></span>';
    for (var d = 1; d <= days; d++) {
      var cls = ['mv-dt-day'];
      var isToday = d===today.getDate() && this._vm===today.getMonth() && this._vy===today.getFullYear();
      var isSel   = d===selD && this._vm===selM && this._vy===selY;
      var isPast  = new Date(this._vy, this._vm, d) < new Date(today.getFullYear(), today.getMonth(), today.getDate());
      if (isToday) cls.push('today');
      if (isSel)   cls.push('sel');
      if (isPast)  cls.push('past');
      html += '<span class="'+cls.join(' ')+'" data-d="'+d+'">'+d+'</span>';
    }
    grid.innerHTML = html;
    grid.querySelectorAll('.mv-dt-day:not(.past)').forEach(function(el){
      el.onclick = function(){
        var day = parseInt(el.getAttribute('data-d'));
        self._sel = new Date(self._vy, self._vm, day);
        self._renderCal();
      };
    });
  };

  MvDatePicker.prototype._confirm = function () {
    if (!this._sel) return;
    var h = parseInt((this._panel||{querySelector:function(){return{value:this._h};}}).querySelector('#mvdtp-h').value) || this._h;
    var m = parseInt(this._panel.querySelector('#mvdtp-m').value) || this._m;
    this._h = h; this._m = m;

    var d  = this._sel;
    var vs = d.getFullYear() + '-' + _p(d.getMonth()+1) + '-' + _p(d.getDate()) +
             'T' + _p(h) + ':' + _p(m);
    this.inp.value = vs;
    this._refreshTrigger();
    if (this._panel) this._panel.style.display = 'none';
    if (this.opts.onChange) this.opts.onChange(vs);
  };

  MvDatePicker.prototype.setValue = function (vs) {
    this.inp.value = vs;
    if (vs) {
      var d = new Date(vs);
      if (!isNaN(d)) {
        this._sel = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        this._h   = d.getHours();
        this._m   = d.getMinutes();
        this._vy  = this._sel.getFullYear();
        this._vm  = this._sel.getMonth();
        if (this._panel) {
          this._panel.querySelector('#mvdtp-h').value = _p(this._h);
          this._panel.querySelector('#mvdtp-m').value = _p(this._m);
        }
      }
    } else {
      this._sel = null;
    }
    this._refreshTrigger();
  };

  function _p(n) { return n < 10 ? '0' + n : '' + n; }

  global.MvDatePicker = MvDatePicker;
})(window);
