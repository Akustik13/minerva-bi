'use strict';
// Meeting recording window (0.3.0). Everything from the server is inserted as text (textContent), never as HTML.
// Modes: opened from Minerva (#minerva=<ticket>, or the binding kept in sessionStorage after F5) — the recording
// window of one meeting; #check — «this PC may open the window»; otherwise the stage-0 test bench when it is switched
// on in «RAG — керування», else a landing card. A tab opened from Minerva never falls back to the test bench.

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const LANGS = [['auto', 'визначати самому'], ['uk', 'українська'], ['de', 'німецька'], ['en', 'англійська'],
  ['ru', 'російська'], ['pl', 'польська']];
const WHO = {L: 'Ви', R: 'Співрозмовники', M: ''};
const KIND = {decision: '✅ Рішення', task: '📋 Завдання', deadline: '⏰ Строк', risk: '⚠️ Ризик',
  question: '❓ Відкрите питання', promise: '🤝 Обіцянка', info: 'ℹ️ Факт'};
const KIND_ORDER = ['decision', 'task', 'deadline', 'risk', 'question', 'promise', 'info'];
// ?v= of this script in index.html: the AudioWorklet module gets the same, so an update is never run from cache
const ASSET_V = (document.currentScript && new URL(document.currentScript.src).searchParams.get('v')) || '';

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v; else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
  return e;
}

function clock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
  const mm = String(m).padStart(2, '0'), ss = String(r).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const num = (v, d = 1) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString('uk-UA', {maximumFractionDigits: d});

async function api(path, opts = {}) {
  const o = {method: opts.method || (opts.json !== undefined ? 'POST' : 'GET'), headers: {'X-Bench': '1'}};
  if (opts.json !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.json); }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

const radio = (name) => ($(`input[name="${name}"]:checked`) || {}).value;

// ---- tabs, status ----------------------------------------------------------------------------
function showTab(name) {
  $$('.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  $$('.tab').forEach((s) => { s.hidden = s.id !== 'tab-' + name; });
  if (name === 'list') loadList();
  if (name === 'metrics') loadMetrics();
  if (name === 'help') loadHelp();
  if (name !== 'session') SS.id = null;
}

// ---- help: the same docs/help_uk.md as the RAG GUI tab «🎙 Наради» -----------------------------
function inline(text) {
  // **bold**, `code` and bare https:// links; everything else stays plain text. Called once per paragraph or list
  // item with its wrapped lines already joined, so bold may cross a line break of the source (review 10.11)
  const frag = document.createDocumentFragment();
  for (const p of text.split(/(\*\*[^*]+\*\*|`[^`]+`|https?:\/\/[^\s)»,;`]+)/g)) {
    if (!p) continue;
    if (p.length > 4 && p.startsWith('**') && p.endsWith('**')) frag.append(el('b', {text: p.slice(2, -2)}));
    else if (p.length > 2 && p.startsWith('`') && p.endsWith('`')) frag.append(el('code', {text: p.slice(1, -1)}));
    else if (/^https?:\/\//.test(p)) frag.append(el('a', {href: p, target: '_blank', rel: 'noopener noreferrer', text: p}));
    else frag.append(p);
  }
  return frag;
}

/** Lines -> blocks (wrapped lines joined) — the same rules as parse_markdown in the RAG GUI tab. */
function mdBlocks(md) {
  const blocks = [];
  for (const raw of md.replace(/\r/g, '').split('\n')) {
    const line = raw.trimEnd();
    const prev = blocks[blocks.length - 1];
    if (!line.trim()) { if (prev && prev.type !== 'blank') blocks.push({type: 'blank'}); continue; }
    let m = line.match(/^(#{1,3})\s+(.*)$/);
    if (m) { blocks.push({type: 'h', level: m[1].length, text: m[2]}); continue; }
    m = line.match(/^(\s*)(-|\d+\.)\s+(.*)$/);
    // inside a paragraph only «- » or «1. » start a list (as in Markdown): a wrapped «2026. …» continues it
    if (m && !(prev && prev.type === 'p' && !m[1] && !['-', '1.'].includes(m[2]))) {
      blocks.push({type: 'li', indent: m[1].length, ordered: m[2] !== '-', text: m[3]});
      continue;
    }
    if (prev && (prev.type === 'p' || (prev.type === 'li' && /^\s/.test(raw)))) { prev.text += ' ' + line.trim(); continue; }
    blocks.push({type: 'p', text: line.trim()});
  }
  return blocks;
}

function renderMarkdown(md) {
  const root = document.createDocumentFragment();
  let lists = []; // open lists: {indent, node}
  for (const b of mdBlocks(md)) {
    if (b.type !== 'li') lists = [];
    if (b.type === 'h') root.append(el(['h2', 'h3', 'h4'][b.level - 1], {}, inline(b.text)));
    else if (b.type === 'p') root.append(el('p', {}, inline(b.text)));
    else if (b.type === 'li') {
      while (lists.length && lists[lists.length - 1].indent > b.indent) lists.pop();
      let top = lists[lists.length - 1];
      if (!top || top.indent < b.indent) {
        const list = el(b.ordered ? 'ol' : 'ul');
        const parentItem = top && top.node.lastElementChild;
        (parentItem || root).append(list);
        top = {indent: b.indent, node: list};
        lists.push(top);
      }
      top.node.append(el('li', {}, inline(b.text)));
    }
  }
  return root;
}

async function loadHelp() {
  try {
    const r = await fetch('/api/help', {cache: 'no-store'});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    $('#help-box').replaceChildren(renderMarkdown(await r.text()));
  } catch (e) {
    $('#help-box').replaceChildren(el('p', {class: 'hint', text: `Довідка не завантажилась: ${e.message}`}));
  }
}

let STATUS = null;
async function pollStatus() {
  try {
    STATUS = await api('/api/status');
    renderChips();
  } catch (e) {
    $('#chips').replaceChildren(el('span', {class: 'chip bad', text: 'Немає зв’язку зі стендом'}));
  }
  setTimeout(pollStatus, 5000);
}

function chip(text, cls = '') { $('#chips').append(el('span', {class: `chip ${cls}`, text})); }

function renderChips() {
  const s = STATUS;
  $('#chips').replaceChildren();
  const g = s.gpu;
  if (g) chip(`Відеокарта: вільно ${num(g.free / 1024)} з ${num(g.total / 1024, 0)} ГБ`, g.free >= 4200 ? 'ok' : 'warn');
  else chip('Відеокарта: немає даних', 'warn');
  const loaded = (s.ollama_loaded || []).map((m) => `${m.name} ${num(m.vram_mb / 1024)} ГБ`).join(', ');
  chip('Ollama: ' + (loaded || 'моделі вивантажені'));
  chip(s.parakeet.ready ? 'Parakeet ✓' : 'Parakeet не встановлено', s.parakeet.ready ? 'ok' : 'bad');
  const w = s.whisper || {};
  const wname = (x) => (x ? `${x.model} ${x.ready ? '✓' : '✗'}` : '—');
  chip(`Whisper: ${wname(w.accurate)} · ${wname(w.fast)}`, w.accurate && w.accurate.ready ? 'ok' : 'warn');
  if (s.live_busy) chip('Іде запис наживо', 'warn');
  if (s.job) chip('⏳ ' + s.job.text, 'warn');
  $('#retention').textContent = s.retention_days;
  $('#version').textContent = `версія ${s.version} · ваша адреса ${s.client_ip}`;
  const ce = $('#config-error');
  ce.textContent = s.config_error ? `⚠️ ${s.config_error}` : '';
  ce.hidden = !s.config_error;
  fillLlm(s.llm || {choices: (s.llm_models || []).map((m) => ({value: `ollama:${m}`, label: m, cloud: false, ready: true})),
    default: `ollama:${s.llm_default}`});
}

// the list follows the RAG GUI tab «🎙 Наради» (models, default, stored key) without a page reload
let LLM_SIG = '';
let LLM_PICK = ''; // what the person chose in the list; restored whenever it is offered again
function fillLlm(llm) {
  const sig = JSON.stringify([llm.choices.map((c) => [c.value, c.ready, c.label]), llm.default]);
  if (sig === LLM_SIG) return;
  LLM_SIG = sig;
  const sel = $('#ss-llm');
  const groups = [['Локально на RAG-ПК — безкоштовно', llm.choices.filter((c) => !c.cloud)],
    ['Платно, за ключем API — текст іде в хмару', llm.choices.filter((c) => c.cloud)]];
  sel.replaceChildren();
  for (const [name, list] of groups) {
    if (!list.length) continue;
    const g = el('optgroup', {label: name});
    for (const c of list) {
      const o = el('option', {value: c.value, text: c.label + (c.value === llm.default ? ' · за замовчуванням' : '')});
      o.disabled = !c.ready;
      o.dataset.cloud = c.cloud ? '1' : '';
      g.append(o);
    }
    sel.append(g);
  }
  const valid = (v) => !!v && [...sel.options].some((o) => o.value === v && !o.disabled);
  // never fall back to a paid model on its own: while Ollama restarts the local models are briefly missing, and the
  // list used to switch to Claude Opus and stay there (review 10.11)
  const local = [...sel.options].find((o) => !o.disabled && !o.dataset.cloud);
  const pick = [LLM_PICK, llm.default].find(valid) || (local ? local.value : '');
  if (!pick) {
    sel.prepend(el('option', {value: '', text: 'Локальна модель зараз недоступна (Ollama не відповідає) — зачекайте або виберіть іншу'}));
  }
  sel.value = pick;
  cloudBox();
}

function cloudBox() {
  const o = $('#ss-llm').selectedOptions[0];
  const cloud = !!(o && o.dataset.cloud);
  $('#ss-cloud').hidden = !cloud;
  if (!cloud) resetConsent();
  if (SS.data) renderJob(SS.data);
}

// consent for the cloud is given for ONE recording and ONE search: the tick remembers the recording it was given
// for, and is cleared when another recording opens and after every search (review 10.11: it used to stick)
function resetConsent() {
  const box = $('#ss-cloud-ok');
  box.checked = false;
  box.dataset.sid = '';
}

function consentGiven() {
  const box = $('#ss-cloud-ok');
  return box.checked && !!SS.id && box.dataset.sid === SS.id;
}

// ---- Minerva mode (stage 1) -------------------------------------------------------------------
const MN = {active: false, binding: '', view: null, lang: 'de', progress: '', recorded: false, dead: '',
  pollTimer: null, maxMinutes: 240, blink: null};
const tabStore = { // per tab, survives F5 (a reload must not drop the window into the test bench)
  get(key) { try { return sessionStorage.getItem(key) || ''; } catch (e) { return ''; } },
  set(key, value) {
    try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); } catch (e) { /* not kept */ }
  },
};
const MN_LANGS = [['auto', 'Автоматично'], ['uk', 'українська'], ['de', 'німецька'], ['en', 'англійська'], ['ru', 'російська']];
const CONSENT_NAMES = {de: 'Deutsch', en: 'English', uk: 'Українська'};
const MARK_TEXT = {consent_ok: '✅ Згоду підтверджено вголос', consent_join: '🙋 Новий учасник погодився'};
const QUIET_MEETING_MS = 10 * 60000; // meeting channel silent this long: «Нарада закінчилась?»
const QUIET_ALL_MS = 15 * 60000;     // every recorded channel silent this long: stop by itself
const BUSY_TEXT = '⏳ Зараз на RAG-ПК іде інший запис. Одночасно — лише один. Спробуйте, коли він закінчиться.';

