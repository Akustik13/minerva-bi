# Промпт для Claude Code — Модуль `strategy/` в Minerva BI

> Вставляй цей промпт у Claude Code (або у новий чат з контекстом репо).
> Перед запуском — прочитай CLAUDE.md та `crm/models.py`.

---

## Контекст проекту

Ти працюєш над **Minerva BI** — Django 5.2 ERP/CRM системою для малого e-commerce бізнесу.

**Стек:** Django 5.2, Python 3.10+, PostgreSQL 16, Vanilla JS, Chart.js 4.4, Docker Compose, Gunicorn + WhiteNoise. Фронтенд — кастомний Django Admin з `templates/admin/base_site.html`. Жодного React, жодного окремого SPA.

**Існуючі додатки:** `crm/` (Customer + RFM), `sales/` (SalesOrder, SalesOrderLine), `inventory/`, `shipping/`, `dashboard/`, `bots/`, `faq/`, `labels_app/`, `backup/`, `api/` (DRF).

**Головний UI-файл:** `templates/admin/base_site.html` — там sidebar через JavaScript масив `GROUPS`. Будь-який новий додаток треба прописати туди.

**Архітектурні правила (з CLAUDE.md):**
- Django Admin як основний UI — не писати окремий фронтенд
- Signals для міжмодульної синхронізації — не дублювати логіку
- RFM логіка — тільки в `crm/utils.py`
- `affects_stock=True` — для попадання в dashboard статистику
- Placeholder app pattern: `managed=False` + `get_urls()` override — для кастомних сторінок
- При змінах моделей → `makemigrations` + `python manage.py check`

---

## Завдання: побудувати модуль `strategy/`

### Що це таке

Модуль стратегій роботи з клієнтами — **CRM Workflow Builder**.

Менеджер будує покрокову стратегію взаємодії з конкретним клієнтом: надіслати email → чекати відповідь → зателефонувати → залогувати реакцію → оновити сегмент.

Архітектура розрахована на два етапи:
- **Фаза 0 (зараз):** без canvas, без AI — Django Admin + кастомна HTML-сторінка
- **Фаза 1 (потім):** canvas-інтерфейс на тій самій сторінці (додається поверх)
- **Фаза 2 (пізніше):** AI-поради через `strategy/services/ai_advisor.py` (заглушка вже зараз)

---

## Що реалізувати зараз (Фаза 0 + Фаза 1)

### 1. Структура файлів

Створи додаток `strategy/` з такою структурою:

```
strategy/
├── __init__.py
├── apps.py
├── models.py
├── admin.py
├── urls.py
├── views.py
├── signals.py
├── templates/
│   └── strategy/
│       ├── canvas.html          ← головна сторінка з canvas
│       └── step_detail.html     ← деталі кроку (modal або aside)
├── static/
│   └── strategy/
│       └── canvas.js            ← логіка canvas (Vanilla JS, без бібліотек)
└── services/
    ├── __init__.py
    ├── engine.py                ← вибір і виконання наступного кроку
    └── ai_advisor.py            ← ЗАГЛУШКА: інтерфейс для майбутнього AI
```

### 2. Моделі (`strategy/models.py`)

#### `StrategyTemplate` — шаблон стратегії (blueprint)

```python
class StrategyTemplate(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    behavior_type = models.CharField(
        max_length=50,
        choices=[
            ('reactivation', 'Реактивація — повернути клієнта'),
            ('nurturing',    'Нарощування — розвинути потенціал'),
            ('retention',    'Утримання — зберегти VIP'),
            ('onboarding',   'Онбординг — новий клієнт'),
        ]
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Шаблон стратегії'
        verbose_name_plural = 'Шаблони стратегій'
```

#### `TemplateStep` — крок шаблону

