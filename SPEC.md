# SPEC — Telegram-бот управления задачами и оценки эффективности

Это технический контракт проекта. Исходное ТЗ — в README.md (раздел «Как это работает»).
Все модули пишутся строго по этому документу; при расхождении правится код, а не контракт
(или контракт меняется осознанно и согласованно).

## 0. Стек и принципы

* Python 3.12, **aiogram 3.31** (polling), **SQLAlchemy 2.1 async + aiosqlite** (SQLite, файл `data/bot.db`),
  **APScheduler 3.11** (AsyncIOScheduler), **google-genai** (бесплатный тариф Gemini), openpyxl, python-docx.
  Всё бесплатно. Платных сервисов нет.
* Интерфейс бота — **только на русском**. Parse mode — **HTML** (`DefaultBotProperties(parse_mode="HTML")`).
  Любой пользовательский текст в сообщениях экранируется `html.escape` (хелпер `bot.utils.text.esc`).
* Лимит Telegram: сообщение ≤ 4096 символов (длинное — обрезать/разбивать, хелпер `bot.utils.text.truncate`),
  callback_data ≤ 64 байт, в строковых полях CallbackData нельзя символ `:`. Числовые поля CallbackData —
  `DbInt` (диапазон INTEGER SQLite): подделанный id вроде 2^63 не совпадает ни с одним фильтром → «Кнопка устарела».
* Время: в БД — **naive UTC**; пользователю — местное время `settings.timezone` (по умолчанию Asia/Tashkent).
  Хелперы — `bot/utils/dates.py` (`utcnow`, `to_local`, `to_utc`, `deadline_from_local_date`, `fmt_*`).
* **AI только предлагает** оценку, окончательное решение — за руководителем. При недоступности AI
  (нет ключа, исчерпан бесплатный лимит, ошибка сети) всё работает на правилах — бот никогда не «ломается» из-за AI.
* Всё, что меняет задачу, пишется в журнал `TaskEvent` («все изменения фиксируются в системе»).

## 1. Структура проекта (владельцы файлов)

```
bot/
  __main__.py, main.py         # запуск, сборка Dispatcher, обработчик ошибок          [A5]
  config.py                    # Settings (готово)                                     [core]
  middlewares.py, filters.py   # (готово)                                              [core]
  notify.py                    # уведомления пользователям                              [A5]
  db/base.py, db/models.py     # (готово)                                              [core]
  services/errors.py           # DomainError (готово)                                  [core]
  services/users.py            # пользователи и роли                                   [A1]
  services/tasks.py            # жизненный цикл задачи                                  [A1]
  services/periods.py          # периоды неделя/месяц/квартал/год                       [A2]
  services/kpi.py              # расчёт коэффициента эффективности                      [A2]
  services/reminders.py        # какие напоминания пора отправить                      [A2]
  services/export.py           # выгрузка в Excel                                      [A2]
  ai/provider.py               # вызов Gemini + перебор моделей                         [A3]
  ai/formulate.py              # подсказка измеримого ожидаемого результата              [A3]
  ai/evaluate.py               # сравнение план↔факт, предварительная оценка             [A3]
  ai/evidence.py               # извлечение содержимого приложенных файлов               [A3]
  utils/dates.py               # (готово)                                              [core]
  utils/dateparse.py           # разбор сроков «5 октября», «завтра», «05.10 18:00»     [A4]
  utils/text.py                # esc, truncate, fmt_pct, plural, progress bar           [A4]
  ui/texts.py, ui/callbacks.py # (готово)                                              [core]
  ui/render.py                 # тексты карточек и отчётов                              [A4]
  ui/keyboards.py              # клавиатуры                                            [A4]
  handlers/common.py           # edit_or_answer, send_new, remove_markup, deny, dt_to/from_state (готово) [core]
  handlers/start.py            # /start, регистрация, меню, помощь, отмена              [B1]
  handlers/users_admin.py      # сотрудники: подтверждение, роли, блокировка             [B2]
  handlers/task_create.py      # руководитель ставит задачу                             [B3]
  handlers/task_propose.py     # сотрудник вносит поручение; подтверждение руководителем [B4]
  handlers/task_view.py        # списки задач, карточка, принять, правка, отмена, история [B5]
  handlers/task_submit.py      # сотрудник сдаёт фактический результат + AI-оценка       [B6]
  handlers/task_review.py      # руководитель подтверждает/меняет/возвращает             [B7]
  handlers/dashboard.py        # команда, карточка сотрудника, моя эффективность, экспорт [B8]
  scheduler/jobs.py            # напоминания, просрочки, еженедельная сводка             [A5]
  scheduler/backup.py          # ежедневная резервная копия базы руководителям в Telegram [A5]
  web.py                       # режим webhook: веб-сервер, фоновый цикл (§10.4, §10.6)
  fsm_storage.py               # DbStorage — диалоги (FSM) в таблице fsm_state (§10.1)
  tools/restore.py             # перенос/восстановление базы из копии SQLite (§10.7)
deploy/make_render_env.py      # deploy/render.env для Render из .env (§10.9)
tests/                         # pytest                                                [T]
```

Правило параллельной работы: **каждый агент правит только свои файлы**. Нужна функция из чужого
модуля, которой нет в контракте, — добавь её в свой модуль локально (приватным хелпером) и
упомяни в отчёте; не правь чужие файлы.

## 2. Модель данных (готово: `bot/db/models.py`)

`User(tg_id, username, full_name, position, role: Role[manager|employee], status: UserStatus[pending|active|blocked])`
— свойства `is_manager`, `is_active`, `short_name` («Иванов И. И.»).

`Task(title, description, expected_result, plan_value, plan_unit, deadline(UTC), priority, weight(1..100),
status, source, assignee_id, created_by_id, manager_id, created_at, updated_at, accepted_at, approved_at,
submitted_at, completed_at, ai_score, final_score, rework_count)`; связи `assignee`, `created_by`, `manager`,
`submissions` (lazy=selectin, по id); свойства `is_open`, `last_submission`.

`Submission(task_id, attempt, fact_text, result_text, fact_value, created_at, deadline_at_submit, is_late,
late_days, ai_score, ai_rationale, ai_model, ai_source["ai"|"rules"], final_score, decision, review_comment,
reviewer_id, reviewed_at)`; связи `task`, `reviewer`, `attachments`.

`Attachment(submission_id, kind[document|photo|video|other], file_id, file_unique_id, file_name, mime_type, file_size)`.

`TaskEvent(task_id, actor_id|None, type: EventType, data: dict JSON, created_at)` — журнал.

`ReminderLog(task_id, kind)` уникально по (task_id, kind).

`DigestLog(period_start UNIQUE, sent_at)` — отправленные еженедельные сводки (по одной на неделю, см. §8).
Таблица добавлена позже: `init_db` (create_all) создаёт её в существующей базе, данные не трогаются.

Все связи загружаются `selectin`, поэтому после `session.get(Task, id)` / `select(Task)` можно читать
`task.assignee.full_name`, `task.submissions[-1].attachments` без MissingGreenlet. `expire_on_commit=False`.

### Жизненный цикл задачи

```
                 (сотрудник вносит)            (руководитель ставит)
                    PROPOSED ──reject──► REJECTED      │
                       │approve                         │
                       ▼                                ▼
   ┌──────────────► ACTIVE ◄────────────────────────────┘
   │                   │ submit (сотрудник)
   │                   ▼
   │               SUBMITTED ──confirm/change──► DONE (final_score)
   │                   │ rework
   └──── REWORK ◄──────┘
   ACTIVE/REWORK/PROPOSED ──cancel (руководитель)──► CANCELLED
```

* REWORK ведёт себя как ACTIVE (можно сдавать снова, приходят напоминания).
* Повторная сдача создаёт новую `Submission` с `attempt = n + 1`.
* Просрочка — вычисляемый признак: `task.is_open and task.deadline < now`.

## 3. Сервисы (сигнатуры — контракт)

Общие правила: функции async, первым аргументом `session: AsyncSession`; **делают `flush`, но не `commit`**
(коммит — middleware или хендлер). Нарушение прав/недопустимый переход статуса → `raise DomainError("понятный текст")`.
Каждое изменение задачи пишет `TaskEvent` через `add_event`.

### 3.1 `bot/services/users.py` [A1]

```python
async def get_by_tg(session, tg_id: int) -> User | None
async def get_user(session, user_id: int) -> User | None
async def register_or_get(session, tg_id: int, username: str | None, tg_full_name: str) -> tuple[User, bool]
    # создаёт при первом /start; если tg_id в settings.admin_ids -> role=MANAGER, status=ACTIVE сразу
    # (и при повторном /start тоже повышает до активного руководителя). Возвращает (user, created).
async def complete_registration(session, user: User, full_name: str, position: str | None) -> User
    # сотрудник ввёл ФИО/должность; статус остаётся PENDING (ждёт руководителя)
async def approve_user(session, user_id: int, actor: User) -> User      # -> ACTIVE (роль не меняется)
async def reject_user(session, user_id: int, actor: User) -> User       # -> BLOCKED
async def block_user(session, user_id: int, actor: User) -> User        # -> BLOCKED; нельзя заблокировать себя
async def unblock_user(session, user_id: int, actor: User) -> User      # -> ACTIVE
async def set_role(session, user_id: int, role: Role, actor: User) -> User  # нельзя понизить себя / последнего руководителя
async def list_employees(session) -> list[User]   # ACTIVE + EMPLOYEE, сортировка по full_name
async def list_managers(session) -> list[User]    # ACTIVE + MANAGER
async def list_pending(session) -> list[User]     # PENDING, у кого заполнено full_name (регистрация завершена)
async def list_all(session) -> list[User]         # все, сортировка: статус, роль, ФИО
```
`actor` во всех мутациях должен быть активным руководителем, иначе DomainError.

### 3.2 `bot/services/tasks.py` [A1]

