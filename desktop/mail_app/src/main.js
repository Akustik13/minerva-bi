'use strict';
/*
 * «Пошта Minerva» для Windows — тонка оболонка навколо справжньої пошти Minerva (/email/): вікно Chromium
 * показує ту саму сторінку, що й браузер, тож вигляд і функції збігаються й оновлюються разом із Minerva.
 * Програма додає те, чого немає в браузері: значок у треї з лічильником, сповіщення Windows про нові листи,
 * mailto:, запуск з Windows. Інші розділи Minerva відкриваються у звичайному браузері (src/nav.js).
 *
 * Сервер: email_assistant/desktop.py (режим «лише пошта» за User-Agent «MinervaMail/<версія>», підключення
 * кодом minerva-mail://pair?url=…&code=…, /email/unread-count/?new_after=… для сповіщень).
 */
const fs = require('fs');
const path = require('path');
const {
  app, BrowserWindow, Tray, Menu, Notification, nativeImage, shell, session, ipcMain, dialog, screen, clipboard,
} = require('electron');
const nav = require('./nav');
const { Config } = require('./config');
const winreg = require('./winreg');

const APP_ID = 'com.minerva.mail';
const PARTITION = 'persist:minerva';
const TITLE = 'Пошта Minerva';
const ASSETS = path.join(__dirname, '..', 'assets');
const ICON = path.join(ASSETS, 'icon.png');
const POLL_FOCUSED = 30 * 1000;
const POLL_BACKGROUND = 60 * 1000;
const NOTIFY_EACH = 3;           // до трьох нових листів — кожен окремо, більше — одне «N нових листів»
const RELEASES_URL = 'https://github.com/Akustik13/minerva-bi/releases/tag/mail-desktop';
const IS_PORTABLE = !!process.env.PORTABLE_EXECUTABLE_FILE;
const EXE = process.env.PORTABLE_EXECUTABLE_FILE || process.execPath;
const PERMISSIONS = new Set(['clipboard-read', 'clipboard-sanitized-write', 'notifications', 'fullscreen', 'media']);

let config;
let ses;
let win = null;            // головне вікно — пошта
let setupWin = null;       // «Підключення до Minerva»
let tray = null;
let quitting = false;
let pendingArg = null;     // mailto:, що прийшов до налаштування сервера
let updater = null;
let updateReady = '';
const notes = new Set();   // сповіщення живуть, доки їх не закрили (інакше збирач сміття забирає клік)
const badCerts = {};       // host → останній неперевірений сертифікат (для «Довіряти цьому сертифікату»)
const certAsked = new Set();
const poll = { timer: null, latest: null, count: 0, loggedOut: false, offline: '', toldLoggedOut: false };

const server = () => config.get('server') || '';

// ── Запуск ───────────────────────────────────────────────────────────────────

// Окремий профіль (налаштування, вхід) — для перевірки й кількох акаунтів: MINERVA_MAIL_PROFILE=<тека>
if (process.env.MINERVA_MAIL_PROFILE) app.setPath('userData', path.resolve(process.env.MINERVA_MAIL_PROFILE));

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_e, argv) => {
    const arg = nav.findArg(argv);
    if (arg) handleArg(arg);
    else if (!server()) openSetup();
    else showMain();
  });
  app.on('before-quit', () => { quitting = true; });
  app.on('window-all-closed', () => { /* працюємо у треї; завершує «Завершити роботу» */ });
  app.on('web-contents-created', (_e, contents) => {
    contents.on('will-attach-webview', (e) => e.preventDefault());
  });
  app.on('certificate-error', onCertificateError);
  app.whenReady().then(ready);
}

function ready() {
  config = new Config(app.getPath('userData'));
  app.setAppUserModelId(APP_ID);
  registerScheme();
  setupSession();
  setupIpc();
  createTray();
  const arg = nav.findArg(process.argv);
  const hidden = process.argv.includes('--hidden');
  if (!server()) {
    if (arg && /^minerva-mail:/i.test(arg)) handleArg(arg);
    else { pendingArg = arg; openSetup(); }
  } else {
    createMain(!hidden && !arg);
    if (arg) handleArg(arg);
    startPolling();
  }
  setupUpdater();
}

function registerScheme() {
  // minerva-mail:// у реєстрі (HKCU) — для встановленої й портативної програми; `npm start` реєстру не чіпає
  if (process.defaultApp) return;
  try { app.setAsDefaultProtocolClient('minerva-mail', EXE, []); } catch (e) { /* працює «Підключити кодом» */ }
}

// ── Сесія: User-Agent програми, дозволи, завантаження, сертифікати ───────────

function userAgent() {
  return `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) `
    + `Chrome/${process.versions.chrome} Safari/537.36 MinervaMail/${app.getVersion()}`;
}

