# 📚 GIT SETUP ІНСТРУКЦІЇ

## Крок 1: Ініціалізація Git

```powershell
cd C:\tabele_mvp

# Ініціалізувати репозиторій
git init

# Додати .gitignore
Copy-Item "$env:USERPROFILE\Downloads\.gitignore" .gitignore

# Додати файли
git add .

# Перший commit
git commit -m "Initial commit: Django Inventory & CRM System

- Inventory module (products, stock, transactions)
- Sales module (orders, import from Excel)
- CRM module (customers, RFM analysis)
- DigiKey bot integration
- Dashboard with analytics
- Dark theme admin interface"
```

## Крок 2: Створити репозиторій на GitHub

1. Відкрийте https://github.com/new
2. Назва: `django-inventory-crm`
3. Опис: `Django-based inventory, sales and CRM system with DigiKey integration`
4. Public/Private: на ваш розсуд
5. НЕ створюйте README, .gitignore (у вас вже є)
6. Create repository

## Крок 3: Підключити до GitHub

```powershell
# Додати remote
git remote add origin https://github.com/Akustik13/django-inventory-crm.git

# Перейменувати branch на main
git branch -M main

# Push
git push -u origin main
```

## Крок 4: Додати README та Docker файли

```powershell
# Копіюємо всі файли
Copy-Item "$env:USERPROFILE\Downloads\README.md" README.md
Copy-Item "$env:USERPROFILE\Downloads\Dockerfile" Dockerfile
Copy-Item "$env:USERPROFILE\Downloads\docker-compose.yml" docker-compose.yml
Copy-Item "$env:USERPROFILE\Downloads\requirements.txt" requirements.txt
Copy-Item "$env:USERPROFILE\Downloads\.dockerignore" .dockerignore

# Commit
git add .
git commit -m "Add Docker support and documentation"
git push
```

## Крок 5: Створити .env.example (БЕЗ секретів)

```powershell
# Створити шаблон .env
@"
SECRET_KEY=change-me-in-production
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1

DATABASE_URL=postgresql://user:password@db:5432/tabele_db

DIGIKEY_LOGIN=your@email.com
DIGIKEY_PASSWORD=yourpassword
"@ | Out-File -Encoding UTF8 .env.example

git add .env.example
git commit -m "Add .env.example template"
git push
```

## Крок 6: Додати GitHub Actions (опціонально)

Створіть `.github/workflows/django.yml` для CI/CD

## 📌 Корисні команди

```powershell
# Статус
git status

# Історія
git log --oneline

# Створити tag для версії
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0

# Новий branch для фічі
git checkout -b feature/new-feature
git push -u origin feature/new-feature
```

## 🔒 ВАЖЛИВО

**НІКОЛИ не комітьте:**
- `.env` з реальними паролями
- `db.sqlite3` або дампи БД
- `media/` з файлами користувачів
- API keys, токени, сертифікати

Все це вже в `.gitignore`!
