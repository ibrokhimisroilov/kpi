# MINIAPP_SPEC — приложение в Telegram (Mini App) для бота «Эффективность»

Это обязательный контракт для трёх агентов, которые строят Mini App параллельно. Кроме этого документа
у них есть только код. Всё, что здесь не сказано, — по `SPEC.md` (контракт бота) и `docs/TZ.md`
(требования заказчика). При расхождении кода с документом правится код; документ меняется только
осознанно и согласованно.

Обозначения: **API** — агент бэкенда Mini App, **CORE** — агент общего конвейера сдачи и интеграции с
ботом, **UI** — агент фронтенда (SPA). «Чат» — существующий Telegram-бот (handlers/*).

---

## 0. Цель, границы, принципы

**Цель.** Приложение открывается кнопкой меню бота «Открыть» и кнопкой «📱 Открыть приложение» после
приветствия `/start`.
* Начальник: дашборд команды с графиками, список задач с фильтрами и поиском, карточка задачи,
  очередь проверки (предложение AI → подтвердить / изменить оценку / вернуть на доработку),
  подтверждение поручений сотрудников, форма «Новая задача» с кнопкой AI «Сделать измеримым»,
  выгрузка Excel в чат.
* Сотрудник: «Мои задачи» (принять, открыть), «Сдать результат» с загрузкой файлов, «Мой KPI» с
  историей оценок, «Поручение» (внести устное поручение начальника).

**Не делаем (остаётся только в чате):** регистрация (`/start`, анкета), управление сотрудниками
(заявки, роли, блокировка — «👥 Сотрудники»). Новых таблиц, колонок и миграций нет. Сервисы
`bot/services/*` не меняются, кроме нового модуля `submission_flow.py` (§9).

**Чат работает как раньше:** тексты, кнопки, диалоги FSM, уведомления и бюджеты обменов с базой
(`tests/perf/test_roundtrip_budget.py`) не меняются. Единственные видимые изменения в чате — кнопка
меню «Открыть» и сообщение с кнопкой «📱 Открыть приложение» после приветствия `/start`, и только в
режиме webhook (§3).

**Принципы**
1. **Бесплатно.** Mini App раздаёт сам бот (aiohttp на Render Free), база та же (Supabase Free),
   AI — та же бесплатная цепочка. Никаких CDN, шрифтов, библиотек, аналитики и сборщиков. Единственный
   внешний скрипт — официальный `https://telegram.org/js/telegram-web-app.js`.
2. **Одни сервисы.** Каждое действие в приложении вызывает ту же функцию сервиса, что и чат, с теми же
   проверками прав (`DomainError` из сервисов → HTTP 400 с тем же русским текстом).
3. **Те же уведомления.** Другим людям приложение пишет через те же функции `bot.notify.*`, что и чат,
   в те же моменты (после commit). Таблица соответствия — §8.1.
4. **Бережно к базе.** Одна сессия БД на запрос, без ленивых загрузок и N+1; число обменов с базой на
   запрос фиксировано и не растёт с числом строк (бюджеты — §12.5). Перед долгой сетевой работой
   (AI, загрузка файлов в Telegram) соединение возвращается в пул (`await session.commit()`).
5. **Безопасность.** Вход — только по подписанному Telegram `initData` (§5); права — по записи `User`
   в базе (а не по данным клиента); в SPA никакого `innerHTML` с данными.
6. **Русский интерфейс**, местное время `settings.timezone` (Asia/Tashkent), формат чисел как в чате
   (`fmt_pct` → «102 %», `fmt_num` → «2,5»).

---

## 1. Агенты, владение файлами, порядок работы

Каждый агент правит **только свои файлы**. Нужна чужая функция, которой нет в контракте, — пишется
локальный приватный хелпер в своём модуле и упоминается в отчёте.

| Агент | Файлы (создаёт/правит) | Тесты (создаёт) |
|---|---|---|
| **API** | `bot/webapp/__init__.py`, `bot/webapp/context.py`, `bot/webapp/auth.py`, `bot/webapp/api.py`, `bot/webapp/serializers.py`, `bot/webapp/queries.py`, `bot/webapp/pages.py`, `bot/webapp/dev.py` | `tests/miniapp/__init__.py`, `tests/miniapp/conftest.py`, `tests/miniapp/test_auth.py`, `test_access.py`, `test_tasks_api.py`, `test_review_api.py`, `test_proposals_api.py`, `test_submit_api.py`, `test_kpi_api.py`, `test_misc_api.py`, `test_pages.py`, `test_dev_server.py`; `tests/e2e/test_webapp_flow.py`; `tests/perf/test_webapp_budget.py` |
| **CORE** | `bot/services/submission_flow.py` (новый), `bot/handlers/task_submit.py` (переход на конвейер), `bot/config.py`, `bot/web.py`, `bot/main.py`, `bot/handlers/start.py`, `.env.example`, `SPEC.md` (§10.2 — строки настроек, новый короткий §11 со ссылкой сюда), `README.md` (раздел о приложении), `docs/DEPLOY_RENDER.md` (заметка) | `tests/e2e/test_submission_flow.py`, `tests/test_webapp_integration.py` |
| **UI** | `bot/webapp/static/index.html`, `bot/webapp/static/app.js`, `bot/webapp/static/app.css`; при необходимости `.claude/launch.json` (запуск dev-сервера для превью) | `tests/miniapp/test_static_assets.py` |

**Общие правила для всех агентов**
* Не трогать `data/`, `.env`, `deploy/render.env`. Не запускать боевого бота (`python -m bot`), не
  делать настоящих запросов к Telegram и AI в тестах (Telegram — `tests/e2e/fakebot.py`, AI выключен
  `AI_PROVIDER=none` или подменён).
* Python: `./.venv/Scripts/python` с `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.
  Полный прогон: `./.venv/Scripts/python -m pytest -q -p no:cacheprovider` (сейчас 1161 passed —
  после работы должно быть 1161 + новые, ни одного упавшего и ни одного пропущенного из новых).
  Второй прогон — на PostgreSQL, если доступен:
  `TEST_DATABASE_URL=postgresql://postgres:pgtest@127.0.0.1:55433/kpi_test`.
* Стиль кода — как в проекте: `from __future__ import annotations`, докстринги и комментарии по-русски,
  идентификаторы по-английски, тексты пользователю — по-русски, без f-строк в логах с секретами.

**Стыки и порядок**
1. CORE **первым делом** создаёт `bot/services/submission_flow.py` строго по сигнатурам §9 (это
   маленький модуль). API импортирует его **внутри** обработчика сдачи и фоновой функции
   (`from bot.services import submission_flow` в теле функции), чтобы остальной API работал и
   тестировался до его появления.
2. API экспортирует `setup_webapp`, `pending_tasks` (§4.1) и `api.ROUTES` (§8.2). CORE подключает их
   в `bot/web.py` через `importlib.util.find_spec("bot.webapp")` (как `main.default_storage`), поэтому
   его код не падает, пока пакета нет; тесты CORE, которым нужен пакет, используют
   `pytest.importorskip("bot.webapp")` (в финальном прогоне пакет есть — пропусков быть не должно).
3. UI пишет SPA по §7, §8, §11 и проверяет его в браузере через dev-сервер API (§4.6). Пока dev-сервера
   нет — по этому документу.
4. Финальная интеграция: полный прогон (SQLite, затем PostgreSQL), `tests/perf` и ручная проверка
   SPA на dev-сервере (§12.4).

---

## 2. Архитектура

```
Telegram (iOS / Android / Desktop / Web)
   │  кнопка меню «Открыть» или inline-кнопка WebAppInfo → открывает {base_url}/app
   ▼
GET /app ─────────────► bot/webapp/pages.py: index.html (+ версия ассетов и JSON-конфиг), без БД
GET /app/static/*  ───► app.js, app.css (белый список, кэш по версии)
   │
   │  SPA (app.js): fetch /api/... с заголовком X-Telegram-Init-Data: <Telegram.WebApp.initData>
   ▼
/api/* (aiohttp sub-app, bot/webapp/api.py)
   ├─ middleware: ошибки → JSON; проверка initData (auth.py); сессия БД на запрос; загрузка User
   ├─ обработчики → bot.services.* (tasks, users, kpi, periods, export, submission_flow),
   │                 bot.ai.formulate, bot.webapp.queries (лёгкие чтения)
   ├─ уведомления → bot.notify.* (тот же Bot, что у Dispatcher)
   └─ фоновые задачи (TaskRegistry): оценка сдачи + уведомление начальнику, экспорт Excel,
                                      пересылка файлов — ждут остановки сервера (web._drain)
```

* Mini App доступен **только в режиме webhook** (на Render есть публичный HTTPS). В режиме polling
  веб-сервера нет, кнопки не ставятся. Для локальной проверки — dev-сервер (§4.6).
* Маршруты монтирует `bot/web.py::build_web_app` вызовом `setup_webapp(...)`, если
  `settings.webapp_enabled`. Порядок остановки сервера (`_stop_background` → `_drain` →
  `_on_shutdown`) не меняется; `_drain` дополнительно ждёт фоновые задачи приложения.
* Вход: Telegram передаёт в Mini App строку `initData`, подписанную токеном бота. Сервер проверяет
  подпись при **каждом** запросе (без cookie и серверных сессий) и находит `User` по `tg_id`.

---

## 3. Настройки и интеграция с ботом (CORE)

### 3.1 `bot/config.py`

Новые поля `Settings` (env без префикса, как остальные):

| Поле / env | По умолчанию | Смысл |
|---|---|---|
| `webapp_enabled` / `WEBAPP_ENABLED` | `True` | раздавать Mini App (`/app`, `/api`) и ставить кнопки (только webhook) |
| `webapp_debug` / `WEBAPP_DEBUG` | `False` | режим локальной проверки: SPA берёт подписанный `initData` из `?tg_debug_init=` (§5.4). В режиме webhook игнорируется |

Новые свойства:
```python
@property
def webapp_url(self) -> str:
    """Адрес Mini App: f"{base_url}/app", если webapp_enabled, run_mode == "webhook" и base_url
    начинается с https:// (без учёта регистра); иначе "" — кнопок и приложения нет."""

@property
def webapp_debug_active(self) -> bool:
    """webapp_debug and run_mode != "webhook" — на Render отладка невозможна, даже если WEBAPP_DEBUG=1."""
```

### 3.2 `bot/web.py`

* `build_web_app(bot, dp, sessionmaker, settings)`: если `settings.webapp_enabled` и пакет `bot.webapp`
  есть (`importlib.util.find_spec`) — `setup_webapp(app, bot=bot, sessionmaker=sessionmaker,
  settings=settings)` **после** маршрутов `/tg/…`, `/tick`, `/health`, `/`. Выключено — `/app` и
  `/api/*` отвечают 404 (маршрутов нет).
* `_drain`: `pending = app[RECEIVER].pending() | app[TICKER].pending() | webapp_pending(app)`, где
  `webapp_pending` — `bot.webapp.pending_tasks(app)` (пустое множество, если пакета нет или приложение
  не смонтировано). Время ожидания то же — `SHUTDOWN_GRACE_SEC`.
* Докстринг модуля и §10.4 SPEC.md дополняются строками про `/app`, `/app/static/*`, `/api/*`.

### 3.3 `bot/main.py`

* `_run_webhook`: после `await _set_commands(bot)` — `await _setup_menu_button(bot, settings)`:
  ```python
  async def _setup_menu_button(bot: Bot, settings: Settings) -> None:
      """Кнопка меню чата: «Открыть» → Mini App (webhook и webapp_url); иначе — стандартная кнопка.
      Ошибка Telegram — только предупреждение в лог (тип ошибки), запуск продолжается."""
      # url есть:  bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=url)))
      # url пуст:  bot.set_chat_menu_button(menu_button=MenuButtonDefault())
  ```
  Без `chat_id` — кнопка по умолчанию для всех личных чатов. Вызывается при каждом запуске (1 запрос).
* `_run_polling`: кнопку **не трогать** (публичного HTTPS нет — молча пропустить). Исключение:
  `_drop_webhook(..., takeover=True)` после успешного `delete_webhook` ставит `MenuButtonDefault()`
  (облачная копия выводится из работы, её адрес больше не откроется); ошибка — в лог.
* `_log_startup_hints`: в webhook — `INFO "Mini App: <webapp_url> (кнопка «Открыть» в чате)"` или
  `"Mini App выключен (WEBAPP_ENABLED=0)"`; если `webapp_debug` и webhook —
  `WARNING "WEBAPP_DEBUG игнорируется в режиме webhook"`.

### 3.4 `bot/handlers/start.py`

* `_send_welcome` (активный пользователь, после приветствия с главным меню): если
  `get_settings().webapp_url` не пуст — ещё одно сообщение через `common.send_new`:
  * начальнику: «📱 Команда, задачи, проверка результатов и KPI — в приложении.»
  * сотруднику: «📱 Ваши задачи, сдача результата и KPI — в приложении.»
  * клавиатура: `InlineKeyboardMarkup([[InlineKeyboardButton(text="📱 Открыть приложение",
    web_app=WebAppInfo(url=webapp_url))]])`.
* Неактивным (заявка, блокировка, регистрация) — не отправлять. В polling (`webapp_url == ""`) —
  не отправлять: существующие e2e-тесты `/start` не меняются. Бюджет S1 не меняется (нет обращений к БД).

### 3.5 Документация (CORE)

`.env.example` — две строки с пояснением; `SPEC.md` — строки в таблице §10.2 и короткий
§11 «Mini App» (ссылка на этот документ, маршруты, настройки); `README.md` — раздел «Приложение в
Telegram»: как открыть, что в нём есть, что BotFather настраивать не нужно (кнопка меню ставится
ботом сам при запуске на Render); `docs/DEPLOY_RENDER.md` — заметка «после деплоя в чате появится
кнопка «Открыть»; ничего настраивать не нужно».

---

## 4. Пакет `bot/webapp` (API)

### 4.1 `bot/webapp/__init__.py` — публичный вход

```python
STATIC_DIR: Path                       # Path(__file__).parent / "static"
CTX: web.AppKey[WebappContext]         # контекст в sub-app /api и в основном приложении
TASKS: web.AppKey[TaskRegistry]        # реестр фоновых задач (в основном приложении)

def setup_webapp(app: web.Application, *, bot: Bot, sessionmaker: async_sessionmaker[AsyncSession],
                 settings: Settings, static_dir: Path = STATIC_DIR) -> None:
    """Смонтировать Mini App: GET /app, /app/, /app/static/{name} (pages.py) и sub-app /api (api.py).
    Сеть и БД при сборке не нужны. Повторный вызов на том же app — RuntimeError."""

def pending_tasks(app: web.Application) -> set[asyncio.Task[Any]]:
    """Незавершённые фоновые задачи приложения (для web._drain); пусто, если не смонтировано."""
```

### 4.2 `bot/webapp/context.py`

```python
@dataclass
class WebappContext:
    bot: Bot
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    tasks: TaskRegistry
    gate: UserGate
    static: StaticBundle          # pages.py

class TaskRegistry:
    """Фоновые задачи приложения: сильные ссылки (задачу не соберёт GC), исключения — в лог."""
    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]
    def pending(self) -> set[asyncio.Task[Any]]
    async def drain(self, timeout: float | None = None) -> None     # для тестов: дождаться всех

class UserGate:
    """«Не больше одного одновременно» на пользователя и вид действия (в памяти процесса)."""
    def try_enter(self, kind: str, tg_id: int) -> bool              # False — уже занято
    def leave(self, kind: str, tg_id: int) -> None
    @asynccontextmanager
    async def hold(self, kind: str, tg_id: int, busy_message: str) -> AsyncIterator[None]
        # занято -> ApiError(429, "busy", busy_message); освобождает при выходе

class RateLimiter:
    """«Не больше N за окно времени» на пользователя и вид действия (в памяти процесса)."""
    def hit(self, kind: str, key: int, limits: Sequence[tuple[int, float]]) -> float | None
        # засчитать, если ни один лимит (сколько, за сколько секунд) не превышен -> None;
        # иначе (не засчитывая) -> через сколько секунд можно снова
    def undo(self, kind: str, key: int) -> None   # действие не выполнено (ошибка данных) — не в счёт
```
`WebappContext.limits: RateLimiter`. UserGate не даёт выполнять действие одновременно, RateLimiter —
слишком часто подряд (HTTP 429, код `rate_limited`, текст «… повторите через 40 с / 7 мин / 3 ч»):

| kind | Где | Лимит | Текст |
|---|---|---|---|
| `formulate` | POST /api/ai/formulate | 6 в минуту; в сутки — начальник 60, сотрудник 20 | «⏳ Слишком много запросов к AI подряд — повторите через {wait} или сформулируйте результат сами.» |
| `formulate_team` | POST /api/ai/formulate | 200 подсказок AI на всю команду в сутки (считается, только когда AI включён) — дальше правила (не 429) с `notice` «⚠️ AI-подсказки на сегодня закончились — показан вариант по правилам. Отредактируйте его сами.» | — |
| `propose` | POST /api/proposals | 5 за 10 минут, 20 в сутки; отказ сервиса (400 `domain`) не в счёт | «⏳ Слишком много поручений подряд — следующее можно внести через {wait}.» |

Зачем: каждая подсказка — вся цепочка AI, а квоты бесплатных моделей общие с оценкой сдач (их
нельзя «выжечь» циклом запросов с одним initData); каждое поручение — карточка с кнопками всем
начальникам (в чате на это нужен целый диалог).
Виды и тексты отказа (HTTP 429, код `busy`):

| kind | Где | Текст |
|---|---|---|
| `formulate` | POST /api/ai/formulate | «⏳ Подождите, формулирую вариант…» |
| `create` | POST /api/tasks | «⏳ Задача уже создаётся…» |
| `propose` | POST /api/proposals | «⏳ Поручение уже отправляется…» |
| `submit` | POST /api/tasks/{id}/submit | «⏳ Результат уже отправляется…» |
| `export` | POST /api/export (держится до конца фоновой выгрузки) | «⏳ Отчёт уже готовится — он придёт в чат с ботом.» |
| `files` | POST /api/submissions/{id}/files (до конца пересылки) | «⏳ Файлы уже отправляются в чат.» |

### 4.3 Статика (`bot/webapp/pages.py`)

* Маршруты основного приложения (не sub-app, без middleware и без БД):
  `GET /app`, `GET /app/` → index; `GET /app/static/{name}` → только файлы из белого списка
  `{"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}`; остальное — 404
  (в т.ч. `..`, `%2e%2e`, подпапки). HEAD — автоматически (aiohttp `add_get`).
* `StaticBundle` читает файлы при `setup_webapp`. Файла нет (UI ещё не закончил) — предупреждение в лог,
  `/app` отвечает 503 `text/plain` «Приложение ещё не собрано». При `settings.webapp_debug_active`
  файлы перечитываются на каждый запрос (правка без перезапуска dev-сервера).
* `ASSET_VERSION` = первые 12 hex-символов `sha256(app.js + app.css)`.
* В `index.html` (его пишет UI) есть ровно эти заглушки, сервер подставляет их при отдаче:
  * `__ASSET_VERSION__` — в `src="/app/static/app.js?v=__ASSET_VERSION__"` и
    `href="/app/static/app.css?v=__ASSET_VERSION__"`;
  * `__KPI_CONFIG__` — содержимое `<script id="kpi-config" type="application/json">__KPI_CONFIG__</script>`:
    `{"version": "<ASSET_VERSION>", "debug": <webapp_debug_active>}`; JSON с `<` → `<`.
* Заголовки index: `Content-Type: text/html; charset=utf-8`, `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Content-Security-Policy: default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'
  'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none';
  base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org`
  (Telegram Web открывает Mini App во фрейме — `X-Frame-Options` не ставить).
* Заголовки статики: `X-Content-Type-Options: nosniff`; `Cache-Control: public, max-age=31536000,
  immutable`, если `?v=` равен текущей версии, иначе `no-cache`.
* Пути в `index.html` — только абсолютные (`/app/static/...`): страница открывается и как `/app`, и
  как `/app/`.

### 4.4 Middleware sub-app `/api`

`api = web.Application(middlewares=[error_middleware, auth_session_middleware])`,
`app.add_subapp("/api", api)`; `api[CTX] = ctx` (и `app[CTX]`, `app[TASKS]` в основном).

Неизвестный путь под `/api` и неверный метод проходят через middleware sub-app (проверено на
aiohttp 3.14: маршрутизатор поднимает `HTTPNotFound` / `HTTPMethodNotAllowed` внутри цепочки), поэтому
отдельный маршрут-«ловушка» не нужен: `error_middleware` превращает их в JSON 404 / 405 (а без
initData такой запрос получает 401 — проверка входа идёт раньше).

1. `error_middleware` — любое исключение → JSON (§6.2): `ApiError` → как есть; `AuthError` → 401;
   `DomainError` → 400 `domain` с `exc.message`; `web.HTTPNotFound` → 404 `not_found`;
   `web.HTTPMethodNotAllowed` → 405 `method_not_allowed`; `web.HTTPRequestEntityTooLarge` (JSON больше
   1 МиБ) → 413 `too_large` «Слишком большой запрос.»; `asyncio.CancelledError` — пробросить;
   прочее → `log.exception` (без тела запроса и заголовков) и 500 `internal`. Ко всем ответам
   `/api` — `Cache-Control: no-store` и `X-App-Version: <ASSET_VERSION>`.
2. `auth_session_middleware`:
   * `init = auth.validate_init_data(request.headers.get(INIT_DATA_HEADER, ""), ctx.settings.bot_token.strip())`
     → `request["init"]`;
   * тело запроса (если есть; кроме сдачи — её multipart читает обработчик потоком и после commit)
     читается целиком ДО открытия сессии: `await asyncio.wait_for(request.read(), BODY_READ_TIMEOUT_SEC)`
     (15 с) → не успело — 408 `request_timeout`; больше 1 МиБ — 413. aiohttp не ограничивает время
     чтения тела: без этого клиент, приславший заголовки без тела, держал бы соединение пула (3 + 1)
     сколько угодно. Прочитанные байты aiohttp запоминает — `_body()` их не перечитывает;
   * `async with ctx.sessionmaker() as session:` → `request["session"] = session`;
     `request["viewer"] = await bot.middlewares.load_user(session, init.tg_id)` (один запрос, повтор при
     оборванном соединении — та же функция, что у чата);
   * обработчик → при успехе `await session.commit()` (у сессии, которая только читала, это не обмен с
     базой), при исключении — `await session.rollback()` и проброс.

Хелперы доступа (в `api.py`):
```python
def active_viewer(request) -> User     # нет записи -> 403 not_registered; PENDING -> 403 pending; BLOCKED -> 403 blocked
def manager_viewer(request) -> User    # active_viewer + is_manager, иначе 403 forbidden «Действие доступно только начальнику»
def employee_viewer(request) -> User   # active_viewer + role EMPLOYEE, иначе 403 forbidden «Действие доступно только сотруднику»
```

### 4.5 Логирование

* Никогда не писать в лог `initData`, `hash`, токен, тело запроса, имена файлов сотрудников.
  Журнал HTTP-запросов aiohttp уже выключен (`AppRunner(access_log=None)`) — не включать.
* `DEBUG`: `"API %s %s -> %s (%d мс)"` с шаблоном маршрута (`/api/tasks/{task_id}`), не с URL.
* `INFO`: отказы 401/403 одной строкой с кодом (`"API 401 auth_expired"`), без деталей.
* `ERROR`: 500 и сбои фоновых задач (`log.exception`).

### 4.6 Dev-сервер для локальной проверки (`bot/webapp/dev.py`)

`python -m bot.webapp.dev [--port 8081] [--db ПУТЬ] [--seed-demo] [--real-telegram]`

* До импорта `bot.config` выставляет окружение: `RUN_MODE=polling`, `WEBAPP_ENABLED=1`,
  `WEBAPP_DEBUG=1`, `DATABASE_URL=sqlite+aiosqlite:///<db>`; без `--real-telegram` ещё
  `BOT_TOKEN=42:DEV` и `AI_PROVIDER=none` (секреты из `.env` не нужны и не используются).
* `--db` по умолчанию `<tempfile.gettempdir()>/kpi_webapp_dev.db`. Отказ (код 2, понятный текст), если
  путь указывает на `data/bot.db` проекта или `DATABASE_URL` — PostgreSQL. `DATABASE_URL` из `.env`
  игнорируется.
* Telegram: по умолчанию `FakeSession` из `tests/e2e/fakebot.py` (добавить `<repo>/tests` в `sys.path`,
  `from e2e.fakebot import FakeSession`; в Docker-образе папки tests нет — инструмент только для
  разработки). Каждый запрос бота к «Telegram» печатается в консоль: метод, chat_id, первые 100 символов
  текста — видно, какие уведомления ушли. `--real-telegram` — настоящий `Bot` с токеном из `.env`
  (только по явному флагу; агенты его не используют).
* Слушает только `127.0.0.1` и отвечает только на `Host` 127.0.0.1 / localhost / ::1 (иначе 403:
  защита от DNS rebinding — страница чужого сайта в браузере разработчика не достучится до
  `/dev/login`). С настоящим токеном (не `42:DEV`, то есть `--real-telegram`) подписанный initData
  годится и для рабочего бота, поэтому `/dev/` и `/dev/login` требуют ключ запуска `?key=…`
  (`secrets.token_urlsafe`, печатается в консоль вместе со ссылкой; без него — 403), а войти можно
  только за пользователя dev-базы (иначе 404). С тестовым токеном ключ не нужен.
  Приложение: `web.Application(middlewares=[_local_host_only])` + `setup_webapp(...)` + маршруты
  отладки (только здесь, не в `setup_webapp`):
  * `GET /dev/` — простая HTML-страница со списком пользователей базы и ссылками «войти как …»;
  * `GET /dev/login?tg_id=<id>` — 302 на `/app?tg_debug_init=<urlencoded свежий initData>`
    (`auth.sign_init_data` с токеном настроек, имя — из базы).
* Никаких webhook, polling, `BackgroundLoop`, `setWebhook`, `set_chat_menu_button`.
* `--seed-demo` (только если в базе нет пользователей; записи прямо через ORM, как
  `tests/perf/test_roundtrip_budget.py::_task`): начальник 1001 «Петрова Анна Сергеевна»;
  сотрудники 2001 «Иванов Иван Иванович» (Юрист), 2002 «Сидоров Пётр Ильич» (Экономист),
  2003 «Кузнецова Анна Сергеевна» (Аналитик); заявка 2004 (PENDING с ФИО). У каждого сотрудника:
  ACTIVE принятая и непринятая, просроченная ACTIVE, REWORK (сдача с `decision=rework` и
  комментарием), SUBMITTED (сдача + оценка по правилам с обоснованием + 1 документ `file_id="demo-doc-1"`),
  три DONE с оценками 90/100/110 на этой и прошлой неделях, одна PROPOSED (source=employee),
  одна CANCELLED. Сроки — относительно «сейчас».
* При запуске печатает: `Mini App (отладка): http://127.0.0.1:<port>/dev/` (с `--real-telegram` —
  `/dev/?key=<ключ запуска>`).

---

## 5. Аутентификация (`bot/webapp/auth.py`)

### 5.1 Интерфейс

```python
INIT_DATA_HEADER = "X-Telegram-Init-Data"
MAX_AGE_SEC = 24 * 60 * 60       # initData старше суток — «сессия устарела»
FUTURE_SKEW_SEC = 5 * 60         # auth_date из будущего больше чем на 5 мин — подделка
MAX_INIT_DATA_LEN = 8192

class AuthError(Exception):
    code: Literal["auth_missing", "auth_invalid", "auth_expired"]
    message: str                 # русский текст из §6.2

@dataclass(frozen=True)
class InitData:
    tg_id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None
    auth_date: int               # unix-время, с
    query_id: str | None
    start_param: str | None

def validate_init_data(init_data: str, bot_token: str, *, now: float | None = None,
                       max_age: int = MAX_AGE_SEC) -> InitData        # иначе AuthError; другого не бросает

def sign_init_data(bot_token: str, *, tg_id: int, first_name: str = "Тест", last_name: str | None = None,
                   username: str | None = None, auth_date: int | None = None,
                   extra: Mapping[str, str] | None = None) -> str
    # для тестов и dev-сервера: query-строка как у Telegram (user — JSON, auth_date, query_id, hash)
```

### 5.2 Алгоритм проверки (по документации Telegram «Validating data received via the Mini App»)

1. Пустая строка → `auth_missing`. Длиннее `MAX_INIT_DATA_LEN` или не `str` → `auth_invalid`.
2. `pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)`;
   `ValueError` → `auth_invalid`; повторяющийся ключ → `auth_invalid`; нет `hash` → `auth_invalid`.
3. `data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs без hash))` — **все** поля,
   кроме `hash` (поле `signature` из Bot API 8.0 входит в строку).
4. `secret_key = hmac.new(b"WebAppData", bot_token.encode(), sha256).digest()`;
   `expected = hmac.new(secret_key, data_check_string.encode(), sha256).hexdigest()`.
5. Сравнение `hmac.compare_digest(expected.encode(), given.encode("ascii"))` за постоянное время;
   не-ASCII в `hash` (`UnicodeError`) → `auth_invalid`, никогда не 500.
6. `auth_date` — целое; иначе `auth_invalid`. `now - auth_date > max_age` → `auth_expired`;
   `auth_date - now > FUTURE_SKEW_SEC` → `auth_invalid`.
7. `user` — JSON-объект с целым `id > 0` (не `bool`); `first_name` — строка (может быть пустой);
   иначе `auth_invalid`.
8. Пустой `bot_token` → всегда `auth_invalid` (как `web.secrets_equal`).

### 5.3 Пользователь и роли

* `User` ищется по `tg_id = init.tg_id` (`load_user`). Записи нет → `GET /api/me` отвечает 200 с
  `access: "unregistered"`, остальные эндпоинты — 403 `not_registered`. API **никогда** не создаёт
  пользователей (регистрация — только `/start` в чате).
* PENDING / BLOCKED → `GET /api/me` 200 с `access: "pending" | "blocked"`, остальное — 403 с этим кодом.
* Роль — из базы: начальник = `user.is_manager` (активный + `Role.MANAGER`). Данным `initData` о
  пользователе (имя, username) не доверять ни для чего, кроме поиска по `tg_id`.

### 5.4 Режим отладки (только локально)

* `settings.webapp_debug_active` (`WEBAPP_DEBUG=1` и не webhook) → в JSON-конфиге страницы
  `"debug": true`. SPA вне Telegram (пустой `Telegram.WebApp.initData`) берёт строку из
  `?tg_debug_init=` адреса страницы, сохраняет её в `sessionStorage["kpi_debug_init"]` (на случай
  перезагрузки), убирает параметр из адреса (`history.replaceState`) и шлёт её в том же заголовке
  `X-Telegram-Init-Data`.
* Сервер **не делает исключений**: подпись проверяется так же (строку подписывает dev-сервер или тест
  `sign_init_data` с настроенным токеном), срок 24 ч тот же. Параметр `tg_debug_init` на `/api` не
  читается никогда. При `debug: false` SPA параметр игнорирует.
* На Render (`RUN_MODE=webhook`) отладка выключена всегда (§3.1).

---

## 6. Общие правила API

### 6.1 Формат

* База — `/api`. Запросы — JSON (`Content-Type: application/json`, тело ≤ 1 МиБ — лимит aiohttp по
  умолчанию), кроме сдачи результата (`multipart/form-data`) и эндпоинтов без тела. Ответы — JSON
  (`json.dumps(ensure_ascii=False, separators=(",", ":"))`), UTF-8.
* Тело должно быть JSON-объектом; иначе 400 `bad_request` «Неверный запрос: ожидается JSON-объект».
  Неизвестные поля → 400 `bad_request` «Неизвестное поле «…»».
* Коды успеха: 200 — чтение и синхронные действия; 201 — создание (`POST /api/tasks`,
  `POST /api/proposals`); 202 — принято, доделывается в фоне (сдача, экспорт, пересылка файлов).
* id в пути — десятичное целое в пределах `dbsafe.is_db_id`; иначе 404 `not_found`.

**Время.** Все моменты — ISO-8601 UTC с `Z`: `iso(dt) = f"{dt:%Y-%m-%dT%H:%M:%S}Z"` для naive UTC из
базы (`None` → `null`). Рядом — готовые местные строки (Asia/Tashkent): `*_local` =
`bot.utils.dates.fmt_datetime` («05.10.2026 18:00»), короткие `dd.mm` / `dd.mm HH:MM` — где указано.

**Срок во входных данных** (`DeadlineInput`, функция `parse_deadline_input(value) -> datetime` naive UTC):
* `"YYYY-MM-DD"` — местная дата + `settings.default_deadline_time` (`dateparse.iso_to_deadline`);
* `"YYYY-MM-DDTHH:MM"` или `"YYYY-MM-DDTHH:MM:SS"` без смещения — местное время → `dates.to_utc`;
* ISO-8601 со смещением или `Z` — абсолютный момент → naive UTC;
* иначе (и не строка) → 400 `bad_request` «Срок: укажите дату в формате ГГГГ-ММ-ДД».
  Прошлое и «дальше 5 лет» проверяют сервисы (их `DomainError` → 400 `domain`).

**Числа во входных данных** (`parse_number_input`): `null`/`""` → `None`; `bool` → 400; конечные
`int`/`float` → `float`; строка → `bot.utils.text.parse_number` («1 200», «10,5», «110 договоров»),
`None` → 400 ««<Поле>»: ожидается число». `weight` — целое 1..100 (`float` с нулевой дробной частью
допустим); `score` — число 0..`settings.max_score`, хранится округлённым до целого (половина — вверх,
как `task_review._parse_score` в чате; правило — в `tasks.review_set_score`).

**Тексты** — `str`, обрезка пробелов по краям; пустая строка у необязательного поля = `null`. Лимиты:

| Поле | Лимит | Откуда |
|---|---|---|
| `title` | 1..255 | `tasks._MAX_TITLE_LEN` |
| `expected_result`, `description`, `raw_result` | ≤ 2000 (`raw_result` ≥ 3) | как `task_propose.RESULT_MAX` |
| `plan_unit` | ≤ 64 | колонка |
| `plan_value` | 0 < x ≤ 1e15 | как `task_create.PLAN_MAX` |
| `fact_text` | 3..3000 | `task_submit.MIN_FACT`, `MAX_TEXT` |
| `result_text` | ≤ 3000 | `task_submit.MAX_TEXT` |
| `materials_text` | ≤ 1500 | `task_submit.MAX_NOTES` |
| `comment` (оценка, доработка) | ≤ 2000 (доработка ≥ 1) | `task_review.MAX_COMMENT_LEN` |
| `reason` (отмена, отклонение) | ≤ 1000 | `task_propose.REASON_MAX` |
| `q` (поиск) | ≤ 100 | — |

Нарушение лимита → 400 `bad_request` с русским текстом (например, «Название: до 255 символов»).

### 6.2 Ошибки

Тело любой ошибки: `{"error": "<русский текст для пользователя>", "code": "<код>"}`.

| HTTP | code | Когда | Текст |
|---|---|---|---|
| 401 | `auth_missing` | нет заголовка / пустой | «Откройте приложение из Telegram — кнопка «Открыть» в чате с ботом.» |
| 401 | `auth_invalid` | подпись, формат, user, будущее | «Не удалось подтвердить вход через Telegram. Закройте приложение и откройте его снова из чата с ботом.» |
| 401 | `auth_expired` | старше 24 ч | «Сессия устарела. Закройте приложение и откройте его снова из чата с ботом.» |
| 403 | `not_registered` | нет `User` | «Вы ещё не зарегистрированы. Откройте чат с ботом и нажмите /start.» |
| 403 | `pending` | заявка | = `start.TXT_PENDING` («⏳ Заявка на рассмотрении у начальника.\nКак только вас подтвердят, придёт уведомление.») |
| 403 | `blocked` | заблокирован | = `start.TXT_BLOCKED` («⛔ Доступ закрыт. Обратитесь к начальнику.») |
| 403 | `forbidden` | роль / чужая задача | «Действие доступно только начальнику» / «Действие доступно только сотруднику» / = `common.NO_RIGHTS` («⛔ Недостаточно прав для этого действия.») |
| 404 | `not_found` | нет объекта / маршрута | «Задача не найдена.» / «Результат не найден.» / «Сотрудник не найден.» / «Не найдено.» |
| 400 | `bad_request` | формат/лимиты входных данных | по §6.1 |
| 400 | `domain` | `DomainError` сервиса или правило чата | `exc.message` как есть |
| 405 | `method_not_allowed` | метод не тот | «Метод не поддерживается.» |
| 413 | `too_large` | файлов > 10, файл > 20 МБ, всего > 50 МБ | «Можно приложить не более 10 файлов.» / «Файл «<имя>» больше 20 МБ.» / «Все файлы вместе — не больше 50 МБ.» |
| 408 | `request_timeout` | тело запроса не пришло за 15 с (JSON) / сдача: связь молчит 60 с или тело дольше 15 мин | «Запрос не дошёл до сервера целиком. Проверьте интернет и повторите.» / «Файлы не дошли до сервера: связь прервалась. Проверьте интернет и отправьте ещё раз.» |
| 413 | `too_large` | частей формы сдачи больше MAX_FILES + 4 текстовых + 2 | «Слишком много полей в форме сдачи.» |
| 429 | `busy` | `UserGate` | §4.2 |
| 429 | `rate_limited` | `RateLimiter` (подсказка AI, поручения) | §4.2 |
| 502 | `telegram_error` | не удалось загрузить файл в Telegram | §8.6 |
| 500 | `internal` | прочее | = `main.GENERIC_ERROR` («⚠️ Произошла ошибка, попробуйте ещё раз») |

Тексты, совпадающие с чатом, задаются константами в `api.py` (без импорта модулей handlers); тест
сверяет их с константами чата.

**Порядок проверок** в каждом обработчике (кроме сдачи — §8.6): 401 (initData) → 403 (доступ и роль:
`active_viewer`/`manager_viewer`/`employee_viewer`) → 404 (объект из пути) → 403 (чужая задача) →
400 (параметры и тело) → 429 (`UserGate`) → сервис (400 `domain`). Поэтому сотрудник на маршруте
начальника получает 403 даже с пустым телом, а `GET /api/me` — единственный маршрут без
`active_viewer`.

**Тексты-пояснения в ответах действий** (`notice`, `null` если нечего сказать):
* уведомление не доставлено (`notify_* → False/None`) — = `common.NOT_DELIVERED`
  («⚠️ Уведомление не доставлено: у сотрудника нет доступа к боту или он заблокировал бота — сообщите ему лично.»);
* поручение не дошло ни до одного начальника — «📥 Поручение #N сохранено, но уведомить начальника
  сейчас не удалось — в боте нет активного начальника. Сообщите начальнику о нём лично.».

### 6.3 База данных

* Одна `AsyncSession` на запрос (middleware). Обработчики не открывают своих сессий; фоновые задачи —
  открывают свою (`ctx.sessionmaker()`), объекты запроса туда не передаются (только id и уже
  загруженные значения).
* Мутации: сервис → `await session.commit()` → уведомления (`bot.notify.*`) → ответ. Так же, как
  чат: «перед уведомлениями других пользователей — commit».
* Перед долгой сетевой работой (AI, загрузка файлов в Telegram) — `await session.commit()`: соединение
  возвращается в пул (их всего 3+1 на экземпляр). Повторное обращение к базе в том же запросе снова
  берёт соединение. Это же — в фоне: оценка сдачи (`_after_submit`) делает commit сразу после чтения
  сдачи, до скачивания файлов и цепочки AI (до 2×AI_TIMEOUT_SEC + 30 с), как чат после `submit_result`.
* Ожидание клиента — тоже «долгая сетевая работа»: JSON-тело читается до открытия сессии (§4.4), тело
  сдачи — после commit и со сроками (§8.6).
* Только жадные связи моделей (`lazy="joined"`/`selectin`): сериализаторы читают лишь то, что уже
  загружено; `session.refresh`/ленивые загрузки в цикле запрещены. Лёгкие чтения списков —
  `bot/webapp/queries.py` (§10): фиксированное число запросов независимо от числа строк.
* Чтения не открывают транзакцию (на PostgreSQL BEGIN отложен до первой записи, `bot.db.base`) —
  не вызывать `text()`/`FOR UPDATE` в чтениях.
* `utcnow` импортировать именем в модуль (`from bot.utils.dates import utcnow`) — тесты подменяют его
  `monkeypatch.setattr(<модуль>, "utcnow", ...)`.

### 6.4 Константы (`bot/webapp/api.py`, тесты их подменяют)

```python
MAX_FILES = 10
MAX_FILE_BYTES = 20 * 1024 * 1024      # = лимит скачивания getFile у Bot API: AI потом сможет прочитать файл
MAX_TOTAL_BYTES = 50 * 1024 * 1024     # бережём 512 МБ памяти и 5 ГБ исходящего трафика Render Free
PHOTO_MAX_BYTES = 10 * 1024 * 1024     # лимит sendPhoto; больше — отправляем документом
MAX_TEXT_PART_BYTES = 64 * 1024        # текстовое поле multipart
PAGE_LIMIT_DEFAULT = 20
PAGE_LIMIT_MAX = 50
SEARCH_SCAN_LIMIT = 2000
MAX_BACK_OFFSET = 500                  # как dashboard.MAX_BACK_OFFSET
HISTORY_PAGE_SIZE = 10                 # как dashboard.HISTORY_PAGE_SIZE
TREND_WEEKS = 8
WEIGHT_OPTIONS = (5, 10, 15, 20, 25, 30, 40, 50)      # как keyboards._WEIGHT_OPTIONS
SCORE_OPTIONS = (50, 70, 80, 90, 100, 110, 120)       # как keyboards._SCORE_OPTIONS
BODY_READ_TIMEOUT_SEC = 15.0           # JSON-тело читается целиком до открытия сессии БД
UPLOAD_IDLE_TIMEOUT_SEC = 60.0         # сдача: столько можно не присылать ни байта
UPLOAD_MAX_SEC = 15 * 60.0             # сдача: всё тело — не дольше
EXTRA_PARTS = 2                        # частей формы сдачи сверх MAX_FILES файлов и текстовых полей
FORMULATE_LIMITS_MANAGER = ((6, 60.0), (60, DAY_SEC))   # RateLimiter: (сколько, за сколько секунд)
FORMULATE_LIMITS_EMPLOYEE = ((6, 60.0), (20, DAY_SEC))
FORMULATE_TEAM_PER_DAY = 200           # подсказок AI на команду в сутки, дальше — правила
PROPOSAL_LIMITS = ((5, 600.0), (20, DAY_SEC))
```

---

## 7. Схемы JSON (`bot/webapp/serializers.py`)

Сериализаторы — чистые функции без обращений к базе. Нотация TypeScript; `Pct` — проценты числом
(как в базе/сервисах, без округления), `*_text` — строка `fmt_pct(...)` («102 %», «—»/«нет данных»).

```ts
type ISO = string                    // "2026-10-05T13:00:00Z"
type TaskStatus = "proposed"|"active"|"submitted"|"rework"|"done"|"cancelled"|"rejected"
type Priority = "high"|"medium"|"low"

UserRef = { id: int, full_name: str, short_name: str, position: str|null,
            role: "manager"|"employee", status: "pending"|"active"|"blocked" }

TaskRow = {                          // строка списка (queries.TaskRowData) — без сдач и файлов
  id: int, title: str,
  status: TaskStatus,
  status_label: str,                 // render.STATUS_LABELS[status], для просроченной открытой — "⏰ Просрочена"
  overdue: bool,                     // status in (active, rework) и deadline < now
  priority: Priority, priority_label: str,      // render.PRIORITY_LABELS («🔴 Высокий»)
  weight: int, source: "manager"|"employee",
  deadline: ISO, deadline_local: str,           // «05.10.2026 18:00»
  tail: str,                         // правая подпись, как в чате (render.task_line), без HTML — правила ниже
  assignee: UserRef,
  accepted: bool,                    // accepted_at is not null
  submitted_at: ISO|null, completed_at: ISO|null,
  final_score: Pct|null, final_score_text: str|null,   // только у done, иначе null
  rework_count: int,
  last_late: bool|null               // is_late последней сдачи (null — сдач нет)
}
```
`TaskRow` строится одинаково из `queries.TaskRowData` (списки) и из ORM `Task` (карточка, ответы
действий; `last_late` — из `task.last_submission`).

Пример элемента `GET /api/tasks`:
```json
{"id":12,"title":"Анализ договоров","status":"active","status_label":"⏰ Просрочена","overdue":true,
 "priority":"high","priority_label":"🔴 Высокий","weight":20,"source":"manager",
 "deadline":"2026-10-05T13:00:00Z","deadline_local":"05.10.2026 18:00","tail":"просрочено на 2 дн.",
 "assignee":{"id":5,"full_name":"Иванов Иван Иванович","short_name":"Иванов И. И.","position":"Юрист",
             "role":"employee","status":"active"},
 "accepted":true,"submitted_at":null,"completed_at":null,"final_score":null,"final_score_text":null,
 "rework_count":0,"last_late":null}
```

`tail` (как `render._line_tail`): done → `fmt_pct(final_score)`; submitted → «сдано dd.mm»
(`submitted_at`, местная дата) или «на проверке»; cancelled → «отменена»; rejected → «отклонена»;
открытая просроченная → «просрочено на N дн.» / «N ч.» / «N мин.» / «меньше минуты» (как
`render._span`); иначе «сегодня до HH:MM» / «завтра до HH:MM» / «до dd.mm» (как `render._due_short`).

```ts
TaskDetail = TaskRow & {             // из ORM Task (submissions загружены)
  expected_result: str, description: str|null,       // description — исходные слова, если отличаются
  plan_value: number|null, plan_unit: str|null,
  plan_text: str|null,               // «100 договоров» (fmt_num + единица), null без плана
  deadline_label: str,               // render.deadline_label(task, now) — «5 октября (вс), 18:00 · осталось 2 дн.»
  created_at: ISO, updated_at: ISO|null, accepted_at: ISO|null, approved_at: ISO|null,
  created_by: UserRef, manager: UserRef|null,
  weight_pending: bool,              // status == proposed: вес и приоритет назначит начальник (не показывать)
  ai_score: Pct|null,                // с учётом правил видимости (ниже)
  attempts: int,                     // число сдач
  rework_comment: str|null,          // комментарий последней сдачи с decision == rework (для формы сдачи)
  actions: Actions
}

Actions = {                          // как keyboards.task_actions_kb для этого зрителя
  accept: bool,      // зритель — исполнитель, status == active, accepted_at is null
  submit: bool,      // зритель — исполнитель, status in (active, rework)
  edit: bool,        // начальник, status in (proposed, active, rework)
  edit_fields: ("title"|"expected_result"|"plan"|"deadline"|"priority"|"weight")[],
                     // proposed: title, expected_result, plan, deadline; active/rework: все шесть; иначе []
  cancel: bool,      // начальник, status in (active, rework)   (у proposed — reject; у submitted — нет, как в чате)
  review: bool,      // начальник, status == submitted и последняя сдача без решения
  review_submission_id: int|null,
  approve: bool, reject: bool        // начальник, status == proposed
}

Attachment = { id: int, kind: "document"|"photo"|"video"|"other",
               name: str,            // file_name или «фото»/«видео»/«документ»/«файл» (render._ATTACHMENT_NAMES)
               mime_type: str|null, size: int|null }

Submission = {
  id: int, attempt: int, created_at: ISO, created_local: str,       // «04.10 18:20»
  fact_text: str, result_text: str|null, fact_value: number|null,
  fact_line: str|null,               // render._fact_value_line без HTML: «🔢 План: 100 договоров → Факт: 110 договоров (110 %)»
  deadline_at_submit: ISO, is_late: bool, late_days: number,
  late_text: str,                    // «в срок» / «с опозданием 1,5 дн.» / «с опозданием»
  attachments: Attachment[],
  ai: { score: Pct, score_text: str, source: "ai"|"rules",
        label: str,                  // «🤖 AI предлагает» | «📐 Расчёт по правилам (AI недоступен)»
        rationale: str|null, model: str|null } | null,
  ai_pending: bool,                  // начальнику: оценки ещё нет, решения нет, задача на проверке
  ai_hidden: bool,                   // сотруднику: оценка скрыта до решения начальника
  decision: "approved"|"changed"|"rework"|null,
  decision_label: str|null,          // «✅ подтверждена» | «✏️ изменена начальником» | «↩️ Возвращено на доработку»
  final_score: Pct|null, final_score_text: str|null,
  review_comment: str|null, reviewer: UserRef|null, reviewed_at: ISO|null
}

Event = { id: int, type: str,        // EventType: created|proposed|approved|rejected|accepted|edited|submitted|
                                     //            ai_evaluated|score_confirmed|score_changed|rework|cancelled|reminder
          at: ISO, at_local: str,    // «04.10 18:20»
          actor: UserRef|null, actor_name: str,   // short_name или «🤖 Бот»
          text: str }                // html.unescape(render._event_phrase(event)) — фраза журнала чата без HTML

TaskCard = { task: TaskDetail, submissions: Submission[] /* по attempt, старые первыми */,
             events: Event[] /* по времени */, viewer: "manager"|"assignee" }