// ---- live capture -----------------------------------------------------------------------------
const Live = {ws: null, ctx: null, node: null, streams: [], ready: false, queue: [], sid: null, started: 0,
  timer: null, partial: {}, running: false, stopping: false,
  starting: false, // between the click on «▶ Почати» and the running state (double click opened two recordings)
  failed: false, // the server said why it refused: the following close must not replace that with "connection lost"
  closeTimer: null, micGain: null, micLost: '',
  paused: false, pausedAt: 0, pausedMs: 0, heardAt: {L: 0, R: 0}, capWarned: false};

function hint(text, ok = false) {
  const h = $('#lv-hint');
  h.textContent = text || '';
  h.classList.toggle('ok', ok);
}

function stopTracks(stream) { if (stream) stream.getTracks().forEach((t) => t.stop()); }

// ---- microphone: device, processing, check ----------------------------------------------------
// 10.11.2026: the user's takes were silent because Chrome picked «Mikrofon (USBAudio2.0)» (a USB audio box with no
// working microphone; Windows default and #1 in Chrome's ranking). So: show the device actually used, let the user
// pick one explicitly, scan all microphones at once, and judge by sustained speech level, not by a single click.
const saved = {
  get(key, fallback = '') { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private window: not remembered */ } },
};
const PROC_NAMES = {standard: 'звичайна', headset: 'гарнітура', system: 'весь звук комп’ютера', raw: 'без обробки'};
const toDb = (x) => (x > 0 ? 20 * Math.log10(x) : -Infinity);
const meterPct = (db) => (Number.isFinite(db) ? Math.max(0, Math.min(100, (db + 70) / 70 * 100)) : 0); // -70..0 dBFS
const SKIP_MS = 500;    // power-on thump of the device and the start-up gap of system echo cancellation
const HEARD = 0.01;     // ≈ -40 dBFS: a 100 ms frame with real sound in it
const HEARD_FRAMES = 3; // ... at least 300 ms of such frames, so one click is not "heard"

function micConstraints(proc, deviceId) {
  const modes = {
    standard: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
    headset: {echoCancellation: false, noiseSuppression: false, autoGainControl: true},
    system: {echoCancellation: 'all', noiseSuppression: true, autoGainControl: true},
    raw: {echoCancellation: false, noiseSuppression: false, autoGainControl: false},
  };
  const c = {...(modes[proc] || modes.standard), channelCount: {ideal: 1}};
  if (deviceId) c.deviceId = {exact: deviceId};
  return c;
}

async function micIdsKnown() {
  return (await audioDevices()).some((d) => d.kind === 'audioinput' && d.deviceId && !['default', 'communications'].includes(d.deviceId));
}

async function getMic() {
  // Chrome hides device ids until the microphone is allowed on this visit: the saved microphone could not be selected,
  // so the browser's own (on 10.11 a dead) choice was recorded while the picker showed the saved one (review 10.11)
  const want = saved.get('bench-mic');
  if (!$('#lv-mic').value && want && !(await micIdsKnown())) {
    stopTracks(await navigator.mediaDevices.getUserMedia({audio: true}));
    await fillMics();
    if ($('#lv-mic').value !== want) hint('Збереженого мікрофона зараз немає — беру той, що вибере браузер. Перевірте, який саме.');
  }
  const proc = $('#lv-proc').value, id = $('#lv-mic').value;
  try {
    return await navigator.mediaDevices.getUserMedia({audio: micConstraints(proc, id)});
  } catch (e) {
    if (e.name === 'NotAllowedError') throw e;
    if (id && (e.name === 'OverconstrainedError' || e.name === 'NotFoundError')) {
      // the chosen microphone is gone (headset unplugged): fall back to the browser's choice and say so
      $('#lv-mic').value = '';
      saved.set('bench-mic', '');
      hint('Вибраного мікрофона немає — беру той, що вибере браузер. Перевірте, який саме.');
      return navigator.mediaDevices.getUserMedia({audio: micConstraints(proc, '')});
    }
    if (proc === 'system') return navigator.mediaDevices.getUserMedia({audio: micConstraints('standard', id)}); // Chrome < 141
    throw e;
  }
}

// labels of the Windows role aliases are «<role> - <device>» in every UI language («За умовчанням - …», «Оборудование
// связи - …»): only those two entries are passed here, so everything up to the first « - » is the role
const stripRole = (label) => (label || '').replace(/^.*? - /, '');

async function audioDevices() {
  try { return await navigator.mediaDevices.enumerateDevices(); } catch (e) { return []; }
}

async function fillMics() {
  const sel = $('#lv-mic');
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const devices = (await audioDevices()).filter((d) => d.kind === 'audioinput');
  const want = sel.value || saved.get('bench-mic');
  const options = [el('option', {value: '', text: 'Як вибере браузер (перевірте, який саме)'})];
  let n = 0;
  for (const d of devices) {
    if (!d.deviceId || d.deviceId === 'default' || d.deviceId === 'communications') continue; // Windows role aliases
    n += 1;
    options.push(el('option', {value: d.deviceId, text: d.label || `Мікрофон ${n} (назва з’явиться після дозволу на мікрофон)`}));
  }
  sel.replaceChildren(...options);
  sel.value = options.some((o) => o.value === want) ? want : '';
}

function rms(buf) {
  let sum = 0;
  for (const v of buf) sum += v * v;
  return Math.sqrt(sum / buf.length);
}

function percentile(values, q) {
  if (!values.length) return -Infinity;
  const s = values.slice().sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(q * (s.length - 1)))];
}

const snrText = (snr) => (snr > 60 ? '> 60 дБ' : `${Math.round(snr)} дБ`);

/** Verdict from 100 ms blocks (high-passed RMS in dBFS) after the first SKIP_MS: [cls, text, speechDb]. */
function judge(blocksDb, zeroShare, clipShare) {
  if (blocksDb.length < 15) return ['', 'Слухаю… скажіть кілька слів звичайним голосом.', null];
  const speech = percentile(blocksDb, 0.9), floor = Math.max(percentile(blocksDb, 0.1), -100), snr = speech - floor;
  const db = Number.isFinite(speech) ? Math.round(speech) : null;
  if (zeroShare >= 0.95) return ['bad', '⛔ Цифрова тиша — мікрофон вимкнено або заблоковано (Windows, кнопка на пристрої).', db];
  if (clipShare > 0.001) return ['warn', `🟠 Звук перевантажений (${db} дБ) — зменште рівень мікрофона в Windows.`, db];
  if (!(speech >= -60) || snr < 8) return ['bad', `⛔ Цей мікрофон вас не чує (${db === null ? 'тиша' : db + ' дБ'}, лише шум). Виберіть інший або натисніть «🔎 Знайти мікрофон».`, db];
  if (speech < -45 || snr < 15) return ['warn', `🟡 Чути, але тихо (${db} дБ): говоріть ближче або підвищте рівень мікрофона в Windows.`, db];
  return ['ok', `✅ Чути вас добре (${db} дБ, голос на ${snrText(snr)} гучніший за шум). Можна записувати.`, db];
}

const Check = {stream: null, ctx: null, timer: null, started: 0, blocks: [], zeros: 0, clips: 0, samples: 0, scanning: false,
  token: 0, opening: false}; // token: a newer check, a recording or a stop makes an older pending check give up

function cancelCheck() {
  Check.token += 1;
  Check.opening = false;
  stopCheck();
}

function stopCheck() {
  clearInterval(Check.timer);
  Check.timer = null;
  stopTracks(Check.stream);
  Check.stream = null;
  if (Check.ctx) Check.ctx.close().catch(() => {});
  Check.ctx = null;
  $('#lv-check').textContent = '🎤 Перевірити мікрофон';
  $('#lv-check-meter').style.width = '0%';
}

function checkVerdict(text, cls, tips = false) {
  const v = $('#lv-check-verdict');
  v.textContent = text;
  v.className = `hint ${cls}`;
  if (tips) $('#lv-check-tips').open = true;
}

function analyser(ctx, stream) {
  const hp = ctx.createBiquadFilter(); // speech band only: hum, thumps and DC do not count as "heard"
  hp.type = 'highpass';
  hp.frequency.value = 150;
  const an = ctx.createAnalyser();
  an.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(hp);
  hp.connect(an);
  const raw = ctx.createAnalyser(); // unfiltered, for exact zeros and clipping
  raw.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(raw);
  return {an, raw, buf: new Float32Array(2048)};
}

async function micCheck() {
  if (Check.stream || Check.opening) { cancelCheck(); return; }
  if (Live.running || Live.starting || Check.scanning) return;
  $('#lv-check-box').hidden = false; // before any verdict: the HTTPS message used to stay hidden
  if (!window.isSecureContext || !navigator.mediaDevices) { checkVerdict('Сторінку відкрито не через HTTPS — браузер не дасть мікрофона.', 'bad'); return; }
  $('#lv-scan-table').replaceChildren();
  checkVerdict('Відкриваю мікрофон… скажіть кілька слів звичайним голосом.', '');
  const token = ++Check.token;
  Check.opening = true;
  $('#lv-check').textContent = '⏹ Зупинити перевірку';
  let stream;
  try { stream = await getMic(); } catch (e) {
    if (token !== Check.token) return;
    Check.opening = false;
    $('#lv-check').textContent = '🎤 Перевірити мікрофон';
    checkVerdict(e.name === 'NotAllowedError' ? '⛔ Браузер не дав доступу до мікрофона: дозвольте його (🎤 праворуч в адресному рядку) і перевірте ще раз.'
      : `⛔ Мікрофон не відкрився: ${e.message}`, 'bad', true);
    return;
  }
  if (token !== Check.token) { stopTracks(stream); return; } // stopped, or another check / a recording began meanwhile
  Check.opening = false;
  Check.stream = stream;
  await fillMics();
  if (token !== Check.token) return; // cancelCheck() has already closed the stream
  const track = stream.getAudioTracks()[0];
  const s = track.getSettings ? track.getSettings() : {};
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  const allOk = Array.isArray(caps.echoCancellation) ? caps.echoCancellation.includes('all') : null;
  $('#lv-check-device').textContent = `Слухає: «${track.label || 'без назви'}» — це той мікрофон, у який ви говорите? · ` +
    `${s.sampleRate ? s.sampleRate + ' Гц' : 'частота невідома'} · обробка: ${PROC_NAMES[$('#lv-proc').value]}` +
    ` (луна: ${s.echoCancellation === undefined ? '—' : s.echoCancellation}, шум: ${s.noiseSuppression === undefined ? '—' : s.noiseSuppression},` +
    ` підсилення: ${s.autoGainControl === undefined ? '—' : s.autoGainControl})` +
    ($('#lv-proc').value === 'system' && allOk === false ? ' · ⚠️ цей браузер не вміє прибирати весь звук комп’ютера' : '');
  const ctx = new AudioContext();
  Check.ctx = ctx;
  const a = analyser(ctx, stream);
  if (ctx.state === 'suspended') await ctx.resume();
  if (token !== Check.token) return;
  Object.assign(Check, {started: Date.now(), blocks: [], zeros: 0, clips: 0, samples: 0});
  Check.timer = setInterval(() => {
    a.an.getFloatTimeDomainData(a.buf);
    const db = toDb(rms(a.buf));
    $('#lv-check-meter').style.width = `${meterPct(db)}%`;
    $('#lv-check-db').textContent = Number.isFinite(db) ? `${Math.round(db)} дБ` : 'тиша';
    const age = Date.now() - Check.started;
    if (age < SKIP_MS) return;
    Check.blocks.push(db);
    if (Check.blocks.length > 40) Check.blocks.shift(); // last 4 s
    a.raw.getFloatTimeDomainData(a.buf);
    for (const v of a.buf) { if (v === 0) Check.zeros++; if (Math.abs(v) >= 0.99) Check.clips++; }
    Check.samples += a.buf.length;
    if (track.muted) { checkVerdict('⛔ Мікрофон приглушено (кнопкою на пристрої або Windows) — увімкніть його.', 'bad', true); return; }
    const [cls, text] = judge(Check.blocks, Check.zeros / Check.samples, Check.clips / Check.samples);
    checkVerdict(text, cls, cls === 'bad' && age > 6000);
    if (age > 30000) stopCheck();
  }, 100);
}