```python
@dataclass
class AttachmentIn:
    kind: AttachmentKind; file_id: str; file_unique_id: str | None = None
    file_name: str | None = None; mime_type: str | None = None; file_size: int | None = None

async def add_event(session, task: Task, actor: User | None, type: EventType, **data) -> TaskEvent
def is_overdue(task: Task, now: datetime | None = None) -> bool         # open и deadline < now
async def get_task(session, task_id: int) -> Task | None
async def get_submission(session, sub_id: int) -> Submission | None

async def create_task(session, *, creator: User, assignee_id: int, title: str, expected_result: str,
                      deadline: datetime, weight: int, priority: Priority = Priority.MEDIUM,
                      description: str | None = None, plan_value: float | None = None,
                      plan_unit: str | None = None) -> Task
    # creator — активный руководитель; assignee — активный сотрудник; weight 1..100; deadline UTC.
    # status=ACTIVE, source=MANAGER, manager_id=creator.id. Событие CREATED.
async def propose_task(session, *, employee: User, title: str, expected_result: str, deadline: datetime,
                       description: str | None = None, plan_value: float | None = None,
                       plan_unit: str | None = None) -> Task
    # status=PROPOSED, source=EMPLOYEE, assignee=employee, created_by=employee, weight=10 (временно). Событие PROPOSED.
async def approve_proposal(session, task_id: int, manager: User, *, weight: int,
                           priority: Priority = Priority.MEDIUM) -> Task
    # PROPOSED -> ACTIVE; manager_id, approved_at, accepted_at=now (сотрудник сам внёс). Событие APPROVED.
async def reject_proposal(session, task_id: int, manager: User, reason: str | None = None) -> Task
    # PROPOSED -> REJECTED. Событие REJECTED(reason).
async def update_task(session, task_id: int, actor: User, **fields) -> tuple[Task, dict[str, tuple]]
    # Разрешённые поля: title, expected_result, description, plan_value, plan_unit, deadline, priority, weight.
    # Только руководитель; только для PROPOSED/ACTIVE/REWORK. Возвращает (task, changes {field: (old, new)}).
    # Пустые changes -> событие не пишется. Смена deadline сбрасывает ReminderLog задачи. Событие EDITED.
async def accept_task(session, task_id: int, employee: User) -> Task
    # исполнитель подтверждает получение (accepted_at); повторно — без ошибки. Событие ACCEPTED.
async def cancel_task(session, task_id: int, manager: User, reason: str | None = None) -> Task
    # PROPOSED/ACTIVE/REWORK/SUBMITTED -> CANCELLED. Событие CANCELLED.
# Смена статуса (approve/reject_proposal, cancel_task, submit_result, review_*) — атомарный условный
# UPDATE … WHERE status=<ожидаемый> (две сессии не примут два решения): проигравший получает DomainError
# «Предложение уже обработано» / «Результат уже обработан» (константы PROPOSAL_/REVIEW_ALREADY_PROCESSED).
# Срок дальше 5 лет вперёд -> DomainError «Срок слишком далёкий — проверьте год …» (опечатка в годе).
async def submit_result(session, task_id: int, employee: User, *, fact_text: str,
                        result_text: str | None = None, fact_value: float | None = None,
                        attachments: list[AttachmentIn] = ()) -> Submission
    # Только исполнитель; только ACTIVE/REWORK. Создаёт Submission(attempt=n+1, deadline_at_submit=task.deadline,
    # is_late = now > deadline, late_days = max(0, дни просрочки, округл. до 0.1)), task -> SUBMITTED,
    # submitted_at=now; если accepted_at пуст — заполнить. Событие SUBMITTED.
async def record_evaluation(session, sub_id: int, *, score: float, rationale: str, source: str,
                            model: str | None = None) -> Submission
    # score ограничивается [0, settings.max_score] и округляется до целого.
    # sub.ai_* и task.ai_score. Событие AI_EVALUATED(actor=None).
async def review_confirm(session, sub_id: int, manager: User) -> Task
    # task SUBMITTED и sub — последняя сдача; final_score = sub.ai_score (если None -> DomainError «введите оценку»).
    # task -> DONE, completed_at; sub.decision=APPROVED, reviewer, reviewed_at, final_score. Событие SCORE_CONFIRMED.
async def review_set_score(session, sub_id: int, manager: User, score: float,
                           comment: str | None = None) -> Task
    # 0 <= score <= settings.max_score, иначе DomainError. decision=CHANGED. Событие SCORE_CHANGED(ai_score, score, comment).
async def review_rework(session, sub_id: int, manager: User, comment: str,
                        new_deadline: datetime | None = None) -> Task
    # task SUBMITTED -> REWORK, rework_count+1, при new_deadline — меняет срок и сбрасывает ReminderLog.
    # sub.decision=REWORK, review_comment, reviewer, reviewed_at. Событие REWORK(comment, new_deadline).

async def list_tasks(session, *, assignee_id: int | None = None, statuses: Sequence[TaskStatus] | None = None,
                     overdue_only: bool = False, limit: int | None = None, offset: int = 0) -> list[Task]
    # сортировка: по deadline (ближайшие сверху); для DONE — по completed_at desc.
async def count_tasks(session, *, assignee_id=None, statuses=None, overdue_only=False) -> int
async def list_proposals(session) -> list[Task]          # PROPOSED, старые сверху
async def list_for_review(session) -> list[Task]         # SUBMITTED, по submitted_at
async def task_events(session, task_id: int) -> list[TaskEvent]   # по created_at
async def weight_load(session, assignee_id: int, deadline: datetime,
                      exclude_task_id: int | None = None) -> int
    # сумма весов задач сотрудника (кроме PROPOSED/REJECTED/CANCELLED) с дедлайном в той же
    # календарной неделе (пн–вс, местное время), что и deadline. Нужна для подсказки «загрузка недели 80 %».
async def evaluated_history(session, assignee_id: int, *, limit: int = 10,
                            offset: int = 0) -> list[Task]   # DONE, по completed_at desc
```

### 3.3 `bot/services/periods.py` [A2]

```python
PERIOD_KINDS = ("week", "month", "quarter", "year")
@dataclass(frozen=True)
class Period:
    kind: str; offset: int; start: datetime; end: datetime   # naive UTC, полуинтервал [start, end)
    label: str      # «Неделя 29.09–05.10.2026», «Октябрь 2026», «IV квартал 2026», «2026 год»
    short: str      # «Неделя», «Месяц», «Квартал», «Год»
def get_period(kind: str, offset: int = 0, now: datetime | None = None) -> Period
    # границы считаются в местном времени (неделя с понедельника), затем переводятся в UTC.
    # now — naive UTC (по умолчанию utcnow()).
```

### 3.4 `bot/services/kpi.py` [A2] — методика коэффициента эффективности

**Задачи периода** — задачи сотрудника с `deadline ∈ [start, end)` и статусом не из
`PROPOSED / REJECTED / CANCELLED`.

**Входят в KPI:**
* `DONE` — с оценкой `final_score`;
* просроченные несданные (`ACTIVE/REWORK` и `deadline < now`) — как **0 %**, если `settings.overdue_counts_as_zero`.

**Не входят (пока):** `SUBMITTED` (на проверке) и не просроченные `ACTIVE/REWORK` (в работе).

**Формула:** `KPI = Σ(вес_i × оценка_i) / Σ(вес_i)` по входящим задачам. Веса не обязаны давать 100 %
— формула нормирует. Пример из ТЗ: 30/20/20/30 × 100/110/90/105 → 101.5 → «102 %».
Нет входящих задач → `kpi = None` («нет данных»). Месяц/квартал/год — та же формула по всем задачам периода
(не среднее недельных).

```python
@dataclass
class TaskSnapshot:          # то, что нужно расчёту, — без ORM, чтобы тестировать чистой функцией
    task_id: int; title: str; weight: int; status: TaskStatus; deadline: datetime
    final_score: float | None; source: TaskSource; last_late: bool | None   # is_late последней сдачи
    @classmethod
    def from_task(cls, task: Task) -> TaskSnapshot
@dataclass
class KpiItem: task_id: int; title: str; weight: int; score: float; zero_overdue: bool
@dataclass
class KpiResult:
    kpi: float | None          # взвешенный коэффициент, %
    items: list[KpiItem]       # что вошло в расчёт
    total: int                 # задач в периоде
    done: int                  # DONE
    done_on_time: int          # DONE, последняя сдача не просрочена
    done_late: int             # DONE, сдано с опозданием
    overdue_open: int          # просрочены и не сданы (в т.ч. REWORK)
    on_review: int             # SUBMITTED
    on_review_late: int        # SUBMITTED, сдано с опозданием
    in_progress: int           # ACTIVE/REWORK, срок не истёк
    overperformed: int         # DONE с оценкой > 100
    self_initiated: int        # source=EMPLOYEE (внесены сотрудником)
    avg_score: float | None    # простое среднее оценок DONE
    @property overdue_total -> int      # overdue_open + done_late + on_review_late («Просрочено»)
    @property on_time_pct -> float|None # done_on_time / done * 100 («Выполнение в срок»)
def compute_kpi(snapshots: Sequence[TaskSnapshot], now: datetime, overdue_as_zero: bool = True) -> KpiResult
async def kpi_for_user(session, user_id: int, period: Period, now: datetime | None = None) -> KpiResult
async def kpi_for_team(session, period: Period, now: datetime | None = None) -> list[tuple[User, KpiResult]]
    # все активные сотрудники (даже без задач), сортировка: kpi desc, None в конце, затем ФИО
def team_kpi(rows: list[tuple[User, KpiResult]]) -> float | None   # взвешенный по всем items команды
```

### 3.5 `bot/services/reminders.py` [A2]