function setupSession() {
  ses = session.fromPartition(PARTITION);
  const ua = userAgent();
  app.userAgentFallback = ua;
  ses.setUserAgent(ua);
  try {
    const have = ses.availableSpellCheckerLanguages || [];
    ses.setSpellCheckerLanguages(['uk', 'de-DE', 'en-US'].filter((l) => have.includes(l)));
  } catch (e) { /* без перевірки правопису */ }
  ses.setPermissionRequestHandler((_wc, perm, cb, details) => {
    if (perm === 'media' && (details.mediaTypes || []).some((t) => t !== 'audio')) return cb(false);   // лише мікрофон (🎤 диктування)
    cb(permitted(perm, details.requestingUrl));
  });
  ses.setPermissionCheckHandler((_wc, perm, origin, details) => {
    if (perm === 'media' && details && details.mediaType && details.mediaType !== 'audio') return false;
    return permitted(perm, origin);
  });
  ses.on('will-download', onDownload);
  ses.setCertificateVerifyProc((req, cb) => {
    if (req.errorCode === 0) return cb(-3);
    const c = req.certificate || {};
    badCerts[req.hostname] = { fingerprint: c.fingerprint, issuer: c.issuerName, subject: c.subjectName,
      error: req.verificationResult };
    const trusted = (config.get('trustedCerts') || {})[req.hostname];
    cb(trusted && trusted === c.fingerprint ? 0 : -3);
  });
}

function permitted(perm, url) {
  return !!server() && PERMISSIONS.has(perm) && nav.sameOrigin(url || '', server());
}

function onCertificateError(event, wc, url, error, certificate, callback) {
  let host = '';
  try { host = new URL(url).hostname; } catch (e) { /* */ }
  if (!server() || !nav.sameOrigin(url, server()) || certAsked.has(host)) return callback(false);
  event.preventDefault();
  certAsked.add(host);
  const owner = BrowserWindow.fromWebContents(wc) || undefined;
  const choice = askSync(owner, {
    type: 'warning', title: TITLE, buttons: ['Довіряти цьому сертифікату', 'Скасувати'], defaultId: 1, cancelId: 1,
    message: `Windows не підтвердив сертифікат сервера ${host}.`,
    detail: `Помилка: ${error}\nВидано: ${certificate.issuerName}\nДля: ${certificate.subjectName}\n`
      + `Відбиток: ${certificate.fingerprint}\n\nДовіряйте лише своєму серверу Minerva (наприклад, NAS у локальній мережі).`,
  });
  if (choice === 0) {
    config.set('trustedCerts', { ...(config.get('trustedCerts') || {}), [host]: certificate.fingerprint });
    callback(true);
    setTimeout(() => { certAsked.delete(host); poll.offline = ''; pollNow(500); }, 500);
  } else {
    callback(false);
  }
}

// ── Вікна ────────────────────────────────────────────────────────────────────

function webPrefs() {
  return {
    partition: PARTITION, contextIsolation: true, nodeIntegration: false, sandbox: true, spellcheck: true,
    plugins: true, webviewTag: false, navigateOnDragDrop: false, safeDialogs: true,
  };
}

function validBounds(b) {
  if (!b || !b.width || !b.height) return null;
  const ok = screen.getAllDisplays().some(({ workArea: a }) => b.x < a.x + a.width - 80 && b.x + b.width > a.x + 80
    && b.y < a.y + a.height - 40 && b.y + 40 > a.y);
  return ok ? b : { width: b.width, height: b.height, maximized: b.maximized };
}

function createMain(show) {
  if (win) { if (show) showMain(); return win; }
  const b = validBounds(config.get('bounds')) || {};
  win = new BrowserWindow({
    width: b.width || 1360, height: b.height || 860, x: b.x, y: b.y, minWidth: 760, minHeight: 480,
    title: TITLE, icon: ICON, show: false, backgroundColor: '#0e1018', autoHideMenuBar: true,
    webPreferences: webPrefs(),
  });
  win.setMenu(windowMenu());
  wire(win.webContents);
  win.once('ready-to-show', () => {
    if (!show) return;
    if (b.maximized) win.maximize();
    win.show();
  });
  win.on('page-title-updated', (e) => { e.preventDefault(); updateTitle(); });
  win.on('close', onMainClose);
  win.on('closed', () => { win = null; });
  win.on('focus', () => pollNow(1500));
  let saveTimer = null;
  const remember = () => { clearTimeout(saveTimer); saveTimer = setTimeout(saveBounds, 600); };
  win.on('resize', remember);
  win.on('move', remember);
  win.on('maximize', remember);
  win.on('unmaximize', remember);
  win.webContents.on('did-finish-load', () => {
    const z = Number(config.get('zoom')) || 0;
    if (z) win.webContents.setZoomLevel(z);
  });
  loadMail();
  updateTitle();
  return win;
}

function saveBounds() {
  if (!win || win.isDestroyed() || win.isMinimized()) return;
  const maximized = win.isMaximized();
  const n = maximized ? (config.get('bounds') || win.getNormalBounds()) : win.getBounds();
  config.set('bounds', { x: n.x, y: n.y, width: n.width, height: n.height, maximized });
}

function onMainClose(e) {
  saveBounds();
  if (quitting) return;
  if (!config.get('closeToTray')) { quitting = true; app.quit(); return; }
  e.preventDefault();
  win.hide();
  if (!config.get('trayHintShown')) {
    config.set('trayHintShown', true);
    notify(TITLE, 'Програма працює у треї й сповіщатиме про нові листи. Завершити — правий клік на значку → «Завершити роботу».',
      showMain);
  }
}