/** Open every microphone at once (no processing), let the user talk 5 s, pick the one that hears speech best. */
async function findMic() {
  if (Live.running || Live.starting || Check.scanning) return;
  if (Check.stream || Check.opening) cancelCheck();
  $('#lv-check-box').hidden = false;
  if (!window.isSecureContext || !navigator.mediaDevices) { checkVerdict('Сторінку відкрито не через HTTPS — браузер не дасть мікрофона.', 'bad'); return; }
  const table = $('#lv-scan-table');
  table.replaceChildren();
  Check.scanning = true;
  $('#lv-find').disabled = true;
  const opened = [];
  let ctx = null;
  try {
    try { stopTracks(await navigator.mediaDevices.getUserMedia({audio: true})); } catch (e) { // permission + device names
      checkVerdict(e.name === 'NotAllowedError' ? '⛔ Браузер не дав доступу до мікрофона — дозвольте його й спробуйте ще раз.' : `⛔ ${e.message}`, 'bad', true);
      return;
    }
    const devices = (await audioDevices()).filter((d) => d.kind === 'audioinput' && d.deviceId && !['default', 'communications'].includes(d.deviceId));
    if (!devices.length) { checkVerdict('⛔ Браузер не бачить жодного мікрофона.', 'bad', true); return; }
    ctx = new AudioContext();
    for (const d of devices) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: micConstraints('raw', d.deviceId)});
        opened.push({device: d, stream, ...analyser(ctx, stream), blocks: []});
      } catch (e) {
        opened.push({device: d, error: e.name});
      }
    }
    if (ctx.state === 'suspended') await ctx.resume();
    checkVerdict(`🔎 Говоріть звичайним голосом 5 секунд — слухаю ${opened.filter((o) => o.stream).length} мікрофон(и) одночасно…` +
      ' (Bluetooth-гарнітура на цей час може перейти в режим дзвінка — це нормально.)', '');
    const t0 = Date.now();
    await new Promise((resolve) => {
      const timer = setInterval(() => {
        const age = Date.now() - t0;
        let best = -Infinity;
        for (const o of opened) {
          if (!o.an) continue;
          o.an.getFloatTimeDomainData(o.buf);
          const db = toDb(rms(o.buf));
          if (age >= SKIP_MS) o.blocks.push(db);
          best = Math.max(best, db);
        }
        $('#lv-check-meter').style.width = `${meterPct(best)}%`;
        $('#lv-check-db').textContent = `${Math.max(0, Math.ceil((5500 - age) / 1000))} с`;
        if (age > 5500) { clearInterval(timer); resolve(); }
      }, 100);
    });
    const rows = opened.map((o) => {
      if (o.error) return {o, speech: -Infinity, snr: 0, note: `не відкрився (${o.error})`};
      const speech = percentile(o.blocks, 0.9), floor = Math.max(percentile(o.blocks, 0.1), -100);
      return {o, speech, snr: speech - floor, note: ''};
    });
    const good = rows.filter((r) => r.speech >= -60 && r.snr >= 10).sort((a, b) => b.speech - a.speech);
    table.replaceChildren(el('tr', {}, el('th', {text: 'Мікрофон'}), el('th', {text: 'Голос'}), el('th', {text: 'Над шумом'}), el('th', {text: ''})));
    for (const r of rows.sort((a, b) => b.speech - a.speech)) {
      const verdict = r.note || (good[0] === r ? '✅ найкраще чує вас' : good.includes(r) ? '🟡 чує' : '⛔ не чує голосу');
      table.append(el('tr', {}, el('td', {text: r.o.device.label || 'без назви'}),
        el('td', {class: 'num', text: Number.isFinite(r.speech) ? `${Math.round(r.speech)} дБ` : '—'}),
        el('td', {class: 'num', text: r.o.error || !Number.isFinite(r.snr) ? '—' : snrText(r.snr)}), el('td', {text: verdict})));
    }
    if (good.length) {
      const id = good[0].o.device.deviceId;
      await fillMics();
      $('#lv-mic').value = id;
      saved.set('bench-mic', id);
      checkVerdict(`✅ Вибрано «${good[0].o.device.label}». Для певності натисніть «🎤 Перевірити мікрофон».`, 'ok');
    } else {
      checkVerdict('⛔ Жоден мікрофон не чув голосу. Перевірте кнопку вимкнення на гарнітурі, підключіть гарнітуру або вебкамеру з мікрофоном і спробуйте ще раз.', 'bad', true);
    }
  } finally {
    opened.forEach((o) => stopTracks(o.stream));
    if (ctx) ctx.close().catch(() => {});
    Check.scanning = false;
    $('#lv-find').disabled = false;
    $('#lv-check-meter').style.width = '0%';
  }
}

function watchSilence(levels) {
  // warn only while a channel has NEVER carried sustained sound since the start: pausing to listen is normal
  const [l, r] = levels;
  const age = Date.now() - Live.started;
  if (age >= SKIP_MS) {
    if (l >= HEARD) Live.micFrames += 1;
    if (r >= HEARD) Live.meetingFrames += 1;
  }
  const warn = [];
  if (Live.mode !== 'meeting' && Live.micFrames < HEARD_FRAMES && age > 12000) {
    warn.push(`🎤 Мікрофон «${Live.micLabel || '?'}» досі не чув вас — скажіть кілька слів. Якщо смужка «Ви» не рухається, зупиніть запис і натисніть «🔎 Знайти мікрофон».`);
  }
  if (Live.mode !== 'mic' && Live.meetingFrames < HEARD_FRAMES && age > 20000) {
    warn.push('🖥 Звуку наради ще не було — у вибраній вкладці чи вікні нічого не звучить або не поставлено галочку «Поділитися звуком».' +
      (Live.outputHint ? ' ' + Live.outputHint : ''));
  }
  const box = $('#lv-warn');
  const text = warn.join(' ');
  if (box.textContent !== text) { box.textContent = text; box.hidden = !text; }
}

/** Entire-screen audio is the loopback of the DEFAULT output only; a desktop meeting app playing to the
 * communications output is then silent (seen on the user's PC: Realtek vs USBAudio2.0). */
async function outputHint(display) {
  const v = display && display.getVideoTracks()[0];
  const surface = v && v.getSettings ? v.getSettings().displaySurface : '';
  if (surface !== 'monitor') return '';
  const outs = (await audioDevices()).filter((d) => d.kind === 'audiooutput');
  const def = outs.find((d) => d.deviceId === 'default');
  const comm = outs.find((d) => d.deviceId === 'communications');
  if (!def || !comm || def.groupId === comm.groupId) return '';
  return `Увага: «Весь екран» бере звук лише з виводу за замовчуванням («${stripRole(def.label)}»), а програма наради може грати у` +
    ` «пристрій зв’язку» («${stripRole(comm.label)}»). Тоді виберіть вікно програми зі «звуком вікна» або зробіть один пристрій і тим, і тим.`;
}

function browserName() {
  const brands = (navigator.userAgentData && navigator.userAgentData.brands) || [];
  const b = brands.find((x) => !/not.a.brand|chromium/i.test(x.brand)) || brands[0];
  return b ? `${b.brand} ${b.version}` : navigator.userAgent.slice(0, 120);
}

async function deviceList() {
  const all = await audioDevices();
  const ids = (kind, role) => (all.find((d) => d.kind === kind && d.deviceId === role) || {}).groupId;
  const out = (kind) => all.filter((d) => d.kind === kind && !['default', 'communications'].includes(d.deviceId)).slice(0, 12)
    .map((d) => ({label: d.label, default: d.groupId === ids(kind, 'default'), comm: d.groupId === ids(kind, 'communications')}));
  return {inputs: out('audioinput'), outputs: out('audiooutput')};
}

async function captureInfo(display, mic) {
  const info = (stream, extra) => {
    const t = stream && stream.getAudioTracks()[0];
    return t ? {label: t.label, settings: t.getSettings ? t.getSettings() : {}, muted: t.muted, ...extra} : null;
  };
  const v = display && display.getVideoTracks()[0];
  const t = mic && mic.getAudioTracks()[0];
  const caps = t && t.getCapabilities ? t.getCapabilities() : {};
  return {mic: info(mic, {processing: $('#lv-proc').value}), display: info(display, {}),
    surface: v && v.getSettings ? v.getSettings().displaySurface : null,
    ec_modes: Array.isArray(caps.echoCancellation) ? caps.echoCancellation.join(',') : null,
    ctx_rate: Live.ctx ? Live.ctx.sampleRate : null, browser: browserName(), ...(await deviceList())};
}

async function buildGraph(display, mic) {
  let ctx;
  try { ctx = new AudioContext({sampleRate: 16000, latencyHint: 'interactive'}); } catch (e) { ctx = new AudioContext(); }
  Live.ctx = ctx;
  await ctx.audioWorklet.addModule(`/static/capture-worklet.js${ASSET_V ? '?v=' + encodeURIComponent(ASSET_V) : ''}`);
  const merger = ctx.createChannelMerger(2);
  const mono = () => {
    const g = ctx.createGain();
    g.channelCount = 1; g.channelCountMode = 'explicit'; g.channelInterpretation = 'speakers';
    return g;
  };
  Live.micGain = null;
  if (mic) { const m = mono(); ctx.createMediaStreamSource(mic).connect(m); m.connect(merger, 0, 0); Live.micGain = m; }
  if (display) {
    const d = mono();
    ctx.createMediaStreamSource(new MediaStream(display.getAudioTracks())).connect(d);
    d.connect(merger, 0, 1);
  }
  const node = new AudioWorkletNode(ctx, 'capture', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
    channelCount: 2, channelCountMode: 'explicit', channelInterpretation: 'discrete'});
  merger.connect(node);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  node.connect(mute);
  mute.connect(ctx.destination);
  node.port.onmessage = (ev) => onAudio(ev.data);
  Live.node = node;
  if (ctx.state === 'suspended') await ctx.resume();
}