```python
@dataclass
class Reminder:
    task: Task
    kind: str        # ключ для ReminderLog, уникален в рамках задачи
    recipient: str   # "employee" | "manager"
    reason: str      # "before_days" | "before_hours" | "deadline_passed" | "overdue_daily" | "overdue_manager" | "review_pending"
    days_left: float | None = None
async def due_reminders(session, now: datetime | None = None) -> list[Reminder]
async def mark_sent(session, task_id: int, kind: str) -> None   # идемпотентно (повтор не падает)
async def reset_reminders(session, task_id: int) -> None        # удалить ReminderLog задачи
def in_quiet_hours(now: datetime | None = None) -> bool          # по местному времени settings.quiet_hours_*
```
Правила (для открытых задач ACTIVE/REWORK, now — UTC):
* **до срока**: для каждого N из `settings.reminder_days_before` (по умолчанию 3, 1), если `0 < осталось ≤ N дней`
  — напоминание `kind=f"before_{N}d"`. Если подходят сразу несколько порогов (задачу поставили за 2 дня),
  отправить только **наименьший** N, а остальные подходящие пометить отправленными (вызов mark_sent делает
  планировщик для каждого из `Reminder`; поэтому due_reminders возвращает основной Reminder, а пропущенные
  пороги помечает сам через mark_sent).
* **в день срока**: `0 < осталось ≤ reminder_hours_before часов` → `kind="before_hours"`.
* **срок истёк**: `kind="deadline_passed"` (сотруднику: «Что фактически сделано? Какой получен результат?
  Какие документы подтверждают выполнение?» + кнопка «Сдать результат») и `kind="overdue_manager"` (руководителю).
* **ежедневно по просроченным**: после `settings.overdue_reminder_hour` местного времени — `kind=f"overdue_{YYYY-MM-DD}"`,
  но не в тот же день, когда ушло `deadline_passed`.
* **непроверенный результат**: SUBMITTED дольше `review_reminder_days` → руководителю `kind=f"review_{YYYY-MM-DD}"` раз в день.
* Уже записанные в ReminderLog kind не возвращаются. Задачи без `accepted_at` получают те же напоминания.

### 3.6 `bot/services/export.py` [A2]

```python
async def build_report_xlsx(session, period: Period, now: datetime | None = None) -> bytes
```
Листы: **«Сводка»** (Сотрудник, Должность, KPI %, Задач, Выполнено, В срок %, Просрочено, На проверке,
В работе, Перевыполнено, Внесено самостоятельно), **«Задачи»** (№, Сотрудник, Задача, Ожидаемый результат,
План, Факт, Срок, Сдано, Просрочка дн., Вес %, Приоритет, Статус, Оценка AI, Итоговая оценка, Решение,
Комментарий), **«Журнал»** (Дата, Задача, Кто, Событие, Детали) — события задач периода.
Шапка жирная, автоширина колонок, закреплённая первая строка, проценты числами.

## 4. AI (бесплатный Gemini) [A3]

### 4.1 `bot/ai/provider.py`
```python
class AIUnavailable(Exception): ...
def ai_available() -> bool                 # settings.ai_enabled
async def generate_json(*, system: str, parts: list, schema: dict, max_output_tokens: int = 8192) -> tuple[dict, str]
    # max_output_tokens меньше MIN_OUTPUT_TOKENS=8192 поднимается до него («думающим» моделям нужен запас);
    # automatic_function_calling отключён; пустой/обрезанный (MAX_TOKENS) ответ — следующая модель.
    # -> (данные, имя_модели). parts — список str и/или google.genai.types.Part.
    # Перебирает settings.gemini_models по порядку: при 429 (лимит бесплатного тарифа), 404 (модель недоступна),
    # 5xx, таймауте — следующая модель. Если все не смогли / ответ не JSON -> raise AIUnavailable.
    # Клиент genai.Client(api_key=..., http_options=types.HttpOptions(timeout=settings.ai_timeout_sec*1000)) — один на процесс.
    # Вызов: await client.aio.models.generate_content(model=m, contents=parts,
    #   config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
    #          response_json_schema=schema, temperature=0.2, max_output_tokens=...))
    # Ошибки: google.genai.errors.ClientError / ServerError / APIError (атрибут .code), asyncio.TimeoutError, httpx/aiohttp ошибки.
    # Глобальный asyncio.Semaphore(2) — не превышать бесплатные лимиты. Ключ API никогда не логировать.
```
JSON-схемы: только `type, properties, required, items, enum, description, minimum, maximum`; для «может быть null»
— `"type": ["number", "null"]`. Ответ всё равно валидировать в коде (типы, диапазоны).

### 4.2 `bot/ai/formulate.py`
```python
@dataclass
class ResultSuggestion:
    expected_result: str        # измеримая формулировка (что, сколько, в какой форме сдаётся)
    plan_value: float | None    # плановое число, если есть (100)
    plan_unit: str | None       # единица («договоров»)
    note: str | None            # короткий совет руководителю (≤ 200 симв.) или None
    source: str                 # "ai" | "rules"
async def suggest_expected_result(title: str, raw_result: str, deadline_text: str | None = None) -> ResultSuggestion
    # никогда не бросает: при AIUnavailable — правила (rules_suggestion).
def rules_suggestion(title: str, raw_result: str) -> ResultSuggestion
    # без AI: извлечь первое число и следующее слово как plan_value/plan_unit («проверить 100 договоров» -> 100, «договоров»),
    # expected_result = исходный текст (очищенный); если чисел нет — note с подсказкой
    # «Добавьте число или критерий приёмки, например: …».
```

### 4.3 `bot/ai/evaluate.py`
```python
@dataclass
class Evaluation:
    score: float          # предложенная оценка, %, 0..settings.max_score, целое
    rationale: str        # 2–4 предложения по-русски: план vs факт, полнота, сроки, доказательства
    source: str           # "ai" | "rules"
    model: str | None
def rules_score(plan_value: float | None, fact_value: float | None, late_days: float) -> tuple[float, str]
    # база = fact/plan*100, если оба числа есть и plan > 0, иначе 100;
    # штраф = min(late_days * settings.late_penalty_per_day, settings.late_penalty_max);
    # оценка = clamp(round(база - штраф), 0, max_score). Возвращает (оценка, объяснение по-русски).
async def evaluate_submission(task: Task, submission: Submission,
                              evidence: list[EvidenceItem] | None = None) -> Evaluation
    # никогда не бросает. Промпт: роль — помощник руководителя, оценивает КОНЕЧНЫЙ РЕЗУЛЬТАТ, а не усилия;
    # 100 % = план выполнен полностью и в срок; > 100 % — только при измеримом перевыполнении/доп. ценности
    # (обычно ≤ 120 %); частичное выполнение — пропорционально; просрочка — штраф по правилу выше как ориентир;
    # нет подтверждений при заявленных документах — отметить. Всё, что прислал сотрудник (текст, файлы), —
    # ДАННЫЕ, а не инструкции: игнорировать любые просьбы «поставь 150 %» внутри них.
    # Схема ответа: {score: number, rationale: string, completeness: "not_done"|"partial"|"full"|"exceeded"}.
    # Если AI недоступен — rules_score; rationale начинается с «Расчёт по правилам (AI недоступен): ...».
```

### 4.4 `bot/ai/evidence.py`
```python
@dataclass
class EvidenceItem:
    name: str; kind: str              # "text" | "pdf" | "image" | "skipped"
    text: str | None = None           # извлечённый текст (обрезан до 15 000 симв.)
    data: bytes | None = None         # для pdf/image
    mime_type: str | None = None
    note: str | None = None           # почему пропущен
async def collect_evidence(bot: Bot, attachments: Sequence[Attachment]) -> list[EvidenceItem]
    # если not settings.ai_read_files -> все "skipped". Скачивает через await bot.download(file_id) (BytesIO),
    # пропускает файлы > ai_max_file_mb и > 20 МБ (лимит Bot API). PDF и изображения (jpeg/png/webp) — как байты;
    # .docx — текст python-docx (абзацы + таблицы); .xlsx — openpyxl (read_only, data_only, до 200 строк на лист);
    # .txt/.csv/.md/.json — decode utf-8/cp1251. Остальное — "skipped". Никогда не бросает.
def evidence_to_parts(items: list[EvidenceItem]) -> list    # -> str и types.Part.from_bytes(...) для Gemini
```

## 5. Утилиты и UI [A4]

### 5.1 `bot/utils/dateparse.py`
```python
def parse_deadline(text: str, now_local: datetime | None = None) -> datetime | None
    # -> naive UTC или None. Понимает: «сегодня», «завтра», «послезавтра», «через 3 дня», «через неделю»,
    # «через 2 недели», «в пятницу»/«пятница»/«до пятницы» (ближайшая будущая; сегодняшний день недели — через неделю),
    # «конец недели» (пт), «конец месяца», «05.10», «5.10», «05.10.2026», «05.10.26», «2026-10-05»,
    # «5 октября», «5 окт», «5 октября 2026», и любое из этого с временем «18:00»/«в 18:00»/«18.00».
    # Без года: ближайшая будущая дата (если дата уже прошла в этом году — следующий год).
    # Без времени — settings.default_deadline_time. Результат в прошлом -> None. Мусор -> None.
    # Год дальше MAX_YEARS_AHEAD=5 лет -> None (опечатка); names_far_year(text) -> bool подсказывает это,
    # чтобы ответить FAR_YEAR_HINT («проверьте год»), а не «не понял срок».
def quick_deadline_options(now_local: datetime | None = None) -> list[tuple[str, str]]
    # [(подпись кнопки, ISO-дата YYYY-MM-DD)]: Сегодня, Завтра, Пятница (ближайшая будущая), Через неделю, Конец месяца
    # (без дубликатов дат; «Сегодня» — только если default_deadline_time ещё не прошло).
def iso_to_deadline(iso_date: str) -> datetime   # «2026-10-05» -> naive UTC с default_deadline_time
```

### 5.2 `bot/utils/text.py`
```python
def esc(text: object) -> str                       # html.escape(str(text)); None -> ""
def truncate(text: str, limit: int = 4000) -> str   # обрезка с «…»
def fmt_pct(value: float | None) -> str            # 101.5 -> «102 %» (округление половины вверх); None -> «—»
def fmt_num(value: float | None) -> str            # 100.0 -> «100», 2.5 -> «2,5», None -> «—»
def plural(n: int, one: str, few: str, many: str) -> str   # plural(5, "задача", "задачи", "задач") -> «5 задач»
def parse_number(text: str) -> float | None        # «110», «110,5», «1 200», «110 договоров» -> число
def parse_percent(text: str) -> float | None       # «95», «95%», «95 %» -> 95.0
def bar(pct: float | None, width: int = 10) -> str  # «▰▰▰▰▰▱▱▱▱▱» для 0..100+ (обрезка по ширине)
```

