# Робочий приватний RAG: з'єднання NAS ↔ RAG-ПК (без SSH)

База знань пошти (`/api/v1/knowledge/` + пошук `/v2`) працює через окремий **приватний RAG** на RAG-ПК.
Він не має нічого спільного з публічним RAG продукції (`app.sevskiy.com` → 8787) і чатом (`ai.sevskiy.com`).

```
Minerva (NAS 192.168.2.123) ──► 192.168.2.68:8789 (portproxy, брандмауер: лише NAS) ──► 127.0.0.1:8789  приватний RAG
приватний RAG ──► https://sevskiy.minerva-bi.com/api/v1/knowledge/…  (стрічка листів, ключ knowledge:read+write)
```

- **Minerva → RAG.** Схема та сама, що й у публічного RAG (8787):
  - Windows-пересилання `192.168.2.68:8789 → 127.0.0.1:8789`;
  - правило брандмауера, яке пускає лише NAS.

  Пошук іде локальною мережею і в інтернет не потрапляє. Кожен запит — з Bearer-ключем, а Minerva ще й сама перевіряє
  кожен фрагмент відповіді (`retrieval.verify_hits`).
- **RAG → Minerva** — через наявну публічну HTTPS-адресу Minerva. Приватний RAG дозволяє до Minerva лише HTTPS або
  `127.0.0.1`, тож іде саме HTTPS. Стрічка без ключа відповідає 401.
- Публічний RAG і чат не змінювались.

## Що вже стоїть на RAG-ПК (192.168.2.68)

| Що | Де |
|---|---|
| Приватний RAG `sevskiy-prod` | `D:\RAG-private-prod` (там же `README.txt`), слухає `127.0.0.1:8789` |
| Автозапуск | задача Windows `Minerva-Private-RAG` при вході користувача `user` → `launcher.py` (перезапуск після збою) |
| Звідки бере листи | `source.base_url` = `https://sevskiy.minerva-bi.com` (`data\config.json`) |
| Індекс | Qdrant `minerva-rag-prod-qdrant` (`127.0.0.1:16334`, з API-ключем), колекція `crm_correspondence` |
| Embeddings / reranker | Ollama `embeddinggemma`; reranker у контейнері `open-webui` (як у тестовій копії) |
| Ключі й стан | `D:\RAG-private-prod\data` — доступ лише `user` і SYSTEM |
| Журнали | `D:\RAG-private-prod\logs\launcher.log`, `runtime.log` |

Тестова копія (`D:\RAG-private-e2e`, 8788), публічний RAG (8787) і чат (8790) від неї не залежать.

## Підключення — по кроках

1. **RAG-ПК** — PowerShell «від імені адміністратора»:
   ```powershell
   netsh interface portproxy add v4tov4 listenaddress=192.168.2.68 listenport=8789 connectaddress=127.0.0.1 connectport=8789
   New-NetFirewallRule -DisplayName "Minerva private RAG from NAS only" -Direction Inbound -Protocol TCP -LocalAddress 192.168.2.68 -LocalPort 8789 -RemoteAddress 192.168.2.123 -Action Allow -Profile Any
   ```
   `192.168.2.123` — NAS, на якому працює Minerva. Якщо адреса інша, вкажіть її.
2. **Minerva → «🧠 База знань»**, блок «Підключення RAG»:
   - «Назва цього сервера для RAG»: `sevskiy-prod`;
   - «Адреса RAG-ПК»: `192.168.2.68`;
   - «Порт»: `8789`;
   - «HTTPS»: вимкнено.

   У блоці «API-ключі», поле «API-ключ RAG (Minerva → RAG)» — вміст файлу `D:\RAG-private-prod\data\private-api-key.txt`.

   Збережіть. «Індексувати» поки не вмикайте.
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

| Ознака | Причина |
|---|---|
| «Перевірити пошук» — тайм-аут | немає пересилання 8789 або брандмауер не пускає NAS (запит Minerva йде з адреси NAS) |
| «Перевірити пошук» — 401 | у Minerva не той «API-ключ RAG» (має бути з `private-api-key.txt`) |
| На панелі «Стан» RAG нічого не забирає | порожній або неправильний `minerva-knowledge-key.txt`; див. `logs\runtime.log` |
| Помилка про назву сервера | «Назва цього сервера для RAG» у Minerva ≠ `sevskiy-prod` |

Перевірити пересилання на RAG-ПК: `netsh interface portproxy show all`.

## Обслуговування

- Новий ключ Minerva для RAG: вставте його в `minerva-knowledge-key.txt` — RAG перезапуститься сам. Старий ключ вимкніть у Minerva.
- Якщо RAG недоступний, Minerva працює далі без нього (запобіжник у `retrieval.py`).
- Адреса Minerva для RAG змінюється в `data\config.json` → `source.base_url` (лише HTTPS), потім перезапуск RAG.
