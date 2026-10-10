"""Надписи кнопок главного меню (reply-клавиатура).

Хендлеры ловят нажатия через F.text == BTN_... . Все текстовые FSM-хендлеры
обязаны использовать фильтр bot.filters.TextInput, который пропускает эти надписи,
чтобы нажатие кнопки меню посреди диалога не было принято за ввод.
"""

# --- Меню начальника ---
BTN_NEW_TASK = "➕ Поставить задачу"
BTN_TEAM = "📊 Команда"
BTN_REVIEW = "📝 На проверке"
BTN_PROPOSALS = "📥 Предложения"
BTN_TASKS = "📋 Задачи"
BTN_STAFF = "👥 Сотрудники"
BTN_EXPORT = "📤 Экспорт"

# --- Меню сотрудника ---
BTN_MY_TASKS = "📋 Мои задачи"
BTN_PROPOSE = "➕ Добавить поручение"
BTN_SUBMIT = "✅ Сдать результат"
BTN_MY_KPI = "📈 Моя эффективность"

# --- Общие ---
BTN_HELP = "❓ Помощь"

MANAGER_MENU_LAYOUT: list[list[str]] = [
    [BTN_NEW_TASK, BTN_TEAM],
    [BTN_REVIEW, BTN_PROPOSALS],
    [BTN_TASKS, BTN_STAFF],
    [BTN_EXPORT, BTN_HELP],
]

EMPLOYEE_MENU_LAYOUT: list[list[str]] = [
    [BTN_MY_TASKS, BTN_PROPOSE],
    [BTN_SUBMIT, BTN_MY_KPI],
    [BTN_HELP],
]

MENU_BUTTONS: frozenset[str] = frozenset(
    text for layout in (MANAGER_MENU_LAYOUT, EMPLOYEE_MENU_LAYOUT) for row in layout for text in row
)