function onAudio(d) {
  if (d.levels) {
    meters(d.levels);
    if (Live.running && !Live.stopping && !Live.paused) {
      watchSilence(d.levels);
      const now = Date.now();
      if (d.levels[0] >= HEARD) Live.heardAt.L = now;
      if (d.levels[1] >= HEARD) Live.heardAt.R = now;
    }
  }
  // ⏸ Пауза: nothing is sent and nothing is kept — the recording simply has no audio for that time
  if (!d.pcm || !Live.running || Live.stopping || Live.paused) return;
  if (Live.ready && Live.ws && Live.ws.readyState === 1) Live.ws.send(d.pcm);
  else if (Live.queue.length < 1200) Live.queue.push(d.pcm); // keep up to 2 minutes while the model loads
}

function meters(levels) {
  // in dB (-70..0 dBFS): a dead microphone at -85 dBFS must look empty, a quiet one must still move
  const [l, r] = levels;
  $('#lv-meter-l').style.width = `${meterPct(toDb(l))}%`;
  $('#lv-meter-r').style.width = `${meterPct(toDb(r))}%`;
}

function setLiveButtons(running) {
  $('#lv-start').disabled = running;
  $('#lv-stop').disabled = !running;
  $('#lv-mark').disabled = !running;
  $('#lv-pause').disabled = !running;
  if (!running) $('#lv-pause').textContent = '⏸ Пауза';
  $$('#tab-live fieldset input, #lv-title, #lv-lang-l, #lv-lang-r, #lv-consent, #lv-mic, #lv-proc, #lv-check, #lv-find')
    .forEach((i) => { i.disabled = running; });
  if (MN.active) {
    ['#mn-join', '#mn-ok', '#mn-discard'].forEach((s) => { $(s).disabled = !running; });
    ['#mn-announced', '#mn-agreed'].forEach((s) => { $(s).disabled = running || MN.recorded || !!MN.dead; });
    $$('#mn-langs button').forEach((b) => { b.disabled = running; });
    if (!running) {
      $('#mn-silence').hidden = true;
      blinkTitle(false);
      updateStart();
    }
  }
}

async function liveStart() {
  if (Live.running || Live.starting) return;
  if (MN.active) {
    if (MN.dead || MN.recorded) { updateStart(); return; }
    if (!mnReady()) { hint('Поставте обидві галочки згоди — тоді можна почати запис.'); return; }
  } else if (!$('#lv-consent').checked) { hint('Поставте галочку: усі, кого чути, погодились на запис.'); return; }
  if (!window.isSecureContext || !navigator.mediaDevices) {
    hint('Сторінку відкрито не через HTTPS — браузер не дасть доступу до звуку.');
    return;
  }
  if (Check.scanning) { hint('Зачекайте, поки закінчиться пошук мікрофона (5 с).'); return; }
  // set synchronously, before the first await: a double click used to build two graphs and open two recordings
  Live.starting = true;
  $('#lv-start').disabled = true;
  try {
    await liveBegin();
  } finally {
    Live.starting = false;
    if (!Live.running) $('#lv-start').disabled = false;
  }
}

async function liveBegin() {
  const mode = radio('lv-mode'), engine = radio('lv-engine');
  if (Check.stream || Check.opening) cancelCheck();
  hint('');
  $('#lv-warn').hidden = true;
  $('#lv-warn').textContent = '';
  micNote('');
  let display = null, mic = null;
  try {
    if (mode !== 'mic') {
      display = await navigator.mediaDevices.getDisplayMedia({
        video: {frameRate: {ideal: 1, max: 5}},
        audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false},
        systemAudio: 'include', windowAudio: 'window', selfBrowserSurface: 'exclude', surfaceSwitching: 'include',
        preferCurrentTab: false,
      });
      if (!display.getAudioTracks().length) {
        stopTracks(display);
        hint('Звуку немає: у вікні вибору поставте галочку «Поділитися звуком» (звук вкладки або системний звук).');
        return;
      }
    }
    if (mode !== 'meeting') {
      mic = await getMic();
      fillMics(); // device names are known once the microphone is allowed
    }
  } catch (e) {
    stopTracks(display); stopTracks(mic);
    hint(e.name === 'NotAllowedError' ? 'Доступ не надано — запис не почався.' : `Не вдалося отримати звук: ${e.message}`);
    return;
  }
  Live.streams = [display, mic].filter(Boolean);
  // before the graph starts, so no audio frame is lost while these run
  const capture = await captureInfo(display, mic);
  const outHint = mode !== 'mic' ? await outputHint(display) : '';
  try {
    await buildGraph(display, mic);
  } catch (e) {
    cleanupCapture();
    hint(`Не вдалося підготувати звук: ${e.message}`);
    return;
  }
  capture.ctx_rate = Live.ctx ? Live.ctx.sampleRate : null;
  const micTrack = mic && mic.getAudioTracks()[0];
  const t0 = Date.now();
  Object.assign(Live, {running: true, stopping: false, failed: false, ready: false, queue: [], partial: {}, sid: null,
    started: t0, mode, micFrames: 0, meetingFrames: 0, micLabel: micTrack ? micTrack.label : '', outputHint: outHint,
    capture, micLost: '', paused: false, pausedAt: 0, pausedMs: 0, heardAt: {L: t0, R: t0}, capWarned: false});
  $('#lv-lines').replaceChildren();
  $('#lv-saved').hidden = true;
  $('#lv-stats').textContent = '';
  setLiveButtons(true);
  Live.timer = setInterval(tickTimer, 500);
  connect(mode, engine);
  if (display) display.getAudioTracks()[0].addEventListener('ended', () => liveStop('Показ звуку зупинено в браузері.'));
  if (micTrack) watchMic(micTrack);
}

// ---- the microphone during a recording: unplugged / muted is said at once (review 10.11: a Bluetooth headset that
// dropped after 5 minutes left the rest of the meeting without the person's voice, and nothing said so)
function micNote(text) {
  const box = $('#lv-miclost');
  if (!box) return;
  box.replaceChildren();
  box.hidden = !text;
  if (!text) return;
  box.append(el('div', {text}));
  if (Live.running && !Live.stopping && Live.micGain) box.append(button('🔄 Підключити мікрофон знову', micReconnect));
}

function watchMic(track) {
  track.addEventListener('ended', () => {
    if (!Live.running || Live.stopping) return;
    Live.micLost = 'ended';
    micNote(`⚠️ ${clock((Date.now() - Live.started) / 1000)}: мікрофон «${track.label || '?'}» від’єднався — ваш голос далі не ` +
      'записується. Підключіть мікрофон і натисніть кнопку нижче (або зупиніть запис і почніть новий).');
    sendMark('мікрофон від’єднано', true);
  });
  track.addEventListener('mute', () => {
    if (!Live.running || Live.stopping || Live.micLost === 'ended') return;
    Live.micLost = 'mute';
    micNote(`⚠️ ${clock((Date.now() - Live.started) / 1000)}: мікрофон «${track.label || '?'}» зараз не дає звуку (приглушено ` +
      'в Windows чи на пристрої, або Bluetooth-гарнітура перемикається).');
  });
  track.addEventListener('unmute', () => {
    if (Live.micLost !== 'mute') return;
    Live.micLost = '';
    micNote('');
  });
}

async function micReconnect() {
  if (!Live.running || Live.stopping || !Live.ctx || !Live.micGain) return;
  let stream;
  try { stream = await getMic(); } catch (e) {
    micNote(`⛔ Мікрофон не відкрився: ${e.name === 'NotAllowedError' ? 'браузер не дав доступу' : e.message}. Перевірте підключення й спробуйте ще раз.`);
    return;
  }
  if (!Live.running || Live.stopping || !Live.ctx) { stopTracks(stream); return; }
  Live.ctx.createMediaStreamSource(stream).connect(Live.micGain);
  Live.streams.push(stream);
  const track = stream.getAudioTracks()[0];
  Live.micLabel = track ? track.label : Live.micLabel;
  Live.micLost = '';
  micNote('');
  if (track) watchMic(track);
  hint(`● Іде запис · 🎤 ${Live.micLabel} (підключено знову о ${clock((Date.now() - Live.started) / 1000)})`, true);
  sendMark('мікрофон підключено знову', true);
}

/** A mark in the recording: 📌 (user), an automatic note (auto) or the reserved consent_ok / consent_join. */
function sendMark(note, auto = false) {
  if (Live.ws && Live.ready && Live.ws.readyState === 1) Live.ws.send(JSON.stringify({type: 'mark', note, auto}));
}

function connect(mode, engine) {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  Live.ws = ws;
  const common = {mode, engine, lang_l: $('#lv-lang-l').value, lang_r: $('#lv-lang-r').value, capture: Live.capture};
  ws.onopen = () => ws.send(JSON.stringify(MN.active
    ? {type: 'start', minerva: MN.binding, ...common,
      consent: {announced: $('#mn-announced').checked, all_agreed: $('#mn-agreed').checked, lang: MN.lang,
        at: new Date().toISOString()}}
    : {type: 'start', title: $('#lv-title').value.trim(), consent: true, ...common}));
  ws.onmessage = (ev) => {
    if (ws !== Live.ws) return; // an older recording's socket
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    onServer(m);
  };
  ws.onclose = () => {
    if (ws !== Live.ws) return;
    clearTimeout(Live.closeTimer);
    if (Live.running && !Live.stopping && !Live.failed) {
      if (MN.active && Live.sid) {
        // what was recorded stays and is transcribed; the rest of the meeting needs a new window from Minerva
        hint('Запис перервався — щоб записати решту, натисніть «🎙 Записати продовження» на сторінці наради в Minerva.');
        MN.recorded = true;
        afterRecording('Те, що встигло записатись, збережено: RAG-ПК розшифрує його й передасть у Minerva.');
      } else {
        hint(Live.sid ? 'З’єднання з RAG-ПК обірвалось. Те, що встигло записатись, збережено.'
          : 'З’єднання з RAG-ПК обірвалось — запис не почався.');
      }
    }
    cleanupCapture();
    Live.running = false;
    Live.stopping = false;
    Live.failed = false;
    micNote('');
    setLiveButtons(false);
  };
}