```python
class TemplateStep(models.Model):
    STEP_TYPES = [
        ('email',    'Email'),
        ('call',     'Дзвінок'),
        ('pause',    'Пауза'),
        ('decision', 'Рішення (так/ні)'),
    ]
    template = models.ForeignKey(StrategyTemplate, on_delete=models.CASCADE,
                                  related_name='steps')
    step_type = models.CharField(max_length=20, choices=STEP_TYPES)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True,
        help_text='Скрипт дзвінка / текст email / умова рішення')
    delay_days = models.PositiveIntegerField(default=0,
        help_text='Днів після попереднього кроку')
    order = models.PositiveIntegerField(default=0)

    # Гілки для кроку 'decision'
    next_step_yes = models.ForeignKey('self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='from_yes')
    next_step_no = models.ForeignKey('self', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='from_no')

    # Canvas координати (для Фази 1)
    canvas_x = models.FloatField(default=0)
    canvas_y = models.FloatField(default=0)

    class Meta:
        ordering = ['order']
        verbose_name = 'Крок шаблону'
        verbose_name_plural = 'Кроки шаблону'
```

#### `CustomerStrategy` — активна стратегія для конкретного клієнта

```python
from crm.models import Customer

class CustomerStrategy(models.Model):
    STATUS = [
        ('active',   'Активна'),
        ('paused',   'Призупинена'),
        ('done',     'Завершена'),
        ('failed',   'Провалена'),
    ]
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,
                                  related_name='strategies')
    template = models.ForeignKey(StrategyTemplate, on_delete=models.SET_NULL,
                                  null=True, blank=True)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUS, default='active')
    current_step = models.ForeignKey('CustomerStep', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+')
    started_at = models.DateTimeField(auto_now_add=True)
    next_action_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Стратегія клієнта'
        verbose_name_plural = 'Стратегії клієнтів'

    def __str__(self):
        return f'{self.customer} — {self.name}'
```

#### `CustomerStep` — конкретний крок у стратегії клієнта

```python
class CustomerStep(models.Model):
    OUTCOMES = [
        ('pending',     'Очікує'),
        ('done_pos',    'Виконано — позитивно'),
        ('done_neg',    'Виконано — негативно'),
        ('skipped',     'Пропущено'),
        ('no_response', 'Без відповіді'),
    ]
    strategy = models.ForeignKey(CustomerStrategy, on_delete=models.CASCADE,
                                  related_name='steps')
    template_step = models.ForeignKey(TemplateStep, null=True, blank=True,
                                       on_delete=models.SET_NULL)
    step_type = models.CharField(max_length=20,
        choices=TemplateStep.STEP_TYPES)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    scheduled_at = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOMES,
                                default='pending')
    outcome_notes = models.TextField(blank=True,
        help_text='Що сказав клієнт, яка реакція')

    class Meta:
        ordering = ['scheduled_at', 'id']
        verbose_name = 'Крок стратегії'
        verbose_name_plural = 'Кроки стратегій'
```

#### `StepLog` — детальний лог кожної взаємодії

```python
class StepLog(models.Model):
    step = models.ForeignKey(CustomerStep, on_delete=models.CASCADE,
                              related_name='logs')
    logged_by = models.ForeignKey(
        'auth.User', null=True, on_delete=models.SET_NULL)
    outcome = models.CharField(max_length=20,
        choices=CustomerStep.OUTCOMES)
    notes = models.TextField(blank=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    # AI-поле (зараз порожнє, заповниться в Фазі 2)
    ai_suggestion = models.TextField(blank=True,
        help_text='Порада AI щодо наступного кроку')

    class Meta:
        ordering = ['-logged_at']
        verbose_name = 'Лог взаємодії'
        verbose_name_plural = 'Лог взаємодій'
```

---

### 3. Чотири вбудовані шаблони поведінки

У `strategy/apps.py` або в management command `strategy/management/commands/create_strategy_templates.py` — автоматично створити 4 стартові шаблони при першому запуску:

**Шаблон 1: Реактивація (reactivation)**
Для клієнтів із сегментом "At Risk" або "Lost". Кроки:
1. Email "Ми сумуємо за вами" → delay 0 днів
2. Рішення: відповів? → delay 5 днів
3. (yes) Лог: зацікавлений → оновити сегмент
4. (no) Дзвінок: уточнити причину → delay 7 днів
5. Рішення: зацікавлений після дзвінка?
6. (yes) Лог: повернули клієнта
7. (no) Пауза 90 днів