```

**Правила видимости оценки AI (как в чате, обязательны).** Зритель-начальник (`viewer.is_manager`)
видит всё. Зритель-исполнитель:
* у сдачи без решения (`decision is null`): `ai = null`, `ai_hidden = true`, `ai_pending = false`;
* у сдачи с решением: `ai = {score, score_text, source, label}` и `rationale = null`, `model = null`
  (обоснование AI сотруднику не показывается никогда — как `render._submission_lines(show_ai=False)`);
* `TaskDetail.ai_score` = `null`, пока последняя сдача без решения;
* события: исключаются `ai_evaluated` после последнего `submitted`, если последняя сдача без решения
  (как `task_view._visible_events`).

**KPI**
```ts
Period = { kind: "week"|"month"|"quarter"|"year", offset: int, label: str, short: str,
           start: ISO, end: ISO, has_prev: bool /* offset > -500 */, has_next: bool /* offset < 0 */ }

KpiStats = { total, done, done_on_time, done_late, overdue_open, overdue_total, on_review,
             on_review_late, in_progress, overperformed, self_initiated: int,
             avg_score: Pct|null, on_time_pct: Pct|null }
KpiItem  = { task_id: int, title: str, weight: int, score: Pct, zero_overdue: bool }
KpiBlock = { kpi: Pct|null, kpi_text: str /* «102 %» | «нет данных» */, stats: KpiStats, items: KpiItem[] }
KpiShort = { kpi: Pct|null, kpi_text: str }
Trend    = { weeks: 8, points: { start: ISO, label: str /* «28.09» — местный понедельник */, kpi: Pct|null }[] }
                                     // от старой недели к текущей (offset -7 … 0)