### 5.3 `bot/ui/render.py` — все функции возвращают HTML-строку (пользовательский текст экранирован)
```python
PRIORITY_LABELS = {HIGH: "🔴 Высокий", MEDIUM: "🟡 Средний", LOW: "🟢 Низкий"}
STATUS_LABELS = {PROPOSED: "📥 На подтверждении", ACTIVE: "🔄 В работе", SUBMITTED: "📝 На проверке",
                 REWORK: "↩️ На доработке", DONE: "✅ Выполнена", CANCELLED: "🚫 Отменена", REJECTED: "❌ Отклонена"}
def status_label(task: Task, now=None) -> str        # для просроченных открытых — «⏰ Просрочена»
def deadline_label(task: Task, now=None) -> str      # «5 октября (вс), 18:00 · осталось 2 дн.» / «· просрочено на 1 дн.»
def plan_text(task: Task) -> str                     # expected_result (+ «План: 100 договоров»)
def task_line(task: Task, now=None, with_assignee: bool = False) -> str   # одна строка для списков
def task_card(task: Task, now=None, *, show_assignee: bool = True) -> str
    # «📌 Задача #12: …», исполнитель, руководитель, ожидаемый результат, срок, приоритет, вес, статус,
    # источник (внесена сотрудником), последняя сдача (факт, оценка AI, итог), оценка.
def task_summary_draft(data: dict) -> str            # черновик из FSM-данных перед созданием (поля как в create_task)
def submission_text(task: Task, sub: Submission) -> str
    # для руководителя: План ↔ Факт, «Сдано: 04.10 18:20 — в срок / с опозданием 1,5 дн.», файлы (кол-во, имена),
    # «🤖 AI предлагает: 110 %» (или «📐 Расчёт по правилам: …»), обоснование.
def review_result_text(task: Task, sub: Submission) -> str   # для сотрудника: итоговая оценка, решение, комментарий
def events_text(task: Task, events: list[TaskEvent]) -> str  # история: «04.10 18:20 — Иванов И.: сдал результат (попытка 1)»
def kpi_block(title: str, res: KpiResult) -> str
    # «Неделя: 102 %», Выполнено задач, Просрочено, Выполнение в срок, Перевыполнено, Внесено самостоятельно,
    # В работе, На проверке; при kpi None — «нет оценённых задач».
def employee_card(user: User, week: KpiResult, month: KpiResult, period: Period | None = None,
                  current: KpiResult | None = None) -> str
    # «👤 Иванов Иван — 103 %» (по выбранному периоду), должность, неделя/месяц, показатели (как пример в ТЗ).
def team_dashboard(period: Period, rows: list[tuple[User, KpiResult]], team_value: float | None) -> str
    # заголовок с периодом, KPI команды, затем по строке на сотрудника:
    # «1. Иванов И. — 103 % ▰▰▰… | ✅ 14 · ⏰ 1 · 🔄 3 · 📝 1», внизу легенда.
def history_text(user: User, tasks: list[Task], page: int, total: int) -> str  # история оценок: AI → итог, решение
def user_line(user: User) -> str                      # для списков сотрудников (роль, статус)
def help_text(user: User | None) -> str               # справка по роли: цикл ПОРУЧЕНИЕ → … → KPI, команды
    # неактивному — по статусу: новичку «нажмите /start…», с заявкой — «Заявка на рассмотрении…», заблокированному — «Доступ закрыт…»
# utils/dates.fmt_deadline: срок дальше ~10 месяцев от сегодня показывается с годом («5 октября 2030 (сб), 18:00»).
```

### 5.4 `bot/ui/keyboards.py`
```python
def main_menu(user: User | None) -> ReplyKeyboardMarkup | ReplyKeyboardRemove
    # по роли: texts.MANAGER_MENU_LAYOUT / EMPLOYEE_MENU_LAYOUT; не активен -> ReplyKeyboardRemove()
def cancel_kb() -> InlineKeyboardMarkup                       # [✖️ Отмена] = PickCB(field="cancel")
def skip_cancel_kb(field: str = "skip") -> InlineKeyboardMarkup  # [⏭ Пропустить][✖️ Отмена]
def choose_user_kb(users: list[User], field: str = "assignee") -> InlineKeyboardMarkup  # PickCB(field, str(user.id)), по 2 в ряд
def deadline_kb() -> InlineKeyboardMarkup     # quick_deadline_options -> PickCB("deadline", iso); + Отмена
def priority_kb() -> InlineKeyboardMarkup     # PickCB("prio", "high"|"medium"|"low")
def weight_kb(load: int | None = None) -> InlineKeyboardMarkup  # 5,10,15,20,25,30,40,50 -> PickCB("weight","20"); + Отмена
def ai_suggestion_kb() -> InlineKeyboardMarkup
    # [✅ Принять] PickCB("ai","accept") [🔁 Другой вариант] ("ai","retry") / [✏️ Свой вариант] ("ai","manual")
    # [📝 Оставить как написал] ("ai","raw") / [✖️ Отмена]
def confirm_kb(yes_text: str = "✅ Создать", edit: bool = True) -> InlineKeyboardMarkup
    # PickCB("confirm","yes") [+ PickCB("confirm","edit") «✏️ Изменить»] + Отмена
def edit_fields_kb(fields: list[tuple[str, str]]) -> InlineKeyboardMarkup   # PickCB("field", key) по списку (key, подпись)
def score_kb(suggested: float | None = None) -> InlineKeyboardMarkup
    # быстрые оценки 50, 70, 80, 90, 100, 110, 120 -> PickCB("score","90") + Отмена
def files_kb(count: int) -> InlineKeyboardMarkup   # [✅ Готово (N)] PickCB("files","done") / [Без файлов] ("files","none") / Отмена
def task_actions_kb(task: Task, viewer: User) -> InlineKeyboardMarkup
    # руководитель: PROPOSED -> approve/reject/pedit (TaskCB); ACTIVE/REWORK -> «✏️ Изменить» edit, «🚫 Отменить» cancel;
    #               SUBMITTED -> «🔍 Проверить» review; всегда «📜 История» history.
    # исполнитель:  ACTIVE без accepted_at -> «✅ Принял в работу» accept; ACTIVE/REWORK -> «📤 Сдать результат» submit;
    #               «📜 История» history.
def new_task_kb(task: Task) -> InlineKeyboardMarkup      # сотруднику: [✅ Принял в работу] [📋 Открыть]
def submit_kb(task: Task) -> InlineKeyboardMarkup        # [📤 Сдать результат] [📋 Открыть] — для напоминаний
def proposal_kb(task: Task) -> InlineKeyboardMarkup      # руководителю: [✅ Подтвердить] [✏️ Изменить] [❌ Отклонить]
def review_kb(sub: Submission) -> InlineKeyboardMarkup
    # [✅ Подтвердить 110 %] SubCB("ok") (если ai_score есть) / [✏️ Изменить оценку] ("change") / [↩ На доработку] ("rework")
    # + [📎 Файлы (N)] ("files") при наличии вложений
def registration_kb(user: User) -> InlineKeyboardMarkup  # [✅ Подтвердить] UserCB("approve") [❌ Отклонить] ("reject")
def user_manage_kb(target: User, viewer: User) -> InlineKeyboardMarkup
    # по статусу/роли: approve/reject (PENDING), block / unblock, role_mgr / role_emp, «📊 Карточка» UserCB("card")
    # (карточка есть и у заблокированного сотрудника — его история оценок не пропадает)
def period_kb(scope: str, kind: str, offset: int, user_id: int = 0) -> InlineKeyboardMarkup
    # ряд: Неделя/Месяц/Квартал/Год (выбранный отмечен «•»), ряд: ◀ ▶ (▶ недоступна при offset=0 → не показывать)
def team_kb(rows: list[tuple[User, KpiResult]], kind: str, offset: int) -> InlineKeyboardMarkup
    # period_kb(scope="team") + кнопка на каждого сотрудника PeriodCB("emp", kind, offset, user_id) —
    # карточка за тот же период, что на дашборде (кнопка из сводки за прошлую неделю — прошлая неделя)
def employee_card_kb(user: User, kind: str, offset: int, back_to_team: bool) -> InlineKeyboardMarkup
    # period_kb(scope="emp"), [📜 История оценок] UserCB("history"), [📋 Задачи] ListCB("emp", "all", user_id=...),
    # [◀ К команде] PeriodCB("team", kind, offset) если back_to_team
def task_list_kb(tasks: list[Task], scope: str, status: str, page: int, total: int, user_id: int = 0,
                 page_size: int = 8, status_tabs: bool = True) -> InlineKeyboardMarkup
    # кнопка на каждую задачу TaskCB("open"), пагинация ListCB(scope, status, page±1, user_id),
    # вкладки статусов (open «В работе», overdue «Просрочены», review «На проверке», done «Выполнены», all «Все»)
def history_kb(user_id: int, page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup
def export_kb() -> InlineKeyboardMarkup     # PeriodCB("export", kind, 0/-1): «Эта неделя», «Прошлая неделя», «Этот месяц», «Прошлый месяц», «Квартал», «Год»
```

## 6. Уведомления `bot/notify.py` [A5]