**Шаблон 2: Нарощування (nurturing)**
Для "Promising" — розвинути потенціал. Кроки:
1. Email: розповісти про новинки → delay 0
2. Пауза 14 днів
3. Email: персональна пропозиція → delay 14
4. Рішення: відповів або замовив?
5. (yes) Лог: конвертований → сегмент Loyal
6. (no) Пауза 60 днів

**Шаблон 3: Утримання VIP (retention)**
Для "Champion" — не втратити. Кроки:
1. Email: подяка + бонус → delay 0
2. Пауза 30 днів
3. Дзвінок: чи задоволені? → delay 30
4. Лог: зворотній зв'язок отримано
5. Пауза 60 днів → повторити цикл

**Шаблон 4: Онбординг (onboarding)**
Для нових клієнтів (перше замовлення). Кроки:
1. Email: вітання + інструкція → delay 0
2. Пауза 3 дні
3. Email: чи все добре? → delay 3
4. Рішення: є питання?
5. (yes) Дзвінок: допомогти
6. (no) Лог: онбординг завершено

---

### 4. Admin (`strategy/admin.py`)

```python
# StrategyTemplateAdmin — з inline TemplateStepInline
# CustomerStrategyAdmin — список з колонками: customer, status, template,
#                         next_action_at, current_step
#                         + кнопка "Відкрити canvas" → /strategy/<id>/canvas/
# CustomerStepInline — всередині CustomerStrategyAdmin
# StepLogAdmin — readonly, тільки перегляд
```

У `CustomerStrategyAdmin` додай кастомну кнопку у `change_form_template` або через `object_tools_template`, яка відкриває `/strategy/<pk>/canvas/`.

---

### 5. Canvas-сторінка (`strategy/views.py` + `strategy/templates/strategy/canvas.html`)

URL: `/strategy/<strategy_pk>/canvas/`

Сторінка розширює `admin/base_site.html` (той самий sidebar, той самий стиль).

