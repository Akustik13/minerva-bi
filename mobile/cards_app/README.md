# 📱 Minerva Візитки (Android)

Додаток для фото візиток і флаєрів: камера з рамкою (CameraX), 📐 обрізка по краях з вирівнюванням (`DocDetect`, без
бібліотек), обидва боки, QR (ML Kit), скан Google (ML Kit Document Scanner — утримати 📐), пресети, примітки голосом,
черга без інтернету (WorkManager), перегляд візиток з Minerva.

Сервер — Minerva: `suppliers/app_api.py` (`/suppliers/app/api/…`), `suppliers/services/card_app.py`,
сторінка підключення «📱 Додаток на телефон» `/admin/inventory/supplier/cards/app/`. Гід —
`docs/guide/inventory/cards-app.md`.

## Як працює

- **Підключення**: Minerva показує QR `{"minerva": "cards", "v": 1, "url": "<адреса>", "code": "<одноразовий>"}` →
  `POST pair/` → ключ телефону `mvc_…` (у базі Minerva — лише SHA-256), далі `Authorization: Bearer mvc_…`.
  Лише https (http — тільки `10.0.2.2` / `localhost` для розробки, `res/xml/network_security_config.xml`).
- **Знімок** → `ImageUtils.cropToFrame` (ділянка рамки з урахуванням FILL_CENTER і EXIF, BitmapRegionDecoder, ≤ maxPx,
  JPEG 90) + QR зі знімка й видошукача → `CaptureStore` (`files/captures.json` + `files/captures/*.jpg`).
- **📐 По краях** (`Prefs.edges`, лише рамки візитки й A4) — `DocDetect.detect`: сіре ≤ ~400 px → кандидати (кілька
  порогів яскравості — Оцу на 2 і 3 класи, обидві полярності; контур перепадів — коли візитка двоколірна або біла
  на білому) → опукла оболонка → найбільший вписаний чотирикутник → сторони уточнюються прямими (PCA) → перевірка
  (кути, заповнення, перепад упоперек кожної сторони) → найкращий за площею. Наживо — площина Y кадру `ImageAnalysis`
  (1280×960, `cropRect` = видиме в PreviewView) раз на 150 мс у межах рамки + 12 % → зелений контур
  (`FrameOverlayView.quad`); знімок — ділянка `ImageUtils.viewRegion`, пошук ще раз на ній, `warp`
  (`Matrix.setPolyToPoly`), версія «по рамці» — поруч (`*-frame-*.jpg`), вибір — перемикач у `ReviewSheet`.
  Знайдене < `CaptureFragment.MIN_FRAC` ділянки (логотип / QR на візитці) не береться. Тести — `src/test/…/DocDetectTest.kt`
  (`gradlew testDebugUnitTest`, синтетичні знімки у перспективі).
- **Вивантаження**: `Sync` / `UploadWorker` — WorkManager з умовою «є мережа» (або Wi-Fi), повтори з наростанням,
  `uuid` знімка → Minerva не створює дубля після обриву; 401 → «підключіть знову».
- **Кеш**: `/meta/` (категорії, пресети, позначки, події) і перша сторінка «🪪 У Minerva» — у SharedPreferences, тож
  без інтернету все видно.

## Зібрати

Потрібні Android SDK (platform 35, build-tools 34) і JDK 17 (наприклад `jbr` з Android Studio).

```
cd mobile/cards_app
set JAVA_HOME=C:\Program Files\Android\Android Studio\jbr
gradlew assembleDebug        # app/build/outputs/apk/debug/app-debug.apk — усі ABI (і емулятор x86_64)
gradlew assembleRelease      # app/build/outputs/apk/release/app-release.apk — ARM, підписаний
```

`local.properties` (не в git): `sdk.dir=C\:/Users/<you>/AppData/Local/Android/Sdk`.

### Підпис release

Ключ — **поза репозиторієм**: `~/.minerva-android/keystore.properties` (або шлях у змінній `MINERVA_KEYSTORE_PROPS`):

```
storeFile=C:/Users/<you>/.minerva-android/minerva-cards.jks
storePassword=…
keyAlias=minerva-cards
keyPassword=…
```

⚠️ **Зберігайте копію `.jks` і паролів** — оновлення встановлюються поверх лише з тим самим ключем. Новий ключ = людям
треба видалити старий додаток (і фото, що ще не надіслані).

## Випустити нову версію

1. `app/build.gradle.kts`: `versionCode` + 1, `versionName`.
2. `gradlew assembleRelease`.
3. GitHub Release з тегом `cards-app-v<версія>`, файл — **`minerva-cards.apk`** (саме ця назва: сторінка Minerva веде на
   `releases/latest/download/minerva-cards.apk`):

```
copy app\build\outputs\apk\release\app-release.apk %TEMP%\minerva-cards.apk
gh release create cards-app-v1.0.1 %TEMP%\minerva-cards.apk -R Akustik13/minerva-bi --title "Minerva Візитки 1.0.1" --notes "…"
```

Releases репозиторію — лише для цього додатка (інакше `latest` вкаже не туди; тоді — `MINERVA_CARDS_APP_URL` у
налаштуваннях Minerva).
