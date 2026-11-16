'use strict';
// «Поштова програма за замовчуванням» — реєстрація в HKCU (без прав адміністратора). Windows 10/11 не дає
// програмі самій стати типовою для mailto:, тож ми реєструємо можливість і відкриваємо «Програми за
// замовчуванням», де людина вибирає «Пошта Minerva». Інсталятор прибирає ці ключі (build/installer.nsh).
const { execFile } = require('child_process');

const APP_KEY = 'Minerva Mail';
const PROG_ID = 'MinervaMail.mailto';

function reg(args) {
  return new Promise((resolve) => {
    execFile('reg.exe', args, { windowsHide: true }, (err) => resolve(!err));
  });
}

function addValue(key, name, data, type = 'REG_SZ') {
  const args = ['add', key];
  if (name === null) args.push('/ve'); else args.push('/v', name);
  args.push('/t', type, '/d', data, '/f');
  return reg(args);
}

async function registerMailClient(exePath) {
  const exe = `"${exePath}"`;
  const base = `HKCU\\Software\\Clients\\Mail\\${APP_KEY}`;
  const caps = `${base}\\Capabilities`;
  const cls = `HKCU\\Software\\Classes\\${PROG_ID}`;
  const steps = [
    [base, null, 'Пошта Minerva'],
    [caps, 'ApplicationName', 'Пошта Minerva'],
    [caps, 'ApplicationDescription', 'Пошта Minerva — окрема програма з поштою Minerva'],
    [caps, 'ApplicationIcon', `${exePath},0`],
    [`${caps}\\URLAssociations`, 'mailto', PROG_ID],
    [`${base}\\shell\\open\\command`, null, exe],
    [cls, null, 'URL:MailTo Protocol'],
    [cls, 'URL Protocol', ''],
    [cls, 'FriendlyTypeName', 'Пошта Minerva'],
    [`${cls}\\DefaultIcon`, null, `${exePath},0`],
    [`${cls}\\shell\\open\\command`, null, `${exe} "%1"`],
    ['HKCU\\Software\\RegisteredApplications', APP_KEY, `Software\\Clients\\Mail\\${APP_KEY}\\Capabilities`],
  ];
  for (const [key, name, data] of steps) {
    if (!(await addValue(key, name, data))) return false;
  }
  return true;
}

function settingsUrl() {
  return 'ms-settings:defaultapps?registeredAppUser=' + encodeURIComponent(APP_KEY);
}

module.exports = { registerMailClient, settingsUrl, APP_KEY, PROG_ID };
