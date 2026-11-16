# «Minerva Пошта» — пошта Minerva на телефоні (Android, Flutter)

Додаток показує скриньку людини в Minerva (ту саму, що «📧 Email» у браузері) і працює через
`/email/phone/api/…` — `email_assistant/app_api.py`. Більшість адрес API викликає ту саму функцію веб-пошти
(`email_assistant/views.py`, `agent/views.py`, `offers/views.py`), тож логіка й права однакові.
Гід для людей — `docs/guide/mail/phone-app.md`.

## Підключення

Сторінка Minerva «📱 Пошта на телефоні» (`/email/phone/`) показує QR
`{"minerva": "mail", "v": 1, "url": "<адреса Minerva>", "code": "<одноразовий код>"}` (10 хв, один раз).
Додаток шле `POST /email/phone/api/pair/ {code, device, version}` → `{token: "mvm_…"}`; далі кожен запит —
`Authorization: Bearer mvm_…` і `X-Minerva-App: <версія>`. У базі Minerva — лише SHA-256 ключа
(`email_assistant.MailAppDevice`), «🚫 Відключити» на сторінці або «Відключити цей телефон» у додатку
(`POST api/unpair/`) — ключ перестає діяти. Ключ у телефоні — `flutter_secure_storage` (EncryptedSharedPreferences).
Лише HTTPS (`android/app/src/main/res/xml/network_security_config.xml`; http — тільки 10.0.2.2 / localhost).

## Що де

| Файл | Що |
|---|---|
| `lib/api.dart` | клієнт API (JSON, multipart з повторюваними `att_ref`, сирі відповіді — HTML листа, вкладення) |
| `lib/store.dart`, `lib/state.dart` | ключ і адреса, вибір людини (тема, мова, групування, свайп, підпис), кеш списків |
| `lib/l10n.dart` | мови uk / en / de (ключ → переклади, `context.t`, `context.tn` для чисел) |
| `lib/screens/home_screen.dart` | нижня навігація: Пошта / Важливі / Папки / Налаштування, «✏️ Написати» |
| `lib/screens/mail_list.dart` | список: свайпи, вибір кількох, пошук, сторінки, кеш першої сторінки |
| `lib/screens/thread_screen.dart` | гілка листа, вкладення, банери (замовлення, запит пропозиції, агент, ⏰), дії |
| `lib/screens/compose_screen.dart` | новий лист / відповідь / пересилання / чернетка, підказки адрес, ❗ 📬 ⏰, «пізніше» |
| `lib/screens/ai_sheet.dart` | AI-відповідь, «📋 Підготувати», «❓ Запитати», «🧭 Що обіцяли», переклад, історія |
| `lib/screens/agent_sheet.dart` | «🤖 Доручити агенту» |
| `lib/screens/html_screen.dart` | оригінал листа (WebView без JavaScript, посилання — у браузері) |

## Збірка

На цьому ПК: Flutter `C:\flutter` (3.32), Android SDK `%LOCALAPPDATA%\Android\Sdk`,
JDK `C:\Program Files\Android\Android Studio\jbr`.

```bash
cd mobile/mail_app
flutter pub get
flutter analyze
flutter test
flutter build apk --release
```

APK — `build/app/outputs/flutter-apk/app-release.apk`. Підпис release — той самий ключ, що «Minerva Візитки»:
`~/.minerva-android/keystore.properties` (або шлях у `MINERVA_KEYSTORE_PROPS`), **у репозиторій не потрапляє**.
Без нього release підписується debug-ключем — лише для перевірки.

## Нова версія

1. `pubspec.yaml` → `version: 1.0.1+2` (і `appVersion` у `lib/api.dart`).
2. `email_assistant/mail_app.py` → `APP_VERSION` (сторінка підключення й «Є нова версія» в додатку).
3. Зібрати й опублікувати реліз **не «latest»** — «latest» тримає «Minerva Візитки»
   (`releases/latest/download/minerva-cards.apk`):

```bash
cp build/app/outputs/flutter-apk/app-release.apk minerva-mail.apk
gh release create mail-app-v1.0.1 minerva-mail.apk --latest=false --title "Minerva Пошта 1.0.1"
```

Файл у релізі — саме `minerva-mail.apk` (посилання в Minerva — `releases/download/mail-app-v<версія>/minerva-mail.apk`).

## Пізніше

Push-сповіщення про нові листи (зараз — оновлення при відкритті й кожні 2 хв, поки додаток відкритий),
кілька скриньок однієї людини (зараз — основна, як у веб-пошті), відповіді на запрошення на зустріч.