```python
async def safe_send(bot, chat_id: int, text: str, **kwargs) -> Message | None
    # ловит TelegramForbiddenError (бот заблокирован), TelegramBadRequest, TelegramRetryAfter (ждать и 1 повтор), логирует.
async def send_attachments(bot, chat_id: int, sub: Submission) -> None
    # фото — send_photo/media group, документы — send_document по file_id, видео — send_video; caption с именем.
# Уведомления одному человеку возвращают bool «доставлено» (None — упали с ошибкой; исключений нет):
# руководителю пишется «исполнитель получил уведомление» только при True, иначе — «⚠️ Уведомление не
# доставлено … сообщите лично» (handlers.common.NOT_DELIVERED).
async def notify_new_task(bot, task: Task) -> bool            # исполнителю: карточка + new_task_kb
async def notify_proposal(bot, session, task: Task) -> int    # всем активным руководителям: карточка + proposal_kb; -> сколько получили
async def notify_proposal_decision(bot, task: Task, approved: bool, reason: str | None = None) -> bool
async def notify_task_changed(bot, task: Task, changes: dict[str, tuple]) -> bool   # исполнителю: что изменилось (план — с единицей)
async def notify_task_cancelled(bot, task: Task, reason: str | None = None) -> bool
async def notify_submission(bot, session, task: Task, sub: Submission) -> None
    # получатель: task.manager (если активный руководитель), иначе все активные руководители;
    # submission_text + review_kb, затем send_attachments.
async def notify_review_result(bot, task: Task, sub: Submission) -> bool   # исполнителю: review_result_text
async def notify_rework(bot, task: Task, sub: Submission) -> bool          # исполнителю: комментарий, новый срок, submit_kb
async def notify_registration(bot, session, user: User) -> None            # руководителям: заявка + registration_kb
async def notify_user_decision(bot, user: User, approved: bool) -> bool    # пользователю: доступ открыт (+ main_menu) / отклонён
```

## 7. Хендлеры (aiogram 3 Router; каждый модуль экспортирует `router`)

Общие правила:
* Каждый хендлер получает `session: AsyncSession`, `user: User | None`, `bot: Bot`, `state: FSMContext` (по необходимости).
* Права проверять фильтрами (`IsManager()`, `IsEmployee()`, `IsActiveUser()`) **и** в сервисах (защита от подделки callback).
* Все FSM-хендлеры текстового ввода — с фильтром `TextInput()`; хендлеры `PickCB` — с фильтром состояния своего модуля.
* Нажатие кнопки главного меню в любом состоянии: хендлер меню **первым делом** `await state.clear()`.
* `DomainError` из сервисов можно не ловить: глобальный обработчик ошибок (main.py) покажет текст пользователю
  (alert для callback, сообщение для текста). Ловить, если нужно продолжить диалог.
* Перед долгими операциями (AI) и перед уведомлениями других пользователей — `await session.commit()`.
* На каждый callback обязательно `await callback.answer()` (или с текстом).
* Состояния FSM (StatesGroup) — локально в своём модуле; имена групп уникальны в проекте.
* Сообщения-меню по возможности редактировать, а не плодить: `bot.handlers.common.edit_or_answer(event, text, kb)`
  (сам игнорирует «message is not modified», при невозможности редактирования шлёт новое). Reply-клавиатуру главного меню
  можно прикрепить только к новому сообщению — `common.send_new(event, text, main_menu(user))`.
* В FSM-данных хранить только JSON-совместимые значения (даты — `common.dt_to_state` / `dt_from_state`, enum — `.value`).
* `callback.message` может быть `InaccessibleMessage` (старое сообщение) — проверять `isinstance(callback.message, Message)`.

### Порядок подключения роутеров (main.py)
`start` → `users_admin` → `task_create` → `task_propose` → `task_submit` → `task_review` → `task_view` → `dashboard`.

### Таблица callback-данных
| Callback | Действие | Модуль |
|---|---|---|
| `PickCB(field="cancel")` | отмена любого диалога (любое состояние) | start |
| `TaskCB` open, accept, history, edit, cancel | карточка, принять, история, правка, отмена | task_view |
| `TaskCB` submit | начать сдачу результата | task_submit |
| `TaskCB` approve, reject, pedit | решение по предложению сотрудника | task_propose |
| `TaskCB` review | открыть проверку последней сдачи | task_review |
| `SubCB` ok, change, rework, files | проверка результата | task_review |
| `UserCB` approve, reject, block, unblock, role_mgr, role_emp, manage | управление сотрудниками | users_admin |
| `UserCB` card, history | карточка эффективности, история оценок | dashboard |
| `PeriodCB` team, emp, me, export | отчёты и экспорт | dashboard |
| `ListCB` my, all, emp | списки задач | task_view |
| `ListCB` review | очередь проверки | task_review |
| `ListCB` proposals | очередь предложений | task_propose |

### Кнопки главного меню (bot/ui/texts.py)
| Кнопка / команда | Роль | Модуль |
|---|---|---|
| /start, /menu, /help, «❓ Помощь», /cancel | все | start |
| «👥 Сотрудники» /staff | руководитель | users_admin |
| «➕ Поставить задачу» /new | руководитель | task_create |
| «➕ Добавить поручение» /propose | сотрудник | task_propose |
| «📥 Предложения» /proposals | руководитель | task_propose |
| «✅ Сдать результат» /submit | сотрудник | task_submit |
| «📝 На проверке» /review | руководитель | task_review |
| «📋 Задачи» /tasks | руководитель | task_view |
| «📋 Мои задачи» /my | сотрудник | task_view |
| «📊 Команда» /team | руководитель | dashboard |
| «📈 Моя эффективность» /kpi | сотрудник | dashboard |
| «📤 Экспорт» /export | руководитель | dashboard |

### 7.1 start.py [B1]
* `/start`: `register_or_get`. Активный → приветствие по роли + `main_menu`. Новый/без ФИО → FSM регистрации:
  ФИО («Иванов Иван Иванович», 2–200 симв., минимум 2 слова) → должность (или «⏭ Пропустить») →
  `complete_registration` → commit → `notify_registration` → «Заявка отправлена руководителю». PENDING с ФИО →
  «Заявка на рассмотрении». BLOCKED → «Доступ закрыт».
* Пользователь вне системы / неактивный пишет что угодно → подсказка «нажмите /start» (последний хендлер — в dashboard? нет:
  в start.py отдельный роутер-«ловушка» `fallback_router`, который main.py подключает **последним**).
  Для активного пользователя неизвестный текст вне FSM → «Не понял. Воспользуйтесь меню 👇» + main_menu.
* `/cancel` и `PickCB(field="cancel")` в любом состоянии: `state.clear()`, «Действие отменено», меню.
  У последнего вопроса прерванного диалога кнопки убираются; к «Действие отменено.» добавляется, что стало
  с отменённым («Задача не создана.», «Предложение по-прежнему ждёт решения — «📥 Предложения».» …).
* `/help`, «❓ Помощь» → `render.help_text(user)`.
* `/menu` → меню.

### 7.2 users_admin.py [B2] (руководитель)
* «👥 Сотрудники»: список всех (`render.user_line`), сначала заявки PENDING; кнопка на каждого → `UserCB("manage")` →
  карточка с `user_manage_kb`. Действия approve/reject/block/unblock/role_mgr/role_emp → сервис → commit →
  обновить карточку; approve/reject → `notify_user_decision`. Заявки, пришедшие уведомлением (`registration_kb`), —
  тот же хендлер; если заявка уже обработана другим руководителем — ответить alert «Уже обработано».

### 7.3 task_create.py [B3] (руководитель) — «Поставить задачу»
Сценарий (каждый шаг — с кнопкой Отмена):
1. Выбор сотрудника (`choose_user_kb(list_employees)`); нет сотрудников → «Сначала подтвердите сотрудников (👥 Сотрудники)».
2. «Задача» — короткое название (≤ 255).
3. «Ожидаемый результат» — своими словами. Затем `suggest_expected_result` (показать «⏳ Формулирую измеримый результат…»):
   показывается предложение AI (или правил) + `ai_suggestion_kb`: Принять / Другой вариант (повторный запрос) /
   Свой вариант (ввести текст) / Оставить как написал. Если plan_value неизвестен — после выбора текста спросить
   «Плановое число (если применимо), например 100 договоров» + Пропустить (`parse_number`; единица — слово после числа).
   Пока идёт запрос — `ai_busy` (+ `ai_busy_since`): кнопки и текст отвечают «⏳ Подождите, формулирую вариант…».
   Диалог хранится в БД и переживает перезапуск, поэтому флаг не должен «заморозить» черновик: при мягкой
   остановке бота посреди запроса (`CancelledError`) сохраняется вариант по правилам (`ai_lost`); флаг старше
   `(ai_timeout_sec + 5) × число моделей + 60 с` (жёсткая остановка) считается брошенным. В обоих случаях
   следующее сообщение руководителя показывает вариант по правилам с кнопками и пояснением «Вариант от AI не пришёл».
4. Срок — `deadline_kb()` или текстом (`parse_deadline`); непонятно/в прошлом → переспросить с примерами.
5. Приоритет — `priority_kb()`.
6. Вес — `weight_kb(load)`, где load = `weight_load(...)` на неделю срока; подсказка «Сейчас на неделе: 70 %. Рекомендуется,
   чтобы сумма весов за неделю была ≈100 %». Ввод текстом 1–100 тоже принимается.
7. Сводка `task_summary_draft` + `confirm_kb("✅ Создать")`. «✏️ Изменить» → `edit_fields_kb` (сотрудник, название,
   результат, план, срок, приоритет, вес) → ввод значения → снова сводка.
8. `create_task` → commit → `notify_new_task` → «✅ Задача #N поставлена» + карточка.

### 7.4 task_propose.py [B4]
Сотрудник «➕ Добавить поручение» (устное поручение): название → ожидаемый результат (AI-помощь как в 7.3, тот же
`ai_suggestion_kb`) → план (если нужно) → срок → сводка → `propose_task` → commit → `notify_proposal` → «Отправлено руководителю».
Руководитель (уведомление с `proposal_kb` или «📥 Предложения» → список `ListCB("proposals")`):
* ✅ Подтвердить → выбор веса (`weight_kb(load)`) → приоритет → `approve_proposal` → commit → `notify_proposal_decision(approved=True)`.
* ✏️ Изменить (`pedit`) → `edit_fields_kb` (название, результат, план, срок) → ввод → `update_task` → показать карточку снова
  с `proposal_kb` (изменения видны сотруднику после подтверждения: `notify_task_changed`).
* ❌ Отклонить → причина (или Пропустить) → `reject_proposal` → commit → `notify_proposal_decision(approved=False, reason)`.
* Предложение уже обработано → alert.