HistoryItem = { task_id: int, title: str, weight: int, completed_at: ISO|null, completed_local: str|null /* «04.10» */,
                ai_score: Pct|null,  // task.ai_score или ai_score последней сдачи
                final_score: Pct|null, final_score_text: str,
                decision: "approved"|"changed"|null, is_late: bool|null, rework_count: int }
```

---

## 8. Эндпоинты

### 8.1 Сводная таблица и соответствие чату

Роли: **M** — активный начальник, **E** — активный сотрудник, **A** — исполнитель задачи.
«RT» — потолок обменов с базой, которые ждёт пользователь (§12.5).

| Метод и путь | Кто | Сервис | Уведомление (как в чате) | RT |
|---|---|---|---|---|
| `GET /api/me` | все с валидным initData | `queries.me_counts` | — | 2 |
| `POST /api/lang` | все с валидным initData | `users.set_lang` (§8.11) | — | 2 |
| `GET /api/tasks` | M (my/all/emp), E (my) | `queries.list_task_rows` / `search_task_rows` / `tab_counts` | — | 3 (+1 counts) |
| `GET /api/tasks/{id}` | M; E — только свои | `tasks.get_task`, `tasks.task_events` | — | 5 |
| `POST /api/ai/formulate` | M, E | `formulate.suggest_expected_result` | — | 1 |
| `POST /api/voice` | M, E | `dictate.transcribe` / `dictate.dictate_task` (§8.10) | — | 2 |
| `POST /api/tasks` | M | `tasks.create_task` | `notify_new_task` | 6 |
| `PATCH /api/tasks/{id}` | M | `tasks.update_task` | `notify_task_changed` (если есть изменения) | — |
| `POST /api/tasks/{id}/accept` | A | `tasks.accept_task` | — (как в чате) | — |
| `POST /api/tasks/{id}/cancel` | M | `tasks.cancel_task` | `notify_task_cancelled` | — |
| `POST /api/tasks/{id}/submit` | A | `tasks.submit_result` → фон `submission_flow.run_after_submit` | фон: `notify_submission` | — |
| `GET /api/review` | M | `tasks.list_for_review` | — | 4 |
| `POST /api/submissions/{id}/confirm` | M | `tasks.review_confirm` | `notify_review_result` | 9 |
| `POST /api/submissions/{id}/score` | M | `tasks.review_set_score` | `notify_review_result` | — |
| `POST /api/submissions/{id}/revise` | M | `tasks.review_revise_auto` (оценка подтверждена автоматически, не позже `AUTO_REVISE_DAYS`) | `notify_review_result` | — |
| `POST /api/submissions/{id}/rework` | M | `tasks.review_rework` | `notify_rework` | — |
| `POST /api/submissions/{id}/files` | M | — | фон: `send_attachments` в чат начальника | — |
| `GET /api/proposals` | M | `tasks.list_proposals` | — | 3 |
| `POST /api/proposals` | E | `tasks.propose_task` → `proposal_flow.run_after_propose` (вес от AI, SPEC §12.2) | `notify_proposal` (всем начальникам) | — |
| `POST /api/tasks/{id}/approve` | M | `tasks.approve_proposal` | `notify_proposal_decision(True)` | — |
| `POST /api/tasks/{id}/reject` | M | `tasks.reject_proposal` | `notify_proposal_decision(False, reason)` | — |
| `GET /api/dashboard` | M (команда), E (сам) | `queries.snapshots_between` + `kpi.compute_kpi` | — | M 3, E 2 |
| `GET /api/users/{id}/kpi` | M (любой), E (только сам) | то же + `queries.history_rows`, `tasks.count_tasks` | — | 5 |
| `GET /api/employees` | M | `users.list_employees` | — | 2 |
| `GET /api/employees/{id}/weight-load` | M | `tasks.weight_load` | — | 2 |
| `POST /api/export` | M | фон `export.build_report_xlsx` | фон: документ в чат начальника | — |

Соответствие чату: создание — `task_create.on_confirm`; правка — `task_view._apply_edit` /
`task_propose._apply_edit`; отмена — `task_view._do_cancel`; принятие — `task_view.accept_task`;
сдача — `task_submit._send_result`; проверка — `task_review.confirm_score` / `_finish_change` /
`_finish_rework` / `send_files`; поручения — `task_propose.propose_confirm` / `proposal_priority_pick` /
`_do_reject`; KPI — `dashboard._team_screen` / `_card_screen` / `employee_history`; экспорт —
`dashboard.export_report`.

**Автоподтверждение** (SPEC.md §12.2) в схемах: `TaskDetail.auto_note` — строка для начальника (когда
оценка / поручение будут приняты без него или почему оценка его ждёт; `null` — сказать нечего);
`actions.revise`, `actions.revise_submission_id`, `actions.revise_until_text` — оценку подтвердил бот и
начальник ещё может её изменить; `Submission.auto_confirmed`, а `decision_label` у такой сдачи —
«⏱ подтверждена автоматически». Экраны: строка `auto_note` на проверке сдачи, в карточке задачи и на
экране поручения (там предложенный вес выбран заранее, подпись «💡 Предлагаемый вес: N %»); в карточке
выполненной задачи — кнопка «✏️ Изменить оценку (до …)» → тот же лист оценки → `POST …/revise`.

### 8.2 `ROUTES`

`bot/webapp/api.py` экспортирует `ROUTES: list[tuple[str, str]]` — все маршруты API (метод, шаблон
полного пути), например `("GET", "/api/tasks/{task_id}")`. Таблица маршрутов строится из него;
тест UI сверяет с ним пути в `app.js` (§12.4). Имена параметров: `task_id`, `sub_id`, `user_id`.

### 8.3 `GET /api/me`

Доступен любому с валидным initData (в т.ч. незарегистрированному и неактивному).
```ts
Me = {
  access: "active"|"pending"|"blocked"|"unregistered",
  message: str|null,                 // для неактивных — текст из §6.2 (not_registered / pending / blocked)
  user: UserRef & { tg_id: int } | null,
  role: "manager"|"employee"|null,   // null у неактивных
  now: ISO, today: str,              // местная дата «2026-10-07»
  config: {                          // у неактивных — тоже
    max_score: int, timezone: str, default_deadline_time: str /* «18:00» */,
    ai_enabled: bool, period_kinds: ["week","month","quarter","year"],
    max_files: 10, max_file_mb: 20, max_total_mb: 50,
    weight_options: int[], score_options: int[], history_page_size: 10, trend_weeks: 8
  },
  deadline_options: { label: str /* «Завтра, 08.10» */, date: str /* «2026-10-08» */ }[],
                                     // dateparse.quick_deadline_options()
  counts: ManagerCounts | EmployeeCounts | null     // null у неактивных
}
ManagerCounts  = { review, proposals, open, overdue, pending_users: int }
EmployeeCounts = { open, overdue, unaccepted, rework, review, proposed: int }
```
`counts` — один запрос (`queries.me_counts`, условные суммы; у начальника `pending_users` —
скалярный подзапрос: PENDING с непустым ФИО). Неактивному — 1 запрос (только пользователь).

### 8.4 Задачи

**`GET /api/tasks`** — query: `scope` = `my|all|emp` (по умолчанию M → `all`, E → `my`);
`status` = `open|overdue|review|done|proposed|all` (по умолчанию `open`); `user_id` (обязателен при
`scope=emp`); `q`; `page` (≥ 0, по умолчанию 0); `limit` (1..50, по умолчанию 20); `counts` (`1` —
добавить счётчики вкладок).
* E со `scope=all|emp` → 403 `forbidden`. Неизвестные значения → 400 `bad_request`.
* Статусы (как `task_view._status_filter` + `proposed`): open → active, rework; overdue → active, rework
  и `deadline < now`; review → submitted; done → done; proposed → proposed; all → M: proposed, active,
  rework, submitted, done, cancelled; E: те же без cancelled. Rejected не показываются нигде, кроме
  карточки.
* Порядок — как `tasks.list_tasks` (незавершённые по сроку, ближайшие сверху; done по `completed_at`
  убыв.; затем id).
* Поиск `q` (после обрезки; короче 2 символов и не `#число` — как пустой): `queries.search_task_rows` —
  до `SEARCH_SCAN_LIMIT` строк выборки (те же фильтры scope/status, тот же порядок) одним запросом,
  фильтр в Python по `casefold()` с `ё→е` и схлопнутыми пробелами в `title`, `expected_result`,
  `assignee.full_name`; `#12` или `12` — ещё и точное совпадение id. Пагинация — в Python.
  Причина: `lower()` в SQLite не понимает кириллицу, а в PostgreSQL зависит от локали базы.
