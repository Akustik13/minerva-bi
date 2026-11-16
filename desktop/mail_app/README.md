# Пошта Minerva для Windows (`desktop/mail_app`)

Окрема програма лише з поштою Minerva. Це **тонка оболонка** (Electron): вікно Chromium показує справжню сторінку
`/email/` сервера Minerva, тож вигляд і функції збігаються з браузером один в один і оновлюються разом із Minerva.
Програма додає значок у треї з лічильником, сповіщення Windows про нові листи, `mailto:`, автозапуск.
Для людей — гід `docs/guide/mail/desktop-app.md` (у Minerva: `/guide/mail/desktop-app/`).

## Як це влаштовано

| Файл | Що робить |
|---|---|
| `src/main.js` | головний процес: вікна, трей, опитування `/email/unread-count/`, сповіщення, завантаження, оновлення |
| `src/nav.js` | що відкривається у вікні програми (пошта, вхід, `/media/`), а що — у браузері; `mailto:` → новий лист; розбір `minerva-mail://pair` |
| `src/setup.*`, `src/preload-setup.js` | вікно «Підключення до Minerva» (адреса, код) — лише воно має доступ до головного процесу |
| `src/offline.*` | «Немає зв'язку з Minerva» |
| `src/config.js` | `settings.json` у `%APPDATA%\Minerva Mail` (адреса сервера, вікно, масштаб, сповіщення) |
| `src/winreg.js` | реєстрація «поштової програми» в HKCU для `mailto:` |
| `build/installer.nsh` | інсталятор: `minerva-mail://`, прибирання ключів при видаленні |
| `tools/make_icons.py` | значки (`build/icon.ico`, `assets/*.ico`, цифри для панелі задач) |

Сервер (Minerva) — `email_assistant/desktop.py`, `views_desktop.py`:
- User-Agent програми закінчується на `MinervaMail/<версія>` → пошта без бокового меню адмінки, сесія діє 30 днів
  від останнього звернення;
- `/email/app/` — сторінка в браузері: завантажити й «🔗 Підключити програму» (одноразовий код, 10 хв) →
  `minerva-mail://pair?url=…&code=…` → у програмі `/email/app/pair/?code=` → «Підключити до акаунта …?» → вхід без пароля;
- `/email/app/hello/` — перевірка адреси; `/email/unread-count/?new_after=<pk>` — від кого й тема нових листів.

Паролів програма не зберігає: cookie сесії лежить у профілі вікна (`%APPDATA%\Minerva Mail\Partitions\minerva`).
Сторінки Minerva працюють з `contextIsolation` і `sandbox`, без доступу до Node.

## Зібрати

Потрібно: **Node.js ≥ 22.12** (Electron 45; на Node 20 `npm install` не завантажить Electron), Python з Pillow —
лише якщо міняєте значки.

```bash
cd desktop/mail_app
npm install
npm test          # правила навігації (src/nav.js)
npm start         # запустити з вихідного коду
npm run dist      # dist/Minerva-Mail-Setup.exe, dist/Minerva-Mail-Portable.exe, dist/latest.yml
```

Перевірка без людини (діалоги відповідають кнопкою за замовчуванням, зовнішні посилання й сповіщення — у журнал;
лише `npm start`, не встановлена програма):

```bash
MINERVA_MAIL_PROFILE=C:\temp\mm-profile MINERVA_MAIL_AUTOCONFIRM=1 npx electron --inspect=9339 . --remote-debugging-port=9333
```

`MINERVA_MAIL_PROFILE` — окремий профіль (налаштування й вхід), можна й для другого акаунта.

## Випуск

Версія — `version` у `package.json`. Випуск на GitHub — один постійний (`mail-desktop`, **не** «Latest», бо
«Latest» — Android-додаток візиток):

```bash
gh release create mail-desktop --latest=false --title "Пошта Minerva для Windows" --notes "..."   # лише перший раз
gh release upload mail-desktop dist/Minerva-Mail-Setup.exe dist/Minerva-Mail-Setup.exe.blockmap dist/latest.yml dist/Minerva-Mail-Portable.exe --clobber
```

Minerva посилається на `…/releases/download/mail-desktop/Minerva-Mail-Setup.exe`; встановлені програми раз на
6 годин читають `…/mail-desktop/latest.yml` (electron-updater) і оновлюються, коли людина завершує роботу.
Портативна версія сама не оновлюється.

## Підпис

Програма не підписана сертифікатом (Authenticode): Windows SmartScreen показує «Windows захистив ваш комп’ютер» →
«Докладніше» → «Однаково запустити». Щоб прибрати попередження — сертифікат підпису коду (OV/EV або Azure Trusted
Signing) і `win.signtoolOptions` / `CSC_LINK` + `CSC_KEY_PASSWORD` для electron-builder.
