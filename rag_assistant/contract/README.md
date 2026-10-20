# Закріплена копія схем приватного API RAG

`rag_private_2_0_draft_1/` — 9 JSON-схем `rag-private/2.0-draft.1` із погодженого wire-контракту
(integration/API_CONTRACT.md, review candidate 0.4).
- Пакет Кодекса: `integration/schemas/rag/`.
- Package sha256: `cd452d684db53721e29e5a8093458c52ca97b6f8b733fa93cd06f64e20575f43`.
- Маніфест хешів: `integration/reviews/rag-schemas-20261020.json`.

Навіщо: контрактні тести клієнта (`rag_assistant/tests_retrieval.py`).
- Кожен запит Minerva до `/v2/*` і кожна відповідь тестового двійника RAG перевіряються цими схемами.
- Хеші файлів закріплено в тесті. Щоб змінити схеми, потрібен новий пакет, нові хеші й новий ack у спільній папці.

Схеми не завантажуються з мережі. `$id` — лише ідентифікатори для локального `Registry`.