### 7.5 task_view.py [B5]
* «📋 Мои задачи» (сотрудник): `ListCB("my", "open")` — вкладки: В работе / Просрочены / На проверке / Выполнены / Все.
* «📋 Задачи» (руководитель): `ListCB("all", "open")`, те же вкладки, в строке — исполнитель. `ListCB("emp", ..., user_id)` —
  задачи одного сотрудника (из карточки сотрудника).
* `TaskCB("open")` → `task_card` + `task_actions_kb(task, user)`. Права: руководитель — любые; сотрудник — только свои.
* `TaskCB("accept")` → `accept_task` → обновить карточку.
* `TaskCB("history")` → `events_text(task, task_events(...))`.
* `TaskCB("edit")` (руководитель, ACTIVE/REWORK) → `edit_fields_kb` (название, результат, план, срок, приоритет, вес) → ввод
  (срок — `deadline_kb`/текст; приоритет — `priority_kb`; вес — `weight_kb`) → `update_task` → commit →
  `notify_task_changed` → карточка.
* `TaskCB("cancel")` → подтверждение + причина (или Пропустить) → `cancel_task` → commit → `notify_task_cancelled`.

### 7.6 task_submit.py [B6] (сотрудник)
* «✅ Сдать результат» → список открытых задач сотрудника (ACTIVE/REWORK) кнопками `TaskCB("submit")`; нет задач — сообщить.
* `TaskCB("submit")` (из списка, карточки, напоминания): проверить, что задача своя и открыта. Показать план
  (`plan_text`), при REWORK — последний комментарий руководителя. Затем вопросы:
  1. «Что фактически сделано?» (текст, обязательно).
  2. «Какой получен результат?» (текст или Пропустить).
  3. Если у задачи есть plan_value: «Фактическое значение? План: 100 договоров» (число или Пропустить; `parse_number`).
     Если plan_value нет — попытаться взять число из ответа 2 не нужно.
  4. «Какие документы или материалы подтверждают выполнение? Пришлите файлы/фото» — принимать document/photo/video
     (несколько, в т.ч. альбомом; на каждый — «📎 Добавлено: N»; `files_kb(N)`), «✅ Готово» / «Без файлов».
  5. Сводка + `confirm_kb("📤 Отправить", edit=False)`.
* Отправка: `submit_result` → **commit** → сообщение «⏳ Анализирую результат…» → `collect_evidence` → `evaluate_submission` →
  `record_evaluation` → commit → `notify_submission` → сотруднику: «✅ Результат отправлен руководителю на проверку»
  (оценку AI сотруднику не показывать до решения руководителя).
* Если AI упал/долго — всё равно `rules_score` (evaluate_submission сам не бросает). Бюджет всей оценки —
  `ai_evaluate.evaluation_budget_sec()` = `ai_timeout_sec × 2 + 30` с.
* Бота остановили посреди оценки (деплой, сбой) — сдача сохранена без оценки, руководитель не уведомлён:
  её находит `jobs.recover_stalled_evaluations` (§8) и доводит до конца.

### 7.7 task_review.py [B7] (руководитель)
* «📝 На проверке» → `ListCB("review")`: задачи SUBMITTED; кнопка → `TaskCB("review")` → `submission_text` + `review_kb`.
* `SubCB("ok")` → `review_confirm` → commit → `notify_review_result` → отредактировать сообщение: «✅ Подтверждено: 110 %».
* `SubCB("change")` → `score_kb(ai_score)` или ввод числа (`parse_percent`, 0..max_score) → «Комментарий к оценке?»
  (текст или Пропустить) → `review_set_score` → commit → `notify_review_result`.
* `SubCB("rework")` → «Что нужно доработать?» (обязательно) → новый срок (`deadline_kb` + «Оставить текущий» PickCB("deadline","keep")
  + текст) → `review_rework` → commit → `notify_rework`.
* `SubCB("files")` → `send_attachments` в этот чат.
* Если сдача уже проверена/не последняя/задача не SUBMITTED → alert «Результат уже обработан».

### 7.8 dashboard.py [B8]
* «📊 Команда» (руководитель) → `get_period("week", 0)`, `kpi_for_team`, `team_dashboard` + `team_kb`. `PeriodCB("team")` —
  переключение периода/листание (редактировать сообщение).
* `UserCB("card")` / `PeriodCB("emp")` → `employee_card` (неделя + месяц + выбранный период) + `employee_card_kb`.
* `UserCB("history")` → `evaluated_history` → `history_text` + `history_kb` (пагинация через UserCB(page)).
* «📈 Моя эффективность» (сотрудник) / `PeriodCB("me")` → своя карточка (`employee_card`) + period_kb("me").
  Сотрудник видит только себя.
* «📤 Экспорт» (руководитель) → `export_kb` → `PeriodCB("export")` → «⏳ Готовлю отчёт…» → `build_report_xlsx` →
  `answer_document(BufferedInputFile(data, filename="kpi_<период>.xlsx"))`.

## 8. Планировщик `bot/scheduler/jobs.py` [A5]

```python
def setup_scheduler(bot: Bot, sessionmaker) -> AsyncIOScheduler
    # interval settings.scheduler_interval_min -> run_reminders и recover_stalled_evaluations (id="stalled_evaluations");
    # cron (digest_weekday, digest_hour, tz) -> weekly_digest(once=True).
    # Бот запущен позже времени сводки, но не больше чем на 6 ч -> первый запуск сводки через минуту (догнать пропущенную).
async def run_reminders(bot, sessionmaker, now: datetime | None = None) -> int   # вернуть число отправленных
    # в тихие часы ничего не отправлять (кроме ничего). due_reminders -> отправить -> mark_sent -> commit.
    # Тексты: «⏰ До срока задачи «…» осталось 3 дня (5 октября, 18:00)» + submit_kb;
    # «⌛ Срок задачи «…» истёк. Что фактически сделано? Какой получен результат? Какие документы подтверждают выполнение?» + submit_kb;
    # руководителю — «⚠️ Просрочена задача #N «…» (Иванов И.)» + кнопка открыть; «📝 Ждёт проверки N дн.» + TaskCB("review").
    # Если сотрудник заблокировал бота — пометить отправленным всё равно (не долбить).
async def weekly_digest(bot, sessionmaker, now: datetime | None = None, *, once: bool = False) -> None
    # каждому активному руководителю: team_dashboard за прошлую неделю (offset=-1) + team_kb
    # (+ кнопки «📝 На проверке (N)» / «📥 Предложения (N)», если есть что решать).
    # Доставлено хоть одному -> запись DigestLog; once=True и запись за эту неделю уже есть -> ничего не слать.
async def recover_stalled_evaluations(bot, sessionmaker, now: datetime | None = None) -> int
    # Сдачи с прерванной оценкой: задача SUBMITTED, сдача без ai_source и decision, created_at старше
    # evaluation_budget_sec() + 5 мин, но не старше суток. Каждая «занимается» JobLog("eval", str(sub.id)),
    # затем rules_score -> record_evaluation(source="rules") -> commit -> notify_submission руководителю,
    # сотруднику — «✅ Результат по задаче #N «…» передан руководителю на проверку». -> сколько передано.
```
`setup_scheduler` при `settings.backup_enabled` добавляет cron-задание `id="backup"` (`backup_hour`:00 по
`settings.tz`, coalesce, misfire_grace_time 1 ч; неверный час -> 23) -> `bot/scheduler/backup.py`:
```python
MAX_BACKUP_BYTES = 45 * 1024 * 1024        # Telegram принимает от бота файлы до 50 МБ
async def make_backup_bytes(database_url, *, max_bytes: int | None = None) -> bytes | None
    # согласованный снимок файла SQLite: sqlite3 online backup (src.backup(:memory:) -> serialize) в asyncio.to_thread;
    # None — база не SQLite / не в файле (:memory:) / файла нет; снимок больше max_bytes -> BackupTooLarge(size, limit).
async def send_backup(bot, sessionmaker, now: datetime | None = None) -> int
    # каждому активному руководителю (users.list_managers) — документ kpi_backup_ГГГГ-ММ-ДД.db (местная дата),
    # без звука, подпись «💾 Резервная копия базы за 02.10.2026. Храните этот файл: …»; после первой загрузки —
    # тот же file_id. База больше MAX_BACKUP_BYTES — warning в лог и один раз (до удачной копии/перезапуска)
    # сообщение руководителям. Никогда не бросает; -> скольким руководителям доставлен файл.
```

## 9. main.py [A5]
* `load .env`, проверить `BOT_TOKEN` (понятная ошибка по-русски), логирование (`settings.log_level`, без ключей).
* `Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))`, `Dispatcher(storage=DbStorage(...),
  events_isolation=SimpleEventIsolation())` — диалоги в таблице `fsm_state` (§10.1; на PostgreSQL — свой пул, §10.7).
* `dp.update.outer_middleware(DbSessionMiddleware(sm))`, затем `dp.update.outer_middleware(UserMiddleware())`.
* Роутеры в порядке из раздела 7 + `start.fallback_router` последним.
* `dp.errors` → `DomainError`: callback → `answer(msg, show_alert=True)`, message → `answer(msg)`;
  прочие → лог + «⚠️ Произошла ошибка, попробуйте ещё раз».
* `set_my_commands` (start, menu, help, cancel; для руководителей — /new /team /review /tasks /staff /export,
  для сотрудников — /my /propose /submit /kpi — достаточно общего списка).
* `init_db`, `setup_scheduler(...).start()`, `dp.start_polling(bot)`; при остановке — `scheduler.shutdown()`, `engine.dispose()`.
* `def build_dispatcher(sessionmaker) -> Dispatcher` — отдельно, чтобы тесты могли собирать бота без сети.

## 10. Режим webhook и облачная база