**Фаза 0 — список кроків:**
Ліва частина: інформація про клієнта (ім'я, RFM, сегмент, статус стратегії).
Права частина: вертикальний список кроків з `CustomerStep` з можливістю:
- позначити крок як виконаний (форма з outcome + outcome_notes)
- переглянути лог кроку
- перейти до наступного кроку

**Фаза 1 — canvas (додається поверх Фази 0 через toggle):**
Кнопка "Показати canvas" / "Показати список" — переключає вигляд.

Canvas реалізується в `strategy/static/strategy/canvas.js` як SVG або HTML Canvas через Vanilla JS:
- Вузли (прямокутники): `CustomerStep` об'єкти, розташовані за `canvas_x/canvas_y`
- Стрілки між вузлами: лінії з arrowhead
- Клік на вузол → sidebar з деталями кроку
- Кольори вузлів за типом: email=синій, call=помаранчевий, pause=сірий, decision=жовтий
- Кольори за outcome: pending=нейтральний, done_pos=зелений, done_neg=червоний

Canvas дані завантажуються через JSON endpoint: `/strategy/<pk>/canvas/data/` (повертає кроки з координатами, зв'язками, outcome).

---

### 6. Signals (`strategy/signals.py`)

```python
# post_save на StepLog → якщо outcome == 'done_pos':
#   - оновити CustomerStrategy.current_step → наступний крок
#   - оновити CustomerStrategy.next_action_at
#   - якщо всі кроки done → статус 'done'

# post_save на CustomerStrategy → якщо статус змінився на 'done':
#   - оновити crm.Customer.segment через crm.utils.recalculate_rfm()
#   - (не дублювати RFM логіку — викликати crm/utils.py)
```

---

### 7. Services layer

**`strategy/services/engine.py`** — бізнес-логіка:
```python
def start_strategy(customer, template) -> CustomerStrategy:
    """Створити CustomerStrategy + CustomerStep з TemplateStep шаблону"""

def advance_step(customer_step, outcome, notes, user) -> CustomerStep | None:
    """Записати StepLog, визначити наступний крок, повернути його або None"""

def get_next_step(current_step, outcome) -> TemplateStep | None:
    """Визначити наступний TemplateStep з урахуванням гілок yes/no"""
```

**`strategy/services/ai_advisor.py`** — ЗАГЛУШКА для Фази 2:
```python
class AIAdvisor:
    """
    Заглушка. В Фазі 2 підключити Anthropic API.
    Інтерфейс зафіксований — реалізація зміниться.
    """
    def suggest_next_action(self, customer_strategy: CustomerStrategy) -> dict:
        """
        Повертає: {
            'suggested_step_type': 'email' | 'call' | 'pause',
            'suggested_text': str,
            'reasoning': str,
            'confidence': float  # 0.0–1.0
        }
        """
        # TODO Фаза 2: викликати Anthropic API
        # Передати: customer RFM, segment, step_logs history
        return {
            'suggested_step_type': None,
            'suggested_text': '',
            'reasoning': 'AI не підключений',
            'confidence': 0.0,
        }

    def analyze_response(self, step_log: StepLog) -> dict:
        """
        Аналіз реакції клієнта після логування.
        TODO Фаза 2: sentiment analysis + рекомендація.
        """
        return {'sentiment': 'neutral', 'recommendation': ''}
```

---

### 8. Реєстрація в Django та sidebar

**`tabele/settings.py`** — додати `'strategy'` до `INSTALLED_APPS` (після `crm`).

**`tabele/urls.py`** — додати:
```python
path('strategy/', include('strategy.urls')),
```

**`templates/admin/base_site.html`** — у масиві `GROUPS` додати в групу CRM:
```javascript
{
  id: 'crm',
  label: 'CRM',
  apps: ['crm', 'strategy'],
  links: [
    { label: 'Клієнти', url: '/admin/crm/customer/' },
    { label: 'Стратегії', url: '/admin/strategy/customerstrategy/' },
    { label: 'Шаблони', url: '/admin/strategy/strategytemplate/' },
  ]
}
```

**`tabele/admin.py`** — у `model_order` додати `strategy` після `crm`.

---

### 9. Міграції та перевірка

Після написання моделей:
```bash
python manage.py makemigrations strategy
python manage.py migrate
python manage.py check
python manage.py create_strategy_templates  # management command
```

---

## Чого НЕ робити зараз

- Не підключати Anthropic API — тільки заглушка
- Не робити drag-and-drop на canvas — тільки відображення вузлів і стрілок
- Не робити окремий React/Vue компонент
- Не торкатися `crm/utils.py` — тільки викликати існуючі функції
- Не змінювати `SalesOrder`, `Customer` моделі — тільки ForeignKey з `strategy/`
- Не використовувати Celery — всі дії синхронні через Django signals

---

## Порядок виконання

1. `strategy/models.py` — всі 5 моделей
2. `python manage.py makemigrations strategy && python manage.py migrate`
3. `strategy/admin.py` — реєстрація в Django Admin
4. Реєстрація в `INSTALLED_APPS`, `urls.py`, sidebar
5. `strategy/services/engine.py` — `start_strategy`, `advance_step`
6. `strategy/services/ai_advisor.py` — заглушка
7. `strategy/signals.py` — логіка переходу між кроками
8. Management command `create_strategy_templates` — 4 шаблони
9. `strategy/views.py` + `strategy/urls.py` — canvas endpoint
10. `strategy/templates/strategy/canvas.html` — Фаза 0 (список кроків)
11. `strategy/static/strategy/canvas.js` — Фаза 1 (SVG canvas)
12. `python manage.py check` — фінальна перевірка

---

## Критерії готовності

- [ ] `python manage.py check` — без помилок
- [ ] Адмін `/admin/strategy/` відкривається
- [ ] Можна створити стратегію для клієнта з CRM
- [ ] Можна позначити крок як виконаний і залогувати реакцію
- [ ] Логування кроку автоматично просуває стратегію до наступного кроку
- [ ] Canvas-сторінка `/strategy/<pk>/canvas/` відкривається з sidebar
- [ ] 4 шаблони з`create_strategy_templates` існують у БД
- [ ] `ai_advisor.py` — інтерфейс зафіксований, повертає заглушку
