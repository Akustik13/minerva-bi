'use strict';
// Налаштування програми — settings.json у %APPDATA%\Пошта Minerva (паролів і cookie тут немає: сесія Minerva
// живе в профілі вікна, як у браузері).
const fs = require('fs');
const path = require('path');

const DEFAULTS = {
  server: '',            // https://minerva.example — лише адреса сервера
  notify: true,          // сповіщення Windows про нові листи
  closeToTray: true,     // ✕ ховає у трей, а не завершує роботу
  trayHintShown: false,
  bounds: null,          // {x, y, width, height, maximized}
  zoom: 0,
  trustedCerts: {},      // {host: SHA-256 відбиток} — сертифікат, якому людина довірилась сама
};

class Config {
  constructor(dir) {
    this.file = path.join(dir, 'settings.json');
    this.data = { ...DEFAULTS };
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      if (raw && typeof raw === 'object') this.data = { ...DEFAULTS, ...raw };
    } catch (e) { /* перший запуск або зіпсований файл — типові налаштування */ }
    if (!this.data.trustedCerts || typeof this.data.trustedCerts !== 'object') this.data.trustedCerts = {};
  }

  get(key) { return this.data[key]; }

  set(key, value) {
    this.data[key] = value;
    this.save();
  }

  save() {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const tmp = this.file + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(this.data, null, 2), 'utf8');
      fs.renameSync(tmp, this.file);
    } catch (e) { /* немає запису на диск — працюємо з тим, що в пам'яті */ }
  }
}

module.exports = { Config, DEFAULTS };