Цель — бесплатная работа 24/7 **без банковской карты** и почти без участия владельца: бот на **Render Free**,
данные в **Supabase Free** (PostgreSQL). Внешних сервисов-«будильников» (cron-job.org, UptimeRobot) нет:
бот сам не даёт Render себя усыпить и сам выполняет задания по времени (§10.6). Инструкция для владельца —
`docs/DEPLOY_RENDER.md`, Blueprint — `render.yaml`, значения для Render — `deploy/make_render_env.py` (§10.9),
образ — `Dockerfile` (один на оба режима). Режим `polling` (свой компьютер, VPS, SQLite) работает
**как раньше**; всё новое включается только `RUN_MODE=webhook`.

### 10.1 Два режима

| | `polling` (по умолчанию) | `webhook` |
|---|---|---|
| Где | свой ПК (`run.bat`), VPS (`docker compose`) | Render и другие веб-хостинги |
| Как приходят апдейты | `dp.start_polling` | Telegram → HTTPS POST на адрес бота (`bot/web.py`) |
| Задания по времени | APScheduler в процессе (`setup_scheduler`) | фоновый цикл `web.BackgroundLoop` каждые 5 мин → `jobs.run_due_jobs` (`GET /tick` — необязательный внешний резерв) |
| Не уснуть на хостинге | — | `BackgroundLoop` каждые 10 мин: `GET <base_url>/health` через публичный адрес |
| База | SQLite `data/bot.db` (по умолчанию) | PostgreSQL (Supabase, session pooler) |
| Диалоги (FSM) | `bot/fsm_storage.DbStorage` (таблица `fsm_state`, запись вдогонку для SQLite) | `DbStorage` (на PostgreSQL — свой пул, §10.7): переживают перезапуск и деплой |

### 10.2 Настройки (`bot/config.py`, готово)

| Поле / env | По умолчанию | Смысл |
|---|---|---|
| `run_mode` / `RUN_MODE` | `polling` | `polling` \| `webhook` |
| `public_url` / `PUBLIC_URL` | `""` | публичный адрес бота |
| `render_external_url` / `RENDER_EXTERNAL_URL` | `""` | задаёт Render сам (`https://<имя>.onrender.com`) |
| `port` / `PORT` | `8080` | порт веб-сервера; Render задаёт `PORT` сам — слушать `0.0.0.0:$PORT` |
| `webhook_secret` / `WEBHOOK_SECRET` | `""` | пусто → `sha256("webhook:" + bot_token)[:32]` |
| `tick_secret` / `TICK_SECRET` | `""` | пусто → `sha256("tick:" + bot_token)[:32]`; нужен только внешнему резервному будильнику |
| `database_password` / `DATABASE_PASSWORD` | `""` | пароль PostgreSQL отдельно от `DATABASE_URL` (§10.7); `repr=False`, в лог не пишется |
| `takeover_webhook` / `TAKEOVER_WEBHOOK` | `false` | `polling`: снять включённый webhook и работать здесь (§10.5); без него запуск при включённом webhook останавливается |

Свойства: `base_url` = (`public_url` или `render_external_url`) без `/` в конце; `webhook_secret_value`,
`tick_secret_value` — заданное значение или выведенное из токена (только `[0-9a-f]`, годится и для
`secret_token` Telegram, и для URL; стабильно, пока не сменён `BOT_TOKEN`). Секреты и токен **не пишутся в лог**.

### 10.3 Платформа: на что рассчитан код

* **Render Free (web service, Docker из GitHub).** 512 МБ памяти; файловая система **эфемерная** (SQLite
  нельзя — всё теряется при деплое, рестарте, засыпании); **засыпает через 15 мин без входящего HTTP**;
  может перезапуститься в любой момент; при деплое (zero-downtime) **старый и новый экземпляры работают
  одновременно ~60–90 с**; трафик переключается, когда новый отвечает на `healthCheckPath`. 750 бесплатных
  часов в месяц на workspace (ровно на один сервис 24/7), 5 ГБ исходящего трафика в месяц.
  В `render.yaml` обязательно `plan: free` (без него Render создаёт платный инстанс), `region: frankfurt`,
  `healthCheckPath: /health`; секреты — `sync: false`. Инструкция `VOLUME` в Dockerfile не используется
  (часть облачных хостингов отклоняет образы с ней; `docker compose` монтирует `./data` сам).
* **Supabase Free (PostgreSQL).** Подключение только через **Supavisor session pooler**:
  `postgresql://postgres.<ref>:<пароль>@aws-N-<регион>.pooler.supabase.com:5432/postgres` (IPv4, порт 5432).
  Direct connection (`db.<ref>.supabase.co`) — только IPv6, с Render недоступна; transaction pooler (порт 6543)
  не рекомендуется (поддержан без кэша подготовленных выражений, §10.7). **SSL обязателен.** Проект «засыпает» после недели без
  обращений к базе — боту это не грозит (задания по времени обращаются к базе каждые 5 мин). Data API
  (PostgREST) боту не нужен: `init_db` включает RLS на таблицах бота (без политик — через REST они недоступны).
* **Без внешнего будильника.** Render считает «активностью» только **входящий** HTTP. Поэтому
  `BackgroundLoop` раз в 10 мин запрашивает `GET {base_url}/health` через **публичный** адрес
  (`RENDER_EXTERNAL_URL`): запрос выходит в интернет и приходит к Render снаружи — сервис не засыпает
  (10 мин < 15 мин с запасом на повтор). Трафик — единицы МБ в месяц. Если Render всё же усыпит сервис
  (сбой, перезапуск), его будит первый же апдейт Telegram (повторяет доставку), после старта цикл снова идёт.
  Резерв на крайний случай — любой внешний планировщик: `GET /health` (только будит) или
  `GET /tick?key=<tick_secret_value>` (будит и запускает задания); владельцу он не нужен.

### 10.4 HTTP-эндпоинты (режим webhook, `bot/web.py`)

`build_web_app(bot, dp, sessionmaker, settings) -> aiohttp.web.Application` (сеть и БД при сборке не нужны):

* `POST /tg/<webhook_secret_value>` (`webhook_path`, `webhook_url = base_url + webhook_path`) — апдейты Telegram.
  Заголовок `X-Telegram-Bot-Api-Secret-Token` сверяется с `webhook_secret_value` за постоянное время
  (`secrets_equal`, никогда не бросает: недопустимые байты в заголовке — просто «не совпало»), иначе `403`. Ответ `200` сразу, апдейт обрабатывается в фоне (`dp.feed_raw_update`):
  долгая AI-оценка не держит Telegram. Повтор того же `update_id` (последние 1000) не обрабатывается.
* `GET /tick?key=<tick_secret_value>` — необязательный внешний будильник (резерв, §10.3). Неверный ключ → `403`.
  Верный → `run_due_jobs` запускается **в фоне** (`TickRunner`, общий с `BackgroundLoop`: в процессе не больше
  одного запуска, таймаут 600 с), ответ сразу — JSON
  `{"ok": true, "status": "started"|"busy", "last_finished": ..., "last_result": {...}}` (быстро и коротко —
  годится для любых бесплатных планировщиков).
* `GET|HEAD /health` → `200` `ok` без обращения к БД и Telegram (health check Render и самопробуждение).
* `GET|HEAD /` → `KPI bot is running`.
* Секретный путь и ключ `/tick` в лог не пишутся; журнал HTTP-запросов aiohttp выключен (в нём была бы
  строка запроса с `?key=`).
* `app[BACKGROUND]` — `BackgroundLoop` (§10.6), `app[TICKER]` — общий `TickRunner`, `app[RECEIVER]` — приём апдейтов.
* Остановка (SIGTERM; Render ждёт ~30 с): `on_shutdown` по порядку — `_stop_background` (фоновый цикл: новые
  задания и запросы к себе не начинаются), `_drain` (апдейты и задания в работе получают до 20 с,
  `SHUTDOWN_GRACE_SEC`), `_on_shutdown` → `dp.emit_shutdown` (FSM-хранилище дописывает состояния).

### 10.5 Запуск и остановка в режиме webhook

* `check_run_mode` → `check_database` (§10.7) → `init_db` → веб-сервер на `0.0.0.0:port` (`AppRunner(access_log=None)`; сначала порт —
  Render ждёт открытого порта) → `get_me` → `ensure_webhook(bot, dp, settings)` → `set_my_commands` →
  `BackgroundLoop.start()` (в лог — расписание самопробуждения и заданий) → работа до SIGTERM.
  При остановке цикл останавливается первым (новые задания не начинаются), затем сервер (§10.4).
  `ensure_webhook` вызывает `setWebhook(url, secret_token=webhook_secret_value, allowed_updates=…,
  drop_pending_updates=False)` только если у Telegram другой адрес или другой список типов апдейтов
  (`getWebhookInfo`); накопившиеся апдейты не сбрасываются.
* При остановке **не вызывать `delete_webhook`**: во время деплоя новый экземпляр уже принимает апдейты,
  и удаление из старого оставило бы бота без сообщений.
* `polling` при старте проверяет webhook (`main._drop_webhook`): webhook включён — бот уже работает в облаке,
  и запуск останавливается `ConfigError` (в тексте — только имя сервера, без секретного пути): случайный
  `run.bat` на ПК не уводит бота из облака на старую локальную базу. Снять webhook и работать в `polling`
  можно только явно — `TAKEOVER_WEBHOOK=1` (`deleteWebhook(drop_pending_updates=False)`); облачную копию перед
  этим нужно остановить.
* Вторая защита — в самом облачном боте: `BackgroundLoop` вместе с заданиями (каждые 5 мин) вызывает
  `web.reclaim_webhook` — webhook снят или указывает на другой адрес (копия в `polling` старой версии, другой
  хостинг) → `setWebhook` обратно и предупреждение в лог. Отличие только в `allowed_updates` не исправляется
  (иначе старый и новый экземпляры при деплое перетягивали бы его друг у друга). Бот — строго в одном месте.
* Проверки настроек webhook (`main.check_run_mode`): нет `base_url` → `ConfigError`; адрес не `https://`,
  неверный `WEBHOOK_SECRET` или `PORT` → `ConfigError` с текстом по-русски.

### 10.6 Задания по времени и самопробуждение (`web.BackgroundLoop`, `jobs.run_due_jobs`)