function showMain() {
  if (!server()) { openSetup(); return; }
  if (!win) createMain(false);
  if (win.isMinimized()) win.restore();
  if (!win.isVisible()) {
    const b = config.get('bounds');
    if (b && b.maximized && !win.isMaximized()) win.maximize();
    win.show();
  }
  win.focus();
}

function loadMail(subpath = '/email/') {
  if (!win || !server()) return;
  win.loadURL(server() + subpath).catch(() => { /* did-fail-load показує сторінку «немає зв'язку» */ });
}

function openChild(url) {
  const child = new BrowserWindow({
    width: 1100, height: 820, title: TITLE, icon: ICON, backgroundColor: '#0e1018', autoHideMenuBar: true,
    webPreferences: webPrefs(),
  });
  prepareChild(child);
  child.loadURL(url).catch(() => {});
  return child;
}

function childOptions() {
  return { width: 1100, height: 820, icon: ICON, backgroundColor: '#0e1018', autoHideMenuBar: true, webPreferences: webPrefs() };
}

function prepareChild(child) {
  child.setMenu(windowMenu());
  wire(child.webContents);
  const z = Number(config.get('zoom')) || 0;
  child.webContents.on('did-finish-load', () => { if (z) child.webContents.setZoomLevel(Number(config.get('zoom')) || 0); });
}

// Навігація: пошта — у вікні програми, решта Minerva й інші сайти — у браузері (src/nav.js)
function wire(wc) {
  wc.setWindowOpenHandler(({ url }) => {
    const kind = nav.classify(url, server());
    if (kind === 'app') return { action: 'allow', overrideBrowserWindowOptions: childOptions() };
    route(kind, url);
    return { action: 'deny' };
  });
  wc.on('did-create-window', (child) => prepareChild(child));
  const guard = (e, url) => {
    const target = (e && e.url) || url;
    const kind = nav.classify(target, server());
    if (kind === 'app') return;
    e.preventDefault();
    if (kind === 'home') { wc.loadURL(server() + '/email/').catch(() => {}); return; }
    route(kind, target);
  };
  wc.on('will-navigate', guard);
  wc.on('will-redirect', (e, url) => {
    const isMain = e && typeof e.isMainFrame === 'boolean' ? e.isMainFrame : true;
    if (isMain) guard(e, url);
  });
  wc.on('will-frame-navigate', (e) => {
    if (e.isMainFrame || nav.isPdfViewer(e.url)) return;     // кадр документа в переглядачі PDF (👁 вкладення)
    const kind = nav.classify(e.url, server());
    if (kind === 'block' || kind === 'pair' || kind === 'mailto') { e.preventDefault(); route(kind, e.url); }
  });
  wc.on('will-prevent-unload', (e) => {
    const owner = BrowserWindow.fromWebContents(wc) || undefined;
    const choice = askSync(owner, {
      type: 'question', title: TITLE, buttons: ['Залишити сторінку', 'Залишитися'], defaultId: 1, cancelId: 1,
      message: 'Залишити сторінку?', detail: 'Зміни, які ви внесли, можуть не зберегтися.',
    });
    if (choice === 0) e.preventDefault();
  });
  wc.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3 || wc !== (win && win.webContents)) return;   // -3 — перехід скасовано
    if (!nav.sameOrigin(url, server())) return;
    win.loadFile(path.join(__dirname, 'offline.html'), { query: { url: server(), err: `${desc} (${code})` } });
    setOffline(`${desc}`);
  });
  wc.on('render-process-gone', () => setTimeout(() => { if (!wc.isDestroyed()) wc.reload(); }, 1000));
  wc.on('zoom-changed', (_e, dir) => zoom(dir === 'in' ? 0.5 : -0.5));
  wc.on('context-menu', (_e, p) => contextMenu(wc, p));
}

function route(kind, url) {
  if (kind === 'browser') openExternal(url);
  else if (kind === 'mailto') { if (server()) openChild(nav.mailtoToCompose(url, server())); }
  else if (kind === 'pair') handleArg(url);
  else if (kind === 'home') { showMain(); loadMail(); }
}

