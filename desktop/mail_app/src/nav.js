'use strict';
/*
 * Що відкривається у вікні програми, а що — у звичайному браузері. Без Electron — перевіряється тестами
 * (test/nav.test.js, `npm test`).
 *
 *  normalizeServer(text)   — «sevskiy.minerva-bi.com» → «https://sevskiy.minerva-bi.com» (лише адреса сервера, без шляху);
 *  classify(url, server)   — 'app' (пошта й вхід — у вікні програми), 'home' (корінь / адмінка — показати пошту),
 *                            'browser' (решта Minerva й інші сайти — звичайний браузер), 'mailto', 'pair', 'block';
 *  isPdfViewer(url)        — внутрішня адреса вбудованого переглядача PDF (chrome-extension://…/<id потоку>): кадр
 *                            з нею не блокувати, інакше 👁 вкладення-PDF — порожнє вікно;
 *  mailtoToCompose(mailto, server) — mailto:… → <server>/email/compose/?to=&cc=&subject=&body=;
 *  parsePair(url)          — minerva-mail://pair?url=…&code=… → {server, code} або null;
 *  findArg(argv)           — mailto:… / minerva-mail:… серед аргументів запуску.
 */

// Сторінки, що відкриваються у вікні програми (і в окремих вікнах програми — «↗ Окремо», вкладення, новий лист)
const IN_APP = [
  /^\/email(\/|$)/,                 // пошта: список, листи, вкладення, новий лист, розбір пошти, підключення програми
  /^\/admin\/login(\/|$)/,          // вхід
  /^\/admin\/logout(\/|$)/,
  /^\/admin\/password_reset(\/|$)/, // «Забули пароль?»
  /^\/admin\/reset\//,
  /^\/media\//,                     // файли (вкладення, документи замовлення) — перегляд у вікні програми
  /^\/rag\/knowledge\/\d+\/?$/,     // документ бази знань із листа
  /^\/i18n\//,                      // перемикач мови
];
// Корінь і головна адмінки → пошта (наприклад, після входу без ?next=)
const HOME = [/^\/$/, /^\/admin\/?$/];
// Вбудований переглядач PDF Chromium — постійний ID розширення (extension_misc::kPdfExtensionId). Документ він
// показує в кадрі з адресою chrome-extension://<ID>/<id потоку> (дані — з уже завантаженої відповіді Minerva)
const PDF_VIEWER = 'mhjfbmdgcfjbbpaeojofohoefgiehjai';

function normalizeServer(text) {
  let s = String(text || '').trim();
  if (!s) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = 'https://' + s;
  let u;
  try { u = new URL(s); } catch (e) { return null; }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  if (!u.hostname || u.username || u.password) return null;
  return u.origin;
}

function sameOrigin(a, b) {
  try { return new URL(a).origin === new URL(b).origin; } catch (e) { return false; }
}

function classify(url, server) {
  let u;
  try { u = new URL(url); } catch (e) { return 'block'; }
  if (u.protocol === 'mailto:') return 'mailto';
  if (u.protocol === 'minerva-mail:') return 'pair';
  if (u.protocol === 'about:' && (u.href === 'about:blank' || u.href === 'about:srcdoc')) return 'app';
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return 'block';
  if (!server || u.origin !== new URL(server).origin) return 'browser';
  if (HOME.some((re) => re.test(u.pathname))) return 'home';
  return IN_APP.some((re) => re.test(u.pathname)) ? 'app' : 'browser';
}

function isPdfViewer(url) {
  let u;
  try { u = new URL(url); } catch (e) { return false; }
  return u.protocol === 'chrome-extension:' && u.hostname === PDF_VIEWER;   // origin у Node для chrome-extension: — 'null'
}

function mailtoToCompose(mailto, server) {
  let u;
  try { u = new URL(mailto); } catch (e) { return server + '/email/compose/'; }
  if (u.protocol !== 'mailto:') return server + '/email/compose/';
  const out = new URLSearchParams();
  const take = (v) => String(v || '').trim();
  let to = '';
  try { to = decodeURIComponent(u.pathname || ''); } catch (e) { to = u.pathname || ''; }
  const params = u.searchParams;
  const getI = (name) => {           // параметри mailto: без огляду на регістр (Subject=, CC=)
    for (const [k, v] of params) if (k.toLowerCase() === name) return v;
    return '';
  };
  const extraTo = getI('to');
  to = [take(to), take(extraTo)].filter(Boolean).join(', ');
  if (to) out.set('to', to.slice(0, 2000));
  const cc = take(getI('cc'));
  if (cc) out.set('cc', cc.slice(0, 2000));
  const subject = take(getI('subject'));
  if (subject) out.set('subject', subject.slice(0, 500));
  const body = getI('body');
  if (body) out.set('body', String(body).slice(0, 5000));
  const q = out.toString();
  return server + '/email/compose/' + (q ? '?' + q : '');
}

function parsePair(url) {
  let u;
  try { u = new URL(url); } catch (e) { return null; }
  if (u.protocol !== 'minerva-mail:') return null;
  const what = (u.hostname || u.pathname.replace(/^\/+/, '')).replace(/\/+$/, '');
  if (what !== 'pair') return null;
  const server = normalizeServer(u.searchParams.get('url'));
  const code = String(u.searchParams.get('code') || '').trim();
  if (!server || !code || code.length > 100 || !/^[\w-]+$/.test(code)) return null;
  return { server, code };
}

function findArg(argv) {
  for (const a of argv || []) {
    const s = String(a || '');
    if (/^mailto:/i.test(s) || /^minerva-mail:/i.test(s)) return s;
  }
  return null;
}

function pairUrl(server, code) {
  return server + '/email/app/pair/?code=' + encodeURIComponent(code);
}

module.exports = {
  IN_APP, normalizeServer, sameOrigin, classify, isPdfViewer, mailtoToCompose, parsePair, findArg, pairUrl,
};
