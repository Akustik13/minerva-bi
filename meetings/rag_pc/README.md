# 🎙 Вікно запису нарад (RAG-ПК) — етап 1, версія 0.3.0

Вікно запису нарад на RAG-ПК (192.168.2.68). З 0.3.0 його відкриває Minerva (сторінка наради → «🎙 Відкрити вікно
запису»): RAG-ПК записує, розшифровує, шукає ключові моменти локальною моделлю й передає текст на сторінку наради.
Колишній тестовий стенд етапу 0 лишився, але вмикається лише галочкою «🧪 Тестові записи без Minerva».

## Етап 1: зв'язок з Minerva

- `bench/minerva.py` — клієнт Minerva (лише стандартна бібліотека): одна адреса `minerva_url` (https), лише шляхи
  `/api/v1/meetings/{redeem,sync,ping}/` і `/api/v1/meetings/<id>/{status,transcript,moments}/`, без проксі й
  переадресацій, сертифікат перевіряється, 20 с, відповідь до 1 МБ. Заголовки `Authorization: Token <ключ>`,
  `X-Meeting-Token: <push_token>` (для запитів однієї наради), `Idempotency-Key` (лише довідково: повтори Minerva
  розпізнає за `seq`, `upload_id` і ключами пунктів). Ключ — комірка `minerva_meetings` сховища RAG
  (`keystore.get`), не `minerva` (там ключ помічника на сайті). Помилки класифікуються за JSON-полем `code`
  (`classify`): `meeting_gone` → видалити запис; `meeting_unknown` → лишити, «Minerva не знає цієї наради»;
  `push_forbidden` / `session_mismatch` → зупинити передачу цього запису (вирішує sync); `invalid_request` /
  `bad_request` / `consent_missing` → викинути елемент + `error {stage: push}`; HTTP 413 / `payload_too_large` →
  частини вдвічі менші й нове `upload_id`; `unauthorized` / `forbidden_scope` / `shop_key_forbidden` → пауза всього
  (раз на `minerva_poll_seconds` — `GET ping`); `transcript_exists` → вважається доставленим; решта (5xx, HTML, без
  коду, тайм-аут, TLS, `transcript_incomplete`, 429) → повтор через 30 с·2ⁿ, не довше `minerva_retry_max_seconds`
  і не раніше Retry-After. Конструктори запитів читають лише JSON-файли запису — аудіо не передається ніколи.