function onServer(m) {
  if (m.type === 'status') hint(m.text, true);
  else if (m.type === 'ready') {
    Live.ready = true;
    Live.sid = m.id || (m.minerva ? 'minerva' : null);
    if (m.progress) { MN.progress = m.progress; tabStore.set('bench-progress', m.progress); }
    for (const b of Live.queue) Live.ws.send(b);
    Live.queue = [];
    const name = {parakeet: 'Parakeet', whisper: 'Whisper turbo', none: MN.active ? 'без тексту' : 'без тексту наживо'}[m.engine] || m.engine;
    hint(`● Іде запис${MN.active ? '' : ' · ' + name}${m.load_s ? ` · модель завантажилась за ${num(m.load_s)} с` : ''}` +
      (Live.micLabel ? ` · 🎤 ${Live.micLabel}` : ''), true);
  } else if (m.type === 'warn') { $('#mn-busy').textContent = m.text; $('#mn-busy').hidden = false; }
  else if (m.type === 'partial') showPartial(m);
  else if (m.type === 'final') showFinal(m);
  else if (m.type === 'drop') dropPartial(m.ch);
  else if (m.type === 'stats') showStats(m);
  else if (m.type === 'marked') {
    const label = MARK_TEXT[m.kind] || (m.kind === 'auto' ? `🔸 ${m.note}` : `📌 Позначка${m.note ? ': ' + m.note : ''}`);
    addLine(el('div', {class: 'line mark', text: `${label} · ${clock(m.t)}`}));
  } else if (m.type === 'saved') onSaved(m);
  else if (m.type === 'capped') hint('Запис зупинено автоматично: досягнуто найбільшої тривалості.');
  else if (m.type === 'discarded') {
    MN.dead = m.message ? `⛔ ${m.message}` : '⛔ Запис зупинено й видалено. Нічого не збережено й не передано в Minerva.';
    tabStore.set('bench-progress', '');
    hint(MN.dead);
    updateStart();
  } else if (m.type === 'error') {
    Live.failed = true;
    hint(m.message);
    cleanupCapture();
    if (MN.active && (m.stale || m.deleted)) { MN.dead = m.message; updateStart(); }
    if (MN.active && m.busy) { $('#mn-busy').textContent = BUSY_TEXT; $('#mn-busy').hidden = false; }
  }
}

function lineEl(ch, t0, text, cls = '') {
  return el('div', {class: `line ${ch} ${cls}`},
    el('span', {class: 't', text: clock(t0)}),
    WHO[ch] ? el('span', {class: 'who', text: WHO[ch] + ':'}) : null,
    el('span', {class: 'x', text}));
}

function addLine(node) {
  const box = $('#lv-lines');
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.append(node);
  if (atEnd) box.scrollTop = box.scrollHeight;
}

function showPartial(m) {
  const p = Live.partial[m.ch];
  if (p) { $('.x', p).textContent = m.text; return; }
  Live.partial[m.ch] = lineEl(m.ch, m.t0, m.text, 'partial');
  addLine(Live.partial[m.ch]);
}

function dropPartial(ch) {
  if (Live.partial[ch]) Live.partial[ch].remove();
  delete Live.partial[ch];
}

function showFinal(m) {
  dropPartial(m.ch);
  const d = lineEl(m.ch, m.t0, m.text);
  d.title = `розпізнано за ${m.decode_ms} мс, у черзі ${m.wait_ms} мс`;
  addLine(d);
  translateLine(m.text, m.lang, d);
}

function showStats(m) {
  const parts = [`записано ${clock(m.seconds)}`, `процесор ${m.cpu}%`];
  if (m.rtf !== null && m.rtf !== undefined) parts.push(`розпізнавання ×${num(1 / Math.max(m.rtf, 0.001), 1)} швидше за мову`);
  if (m.avg_wait_ms !== null && m.avg_wait_ms !== undefined) parts.push(`у черзі ≈ ${num(m.avg_wait_ms / 1000, 1)} с`);
  if (m.queue > 2) parts.push(`⚠️ не встигає: у черзі ${m.queue}`);
  if (m.errors) parts.push(`помилок ${m.errors}`);
  $('#lv-stats').textContent = parts.join(' · ');
}

function liveStop(reason, autoNote) {
  if (!Live.running || Live.stopping) return;
  if (autoNote) sendMark(autoNote, true);
  Live.stopping = true;
  blinkTitle(false);
  hint(`${reason ? reason + ' ' : ''}Зберігаю…`, true);
  try {
    if (Live.ws && Live.ws.readyState === 1) {
      for (const b of Live.queue) Live.ws.send(b);
      Live.queue = [];
      Live.ws.send(JSON.stringify({type: 'stop'}));
    }
  } catch (e) { /* closing anyway */ }
  cleanupCapture();
  micNote('');
  // close THIS recording's socket if the server never answers; the timer used to read the global Live.ws and cut the
  // next recording 2 minutes after the previous Stop (review 10.11)
  const ws = Live.ws;
  clearTimeout(Live.closeTimer);
  Live.closeTimer = setTimeout(() => { if (ws && ws.readyState < 2) ws.close(); }, 120000);
}

function cleanupCapture() {
  try { if (Live.node) Live.node.port.postMessage('stop'); } catch (e) { /* ignore */ }
  Live.streams.forEach(stopTracks);
  Live.streams = [];
  if (Live.ctx) Live.ctx.close().catch(() => {});
  Live.ctx = null;
  Live.node = null;
  clearInterval(Live.timer);
  meters([0, 0]);
}

function onSaved(m) {
  hint('');
  if (MN.active && m.minerva) {
    MN.recorded = true;
    const silent = m.silent || [];
    afterRecording(`✅ Збережено (${clock(m.duration)}). RAG-ПК розшифрує запис і передасть у Minerva. Вікно можна закрити.`,
      silent.map((c) => (c === 'L' ? '⚠️ Мікрофон у цьому записі майже мовчав.' : '⚠️ Звуку наради в цьому записі не було.')).join(' '));
    clearTimeout(Live.closeTimer);
    if (Live.ws && Live.ws.readyState < 2) Live.ws.close();
    return;
  }
  const box = $('#lv-saved');
  box.hidden = false;
  const silent = m.silent || [];
  // replaceChildren() would turn a null into the text "null": only real nodes (review 10.11)
  box.replaceChildren(...[
    el('div', {text: `✅ Збережено на RAG-ПК · ${clock(m.duration)}`}),
    silent.length ? el('div', {class: 'hint warn', text: silent.map((c) => (c === 'L'
      ? '⚠️ Мікрофон у цьому записі майже мовчав — розшифрувати не буде що. Натисніть «🎤 Перевірити мікрофон».'
      : '⚠️ Звуку наради в цьому записі не було.')).join(' ')}) : null,
    el('div', {class: 'row'},
      button('📝 Точна розшифровка (large-v3)', () => startTranscribe(m.id, 'accurate', true), 'primary'),
      button('⚡ Швидка (turbo)', () => startTranscribe(m.id, 'fast', true)),
      button('Відкрити запис', () => openSession(m.id)))].filter(Boolean));
  clearTimeout(Live.closeTimer);
  if (Live.ws && Live.ws.readyState < 2) Live.ws.close();
}

function button(text, onClick, cls = '') {
  const b = el('button', {type: 'button', class: cls, text});
  b.addEventListener('click', onClick);
  return b;
}

// ---- pause, silence, the longest recording ----------------------------------------------------------
function recordedSeconds() {
  const paused = Live.pausedMs + (Live.paused ? Date.now() - Live.pausedAt : 0);
  return Math.max(0, (Date.now() - Live.started - paused) / 1000);
}

function togglePause() {
  if (!Live.running || Live.stopping) return;
  if (!Live.paused) {
    sendMark('пауза', true);
    Object.assign(Live, {paused: true, pausedAt: Date.now()});
    $('#lv-pause').textContent = '▶ Продовжити';
    hint('⏸ Пауза — зараз нічого не записується. Натисніть «▶ Продовжити».');
  } else {
    const now = Date.now();
    Object.assign(Live, {paused: false, pausedMs: Live.pausedMs + now - Live.pausedAt, heardAt: {L: now, R: now}});
    sendMark('продовжено', true);
    $('#lv-pause').textContent = '⏸ Пауза';
    hint('● Іде запис', true);
  }
}

function blinkTitle(on) {
  if (on && !MN.blink) {
    const normal = document.title;
    let flip = false;
    MN.blink = {normal, timer: setInterval(() => { flip = !flip; document.title = flip ? 'Нарада закінчилась? ⏹ Зупинити' : normal; }, 1000)};
  } else if (!on && MN.blink) {
    clearInterval(MN.blink.timer);
    document.title = MN.blink.normal;
    MN.blink = null;
  }
}

function tickTimer() {
  const secs = recordedSeconds();
  $('#lv-timer').textContent = clock(secs) + (Live.paused ? ' ⏸' : '');
  if (!MN.active || !Live.running || Live.stopping) return;
  const now = Date.now();
  if (!Live.paused) {
    const chans = Live.mode === 'mic' ? ['L'] : Live.mode === 'meeting' ? ['R'] : ['L', 'R'];
    if (chans.every((c) => now - Live.heardAt[c] > QUIET_ALL_MS)) {
      liveStop('Зупинено автоматично: 15 хвилин тиші.', 'зупинено автоматично: 15 хв тиші');
      return;
    }
    const quiet = Live.mode !== 'mic' && now - Live.heardAt.R > QUIET_MEETING_MS;
    $('#mn-silence').hidden = !quiet;
    blinkTitle(quiet);
  }
  const max = MN.maxMinutes * 60;
  if (secs >= max) {
    liveStop('Зупинено автоматично: досягнуто найбільшої тривалості запису.', 'зупинено автоматично: найбільша тривалість');
  } else if (secs >= max - 300 && !Live.capWarned) {
    Live.capWarned = true;
    $('#mn-busy').textContent = `⏰ Запис зупиниться сам приблизно за 5 хвилин (найбільша тривалість — ${num(MN.maxMinutes / 60)} год).`;
    $('#mn-busy').hidden = false;
  }
}

function liveDiscard() {
  if (!MN.active || !Live.running || Live.stopping) return;
  if (!confirm('Зупинити запис і видалити його з RAG-ПК? Нічого не буде збережено й передано в Minerva.')) return;
  Live.stopping = true;
  blinkTitle(false);
  hint('Зупиняю й видаляю…');
  try { if (Live.ws && Live.ws.readyState === 1) Live.ws.send(JSON.stringify({type: 'discard'})); } catch (e) { /* closing anyway */ }
  cleanupCapture();
  micNote('');
  const ws = Live.ws;
  clearTimeout(Live.closeTimer);
  Live.closeTimer = setTimeout(() => { if (ws && ws.readyState < 2) ws.close(); }, 60000);
}

// ---- the Minerva window ---------------------------------------------------------------------------------
function setMode(mode) { // minerva | landing | check | test
  document.body.classList.remove('boot');
  const test = mode === 'test';
  $$('.stage0').forEach((e) => { e.hidden = !test; });
  $('#landing').hidden = mode !== 'landing';
  $('#check-ok').hidden = mode !== 'check';
  $('main').hidden = !(mode === 'minerva' || test);
  $('#page-title').textContent = test ? '🎙 Стенд нарад · тестовий режим без Minerva' : '🎙 Вікно запису наради';
  document.title = test ? 'Стенд нарад — тест' : 'Вікно запису наради';
}

function mnReady() { return $('#mn-announced').checked && $('#mn-agreed').checked; }

function updateStart() {
  if (!MN.active) return;
  let text = '';
  if (MN.dead) text = MN.dead;
  else if (MN.recorded) text = 'Цей запис уже передано на розшифровку. Зупинили випадково? У Minerva — «🎙 Записати продовження».';
  else if (!Live.running && !Live.starting && !mnReady()) text = 'Спершу прочитайте текст вголос і поставте обидві галочки — тоді «▶ Почати» запрацює.';
  const note = $('#mn-start-note');
  note.textContent = text;
  note.className = `hint ${MN.dead ? 'bad' : ''}`;
  note.hidden = !text;
  if (!Live.running && !Live.starting) $('#lv-start').disabled = !!(MN.dead || MN.recorded || !mnReady());
}