function openExternal(url) {
  let u;
  try { u = new URL(url); } catch (e) { return; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
  if (AUTO) { console.log('[external]', u.href); return; }
  shell.openExternal(u.href);
}

function zoom(delta) {
  const z = delta === 0 ? 0 : Math.max(-3, Math.min(4, (Number(config.get('zoom')) || 0) + delta));
  config.set('zoom', z);
  BrowserWindow.getAllWindows().forEach((w) => {
    if (w !== setupWin && !w.isDestroyed()) w.webContents.setZoomLevel(z);
  });
}

// ── mailto:, minerva-mail://pair, minerva-mail://setup ───────────────────────

function handleArg(arg) {
  if (!arg) return;
  if (/^mailto:/i.test(arg)) {
    if (!server()) { pendingArg = arg; openSetup(); return; }
    openChild(nav.mailtoToCompose(arg, server()));
    return;
  }
  let u;
  try { u = new URL(arg); } catch (e) { return; }
  if (u.protocol !== 'minerva-mail:') return;
  if (u.hostname === 'setup') { openSetup(); return; }
  const p = nav.parsePair(arg);
  if (!p) {
    tell({ type: 'error', title: TITLE, message: 'Посилання підключення пошкоджене.',
      detail: 'У браузері на сторінці «🖥 Пошта Minerva для Windows» натисніть «🔗 Підключити програму» ще раз.' });
    return;
  }
  const cur = server();
  if (cur !== p.server) {
    const host = new URL(p.server).host;
    const choice = askSync({
      type: 'question', title: TITLE, buttons: ['Підключити', 'Скасувати'], defaultId: 0, cancelId: 1,
      message: cur ? `Підключити програму до іншої Minerva — ${host}?` : `Підключити програму до Minerva — ${host}?`,
      detail: cur ? `Зараз програма підключена до ${new URL(cur).host}.` : 'Далі Minerva спитає, до якого акаунта підключити.',
    });
    if (choice !== 0) return;
    connectTo(p.server, p.code);
    return;
  }
  connectTo(p.server, p.code);
}

function connectTo(origin, code) {
  const changed = origin !== server();
  config.set('server', origin);
  if (setupWin) { const w = setupWin; setupWin = null; w.destroy(); }
  if (changed) resetPoll();
  createMain(false);
  win.loadURL(code ? nav.pairUrl(origin, code) : origin + '/email/').catch(() => {});
  showMain();
  startPolling();
  if (pendingArg && /^mailto:/i.test(pendingArg)) { const a = pendingArg; pendingArg = null; setTimeout(() => handleArg(a), 1500); }
}

// ── Підключення до Minerva (перший запуск, «Змінити сервер», «Підключити кодом») ─

function openSetup() {
  if (setupWin) { setupWin.show(); setupWin.focus(); return; }
  setupWin = new BrowserWindow({
    width: 560, height: 700, resizable: false, maximizable: false, minimizable: true, title: `${TITLE} — підключення`,
    icon: ICON, backgroundColor: '#080b0f', autoHideMenuBar: true, show: false,
    webPreferences: { preload: path.join(__dirname, 'preload-setup.js'), contextIsolation: true, sandbox: true,
      nodeIntegration: false, spellcheck: false },
  });
  setupWin.setMenu(null);
  setupWin.webContents.setWindowOpenHandler(({ url }) => { openExternal(url); return { action: 'deny' }; });
  setupWin.webContents.on('will-navigate', (e, url) => { e.preventDefault(); openExternal((e && e.url) || url); });
  setupWin.once('ready-to-show', () => setupWin && setupWin.show());
  setupWin.on('closed', () => {
    setupWin = null;
    if (!server() && !quitting) app.quit();   // нічого не налаштовано — програмі нічого робити
  });
  setupWin.loadFile(path.join(__dirname, 'setup.html'));
}

function fromSetup(e) {
  return !!setupWin && e.sender === setupWin.webContents && String(e.senderFrame && e.senderFrame.url).startsWith('file:');
}

async function checkServer(origin) {
  const host = new URL(origin).hostname;
  delete badCerts[host];
  const get = (p) => ses.fetch(origin + p, { signal: AbortSignal.timeout(15000), headers: { Accept: 'application/json, text/html' } });
  try {
    const r = await get('/email/app/hello/');
    if (r.ok) {
      const d = await r.json().catch(() => null);
      if (d && d.minerva === 'mail') return { ok: true, pair: !!d.pair };
    }
    // Minerva без оновлення (немає /email/app/hello/) — вхід паролем працює, підключення кодом — ні
    const r2 = await get('/admin/login/');
    const text = r2.ok ? await r2.text() : '';
    if (/Minerva/i.test(text)) return { ok: true, pair: false, old: true };
    return { ok: false, error: `За адресою ${new URL(origin).host} немає Minerva (відповідь ${r.status}). Перевірте адресу.` };
  } catch (err) {
    const cert = badCerts[host];
    if (cert) return { ok: false, cert, error: `Windows не підтвердив сертифікат сервера ${host} (${cert.error}).` };
    return { ok: false, error: `Немає зв'язку з ${new URL(origin).host}: ${err && err.message ? err.message : err}` };
  }
}

function setupIpc() {
  ipcMain.handle('setup:init', (e) => {
    if (!fromSetup(e)) return null;
    return { server: server(), version: app.getVersion() };
  });
  ipcMain.handle('setup:connect', async (e, data) => {
    if (!fromSetup(e)) return { ok: false, error: 'Недоступно' };
    const origin = nav.normalizeServer(data && data.server);
    if (!origin) return { ok: false, error: 'Вкажіть адресу Minerva, наприклад https://minerva.example.com' };
    const code = String((data && data.code) || '').trim();
    if (code && !/^[\w-]{10,100}$/.test(code)) return { ok: false, error: 'Код підключення має бути таким, як на сторінці Minerva (букви, цифри, - і _).' };
    const host = new URL(origin).hostname;
    if (data && data.trust && badCerts[host] && badCerts[host].fingerprint) {
      config.set('trustedCerts', { ...(config.get('trustedCerts') || {}), [host]: badCerts[host].fingerprint });
    }
    const check = await checkServer(origin);
    if (!check.ok) return check;
    if (code && !check.pair) {
      return { ok: false, error: 'Ця Minerva ще не вміє підключати програму кодом — оновіть Minerva або залиште код порожнім і увійдіть паролем.' };
    }
    setImmediate(() => connectTo(origin, code));   // спершу відповідь вікну підключення, потім закриваємо його
    return { ok: true };
  });
  ipcMain.handle('setup:cancel', (e) => {
    if (!fromSetup(e)) return;
    if (setupWin) setupWin.close();
  });
}

// ── Трей, меню, лічильник ────────────────────────────────────────────────────

function trayIcon() {
  const name = poll.loggedOut || poll.offline || !server() ? 'tray-off' : (poll.count ? 'tray-unread' : 'tray');
  return nativeImage.createFromPath(path.join(ASSETS, `${name}.ico`));
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip(TITLE);
  tray.on('click', showMain);
  refreshTray();
}

function statusText() {
  if (!server()) return 'Не підключено до Minerva';
  if (poll.loggedOut) return 'Потрібно увійти в Minerva';
  if (poll.offline) return `Немає зв'язку з Minerva`;
  return poll.count ? `Непрочитаних: ${poll.count}` : 'Нових листів немає';
}

function refreshTray() {
  if (!tray) return;
  tray.setImage(trayIcon());
  tray.setToolTip(`${TITLE} — ${statusText()}`);
  tray.setContextMenu(Menu.buildFromTemplate(trayTemplate()));
}

function autostart() {
  try { return app.getLoginItemSettings({ path: EXE, args: ['--hidden'] }).openAtLogin; } catch (e) { return false; }
}

function setAutostart(on) {
  app.setLoginItemSettings({ openAtLogin: on, path: EXE, args: ['--hidden'] });
}

function trayTemplate() {
  const host = server() ? new URL(server()).host : '';
  return [
    { label: 'Відкрити пошту', click: showMain, enabled: !!server() },
    { label: '✍️ Новий лист', click: newLetter, enabled: !!server() },
    { type: 'separator' },
    { label: statusText(), enabled: false },
    { type: 'separator' },
    { label: 'Сповіщення про нові листи', type: 'checkbox', checked: !!config.get('notify'),
      click: (i) => config.set('notify', i.checked) },
    { label: 'Запускати разом з Windows', type: 'checkbox', checked: autostart(), click: (i) => setAutostart(i.checked) },
    { label: '✕ ховає вікно у трей', type: 'checkbox', checked: !!config.get('closeToTray'),
      click: (i) => config.set('closeToTray', i.checked) },
    { label: '📧 Поштова програма за замовчуванням…', click: makeDefaultMailApp },
    { label: '📂 Відкрити теку «Завантаження»', click: () => shell.openPath(app.getPath('downloads')) },
    { type: 'separator' },
    { label: host ? `Сервер: ${host}` : 'Сервер не вибрано', enabled: false },
    { label: '🔗 Змінити сервер / підключити кодом…', click: openSetup },
    { label: '🚪 Вийти з акаунта', click: signOut, enabled: !!server() },
    { type: 'separator' },
    updateReady
      ? { label: `⟳ Перезапустити й оновити до ${updateReady}`, click: installUpdate }
      : { label: '⟳ Перевірити оновлення', click: () => checkUpdates(true) },
    { label: 'ℹ Про програму', click: about },
    { label: 'Завершити роботу', click: () => { quitting = true; app.quit(); } },
  ];
}

function windowMenu() {
  return Menu.buildFromTemplate([
    { label: 'Пошта', submenu: [
      { label: 'Новий лист', accelerator: 'CmdOrCtrl+N', click: newLetter },
      { label: 'Вхідні', accelerator: 'CmdOrCtrl+Shift+H', click: () => { showMain(); loadMail(); } },
      { type: 'separator' },
      { label: 'Змінити сервер / підключити кодом…', click: openSetup },
      { label: 'Вийти з акаунта', click: signOut },
      { type: 'separator' },
      { label: 'Закрити вікно', accelerator: 'CmdOrCtrl+W', role: 'close' },
      { label: 'Завершити роботу', accelerator: 'CmdOrCtrl+Q', click: () => { quitting = true; app.quit(); } },
    ] },
    { label: 'Вигляд', submenu: [
      { label: 'Оновити', accelerator: 'F5', click: (_i, w) => w && w.webContents.reload() },
      { label: 'Оновити', accelerator: 'CmdOrCtrl+R', visible: false, click: (_i, w) => w && w.webContents.reload() },
      { label: 'Назад', accelerator: 'Alt+Left', click: (_i, w) => w && w.webContents.navigationHistory.goBack() },
      { label: 'Вперед', accelerator: 'Alt+Right', click: (_i, w) => w && w.webContents.navigationHistory.goForward() },
      { type: 'separator' },
      { label: 'Збільшити', accelerator: 'CmdOrCtrl+=', click: () => zoom(0.5) },
      { label: 'Збільшити', accelerator: 'CmdOrCtrl+Plus', visible: false, click: () => zoom(0.5) },
      { label: 'Зменшити', accelerator: 'CmdOrCtrl+-', click: () => zoom(-0.5) },
      { label: 'Звичайний розмір', accelerator: 'CmdOrCtrl+0', click: () => zoom(0) },
      { type: 'separator' },
      { label: 'На весь екран', accelerator: 'F11', click: (_i, w) => w && w.setFullScreen(!w.isFullScreen()) },
      { label: 'Інструменти розробника', accelerator: 'CmdOrCtrl+Shift+I', click: (_i, w) => w && w.webContents.toggleDevTools() },
    ] },
    { label: 'Довідка', submenu: [
      { label: 'Гід: Пошта Minerva для Windows', click: () => server() && openExternal(server() + '/guide/mail/desktop-app/') },
      { label: 'Перевірити оновлення', click: () => checkUpdates(true) },
      { label: 'Про програму', click: about },
    ] },
  ]);
}

function newLetter() {
  if (!server()) { openSetup(); return; }
  openChild(server() + '/email/compose/');
}

function contextMenu(wc, p) {
  const items = [];
  if (p.misspelledWord) {
    (p.dictionarySuggestions || []).slice(0, 6).forEach((s) => items.push({ label: s, click: () => wc.replaceMisspelling(s) }));
    if (!items.length) items.push({ label: 'Немає варіантів', enabled: false });
    items.push({ label: 'Додати до словника', click: () => wc.session.addWordToSpellCheckerDictionary(p.misspelledWord) });
    items.push({ type: 'separator' });
  }
  if (p.linkURL && !/^javascript:/i.test(p.linkURL)) {
    items.push({ label: 'Відкрити посилання в браузері', click: () => openExternal(p.linkURL) });
    items.push({ label: 'Копіювати адресу посилання', click: () => clipboard.writeText(p.linkURL) });
    items.push({ type: 'separator' });
  }
  if (p.mediaType === 'image' && p.srcURL) {
    items.push({ label: 'Копіювати зображення', click: () => wc.copyImageAt(p.x, p.y) });
    items.push({ label: 'Зберегти зображення', click: () => wc.downloadURL(p.srcURL) });
    items.push({ type: 'separator' });
  }
  if (p.isEditable) {
    const f = p.editFlags || {};
    items.push({ label: 'Скасувати', role: 'undo', enabled: f.canUndo });
    items.push({ label: 'Повторити', role: 'redo', enabled: f.canRedo });
    items.push({ type: 'separator' });
    items.push({ label: 'Вирізати', role: 'cut', enabled: f.canCut });
    items.push({ label: 'Копіювати', role: 'copy', enabled: f.canCopy });
    items.push({ label: 'Вставити', role: 'paste', enabled: f.canPaste });
    items.push({ label: 'Вставити як текст', role: 'pasteAndMatchStyle', enabled: f.canPaste });
    items.push({ label: 'Виділити все', role: 'selectAll' });
  } else if (p.selectionText && p.selectionText.trim()) {
    items.push({ label: 'Копіювати', role: 'copy' });
  }
  while (items.length && items[items.length - 1].type === 'separator') items.pop();
  if (items.length) Menu.buildFromTemplate(items).popup({ window: BrowserWindow.fromWebContents(wc) || undefined });
}

function updateTitle() {
  if (!win || win.isDestroyed()) return;
  win.setTitle(poll.count ? `${TITLE} (${poll.count})` : TITLE);
}

function updateBadge() {
  refreshTray();
  updateTitle();
  if (!win || win.isDestroyed()) return;
  const n = poll.loggedOut ? 0 : poll.count;
  if (AUTO) console.log('[badge]', n, statusText());
  if (!n) { win.setOverlayIcon(null, ''); return; }
  const file = n > 9 ? '9plus' : String(n);
  win.setOverlayIcon(nativeImage.createFromPath(path.join(ASSETS, 'badge', `${file}.ico`)), `${n} непрочитаних`);
}

// ── Нові листи: опитування Minerva й сповіщення Windows ──────────────────────

function startPolling() { pollNow(2000); }

function resetPoll() {
  poll.latest = null; poll.count = 0; poll.loggedOut = false; poll.offline = ''; poll.toldLoggedOut = false;
  updateBadge();
}

function pollNow(delay = 0) {
  clearTimeout(poll.timer);
  poll.timer = setTimeout(checkMail, delay);
}

async function checkMail() {
  clearTimeout(poll.timer);
  const s = server();
  if (!s) return;
  try {
    const q = poll.latest != null ? `?new_after=${poll.latest}` : '';
    const r = await ses.fetch(`${s}/email/unread-count/${q}`, {
      credentials: 'include', signal: AbortSignal.timeout(20000),
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    });
    const type = r.headers.get('content-type') || '';
    if (/\/admin\/login/.test(r.url || '') || r.status === 401 || r.status === 403
        || (r.ok && !type.includes('json'))) {
      setLoggedOut();
    } else if (!r.ok) {
      setOffline(`HTTP ${r.status}`);
    } else {
      gotCounts(await r.json());
    }
  } catch (err) {
    setOffline(err && err.message ? err.message : String(err));
  } finally {
    const focused = win && win.isVisible() && win.isFocused();
    poll.timer = setTimeout(checkMail, focused ? POLL_FOCUSED : POLL_BACKGROUND);
  }
}

function gotCounts(d) {
  const wasOffline = poll.offline;
  poll.offline = '';
  poll.loggedOut = false;
  poll.toldLoggedOut = false;
  const count = Math.max(0, parseInt(d.count, 10) || 0);
  const latest = Math.max(0, parseInt(d.latest, 10) || 0);
  if (poll.latest != null && Array.isArray(d.new) && d.new.length) announce(d.new);
  poll.latest = Math.max(poll.latest || 0, latest);
  poll.count = count;
  updateBadge();
  if (wasOffline && win && win.webContents.getURL().startsWith('file:')) loadMail();   // зв'язок повернувся
}

function setLoggedOut() {
  poll.loggedOut = true;
  poll.offline = '';
  updateBadge();
  if (poll.toldLoggedOut) return;
  poll.toldLoggedOut = true;
  if (!(win && win.isVisible())) {
    notify(TITLE, 'Потрібно увійти в Minerva знову — натисніть, щоб відкрити вхід.', () => { showMain(); loadMail(); });
  }
}

function setOffline(reason) {
  poll.offline = reason || 'offline';
  updateBadge();
}

function announce(list) {
  if (!config.get('notify')) return;
  if (win && win.isVisible() && win.isFocused()) return;   // людина дивиться в пошту — сторінка сама покаже новий лист
  if (list.length > NOTIFY_EACH) {
    const body = list.slice(0, 4).map((m) => `${m.from || '—'}: ${m.subject || '(без теми)'}`).join('\n');
    notify(`📬 Нових листів: ${list.length >= 5 ? `${list.length}+` : list.length}`, body, () => { showMain(); loadMail(); });
    return;
  }
  list.slice().reverse().forEach((m) => {
    notify(m.from || 'Новий лист', m.subject || '(без теми)', () => { showMain(); loadMail(`/email/?msg=${parseInt(m.pk, 10)}`); });
  });
}

function notify(title, body, onClick) {
  if (AUTO) console.log('[notify]', title, '|', body);
  if (!Notification.isSupported()) return;
  const n = new Notification({ title, body, icon: ICON });
  notes.add(n);
  n.on('click', () => { notes.delete(n); if (onClick) onClick(); });
  n.on('close', () => notes.delete(n));
  n.show();
  setTimeout(() => notes.delete(n), 10 * 60 * 1000);
}

// ── Діалоги ──────────────────────────────────────────────────────────────────
// `npm start` з MINERVA_MAIL_AUTOCONFIRM=1 — діалоги відповідають кнопкою за замовчуванням і пишуть у журнал
// (перевірка без людини). У встановленій програмі не діє.
const AUTO = !!process.defaultApp && process.env.MINERVA_MAIL_AUTOCONFIRM === '1';

function askSync(owner, opts) {
  if (opts === undefined) { opts = owner; owner = undefined; }
  if (AUTO) { console.log('[dialog]', opts.message, '→', (opts.buttons || [])[opts.defaultId || 0]); return opts.defaultId || 0; }
  return owner ? dialog.showMessageBoxSync(owner, opts) : dialog.showMessageBoxSync(opts);
}

function tell(opts) {
  if (AUTO) { console.log('[dialog]', opts.message); return Promise.resolve({ response: 0 }); }
  return dialog.showMessageBox(opts);
}

// ── Завантаження ─────────────────────────────────────────────────────────────

function uniquePath(dir, name) {
  const safe = String(name || 'file').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0, 180) || 'file';
  const ext = path.extname(safe);
  const stem = safe.slice(0, safe.length - ext.length);
  let p = path.join(dir, safe);
  for (let i = 1; fs.existsSync(p) && i < 1000; i++) p = path.join(dir, `${stem} (${i})${ext}`);
  return p;
}