- `bench/link.py` — прив'язки, стан запису, черга, sync, продовження після перезапуску, видалення:
  - `POST /api/minerva/redeem {ticket}` (сторінка → RAG-ПК → Minerva) → прив'язка у `data/minerva-bindings.json`
    (`minerva_binding_minutes` = 240 хв; новий redeem тієї ж наради прибирає старі невикористані), сторінці — назва,
    режим, тексти згоди `{de|en|uk: {ask, after}}`, мови, `return_url`, без push-токена;
    `GET /api/minerva/binding/<id>` — банер після F5 (id — у sessionStorage вкладки);
  - WS `start {minerva: <binding>, consent: {announced, all_agreed, lang, at}}` → `store.create` → синхронний
    статус `recording` (≤ 8 с, з `consent`, `capture {mode, processing, headset?}`, `lang_l`/`lang_r`) **до** запису:
    2xx — прив'язку використано; JSON 403/404/409/410 — «Це вікно запису застаріло», порожню теку видалено;
    мережа / 5xx — запис іде, статус чекає першим у черзі; лише Parakeet або без тексту (без Whisper наживо);
  - `<id>/minerva.json`: meeting, uid, режим, push-токен, hotwords (лише в `job.json` воркера), `seq`, черга
    (статуси, частини розшифровки — посилання на `transcript.json`, моменти), кроки (`recorded`, `transcript` +
    `upload_id`, `moments`, `done`), спроби (≤ 3 на крок — рахуються лише справжні помилки завдання, не перезапуски
    RAG-ПК; автоповтор через 2 і 10 хв; крок, що не закінчився за 10 запусків, — помилка без видалення аудіо),
    `failed {transcribe | moments | push}`, `audio_deleted_at`;
  - кожен статус — `{uid, session, seq, at, started_at, state, bench_version, …}`; стани recording, recorded
    (`duration`, `levels {L|R: {peak_dbfs}}`, `silent`, `marks [{t, note, auto, at}]` — `t` за записаним звуком,
    `at` — годинник), transcribing (`progress` 0–100, сусідні зливаються, відкладений лишається відкладеним),
    transcribed (`segments`), finding_moments, done (`segments`, `items`), error (`stage`, `message`), audio_deleted,
    discarded, purged; черга — строго FIFO для кожного запису; 2xx зараховується лише з `ok: true` або полями
    відповіді (`applied` / `complete` / `added`);
  - розшифровка — частинами ≤ 512 КБ (`json.dumps(ensure_ascii=False)`) і ≤ 1000 сегментів, `{uid, session,
    upload_id, part, parts, segments [{n, start, end, ch, text}], languages {L|R|M: "de"}, model, duration, marks?}`
    (`marks` лише коли є `marks.json`); моменти — `{uid, session, model, provider: ollama, lang, summary, items
    [{kind, text, who, due, quote, line = n, start, end, ch}]}`; Minerva відмовила в розшифровці / моментах (або
    20 разів «transcript_incomplete») → `error {stage: push}`, `failed.push`, файли лишаються для «Спробувати ще раз»;
  - «⛔ зупинити й видалити» записується в `minerva.json` до зупинки запису (перезапуск у цю мить нічого не
    розшифрує); запис без жодного звуку, вікно, закрите під час claim, чи запис, що не зміг початися, → `discarded`;
    коли вікно зникає, aiohttp скасовує обробник — зупинка й передача йдуть окремою задачею (`live_finish`);
  - sync кожні `minerva_poll_seconds` (120 с): записи `{meeting, uid, session, token_sha256, stage, progress,
    queue_pos, audio_on_bench}` + `bench {version, live_busy, queue, outbox, failed, last_error, retention_days,
    standalone}`; відповіді keep | delete | unknown | retry | drop_audio (delete ніколи не запам'ятовується);
  - «лише текст»: аудіо видаляється, щойно є `transcript.json` (і порожній, і після останньої невдалої спроби);
    «аудіо + текст» — до `drop_audio` або min(audio_days, retention_days, 30) днів; після доставки всього
    тексти видаляються (лишаються `meta.json` + `minerva.json`); будь-який файл — не довше 30 днів;
  - видалення — вміст спершу (з повторами), перевірка, `minerva.json` / `meta.json` останніми, інакше тека стає
    `.deleting-<id>` і довидаляється пізніше; статуси для вже видалених тек (`discarded`, `purged`) —
    `data/minerva-notices.json`, надсилаються лише після перевірки;
  - `GET /api/minerva/progress/<token>` — лише стан (токен з WS `ready`, у `minerva.json` — його sha256).
- Приватність: записи Minerva (і теки без читабельного `meta.json`) не видно в `/api/sessions`, замірах і жодній
  адресі `/api/sessions/{sid}…` (404); `/api/status` показує для їхнього завдання лише вид, стан і відсоток;
  ключові моменти — лише локальна модель Ollama, без хмарних тегів (`gpu.ollama_local`, `jobs.local_model`).
- `standalone_enabled` (за замовчуванням ні): без нього `/api/sessions`, `/api/upload`, `/api/metrics` → 403, а WS
  без прив'язки відмовляє; сторінка без посилання показує «🎙 Вікно запису наради…», `#check` — «✅ Цей ПК може
  відкривати вікно запису». ПК не з дозволених адрес отримує HTML-сторінку з кроками.
- Налаштування (`config.json`): `minerva_url`, `minerva_poll_seconds` 120, `minerva_retry_max_seconds` 3600,
  `minerva_binding_minutes` 240, `minerva_max_minutes` 240, `standalone_enabled` false. `/api/status` → `minerva
  {configured, url, url_error, key_saved, standalone, sessions, outbox, failed, blocked, paused, last_ok,
  last_error, last_sync}` — його показує вкладка «🎙 Наради».

## Тестовий режим (етап 0)

Що вміє:

- **🔴 Наживо** — захоплює в браузері звук наради (вкладку, вікно або весь екран) і мікрофон.
  Канал L — це «ви» (мікрофон), R — «співрозмовники». Звук записується в `audio.wav` на RAG-ПК, а текст
  з'являється одразу: Parakeet-TDT-0.6B-v3 на процесорі або Whisper turbo на відеокарті.
- **🌐 Переклад** тексту наживо робить браузер (Chrome 138+ / Edge 148+). Переклад лише на екрані,
  нічого не зберігається.
- **📁 Свій файл** — mp3, m4a, wav, ogg, webm, mp4 тощо до 1 ГБ. Розшифровку робить Whisper large-v3
  (точна) або large-v3-turbo (швидка), стерео можна розкласти на два канали.
- **📝 Точна розшифровка** запису — в окремому процесі. Якщо бракує відеопам'яті, стенд за дозволом
  вивантажує моделі Ollama; якщо й тоді не вистачає — рахує на процесорі.
- **🧠 Ключові моменти** — локальна LLM в Ollama (за замовчуванням `qwen3.5:9b-q4_K_M`) або, з 0.2.0, платна
  Anthropic Claude (Opus / Sonnet / Haiku 5.5) за ключем API. Шукає рішення, завдання, строки, ризики й обіцянки.
  Пункт лишається, лише якщо його цитата дослівно є в розшифровці.
- **📊 Заміри** — швидкість, пам'ять відеокарти, процесор, токени/с, ціна платної моделі; є вивантаження в CSV
  для Excel.
- **🎤 Перевірка мікрофона** (0.2.0) — вибір мікрофона й обробки, рівень наживо, підказки. Під час запису —
  попередження «мікрофон досі не чув вас». У кожному записі зберігаються рівні обох каналів і назва мікрофона;
  у списку позначка «🔇».
- **❓ Довідка** — `docs/help_uk.md`, той самий текст, що на вкладці «🎙 Наради» програми «RAG — керування».

## Платна модель (Anthropic Claude)

- `bench/cloud.py`: офіційний SDK `anthropic` (версію закріплено в requirements.txt), стрімінг, відповідь за
  JSON-схемою (`output_config.format`), effort для кожної моделі (Opus medium, Sonnet low, Haiku medium), без
  temperature. Opus і Sonnet надсилаються з бетою запасної моделі на випадок відмови. Якщо акаунт її не приймає,
  той самий запит іде без неї.
- Ключ — той самий, що в RAG: `D:\RAG\provider-secrets.dpapi` (DPAPI цього користувача Windows). Стенд лише
  читає його (`bench/keystore.py`), а зберігає ключ вкладка «🎙 Наради».
- Згода: сторінка запису вимагає галочку «Учасники погодились на обробку тексту в хмарі». Вона записується в
  `meta.json` (`cloud_consent`), і завдання перевіряє її ще раз перед надсиланням. Аудіо не передається ніколи.
- Ціна запуску (USD) рахується з токенів і записується в `moments_runs.json`, на сторінку й у CSV.

## Вкладка «🎙 Наради» в «RAG — керування»

`AI_models\rag_meeting_ui.py` + `rag_meeting_status.py` (розгорнуто в `D:\RAG`). На вкладці:
- стан стенду, «▶ Запустити / ⏹ Зупинити / 🔄 Перезапустити» (через start.ps1 / stop.ps1), блок Minerva зі
  `/api/status` (на зв'язку, черга, помилки, пауза);
- дозволені адреси, строк зберігання записів, модель за замовчуванням, «Адреса Minerva», «🧪 Тестові записи без
  Minerva» (з попередженням) — пише в `D:\Meeting\data\config.json`;
- «Ключ Minerva (наради)» (комірка `minerva_meetings`, `rag_secrets.save_secret`) і «🔍 Перевірити зв’язок з
  Minerva» = `GET <адреса>/api/v1/meetings/ping/` з вимогою `meetings:write` у `scopes`;
- ключ Anthropic, команда автозапуску, ця довідка.

Тести вкладки — `AI_models\test_rag_meeting_status.py`; щоб перевірити з кодом цієї гілки:
`set MEETING_BENCH_CONFIG=C:\tabele_mvp_meetings\meetings\rag_pc\bench\config.py`.

Стенд перечитує `config.json` при кожному запиті, тож зміни діють без перезапуску. Виняток — `host`, `port`,
`tls`, `live_threads`, `cert_names`.

## Як відкрити

1. Порт 8791 у брандмауері RAG-ПК відкриває людина — це системне налаштування. PowerShell від
   адміністратора на RAG-ПК:

   ```powershell
   New-NetFirewallRule -DisplayName 'Minerva Meeting Bench' -Direction Inbound -Protocol TCP -LocalPort 8791 -RemoteAddress 192.168.2.108,192.168.2.120 -Action Allow -Profile Any
   ```

   Коли список дозволених ПК змінюється — вкладка «🎙 Наради» в «RAG — керування» дає готову команду
   («📋 Копіювати команду брандмауера»): вона змінює це саме правило або створює його.

2. У Chrome чи Edge відкрийте **https://192.168.2.68:8791**. Сертифікат власний, тож браузер попередить один
   раз: «Додатково → Перейти». Відбиток SHA-256 записано в `D:\Meeting\data\logs\bench.log`.
3. Стенд відповідає лише адресам зі списку `allowed_clients` у `D:\Meeting\data\config.json`. Іншій
   адресі він сам підкаже, яку адресу дописати.

## Як тестувати

- **Свій голос / YouTube:** «Лише звук наради» → у вікні вибору вкладка YouTube і галочка «Поділитися звуком».
- **Нарада в Teams / Zoom / Meet у браузері:** вкладка наради + «Поділитися звуком вкладки».
- **Програма Teams / Zoom:** «Весь екран» + «Поділитися системним звуком». Найкраще — у гарнітурі, тоді
  мікрофон не чує співрозмовників із динаміків.
- Після «⏹ Зупинити» — «📝 Точна розшифровка», потім «🧠 Знайти» ключові моменти.
- ⚖️ Записувати лише з дозволу всіх, кого чути (§ 201 StGB). Записи лежать лише в `D:\Meeting\data\sessions`
  (доступ — лише користувач RAG-ПК і SYSTEM), самі видаляються через `retention_days` = 14 днів, а
  «🗑 Видалити запис» прибирає їх одразу.

## Обмеження

- Одночасно лише один запис наживо і одне завдання. Кожен запис і кожен пошук моментів займає відеокарту
  на кілька хвилин.
- Відеокарта спільна з Ollama: чатом сайту, помічником по продукції, підказками пошуку магазину й пошуком
  RAG. Поки йде розшифровка, вони відповідають повільніше.
- Parakeet не дозволяє задати мову: на змішаній нараді він іноді видає слова іншої мови або переклад.
  Whisper наживо дозволяє, але потребує ≈ 2,8 ГБ вільної відеопам'яті (займає ≈ 1,2 ГБ).
- Точна розшифровка large-v3 займає ≈ 5,6 ГБ відеопам'яті, turbo — ≈ 2,5 ГБ (заміри 09.11.2026).

## Встановлення й оновлення (RAG-ПК, без прав адміністратора)

Код — копія `meetings/rag_pc` у `D:\Meeting\app`.

```powershell
powershell -ExecutionPolicy Bypass -File D:\Meeting\app\setup.ps1     # venv, пакети, моделі, самоперевірка
powershell -ExecutionPolicy Bypass -File D:\Meeting\app\start.ps1     # запуск у фоні
powershell -ExecutionPolicy Bypass -File D:\Meeting\app\stop.ps1      # зупинка
D:\Meeting\venv\Scripts\python.exe -m bench.check --gpu-test          # з D:\Meeting\app: CUDA, моделі, пам'ять
```

Автозапуск після перезавантаження — завдання в Планувальнику, яке теж створює людина:

```powershell
$a = New-ScheduledTaskAction -Execute 'D:\Meeting\venv\Scripts\pythonw.exe' -Argument '-B -m bench --root D:\Meeting' -WorkingDirectory 'D:\Meeting\app'
Register-ScheduledTask -TaskName 'Minerva-Meeting-Bench' -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -Priority 4)
```

`-Priority 4` — звичайний пріоритет: без нього Планувальник запускає з пріоритетом 7 (нижче звичайного), і субтитри
наживо відстають, коли ПК зайнятий.

Що де лежить:

- `D:\Meeting\venv` — Python 3.12 із `D:\AI_training\Python312`.
- `D:\Meeting\models` — `whisper\` (кеш Hugging Face), `parakeet\`, `vad\`.
- `D:\Meeting\data` — `config.json` (лише те, що відрізняється від `DEFAULTS` у `bench/config.py`,
  зазвичай `allowed_clients`), `tls\` (сертифікат), `sessions\<id>\` (`meta.json`, `audio.wav` або
  `source.*`, `live.jsonl`, `transcript.json`, `moments.json`; у нарад Minerva ще `minerva.json`),
  `sessions\.deleting-<id>\` (недовидалене), `minerva-bindings.json`, `minerva-notices.json`, `logs\bench.log`.
  Тексти розшифровок, назви нарад і ключі в журнал не пишуться.

Пакети й причини версій — у `requirements.txt`. Головне:

- ctranslate2 4.8.5 має нативний код для GTX 1070 (sm_61) і CUDA 12.8.
- cuBLAS 12.8.4.1 — останній для Windows, що підходить. Для 12.9.3 збірки під Windows немає.
- Аудіо стенд декодує сам через PyAV: `decode_audio` з faster-whisper 1.2.1 несумісна з PyAV 19.

## Порівняння LLM (`tools/compare_llm.py`)

Синтетична нарада українською й німецькою з шістьма закладеними моментами; вимірюються швидкість, частка
моделі у відеопам'яті, знайдені моменти й дослівність цитат. Ще два тексти (українською й німецькою)
оцінює людина. Запуск на RAG-ПК:

```powershell
cd D:\Meeting\app; D:\AI_training\Python312\python.exe -B tools\compare_llm.py qwen2.5:7b qwen3.5:9b-q4_K_M > D:\Meeting\data\llm-compare.json
```

Результат 09.11.2026 (Ollama 0.40.15, GTX 1070, контекст 8K):

| Модель | Відеопам'ять | Завантаження | Генерація | Моменти з 6 | Українська |
|---|---|---|---|---|---|
| qwen2.5:7b | 4,9 ГБ | 9 с | 29–32 ток/с | 4, з них 2 з помилкою | помилки й русизми |
| qwen3.5:4b-q4_K_M | 3,2 ГБ | 17 с | 39 ток/с | 4 | вигадує факти |
| qwen3.5:9b-q4_K_M | 5,5 ГБ | 21 с | 26–27 ток/с | 4, без вигадок | найкраща |

> ℹ️ Qwen3.5 «думає» перед відповіддю. Стенд вимикає це сам (`think: false`); служби RAG сайту — теж, з 10.11.2026
> (`rag_providers.py`), і з того дня самі відповідають на qwen3.5:9b. Тож модель зазвичай уже завантажена, а коли
> стенд вивантажує її на час розшифровки, наступна відповідь сайту чекає ~20 с на повторне завантаження.

## Тести

```powershell
cd D:\Meeting\app; D:\Meeting\venv\Scripts\python.exe -B -m unittest discover -s tests -p "tests*.py"
```