* Ответ:
  ```ts
  { items: TaskRow[], total: int, page: int, pages: int /* max(1, ceil(total/limit)) */, limit: int,
    truncated: bool,                 // поиск упёрся в SEARCH_SCAN_LIMIT
    counts?: { open, overdue, review, done, proposed, all: int } }   // при counts=1: тот же scope,
                                                                       // без учёта q (бейджи вкладок)
  ```
  Страница за пределами — пустой `items` (без «прижимания»).

**`GET /api/tasks/{task_id}`** → `TaskCard`. Задачи нет → 404. E не исполнитель → 403 `forbidden`.
Запросы: `tasks.get_task` (задача с людьми + сдачи + файлы) и `tasks.task_events`.

**`POST /api/tasks`** (M). Тело:
```ts
{ assignee_id: int, title: str, expected_result: str, description?: str|null,
  plan_value?: number|str|null, plan_unit?: str|null, deadline: DeadlineInput,
  priority?: Priority /* "medium" */, weight: int }
```
`plan_unit` без `plan_value` отбрасывается (как `task_create._clean_plan`). `gate("create")` →
`tasks.create_task(session, creator=viewer, …)` → commit → `delivered = await notify.notify_new_task(bot, task)`
→ **201** `{ task: TaskDetail, delivered: bool, notice: str|null }`.

**`PATCH /api/tasks/{task_id}`** (M). Тело — непустое подмножество `{title, expected_result,
description, plan_value, plan_unit, deadline, priority, weight}`; пустое → 400 «Нет изменений».
* `plan_value: null` очищает план и единицу (`plan_unit=None`).
* Задача `proposed` и в теле `priority`/`weight` → 400 `domain` «Вес и приоритет назначаются при
  подтверждении поручения» (как в чате: у предложения правятся только название, результат, план, срок).
* `task, changes = await tasks.update_task(session, task_id, viewer, **fields)` → commit →
  `delivered = await notify.notify_task_changed(bot, task, changes) if changes else None` →
  200 `{ task: TaskDetail, changed: str[] /* имена полей сервиса */, delivered: bool|null, notice: str|null }`.

**`POST /api/tasks/{task_id}/accept`** (A; иначе 403) → `tasks.accept_task` → commit → 200
`{ task: TaskDetail }`. Уведомлений нет (как в чате). Принять можно одновременно из чата и приложения:
сервис пишет отметку условным UPDATE (`accepted_at IS NULL` и прежний статус) — событие ACCEPTED одно.

**`POST /api/tasks/{task_id}/cancel`** (M). Тело `{ reason?: str|null }` → `tasks.cancel_task` →
commit → `notify_task_cancelled(bot, task, reason)` → 200 `{ task, delivered, notice }`.

**`POST /api/ai/formulate`** (M, E). Тело `{ title: str, raw_result: str, previous?: str|null /* ≤ 1000 */ }`.
1. `gate("formulate")`; лимит частоты `formulate` (§4.2; превышен — 429 `rate_limited`, к AI не
   обращаемся); `await session.commit()` (отпустить соединение перед AI). Суточный лимит команды
   `formulate_team` исчерпан — сразу `rules_suggestion(title, raw)` с `notice` §4.2 (шаги 2–3 пропускаются).
2. `raw_for_ai = raw` или, если `previous`, `f"{raw}\n\nПредыдущий вариант: {previous}. Предложи другую формулировку."`
   (как `task_create`).
3. `s = await asyncio.wait_for(formulate.suggest_expected_result(title, raw_for_ai, deadline_text=None),
   timeout=provider.chain_budget_sec(settings, "formulate") + 10)`; `TimeoutError`/любое исключение →
   `formulate.rules_suggestion(title, raw)` (+ `log.exception`).
4. Если `previous` и `s.source != "ai"` → `s = rules_suggestion(title, raw)`, `notice` = «⚠️ AI сейчас
   недоступен — другой вариант предложить не получилось. Отредактируйте формулировку сами.»
5. 200 `{ expected_result: str, plan_value: number|null, plan_unit: str|null, note: str|null,
   source: "ai"|"rules", notice: str|null }`.

### 8.5 Поручения сотрудников

**`GET /api/proposals`** (M) → `tasks.list_proposals` (старые сверху) →
`{ items: { task: TaskDetail }[] }`.

**`POST /api/proposals`** (E; M → 403). Тело `{ title, expected_result, description?, plan_value?,
plan_unit?, deadline }`. `gate("propose")` → лимит частоты `propose` (§4.2; 429 `rate_limited`; отказ
сервиса — не в счёт) → `tasks.propose_task(session, employee=viewer, …)` → commit →
`notified = await notify.notify_proposal(bot, session, task)` → **201**
`{ task: TaskDetail, notified: int, notice: str|null }` (`notified == 0` → текст §6.2).

**`POST /api/tasks/{task_id}/approve`** (M). Тело `{ weight: int, priority?: Priority }` →
`tasks.approve_proposal(session, task_id, viewer, weight=…, priority=…)` → commit →
`notify_proposal_decision(bot, task, True)` → 200 `{ task, delivered, notice }`.
Срок прошёл → сервис: «Срок поручения уже прошёл — сначала измените срок» (400 `domain`).

**`POST /api/tasks/{task_id}/reject`** (M). Тело `{ reason?: str|null }` → `tasks.reject_proposal` →
commit → `notify_proposal_decision(bot, task, False, reason)` → 200 `{ task, delivered, notice }`.

Правка предложения — `PATCH /api/tasks/{id}` (§8.4).

### 8.6 Сдача результата

**`POST /api/tasks/{task_id}/submit`** (A), `multipart/form-data` (иное → 400 `bad_request`
«Ожидается multipart/form-data»). Поля (порядок любой; SPA шлёт сначала текстовые):

| Имя | Тип | Правило |
|---|---|---|
| `fact_text` | текст | обязательно, 3..3000 («Опишите чуть подробнее, пожалуйста.» / «Слишком длинно…») |
| `result_text` | текст | ≤ 3000 |
| `fact_value` | текст | число ≥ 0 через `parse_number` («110», «110,5», «1 200»); пусто — нет |
| `materials_text` | текст | ≤ 1500 — где лежат материалы (ссылка и т. п.) |
| `files` (или `files[]`) | файл, повторяется | 0..10; каждый 1 байт..20 МБ; всего ≤ 50 МБ; пустой → 400 «Пустой файл «<имя>»» |

Неизвестное имя поля → 400. Текстовое поле > `MAX_TEXT_PART_BYTES` → 400.

Порядок обработки (строго):
1. Права и состояние до чтения тела: `task = tasks.get_task` (нет → 404); `task.assignee_id != viewer.id`
   → 403; `not task.is_open` → 400 `domain` с текстом как `task_submit._not_open_text` (submitted →
   «📝 Результат уже отправлен и ждёт проверки начальника.» и т. д.).
2. `gate("submit")`; `await session.commit()` — соединение свободно на время загрузки.
3. Чтение `await request.multipart()` по частям: текст — в память (с лимитом), файлы — потоком во
   временные файлы (`tempfile.NamedTemporaryFile(delete=False, prefix="kpi_upload_")`), считая байты;
   превышение лимитов §6.4 → 413 сразу, временные файлы удаляются. Ничего не отправляется в Telegram,
   пока всё тело не прочитано и не проверено. Частей формы не больше `MAX_FILES + 4 + EXTRA_PARTS`
   (иначе 413 «Слишком много полей в форме сдачи.»); пустое поле выбора файла (`filename=""` без
   содержимого) пропускается, не создавая временного файла. Сроки: связь молчит дольше
   `UPLOAD_IDLE_TIMEOUT_SEC` (60 с) или всё тело дольше `UPLOAD_MAX_SEC` (15 мин) → 408
   `request_timeout`, временные файлы удаляются, `gate("submit")` снимается.