function onDownload(_e, item, wc) {
  const file = uniquePath(app.getPath('downloads'), item.getFilename());
  item.setSavePath(file);
  item.once('done', (_ev, state) => {
    if (state === 'completed') notify('⬇ Завантажено', path.basename(file), () => shell.openPath(file));
    else if (state === 'interrupted') notify('Не вдалося завантажити', item.getFilename());
  });
  // вкладення, відкрите «в новому вікні», виявилось файлом — порожнє вікно закриваємо
  const owner = wc && BrowserWindow.fromWebContents(wc);
  if (owner && owner !== win && owner !== setupWin) {
    const url = wc.getURL();
    if (!url || url === 'about:blank' || url === item.getURL()) setTimeout(() => { if (!owner.isDestroyed()) owner.destroy(); }, 300);
  }
}

// ── Вихід, сервер, поштова програма за замовчуванням, про програму ──────────

async function signOut() {
  const choice = askSync(win && win.isVisible() ? win : undefined, {
    type: 'question', title: TITLE, buttons: ['Вийти', 'Скасувати'], defaultId: 0, cancelId: 1,
    message: 'Вийти з акаунта Minerva на цьому комп’ютері?',
    detail: 'Програма забуде вхід. Щоб знову бачити пошту, увійдіть у Minerva у вікні програми або підключіть її кодом.',
  });
  if (choice !== 0) return;
  try {
    if (win && nav.sameOrigin(win.webContents.getURL(), server())) {
      await win.webContents.executeJavaScript(
        "fetch('/admin/logout/', {method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': "
        + "(document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/) || [])[1] || ''}}).then(r => r.status).catch(() => 0)",
        true);
    }
  } catch (e) { /* локально вхід усе одно забудеться */ }
  BrowserWindow.getAllWindows().forEach((w) => { if (w !== win && w !== setupWin) w.destroy(); });
  await ses.clearStorageData();
  await ses.clearCache();
  resetPoll();
  showMain();
  loadMail();
}