function consentText(lang) {
  // {ask, after}: the question read before ▶, the sentence said once the recording runs
  const texts = (MN.view && MN.view.consent_texts) || {};
  MN.lang = texts[lang] ? lang : (Object.keys(texts)[0] || lang);
  const t = texts[MN.lang] || {};
  $('#mn-text').textContent = t.ask || 'Текст для читання вголос — на сторінці наради в Minerva.';
  $('#mn-after').textContent = t.after ? `«${t.after}»` : '';
  $('#mn-after-box').hidden = !t.after;
  $$('#mn-langs button').forEach((b) => b.classList.toggle('on', b.dataset.lang === MN.lang));
}

function fillLangs(view) {
  for (const [id, value] of [['#lv-lang-l', view.lang_l], ['#lv-lang-r', view.lang_r]]) {
    const s = $(id);
    s.replaceChildren(...MN_LANGS.map(([v, t]) => el('option', {value: v, text: t})));
    s.value = value || 'auto';
  }
}

function setBack(url) {
  for (const id of ['#mn-back', '#mn-done-back']) {
    const a = $(id);
    const ok = typeof url === 'string' && url.startsWith('https://');
    a.hidden = !ok;
    if (ok) a.href = url;
  }
}

function minervaUi(view) {
  Object.assign(MN, {active: true, view, binding: view.binding, maxMinutes: view.max_minutes || 240});
  setMode('minerva');
  $('#mn-banner').hidden = false;
  $('#mn-error').hidden = true;
  const title = view.title || 'без назви';
  const code = view.project_code && !title.includes(view.project_code) ? ` (${view.project_code})` : '';
  $('#mn-title').textContent = `🎙 Нарада Minerva: ${title}${code}.`;
  const mode = view.retention === 'audio_text'
    ? `Режим «аудіо + текст»: аудіо лишається на RAG-ПК до ${view.audio_days || 30} днів.`
    : 'Режим «лише текст»: аудіо видаляється з RAG-ПК одразу після розшифровки.';
  $('#mn-mode').textContent = `Розшифровка й ключові моменти підуть лише на вашу сторінку наради в Minerva, нічого — у хмару. ${mode}`;
  $('#mn-until').textContent = view.used ? '' : `Почніть запис до ${view.expires_hm}.`;
  $('#mn-busy').textContent = view.live_busy ? BUSY_TEXT : '';
  $('#mn-busy').hidden = !view.live_busy;
  setBack(view.return_url);
  $('#lv-engine-legend').textContent = 'Текст під час запису';
  $('#lv-engine-parakeet span').textContent = 'Показувати текст під час запису';
  $('#lv-engine-none span').textContent = 'Без тексту — лише запис';
  if (radio('lv-engine') === 'whisper') $('input[name="lv-engine"][value="parakeet"]').checked = true;
  $('#lv-lang-note').textContent = 'Мову бере до уваги розшифровка після запису. Нарада однією мовою — виберіть її; ' +
    'кілька мов — «Автоматично».';
  fillLangs(view);
  const texts = view.consent_texts || {};
  $('#mn-langs').replaceChildren(...['de', 'en', 'uk'].filter((l) => texts[l]).map((l) => {
    const b = button(CONSENT_NAMES[l], () => consentText(l));
    b.dataset.lang = l;
    return b;
  }));
  consentText(view.consent_lang || 'de');
  $('#mn-consent').hidden = !!view.used;
  $('#mn-rec-buttons').hidden = false;
  showTab('live');
  if (view.used) {
    MN.recorded = true;
    afterRecording('Цей запис уже передано на розшифровку.');
  }
  setLiveButtons(false);
}

function minervaError(text) {
  MN.active = true; // never falls back to the test bench
  setMode('minerva');
  $('main').hidden = true;
  $('#mn-banner').hidden = true;
  $('#mn-error').replaceChildren(el('p', {class: 'big', text: `⚠️ ${text}`}),
    /можна закрити/.test(text) ? '' : el('p', {class: 'muted', text: 'Це вікно можна закрити.'}));
  $('#mn-error').hidden = false;
}

function afterRecording(text, warn = '') {
  $('#mn-consent').hidden = true;
  $('#mn-until').textContent = '';
  if (text) $('#mn-done-text').replaceChildren(el('div', {text}), warn ? el('div', {class: 'hint warn', text: warn}) : '');
  $('#mn-done').hidden = false;
  updateStart();
  pollProgress();
}

async function pollProgress() {
  clearTimeout(MN.pollTimer);
  const token = MN.progress || tabStore.get('bench-progress');
  const box = $('#mn-progress');
  if (!token) { box.textContent = 'Хід обробки видно на сторінці наради в Minerva.'; return; }
  try {
    const p = await api(`/api/minerva/progress/${encodeURIComponent(token)}`);
    box.textContent = p.text;
    box.className = `hint ${p.state === 'failed' ? 'bad' : 'ok'}`;
    if (!p.done) MN.pollTimer = setTimeout(pollProgress, 5000);
  } catch (e) {
    box.textContent = e.message;
    box.className = 'hint';
  }
}

async function minervaRedeem(ticket) {
  MN.active = true;
  setMode('minerva');
  $('main').hidden = true;
  $('#mn-banner').hidden = false;
  $('#mn-title').textContent = '🎙 Звіряю нараду з Minerva…';
  tabStore.set('bench-binding', '');
  tabStore.set('bench-progress', '');
  try {
    const view = await api('/api/minerva/redeem', {json: {ticket}});
    tabStore.set('bench-binding', view.binding);
    minervaUi(view);
  } catch (e) { minervaError(e.message); }
}

async function minervaRestore(bid) {
  MN.active = true;
  setMode('minerva');
  $('main').hidden = true;
  try {
    minervaUi(await api(`/api/minerva/binding/${encodeURIComponent(bid)}`));
  } catch (e) {
    tabStore.set('bench-binding', '');
    minervaError(e.message);
  }
}

async function startPage() {
  const frag = location.hash || '';
  if (frag.startsWith('#minerva=')) {
    // the ticket leaves the address bar at once (history, screenshots); it works only once anyway
    history.replaceState(null, '', location.pathname + location.search);
    minervaRedeem(decodeURIComponent(frag.slice('#minerva='.length)));
    return;
  }
  const bid = tabStore.get('bench-binding');
  if (frag !== '#check' && bid) { minervaRestore(bid); return; }
  let s = null;
  try { s = await api('/api/status'); } catch (e) { s = null; }
  if (frag === '#check') {
    setMode('check');
    const m = (s && s.minerva) || {};
    $('#check-link').textContent = m.configured && m.key_saved ? 'RAG-ПК підключено до Minerva.'
      : '⚠️ RAG-ПК ще не підключено до Minerva — скажіть адміністратору («RAG — керування» → «🎙 Наради»).';
    return;
  }
  if (s && s.standalone) {
    setMode('test');
    api('/api/sessions').then((d) => { $('#count').textContent = d.sessions.length ? `(${d.sessions.length})` : ''; }).catch(() => {});
  } else setMode('landing');
}

async function startTranscribe(id, model, open) {
  try {
    await api(`/api/sessions/${id}/transcribe`, {json: {model, device: 'auto', allow_unload: true}});
    if (open) openSession(id); else refreshSession();
  } catch (e) { alert(e.message); }
}

// ---- translation in the browser (Chrome 138+ / Edge 148+) ------------------------------------
const TR = {on: false, target: 'uk', translators: {}, detector: null, offers: {}};

async function trToggle() {
  const box = $('#tr-on');
  const note = $('#tr-note');
  if (!box.checked) { TR.on = false; note.textContent = ''; return; }
  if (!('Translator' in self)) {
    box.checked = false;
    note.textContent = 'Цей браузер не має вбудованого перекладача (потрібен Chrome 138+ або Edge 148+).';
    return;
  }
  TR.target = $('#tr-target').value;
  TR.translators = {};
  note.textContent = 'Готую перекладач…';
  try {
    if ('LanguageDetector' in self && !TR.detector) TR.detector = await LanguageDetector.create();
    const sources = ['de', 'en', 'uk', 'ru'].filter((s) => s !== TR.target);
    await Promise.all(sources.map(async (src) => {
      const a = await Translator.availability({sourceLanguage: src, targetLanguage: TR.target});
      if (a !== 'unavailable') TR.translators[src] = await Translator.create({sourceLanguage: src, targetLanguage: TR.target});
    }));
    TR.on = true;
    note.textContent = `Переклад з: ${Object.keys(TR.translators).join(', ') || '—'}. Лише на екрані, нічого не зберігається.`;
  } catch (e) {
    box.checked = false;
    note.textContent = `Перекладач не запустився: ${e.message}`;
  }
}

async function translateLine(text, lang, node) {
  if (!TR.on || !text) return;
  try {
    let src = lang;
    if (!src && TR.detector) {
      const found = await TR.detector.detect(text);
      if (found && found[0] && found[0].confidence > 0.5) src = found[0].detectedLanguage;
    }
    if (!src || src === TR.target) return;
    const t = TR.translators[src.split('-')[0]];
    if (!t) return;
    const out = await t.translate(text);
    node.append(el('span', {class: 'tr', text: '↳ ' + out}));
  } catch (e) { /* translation is optional */ }
}

// ---- upload -----------------------------------------------------------------------------------
function upMsg(text, ok = false) {
  const m = $('#up-msg');
  m.textContent = text;
  m.classList.toggle('ok', ok);
}

function upload() {
  const f = $('#up-file').files[0];
  if (!f) { upMsg('Виберіть файл.'); return; }
  if (!$('#up-consent').checked) { upMsg('Поставте галочку про згоду всіх, кого чути.'); return; }
  if (STATUS && f.size > STATUS.max_upload_mb * 1048576) { upMsg(`Файл більший за ${STATUS.max_upload_mb} МБ.`); return; }
  const fd = new FormData();
  fd.append('title', $('#up-title').value.trim() || f.name.replace(/\.[^.]+$/, ''));
  fd.append('lang', $('#up-lang').value);
  fd.append('split', $('#up-split').checked ? '1' : '0');
  fd.append('model', $('#up-model').value);
  fd.append('hotwords', $('#up-hotwords').value.trim());
  fd.append('allow_unload', $('#up-unload').checked ? '1' : '0');
  fd.append('consent', '1');
  fd.append('transcribe', '1');
  fd.append('file', f, f.name); // last: the server reads the fields before the file
  const prog = $('#up-progress');
  prog.hidden = false;
  prog.value = 0;
  $('#up-send').disabled = true;
  upMsg('Передаю файл на RAG-ПК…', true);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/upload');
  xhr.setRequestHeader('X-Bench', '1');
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) prog.value = e.loaded / e.total * 100; };
  xhr.onload = () => {
    $('#up-send').disabled = false;
    let d = {};
    try { d = JSON.parse(xhr.responseText); } catch (e) { d = {}; }
    if (xhr.status === 200) {
      upMsg('Файл на RAG-ПК, розшифровка в черзі.', true);
      $('#up-file').value = '';
      openSession(d.id);
    } else upMsg(d.error || `Помилка ${xhr.status}`);
  };
  xhr.onerror = () => { $('#up-send').disabled = false; upMsg('Не вдалося передати файл.'); };
  xhr.send(fd);
}