`BackgroundLoop(ticker, keepalive_url, webhook_check=...)` — фоновая задача asyncio процесса бота (только `webhook`):
* каждые `JOB_INTERVAL_SEC` = 5 мин (первый раз — через 30 с после старта) — `run_due_jobs` через тот же
  `TickRunner`, что и `/tick`: прошлый запуск ещё идёт → этот пропускается; вместе с ним — `webhook_check()`
  (`build_web_app` передаёт `reclaim_webhook`, §10.5) отдельной задачей, таймаут 60 с, ошибка — только в лог;
* каждые `KEEPALIVE_INTERVAL_SEC` = 10 мин (первый раз — через 60 с) — `GET keepalive_url(settings)` =
  `<base_url>/health`, таймаут 20 с; неудача → повтор через 2 мин, в лог — только тип ошибки (без адреса).
  Нет `base_url` → самопробуждения нет.

```python
async def run_due_jobs(bot, sessionmaker, now: datetime | None = None) -> dict[str, object]
    # {"evaluations": <передано сдач>, "reminders": <отправлено>, "digest": "not_due"|"done"|"sent"|"nobody"|"failed",
    #  "backup": "disabled"|"not_due"|"done"|"sent:<N>"}; упавшее задание -> "error", остальные выполняются.
def tick_schedule_summary(interval_sec: float | None = None) -> str   # расписание заданий — для лога при запуске
```

* Сдачи с прерванной оценкой — `recover_stalled_evaluations` (§8) при каждом запуске, без учёта тихих часов
  (это запоздавшее уведомление о сдаче, а не напоминание).
* Напоминания — `run_reminders` при каждом запуске (цикл бота или `/tick`; тихие часы те же).
* Еженедельная сводка — если её время (`DIGEST_WEEKDAY`, `DIGEST_HOUR`) наступило не больше 6 ч назад и она
  ещё не ушла (`DigestLog`).
* Резервная копия — раз в местные сутки, начиная с `BACKUP_HOUR:00` (если `BACKUP_ENABLED`); повтора при
  неудаче нет, как и в `polling`: следующая копия — завтра.

Защита от двойного выполнения (два экземпляра при деплое, цикл бота и `/tick`): действие сначала «занимается»
записью в БД отдельной транзакцией, потом отправляется — напоминание `ReminderLog(task_id, kind)`, сводка
и копия — `JobLog(job, key)` уникально (`job` = `"digest"` / `"backup"`, `key` — начало недели / местная дата),
сдача с прерванной оценкой — `JobLog("eval", <id сдачи>)`.
`IntegrityError` → действие уже выполняет другой экземпляр → пропустить. Напоминание или сводка не доставлены
никому из-за сбоя → запись снимается, следующий запуск (через 5 мин) повторит.

### 10.7 PostgreSQL

* `bot/db/base.py` (`make_engine`, `normalize_url`, `postgres_connect_args`): `DATABASE_URL` вида `postgres://` /
  `postgresql://` приводится к `postgresql+asyncpg://` (драйвер `asyncpg` в `requirements.txt`); параметры libpq
  (`sslmode`, `connect_timeout`, `options`, ...) переводятся в аргументы asyncpg; для нелокального сервера
  без `sslmode` SSL включается как `require` — строку из Supabase вставляют как есть.
* `DATABASE_PASSWORD` (`settings.database_password`): `make_engine` → `apply_password` подставляет его в адрес
  PostgreSQL, если пароля в адресе нет или вместо него заглушка Supabase `[YOUR-PASSWORD]`
  (`is_password_placeholder`); спецсимволы экранировать не нужно; пароль, вписанный в адрес, важнее.
  Владелец вставляет строку «Session pooler» как есть, а пароль — отдельным полем Render (§10.9).
* Пул на экземпляр: `pool_size=3, max_overflow=1` (`PG_POOL_SIZE`, `PG_MAX_OVERFLOW`), `pool_pre_ping=True`,
  `pool_recycle=300` с; таймаут подключения 15 с. Плюс **отдельный пул хранилища диалогов** —
  `make_storage_engine(url) -> AsyncEngine | None` (PostgreSQL — 1 соединение, `PG_STORAGE_POOL_SIZE`; SQLite —
  `None`); `main.main` отдаёт его `DbStorage`. Причина: `DbStorage` на PostgreSQL пишет отдельной транзакцией,
  пока сессия хендлера держит своё соединение, — из общего пула 5 одновременных апдейтов разных людей
  ждали бы шестое соединение до `pool_timeout` (30 с). Итого ≤ 5 соединений на экземпляр, 10 при деплое
  (два экземпляра) — в пределах лимита бесплатного пулера Supabase. Порт 6543 / `?pgbouncer=true`
  (transaction pooler) — кэши подготовленных выражений выключаются; рекомендуемый режим всё равно
  session pooler (5432).
* `main.check_database(settings)` до подключения: заглушка `ВСТАВЬТЕ_СЮДА_…` (из `deploy/make_render_env.py`)
  в `DATABASE_URL`, пустой или неразборчивый адрес, не SQLite и не PostgreSQL → `ConfigError` с понятным
  текстом (значения в текст не попадают). `DATABASE_PASSWORD` проверяется, только если он нужен — в адресе
  PostgreSQL нет настоящего пароля: заглушка `ВСТАВЬТЕ_СЮДА_…` или `[YOUR-PASSWORD]` при пустом
  `DATABASE_PASSWORD` → `ConfigError`; пароль в адресе важнее, и заглушка в `DATABASE_PASSWORD` тогда не мешает
  (её пишет `make_render_env`, «строку можно не заполнять»). `RUN_MODE=webhook` + Direct connection Supabase
  (`db.*.supabase.co`, только IPv6) → `ConfigError` «возьмите Session pooler».
* `main` → `_prepare_database` (`init_db`): не удалось подключиться к PostgreSQL → `ConfigError` с причиной
  по-русски вместо трейсбека (`_db_startup_problem`): SQLSTATE 28P01/28000 — пароль или пользователь;
  3D000 — нет такой базы; «Tenant or user not found» (Supavisor) — не тот проект или он приостановлен;
  `gaierror` / `TimeoutError` / `ConnectionRefusedError` / прочие `OSError` — сервер недоступен. Прочие ошибки — как есть.
* `init_db`: `create_all` в текущей схеме (`public`) под `pg_advisory_xact_lock` (два экземпляра стартуют
  одновременно), затем RLS на таблицах бота. Время — naive UTC (`timestamp without time zone`), как в SQLite.
* Строгие ограничения PostgreSQL (длина VARCHAR, NUL в тексте, int32, naive datetime) соблюдаются до записи —
  `bot/services/dbsafe.py`.
* Перенос данных SQLite → PostgreSQL и восстановление из копии — `python -m bot.tools.restore <файл.db> [--force]`
  (в базу из `DATABASE_URL`): до первого запуска на Render, при остановленной старой копии; одна транзакция,
  `id` сохраняются, последовательности сдвигаются на `max(id)`, непустая база — только с `--force`.

### 10.8 Резервная копия

Ежедневная копия базы руководителям в Telegram (§8, `BACKUP_ENABLED`, `BACKUP_HOUR`) работает в обоих режимах:
в `polling` — по APScheduler, в `webhook` — фоновым циклом бота (`JobLog("backup", <местная дата>)`). Копия — **всегда
файл SQLite** `kpi_backup_ГГГГ-ММ-ДД.db`: для SQLite — online backup, для PostgreSQL — `backup.export_database`
(все таблицы `Base.metadata` на один момент времени, REPEATABLE READ, в файл SQLite той же схемы). Такой файл
бот открывает как `data/bot.db`, а в PostgreSQL его загружает `bot.tools.restore` — без доступа к Render.

### 10.9 Развёртывание на Render (участие владельца — минимальное)

* `render.yaml` (Blueprint): один `type: web`, `runtime: docker`, `plan: free`, `region: frankfurt`,
  `healthCheckPath: /health`, `autoDeploy: true`; `envVars`: `RUN_MODE=webhook`, `TIMEZONE=Asia/Tashkent`,
  `AI_PROVIDER=gemini` и `sync: false` (Render спрашивает при создании) — `BOT_TOKEN`, `ADMIN_IDS`,
  `GEMINI_API_KEY`, `DATABASE_URL`, `DATABASE_PASSWORD`. Тест `tests/test_make_render_env.py` сверяет этот
  список со строками, которые пишет `deploy/make_render_env.py`.
* `deploy/make_render_env.py [--env .env] [--out deploy/render.env] [--database-url СТРОКА|-]
  [--database-password ПАРОЛЬ|-]` — только стандартная библиотека (+ python-dotenv, если есть). Из `.env`
  берёт `BOT_TOKEN` (проверка формата), `ADMIN_IDS` (числа через запятую), `GEMINI_API_KEY`; пишет ровно
  6 строк `KEY=значение`: эти три, `RUN_MODE=webhook`, `DATABASE_URL`, `DATABASE_PASSWORD` (значения из
  параметров, `-` — скрытый ввод; иначе — значения из прежнего `deploy/render.env`; иначе заглушки
  `ВСТАВЬТЕ_СЮДА_…`, которые владелец заменяет в Блокноте). На экран — только названия настроек и
  пояснения, **никогда значения**; ошибка — код 2, файл не создаётся; в сам `.env` не пишет.
  `deploy/render.env` — в `.gitignore`, папка `deploy` — в `.dockerignore`.
* Владелец (`docs/DEPLOY_RENDER.md`): GitHub (вход через Google; один раз «Authorize» в окне Git Credential
  Manager, когда разработчик загружает код) → Supabase (проект `kpi-bot`, Frankfurt, сгенерированный пароль,
  строка «Session pooler» как есть → в `deploy/render.env`) → Render (вход через GitHub, New → Blueprint,
  значения из `deploy/render.env`) → статус **Live** → `/start`. Перенос данных (`bot.tools.restore`),
  создание закрытого хранилища, `git push` и проверка адреса сервиса (поле `url` из `getWebhookInfo`, `/health`) —
  разработчик.