async function makeDefaultMailApp() {
  if (process.defaultApp) {
    tell({ type: 'info', title: TITLE, message: 'Доступно у встановленій програмі.' });
    return;
  }
  const ok = await winreg.registerMailClient(EXE);
  if (!ok) {
    tell({ type: 'error', title: TITLE, message: 'Не вдалося зареєструвати програму в Windows.' });
    return;
  }
  askSync({
    type: 'info', title: TITLE, buttons: ['Відкрити налаштування Windows'],
    message: 'Виберіть «Пошта Minerva» для MAILTO',
    detail: 'Windows не дозволяє програмам ставати типовими самим. Зараз відкриються «Програми за замовчуванням» — '
      + 'натисніть MAILTO (або «Електронна пошта») і виберіть «Пошта Minerva».',
  });
  shell.openExternal(winreg.settingsUrl()).catch(() => shell.openExternal('ms-settings:defaultapps'));
}

function about() {
  tell({
    type: 'info', title: TITLE, icon: nativeImage.createFromPath(ICON),
    message: `${TITLE} ${app.getVersion()}`,
    detail: `Сервер: ${server() || '—'}\nElectron ${process.versions.electron} · Chromium ${process.versions.chrome}\n\n`
      + 'Та сама пошта Minerva, що в браузері, — у власному вікні, зі значком у треї й сповіщеннями Windows.'
      + `${IS_PORTABLE ? '\n\nПортативна версія: оновлення — вручну зі сторінки випусків.' : ''}`,
  });
}