// ---- list and measurements ---------------------------------------------------------------------
function jobText(s) {
  if (s.job && (s.job.state === 'queued' || s.job.state === 'running')) return `⏳ ${s.job.text}`;
  if (s.job && s.job.state === 'error') return `⚠️ ${s.job.text}`;
  if (s.transcript) return `✅ ${s.transcript_model || ''}`;
  if (s.error) return `⚠️ ${s.error.message}`;
  return '—';
}

async function loadList() {
  let d;
  try { d = await api('/api/sessions'); } catch (e) { return; }
  $('#count').textContent = d.sessions.length ? `(${d.sessions.length})` : '';
  const body = $('#list-body');
  body.replaceChildren();
  $('#list-empty').hidden = d.sessions.length > 0;
  for (const s of d.sessions) {
    const tr = el('tr', {class: 'clickable'},
      el('td', {class: 'num', text: s.created || ''}),
      el('td', {text: `${s.kind === 'live' ? '🔴' : '📁'} ${s.title || '—'}${(s.silent || []).length ? ' · 🔇 ' +
        s.silent.map((c) => (c === 'L' ? 'мікрофон мовчав' : 'без звуку наради')).join(', ') : ''}`}),
      el('td', {class: 'num', text: s.duration ? clock(s.duration) : '—'}),
      el('td', {text: jobText(s)}),
      el('td', {class: 'num', text: s.moments === null || s.moments === undefined ? '—' : String(s.moments)}),
      el('td', {}, button('Відкрити', (e) => { e.stopPropagation(); openSession(s.id); })));
    tr.addEventListener('click', () => openSession(s.id));
    body.append(tr);
  }
}

const METRIC_COLS = [
  ['created', 'Коли'], ['title', 'Назва'], ['duration_s', 'Тривалість, с'], ['live_engine', 'Наживо'],
  ['live_wait_ms', 'Черга наживо, мс'], ['live_cpu_avg', 'Процесор наживо, %'], ['whisper_model', 'Розшифровка'],
  ['device', 'Пристрій'], ['compute_type', 'Обчислення'], ['load_s', 'Модель, с'], ['transcribe_s', 'Розшифровка, с'],
  ['speed_x', 'Швидкість ×'], ['vram_peak_mb', 'Пік відеопам’яті, МБ'], ['ollama_unloaded', 'Вивантажено з Ollama'],
  ['llm', 'Модель моментів'], ['llm_cost_usd', 'Моменти, $'], ['llm_s', 'Моменти, с'], ['llm_tok_s', 'Токенів/с'], ['llm_gpu_share', 'У відеокарті, %'],
  ['moments', 'Моментів'], ['moments_rejected', 'Відкинуто'],
];

async function loadMetrics() {
  let d;
  try { d = await api('/api/metrics'); } catch (e) { return; }
  const t = $('#metrics-table');
  t.replaceChildren(el('thead', {}, el('tr', {}, ...METRIC_COLS.map(([, h]) => el('th', {text: h})))));
  const body = el('tbody');
  for (const r of d.rows) {
    const tr = el('tr', {class: 'clickable'}, ...METRIC_COLS.map(([k]) => el('td', {class: typeof r[k] === 'number' ? 'num' : '', text: r[k] === null || r[k] === undefined || r[k] === '' ? '—' : (typeof r[k] === 'number' ? num(r[k], 2) : String(r[k]))})));
    tr.addEventListener('click', () => openSession(r.id));
    body.append(tr);
  }
  t.append(body);
}

// ---- one session ------------------------------------------------------------------------------
const SS = {id: null, data: null, timer: null, sig: ''};

async function openSession(id) {
  SS.id = id;
  SS.sig = '';
  resetConsent();
  showTab('session');
  SS.id = id;
  const audio = $('#ss-audio');
  audio.removeAttribute('src');
  audio.dataset.sid = '';
  await refreshSession();
}

async function refreshSession() {
  clearTimeout(SS.timer);
  if (!SS.id) return;
  let d;
  try { d = await api(`/api/sessions/${SS.id}`); } catch (e) { $('#ss-job').textContent = e.message; return; }
  SS.data = d;
  const m = d.meta || {};
  $('#ss-title').textContent = `${m.kind === 'live' ? '🔴' : '📁'} ${m.title || m.id}`;
  const info = [m.created, m.duration ? `тривалість ${clock(m.duration)}` : null,
    m.kind === 'live' ? ({both: 'нарада + мікрофон', mic: 'лише мікрофон', meeting: 'лише нарада'}[m.mode] || '') : (m.source_name || ''),
    m.size_mb ? `${num(m.size_mb)} МБ` : null, m.engine ? `наживо: ${m.engine}` : null];
  $('#ss-info').textContent = info.filter(Boolean).join(' · ');
  const audio = $('#ss-audio');
  if (audio.dataset.sid !== SS.id) {
    audio.dataset.sid = SS.id;
    if (d.has_audio) audio.src = `/api/sessions/${SS.id}/audio`;
  }
  renderLangControls(m);
  $('#ss-export').href = `/api/sessions/${SS.id}/export.txt`;
  renderJob(d);
  const sig = JSON.stringify([d.transcript && d.transcript.created, (d.live || []).length, d.moments && d.moments.created,
    (d.moments_runs || []).length, d.job && d.job.state]);
  if (sig !== SS.sig) {
    SS.sig = sig;
    renderLines(d);
    renderMoments(d.moments, !!(d.transcript && d.transcript.segments && d.transcript.segments.length));
    renderMetrics(d);
  }
  if (d.job && (d.job.state === 'queued' || d.job.state === 'running')) SS.timer = setTimeout(refreshSession, 2000);
}

function langSelect(id, value) {
  const s = el('select', {id, class: 'lang'});
  for (const [v, t] of LANGS) s.append(el('option', {value: v, text: t}));
  s.value = value || 'auto';
  return s;
}

function renderLangControls(m) {
  const box = $('#ss-langs');
  if (box.dataset.sid === m.id) return;
  box.dataset.sid = m.id;
  box.replaceChildren();
  if (m.kind === 'live') {
    if (m.mode !== 'meeting') box.append('Ви: ', langSelect('ss-lang-l', m.lang_l));
    if (m.mode !== 'mic') box.append(' Співрозмовники: ', langSelect('ss-lang-r', m.lang_r));
  } else box.append('Мова: ', langSelect('ss-lang', m.lang));
}

function renderJob(d) {
  const box = $('#ss-job');
  box.replaceChildren();
  const j = d.job;
  if (j && (j.state === 'queued' || j.state === 'running')) {
    box.append(el('div', {text: `⏳ ${j.text}`}));
    if (j.progress !== null && j.progress !== undefined) box.append(el('progress', {max: '100', value: String(Math.round(j.progress * 100))}));
  } else if (j && j.state === 'error') box.append(el('div', {class: 'hint', text: `⚠️ ${j.text}`}));
  else if (d.error) box.append(el('div', {class: 'hint', text: `⚠️ ${d.error.message}`}));
  const busy = j && (j.state === 'queued' || j.state === 'running');
  $('#ss-transcribe').disabled = !!busy;
  const o = $('#ss-llm').selectedOptions[0];
  const needsConsent = !!(o && o.dataset.cloud) && !consentGiven();
  $('#ss-moments').disabled = !!busy || !o || !o.value || needsConsent || !(d.transcript && d.transcript.segments && d.transcript.segments.length);
}

function seek(t) {
  const a = $('#ss-audio');
  if (!a.src) return;
  a.currentTime = Math.max(0, t - 0.3);
  a.play().catch(() => {});
}

function renderLines(d) {
  const box = $('#ss-lines');
  box.replaceChildren();
  const tr = d.transcript;
  if (tr && tr.segments) {
    const langs = Object.entries(tr.languages || {}).map(([ch, l]) => `${WHO[ch] || 'мова'}: ${l.language}`).join(', ');
    $('#ss-lines-title').textContent = `Розшифровка · ${tr.model}${langs ? ' · ' + langs : ''}`;
    if (!tr.segments.length) box.append(el('p', {class: 'muted', text: 'Мови не знайдено (тиша або дуже тихий запис).'}));
    for (const s of tr.segments) {
      const line = lineEl(s.ch, s.start, s.text);
      $('.t', line).addEventListener('click', () => seek(s.start));
      line.dataset.text = s.text.toLowerCase();
      box.append(line);
    }
  } else if (d.live && d.live.length) {
    $('#ss-lines-title').textContent = 'Текст наживо (точної розшифровки ще немає)';
    for (const s of d.live) {
      const line = lineEl(s.ch, s.t0, s.text);
      $('.t', line).addEventListener('click', () => seek(s.t0));
      line.dataset.text = s.text.toLowerCase();
      box.append(line);
    }
  } else {
    $('#ss-lines-title').textContent = 'Розшифровка';
    box.append(el('p', {class: 'muted', text: 'Ще немає тексту — натисніть «📝 Розшифрувати».'}));
  }
  for (const mk of d.marks || []) box.append(el('div', {class: 'line mark', text: `📌 ${clock(mk.t)} ${mk.note || ''}`}));
  search();
}

function search() {
  const q = $('#ss-search').value.trim().toLowerCase();
  let first = null;
  for (const line of $$('#ss-lines .line')) {
    const hit = q && line.dataset.text && line.dataset.text.includes(q);
    line.classList.toggle('hit', !!hit);
    if (hit && !first) first = line;
  }
  if (first) first.scrollIntoView({block: 'center'});
}

function renderMoments(mo, hasTranscript) {
  const box = $('#ss-moments-box');
  box.replaceChildren();
  if (!mo) {
    box.append(el('p', {class: 'muted', text: hasTranscript ? 'Ще не шукали — оберіть модель і натисніть «Знайти».'
      : 'Спершу потрібна точна розшифровка («📝 Розшифрувати»).'}));
    return;
  }
  const mt = mo.metrics || {};
  box.append(el('p', {class: 'muted', text: mt.provider && mt.provider !== 'ollama'
    ? `☁️ ${mt.served_by || mt.model} · ${num(mt.seconds)} с · токенів: ${num(mt.prompt_tokens, 0)} → ${num(mt.eval_tokens, 0)}` +
      ` · ≈ $${num(mt.cost_usd, 3)}${mt.fallbacks ? ' · відповіла запасна модель' : ''} · ${mo.created || ''}`
    : `${mt.model} · ${num(mt.seconds)} с · ${num(mt.eval_tok_s)} токенів/с` +
      (mt.gpu_share !== undefined ? ` · у відеокарті ${mt.gpu_share}%` : '') + ` · ${mo.created || ''}`}));
  if (mo.summary) box.append(el('p', {text: mo.summary}));
  if (!mo.items.length) box.append(el('p', {class: 'muted', text: 'Моментів не знайдено.'}));
  for (const kind of KIND_ORDER) {
    for (const it of mo.items.filter((i) => i.kind === kind)) {
      const meta = [it.who && `хто: ${it.who}`, it.due && `строк: ${it.due}`, it.speaker].filter(Boolean).join(' · ');
      box.append(el('div', {class: 'moment'},
        el('span', {class: 'k', text: KIND[kind] || kind}), ' ', it.text,
        meta ? el('div', {class: 'meta', text: meta}) : null,
        el('span', {class: 'q', text: `«${it.quote}»`}),
        button(`▶ ${clock(it.start)}`, () => seek(it.start))));
    }
  }
  if (mo.rejected && mo.rejected.length) {
    const det = el('details', {}, el('summary', {class: 'muted', text: `Відкинуто ${mo.rejected.length}: цитати немає в розшифровці`}));
    for (const r of mo.rejected) det.append(el('div', {class: 'moment muted', text: `${KIND[r.kind] || r.kind}: ${r.text} — «${r.quote}»`}));
    box.append(det);
  }
}