4. Имя файла (`part.filename` как есть — браузер шлёт его в UTF-8): только базовое имя (после
   последнего `/` или `\`), без управляющих символов, пробелы схлопнуты, ≤ 255 символов с сохранением
   расширения; пустое → «file». (Проверено на aiohttp 3.14: `request.multipart()` читает файлы
   потоком и не ограничен `client_max_size` 1 МиБ. В тестах отправлять как браузер —
   `aiohttp.FormData(quote_fields=False)`, иначе клиент aiohttp закодирует кириллицу в имени как `%D0%…`.)
5. Загрузка в Telegram по одному, в **личный чат сотрудника с ботом** (`chat_id = viewer.tg_id`),
   подпись `f"📎 К задаче #{task.id}"`, `disable_notification=True`:
   * изображение (`image/jpeg|png|webp` по типу из запроса или расширению) ≤ `PHOTO_MAX_BYTES` →
     `bot.send_photo(chat_id, FSInputFile(path, filename=name), caption=…)` →
     `AttachmentIn(kind=PHOTO, file_id=msg.photo[-1].file_id, file_unique_id=…, file_name=name,
     mime_type="image/jpeg", file_size=msg.photo[-1].file_size)`; `TelegramBadRequest` (размеры фото) —
     один повтор документом;
   * остальное → `bot.send_document(…, disable_content_type_detection=True)` → из ответа
     `document` → `DOCUMENT`; если Telegram всё же вернул `video` → `VIDEO`, `animation`/`audio`/`voice`
     → `OTHER`; `file_name=name`, `mime_type` из ответа (или из запроса), `file_size` из ответа.
   * `TelegramRetryAfter` ≤ 60 с — подождать и повторить один раз (как `notify._call`); больше → ошибка.
   * `TelegramForbiddenError` → 502 `telegram_error` «Не удалось передать файлы в чат с ботом: бот
     заблокирован или чат удалён. Откройте чат с ботом, нажмите «Перезапустить» и повторите.»;
     прочие `TelegramAPIError`/`OSError` → 502 «Telegram не принял файл «<имя>». Попробуйте ещё раз
     или отправьте файл через чат.» Сдача при этом не создаётся (уже загруженные файлы остаются в чате).
   * Временные файлы удаляются в `finally` всегда.
6. `sub = await tasks.submit_result(session, task_id, viewer, fact_text=…,
   result_text=submission_flow.result_with_notes(result_text, [materials_text] if materials_text else []),
   fact_value=…, attachments=[…])` → `await session.commit()`. Перед вызовом задача и её сдачи
   перечитываются (`populate_existing`): пока грузились файлы, задачу могли сдать из чата и вернуть на
   доработку — переход статуса и номер попытки считаются по свежим данным. `DomainError` (задачу
   отменили, пока грузились файлы) → 400 `domain`.
7. Фон: `ctx.tasks.spawn(_after_submit(ctx, task.id, sub.id), name=f"webapp-submit-{sub.id}")`:
   ```python
   async def _after_submit(ctx, task_id, sub_id):
       from bot.services import submission_flow
       async with ctx.sessionmaker() as session:
           sub = await tasks.get_submission(session, sub_id)
           await session.commit()   # соединение — в пул до скачивания файлов и цепочки AI (как чат)
           if sub is not None:
               await submission_flow.run_after_submit(ctx.bot, session, sub.task, sub)
   ```
   Остановка сервера посреди оценки — сдачу доведёт `jobs.recover_stalled_evaluations` (как в чате).
8. **202** `{ task_id: int, submission_id: int, attempt: int, files: int, status: "submitted",
   evaluation: "pending" }`. Сотруднику в чат отдельное сообщение не шлётся (его файлы уже там с
   подписью «📎 К задаче #N»); решение начальника придёт в чат как обычно.

### 8.7 Проверка результатов

**`GET /api/review`** (M) → `tasks.list_for_review` (по `submitted_at`, давние сверху) →
`{ items: { task: TaskDetail, submission: Submission /* последняя, вид начальника */ }[] }`.

Общее для `/api/submissions/{sub_id}/…` (M): `sub = await tasks.get_submission(session, sub_id)`;
нет → 404 «Результат не найден.». Остальные проверки («Результат уже обработан», «Нельзя оценивать
результат собственной задачи», нет оценки AI) — в сервисах (400 `domain`).

* **`POST …/confirm`** (без тела) → `tasks.review_confirm(session, sub.id, viewer)` → commit →
  `delivered = notify_review_result(bot, task, sub)` → 200 `{ task: TaskDetail, delivered, notice }`.
* **`POST …/score`** `{ score: number, comment?: str|null }` → `tasks.review_set_score` → commit →
  `notify_review_result` → 200 `{ task, delivered, notice }`.
* **`POST …/rework`** `{ comment: str, deadline?: DeadlineInput|null }`:
  `deadline` нет и `sub.task.deadline <= now` → 400 `domain` «Текущий срок уже прошёл — укажите новый
  срок.» (правило чата `task_review._finish_rework`); → `tasks.review_rework(session, sub.id, viewer,
  comment, new_deadline)` → commit → `notify_rework(bot, task, sub)` → 200 `{ task, delivered, notice }`.
* **`POST …/files`** (без тела): у сдачи нет файлов → 400 `domain` «У этого результата нет файлов.»;
  `gate("files")` (освобождается в конце фоновой задачи); `await session.commit()`;
  `spawn(notify.send_attachments(bot, viewer.tg_id, sub))` → **202** `{ count: int }`.
  (`sub` и его `attachments` уже загружены; к базе фон не обращается.)

### 8.8 KPI и отчёты

**`GET /api/dashboard?kind=week|month|quarter|year&offset=n`** — `kind` по умолчанию `week`
(неизвестный → 400), `offset` — целое, `> 0` → 0, `< -500` → -500 (как `dashboard._normalize_period`).
`period = periods.get_period(kind, offset, now)`; `trend` — недели `offset -7 … 0` от текущей.

Начальник:
```ts
{ scope: "team", period: Period, team: KpiShort,
  totals: { employees, total, done, overdue_total, in_progress, on_review: int },
  rows: { user: UserRef, kpi: Pct|null, kpi_text: str, stats: KpiStats }[],
  trend: Trend }               // точка недели — KPI команды за неделю
```
Сотрудник (свой):
```ts
{ scope: "self", period: Period, user: UserRef, current: KpiBlock,
  week: KpiShort, month: KpiShort /* текущие неделя и месяц, как employee_card */, trend: Trend }
```
Расчёт (`queries.snapshots_between` + `kpi.compute_kpi`, §10.3) обязан давать **те же числа и тот
же порядок строк**, что `kpi.kpi_for_team` / `kpi.kpi_for_user` / `kpi.team_kpi`.

**`GET /api/users/{user_id}/kpi?kind&offset&page`** — M: любой пользователь (как карточка в чате, в т.ч.
заблокированный); E: только `user_id == viewer.id`, иначе 403. Нет пользователя → 404 «Сотрудник не
найден.». Ответ — те же поля, что у дашборда сотрудника выше, но `scope: "user"` и `user` —
запрошенный пользователь, плюс
`history: { items: HistoryItem[], page: int, pages: int, total: int, page_size: 10 }`
(`page` ≥ 0, по 10, `completed_at` убыв., затем id убыв., как `tasks.evaluated_history`).

**`POST /api/export?kind=…&offset=…`** (M). Параметры как у дашборда. `gate("export")` (держится до
конца фона) → `period` → `spawn(_export_job(...))` → **202**
`{ status: "started", period: Period, message: "📤 Отчёт «<label>» придёт в чат с ботом через несколько секунд." }`.
Фон: своя сессия → `export.build_report_xlsx(session, period, now)` →
`bot.send_document(viewer.tg_id, BufferedInputFile(data, filename=f"kpi_{kind}_{to_local(period.start):%Y%m%d}.xlsx"),
caption=f"📊 Отчёт: {esc(period.label)}")` (как `dashboard.export_report`); сбой сборки →
`notify.safe_send(bot, chat, "⚠️ Не удалось подготовить отчёт. Попробуйте ещё раз чуть позже.")`;
сбой отправки → `"⚠️ Не удалось отправить файл. Попробуйте ещё раз."` (тексты `dashboard.EXPORT_FAILED`,
`EXPORT_SEND_FAILED`).

### 8.9 Сотрудники и загрузка недели

**`GET /api/employees`** (M) → `users.list_employees` →
`{ items: UserRef[] }` (активные сотрудники по ФИО).

**`GET /api/employees/{user_id}/weight-load?deadline=<DeadlineInput>&exclude_task_id=<id>`** (M) →
`load = tasks.weight_load(session, user_id, deadline, exclude_task_id)` →
`{ load: int, week_label: str /* «Неделя 05.10–11.10.2026» — местные пн–вс недели срока */,
   options: { weight: int, over: bool /* load + weight > 100 */ }[] /* WEIGHT_OPTIONS */ }`.
Сотрудника нет → 404. `exclude_task_id` — при подтверждении поручения (как в чате).

---

### 8.10 Голосовой ввод: `POST /api/voice`

Диктовка в формах (SPEC.md §13). Тело запроса — сама запись (не JSON и не форма), `Content-Type` — тип
записи от `MediaRecorder`: `audio/webm;codecs=opus` (Android, компьютер), `audio/mp4` (iPhone),
`audio/ogg`. Размер — до `VOICE_MAX_BYTES` (6 МБ), чтение — не дольше `VOICE_READ_TIMEOUT_SEC` (60 с);
обработчик читает тело сам (`_STREAMING_HANDLERS`), соединение с базой на это время и на ответ AI отпущено.

* без параметров — `{"text": "распознанный текст"}` (поле формы);
* `?mode=task` — задача целиком: `{"text": …, "task": {assignee_id, title, expected_result, plan_value,
  plan_unit, deadline_date, deadline_time, source}}`; начальнику исполнитель подбирается из активных
  сотрудников, у сотрудника `assignee_id` всегда `null`; чего в записи не было — `null`. Срок — местные
  дата `YYYY-MM-DD` и время `HH:MM`, как их вводит форма (§11.8).

Ошибки: `400` — пустое тело или неизвестный `mode`; `413 too_large` — запись больше лимита; `422
voice_failed` — речь не распознана (текст — `dictate.VoiceError.message`: «не удалось разобрать речь»,
«сейчас не получилось…», «голос сейчас не распознаётся…»); `429 rate_limited` — чаще `VOICE_LIMITS`
(12 в минуту, 200 в сутки на человека). После `VOICE_TEAM_PER_DAY` (800) записей команды за сутки —
`422` «напишите текстом»: квоты бесплатного AI остаются оценке сдач. Одновременно у человека распознаётся
одна запись (`BUSY["voice"]`). `GET /api/me`: `config.voice_enabled` (голос включён и AI доступен) и
`config.voice_max_sec`.

### 8.11 Язык: `POST /api/lang` и язык ответов

Язык запроса (SPEC.md §14): `users.lang`; не выбран — `language_code` из initData (узбекский запоминается
у пользователя сразу). `error_middleware` сбрасывает язык в начале каждого запроса (соединение общее),
`auth_session_middleware` ставит его после входа.

* `GET /api/me` — поля `lang` (`"ru"` | `"uz"`) и `langs` (`[{code, name}]`).
* `POST /api/lang` `{"lang": "ru" | "uz"}` → `{"lang": …}`; доступен всем с валидным initData (и до
  подтверждения заявки); меняет язык и в чате. Другое значение или лишние поля — `400`.
* Подписи, которые строит сервер, приходят на языке пользователя: `status_label`, `priority_label`,
  `deadline_label`, `deadline_local`, `tail`, `auto_note`, `late_text`, `fact_line`, `decision_label`,
  `ai.label`, обоснование расчёта по правилам, `events[].text`, `actor_name` бота, `period.label/short`,
  `kpi_text`, `week_label`, `deadline_options[].label`, `message`, `notice`. Слова пользователя (`title`,
  ФИО, тексты сдачи, комментарии) и обоснование AI не переводятся. `revise_until_text` остаётся
  «03.10 в 12:00» — приложение вставляет его в свою фразу.
* Ошибки: `error` — на языке пользователя; если текст переведён, рядом `ru` — тот же текст по-русски.
* Невидимые метки слов пользователя в JSON не попадают (`_dumps`).

## 9. Общий конвейер сдачи (`bot/services/submission_flow.py`, CORE)

Вынести из `bot/handlers/task_submit.py` всё, что идёт **после** `submit_result` + commit, чтобы чат
и приложение делали одно и то же. Модуль не знает про FSM и сообщения сотруднику.

```python
RULES_PREFIX = "Расчёт по правилам (AI недоступен): "
AI_BUDGET_MARGIN_SEC = 5

@dataclass(frozen=True)
class FlowResult:
    task_id: int
    submission_id: int
    status: TaskStatus | None   # свежий статус задачи после оценки; None — перечитать не удалось
    source: str | None          # "ai" | "rules" — чем оценено; None — оценку записать не удалось
    notified: bool              # notify_submission вызван (сдача ещё ждала решения)

def result_with_notes(result: str | None, notes: Sequence[str]) -> str | None
    # = task_submit._result_with_notes: «результат» + "\n\nПодтверждающие материалы: " + "; ".join(notes)

def default_budget_sec() -> float              # ai_evaluate.evaluation_budget_sec()

async def evaluate(bot: Bot, task: Task, sub: Submission, *, budget_sec: float,
                   use_ai: bool) -> Evaluation | None
    # = task_submit._ai_evaluation: файлы скачиваются только при use_ai; time_budget для AI — остаток
    # после скачивания минус AI_BUDGET_MARGIN_SEC; общий срок budget_sec; никогда не бросает (None)

async def evaluate_and_record(bot: Bot, session: AsyncSession, task: Task, sub: Submission, *,
                              budget_sec: float, use_ai: bool) -> tuple[Task, Submission]
    # = task_submit._evaluate: оценка AI -> tasks_svc.record_evaluation -> commit; ошибка записи ->
    # reload + правила; AI нет/упал -> rules_score с RULES_PREFIX, source="rules" -> commit

async def reload(session: AsyncSession, task_id: int, sub_id: int) -> tuple[Task, Submission]   # = _reload
async def fresh(session: AsyncSession, task_id: int, sub_id: int,
                fallback: tuple[Task, Submission]) -> tuple[Task, Submission]              # = _fresh
def awaits_review(task: Task, sub: Submission) -> bool                                       # = _awaits_review

async def run_after_submit(bot: Bot, session: AsyncSession, task: Task, sub: Submission, *,
                           budget_sec: float | None = None, use_ai: bool | None = None) -> FlowResult
    # budget_sec None -> default_budget_sec(); use_ai None -> provider.ai_available().
    # evaluate_and_record (исключение -> log.exception + reload; и он упал -> current=None) ->
    # fresh -> awaits_review ? notify.notify_submission(bot, session, task, sub) : лог «уже не ждёт проверки».
    # Никогда не бросает Exception (CancelledError пробрасывает). Логи — те же тексты, что сейчас в task_submit.
```

**Обязательная совместимость** (на это опираются существующие тесты):
* сервисы и уведомления вызывать **через атрибут модуля**: `from bot.services import tasks as tasks_svc`
  → `tasks_svc.record_evaluation(...)`, `from bot import notify` → `notify.notify_submission(...)`,
  `from bot.ai import evaluate as ai_evaluate, evidence as ai_evidence` — тесты подменяют
  `tasks_svc.record_evaluation`, `evaluate_module.generate_json`;
* `bot/handlers/task_submit.py` сохраняет `_ai_budget_sec()` (тест подменяет его) и `ai_available`,
  импортированный в модуль (тест подменяет `task_submit.ai_available`), и передаёт их явно:
  `result = await submission_flow.run_after_submit(bot, session, task, sub, budget_sec=_ai_budget_sec(),
  use_ai=ai_available())`; итоговый текст сотруднику выбирается по `result.status` (CANCELLED / DONE /
  REWORK / иначе), как сейчас;
* `task_submit.RULES_PREFIX` и `_result_with_notes` остаются именами (как алиасы на модуль конвейера);
* поведение чата (тексты, «печатает…», порядок сообщений, обработка гонок), все тесты
  `tests/e2e/test_submit_review.py`, `tests/test_ai_chain.py`, `tests/e2e/test_security.py` и бюджеты
  `tests/perf` — без изменений.

`jobs.recover_stalled_evaluations` не меняется.

---

## 10. Лёгкие чтения (`bot/webapp/queries.py`, API)

Каждая функция — **один** SQL-запрос (столбцы, без загрузки ORM-связей), если не сказано иное.
Фильтры и порядок — копия выражений из сервисов (сервисы не меняются).

### 10.1 Строки задач

```python
@dataclass(frozen=True)
class TaskRowData:
    id: int; title: str; status: TaskStatus; priority: Priority; weight: int; source: TaskSource
    deadline: datetime; accepted_at: datetime | None; submitted_at: datetime | None
    completed_at: datetime | None; final_score: float | None; rework_count: int
    last_late: bool | None                         # подзапрос: is_late последней сдачи (order by id desc limit 1)
    assignee_id: int; assignee_full_name: str; assignee_position: str | None
    assignee_role: Role; assignee_status: UserStatus
    expected_result: str | None = None             # только в search_task_rows (для совпадений)

def status_filter(status: str, viewer_is_manager: bool) -> tuple[tuple[TaskStatus, ...], bool]   # §8.4
async def list_task_rows(session, *, assignee_id: int | None, statuses, overdue_only: bool, now,
                         limit: int, offset: int) -> list[TaskRowData]
async def count_task_rows(session, *, assignee_id, statuses, overdue_only, now) -> int
async def search_task_rows(session, q: str, *, assignee_id, statuses, overdue_only, now,
                           scan_limit: int = SEARCH_SCAN_LIMIT) -> tuple[list[TaskRowData], bool]
    # (совпавшие в порядке списка, truncated)
async def tab_counts(session, *, assignee_id: int | None, viewer_is_manager: bool, now) -> dict[str, int]
async def me_counts(session, viewer: User, now) -> dict[str, int]
```
`list_task_rows` — `select(<столбцы Task>, <столбцы User>, last_late).join(User, User.id == Task.assignee_id)`,
фильтры и `ORDER BY` как `tasks.list_tasks` (`case(done→1)`, `case(not done → deadline).nulls_first()`,
`completed_at desc nulls last`, `id`), `offset/limit` через `dbsafe.non_negative/sql_limit`.
Тест: порядок id совпадает с `tasks.list_tasks` для тех же фильтров.

### 10.2 История оценок

```python
async def history_rows(session, assignee_id: int, *, limit: int, offset: int) -> list[HistoryRowData]
    # DONE исполнителя: id, title, weight, completed_at, ai_score (coalesce task.ai_score, последней сдачи),
    # final_score, rework_count, decision и is_late последней сдачи (коррелированные подзапросы);
    # порядок как tasks.evaluated_history. Итог — tasks.count_tasks(assignee_id=…, statuses=[DONE]).
```

### 10.3 KPI одним запросом

```python
async def snapshots_between(session, start: datetime, end: datetime,
                            assignee_ids: Sequence[int]) -> list[tuple[int, TaskSnapshot]]
    # копия kpi._period_snapshots для произвольного [start, end): deadline в диапазоне, status не в
    # EXCLUDED_FROM_KPI, assignee_id in ids; те же столбцы и last_late; порядок deadline, id.
def kpi_in(snaps: Sequence[tuple[int, TaskSnapshot]], start: datetime, end: datetime, now: datetime,
           assignee_id: int | None = None) -> KpiResult
    # kpi.compute_kpi(отобранные по start <= deadline < end [и исполнителю], now, settings.overdue_counts_as_zero)
```
Алгоритм дашборда начальника: (1) активные сотрудники —
`select(User).where(ACTIVE, EMPLOYEE).order_by(User.full_name, User.id)`; (2) диапазон
`R = [min(period.start, trend.start), max(period.end, trend.end))`, если период и окно тренда
пересекаются или соприкасаются, иначе два запроса (период; окно тренда); (3) строки — `kpi_in` по
каждому сотруднику, сортировка как `kpi.kpi_for_team`:
`key=(kpi is None, -(kpi or 0), full_name)`; `team = kpi.team_kpi(rows)`; точки тренда —
`kpi_in(все снимки, неделя).kpi` (это равно `team_kpi` за ту неделю). Сотрудник: один запрос по
`assignee_ids=[id]` на объединение периода, окна тренда, текущих недели и месяца (или два, если
период далеко).

---

## 11. SPA (`bot/webapp/static/*`, UI)

### 11.1 Файлы и ограничения

* `index.html` (≤ 4 КБ), `app.css` (≤ 40 КБ), `app.js` (≤ 256 КБ, вместе со словарём узбекского языка) — без сборки, без модулей
  (`<script defer>`), без сторонних библиотек и шрифтов; иконки — эмодзи (как в чате).
* `index.html`: `<html lang="ru">`, `<meta charset="utf-8">`,
  `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`
  (масштабирование не запрещать), `<meta name="color-scheme" content="light dark">`, в `<head>` первым
  скриптом — `<script src="https://telegram.org/js/telegram-web-app.js"></script>`, затем
  `<link rel="stylesheet" href="/app/static/app.css?v=__ASSET_VERSION__">`,
  `<script defer src="/app/static/app.js?v=__ASSET_VERSION__"></script>`,
  `<script id="kpi-config" type="application/json">__KPI_CONFIG__</script>`; в `<body>` — корневой
  `<div id="app">` с текстом «Загрузка…» и `<div id="toast" role="status" aria-live="polite">`.
  Никаких inline-скриптов с кодом и обработчиков `on…=` (CSP).
* JS: ES2020 (optional chaining и `??` можно), без `eval`/`new Function`/`document.write`,
  **без `innerHTML`/`outerHTML`/`insertAdjacentHTML` с любыми данными** — DOM строится хелпером
  `h(tag, props, ...children)`, строки становятся текстовыми узлами. Целевые клиенты: Telegram iOS
  (WKWebView, iOS 15+), Android (Chromium 90+), Desktop, Web K/A.
* Все пути API — абсолютные строки, начинающиеся с `/api/` (тест §12.4 их сверяет с `ROUTES`);
  рекомендуется собрать их в одном объекте `EP`.

### 11.2 Запуск и интеграция с Telegram

```js
const tg = window.Telegram && window.Telegram.WebApp;
const CONFIG = JSON.parse(document.getElementById('kpi-config').textContent);
const insideTelegram = Boolean(tg && tg.initData);
```
1. `initData` = `tg.initData`; если пусто и `CONFIG.debug` — из `?tg_debug_init=` или
   `sessionStorage.kpi_debug_init` (§5.4); если всё равно пусто — экран «Откройте приложение из
   Telegram: кнопка «Открыть» в чате с ботом» (без запросов к API).
2. `tg.expand()` сразу; применить тему (§11.3); отрисовать каркас; `tg.ready()` после первой отрисовки.
3. `GET /api/me` → по `access`: неактивные — экран доступа (§11.6.4); активные — вкладки роли.
4. События: `themeChanged` → тема; `viewportChanged` — ничего не пересчитывать, кроме высоты листов;
   при возврате в приложение (`activated`, Bot API 8.0, если есть; иначе `visibilitychange`) —
   перезагрузить текущий экран, если его данные старше 30 с.
5. Проверки версии: `tg.isVersionAtLeast('6.1')` — `BackButton`, `HapticFeedback`; `'6.2'` —
   `enableClosingConfirmation`, `showConfirm`/`showPopup`; `'7.7'` — `disableVerticalSwipes()` (вызвать,
   чтобы прокрутка списков не закрывала приложение). Нет метода — тихий запасной вариант.
6. **MainButton** — основное действие формы. Обёртка `primary.set({ text, onClick, enabled, progress })`
   / `primary.hide()`: в Telegram — `tg.MainButton.setText/onClick/offClick/enable/disable/showProgress/
   hideProgress/show/hide` (всегда снимать прежний обработчик); вне Telegram (отладка) — своя
   закреплённая снизу кнопка с теми же свойствами. Пока запрос идёт — `progress: true`, кнопка
   недоступна, повторные нажатия игнорируются.
7. **BackButton** — на вложенных экранах и при открытом листе (§11.4); на корневых вкладках скрыта.
   Нажатие: закрыть лист, иначе «назад» по стеку маршрутов, иначе на корень вкладки.
8. **Подтверждение закрытия** — `tg.enableClosingConfirmation()`, пока в любой форме есть
   несохранённые изменения; `disableClosingConfirmation()` после успешной отправки/сброса. Уход
   кнопкой «назад» с изменённой формы — `showConfirm("Выйти без сохранения?")` (запасной —
   `window.confirm`).
9. **HapticFeedback**: успех действия — `notificationOccurred('success')`; ошибка API —
   `notificationOccurred('error')`; выбор чипа/вкладки — `selectionChanged()`.
10. Версия: ответ API с `X-App-Version` ≠ `CONFIG.version` → полоса «Приложение обновилось» с
    кнопкой «Обновить» (`location.reload()`).

### 11.3 Тема и токены (`app.css`)

Telegram сам выставляет CSS-переменные `--tg-theme-*` и `--tg-viewport-stable-height`. Все цвета
интерфейса — только через токены приложения с запасной палитрой (вне Telegram, в отладке):

```css
:root {                                   /* светлая запасная палитра (контраст текста ≥ 4.5:1) */
  --bg: var(--tg-theme-bg-color, #ffffff);
  --bg2: var(--tg-theme-secondary-bg-color, #eef0f4);
  --section: var(--tg-theme-section-bg-color, #ffffff);
  --text: var(--tg-theme-text-color, #121821);
  --hint: var(--tg-theme-hint-color, #5b6573);
  --link: var(--tg-theme-link-color, #2557c7);
  --accent: var(--tg-theme-button-color, #2557c7);
  --accent-text: var(--tg-theme-button-text-color, #ffffff);
  --destructive: var(--tg-theme-destructive-text-color, #c4352b);
  --separator: var(--tg-theme-section-separator-color, rgba(18, 24, 33, .12));
  --good: #136f40; --warn: #9a5b00; --bad: #c4352b;      /* шкалы KPI — всегда с подписью-текстом */
  --radius: 12px; --gap: 12px; --tap: 44px;
}
:root[data-theme="dark"] {                /* тёмная запасная палитра */
  --bg: var(--tg-theme-bg-color, #141a22);
  --bg2: var(--tg-theme-secondary-bg-color, #0c1117);
  --section: var(--tg-theme-section-bg-color, #19212b);
  --text: var(--tg-theme-text-color, #edf1f5);
  --hint: var(--tg-theme-hint-color, #8d9aab);
  --link: var(--tg-theme-link-color, #86a8ff);
  --accent: var(--tg-theme-button-color, #3d63d8);
  --accent-text: var(--tg-theme-button-text-color, #ffffff);
  --destructive: var(--tg-theme-destructive-text-color, #ff7a70);
  --separator: var(--tg-theme-section-separator-color, rgba(237, 241, 245, .1));
  --good: #47cf86; --warn: #eeb04a; --bad: #ff7a70;
}
```
Значения — как в `app.css` (первоначальные `#6b7480` на `#f1f2f5` и белый на `#2a7de1` давали ≈ 4.2:1 —
ниже требования §11.10; имена токенов не менялись). Вспомогательные токены (`--badge-*`, `--toast-*`,
`--overlay`, `--shadow`, `--font`) — только в `app.css`.
`document.documentElement.dataset.theme` = `tg.colorScheme` (в Telegram) или по
`matchMedia('(prefers-color-scheme: dark)')` (вне), обновлять по `themeChanged`/смене схемы.
`body { background: var(--bg2); color: var(--text); }`; карточки — `var(--section)`.
Шрифт — системный (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial,
sans-serif`), базовый размер 16 px, мелкий текст не меньше 13 px. `@media (prefers-reduced-motion: reduce)`
— без анимаций.

### 11.4 Раскладка и навигация

* Телефон прежде всего: ширина 360–430 px без горизонтальной прокрутки, отступы по бокам 16 px;
  на широком экране — колонка `max-width: 640px` по центру. Высота — `var(--tg-viewport-stable-height, 100vh)`;
  снизу учитывать `env(safe-area-inset-bottom)`.
* Нижняя панель вкладок (фиксированная, 4 кнопки «эмодзи + подпись», `aria-current="page"` у активной,
  бейдж с числом — по `me.counts`). Повторное нажатие активной вкладки — прокрутка вверх и обновление.
* Маршрутизация по `location.hash`, стек истории внутри приложения:

| Роль | Вкладка (корень) | Вложенные экраны (BackButton) |
|---|---|---|
| M | `#/team` «📊 Команда» | `#/team/user/{id}` карточка сотрудника |
| M | `#/tasks` «📋 Задачи» | `#/task/{id}` карточка, `#/task/{id}/edit` правка |
| M | `#/review` «📝 Проверка» (бейдж: review + proposals) | `#/review/{task_id}` проверка сдачи, `#/proposal/{task_id}` поручение |
| M | `#/new` «➕ Новая» | — |
| E | `#/my` «📋 Мои задачи» (бейдж: unaccepted + rework) | `#/task/{id}` |
| E | `#/submit` «📤 Сдать» (бейдж: overdue) | `#/submit/{task_id}` форма сдачи |
| E | `#/kpi` «📈 Мой KPI» | — |
| E | `#/propose` «➕ Поручение» | — |

  Стартовый экран: M — `#/team`, E — `#/my` (или маршрут из `hash`, если он допустим для роли;
  чужой — на стартовый).
* Листы (bottom sheet) для коротких диалогов: изменить оценку, вернуть на доработку, отменить задачу,
  отклонить поручение, вариант AI. Лист: заголовок, кнопка «✕», фокус внутри, Escape/BackButton
  закрывают; основное действие листа — MainButton.

### 11.5 Клиент API

* `api(method, path, body?)`: `fetch(path, {method, headers: {'X-Telegram-Init-Data': initData,
  'Content-Type': 'application/json'}, body: JSON})`, разбор JSON, ошибка → `ApiError{status, code, message}`
  (`message` = `error` из ответа; сеть — «Нет связи с сервером. Проверьте интернет и нажмите «Повторить».»).
* Сдача результата — `XMLHttpRequest` с `FormData` (текстовые поля первыми, затем `files`) ради
  `upload.onprogress`; тот же заголовок; таймаут 10 мин.
* Глобально: 401 → экран «Сессия устарела» (текст из ответа) с кнопкой «Закрыть» (`tg.close()`);
  403 `not_registered|pending|blocked` → экран доступа; 429 `busy` → тост с текстом; прочие → в месте
  действия (тост или блок ошибки с «Повторить») + haptic error.
* Кэш в памяти на маршрут (TTL 30 с); после любой мутации — сброс связанных списков и `me.counts`.

### 11.6 Экраны

Общие состояния каждого экрана с данными: **загрузка** — скелетон (серые блоки той же формы, не
спиннер на весь экран); **пусто** — текст из списка ниже; **ошибка** — текст ошибки и кнопка
«Повторить». Все строки состояния и подписи статусов брать из ответа API (`status_label`, `tail`,
`deadline_label`, `late_text`, `*_text`), не форматировать на клиенте заново.

#### 11.6.1 Начальник

**Команда (`#/team`).** Переключатель периода: сегменты «Неделя · Месяц · Квартал · Год» и строка
«◀ {period.label} ▶» (▶ недоступна при `has_next=false`). Крупно `team.kpi_text`, подпись
«Эффективность команды»; при `null` — «нет данных» и «Оценённых задач в периоде пока нет».
Итоги-чипы: «✅ {done} · ⏰ {overdue_total} · 🔄 {in_progress} · 📝 {on_review}». Карточка тренда
(§11.7). Список сотрудников (строка = кнопка): ФИО (`short_name`), должность мелко, справа `kpi_text`,
под ним полоса KPI (§11.7) и счётчики «✅ 14 · ⏰ 1 · 🔄 3 · 📝 1» или «задач нет» → карточка
сотрудника за тот же период. Внизу «📤 Excel-отчёт в чат» → `POST /api/export` → тост `message`.
Если `me.counts.pending_users > 0` — полоса «📥 Заявок на доступ: N — подтвердите в чате («👥 Сотрудники»)».
Пусто: «Активных сотрудников пока нет. Подтвердите заявки в чате с ботом: «👥 Сотрудники».»

**Карточка сотрудника (`#/team/user/{id}`)** и **Мой KPI (`#/kpi`)** — общий вид `KpiView` по
`GET /api/users/{id}/kpi`: ФИО и должность; переключатель периода; крупно `current.kpi_text` + полоса;
сетка показателей (Выполнено «done из total», Просрочено `overdue_total`, Выполнение в срок
`on_time_pct`, Перевыполнено, Внесено самостоятельно, В работе, На проверке); «Неделя: X · Месяц: Y»;
тренд; «🧮 Вошли в расчёт» (`items`: «Название — вес 20 % × 110 %», у `zero_overdue` — «⏰ просрочена»);
«📜 История оценок» (`history`: «#12 Название», «04.10 · вес 20 % · ⚠️ с опозданием · ↩️ доработок: 1»,
«🤖 110 % → 🏁 105 % ✏️ изменена» / «✅ подтверждена») и «Показать ещё». Только у начальника —
кнопка «📋 Задачи сотрудника» → `#/tasks?user={id}&status=all` (все его задачи, как
`ListCB(scope="emp", status="all")` в чате). Дальше адрес `#/tasks` следует за выбором фильтров
(`replaceQuery`), поэтому «Назад» из карточки задачи возвращает выбранный фильтр, а не исходный `?user`.
Тренд — всегда последние 8 недель до сегодня (§8.8), поэтому карточка называется «Последние 8 недель
(до сегодня)», а не «Динамика по неделям» (иначе под «2025 год» её легко принять за данные периода).
Пусто: задач в периоде нет — «Задач в этом периоде нет.»; истории нет — «Оценённых задач пока нет.»

**Задачи (`#/tasks`).** Поле поиска («Поиск: название, сотрудник или #номер», задержка 300 мс,
кнопка очистки), выбор сотрудника («Все сотрудники» + `GET /api/employees`), чипы статусов со
счётчиками (`counts=1`): «В работе · Просрочены · На проверке · Выполнены · На подтверждении · Все».
Строка задачи: иконка/подпись статуса, «#12 Название», исполнитель, справа `tail`, приоритет и вес
мелко, у непринятой активной — «не принята». «Показать ещё» (следующая страница). Пусто: «Задач нет.»;
при поиске — «По запросу «…» ничего не найдено.» (+ «Показаны первые 2000 задач» при `truncated`).

**Карточка задачи (`#/task/{id}`)** — общая для ролей: «Задача #12», название, статус-«пилюля»;
люди (у начальника — «👤 Исполнитель»; «🧑‍💼 Постановщик»/«Ответственный начальник»;
«✋ Внесена сотрудником (устное поручение)»); «🎯 Ожидаемый результат» + «📊 План: …»; описание (если
есть); «📅 Срок: {deadline_label}»; приоритет и вес (или «Вес и приоритет: назначит начальник при
подтверждении» при `weight_pending`); «✔️ Принята в работу …» / «⏳ … ещё не подтвердил(и) получение»;
«↩️ Возвратов на доработку: N». Сдачи — последняя раскрыта, прежние свёрнуты: попытка, время,
`late_text`, «✅ Факт», «📈 Результат», `fact_line`, файлы (имя, размер; у начальника — кнопка
«📎 Прислать файлы в чат» → `POST …/files` → тост «Файлы придут в чат с ботом»), блок AI (начальнику:
`ai.label` + `score_text` + обоснование; `ai_pending` — «⏳ Предварительная оценка ещё рассчитывается»
и автообновление каждые 5 с, не дольше 3 мин), решение (`decision_label`, итог, проверяющий,
комментарий). «📜 История» — свёрнутый список `events` («04.10 18:20 — Иванов И. И.: текст»).
Кнопки по `actions`: начальник — «✏️ Изменить», «🚫 Отменить» (лист: причина необязательна,
MainButton «Отменить задачу», подтверждение `showConfirm`), «🔍 Проверить» (→ `#/review/{id}`),
«✅ Подтвердить» / «✏️ Изменить» / «❌ Отклонить» у поручения (→ `#/proposal/{id}`); исполнитель —
«✅ Принял в работу» (`POST accept`, haptic, обновить карточку), «📤 Сдать результат» (→ `#/submit/{id}`).

**Правка (`#/task/{id}/edit`).** Только поля из `actions.edit_fields`, заполнены текущими значениями;
план — число + единица и «Убрать план» (`plan_value: null`); срок — §11.8; приоритет и вес — как в
форме создания. MainButton «Сохранить» доступна, когда есть изменения; шлётся только изменённое.
Успех: тост «✅ Сохранено» (+ `notice`), назад в карточку.

**Проверка (`#/review`).** Сегменты «Результаты (N)» | «Поручения (M)».
Результаты (`GET /api/review`): исполнитель, «#12 Название», «сдано 04.10 18:20 · {late_text}»,
справа «🤖 110 %» / «📐 100 %» / «⏳» → `#/review/{task_id}`. Пусто: «Нечего проверять 🎉».
Поручения (`GET /api/proposals`): исполнитель, «#15 Название», срок → `#/proposal/{id}`.
Пусто: «Новых поручений нет.»

**Проверка сдачи (`#/review/{task_id}`)** — по `GET /api/tasks/{id}`: шапка (задача, исполнитель,
вес, попытка); «🎯 План» ↔ «✅ Факт», «📈 Результат», `fact_line`; срок при сдаче, «📤 Сдано … — в срок /
с опозданием»; файлы (+ «📎 Прислать файлы в чат»); карточка AI (крупно оценка, `label`, обоснование;
`ai_pending` — ожидание с автообновлением); «Окончательное решение — за начальником.»
Действия:
* «✅ Подтвердить {score_text}» — только если есть `ai.score`; это MainButton экрана → `POST confirm`;
* «✏️ Изменить оценку» → лист: чипы `score_options` (предложенная AI отмечена «🤖», добавлена в ряд,
  если её нет; как `keyboards.score_kb`), поле числа 0..`max_score`, комментарий (≤ 2000,
  необязателен); MainButton «Поставить {n} %» → `POST score`;
* «↩ На доработку» → лист: «Что нужно доработать?» (обязательно, ≤ 2000); срок — «Оставить текущий
  ({deadline_local})» (недоступно с подписью «срок уже прошёл — укажите новый», если `deadline` в
  прошлом) или «Новый срок» (§11.8); MainButton «Вернуть на доработку» → `POST rework`.
Успех: haptic, тост «✅ Подтверждено: 110 %» / «✅ Оценка: 95 %» / «↩ Возвращено на доработку»
(+ `notice`), возврат в `#/review` с обновлением. Ответ 400 «Результат уже обработан…» — тост и
возврат с обновлением.

**Поручение (`#/proposal/{task_id}`).** Карточка предложения; блок «Подтвердить»: вес — чипы из
`GET /api/employees/{assignee}/weight-load?deadline=…&exclude_task_id={id}` с «⚠️» у `over` и подсказкой
«Сейчас на неделе: {load} %. Рекомендуется, чтобы сумма весов за неделю была ≈100 %», плюс своё
число 1..100; приоритет — сегменты «🔴 Высокий · 🟡 Средний · 🟢 Низкий» (по умолчанию средний);
MainButton «✅ Подтвердить поручение» → `POST approve`. Кнопки «✏️ Изменить» (→ правка с полями
предложения) и «❌ Отклонить» (лист: причина необязательна; MainButton «Отклонить» → `POST reject`).

**Новая задача (`#/new`).** Поля: «Сотрудник» (выбор из `GET /api/employees`; пусто — «Нет активных
сотрудников. Подтвердите заявки в чате: «👥 Сотрудники».» и форма недоступна), «Задача» (≤ 255),
«Ожидаемый результат» (своими словами, ≤ 2000) с кнопкой «✨ Сделать измеримым» (§11.9), «План»
(число + единица, необязательно; заполняется из подсказки), «Срок» (§11.8), «Приоритет» (сегменты,
средний), «Вес» (чипы `weight_options` с «⚠️» и подсказкой загрузки недели — запрос weight-load при
выборе сотрудника и срока; своё число 1..100). MainButton «Поставить задачу» доступна, когда
заполнены сотрудник, название, результат, срок и вес. Успех → тост «✅ Задача #N поставлена»
(+ `notice`), сброс формы, переход в `#/task/{id}`.

#### 11.6.2 Сотрудник

**Мои задачи (`#/my`).** Чипы «В работе · Просрочены · На проверке · Выполнены · На подтверждении ·
Все» (`scope=my`, `counts=1`), строки как у начальника без исполнителя; у непринятой активной —
кнопка «✅ Принять» прямо в строке (`POST accept`). Пусто: «Задач нет.» → карточка `#/task/{id}`.

**Сдать (`#/submit`).** Список открытых задач (`scope=my&status=open&limit=50`) с иконкой «⏰»
(просрочена) / «↩️» (на доработке) / «📌», «до dd.mm». Пусто: «Нет задач для сдачи. Здесь появятся
задачи в работе и на доработке.» → `#/submit/{id}`.

**Форма сдачи (`#/submit/{task_id}`)** — по `GET /api/tasks/{id}` (если `actions.submit = false` —
текст статуса и кнопка «К моим задачам»). Вверху: «📌 Задача #N: …», «🎯 Ожидаемый результат» + план,
«⏳ Срок», при доработке — «↩️ Задача возвращена на доработку» и «💬 Комментарий начальника:
{rework_comment}», при `attempts > 0` — «🔁 Попытка сдачи №{attempts+1}». Поля:
1. «Что фактически сделано?» * (3..3000), пример «Проверено 110 договоров, в 12 выявлены нарушения»;
2. «Какой получен результат?» (≤ 3000), пример «Подготовлен отчёт и рекомендации по нарушениям»;
3. «Фактическое значение» — только при `plan_value` («План: 100 договоров»), число ≥ 0;
4. «Подтверждающие материалы»: выбор файлов (`<input type="file" multiple>`), список «имя · размер ·
   ✕», проверка на клиенте: ≤ 10, каждый ≤ 20 МБ, всего ≤ 50 МБ, пустые нельзя (сообщение рядом);
   «Где лежат материалы» (≤ 1500), пример «ссылка на папку».
При `overdue` — «⚠️ Срок уже прошёл — результат будет отмечен как сданный с опозданием.»
MainButton «📤 Отправить» → загрузка с прогрессом («Отправка… 45 %» в кнопке/полосе). Успех (202) —
экран «✅ Результат отправлен начальнику на проверку. Решение придёт в чат с ботом.» (+ «📎 Файлы
сохранены в чате с ботом», если были) и кнопка «К моим задачам».

**Мой KPI (`#/kpi`)** — `KpiView` для себя (`GET /api/users/{me.id}/kpi`).

**Поручение (`#/propose`).** Подсказка «Внесите поручение, полученное устно: начальник подтвердит
его». Поля: «Задача» (≤ 255), «Ожидаемый результат» + «✨ Сделать измеримым», «План», «Срок».
MainButton «📤 Отправить начальнику» → `POST /api/proposals`. Успех: тост «📤 Поручение #N
отправлено начальнику на подтверждение» (или `notice`), сброс, переход в `#/task/{id}`.

#### 11.6.3 Тексты вкладок и экранов — по-русски, как в таблице §11.4; кнопки — «глагол + объект».

#### 11.6.4 Экраны доступа

`access = unregistered | pending | blocked` → полноэкранное сообщение `me.message` и кнопка
«Перейти в чат» (`tg.close()`); вне Telegram без отладки — «Откройте приложение из Telegram: кнопка
«Открыть» в чате с ботом.» 401 — «Сессия устарела…» (§6.2) и «Закрыть».

### 11.7 Графики (встроенный SVG, без библиотек)

**Полоса KPI** (строка сотрудника, KPI в карточке): SVG `width="100%" height="10"`, единая шкала на
экране `0 … max(120, ⌈max KPI / 10⌉ × 10)`; подложка `var(--bg2)`, полоса — `var(--good)` при KPI ≥ 100,
`var(--warn)` при 80–99, `var(--bad)` ниже 80; вертикальная отметка 100 % (2 px, `var(--text)` с
прозрачностью 0.5) — одинаково во всех строках; `null` — только подложка. Число — текстом рядом
(`kpi_text`), цвет не единственный носитель смысла.

**Тренд по неделям** (команда, сотрудник): SVG `width="100%" height="56"`, 8 точек `trend.points`
слева направо; ось Y `[min(60, min − 5), max(120, max + 5)]`; пунктир на уровне 100 % с подписью
«100 %»; линия `var(--accent)` 2 px через точки с данными (на `null` — разрыв), точки радиусом 3;
последняя точка подписана значением; под графиком — «{первая неделя} — {последняя неделя}».
Нет ни одной точки — «Недостаточно данных для динамики».

Доступность графиков: `role="img"` и `aria-label` с данными («KPI по неделям: 28.09 — 95 %, 05.10 —
нет данных, …»), декоративные элементы `aria-hidden`.

### 11.8 Выбор срока (общий компонент)

Чипы `me.deadline_options` («Завтра, 08.10» …) + `<input type="date" min="{me.today}">` +
`<input type="time">` (по умолчанию `default_deadline_time`). В запрос — `"YYYY-MM-DD"`, если время
не менялось, иначе `"YYYY-MM-DDTHH:MM"`. Подпись выбранного срока — местная дата и время.
Ошибки срока приходят от сервера (400) и показываются у поля.

### 11.9 Подсказка AI «Сделать измеримым»

Кнопка доступна, когда есть «Задача» и «Ожидаемый результат» (≥ 3 символов) → `POST /api/ai/formulate`
(клиентский таймаут 40 с; пока ждём — «⏳ Формулирую измеримый результат…», кнопка недоступна).
Ответ — карточка: формулировка, «📊 План: …», `note` курсивом, при `source = "rules"` — пометка
«по правилам, без AI», `notice` — предупреждением. Кнопки: «✅ Принять» (подставить в «Ожидаемый
результат», план и единицу; исходные слова уходят в `description`, если отличаются), «🔁 Другой
вариант» (тот же запрос с `previous` = текущая формулировка), «📝 Оставить как написал» (закрыть).

### 11.11 Голосовой ввод (микрофон)

Показывается, только если `cfg().voice_enabled` и в окне есть `navigator.mediaDevices.getUserMedia` и
`MediaRecorder` (`voiceSupported()`); иначе кнопок нет и формы работают как раньше.

* **Кнопка «🎤» у текстового поля** (`field()`: однострочные и многострочные поля; не числа, не даты, не
  поиск; `mic: false` — без кнопки): нажал — идёт запись (кнопка красная, «⏹ 0:07»), нажал ещё раз —
  «⏳», запись уходит на `POST /api/voice`, распознанный текст дописывается в конец поля (`appendDictated`,
  событие `input` — счётчик и черновик обновляются как при вводе с клавиатуры).
* **«🎤 Надиктовать задачу целиком»** — вверху «Новой задачи» и «Поручения» (`dictateBar`): `?mode=task` →
  `applyDictation` кладёт поля в черновик, форма перерисовывается; тост «Готово — проверьте поля». Вес и
  приоритет начальник выбирает сам.
* Запись: `MediaRecorder` с первым поддерживаемым типом из `VOICE_TYPES`, 32 кбит/с; не дольше
  `voice_max_sec` (потом останавливается сама); короче 0,6 с — «Запись слишком короткая». Одна запись за раз.
* Микрофон остаётся открытым `VOICE_IDLE_MS` (60 с) после записи — Telegram на Android спрашивает
  разрешение при каждом `getUserMedia`; приложение свернули — запись останавливается, микрофон отпускается.
* Ошибки — тостом: нет доступа («Разрешите микрофон для Telegram в настройках телефона…»), микрофон не
  найден, текст ошибки сервера (422/429/413).

### 11.12 Язык интерфейса

Тексты SPA написаны по-русски; для узбекского переводятся на выходе (SPEC.md §14): `tr()` вызывается в
`appendKids` (текстовые узлы), `setProps` (`aria-label`, `placeholder`, `title`, `alt`), `setText`,
MainButton, `confirmDialog`, заголовке окна. Строка → скелет (числа → `{}`, слова из `own()` → `{u}`) →
словарь `UZ` в конце `app.js` (между `/*UZ-BEGIN*/` и `/*UZ-END*/`). Чего в словаре нет, остаётся как есть.

* `own(value)` — слова пользователя и готовые подписи сервера: название задачи, ФИО, должность, тексты
  сдачи, комментарии, имена файлов, подпись срока. Составные подписи склеиваются по-русски и переводятся
  целиком («📌 Задача #5: {u}»); части, которые стоят рядом с подписями сервера, переводятся до склейки
  (`meta.push(tr('вес 20 %'))`).
* Язык: до ответа сервера — по `initDataUnsafe.user.language_code`, дальше `Me.lang` (`setLang`).
* Кнопка «🌐 Oʻzbekcha» / «🌐 Русский» справа от заголовка главных экранов вкладок (`langButton`): `POST
  /api/lang`, сброс кэша GET, `/api/me`, перерисовка каркаса и экрана. Черновики форм сохраняются.
* Даты (`humanDate`), размеры файлов и неразрывные пробелы учитывают язык. Логика, зависящая от текста
  ошибки сервера («срок», «уже обработан», «файл»), смотрит на `ApiError.ru` — русский исходник.
* Локальная отладка: `window.kpiI18n.misses` — скелеты строк без перевода.

### 11.10 Доступность

Только нативные `<button>`, `<input>`, `<label for>`; видимый `:focus-visible`; цели касания ≥ 44×44 px (в т.ч. сегменты: `min-height: var(--tap)`);
контраст текста ≥ 4.5:1 на запасной палитре (красная основная кнопка: текст `--danger-text` — белый в
светлой теме, тёмный в тёмной; в Telegram цвет текста MainButton для «опасного» действия выбирается по
яркости `destructive_text_color`); заголовки экранов `<h1>`/`<h2>` по порядку; при смене
экрана фокус — на заголовок; сегменты — `role="tablist"`/`role="tab"`/`aria-selected`; тосты —
`aria-live="polite"`; поля с ошибкой — `aria-invalid` и `aria-describedby`; состояние загрузки —
`aria-busy` на контейнере.

---

## 12. Тест-план

### 12.1 Общее

* Telegram — `tests/e2e/fakebot.py` (`FakeSession`, `BotHarness`), AI выключен или подменён; сети нет.
* Проверка «уведомление как в чате»: тот же текст, что даёт соответствующая `notify_*` (сравнивать с
  результатом вызова той же функции/рендера на тех же данных или по ключевым фразам чата:
  «🆕 Вам поставлена новая задача», «📥 Сотрудник внёс поручение», «✅ Начальник подтвердил ваше
  поручение», «❌ Начальник отклонил ваше поручение», «✏️ Начальник изменил задачу»,
  «🚫 Задача отменена начальником», «📝 Результат по задаче #N», «🏁 Результат по задаче #N оценён»,
  «↩️ Задача #N возвращена на доработку») и те же кнопки (`review_kb`, `new_task_kb`, `submit_kb`, …).
* `delivered=false` + `notice` при `session.blocked_chats` у получателя.
* Каждый тест-модуль самодостаточен; общий `tests/miniapp/conftest.py` (API) ставит окружение как
  `tests/e2e/conftest.py` (`BOT_TOKEN=42:TEST`, `ADMIN_IDS=1001`, `AI_PROVIDER=none`, ключи AI пустые,
  `TIMEZONE=Asia/Tashkent`, `DATABASE_URL=sqlite+aiosqlite:///:memory:`, `WEBAPP_ENABLED=1`) через
  `monkeypatch` + `get_settings.cache_clear()`, добавляет `tests` в `sys.path`
  (`from e2e.fakebot import BotHarness, FakeSession`), и даёт фикстуру `ma`:
  БД — фикстура `engine` из `tests/conftest.py`; `build_dispatcher` (с `release_bot_routers`, как в e2e);
  `Bot("42:TEST", session=FakeSession())`; `web.Application()` + `setup_webapp(..., static_dir=<tmp со
  стабами>)`; `aiohttp.test_utils.TestClient`; хелперы `ma.h` (BotHarness — чат на том же боте и базе),
  `ma.auth(tg_id)` (заголовок с `sign_init_data`), `ma.get/post/patch(path, as_=tg_id, json=…)`,
  `ma.drain()` (дождаться `TaskRegistry`), `ma.seed_team()`, `ma.seed_task(kind=…)`.

### 12.2 API

**`test_auth.py`** (векторы): верный initData → 200 `/api/me`; нет заголовка → 401 `auth_missing`;
изменён один символ `hash` → `auth_invalid`; изменено поле (`user.id`) при старом hash →
`auth_invalid`; подпись другим токеном → `auth_invalid`; `auth_date` = сейчас − 24 ч − 1 с →
`auth_expired`, сейчас − 24 ч + 60 с → 200; `auth_date` в будущем на 10 мин → `auth_invalid`;
нет `user` / `user` не JSON / `id` не целое / `id ≤ 0` / `id = true` → `auth_invalid`; нет `hash`;
повтор ключа; не-hex и не-ASCII `hash`; строка > 8 КБ; мусор («%%%», «a=b&&») — всегда 401, никогда 500;
поле `signature` присутствует → проверка проходит; наш `validate_init_data` согласен с
`aiogram.utils.web_app.check_webapp_signature` на наборе верных и испорченных строк; пустой токен —
всегда 401; в логах нет строки initData и hash (caplog).

**`test_access.py`** — матрица (параметризованный тест по `ROUTES`): для каждого маршрута — без
initData (401), незарегистрированный (403 `not_registered`, кроме `/api/me` — 200 `unregistered`),
PENDING (403 `pending` / 200), BLOCKED (403 `blocked` / 200), сотрудник (ожидание по §8.1: 403 на
маршрутах M), начальник (не 401/403), чужая задача сотрудника (403), несуществующие id (404),
id > 2^63 и «abc» (404), неизвестный маршрут (404 JSON), неверный метод (405 JSON).

**`test_tasks_api.py`**: списки всех `scope`/`status` и их совпадение с `tasks.list_tasks`/`count_tasks`
(порядок и итог); пагинация (за пределами — пусто); `counts`; поиск по кириллице без учёта регистра
(«ДОГОВОР» находит «Анализ договоров»), «ё/е», по ФИО исполнителя, «#12» и «12», `truncated` при
маленьком `SEARCH_SCAN_LIMIT`; `tail`/`status_label` совпадают с `render.task_line` (без HTML) на
задачах всех статусов; карточка: поля, `actions` для каждой роли и статуса совпадают с
`keyboards.task_actions_kb`; **видимость AI**: сотрудник на SUBMITTED с оценкой — нет `ai`,
`ai_hidden=true`, `ai_score=null`, нет события `ai_evaluated`; после решения — оценка есть,
обоснования нет; начальник видит всё. Создание (201, уведомление исполнителю как в чате,
`delivered`, `notice` при блокировке), ошибки валидации (каждое поле), прошлый срок (400 `domain`
«Срок должен быть в будущем»), неактивный исполнитель; правка ACTIVE (все поля, `changed`,
уведомление «было → стало»), правка PROPOSED с весом → 400, `plan_value: null` очищает план, пустое
тело → 400; принятие (повтор — без ошибки, чужая — 403); отмена (уведомление, повтор → 400).

**`test_review_api.py`**: очередь (порядок, вид начальника); confirm (DONE, итог = AI, уведомление
сотруднику, ответ); score (границы 0 и `max_score`, `true`/строка → 400, комментарий); rework
(комментарий обязателен; без срока при прошедшем сроке → 400; новый срок; уведомление с
`submit_kb`); гонка: два confirm подряд (и confirm в API + «✅ Подтвердить» в чате) — второй
400 «Результат уже обработан»; своя задача (повышенный до начальника исполнитель) → 400;
нет AI-оценки → 400; `files` → файлы пришли в чат начальника, без файлов → 400.

**`test_proposals_api.py`**: сотрудник вносит (201, всем начальникам `proposal_kb`, `notified`;
начальников нет → `notified=0` + `notice`); начальник не может вносить (403); approve (вес,
приоритет, ACTIVE, `accepted_at`, уведомление), прошедший срок → 400; reject с причиной и без;
правка предложения; weight-load с `exclude_task_id`.

**`test_submit_api.py`**: успешная сдача с 2 файлами (фото + документ): 202 до окончания оценки
(оценку задержать подменой), файлы в чате сотрудника с подписью «📎 К задаче #N» и
`disable_notification`, `Attachment` с `file_id` из ответа Telegram и исходными именами, после
`ma.drain()` — начальнику `submission_text` + `review_kb` + файлы, оценка по правилам с
`RULES_PREFIX`; `materials_text` попадает в `result_text` как в чате; `fact_value` «1 200» → 1200;
лимиты (11 файлов, файл > лимита, сумма > лимита — с подменёнными малыми константами), пустой
файл, неизвестное поле, не multipart; чужая задача (403), задача не открыта (400 с текстом чата);
`TelegramForbiddenError` при загрузке → 502 и сдачи нет; задачу отменили во время загрузки → 400 и
сдачи нет; второй одновременный запрос → 429 `busy`; временные файлы удалены во всех случаях;
сотрудник после сдачи не видит оценку AI; диалог сдачи в чате, начатый до сдачи через API, на
«📤 Отправить» отвечает «Результат уже отправлен…» (без второй сдачи).

**`test_kpi_api.py`**: дашборд начальника и сотрудника для всех `kind` и `offset` 0, −1, −30
(далеко — два запроса) совпадает с `kpi.kpi_for_team`/`kpi_for_user`/`team_kpi` (числа, счётчики,
порядок строк); тренд — 8 точек, значения = `kpi_for_user`/`team_kpi` по каждой неделе; `offset > 0`
→ 0, `< −500` → −500, неизвестный `kind` → 400; `/api/users/{id}/kpi`: начальник — любой (в т.ч.
заблокированный), сотрудник — только себя (403), история и пагинация = `evaluated_history`/`count_tasks`.

**`test_misc_api.py`**: `/api/me` всех видов доступа и ролей (`counts` верные, `deadline_options`,
`config`); formulate (правила при выключенном AI; `previous` → `notice`; AI подменён — ответ AI;
зависший AI → правила по таймауту; второй одновременный → 429); export (документ с именем и подписью
как в чате; сбой сборки — текст ошибки в чат; 429 при повторе до окончания); employees; weight-load.

**`test_pages.py`**: `GET /app` и `/app/` — 200, `text/html`, CSP и прочие заголовки §4.3, заглушки
заменены (нет `__ASSET_VERSION__`, `__KPI_CONFIG__`), версия = sha256 стабов, `debug` по
`webapp_debug_active` (в webhook — всегда false); статика из белого списка с верным типом и кэшем по
`?v=`; неизвестный файл и обходы пути (`..`, `%2e%2e%2f`, `app.js/`) — 404; нет файлов — 503;
`/api/*` отвечает `X-App-Version` и `Cache-Control: no-store`.

**`test_dev_server.py`**: сборка dev-приложения не обращается к сети; отказ при `data/bot.db`, при
PostgreSQL; `/dev/login?tg_id=…` → 302 с валидным `tg_debug_init` (проходит `validate_init_data`);
`--seed-demo` создаёт описанный набор (только в пустой базе).

**`tests/e2e/test_webapp_flow.py`** (полный цикл через API + чат на фикстуре `app`):
начальник создаёт задачу через API → сотруднику в чат пришла карточка с «✅ Принял в работу» →
сотрудник нажимает её **в чате** → API-карточка показывает «принята» → сотрудник сдаёт через API
(1 файл) → `drain` → начальнику в чат пришёл результат с кнопками → начальник подтверждает
**через API** → сотруднику в чат пришло «🏁 Результат по задаче #N оценён» → дашборд и
`/api/users/{id}/kpi` показывают 110 % и задачу в истории. Обратный путь: задача из чата
(«➕ Поставить задачу») видна в API; сдача через чат проверяется через API; сдача через API
проверяется в чате (SubCB ok). Поручение: внесено через API → начальник подтверждает в чате.

### 12.3 CORE

**`tests/e2e/test_submission_flow.py`**: `run_after_submit` при выключенном AI — оценка по правилам с
`RULES_PREFIX`, `source="rules"`, начальнику `notify_submission` (`notified=True`); AI подменён —
`source="ai"`; AI зависает дольше `budget_sec` — правила; первая запись оценки AI падает ошибкой
базы — откат и правила; задачу отменили во время оценки — `status=CANCELLED`, уведомления нет;
решение уже принято — уведомления нет; никогда не бросает (подменённые сервисы бросают) —
`FlowResult` с `source=None`; `result_with_notes`.
Регрессия: весь набор `tests/e2e/test_submit_review.py`, `tests/test_ai_chain.py`,
`tests/e2e/test_security.py`, `tests/test_jobs_tick.py` — без изменений; `tests/perf` — бюджеты
S6 те же.

**`tests/test_webapp_integration.py`**: `Settings` — значения по умолчанию, `WEBAPP_ENABLED=0`,
`webapp_url` (webhook + https → «…/app»; polling, http, выключено → «»), `webapp_debug_active`;
`build_web_app` монтирует `/app` и `/api` (200/401) при включённом и не монтирует при выключенном
(404), `/health` и webhook работают как раньше; `_drain` ждёт фоновую задачу приложения (не дольше
`SHUTDOWN_GRACE_SEC`); `_run_webhook` вызывает `SetChatMenuButton` с `MenuButtonWebApp(text="Открыть",
url=…/app)` (проверка по запросам `FakeSession`/`WebhookApi`), при выключенном — `MenuButtonDefault`;
ошибка Telegram при установке — запуск продолжается, предупреждение в логе; polling — запроса
`SetChatMenuButton` нет; `TAKEOVER_WEBHOOK=1` — `MenuButtonDefault`; `/start` активного в webhook-
настройках — второе сообщение с кнопкой `web_app.url = …/app`, у неактивного и в polling — нет.

### 12.4 UI

**`tests/miniapp/test_static_assets.py`** (статический анализ, без браузера): три файла есть;
`index.html` содержит `lang="ru"`, viewport без запрета масштаба, скрипт Telegram первым скриптом,
обе заглушки `__ASSET_VERSION__` и `__KPI_CONFIG__` (в `type="application/json"`), нет `on…=` и
inline-скриптов с кодом; во всех файлах нет внешних адресов, кроме
`https://telegram.org/js/telegram-web-app.js`; в `app.js` нет `innerHTML`, `outerHTML`,
`insertAdjacentHTML`, `document.write`, `eval(`, `new Function`; размеры ≤ 4/40/256 КБ; каждый
литерал `/api/...` в `app.js` (шаблон `${…}` → параметр, без `?…`) соответствует маршруту из
`bot.webapp.api.ROUTES`; в `app.css` есть токены §11.3 и блок `:root[data-theme="dark"]`; в `app.js`
есть подписи всех восьми вкладок.

**Проверка в браузере** (обязательна до сдачи; dev-сервер §4.6 с `--seed-demo`, вход через `/dev/`
за начальника 1001 и сотрудника 2001; встроенный браузер / превью): ширины 360, 375, 430 px, светлая
и тёмная тема (`colorScheme` эмуляция); на каждом экране §11.6 — загрузка, данные, пусто, ошибка
(остановить dev-сервер → «Повторить»); `document.documentElement.scrollWidth <= innerWidth` на каждом
экране; полный цикл: создать задачу → войти сотрудником → принять → сдать с файлом → войти
начальником → проверить (подтвердить / изменить / вернуть) → KPI изменился; поручение →
подтвердить; поиск и фильтры; клавиатура не перекрывает поля; консоль без ошибок. Скриншоты ключевых
экранов — в отчёт агента.

### 12.5 Бюджеты обменов с базой (`tests/perf/test_webapp_budget.py`, API)

Замер — `RoundTripProbe` (`tests/perf/roundtrips.py`) на движке «main» вокруг одного HTTP-запроса
через `TestClient` (фоновые задачи до/после замера дождаться `drain`). Свой словарь
`WEBAPP_BUDGETS` и своя проверка (запас как `_slack`: 10 %, не меньше 1). Потолки ниже; агент ставит
в словарь фактически измеренные числа (не больше потолка) с датой.

| Замер | Данные | Потолок RT |
|---|---|---|
| `M01 GET /api/me (начальник)` | 5 сотрудников × 10 задач | 2 |
| `M02 GET /api/tasks scope=all status=open` | 50 задач, limit 20 | 3 |
| `M03 GET /api/tasks … counts=1` | то же | 4 |
| `M04 GET /api/tasks q=договор` | 50 задач | 2 |
| `M05 GET /api/tasks/{id}` (доработка: сдача + файл + журнал) | — | 5 |
| `M06 GET /api/dashboard (начальник)` | 5 × 10, неделя | 3 |
| `M07 GET /api/dashboard (сотрудник)` | 10 задач | 2 |
| `M08 GET /api/users/{id}/kpi` | 10 задач, история | 5 |
| `M09 GET /api/review` | 5 сдач с файлами | 4 |
| `M10 GET /api/proposals` | 3 поручения | 3 |
| `M11 POST /api/submissions/{id}/confirm` | — | 9 |
| `M12 POST /api/tasks` (создание) | — | 6 |

Независимость от объёма: `M02`, `M06`, `M09` повторить с данными ×5 (и 2 → 5 сотрудников) — число
обменов то же. Счётчики одинаковы на SQLite и PostgreSQL. Бюджеты чата
(`tests/perf/test_roundtrip_budget.py`) не меняются.

### 12.6 Финальная проверка

1. `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python -m pytest -q -p no:cacheprovider` —
   всё зелёное (1161 прежних + новые).
2. То же с `TEST_DATABASE_URL=postgresql://postgres:pgtest@127.0.0.1:55433/kpi_test` (если сервер
   доступен).
3. `./.venv/Scripts/python -m pytest tests/perf -q -p no:cacheprovider` — бюджеты чата и приложения.
4. Проверка в браузере §12.4 на итоговом коде.

---

## 13. Чек-лист приёмки

- [ ] Кнопка меню «Открыть» и «📱 Открыть приложение» после `/start` — только в webhook; в polling
      чат ведёт себя как раньше (тесты `/start` не менялись).
- [ ] Вход только по проверенному `initData` (24 ч, постоянное время сравнения); права — по базе;
      неактивные и незарегистрированные видят понятный экран.
- [ ] Каждое действие приложения вызывает тот же сервис, что и чат; ошибки сервисов — их же текстом.
- [ ] Уведомления другим людям — те же `notify_*` и те же тексты/кнопки, после commit; начальник
      видит предупреждение, если уведомление не доставлено.
- [ ] Сотрудник не видит оценку AI до решения и никогда не видит обоснование AI.
- [ ] Сдача через приложение: файлы в чате сотрудника «📎 К задаче #N», `file_id` в базе, оценка и
      уведомление начальника — общим конвейером `submission_flow`, ответ 202 сразу.
- [ ] KPI приложения совпадает с чатом и Excel; тренд по 8 неделям.
- [ ] Обмены с базой в бюджетах и не растут с объёмом данных; бюджеты чата не изменились.
- [ ] Нет внешних ресурсов, кроме `telegram-web-app.js`; нет `innerHTML` с данными; CSP включён.
- [ ] Интерфейс по-русски, телефон 360–430 px без горизонтальной прокрутки, светлая и тёмная темы,
      MainButton/BackButton/HapticFeedback/подтверждение закрытия работают; доступность §11.10.
- [ ] Полный набор тестов зелёный на SQLite и PostgreSQL; `data/`, `.env`, `deploy/render.env` не
      тронуты; боевой бот не запускался.

---

## 14. Итоговая реализация: раскладка и отличия от плана

Документ выше — план, по которому строили три агента. Здесь — что получилось на самом деле (по итогам
интеграции); при расхождении верно то, что написано здесь.

**Файлы пакета `bot/webapp`**
* `__init__.py` — вход (`setup_webapp`, `register_webapp`, `pending_tasks`) и то, что план относил к
  `context.py` и `pages.py`: `ApiError`, `TaskRegistry`, `UserGate`, `StaticBundle`, страница `/app` и
  статика, ключи `CTX` / `TASKS`.
* `api.py` — middleware, обработчики, `ROUTES` и лёгкие чтения §10 (план — `queries.py`): `list_task_rows`,
  `search_task_rows`, `tab_counts`, `me_counts`, `history_rows`, `snapshots_between`, `kpi_in`.
* `serializers.py` — схемы §7 и строки лёгких чтений `TaskRowData`, `HistoryRowData`.
* `auth.py` — §5. `dev.py` — dev-сервер §4.6. `static/` — SPA §11.

**Тесты**

| План | Сделано |
|---|---|
| `tests/miniapp/test_*.py` | `tests/webapp/test_auth.py`, `test_api_access.py`, `test_api_tasks.py`, `test_api_review.py`, `test_api_proposals.py`, `test_api_submit.py`, `test_api_kpi.py`, `test_api_misc.py`, `test_api_pages.py`, `test_dev_server.py` |
| `tests/miniapp/test_static_assets.py` | `tests/webapp/test_static_contract.py` (+ сверка путей `app.js` с `api.ROUTES`) |
| `tests/e2e/test_webapp_flow.py` | `tests/webapp/test_api_e2e.py` (сквозной цикл приложение ↔ чат) |
| `tests/perf/test_webapp_budget.py` | `tests/webapp/test_api_budget.py` (бюджеты §12.5, `WEBAPP_BUDGETS`) |
| `tests/e2e/test_submission_flow.py`, `tests/test_webapp_integration.py` | как в плане |

**Отличия в поведении (осознанные)**
1. Файлы сдачи грузятся в Telegram до трёх одновременно (`api.UPLOAD_CONCURRENCY`), порядок вложений —
   как у файлов в запросе.
2. Данные запроса лежат под ключами aiohttp `api.INIT` / `api.SESSION` / `api.VIEWER` (`RequestKey`), а не
   `request["init"]` — без предупреждений aiohttp.
3. Заявка без ФИО (анкета в чате не заполнена) — `access: "unregistered"` с текстом про `/start`, а не
   `pending`.
4. Итог списков и истории считается `count(*) OVER ()` в том же запросе (на один обмен с базой меньше §12.5).
5. `TAKEOVER_WEBHOOK=1`: стандартную кнопку меню ставит `_run_polling` после `SetMyCommands`, а не
   `_drop_webhook` (сохранён порядок первых запросов, который проверяет `tests/test_web.py`); результат тот же.
6. Запасная палитра §11.3 — обновлена ради контраста ≥ 4.5:1 (значения в §11.3 уже новые).
7. Поле поиска задач: подсказка «Название, сотрудник или #номер» — полная фраза §11.6.1 обрезалась на
   экране 360 px; для чтения с экрана есть подпись «Поиск задач».
8. SPA: в подписях с сервера пробелы в «100 %», «на 1 дн.», «до 09.10» неразрывные — подпись не рвётся
   посередине; подписи на графике тренда — с «ореолом» цвета карточки (линия их не перечёркивает).
9. Dev-сервер: `/dev/login` принимает ещё `to=/route` (сразу открыть нужный экран), на `/dev/` — ссылки на
   вкладки роли и вход «как незарегистрированный»; `--seed-demo` кроме набора §4.6 кладёт по две оценённые
   задачи в прошлые недели (график тренда не пустой); `seed_demo(extended=True)` (для скриншотов) добавляет
   сотрудников 2005 (лидер, сдача с оценкой AI и фото) и 2006 (новичок без оценок).
10. `start_param` вида `task_<id>` SPA уже понимает (открывает `#/task/<id>`), но ссылок
    `t.me/<бот>/<приложение>?startapp=…` в уведомлениях нет: для них нужно короткое имя Mini App в
    BotFather, а принцип проекта — «в BotFather ничего настраивать не нужно».

**Проверка в браузере.** Скриншоты экранов обеих ролей (светлая и тёмная темы, 360 и 390 px) —
`docs/miniapp_screens/`. Снимались на `bot.web.build_web_app` (тот же сервер, что на Render) с демо-базой
`seed_demo(extended=True)`, фейковым Telegram и `WEBAPP_DEBUG=1`; сквозной цикл «сотрудник сдал с двумя
файлами → начальник подтвердил → сотрудник видит итог» прошёл в настоящем браузере. После исправлений
по итогам ревью (08.10.2026) пересняты экраны с сегментами, трендом и полями плана (01, 04, 06, 08–10, 14,
15, 27, 28, 30, 31, 33) и добавлены: `01b` — «Команда» без заявок на доступ (раньше там появлялся текст
«null»), `02b` — «📋 Задачи сотрудника» из карточки (фильтр «Иванов И. И.», статус «Все»), `34` — тёмная тема,
красная основная кнопка «Отменить задачу» (тёмный текст на светло-красном).