// ── Оновлення програми (electron-updater; випуск mail-desktop на GitHub) ─────

function setupUpdater() {
  if (!app.isPackaged || IS_PORTABLE) return;
  try {
    updater = require('electron-updater').autoUpdater;
  } catch (e) { updater = null; return; }
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = true;
  updater.on('update-downloaded', (info) => {
    updateReady = info && info.version ? info.version : 'нової версії';
    refreshTray();
    notify(TITLE, `Нову версію ${updateReady} завантажено — встановиться, коли ви завершите роботу програми. Натисніть, щоб перезапустити зараз.`,
      installUpdate);
  });
  updater.on('error', () => { /* немає інтернету / GitHub недоступний — спробуємо пізніше */ });
  setTimeout(() => checkUpdates(false), 20 * 1000);
  setInterval(() => checkUpdates(false), 6 * 60 * 60 * 1000);
}

async function checkUpdates(manual) {
  if (!updater) {
    if (manual) shell.openExternal(RELEASES_URL);
    return;
  }
  try {
    const r = await updater.checkForUpdates();
    if (!manual) return;
    const latest = r && r.updateInfo && r.updateInfo.version;
    if (latest && latest !== app.getVersion()) {
      tell({ type: 'info', title: TITLE, message: `Завантажую версію ${latest}…`,
        detail: 'Коли завантажиться, з’явиться сповіщення. Встановиться, коли ви завершите роботу програми.' });
    } else {
      tell({ type: 'info', title: TITLE, message: `У вас остання версія — ${app.getVersion()}.` });
    }
  } catch (e) {
    if (manual) tell({ type: 'warning', title: TITLE, message: 'Не вдалося перевірити оновлення.',
      detail: String(e && e.message ? e.message : e).slice(0, 300) });
  }
}

function installUpdate() {
  if (!updater) return;
  quitting = true;
  updater.quitAndInstall(false, true);
}

// `npm start` з MINERVA_MAIL_AUTOCONFIRM=1 і --inspect — функції для перевірки без людини (у встановленій програмі немає)
if (AUTO) {
  global.minervaMailTest = {
    signOut, checkMail, openSetup, newLetter, handleArg,
    state: () => ({ ...poll, timer: undefined, server: server(), windows: BrowserWindow.getAllWindows().length }),
    tray: () => trayTemplate().map((i) => i.label || '—'),
    cookies: () => ses.cookies.get({}).then((list) => list.map((c) => ({ name: c.name, days: c.expirationDate ? Math.round((c.expirationDate * 1000 - Date.now()) / 864e5) : null }))),
  };
}