function kv(pairs) {
  const dl = el('dl', {class: 'kv'});
  for (const [k, v] of pairs) if (v !== null && v !== undefined && v !== '') dl.append(el('dt', {text: k}), el('dd', {text: String(v)}));
  return dl;
}

const CH_NAMES = {L: '🎤 Мікрофон (ви)', R: '🖥 Звук наради'};

function levelText(lv) {
  if (!lv || lv.peak_dbfs === null || lv.peak_dbfs === undefined) return 'тиша (самі нулі)';
  const secs = lv.sound_s === undefined ? '' : ` (${num(lv.sound_s)} с)`;
  return `середній ${num(lv.rms_dbfs)} дБ · найгучніше ${num(lv.peak_dbfs)} дБ · зі звуком ${num((lv.sound_share || 0) * 100, 0)}% часу${secs}`;
}

function renderSound(box, m) {
  const levels = m.levels || {};
  const cap = m.capture || {};
  if (!Object.keys(levels).length && !cap.mic && !cap.display) return;
  const rows = [];
  for (const ch of ['L', 'R']) if (levels[ch]) rows.push([CH_NAMES[ch], levelText(levels[ch])]);
  if (cap.mic) {
    const s = cap.mic.settings || {};
    rows.push(['Мікрофон', `${cap.mic.label || 'без назви'} · обробка: ${PROC_NAMES[cap.mic.processing] || cap.mic.processing || '—'}` +
      ` (луна: ${s.echoCancellation === undefined ? '—' : s.echoCancellation}, шум: ${s.noiseSuppression === undefined ? '—' : s.noiseSuppression},` +
      ` підсилення: ${s.autoGainControl === undefined ? '—' : s.autoGainControl})` + (s.sampleRate ? ` · ${s.sampleRate} Гц` : '') +
      (cap.mic.muted ? ' · ⚠️ приглушений' : '')]);
  }
  if (cap.display) rows.push(['Звук наради з', cap.display.label || 'вкладка / вікно / екран']);
  if (cap.browser) rows.push(['Браузер', cap.browser]);
  box.append(el('p', {class: 'muted', text: 'Звук'}), kv(rows));
  const silent = m.silent || [];
  if (silent.length) {
    box.append(el('div', {class: 'hint warn', text: silent.map((c) => c === 'L'
      ? '⚠️ Мікрофон у цьому записі майже мовчав. Перед наступним записом натисніть «🎤 Перевірити мікрофон» на вкладці «Наживо».'
      : '⚠️ Звуку наради в цьому записі не було: у вибраній вкладці чи вікні нічого не звучало або не стояла галочка «Поділитися звуком».').join(' ')}));
  }
}

function renderMetrics(d) {
  const box = $('#ss-metrics');
  box.replaceChildren();
  renderSound(box, d.meta || {});
  const live = (d.meta || {}).live;
  if (live) {
    box.append(el('p', {class: 'muted', text: 'Наживо'}), kv([
      ['Рушій', `${live.engine || '—'} (${live.device || '—'})`], ['Фраз', live.finals],
      ['Розпізнавання', live.rtf ? `×${num(1 / live.rtf)} швидше за мову` : null],
      ['Середній час на фразу', live.avg_decode_ms !== undefined ? `${live.avg_decode_ms} мс` : null],
      ['Чекання в черзі', live.avg_wait_ms !== null && live.avg_wait_ms !== undefined ? `${live.avg_wait_ms} мс` : null],
      ['Процесор', live.cpu_avg !== null && live.cpu_avg !== undefined ? `≈ ${live.cpu_avg}%, пік ${live.cpu_max}%` : null],
      ['Помилки', live.errors || null], ['Не встигло до кінця', live.unfinished ? 'так' : null]]));
  }
  const tm = d.transcript && d.transcript.metrics;
  if (tm) {
    box.append(el('p', {class: 'muted', text: 'Точна розшифровка'}), kv([
      ['Модель', `${tm.model} · ${tm.device} · ${tm.compute_type} · промінь ${tm.beam_size} · пакет ${tm.batch_size}`],
      ['Аудіо', `${clock(tm.audio_s)}` + (tm.channels_s > tm.audio_s + 1 ? ` (два канали разом ${clock(tm.channels_s)})` : '')],
      ['Час', `читання ${num(tm.decode_s)} с · модель ${num(tm.load_s)} с · розшифровка ${num(tm.transcribe_s)} с · разом ${num(tm.total_s)} с`],
      ['Швидкість', tm.speed_x ? `×${num(tm.speed_x)} швидше за реальний час` : null],
      ['Відеопам’ять', tm.vram_peak_mb ? `до ${num(tm.vram_before_mb, 0)} МБ → пік ${num(tm.vram_peak_mb, 0)} МБ` : null],
      ['Вивантажено з Ollama', (tm.ollama_unloaded || []).join(', ')], ['Тихі канали', (tm.skipped_silent || []).map((c) => WHO[c] || c).join(', ')],
      ['Примітка', tm.fallback]]));
  }
  const runs = d.moments_runs || [];
  if (runs.length) {
    const t = el('table', {class: 'list'}, el('thead', {}, el('tr', {}, ...['Коли', 'Модель', 'С', 'Токенів/с', 'У відеокарті, %', 'Ціна, $', 'Моментів', 'Відкинуто'].map((h) => el('th', {text: h})))));
    const b = el('tbody');
    for (const r of runs.slice().reverse()) {
      const model = r.error ? `${r.model} ⚠️ ${r.error}` : (r.served_by || r.model);
      b.append(el('tr', {}, ...[r.created, model, num(r.seconds), num(r.eval_tok_s), r.gpu_share,
        r.cost_usd === undefined || r.cost_usd === null ? null : num(r.cost_usd, 3), r.items, r.rejected]
        .map((v) => el('td', {text: v === undefined || v === null ? '—' : String(v)}))));
    }
    t.append(b);
    box.append(el('p', {class: 'muted', text: 'Пошук ключових моментів'}), t);
  }
  if (!box.childNodes.length) box.append(el('p', {class: 'muted', text: 'Замірів ще немає.'}));
}

async function sessionTranscribe() {
  const params = {model: $('#ss-model').value, device: $('#ss-device').value, allow_unload: $('#ss-unload').checked,
    hotwords: $('#ss-hotwords').value.trim()};
  for (const [id, key] of [['#ss-lang', 'lang'], ['#ss-lang-l', 'lang_l'], ['#ss-lang-r', 'lang_r']]) if ($(id)) params[key] = $(id).value;
  try {
    await api(`/api/sessions/${SS.id}/transcribe`, {json: params});
    await refreshSession();
  } catch (e) { alert(e.message); }
}

async function sessionMoments() {
  const o = $('#ss-llm').selectedOptions[0];
  if (!o || !o.value) return;
  const cloud = !!o.dataset.cloud;
  if (cloud && !consentGiven()) { alert('Поставте галочку: учасники погодились на обробку тексту в хмарі.'); return; }
  try {
    // the server accepts the consent only for this recording's id
    await api(`/api/sessions/${SS.id}/moments`, {json: {choice: o.value, lang: $('#ss-outlang').value, cloud_consent: cloud ? SS.id : false}});
    if (cloud) resetConsent();
    await refreshSession();
  } catch (e) { alert(e.message); }
}

async function sessionRename() {
  const now = (SS.data && SS.data.meta && SS.data.meta.title) || '';
  const title = prompt('Нова назва запису', now);
  if (title === null) return;
  try { await api(`/api/sessions/${SS.id}/rename`, {json: {title}}); SS.sig = ''; await refreshSession(); } catch (e) { alert(e.message); }
}

async function sessionDelete() {
  if (!confirm('Видалити запис з RAG-ПК разом з аудіо й розшифровкою? Повернути не можна.')) return;
  try { await api(`/api/sessions/${SS.id}/delete`, {json: {}}); showTab('list'); } catch (e) { alert(e.message); }
}

// ---- start -------------------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  for (const s of $$('select.lang')) for (const [v, t] of LANGS) s.append(el('option', {value: v, text: t}));
  $$('.tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
  const procs = [...$('#lv-proc').options].map((o) => o.value);
  $('#lv-proc').value = procs.includes(saved.get('bench-proc')) ? saved.get('bench-proc') : 'standard';
  // a check that is open or still opening is restarted with the new device / processing
  const recheck = () => { if (Check.stream || Check.opening) { cancelCheck(); micCheck(); } };
  $('#lv-proc').addEventListener('change', () => { saved.set('bench-proc', $('#lv-proc').value); recheck(); });
  $('#lv-mic').addEventListener('change', () => { saved.set('bench-mic', $('#lv-mic').value); recheck(); });
  $('#lv-check').addEventListener('click', micCheck);
  $('#lv-find').addEventListener('click', findMic);
  fillMics();
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) navigator.mediaDevices.addEventListener('devicechange', fillMics);
  $('#lv-start').addEventListener('click', liveStart);
  $('#lv-stop').addEventListener('click', () => liveStop(''));
  $('#lv-mark').addEventListener('click', () => sendMark(''));
  $('#lv-pause').addEventListener('click', togglePause);
  $('#mn-join').addEventListener('click', () => { sendMark('consent_join'); hint('🙋 Позначено: новий учасник погодився.', true); });
  $('#mn-ok').addEventListener('click', () => { sendMark('consent_ok'); hint('✅ Позначено: згоду підтверджено вголос.', true); });
  $('#mn-discard').addEventListener('click', liveDiscard);
  $('#mn-silence-stop').addEventListener('click', () => liveStop(''));
  $('#mn-announced').addEventListener('change', updateStart);
  $('#mn-agreed').addEventListener('change', updateStart);
  $('#tr-on').addEventListener('change', trToggle);
  $('#tr-target').addEventListener('change', () => { if ($('#tr-on').checked) trToggle(); });
  $('#up-send').addEventListener('click', upload);
  $('#ss-back').addEventListener('click', () => showTab('list'));
  $('#ss-transcribe').addEventListener('click', sessionTranscribe);
  $('#ss-moments').addEventListener('click', sessionMoments);
  $('#ss-llm').addEventListener('change', () => { LLM_PICK = $('#ss-llm').value; cloudBox(); });
  $('#ss-cloud-ok').addEventListener('change', (e) => {
    e.target.dataset.sid = e.target.checked ? SS.id || '' : '';
    if (SS.data) renderJob(SS.data);
  });
  $('#ss-rename').addEventListener('click', sessionRename);
  $('#ss-delete').addEventListener('click', sessionDelete);
  $('#ss-search').addEventListener('input', search);
  window.addEventListener('beforeunload', (e) => { if (Live.running) { e.preventDefault(); e.returnValue = ''; } });
  startPage();
  pollStatus();
});
