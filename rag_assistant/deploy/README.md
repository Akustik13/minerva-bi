# Робочий приватний RAG: з'єднання Minerva ↔ RAG-ПК

Minerva (NAS у своїй мережі, публічна адреса `sevskiy.minerva-bi.com`) і RAG-ПК (`192.168.2.68`, інша мережа,
за `app.sevskiy.com`) з'єднуються лише через наявні HTTPS-адреси — без SSH, VPN і нових портів на роутері.

```
пошук:   Minerva ──► https://app.sevskiy.com/v2/… ──► DSM-проксі 192.168.2.142 ──► portproxy 192.168.2.68:8787
                     ──► розподільник 127.0.0.1:8786 ──► /v2 → приватний RAG 127.0.0.1:8789
                                                       └─► решта → публічний RAG 127.0.0.1:8787 (як і раніше)
листи:   приватний RAG ──► https://sevskiy.minerva-bi.com/api/v1/knowledge/…  (ключ knowledge:read+write)
```

- **Розподільник** (`rag_router.py`, на RAG-ПК — `D:\RAG-router\router.py`):
  - `/v2/…` віддає приватному RAG, усе інше — публічному RAG без змін; код публічного RAG не змінювався;
  - лише стандартна бібліотека Python.
- **`/v2` пропускається лише з публічної адреси Minerva:**
  - адресу береться з DNS `sevskiy.minerva-bi.com` і оновлюється кожні 5 хв;
  - звіряються обидва заголовки DSM — `X-Real-IP` і останній елемент `X-Forwarded-For`;
  - чужі запити отримують 403 у форматі помилок контракту ще до приватного RAG.

  Далі ще діють Bearer-ключ приватного RAG і перевірка кожного фрагмента в Minerva (`retrieval.verify_hits`).
- **Публічний RAG продукції** (помічник «Minerva») працює через ту саму адресу, що й раніше.
- **Відкат** — одна команда. Після неї публічний RAG знову працює напряму, а `/v2` зникає.

## Що стоїть на RAG-ПК (192.168.2.68)

| Що | Де |
|---|---|
| Приватний RAG `sevskiy-prod` | `D:\RAG-private-prod` (там `README.txt`), `127.0.0.1:8789`; задача `Minerva-Private-RAG` → `launcher.py` |
| Звідки бере листи | `source.base_url` = `https://sevskiy.minerva-bi.com` (`data\config.json`) |
| Індекс | Qdrant `minerva-rag-prod-qdrant` (`127.0.0.1:16334`, з API-ключем), колекція `crm_correspondence` |
| Розподільник | `D:\RAG-router` (там `README.txt`), `127.0.0.1:8786`; задача `Minerva-RAG-Router` (перезапуск після збою); журнал `logs\router.log` |
| Ключі приватного RAG | `D:\RAG-private-prod\data` — доступ лише `user` і SYSTEM |

## Підключення — по кроках

1. **RAG-ПК, перемикання** — PowerShell «від імені адміністратора»:
   ```powershell
   netsh interface portproxy set v4tov4 listenaddress=192.168.2.68 listenport=8787 connectaddress=127.0.0.1 connectport=8786
   ```
   Відкат:
   ```powershell
   netsh interface portproxy set v4tov4 listenaddress=192.168.2.68 listenport=8787 connectaddress=127.0.0.1 connectport=8787
   ```
   Одразу після перемикання задайте помічнику «Minerva» будь-яке питання — він має відповідати як раніше.
2. **Minerva → «🧠 База знань»** (меню «AI та Боти»). Блок «Підключення RAG»:
   - «Назва цього сервера для RAG»: `sevskiy-prod`;
   - «Адреса RAG-ПК»: `app.sevskiy.com`;
   - «Порт»: `443`;
   - «HTTPS» і «Перевіряти HTTPS-сертифікат»: увімкнено.

   У блоці «API-ключі», поле «API-ключ RAG (Minerva → RAG)» — вміст файлу `D:\RAG-private-prod\data\private-api-key.txt`.

   Збережіть. Індексацію поки не вмикайте.
3. **Ключ Minerva для RAG**:
   - «🔑 Створити ключ для RAG» → відкрийте створений ключ і скопіюйте його;
   - на RAG-ПК вставте його в `D:\RAG-private-prod\data\minerva-knowledge-key.txt` (Блокнот) і збережіть.

   Протягом ~30 с RAG запуститься: у `logs\launcher.log` з'явиться «RAG запущено».
4. **«🔌 Перевірити пошук RAG»** → має бути успіх.
5. **Пілот**:
   - у блоці «Що індексувати» увімкніть індексацію, виберіть одну скриньку й «з дати» за останні 30 днів;
   - стан видно на панелі «Стан».

   Масову індексацію архіву запускайте лише після перевірки пілоту.

## Якщо не працює

| Ознака | Що перевірити |
|---|---|
| «Перевірити пошук» — 403 «Адреса клієнта не дозволена» | `D:\RAG-router\logs\router.log`: з якою адресою прийшов запит. Якщо Minerva виходить в інтернет з іншої адреси — додайте її в `D:\RAG-router\router.json` `{"allow_ips": ["…"]}` і перезапустіть задачу |
| «Перевірити пошук» — 401 | у Minerva не той «API-ключ RAG» (має бути з `private-api-key.txt`) |
| «Перевірити пошук» — 503 / «недоступний» | приватний RAG не запущено: порожній `minerva-knowledge-key.txt` або див. `D:\RAG-private-prod\logs\` |
| Помічник «Minerva» перестав відповідати | задача `Minerva-RAG-Router` (журнал `router.log`), за потреби — команда відкату |
| На панелі «Стан» RAG нічого не забирає | ключ у `minerva-knowledge-key.txt`, `logs\runtime.log` |

## Перевірка коду розподільника

```sh
python rag_assistant/deploy/check_rag_router.py
```

Перевірка без RAG-ПК, на підставних серверах. Перевіряє:
- пересилання публічних запитів;
- відмови `/v2` і стійкість до підроблених заголовків;
- відповіді 503/502, коли RAG недоступний;
- що в журнал не потрапляють ключі.

Після зміни `rag_assistant/deploy/rag_router.py` скопіюйте його на RAG-ПК як `D:\RAG-router\router.py` і перезапустіть задачу.

## Обслуговування

- Новий ключ Minerva для RAG: вставте його в `minerva-knowledge-key.txt` — RAG перезапуститься сам. Старий ключ вимкніть у Minerva.
- Якщо RAG недоступний, Minerva працює далі без нього (запобіжник у `retrieval.py`).
- Адреса Minerva для RAG — `data\config.json` → `source.base_url` (лише HTTPS), після зміни перезапустіть RAG.
