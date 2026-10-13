/*
 * Mini App «Эффективность» — одностраничное приложение без сборки (docs/MINIAPP_SPEC.md §11).
 *
 * Устройство:
 *  • h()/s() строят DOM; данные попадают в страницу только текстовыми узлами (никакой HTML-разметки
 *    из строк — так требует CSP и §11.1);
 *  • api() ходит в /api/* с заголовком X-Telegram-Init-Data (на сайте — со знаком страницы X-KPI-CSRF,
 *    раздел 17b), кэширует GET в памяти на 30 с и превращает ответы-ошибки в ApiError (401/403 доступа/429
 *    обрабатываются глобально);
 *  • маршрутизатор по location.hash держит свой стек экранов (history.replaceState — браузерная
 *    история не растёт); экран — функция View(scr, args, query), строящая DOM внутри scr.el;
 *  • primary/back — обёртки над MainButton и BackButton Telegram; вне Telegram (локальная отладка)
 *    те же роли играют свои кнопки на странице.
 * Все адреса API собраны в EP: тест tests/webapp/test_static_contract.py сверяет их со спецификацией.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------------------------
  // 0. Окружение и константы
  // ---------------------------------------------------------------------------------------------

  const tg = (window.Telegram && window.Telegram.WebApp) || null;
  const CONFIG = readConfig();
  const insideTelegram = Boolean(tg && tg.initData);
  // Сайт (обычный браузер, §15): страницу «/» отдал сервер с пометкой web. Человека называет cookie сессии,
  // вход — через подтверждение в боте; экраны, права и API — те же.
  const WEB = Boolean(CONFIG.web) && !insideTelegram;
  // Большой экран сайта (§15.6): от 1024 px — боковое меню и таблицы, от 1280 px — панель подробностей рядом
  // со списком. В Mini App оба условия ложны всегда: его раскладка не меняется.
  const wideQuery = WEB && window.matchMedia ? window.matchMedia('(min-width: 1024px)') : null;
  const paneQuery = WEB && window.matchMedia ? window.matchMedia('(min-width: 1280px)') : null;

  function isWide() {
    return Boolean(wideQuery && wideQuery.matches);
  }

  function hasPane() {
    return Boolean(paneQuery && paneQuery.matches);
  }

  /** Строк на страницу списка: таблица большого экрана вмещает больше. */
  function pageSize() {
    return isWide() ? 50 : 20;
  }
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const CACHE_TTL_MS = 30 * 1000;      // GET-ответы живут в памяти 30 с (§11.5)
  const STALE_MAX_MS = 10 * 60 * 1000; // прежний ответ не старше 10 мин виден, пока идёт запрос за свежим
  const STALE_MS = 30 * 1000;          // при возврате в приложение экран старше 30 с перечитывается
  const GET_TIMEOUT_MS = 20 * 1000;
  const POST_TIMEOUT_MS = 30 * 1000;
  const AI_TIMEOUT_MS = 40 * 1000;     // «Сделать измеримым» (§11.9)
  const UPLOAD_TIMEOUT_MS = 10 * 60 * 1000;
  const PENDING_POLL_MS = 5 * 1000;    // оценка AI ещё считается — обновлять каждые 5 с…
  const PENDING_MAX_MS = 3 * 60 * 1000; // …но не дольше 3 мин
  const SEARCH_DELAY_MS = 300;
  const MB = 1024 * 1024;
  const ACCESS_CODES = ['not_registered', 'pending', 'blocked'];

  /** Все маршруты API: [метод, шаблон пути] — имена параметров как в bot.webapp.api.ROUTES. */
  const EP = Object.freeze({
    me: ['GET', '/api/me'],
    setLang: ['POST', '/api/lang'],
    tasks: ['GET', '/api/tasks'],
    task: ['GET', '/api/tasks/{task_id}'],
    createTask: ['POST', '/api/tasks'],
    updateTask: ['PATCH', '/api/tasks/{task_id}'],
    acceptTask: ['POST', '/api/tasks/{task_id}/accept'],
    cancelTask: ['POST', '/api/tasks/{task_id}/cancel'],
    submitTask: ['POST', '/api/tasks/{task_id}/submit'],
    approveTask: ['POST', '/api/tasks/{task_id}/approve'],
    rejectTask: ['POST', '/api/tasks/{task_id}/reject'],
    formulate: ['POST', '/api/ai/formulate'],
    voice: ['POST', '/api/voice'],
    review: ['GET', '/api/review'],
    confirm: ['POST', '/api/submissions/{sub_id}/confirm'],
    setScore: ['POST', '/api/submissions/{sub_id}/score'],
    reviseScore: ['POST', '/api/submissions/{sub_id}/revise'],
    rework: ['POST', '/api/submissions/{sub_id}/rework'],
    sendFiles: ['POST', '/api/submissions/{sub_id}/files'],
    proposals: ['GET', '/api/proposals'],
    propose: ['POST', '/api/proposals'],
    dashboard: ['GET', '/api/dashboard'],
    userKpi: ['GET', '/api/users/{user_id}/kpi'],
    employees: ['GET', '/api/employees'],
    weightLoad: ['GET', '/api/employees/{user_id}/weight-load'],
    exportReport: ['POST', '/api/export'],
  });
  /** Запросы, которые не меняют ни списков, ни карточек (и бывают долгими): загрузчики их не ждут (см. settled). */
  const QUIET = new Set([EP.formulate, EP.voice, EP.sendFiles, EP.exportReport]);

  /** Настройки по умолчанию — до ответа /api/me (тот же состав, что Me.config). */
  const DEFAULT_CFG = {
    max_score: 150, timezone: 'Asia/Tashkent', default_deadline_time: '18:00', ai_enabled: false,
    period_kinds: ['week', 'month', 'quarter', 'year'], max_files: 10, max_file_mb: 20, max_total_mb: 50,
    weight_options: [5, 10, 15, 20, 25, 30, 40, 50], score_options: [50, 70, 80, 90, 100, 110, 120],
    history_page_size: 10, trend_weeks: 8,
  };

  const T = {
    network: 'Нет связи с сервером. Проверьте интернет и нажмите «Повторить».',
    timeout: 'Сервер долго не отвечает. Проверьте интернет и нажмите «Повторить».',
    generic: 'Произошла ошибка, попробуйте ещё раз',
    openFromTelegram: 'Откройте приложение из Telegram: кнопка «Открыть» в чате с ботом.',
    leave: 'Выйти без сохранения?',
    leaveUpload: 'Результат ещё отправляется. Уйти с этого экрана?',
    retry: 'Повторить',
    uploadTimeout: 'Отправка заняла слишком много времени. Проверьте интернет и попробуйте ещё раз.',
    aiTimeout: 'AI не ответил вовремя. Попробуйте ещё раз или сформулируйте результат сами.',
    noEmployees: 'Нет активных сотрудников. Подтвердите заявки в чате: «Сотрудники».',
  };

  const STATUS_FILTERS = [
    { value: 'open', label: 'В работе' },
    { value: 'overdue', label: 'Просрочены' },
    { value: 'review', label: 'На проверке' },
    { value: 'done', label: 'Выполнены' },
    { value: 'proposed', label: 'На подтверждении' },
    { value: 'all', label: 'Все' },
  ];
  const PERIOD_KINDS = [
    { value: 'week', label: 'Неделя' },
    { value: 'month', label: 'Месяц' },
    { value: 'quarter', label: 'Квартал' },
    { value: 'year', label: 'Год' },
  ];
  const PRIORITIES = [
    { value: 'high', label: 'Высокий', tone: 'bad' },
    { value: 'medium', label: 'Средний', tone: 'warn' },
    { value: 'low', label: 'Низкий', tone: 'good' },
  ];
  const PRIORITY_TONES = { high: 'bad', medium: 'warn', low: 'good' };
  // Цветной кружок перед подписью приоритета в строках журнала с сервера («приоритет ⟨кружок⟩ Высокий»):
  // убирается только он — такой же кружок в словах пользователя (причина, название) остаётся.
  const PRIORITY_DOTS = new RegExp('[' + String.fromCodePoint(0x1F534, 0x1F7E1, 0x1F7E2) + '] (?=Высокий|Средний|Низкий|Yuqori|Oʻrta|Past)', 'gu');
  const NOT_OPEN_TEXTS = {
    submitted: 'Результат уже отправлен и ждёт проверки начальника.',
    done: 'Задача уже выполнена и оценена — сдавать результат не нужно.',
    cancelled: 'Задача отменена начальником — сдавать результат не нужно.',
    proposed: 'Поручение ещё не подтверждено начальником — сдать результат можно после подтверждения.',
    rejected: 'Поручение отклонено начальником — сдавать результат не нужно.',
  };
  const KIND_ICONS = { document: 'file', photo: 'image', video: 'video', other: 'clip' };
  const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
    'сентября', 'октября', 'ноября', 'декабря'];
  const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const MONTHS_UZ = ['yanvar', 'fevral', 'mart', 'aprel', 'may', 'iyun', 'iyul', 'avgust',
    'sentabr', 'oktabr', 'noyabr', 'dekabr'];
  const WEEKDAYS_UZ = ['yak', 'du', 'se', 'chor', 'pay', 'ju', 'shan'];

  // ---------------------------------------------------------------------------------------------
  // 0a. Язык интерфейса (SPEC.md §14, MINIAPP_SPEC.md §11.12)
  // ---------------------------------------------------------------------------------------------

  /*
   * Тексты приложения написаны по-русски. Для узбекского они переводятся в одном месте — на выходе:
   * текстовые узлы и подписи-атрибуты (appendKids, setProps, setText), кнопка MainButton, диалоги.
   * Строка превращается в «скелет»: числа -> {}, слова пользователя, обёрнутые в own(), -> {u};
   * скелет ищется в словаре UZ (в конце файла). Чего в словаре нет, остаётся как есть.
   * Подписи с сервера (статус, срок, журнал, ошибки) приходят уже на языке пользователя.
   */
  const U0 = String.fromCharCode(0x2062);  // невидимые метки слов пользователя — как bot.i18n на сервере
  const U1 = String.fromCharCode(0x2063);
  const MARKS_RE = new RegExp('[' + U0 + U1 + ']', 'g');
  const USER_RE = new RegExp(U0 + '([^' + U0 + U1 + ']*)' + U1, 'g');
  const CYR_RE = /[А-Яа-яЁё]/;
  const NUM_RE = /\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|\d+(?:,\d+)?/g;
  const SLOT_RE = /\{(u?)(\d*)\}/g;
  const HOLE = String.fromCharCode(0xE000);  // место слов пользователя в строке, пока она переводится
  const HOLES_RE = new RegExp(HOLE, 'g');
  const i18n = { lang: 'ru', misses: new Set() };
  if (CONFIG.debug) window.kpiI18n = i18n;  // локальная отладка: непереведённые строки — в kpiI18n.misses

  /** Слова пользователя (название задачи, ФИО, комментарий) и готовые подписи с сервера: перевод их не трогает. */
  function own(value) {
    return value === null || value === undefined || value === '' ? '' : U0 + String(value) + U1;
  }

  function plain(text) {
    return text.indexOf(U0) < 0 && text.indexOf(U1) < 0 ? text : text.replace(MARKS_RE, '');
  }

  /** Готовый текст -> на язык интерфейса; метки убираются всегда. */
  function tr(value) {
    const text = String(value);
    if (i18n.lang !== 'uz' || !CYR_RE.test(text)) return plain(text);
    const users = [];
    const masked = plain(text.replace(USER_RE, (_, words) => {
      users.push(words);
      return HOLE;
    }));
    return masked.split('\n').map((line) => trLine(line, users)).join('\n');
  }

  function trLine(line, users) {
    const mine = users.splice(0, (line.match(HOLES_RE) || []).length);
    const restore = (text) => {
      let i = 0;
      return text.replace(HOLES_RE, () => {
        i += 1;
        return mine[i - 1] || '';
      });
    };
    const core = line.trim();
    if (!core || !CYR_RE.test(core)) return restore(line);  // переводить нечего: только слова пользователя
    const nums = [];
    const key = core.replace(NUM_RE, (n) => {
      nums.push(n);
      return '{}';
    }).replace(HOLES_RE, '{u}');
    const template = UZ[key];
    if (template === undefined) {
      i18n.misses.add(key);
      return restore(line);
    }
    const seq = { u: 0, n: 0 };
    const out = template.replace(SLOT_RE, (_, kind, index) => {
      const pool = kind ? mine : nums;
      let at;
      if (index) at = Number(index);
      else if (kind) {
        at = seq.u;
        seq.u += 1;
      } else {
        at = seq.n;
        seq.n += 1;
      }
      return at < pool.length ? pool[at] : '';
    });
    return line.slice(0, line.length - line.trimStart().length) + out + line.slice(line.trimEnd().length);
  }

  function setLang(lang) {
    i18n.lang = lang === 'uz' ? 'uz' : 'ru';
    document.documentElement.lang = i18n.lang;
  }

  /** Текст элемента — через перевод (вместо прямого el.textContent = …). */
  function setText(el, text) {
    el.textContent = tr(text === null || text === undefined ? '' : text);
  }

  function readConfig() {
    const fallback = { version: '', debug: false };
    const node = document.getElementById('kpi-config');
    if (!node) return fallback;
    try {
      const value = JSON.parse(node.textContent || '');
      return value && typeof value === 'object' ? Object.assign(fallback, value) : fallback;
    } catch (_) {
      return fallback;  // заглушка не подставлена (страница открыта не через сервер)
    }
  }

  // ---------------------------------------------------------------------------------------------
  // 1. DOM-хелперы
  // ---------------------------------------------------------------------------------------------

  const PROP_KEYS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'multiple', 'readOnly']);
  const TEXT_ATTRS = new Set(['aria-label', 'placeholder', 'title', 'alt']);  // подписи: переводятся

  /** h('button', {class, onclick, 'aria-label': …}, 'текст', узел, [массив]) — строки становятся текстом. */
  function h(tag, props) {
    const el = document.createElement(tag);
    setProps(el, props);
    appendKids(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }

  /** То же для SVG (атрибуты — только setAttribute). */
  function s(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const v = attrs[key];
        if (v !== null && v !== undefined && v !== false) el.setAttribute(key, String(v));
      }
    }
    appendKids(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }

  function setProps(el, props) {
    if (!props) return;
    for (const key of Object.keys(props)) {
      const v = props[key];
      if (v === null || v === undefined || v === false) continue;
      if (key === 'class') el.className = v;
      else if (key.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(key.slice(2), v);
      else if (PROP_KEYS.has(key)) el[key] = typeof v === 'string' ? plain(v) : v;
      else if (TEXT_ATTRS.has(key)) el.setAttribute(key, tr(v));
      else el.setAttribute(key, v === true ? '' : String(v));
    }
  }

  function appendKids(el, kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false || kid === '') continue;
      if (Array.isArray(kid)) appendKids(el, kid);
      else if (kid instanceof Node) el.appendChild(kid);
      else el.appendChild(document.createTextNode(keepTogether(tr(kid))));
    }
  }

  /** Неразрывные пробелы в подписях: «100 %», «на 1 дн.», «до 09.10» не рвутся на две строки. */
  function keepTogether(text) {
    if (text.indexOf(' ') < 0) return text;
    return text
      .replace(/(\d) (?=%)/g, '$1 ')
      .replace(/(\d) (?=(?:дн|ч|мин)\.)/g, '$1 ')
      .replace(/(\d) (?=(?:kun|soat|daq)(?![a-z]))/g, '$1 ')
      .replace(/(^|\s)(на|до) (?=\d)/g, '$1$2 ');
  }

  /** Плоский список узлов для replaceChildren: вложенные массивы раскрываются, пустые значения пропускаются. */
  function nodes(value) {
    return [].concat(value).flat(Infinity).filter(Boolean);
  }

  let uidSeq = 0;
  function uid(prefix) {
    uidSeq += 1;
    return prefix + '-' + uidSeq;
  }

  function safely(fn) {
    try {
      return fn();
    } catch (_) {
      return undefined;  // метода нет в этой версии клиента — тихий запасной вариант
    }
  }

  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const coarsePointer = Boolean(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  function scrollBehavior() {
    return reducedMotion && reducedMotion.matches ? 'auto' : 'smooth';
  }

  // ---------------------------------------------------------------------------------------------
  // 1a. Значки: свой набор линейных SVG 24×24 (одна толщина линии, цвет — от текста)
  // ---------------------------------------------------------------------------------------------

  /** Контуры значков (атрибут d одного <path>). Имена — по смыслу в приложении, а не по рисунку. */
  const ICONS = {
    chart: 'M6 20L6 14M12 20L12 4M18 20L18 10',
    tasks: 'M3 6.5l1.5 1.5L7.5 5M3 12.5l1.5 1.5 3-3M3 18.5l1.5 1.5 3-3M11 6.5L21 6.5M11 12.5L21 12.5M11 18.5L21 18.5',
    review: 'M15 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2M10 2h4a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1h-4a1 1 0 0 1-1-1v-2a1 1 0 0 1 1-1zM9 14l2 2 4-4',
    plus: 'M12 5L12 19M5 12L19 12',
    send: 'M21 3L10.5 13.5M21 3l-6.5 18-4-7.5L3 9.5z',
    trend: 'M22 7L13.5 15.5L8.5 10.5L2 17M16 7L22 7L22 13',
    back: 'M15 18L9 12L15 6',
    next: 'M9 18L15 12L9 6',
    x: 'M18 6L6 18M6 6L18 18',
    search: 'M4 11a7 7 0 1 0 14 0a7 7 0 1 0-14 0M20 20L16 16',
    globe: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M3 12L21 12M12 3a13.8 13.8 0 0 1 3.6 9 13.8 13.8 0 0 1-3.6 9 13.8 13.8 0 0 1-3.6-9A13.8 13.8 0 0 1 12 3z',
    refresh: 'M20 11a8 8 0 0 0-14.6-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16M20 20v-4h-4',
    check: 'M20 6L9 17L4 12',
    done: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M8.5 12.2l2.4 2.4 4.6-4.9',
    work: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M10 8.6v6.8l5.4-3.4z',
    clock: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 7L12 12L15.5 14',
    wait: 'M6 3h12M6 21h12M7 3v3.2a5 5 0 0 0 2 4L12 12l3-1.8a5 5 0 0 0 2-4V3M7 21v-3.2a5 5 0 0 1 2-4L12 12l3 1.8a5 5 0 0 1 2 4V21',
    undo: 'M9 14L4 9L9 4M20 20v-7a4 4 0 0 0-4-4H4',
    ban: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M5.7 5.7L18.3 18.3',
    reject: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M15 9L9 15M9 9L15 15',
    inbox: 'M21 12L16 12L14 15L10 15L8 12L3 12M5.9 5.2L3 12v6a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-6l-2.9-6.8A2 2 0 0 0 16.3 4H7.7a2 2 0 0 0-1.8 1.2z',
    alert: 'M10.3 4.2L2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0zM12 9.5L12 13.5M12 17h.01',
    info: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 16.5L12 11.5M12 8h.01',
    flag: 'M5 15s1-1 3.8-1 4.4 2 7.2 2 3-1 3-1V4s-.2 1-3 1-4.4-2-7.2-2S5 4 5 4zM5 22L5 15',
    calendar: 'M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-14a2 2 0 0 1-2-2v-12a2 2 0 0 1 2-2zM16 3L16 7M8 3L8 7M3 10.5L21 10.5',
    user: 'M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2M8.5 7.5a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0',
    users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M5.5 7.5a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M22 21v-2a4 4 0 0 0-3-3.85M15.5 4.2a3.5 3.5 0 0 1 0 6.6',
    boss: 'M5 7.5h14a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-14a2 2 0 0 1-2-2v-8.5a2 2 0 0 1 2-2zM8.5 7.5V6a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v1.5M3 13h18',
    bolt: 'M13 2.5L4.5 13.5H12l-1 8 8.5-11H12z',
    weight: 'M12 4L12 20M7.5 20L16.5 20M4.5 7.5h3c1.6 0 3-.6 4.5-1.6 1.5 1 2.9 1.6 4.5 1.6h3M3 14.5L5.6 8l2.6 6.5a3.4 3.4 0 0 1-5.2 0zM15.8 14.5L18.4 8l2.6 6.5a3.4 3.4 0 0 1-5.2 0z',
    target: 'M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M7 12a5 5 0 1 0 10 0a5 5 0 1 0-10 0M11 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0',
    message: 'M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z',
    clip: 'M20.5 11.3l-8.4 8.4a5.5 5.5 0 0 1-7.8-7.8l8.4-8.4a3.7 3.7 0 0 1 5.2 5.2l-8.4 8.4a1.8 1.8 0 0 1-2.6-2.6l7.8-7.7',
    file: 'M13.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5zM13.5 3L13.5 8.5L19 8.5',
    image: 'M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-14a2 2 0 0 1-2-2v-12a2 2 0 0 1 2-2zM7 9a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M21 15.5L16 10.5L6 20',
    video: 'M4.5 6h9a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zM15.5 10.5l6-3.5v10l-6-3.5',
    mic: 'M12 2.5h0a3 3 0 0 1 3 3v5.5a3 3 0 0 1-3 3h0a3 3 0 0 1-3-3v-5.5a3 3 0 0 1 3-3zM18.5 11a6.5 6.5 0 0 1-13 0M12 17.5L12 21.5M8.5 21.5L15.5 21.5',
    stop: 'M8.5 6.5h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-7a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2z',
    edit: 'M16.5 3.6a2.5 2.5 0 0 1 3.9 3.9L8 20l-4.5 1 1-4.5zM14.5 5.6L18.4 9.5',
    ai: 'M10.5 4.5c.5 4 2.5 6 6.5 6.5-4 .5-6 2.5-6.5 6.5-.5-4-2.5-6-6.5-6.5 4-.5 6-2.5 6.5-6.5zM18.5 15L18.5 20M16 17.5L21 17.5M19 3L19 6M17.5 4.5L20.5 4.5',
    history: 'M3.5 12A8.5 8.5 0 1 0 6 6L3.5 8.5M3.5 3.5L3.5 8.5L8.5 8.5M12 7.5L12 12L15 13.8',
    table: 'M5 4.5h14a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-14a2 2 0 0 1-2-2v-11a2 2 0 0 1 2-2zM3 10L21 10M3 15L21 15M9.5 10L9.5 19.5',
    calc: 'M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-10a2 2 0 0 1-2-2v-14a2 2 0 0 1 2-2zM8.5 7.5L15.5 7.5M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 16.5h.01M12 16.5h.01M15.5 16.5h.01',
    add: 'M8 3.5h8a4.5 4.5 0 0 1 4.5 4.5v8a4.5 4.5 0 0 1-4.5 4.5h-8a4.5 4.5 0 0 1-4.5-4.5v-8a4.5 4.5 0 0 1 4.5-4.5zM12 8.2L12 15.8M8.2 12L15.8 12',
    offline: 'M3 3L21 21M8.5 16.4a5.5 5.5 0 0 1 7 0M5 12.7a10 10 0 0 1 4-2.2M14.5 10.3a10 10 0 0 1 4.5 2.4M2 9a15 15 0 0 1 3.5-2.3M10.5 5.1A15 15 0 0 1 22 9M12 20h.01',
    lock: 'M6.5 11h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM8 11V7.5a4 4 0 0 1 8 0V11',
    phone: 'M9 2.5h6a2.5 2.5 0 0 1 2.5 2.5v14a2.5 2.5 0 0 1-2.5 2.5h-6a2.5 2.5 0 0 1-2.5-2.5v-14a2.5 2.5 0 0 1 2.5-2.5zM12 18h.01',
    adduser: 'M15 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M5 7.5a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M19 8L19 14M16 11L22 11',
    bulb: 'M9 18h6M10 21.5h4M15 14.5c.2-1 .7-1.7 1.5-2.5A5.5 5.5 0 0 0 18 8 6 6 0 0 0 6 8c0 1.4.5 2.8 1.5 4 .8.8 1.3 1.5 1.5 2.5',
    empty: 'M21 19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4.5l2 2.5H19a2 2 0 0 1 2 2z',
    more: 'M4 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M11 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0M18 12a1 1 0 1 0 2 0a1 1 0 1 0-2 0',
    logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17L21 12L16 7M21 12L9 12',
    sort: 'M8 10L12 6L16 10M8 14L12 18L16 14',
    sortup: 'M7 14L12 9L17 14',
    sortdown: 'M7 10L12 15L17 10',
  };

  /** icon('clock') -> <svg class="ic">. cls — размер и тон: ic-sm | ic-lg | ic-xl, tone-good | tone-warn | tone-bad. */
  function icon(name, cls) {
    if (!ICONS[name] && CONFIG.debug && window.console) console.error('unknown icon: ' + name);  // опечатка в имени видна при отладке
    return s('svg', { class: 'ic' + (cls ? ' ' + cls : ''), viewBox: '0 0 24 24', width: 20, height: 20, 'aria-hidden': 'true', focusable: 'false' },
      s('path', { d: ICONS[name] || ICONS.info }));
  }

  /** Значок и подпись кнопки или строки. Менять подпись — тоже здесь: setText стёр бы значок. */
  function setLabel(el, name, text) {
    el.replaceChildren();
    appendKids(el, [name ? icon(name) : null, text]);
  }

  /**
   * Подписи сервера (статус, решение, ошибки, уведомления) начинаются с эмодзи — так они выглядят в чате.
   * В приложении эмодзи убирается (stripLeadingEmoji), а значок того же смысла подбирается по его коду.
   */
  const MARK_ICONS = {
    0x2705: 'done', 0x2714: 'done', 0x26A0: 'alert', 0x274C: 'reject', 0x26D4: 'ban', 0x1F6AB: 'ban',
    0x23F3: 'wait', 0x231B: 'wait', 0x23F1: 'clock', 0x23F0: 'clock', 0x1F4E5: 'inbox', 0x1F4E4: 'send',
    0x1F4DD: 'review', 0x1F3A4: 'mic', 0x1F4CE: 'clip', 0x21A9: 'undo',
  };

  function markIcon(text) {
    return MARK_ICONS[String(text || '').codePointAt(0)] || null;
  }

  // ---------------------------------------------------------------------------------------------
  // 2. Форматирование (числа как fmt_pct / fmt_num в чате, время — местное из настроек)
  // ---------------------------------------------------------------------------------------------

  function isNum(v) {
    return typeof v === 'number' && isFinite(v);
  }

  function pct(v) {
    return isNum(v) ? Math.floor(v + 0.5) + ' %' : '—';
  }

  function fmtNum(v) {
    if (!isNum(v)) return '—';
    let text = (Math.round(v * 100) / 100).toFixed(2).replace(/\.?0+$/, '');
    if (text === '-0') text = '0';
    return text.replace('.', ',');
  }

  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100;
    const b = a % 10;
    const word = a > 10 && a < 20 ? many : b > 1 && b < 5 ? few : b === 1 ? one : many;
    return n + ' ' + word;
  }

  function fileSize(bytes) {
    if (!isNum(bytes)) return '';
    const units = i18n.lang === 'uz' ? [' B', ' KB', ' MB'] : [' Б', ' КБ', ' МБ'];
    if (bytes < 1024) return bytes + units[0];
    if (bytes < MB) return Math.max(1, Math.round(bytes / 1024)) + units[1];
    return fmtNum(Math.round(bytes / MB * 10) / 10) + units[2];
  }

  /** «1 200», «10,5», «110 договоров» -> число (как parse_number в чате, мягче); иначе null. */
  function parseLooseNumber(text) {
    const compact = String(text || '').replace(/[\s  ]/g, '');
    const match = /-?\d+(?:[.,]\d+)?/.exec(compact);
    return match ? Number(match[0].replace(',', '.')) : null;
  }

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  let tzFormatter;
  function tzParts(iso) {
    const date = iso ? new Date(iso) : null;
    if (!date || isNaN(date.getTime())) return null;
    if (tzFormatter === undefined) {
      tzFormatter = safely(() => new Intl.DateTimeFormat('ru-RU', {
        timeZone: cfg().timezone, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      })) || null;
    }
    if (tzFormatter && tzFormatter.formatToParts) {
      const p = {};
      for (const part of tzFormatter.formatToParts(date)) p[part.type] = part.value;
      return { y: p.year, m: p.month, d: p.day, H: p.hour === '24' ? '00' : p.hour, M: p.minute };
    }
    const t = new Date(date.getTime() + 5 * 3600 * 1000);  // Asia/Tashkent: UTC+5, без летнего времени
    return {
      y: String(t.getUTCFullYear()), m: pad2(t.getUTCMonth() + 1), d: pad2(t.getUTCDate()),
      H: pad2(t.getUTCHours()), M: pad2(t.getUTCMinutes()),
    };
  }

  function fmtDmHm(iso) {
    const p = tzParts(iso);
    return p ? p.d + '.' + p.m + ' ' + p.H + ':' + p.M : '—';
  }

  function fmtFull(iso) {
    const p = tzParts(iso);
    return p ? p.d + '.' + p.m + '.' + p.y + ' ' + p.H + ':' + p.M : '—';
  }

  function localDate(iso) {
    const p = tzParts(iso);
    return p ? p.y + '-' + p.m + '-' + p.d : '';
  }

  function localTime(iso) {
    const p = tzParts(iso);
    return p ? p.H + ':' + p.M : '';
  }

  /** «2026-10-08» -> «8 октября (чт)»; год — только если не текущий. */
  function humanDate(ymd) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd || '');
    if (!m) return '';
    const day = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
    const thisYear = state.me && state.me.today ? state.me.today.slice(0, 4) : '';
    const year = m[1] !== thisYear ? ' ' + m[1] : '';
    if (i18n.lang === 'uz') {  // «8-oktabr (pay)», год — впереди: «2027-yil 8-oktabr (pay)»
      return (year ? m[1] + '-yil ' : '') + Number(m[3]) + '-' + MONTHS_UZ[Number(m[2]) - 1] + ' (' + WEEKDAYS_UZ[day.getUTCDay()] + ')';
    }
    return Number(m[3]) + ' ' + MONTHS_GEN[Number(m[2]) - 1] + year + ' (' + WEEKDAYS[day.getUTCDay()] + ')';
  }

  function serverNow() {
    return Date.now() + state.skew;
  }

  function withNotice(text, notice) {
    return notice ? text + '\n' + notice : text;
  }

  /** Текст сервера без эмодзи в начале: «⟨эмодзи⟩ На проверке» -> «На проверке» (значок рисует приложение). */
  function stripLeadingEmoji(text) {
    return String(text || '').replace(/^(?:\p{Extended_Pictographic}|\uFE0F|\u200D)+\s*/u, '');
  }

  // ---------------------------------------------------------------------------------------------
  // 3. Состояние приложения
  // ---------------------------------------------------------------------------------------------

  const state = { me: null, role: null, cfg: Object.assign({}, DEFAULT_CFG), skew: 0, meAt: 0 };
  const auth = { initData: '' };
  let appRoot = null;
  let shell = null;      // каркас с вкладками (только у активного пользователя)
  let current = null;    // активный экран
  let under = null;      // сайт, большой экран: экран списка слева, пока справа открыта панель подробностей
  const nav = { stack: [], current: null };
  const scrollMemo = new Map();

  function cfg() {
    return state.cfg;
  }

  function counts() {
    return (state.me && state.me.counts) || {};
  }

  function isManager() {
    return state.role === 'manager';
  }

  function applyMe(me) {
    // Ответы в кэше написаны на прежнем языке и для прежней роли (язык могли сменить и в чате): после смены — сброс.
    if (state.me && ((me.lang === 'uz' ? 'uz' : 'ru') !== i18n.lang || me.role !== state.role)) dropCache();
    state.me = me;
    state.role = me.role;
    setLang(me.lang);
    state.cfg = Object.assign({}, DEFAULT_CFG, me.config || {});
    const now = Date.parse(me.now);
    if (!isNaN(now)) state.skew = now - Date.now();
    state.meAt = Date.now();
    tzFormatter = undefined;
  }

  // ---------------------------------------------------------------------------------------------
  // 4. Telegram: версия, тема, вибрация, диалоги, MainButton, BackButton, подтверждение закрытия
  // ---------------------------------------------------------------------------------------------

  function tgv(version) {
    return Boolean(insideTelegram && tg.isVersionAtLeast && safely(() => tg.isVersionAtLeast(version)));
  }

  const haptic = {
    ok() { if (tgv('6.1')) safely(() => tg.HapticFeedback.notificationOccurred('success')); },
    err() { if (tgv('6.1')) safely(() => tg.HapticFeedback.notificationOccurred('error')); },
    warn() { if (tgv('6.1')) safely(() => tg.HapticFeedback.notificationOccurred('warning')); },
    sel() { if (tgv('6.1')) safely(() => tg.HapticFeedback.selectionChanged()); },
  };

  const darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function applyTheme() {
    const scheme = insideTelegram && tg.colorScheme ? tg.colorScheme : (darkQuery && darkQuery.matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = scheme === 'dark' ? 'dark' : 'light';
    if (tgv('6.1')) {
      safely(() => tg.setHeaderColor('secondary_bg_color'));
      safely(() => tg.setBackgroundColor('secondary_bg_color'));
    }
    if (tgv('7.10')) safely(() => tg.setBottomBarColor('secondary_bg_color'));
    primary.refresh();
  }

  function confirmDialog(text) {
    const message = tr(text);
    return new Promise((resolve) => {
      if (tgv('6.2')) {
        try {
          tg.showConfirm(message, (ok) => resolve(Boolean(ok)));
          return;
        } catch (_) {
          // другое всплывающее окно ещё открыто — запасной вариант ниже
        }
      }
      resolve(window.confirm(message));
    });
  }

  /** Белый или почти чёрный текст — что контрастнее на фоне #rgb/#rrggbb (WCAG, относительная яркость). */
  function readableTextOn(hex) {
    let raw = String(hex || '').trim().replace(/^#/, '');
    if (/^[0-9a-f]{3}$/i.test(raw)) raw = raw.replace(/./g, '$&$&');
    if (!/^[0-9a-f]{6}$/i.test(raw)) return '#ffffff';
    const lin = (i) => {
      const v = parseInt(raw.slice(i, i + 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    const lum = 0.2126 * lin(0) + 0.7152 * lin(2) + 0.0722 * lin(4);
    return 1.05 / (lum + 0.05) >= (lum + 0.05) / 0.05 ? '#ffffff' : '#1b0c0b';
  }

  function setCssPx(name, px) {
    document.documentElement.style.setProperty(name, Math.max(0, Math.round(px)) + 'px');
  }

  /**
   * Основное действие экрана или листа: primary.set({text, onClick, enabled, progress, danger}, слой).
   * Слой «sheet» перекрывает «screen», пока открыт лист. В Telegram — MainButton (один постоянный
   * обработчик раздаёт нажатия текущему слою), вне Telegram — своя кнопка, закреплённая снизу.
   */
  const primary = (function () {
    const layers = { screen: null, sheet: null };
    const native = Boolean(insideTelegram && tg.MainButton);
    let box = null;
    let button = null;

    function active() {
      return layers.sheet || layers.screen;
    }

    function click() {
      const c = active();
      if (!c || !c.enabled || c.progress || typeof c.onClick !== 'function') return;
      c.onClick();
    }

    function mount() {
      if (native) {
        safely(() => tg.MainButton.offClick(click));
        safely(() => tg.MainButton.onClick(click));
        return;
      }
      button = h('button', { type: 'button', class: 'btn btn-primary', onclick: click });
      box = h('div', { class: 'primary-fallback', hidden: true }, button);
      document.body.appendChild(box);
    }

    function render() {
      const c = active();
      if (native) {
        const mb = tg.MainButton;
        if (!c) {
          safely(() => mb.hideProgress());
          safely(() => mb.hide());
          return;
        }
        const tp = tg.themeParams || {};
        const enabled = Boolean(c.enabled && !c.progress);
        const params = { text: tr(c.progress && c.progressText ? c.progressText : c.text), is_visible: true, is_active: enabled };
        if (enabled) {
          const color = c.danger ? tp.destructive_text_color : tp.button_color;
          if (color) params.color = color;
          // Красный тёмной темы светлый: белый текст на нём нечитаем — цвет текста по яркости фона.
          if (c.danger && color) params.text_color = readableTextOn(color);
          else if (tp.button_text_color) params.text_color = tp.button_text_color;
        } else {
          params.color = tp.hint_color || '#9aa3ad';
          params.text_color = tp.bg_color || '#ffffff';
        }
        safely(() => mb.setParams(params));
        safely(() => (c.progress ? mb.showProgress(false) : mb.hideProgress()));
        return;
      }
      if (!box) return;
      // Большой экран сайта: лист — окно по центру, и его кнопка стоит в нём, а не внизу страницы.
      const panel = layers.sheet && isWide() ? document.querySelector('.sheet') : null;
      const host = panel || document.body;
      if (box.parentNode !== host) host.appendChild(box);
      if (!c) {
        box.hidden = true;
        setCssPx('--primary-h', 0);
        return;
      }
      box.hidden = false;
      box.classList.toggle('over-sheet', Boolean(layers.sheet));
      box.classList.toggle('is-danger', Boolean(c.danger));
      setText(button, c.progress && c.progressText ? c.progressText : c.text);
      button.disabled = !c.enabled || Boolean(c.progress);
      button.setAttribute('aria-busy', String(Boolean(c.progress)));
      setCssPx('--primary-h', box.offsetHeight);
    }

    return {
      mount,
      refresh: render,
      set(config, layer) {
        layers[layer || 'screen'] = config ? Object.assign({ enabled: true, progress: false }, config) : null;
        render();
      },
      update(patch, layer) {
        const target = layers[layer || 'screen'];
        if (!target) return;
        Object.assign(target, patch);
        render();
      },
      hide(layer) {
        layers[layer || 'screen'] = null;
        render();
      },
    };
  }());

  /** BackButton Telegram (6.1+); вне Telegram — ссылка «Назад» в шапке экрана. */
  const back = {
    native: tgv('6.1') && Boolean(tg.BackButton),
    mount() {
      if (!this.native) return;
      safely(() => tg.BackButton.offClick(goBack));
      safely(() => tg.BackButton.onClick(goBack));
    },
    set(visible) {
      if (this.native) safely(() => (visible ? tg.BackButton.show() : tg.BackButton.hide()));
    },
  };

  let closingOn = false;
  let closingFrame = 0;

  function isAnythingDirty() {
    return Boolean(
      (current && current.dirty && current.dirty())
      || sheet.dirty()
      || draftDirty(drafts.newTask)
      || draftDirty(drafts.propose));
  }

  function syncClosing() {
    const dirty = isAnythingDirty();
    if (dirty === closingOn) return;
    closingOn = dirty;
    if (tgv('6.2')) safely(() => (dirty ? tg.enableClosingConfirmation() : tg.disableClosingConfirmation()));
  }

  function syncClosingSoon() {
    if (closingFrame) return;
    closingFrame = requestAnimationFrame(() => {
      closingFrame = 0;
      syncClosing();
    });
  }

  window.addEventListener('beforeunload', (event) => {
    if (insideTelegram || !closingOn) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // ---------------------------------------------------------------------------------------------
  // 5. Тост и полоса «Приложение обновилось»
  // ---------------------------------------------------------------------------------------------

  let toastTimer = 0;

  /** Тост. name — значок («done» после успешного действия); у текста сервера значок берётся по его эмодзи. */
  function toast(text, name) {
    const box = document.getElementById('toast');
    if (!box || !text) return;
    const mark = name || markIcon(text);
    // Строки после первой — пояснения сервера («уведомление не доставлено»): значок остаётся при своей строке.
    const body = String(text).split('\n').map((row, i) => {
      const rowMark = i ? markIcon(row) : null;
      return [i ? '\n' : null, rowMark ? [icon(rowMark, 'ic-sm'), ' '] : null, stripLeadingEmoji(row)];
    });
    const msg = h('div', { class: 'toast-msg' }, mark ? icon(mark) : null, h('span', null, body));
    box.classList.remove('show');
    box.replaceChildren(msg);
    void msg.offsetWidth;  // перезапуск анимации появления
    box.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.remove('show'), Math.min(9000, 2600 + text.length * 40));
  }

  let versionNoticed = false;

  function noteVersion(version) {
    if (!version || !CONFIG.version || version === CONFIG.version || versionNoticed) return;
    versionNoticed = true;
    const bar = h('div', { class: 'banner', role: 'status' },
      icon('refresh'),
      h('span', null, 'Приложение обновилось'),
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => window.location.reload() }, 'Обновить'));
    const slot = shell ? shell.bannerSlot : appRoot;
    if (slot) slot.prepend(bar);
  }

  // ---------------------------------------------------------------------------------------------
  // 6. Клиент API
  // ---------------------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, code, message, ru) {
      super(message);
      this.name = 'ApiError';
      this.ru = ru || message;  // тот же текст по-русски: по нему приложение узнаёт вид ошибки
      this.status = status;
      this.code = code;
      this.handled = false;
    }
  }

  const cache = new Map();     // адрес -> {at, data}
  const inflight = new Map();  // адрес -> Promise (одинаковые GET не дублируются)
  let cacheGen = 0;
  let posting = 0;             // идущие действия (всё, что не GET)
  const whenIdle = [];         // загрузчики, которые ждут их окончания

  function dropCache() {
    cacheGen += 1;
    cache.clear();
    inflight.clear();
  }

  /** Действия закончились. Ответ GET, пришедший во время действия, сервер мог прочитать ещё до него. */
  function settled() {
    return posting ? new Promise((resolve) => whenIdle.push(resolve)) : Promise.resolve();
  }

  function fillPath(template, params) {
    return template.replace(/\{(\w+)\}/g, (_, name) => {
      const value = params ? params[name] : undefined;
      if (value === undefined || value === null || value === '') throw new Error('Не задан параметр пути ' + name);
      return encodeURIComponent(String(value));
    });
  }

  function withQuery(path, query) {
    if (!query) return path;
    const qs = new URLSearchParams();
    for (const key of Object.keys(query)) {
      const v = query[key];
      if (v !== undefined && v !== null && v !== '') qs.set(key, String(v));
    }
    const text = qs.toString();
    return text ? path + '?' + text : path;
  }

  function urlOf(ep, opts) {
    return withQuery(fillPath(ep[1], opts && opts.params), opts && opts.query);
  }

  function peek(ep, opts) {
    const hit = cache.get(urlOf(ep, opts));
    return hit && Date.now() - hit.at < CACHE_TTL_MS ? hit : null;
  }

  /** Прежний ответ (не старше STALE_MAX_MS): его показывают сразу, пока идёт запрос за свежим. Только для загрузчиков. */
  function stale(ep, opts) {
    const hit = cache.get(urlOf(ep, opts));
    return hit && Date.now() - hit.at < STALE_MAX_MS ? hit : null;
  }

  /** api(EP.task, {params: {task_id: 5}, query, body, fresh, timeout}) -> данные ответа или ApiError. */
  function api(ep, opts) {
    const o = opts || {};
    const method = ep[0];
    const url = urlOf(ep, o);
    if (method !== 'GET') {
      if (QUIET.has(ep)) return send(method, url, o.body, o.timeout || POST_TIMEOUT_MS);
      posting += 1;
      return send(method, url, o.body, o.timeout || POST_TIMEOUT_MS).finally(() => {
        posting -= 1;
        // Ждущих будим отдельной задачей: сначала тот, кто отправлял, сбросит кэш (afterMutation).
        if (!posting) setTimeout(() => whenIdle.splice(0).forEach((resolve) => resolve()), 0);
      });
    }
    if (!o.fresh) {
      const hit = cache.get(url);
      if (hit && Date.now() - hit.at < CACHE_TTL_MS) return Promise.resolve(hit.data);
    }
    if (inflight.has(url)) return inflight.get(url);
    const gen = cacheGen;
    const promise = send('GET', url, undefined, o.timeout || GET_TIMEOUT_MS)
      .then((data) => {
        if (gen === cacheGen) cache.set(url, { at: Date.now(), data });
        return data;
      }, (err) => {
        if (err.status === 403 || err.status === 404) cache.delete(url);  // прежний ответ больше не показывать
        throw err;
      })
      .finally(() => {
        if (inflight.get(url) === promise) inflight.delete(url);
      });
    inflight.set(url, promise);
    return promise;
  }

  /** Знак страницы сайта: сервер кладёт его в cookie при входе, страница возвращает в заголовке (до входа — «1»). */
  function pageToken() {
    const found = /(?:^|;\s*)(?:__Host-)?kpi_csrf=([^;]+)/.exec(document.cookie || '');
    return found ? found[1] : '1';
  }

  /** Кто пришёл: Mini App — подпись Telegram; сайт — знак страницы (человека называет cookie сессии). */
  function authHeaders() {
    return WEB ? { 'X-KPI-CSRF': pageToken() } : { 'X-Telegram-Init-Data': auth.initData };
  }

  /** raw — ошибку вернуть как есть, без общих реакций routeError (запросы входа на сайт). */
  async function send(method, url, body, timeoutMs, raw) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (ctrl) ctrl.abort();
    }, timeoutMs);
    const headers = Object.assign({ Accept: 'application/json' }, authHeaders());
    const init = { method, headers, credentials: 'same-origin', cache: 'no-store' };
    if (ctrl) init.signal = ctrl.signal;
    if (typeof Blob === 'function' && body instanceof Blob) {
      // Запись с микрофона уходит как есть: тип — тот, что дал MediaRecorder (audio/webm, audio/mp4…).
      headers['Content-Type'] = body.type || 'application/octet-stream';
      init.body = body;
    } else if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let resp;
    let data = null;
    try {
      resp = await fetch(url, init);
      noteVersion(resp.headers.get('X-App-Version'));
      data = await resp.json().catch(() => null);
    } catch (_) {
      throw new ApiError(0, timedOut ? 'timeout' : 'network', timedOut ? T.timeout : T.network);
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok || data === null) {
      const err = new ApiError(resp.status, (data && data.code) || 'internal', (data && data.error) || T.generic, data && data.ru);
      throw raw ? err : routeError(err);
    }
    return data;
  }

  /** Сдача результата: XMLHttpRequest ради прогресса загрузки (§11.5). */
  function upload(ep, params, form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(ep[0], fillPath(ep[1], params));
      const who = authHeaders();
      Object.keys(who).forEach((name) => xhr.setRequestHeader(name, who[name]));
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.timeout = UPLOAD_TIMEOUT_MS;
      if (xhr.upload && onProgress) {
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total);
        };
      }
      xhr.onload = () => {
        noteVersion(xhr.getResponseHeader('X-App-Version'));
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch (_) {
          data = null;
        }
        if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
        else reject(routeError(new ApiError(xhr.status, (data && data.code) || 'internal', (data && data.error) || T.generic, data && data.ru)));
      };
      xhr.onerror = () => reject(new ApiError(0, 'network', T.network));
      xhr.ontimeout = () => reject(new ApiError(0, 'timeout', T.uploadTimeout));
      xhr.send(form);
    });
  }

  /** Общие реакции на ошибки: 401 — «сессия устарела», 403 доступа — экран доступа, 429 — тост. */
  function routeError(err) {
    if (err.status === 401) {
      err.handled = true;
      // Сайт: сессии нет или она закончилась — экран входа (первый приход — без пояснений). Если он уже
      // показан, опоздавший ответ прежнего запроса его не сбрасывает (человек может как раз подтверждать вход).
      if (!WEB) showBlocking('session', err.message);
      else if (!loginOn) showLogin(err.code === 'web_session' ? 'ended' : null);
    } else if (err.status === 403 && ACCESS_CODES.indexOf(err.code) >= 0) {
      err.handled = true;
      showBlocking(err.code, err.message);
    } else if (err.status === 429) {
      err.handled = true;
      haptic.warn();
      toast(err.message, 'alert');
    }
    return err;
  }

  function invalidate(prefixes) {
    cacheGen += 1;
    inflight.clear();  // запрос, начатый до изменения, не должен достаться тому, кто придёт после
    for (const key of Array.from(cache.keys())) {
      if (prefixes.some((p) => key === p || key.indexOf(p) === 0)) cache.delete(key);
    }
  }

  /** Неизменная часть шаблона пути до первого параметра (для userKpi — всё до {user_id}). */
  function routePrefix(ep) {
    return ep[1].split('{')[0];
  }

  /** После любого изменения: сбросить связанные списки и обновить счётчики вкладок. */
  function afterMutation() {
    // '/api/tasks' покрывает и списки, и карточки задач; список сотрудников (/api/employees) не меняется.
    invalidate([EP.tasks[1], EP.review[1], EP.proposals[1], EP.dashboard[1], routePrefix(EP.userKpi), routePrefix(EP.weightLoad)]);
    scheduleMeRefresh();
    if (under && under.refresh) under.refresh();  // список слева от панели подробностей — сразу в новом виде
  }

  function reportError(err) {
    if (err.handled) return;
    haptic.err();
    toast(err.message || T.generic, 'alert');
  }

  let meTimer = 0;

  function scheduleMeRefresh() {
    clearTimeout(meTimer);
    meTimer = setTimeout(async () => {
      try {
        const me = await api(EP.me, { fresh: true });
        if (!me || me.access !== 'active') {
          showBlocking(me ? me.access : 'session', me && me.message ? me.message : T.generic);
          return;
        }
        const roleChanged = me.role !== state.role;
        applyMe(me);
        if (roleChanged) {
          buildShell();
          go(startHash(), { force: true, reset: true });
          return;
        }
        renderTabbar();
        if (current && current.onCounts) current.onCounts();
        if (under && under.onCounts) under.onCounts();
      } catch (_) {
        // бейджи обновятся при следующем действии
      }
    }, 250);
  }

  // ---------------------------------------------------------------------------------------------
  // 7. Лист (bottom sheet)
  // ---------------------------------------------------------------------------------------------

  const sheet = (function () {
    let open = null;

    function focusables(panel) {
      return Array.from(panel.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'))
        .filter((n) => !n.disabled && n.offsetParent !== null);
    }

    function onKey(event) {
      if (!open) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        requestClose();
      } else if (event.key === 'Tab') {
        const list = focusables(open.panel);
        if (!list.length) return;
        const first = list[0];
        const last = list[list.length - 1];
        if (event.shiftKey && (document.activeElement === first || !open.panel.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    function show(opts) {
      close(true);
      const titleId = uid('sheet-title');
      const title = h('h2', { id: titleId, tabindex: '-1' }, opts.title);
      const panel = h('section', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId },
        h('div', { class: 'sheet-head' },
          title,
          h('button', { type: 'button', class: 'sheet-close', 'aria-label': 'Закрыть', onclick: () => requestClose() }, icon('x'))),
        opts.body);
      const backdrop = h('div', { class: 'sheet-backdrop' }, panel);
      backdrop.addEventListener('click', (event) => {
        if (event.target === backdrop) requestClose();
      });
      open = { backdrop, panel, dirty: opts.dirty, onClose: opts.onClose, opener: document.activeElement };
      document.body.appendChild(backdrop);
      document.body.classList.add('sheet-open');
      document.addEventListener('keydown', onKey, true);
      if (opts.primary) primary.set(opts.primary, 'sheet');
      updateBack();
      requestAnimationFrame(() => safely(() => title.focus({ preventScroll: true })));
      syncClosing();
    }

    function close(silent) {
      if (!open) return;
      const was = open;
      open = null;
      document.removeEventListener('keydown', onKey, true);
      was.backdrop.remove();
      document.body.classList.remove('sheet-open');
      primary.hide('sheet');
      updateBack();
      syncClosing();
      if (!silent && was.opener && was.opener.isConnected) safely(() => was.opener.focus({ preventScroll: true }));
      if (was.onClose) was.onClose();
    }

    async function requestClose() {
      if (!open) return;
      if (open.dirty && open.dirty() && !(await confirmDialog(T.leave))) return;
      close(false);
    }

    return {
      show,
      close,
      requestClose,
      isOpen: () => Boolean(open),
      dirty: () => Boolean(open && open.dirty && open.dirty()),
    };
  }());

  // ---------------------------------------------------------------------------------------------
  // 8. Маршрутизация
  // ---------------------------------------------------------------------------------------------

  const TABS = {
    manager: [
      { id: 'team', hash: '#/team', icon: 'chart', label: 'Команда' },
      { id: 'tasks', hash: '#/tasks', icon: 'tasks', label: 'Задачи' },
      { id: 'review', hash: '#/review', icon: 'review', label: 'Проверка', badge: (c) => (c.review || 0) + (c.proposals || 0) },
      { id: 'new', hash: '#/new', icon: 'add', label: 'Новая' },
    ],
    employee: [
      { id: 'my', hash: '#/my', icon: 'tasks', label: 'Мои задачи', badge: (c) => (c.unaccepted || 0) + (c.rework || 0) },
      { id: 'submit', hash: '#/submit', icon: 'send', label: 'Сдать', badge: (c) => c.overdue || 0 },
      { id: 'kpi', hash: '#/kpi', icon: 'trend', label: 'Мой KPI' },
      { id: 'propose', hash: '#/propose', icon: 'add', label: 'Поручение' },
    ],
  };

  // pane — сайт, большой экран: вложенные экраны этой вкладки (карточка, проверка, сдача) открываются панелью
  // справа от списка, а не вместо него.
  const ROUTES = [
    { pat: /^\/team$/, role: 'manager', tab: 'team', root: true, view: TeamView },
    { pat: /^\/team\/user\/(\d+)$/, role: 'manager', tab: 'team', view: UserCardView },
    { pat: /^\/tasks$/, role: 'manager', tab: 'tasks', root: true, pane: true, view: TasksView },
    { pat: /^\/task\/(\d+)$/, role: '*', view: TaskView },
    { pat: /^\/task\/(\d+)\/edit$/, role: 'manager', view: EditTaskView },
    { pat: /^\/review$/, role: 'manager', tab: 'review', root: true, pane: true, view: ReviewQueueView },
    { pat: /^\/review\/(\d+)$/, role: 'manager', tab: 'review', view: ReviewView },
    { pat: /^\/proposal\/(\d+)$/, role: 'manager', tab: 'review', view: ProposalView },
    { pat: /^\/new$/, role: 'manager', tab: 'new', root: true, view: NewTaskView },
    { pat: /^\/my$/, role: 'employee', tab: 'my', root: true, pane: true, view: MyTasksView },
    { pat: /^\/submit$/, role: 'employee', tab: 'submit', root: true, pane: true, view: SubmitListView },
    { pat: /^\/submit\/(\d+)$/, role: 'employee', tab: 'submit', view: SubmitFormView },
    { pat: /^\/kpi$/, role: 'employee', tab: 'kpi', root: true, view: MyKpiView },
    { pat: /^\/propose$/, role: 'employee', tab: 'propose', root: true, view: ProposeView },
  ];

  function roleKey() {
    return isManager() ? 'manager' : 'employee';
  }

  function startHash() {
    return isManager() ? '#/team' : '#/my';
  }

  function defaultTab() {
    return isManager() ? 'tasks' : 'my';
  }

  function tabRoot(tabId) {
    const tab = TABS[roleKey()].find((t) => t.id === tabId);
    return tab ? tab.hash : startHash();
  }

  function resolve(hash) {
    const raw = String(hash || '').replace(/^#/, '');
    if (raw.charAt(0) !== '/') return null;  // в том числе #tgWebAppData=… при запуске из Telegram
    const q = raw.indexOf('?');
    const path = q < 0 ? raw : raw.slice(0, q);
    const query = new URLSearchParams(q < 0 ? '' : raw.slice(q + 1));
    for (const route of ROUTES) {
      if (route.role !== '*' && route.role !== roleKey()) continue;
      const match = route.pat.exec(path);
      if (!match) continue;
      const qs = query.toString();
      return { hash: '#' + path + (qs ? '?' + qs : ''), route, args: match.slice(1), query };
    }
    return null;
  }

  async function canLeave() {
    const guard = current && current.guard ? current.guard() : false;
    if (!guard) return true;
    return confirmDialog(typeof guard === 'string' ? guard : T.leave);
  }

  /** Переход: go('#/task/5'); {reset} — начать стек заново, {replace} — заменить верхний экран. */
  async function go(hash, opts) {
    const o = opts || {};
    if (!shell) return false;  // показан экран доступа / «сессия устарела» — переходов нет
    const target = resolve(hash) || resolve(startHash());
    if (!o.force && !(await canLeave())) return false;
    if (!shell) return false;
    if (target.route.root || o.reset) nav.stack = [target.hash];
    else if (o.replace && nav.stack.length) nav.stack[nav.stack.length - 1] = target.hash;
    else {
      if (o.fromList && under) nav.stack.length = 1;  // другая строка списка меняет подробности, а не копит их
      nav.stack.push(target.hash);
    }
    if (o.reset && !target.route.root && hasPane()) {
      // Прямая ссылка на карточку: слева — список её вкладки, карточка — в панели.
      const root = resolve(tabRoot(target.route.tab || defaultTab()));
      if (root && root.route.pane) nav.stack = [root.hash, target.hash];
    }
    render(target, o);
    return true;
  }

  async function goBack() {
    if (sheet.isOpen()) {
      sheet.requestClose();
      return;
    }
    if (!shell || !nav.current) return;
    if (!(await canLeave())) return;
    if (nav.stack.length > 1) {
      nav.stack.pop();
      render(resolve(nav.stack[nav.stack.length - 1]) || resolve(startHash()), { back: true });
      return;
    }
    const root = tabRoot(nav.current.tab);
    if (nav.current.hash !== root) {
      nav.stack = [root];
      render(resolve(root), { back: true });
    }
  }

  /** Записать маршрут в адрес без новой записи истории (в ограниченных WebView вызов может быть запрещён). */
  function setHash(hash) {
    safely(() => history.replaceState(null, '', hash));
  }

  /** Обновить параметры адреса текущего экрана без перерисовки (период карточки и т. п.). */
  function replaceQuery(query) {
    if (!nav.current) return;
    if (under) {
      // Фильтр меняют в списке слева, пока справа открыты подробности: адрес списка — первая запись стека.
      const list = resolve(withQuery(under.target.hash.split('?')[0], query));
      if (!list) return;
      list.tab = under.target.tab;
      under.target = list;
      nav.stack[0] = list.hash;
      return;
    }
    const base = nav.current.hash.split('?')[0];
    const target = resolve(withQuery(base, query));
    if (!target) return;
    target.tab = nav.current.tab;
    nav.current = target;
    nav.stack[nav.stack.length - 1] = target.hash;
    if (current) current.target = target;
    setHash(target.hash);
  }

  function render(target, opts) {
    const o = opts || {};
    if (!shell || !target) return;
    // Сайт, большой экран: вложенный экран вкладки со списком открывается панелью справа от списка.
    const root = !target.route.root && hasPane() && nav.stack.length > 1 ? resolve(nav.stack[0]) : null;
    const paned = Boolean(root && root.route.pane);
    if (!paned && under && under.target.hash === target.hash) {
      closeDetail();  // «Назад» из панели: список слева остаётся как был
      return;
    }
    if (nav.current && !under) scrollMemo.set(nav.current.hash, window.scrollY);
    // Список слева уже на экране (он под панелью или только что был текущим) — остаётся как есть.
    const list = paned ? under || (current && current.alive && current.target.route.root ? current : null) : null;
    const keep = Boolean(list && list.target.hash === root.hash);
    if (keep) {
      if (current && current !== list) current.destroy();
      under = list;
    } else dropScreens();
    sheet.close(true);
    primary.hide('screen');
    if (paned && !keep) {
      root.tab = root.route.tab || defaultTab();
      under = mountScreen(root, shell.main);
      window.scrollTo(0, 0);
    }
    target.tab = paned ? under.target.tab : target.route.tab || (nav.current && nav.current.tab) || defaultTab();
    setDetail(paned);
    nav.current = target;
    if (window.location.hash !== target.hash) setHash(target.hash);
    mountScreen(target, paned ? shell.detail : shell.main);
    if (paned) shell.detail.scrollTop = 0;
    else window.scrollTo(0, o.back ? scrollMemo.get(target.hash) || 0 : 0);
    const heading = current.el.querySelector('h1');
    if (heading) safely(() => heading.focus({ preventScroll: true }));
    markOpenRows();
    syncClosing();
  }

  /** Создать экран target в контейнере host и сделать его текущим. */
  function mountScreen(target, host) {
    const scr = createScreen(target);
    current = scr;
    host.replaceChildren(scr.el);
    renderTabbar();
    updateBack();
    mounting = target;
    try {
      target.route.view(scr, target.args, target.query);
    } catch (err) {
      if (window.console) console.error(err);
      scr.el.append(errorState(new ApiError(0, 'internal', T.generic), () => render(target)));
    } finally {
      mounting = null;
    }
    return scr;
  }

  let mounting = null;  // экран, который строится прямо сейчас (его шапка спрашивает, вложенный ли он)

  /** У экрана есть «Назад»? Список слева от панели подробностей — главный экран вкладки, у него «Назад» нет. */
  function isNested() {
    const t = mounting || nav.current;
    if (!t) return false;
    if (!t.route.root) return true;
    return nav.stack.length > 1 && !(hasPane() && t.route.pane && t.hash === nav.stack[0]);
  }

  function dropScreens() {
    if (current) current.destroy();
    if (under && under !== current) under.destroy();
    under = null;
    current = null;  // экран уничтожается один раз: следующий вызов (buildShell, затем render) его уже не тронет
  }

  /** Показать или убрать панель подробностей (она есть только на сайте). */
  function setDetail(on) {
    if (!shell.detail) return;
    shell.detail.hidden = !on;
    document.body.classList.toggle('has-detail', on);
    if (!on) shell.detail.replaceChildren();
  }

  /** Панель подробностей закрыта: слева тот же список — с той же прокруткой и фильтрами. */
  function closeDetail() {
    const list = under;
    if (current && current !== list) current.destroy();
    under = null;
    current = list;
    sheet.close(true);
    primary.hide('screen');
    setDetail(false);
    nav.current = list.target;
    setHash(nav.current.hash);
    renderTabbar();
    updateBack();
    markOpenRows();
    syncClosing();
    if (list.refresh && Date.now() - list.loadedAt > STALE_MS) list.refresh();
  }

  /** Строка списка, чьи подробности открыты в панели, — выделена. */
  function markOpenRows() {
    if (!shell || !shell.detail) return;
    const open = under && nav.current ? nav.current.hash : '';
    shell.main.querySelectorAll('[data-to]').forEach((el) => {
      el.closest('li, tr').classList.toggle('is-open', Boolean(open) && el.getAttribute('data-to') === open);
    });
  }

  /** Переход из строки списка или таблицы. Строка слева от открытой панели меняет подробности в ней. */
  function openRow(event, to) {
    const el = event.currentTarget;
    go(to, { fromList: Boolean(under && shell && shell.detail && !shell.detail.contains(el)) });
  }

  function updateBack() {
    const nested = Boolean(nav.current && (!nav.current.route.root || nav.stack.length > 1));
    back.set(Boolean(shell) && (sheet.isOpen() || nested));
  }

  function createScreen(target) {
    const timers = new Set();
    const cleanups = [];
    const scr = {
      target,
      alive: true,
      loadedAt: 0,
      el: h('div', { class: 'screen' }),
      refresh: null,   // перечитать данные (вкладка нажата повторно, возврат в приложение)
      dirty: null,     // есть несохранённые изменения -> подтверждение закрытия
      guard: null,     // уход «назад» требует подтверждения (true или текст вопроса)
      onCounts: null,  // обновились счётчики /api/me
      later(fn, ms) {
        const id = setTimeout(() => {
          timers.delete(id);
          if (scr.alive) fn();
        }, ms);
        timers.add(id);
        return id;
      },
      cancel(id) {
        clearTimeout(id);
        timers.delete(id);
      },
      onDestroy(fn) {
        cleanups.push(fn);
      },
      destroy() {
        scr.alive = false;
        timers.forEach(clearTimeout);
        timers.clear();
        cleanups.forEach((fn) => safely(fn));
      },
    };
    return scr;
  }

  window.addEventListener('hashchange', () => {
    if (!shell || !nav.current || window.location.hash === nav.current.hash) return;
    const target = resolve(window.location.hash);
    const previous = nav.current.hash;
    if (!target) {
      setHash(previous);
      return;
    }
    go(target.hash).then((ok) => {
      if (!ok) setHash(previous);
    });
  });

  // ---------------------------------------------------------------------------------------------
  // 9. Каркас: вкладки, полоса обновления, нижняя панель
  // ---------------------------------------------------------------------------------------------

  let dock = null;
  let dockObserver = null;

  function measureDock() {
    // На большом экране сайта панель разделов стоит сбоку и места снизу не занимает.
    setCssPx('--dock-h', dock && dock.isConnected && !isWide() ? dock.offsetHeight : 0);
  }

  function buildShell() {
    const bannerSlot = h('div', { class: 'banner-slot' });
    const main = h('main', { id: 'main', class: 'main' });
    // Сайт: панель подробностей (большой экран) и боковое меню — название, разделы, человек, язык, выход.
    // На узком экране название и подвал меню скрыты стилями: остаётся нижняя панель вкладок, как в Mini App.
    const detail = WEB ? h('aside', { class: 'detail', hidden: true, 'aria-label': 'Подробности' }) : null;
    const tabbar = h('div', { class: 'tabbar' });
    const user = (state.me && state.me.user) || {};
    if (dock) dock.remove();
    dropScreens();
    dock = h('nav', { class: 'dock', 'aria-label': 'Разделы' },
      WEB ? h('div', { class: 'side-brand' }, icon('chart', 'ic-lg'), h('span', null, own(appName()))) : null,
      tabbar,
      WEB ? h('div', { class: 'side-foot' },
        h('div', { class: 'side-user' },
          h('b', null, own(user.short_name || user.full_name)),
          h('span', null, user.position ? own(user.position) : isManager() ? 'Начальник' : 'Сотрудник')),
        langButton(), logoutButton()) : null);
    appRoot.replaceChildren.apply(appRoot, nodes([bannerSlot, main, detail]));
    appRoot.removeAttribute('aria-busy');
    document.body.classList.remove('has-detail');
    document.body.appendChild(dock);
    shell = { bannerSlot, main, tabbar, detail };
    if (typeof ResizeObserver === 'function') {
      if (!dockObserver) dockObserver = new ResizeObserver(measureDock);
      dockObserver.disconnect();
      dockObserver.observe(dock);
    }
    measureDock();
  }

  function renderTabbar() {
    if (!shell) return;
    const c = counts();
    shell.tabbar.replaceChildren.apply(shell.tabbar, TABS[roleKey()].map((tab) => {
      const n = tab.badge ? tab.badge(c) : 0;
      const isCurrent = Boolean(nav.current && nav.current.tab === tab.id);
      return h('button', {
        type: 'button', class: 'tab', 'aria-current': isCurrent ? 'page' : null,
        onclick: () => onTab(tab),
      },
      h('span', { class: 't-ico' }, icon(tab.icon, 'ic-lg')),
      h('span', { class: 't-label' }, tab.label),
      n > 0 ? h('span', { class: 'badge', 'aria-hidden': 'true' }, n > 99 ? '99+' : String(n)) : null,
      n > 0 ? h('span', { class: 'sr-only' }, ', требуют внимания: ' + n) : null);
    }));
  }

  function onTab(tab) {
    haptic.sel();
    if (nav.current && nav.current.tab === tab.id && nav.current.route.root) {
      window.scrollTo({ top: 0, behavior: scrollBehavior() });
      if (current && current.refresh) current.refresh();
      scheduleMeRefresh();
      return;
    }
    go(tab.hash, { reset: true });
  }

  // ---------------------------------------------------------------------------------------------
  // 10. Общие элементы интерфейса
  // ---------------------------------------------------------------------------------------------

  function appName() {
    return i18n.lang === 'uz' ? 'Samaradorlik' : 'Эффективность';
  }

  function setDocTitle(title) {
    document.title = title ? tr(title) + ' · ' + appName() : appName();
  }

  /** Кнопка языка на главных экранах вкладок: одно нажатие — другой язык (и в приложении, и в чате). */
  function langButton() {
    const next = i18n.lang === 'uz' ? 'ru' : 'uz';
    const btn = h('button', { type: 'button', class: 'lang-btn', lang: next, 'aria-label': next === 'uz' ? 'Oʻzbek tiliga oʻtish' : 'Перейти на русский язык' },
      icon('globe', 'ic-sm'), own(next === 'uz' ? 'Oʻzbekcha' : 'Русский'));
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      haptic.sel();
      try {
        await api(EP.setLang, { body: { lang: next } });
        dropCache();
        setLang(next);
        const me = await api(EP.me, { fresh: true });
        if (me && me.access === 'active') applyMe(me);
        if (shell && nav.current) {
          buildShell();
          render(nav.current);
        }
      } catch (err) {
        btn.disabled = false;
        reportError(err);
      }
    });
    return btn;
  }

  /** Шапка экрана: заголовок h1 (на него переходит фокус), подзаголовок, «Назад» вне Telegram; actions — кнопки справа от заголовка. */
  function screenHead(title, sub, actions) {
    setDocTitle(title);
    const nested = isNested();
    const h1 = h('h1', { tabindex: '-1' }, title);
    const subEl = h('p', { class: 'sub', hidden: !sub }, sub || '');
    const el = h('header', { class: 'screen-head' },
      nested && !back.native ? h('button', { type: 'button', class: 'back-link', onclick: () => goBack() },
        under && nav.stack.length === 2 ? [icon('x'), 'Закрыть'] : [icon('back'), 'Назад']) : null,
      h('div', { class: 'head-row' }, h1, actions || null, nested ? null : langButton(), WEB && !nested ? logoutButton() : null),
      subEl);
    return {
      el,
      setTitle(text) {
        setText(h1, text);
        setDocTitle(text);
      },
      setSub(text) {
        setText(subEl, text || '');
        subEl.hidden = !text;
      },
    };
  }

  function sec(text, extra) {
    return h('h2', { class: 'sec' }, h('span', null, text), extra ? h('span', { class: 'sec-extra' }, extra) : null);
  }

  function sk(cls) {
    return h('div', { class: 'skel ' + cls, 'aria-hidden': 'true' });
  }

  function skelList(n) {
    const rows = [];
    for (let i = 0; i < n; i += 1) {
      rows.push(h('div', { class: 'sk-row' }, h('div', { class: 'sk-lines' }, sk('sk-line w70'), sk('sk-line w40'))));
    }
    return h('div', { class: 'list', 'aria-hidden': 'true' }, rows);
  }

  function skelHero() {
    return h('div', { class: 'card hero', 'aria-hidden': 'true' }, sk('sk-big'), sk('sk-line w60'), h('div', { class: 'mt12' }, sk('sk-line w90')));
  }

  function skelDashboard() {
    return [
      skelHero(),
      skelList(4),
      sk('sk-block skel-on-page'),
    ];
  }

  function skelCard() {
    return [
      h('div', { class: 'card', 'aria-hidden': 'true' }, sk('sk-line w90'), h('div', { class: 'mt8' }, sk('sk-line w60')), h('div', { class: 'mt12' }, sk('sk-line w40'))),
      h('div', { class: 'card', 'aria-hidden': 'true' }, sk('sk-line w40'), h('div', { class: 'mt8' }, sk('sk-line w90')), h('div', { class: 'mt8' }, sk('sk-line w70'))),
      skelList(2),
    ];
  }

  function skelForm() {
    const block = () => h('div', { class: 'field', 'aria-hidden': 'true' }, sk('sk-line w30'), h('div', { class: 'mt8' }, sk('sk-block skel-on-page sk-input')));
    return [block(), block(), block()];
  }

  /** Пустое состояние: крупный значок (name из ICONS), пояснение и — если есть честный следующий шаг — кнопка action. */
  function emptyState(text, name, action) {
    return h('div', { class: 'state' }, h('div', { class: 'state-ico' }, icon(name || 'empty', 'ic-xl')), h('p', null, text), action);
  }

  function errorState(err, retry) {
    const offline = err.status === 0;
    const final = err.status === 404 || err.status === 403;
    return h('div', { class: 'state state-error', role: 'alert' },
      h('div', { class: 'state-ico' }, icon(offline ? 'offline' : err.status === 404 ? 'search' : 'alert', 'ic-xl')),
      h('p', null, stripLeadingEmoji(err.message || T.generic)),
      final
        ? button('Назад', () => goBack(), '', null, 'back')
        : retry ? button(T.retry, retry, 'btn-primary', null, 'refresh') : null);
  }

  const NOTE_ICONS = { info: 'info', warn: 'alert', bad: 'alert', good: 'done' };

  /** Плашка-пояснение: значок по тону (или name), текст, под ним — extra (кнопка, комментарий). */
  function note(text, tone, extra, name) {
    const mark = name || (typeof text === 'string' && markIcon(text)) || NOTE_ICONS[tone] || 'info';
    return h('div', { class: 'note' + (tone ? ' ' + tone : '') }, icon(mark),
      h('div', { class: 'note-body' }, typeof text === 'string' ? stripLeadingEmoji(text) : text, extra || null));
  }

  /** Метка статуса: цвет по тону (good | warn | bad | info), значок name или точка. */
  function pill(text, tone, name) {
    return h('span', { class: 'pill' + (tone ? ' ' + tone : '') },
      name ? icon(name, 'ic-sm') : h('span', { class: 'dot', 'aria-hidden': 'true' }),
      h('span', { class: 'pill-t' }, stripLeadingEmoji(text)));  // подпись метки — всегда с заглавной (стили)
  }

  /** Строка «подпись: значение»; name — значок слева (текст рядом с ним переносится ровным столбцом). */
  function line(label, value, cls, name) {
    const text = [label ? h('span', { class: 'muted' }, label + ' ') : null, value];
    return h('p', { class: 'line' + (name ? ' has-ic' : '') + (cls ? ' ' + cls : '') }, name ? [icon(name, 'ic-sm'), h('span', null, text)] : text);
  }

  function kvBlock(label, value, extra) {
    // value — слова пользователя (результат, факт, комментарий): перевод их не трогает.
    return h('div', { class: 'kvb' }, h('div', { class: 'kvb-k' }, label), h('div', { class: 'kvb-v pre' }, typeof value === 'string' ? own(value) : value), extra || null);
  }

  /** Кнопка; name — значок перед подписью (подпись такой кнопки меняет setLabel, а не setText). */
  function button(text, onClick, cls, attrs, name) {
    return h('button', Object.assign({ type: 'button', class: 'btn' + (cls ? ' ' + cls : ''), onclick: onClick }, attrs || {}),
      name ? icon(name) : null, text);
  }

  function chip(content, pressed, onClick, ariaLabel) {
    return h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(Boolean(pressed)), 'aria-label': ariaLabel || null,
      onclick: () => {
        haptic.sel();
        onClick();
      },
    }, content);
  }

  /** Сегменты: kind 'tab' — переключение вида (tablist), 'radio' — выбор значения в форме. */
  function segmented(items, value, onChange, label, kind) {
    const isRadio = kind === 'radio';
    let currentValue = value;
    const btns = items.map((item) => h('button', {
      type: 'button', role: isRadio ? 'radio' : 'tab',
      onclick: () => pick(item.value, true),
    }, item.tone ? h('span', { class: 'dot ' + item.tone, 'aria-hidden': 'true' }) : null, item.label));
    const el = h('div', { class: 'seg', role: isRadio ? 'radiogroup' : 'tablist', 'aria-label': label }, btns);
    el.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      event.preventDefault();
      const i = items.findIndex((it) => it.value === currentValue);
      const next = (i + (event.key === 'ArrowRight' ? 1 : items.length - 1)) % items.length;
      pick(items[next].value, true);
      btns[next].focus();
    });
    function paint() {
      btns.forEach((b, i) => {
        const on = items[i].value === currentValue;
        b.setAttribute(isRadio ? 'aria-checked' : 'aria-selected', String(on));
        b.tabIndex = on ? 0 : -1;
      });
    }
    function pick(v, byUser) {
      if (byUser && v === currentValue) return;
      currentValue = v;
      paint();
      if (byUser) {
        haptic.sel();
        onChange(v);
      }
    }
    paint();
    return {
      el,
      set: (v) => pick(v, false),
      setLabel(v, text) {
        const i = items.findIndex((it) => it.value === v);
        if (i >= 0) setText(btns[i], text);
      },
    };
  }

  // --- Графики (§11.7) ------------------------------------------------------------------------

  function toneOf(kpi) {
    return kpi >= 100 ? 'good' : kpi >= 80 ? 'warn' : 'bad';
  }

  /** Единая шкала полос на экране: 0 … max(120, ⌈max KPI / 10⌉ × 10). */
  function scaleMax(values) {
    const top = Math.max.apply(null, [0].concat(values.filter(isNum)));
    return Math.max(120, Math.ceil(top / 10) * 10);
  }

  /** Полоса KPI; decorative — число уже написано рядом в той же строке (полоса скрыта от чтения с экрана). */
  function kpiBar(kpi, max, text, decorative) {
    const svg = s('svg', decorative
      ? { class: 'kbar', width: '100%', height: '10', 'aria-hidden': 'true', focusable: 'false' }
      : { class: 'kbar', width: '100%', height: '10', role: 'img', 'aria-label': 'KPI: ' + own(text || pct(kpi)) + ', отметка — 100 %', focusable: 'false' });
    svg.appendChild(s('rect', { class: 'kbar-track', x: 0, y: 0, width: '100%', height: 10, rx: 5 }));
    if (isNum(kpi) && kpi > 0) {
      const width = Math.min(100, kpi / max * 100);
      svg.appendChild(s('rect', { class: 'kbar-fill ' + toneOf(kpi), x: 0, y: 0, width: width.toFixed(2) + '%', height: 10, rx: 5 }));
    }
    const x = (100 / max * 100).toFixed(2) + '%';
    svg.appendChild(s('line', { class: 'kbar-100', x1: x, x2: x, y1: -2, y2: 12, 'aria-hidden': 'true' }));
    return svg;
  }

  /** Тренд по неделям: линия через точки с данными (на пустых неделях — разрыв), пунктир 100 %. */
  function trendChart(trend) {
    const points = (trend && trend.points) || [];
    const values = points.map((p) => p.kpi).filter(isNum);
    if (!values.length) return h('p', { class: 'muted small' }, 'Недостаточно данных для динамики');
    const lo = Math.min(60, Math.min.apply(null, values) - 5);
    const hi = Math.max(120, Math.max.apply(null, values) + 5);
    const top = 20;
    const bottom = 50;
    const y = (v) => top + (hi - v) / (hi - lo) * (bottom - top);
    const n = points.length;
    const xPct = (i) => (n === 1 ? 50 : i / (n - 1) * 100);
    let path = '';
    let pen = false;
    let lastIndex = -1;
    points.forEach((p, i) => {
      if (!isNum(p.kpi)) {
        pen = false;
        return;
      }
      path += (pen ? 'L' : 'M') + (xPct(i) * 10).toFixed(1) + ' ' + y(p.kpi).toFixed(1) + ' ';
      pen = true;
      lastIndex = i;
    });
    const label = tr('KPI по неделям:') + ' ' + points.map((p) => p.label + ' — ' + (isNum(p.kpi) ? pct(p.kpi) : tr('нет данных'))).join(', ');
    const y100 = y(100).toFixed(1);
    const svg = s('svg', { class: 'trend', width: '100%', height: '56', role: 'img', 'aria-label': label, focusable: 'false' },
      s('line', { class: 'trend-100', x1: 0, x2: '100%', y1: y100, y2: y100, 'aria-hidden': 'true' }),
      s('text', { class: 'trend-label', x: 0, y: (Number(y100) - 5).toFixed(1), 'aria-hidden': 'true' }, '100 %'),
      s('svg', { x: 0, y: 0, width: '100%', height: '56', viewBox: '0 0 1000 56', preserveAspectRatio: 'none', 'aria-hidden': 'true', overflow: 'visible' },
        s('path', { class: 'trend-line', d: path.trim(), 'vector-effect': 'non-scaling-stroke' })));
    points.forEach((p, i) => {
      if (!isNum(p.kpi)) return;
      svg.appendChild(s('circle', { class: 'trend-dot' + (i === lastIndex ? ' last' : ''), cx: xPct(i).toFixed(2) + '%', cy: y(p.kpi).toFixed(1), r: 3, 'aria-hidden': 'true' }));
    });
    const lastValue = points[lastIndex].kpi;
    svg.appendChild(s('text', {
      class: 'trend-value', x: xPct(lastIndex).toFixed(2) + '%', y: (y(lastValue) - 8).toFixed(1),
      'text-anchor': lastIndex === n - 1 ? 'end' : lastIndex === 0 ? 'start' : 'middle', 'aria-hidden': 'true',
    }, pct(lastValue)));
    return h('div', null,
      h('div', { class: 'trend-wrap' }, svg),
      h('div', { class: 'trend-cap', 'aria-hidden': 'true' }, h('span', null, points[0].label), h('span', null, points[n - 1].label)));
  }

  function kpiNumber(text, kpi) {
    const match = /^(\d+)\s%$/.exec(text || '');
    if (match && isNum(kpi)) return h('div', { class: 'kpi-num' }, match[1], h('span', { class: 'unit' }, '%'));
    return h('div', { class: 'kpi-num is-empty' }, text || 'нет данных');
  }

  // --- Загрузка данных в контейнер --------------------------------------------------------------

  /**
   * loadInto(scr, box, {request: () => ({ep, opts}), skeleton, render, stale}) -> run(fresh).
   * Кэш свежий — рисуем сразу; иначе скелетон (если показывать нечего), затем данные или ошибка
   * с кнопкой «Повторить». Поздние ответы (экран сменился, запрос устарел) отбрасываются.
   * spec.stale — вместо скелетона сразу виден прежний ответ, свежий приходит следом (не для форм и экранов
   * решения: их render строит ввод и основную кнопку из данных — там прежнее показывать нельзя).
   * spec.keep — ответ не старше STALE_MAX_MS годится как свежий, запроса вдогонку нет (форма не перерисуется).
   */
  function loadInto(scr, box, spec) {
    let seq = 0;

    function paint(data) {
      // Перерисовка на месте (опрос «оценка рассчитывается», обновление после действия) не должна
      // сворачивать открытые блоки и сбрасывать прокрутку: раскрытые <details data-key> и позиция — как были.
      const again = box.dataset.state === 'ok';
      const opened = again ? openFolds(box) : null;
      const pane = box.closest('.detail');  // сайт: панель подробностей прокручивается сама, не вместе с окном
      const scrollY = () => (pane ? pane.scrollTop : window.scrollY);
      const y = scrollY();
      // spec.anchor: блок выше места чтения может вырасти (пришла оценка) — на месте остаётся не число
      // прокрутки, а первый видимый блок: текст под глазами и кнопка под пальцем не уезжают.
      const kids = again && spec.anchor ? Array.from(box.children) : [];
      const at = kids.findIndex((el) => el.getBoundingClientRect().bottom > 0);
      const was = at > 0 ? kids[at].getBoundingClientRect().top : 0;
      box.dataset.state = 'ok';
      box.replaceChildren.apply(box, nodes(spec.render(data)));
      if (!again) return;
      box.querySelectorAll('details[data-key]').forEach((d) => {
        if (opened.has(d.dataset.key)) d.open = true;
      });
      const to = at > 0 && box.children.length === kids.length
        ? Math.max(0, Math.round(scrollY() + box.children[at].getBoundingClientRect().top - was)) : y;
      if (scrollY() === to) return;
      if (pane) pane.scrollTop = to;
      else window.scrollTo(0, to);
    }

    async function run(fresh) {
      const my = ++seq;
      const gen = cacheGen;
      const req = spec.request();
      const hit = fresh ? null : (spec.keep ? stale : peek)(req.ep, req.opts);
      if (hit) {
        scr.loadedAt = hit.at;
        box.removeAttribute('aria-busy');  // запрос, который ещё в пути, устарел и флаг сам не снимет
        paint(hit.data);
        return;
      }
      box.setAttribute('aria-busy', 'true');
      const old = spec.stale && !fresh ? stale(req.ep, req.opts) : null;
      const snap = old ? JSON.stringify(old.data) : '';  // снимок: карточка задачи правит свой объект на месте
      if (old) {
        scr.loadedAt = old.at;
        paint(old.data);
      } else if (box.dataset.state !== 'ok') box.replaceChildren.apply(box, nodes(spec.skeleton()));
      try {
        const data = await api(req.ep, Object.assign({}, req.opts, { fresh: Boolean(fresh) }));
        while (posting) await settled();
        if (!scr.alive || my !== seq) return;
        if (gen !== cacheGen) {  // пока ждали, что-то изменили: этот ответ мог быть прочитан до изменения
          run(true);
          return;
        }
        scr.loadedAt = Date.now();
        if (old && snap === JSON.stringify(data)) return;  // то же самое уже на экране
        paint(data);
      } catch (err) {
        // 401/403 доступа уже сменили экран; 429 показан тостом, но здесь нужна кнопка «Повторить».
        if (!scr.alive || my !== seq || (err.handled && err.status !== 429)) return;
        if (!err.handled) haptic.err();
        box.dataset.state = 'error';
        box.replaceChildren(errorState(err, () => run(true)));
        if (spec.onError) spec.onError();
      } finally {
        if (scr.alive && my === seq) box.removeAttribute('aria-busy');
      }
    }

    return run;
  }

  /** Ключи раскрытых блоков <details data-key> внутри контейнера. */
  function openFolds(root) {
    const keys = new Set();
    root.querySelectorAll('details[data-key]').forEach((d) => {
      if (d.open) keys.add(d.dataset.key);
    });
    return keys;
  }

  // --- Поля формы -------------------------------------------------------------------------------

  /** Текстовое поле с подписью, счётчиком, подсказкой и ошибкой (aria-invalid/aria-describedby). */
  // ---- Голосовой ввод (§11.11): нажал — говоришь — нажал ещё раз; текст распознаёт сервер ----

  const VOICE_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  const VOICE_IDLE_MS = 60 * 1000; // микрофон остаётся открытым между записями: Telegram не спрашивает разрешение каждый раз
  const VOICE_TIMEOUT_MS = 75 * 1000;
  const VOICE_MIN_MS = 600;
  const voice = { stream: null, idleTimer: 0, active: null };

  function voiceSupported() {
    return Boolean(cfg() && cfg().voice_enabled && navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      && typeof MediaRecorder === 'function');
  }

  function voiceMime() {
    if (typeof MediaRecorder.isTypeSupported !== 'function') return '';
    for (let i = 0; i < VOICE_TYPES.length; i += 1) {
      if (MediaRecorder.isTypeSupported(VOICE_TYPES[i])) return VOICE_TYPES[i];
    }
    return '';
  }

  async function voiceStream() {
    clearTimeout(voice.idleTimer);
    if (voice.stream && voice.stream.getAudioTracks().some((t) => t.readyState === 'live')) return voice.stream;
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    return voice.stream;
  }

  function voiceRelease(now) {
    clearTimeout(voice.idleTimer);
    const stop = () => {
      if (!voice.stream) return;
      voice.stream.getTracks().forEach((t) => t.stop());
      voice.stream = null;
    };
    if (now) stop();
    else voice.idleTimer = setTimeout(stop, VOICE_IDLE_MS);
  }

  function voiceErrorText(err) {
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return WEB
        ? 'Нет доступа к микрофону. Разрешите микрофон для этого сайта в браузере и попробуйте ещё раз.'
        : 'Нет доступа к микрофону. Разрешите микрофон для Telegram в настройках телефона и попробуйте ещё раз.';
    }
    if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'Микрофон не найден.';
    return 'Не удалось включить микрофон. Попробуйте ещё раз или введите текст с клавиатуры.';
  }

  function fmtClock(ms) {
    const s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  /**
   * Кнопка-микрофон. o.mode: 'text' (по умолчанию) — распознать речь; 'task' — разобрать задачу целиком.
   * o.onResult(data) — ответ POST /api/voice. o.text — подпись большой кнопки (иначе только значок).
   */
  function micButton(o) {
    const idle = o.text || '';
    const label = o.label || 'Надиктовать голосом';
    const btn = h('button', { type: 'button', class: 'mic-btn' + (o.text ? ' mic-big' : ''), 'aria-label': label, title: label }, icon('mic'), idle);
    let recorder = null;
    let chunks = [];
    let startedAt = 0;
    let tick = 0;
    let stage = 'idle'; // idle | starting | rec | sending

    function paint() {
      btn.classList.toggle('rec', stage === 'rec');
      btn.classList.toggle('busy', stage === 'starting' || stage === 'sending');
      btn.disabled = stage === 'starting' || stage === 'sending';
      if (stage === 'rec') {
        setLabel(btn, 'stop', fmtClock(Date.now() - startedAt));
        btn.setAttribute('aria-label', tr('Остановить запись'));
      } else if (stage === 'sending') {
        setLabel(btn, 'wait', o.text ? 'Распознаю…' : '');
        btn.setAttribute('aria-label', tr('Распознаю речь'));
      } else {
        setLabel(btn, 'mic', idle);
        btn.setAttribute('aria-label', tr(label));
      }
    }

    function reset() {
      clearInterval(tick);
      tick = 0;
      recorder = null;
      chunks = [];
      stage = 'idle';
      if (voice.active === api_) voice.active = null;
      paint();
    }

    async function start() {
      if (voice.active && voice.active !== api_) voice.active.stop(); // одна запись за раз
      voice.active = api_;
      stage = 'starting';
      paint();
      let stream;
      try {
        stream = await voiceStream();
      } catch (err) {
        reset();
        haptic.err();
        toast(voiceErrorText(err), 'mic');
        return;
      }
      if (stage !== 'starting') return;
      const mime = voiceMime();
      try {
        recorder = new MediaRecorder(stream, mime ? { mimeType: mime, audioBitsPerSecond: 32000 } : { audioBitsPerSecond: 32000 });
      } catch (_) {
        try {
          recorder = new MediaRecorder(stream);
        } catch (err) {
          reset();
          voiceRelease(true);
          toast(voiceErrorText(err), 'mic');
          return;
        }
      }
      chunks = [];
      recorder.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size) chunks.push(e.data);
      });
      recorder.addEventListener('stop', finish);
      recorder.addEventListener('error', () => {
        reset();
        voiceRelease(true);
        toast(voiceErrorText(null), 'mic');
      });
      startedAt = Date.now();
      recorder.start();
      stage = 'rec';
      haptic.sel();
      paint();
      const maxMs = ((cfg() && cfg().voice_max_sec) || 120) * 1000;
      tick = setInterval(() => {
        if (Date.now() - startedAt >= maxMs) stop();
        else paint();
      }, 500);
    }

    function stop() {
      if (stage !== 'rec' || !recorder) return;
      clearInterval(tick);
      tick = 0;
      stage = 'sending';
      paint();
      safely(() => recorder.stop());
    }

    async function finish() {
      const took = Date.now() - startedAt;
      const type = (recorder && recorder.mimeType) || (chunks[0] && chunks[0].type) || 'audio/webm';
      const blob = new Blob(chunks, { type });
      voiceRelease(false);
      if (took < VOICE_MIN_MS || !blob.size) {
        reset();
        toast('Запись слишком короткая. Нажмите, скажите фразу и нажмите ещё раз.', 'mic');
        return;
      }
      stage = 'sending';
      paint();
      try {
        const data = await api(EP.voice, { query: o.mode === 'task' ? { mode: 'task' } : null, body: blob, timeout: VOICE_TIMEOUT_MS });
        reset();
        haptic.ok();
        if (btn.isConnected) o.onResult(data);
      } catch (err) {
        reset();
        reportError(err);
      }
    }

    const api_ = { stop };
    btn.addEventListener('click', () => {
      if (stage === 'idle') start();
      else if (stage === 'rec') stop();
    });
    return btn;
  }

  /** Вставить распознанный текст в поле: в конец, через пробел; дальше — как обычный ввод с клавиатуры. */
  function appendDictated(input, text) {
    const add = String(text || '').trim();
    if (!add) return;
    const cur = input.value;
    input.value = cur && !/\s$/.test(cur) ? cur + ' ' + add : cur + add;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function field(o) {
    const id = uid('f');
    const errId = id + '-err';
    const hintId = o.hint ? id + '-hint' : null;
    const input = h(o.multiline ? 'textarea' : 'input', {
      id, class: 'input', value: o.value || '', placeholder: o.placeholder || null,
      type: o.multiline ? null : o.type || 'text', rows: o.multiline ? String(o.rows || 3) : null,
      inputmode: o.inputmode || null, enterkeyhint: o.enterkeyhint || null, autocomplete: 'off',
      'aria-required': o.required ? 'true' : null,
      'aria-describedby': [hintId, errId].filter(Boolean).join(' '),
    });
    const counter = o.max ? h('span', { class: 'counter', 'aria-hidden': 'true' }) : null;
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });
    const hint = hintId ? h('p', { class: 'field-hint', id: hintId }, o.hint) : null;
    const labelEl = h('label', { for: id }, h('span', null, o.label, o.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null), counter);
    // Текстовые поля можно надиктовать (числа, даты и поиск — с клавиатуры).
    const dictable = o.mic !== false && !o.inputmode && (o.multiline || !o.type || o.type === 'text') && voiceSupported();
    const mic = dictable ? micButton({ label: tr('Надиктовать:') + ' ' + tr(o.label), onResult: (data) => appendDictated(input, data.text) }) : null;
    const el = h('div', { class: 'field' },
      mic ? h('div', { class: 'field-head' }, labelEl, mic) : labelEl,
      input, hint, err);

    function updateCounter() {
      if (!counter) return;
      const n = input.value.length;
      setText(counter, n >= o.max * 0.8 ? n + ' / ' + o.max : '');
      counter.classList.toggle('over', n > o.max);
    }

    function setError(message) {
      setText(err, message || '');
      if (message) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }

    function validate() {
      const v = input.value.trim();
      if (o.required && !v) {
        setError(o.requiredText || 'Заполните это поле');
        return false;
      }
      if (v && o.min && v.length < o.min) {
        setError(o.minText || 'Слишком коротко');
        return false;
      }
      if (o.max && v.length > o.max) {
        setError('Слишком длинно: ' + v.length + ' из ' + o.max + ' символов. Сократите, пожалуйста.');
        return false;
      }
      if (o.check) {
        const problem = o.check(v);
        if (problem) {
          setError(problem);
          return false;
        }
      }
      setError('');
      return true;
    }

    input.addEventListener('input', () => {
      updateCounter();
      if (input.getAttribute('aria-invalid') === 'true') setError('');
      if (o.onInput) o.onInput(input.value);
    });
    updateCounter();
    return {
      el, input, setError, validate,
      get: () => input.value,
      set(v) {
        input.value = v;
        updateCounter();
      },
      focus: () => input.focus(),
    };
  }

  function selectField(o) {
    const id = uid('sel');
    const errId = id + '-err';
    const select = h('select', {
      id, class: 'input', 'aria-required': o.required ? 'true' : null, 'aria-describedby': errId,
      onchange: () => {
        setError('');
        haptic.sel();
        o.onChange(select.value);
      },
    },
    h('option', { value: '' }, o.placeholder || '—'),
    o.options.map((opt) => h('option', { value: opt.value, selected: opt.value === o.value }, opt.label)));
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });
    function setError(message) {
      setText(err, message || '');
      if (message) select.setAttribute('aria-invalid', 'true');
      else select.removeAttribute('aria-invalid');
    }
    return {
      el: h('div', { class: 'field' },
        h('label', { for: id }, h('span', null, o.label, o.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null)),
        select, err),
      setError,
      validate() {
        if (o.required && !select.value) {
          setError(o.requiredText || 'Выберите значение');
          return false;
        }
        return true;
      },
      focus: () => select.focus(),
    };
  }

  /** Срок (§11.8): чипы быстрых дат + дата + время; model = {date, time, touched}. */
  function deadlinePicker(model, o) {
    const opts = o || {};
    const defTime = cfg().default_deadline_time || '18:00';
    if (!model.time) model.time = defTime;
    const id = uid('dl');
    const errId = id + '-err';
    const quick = (state.me && state.me.deadline_options) || [];
    const chips = quick.map((opt) => chip(opt.label, opt.date === model.date, () => {
      model.date = opt.date;
      dateInput.value = opt.date;
      sync(true);
    }));
    const dateInput = h('input', {
      id, type: 'date', class: 'input', value: model.date || '', min: state.me ? state.me.today : null,
      'aria-describedby': errId,
    });
    const timeInput = h('input', { type: 'time', class: 'input', value: model.time, step: '60', 'aria-label': 'Время' });
    const caption = h('p', { class: 'field-hint', 'aria-live': 'polite' });
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });
    const onDate = () => {
      if (model.date === dateInput.value) return;
      model.date = dateInput.value;
      sync(true);
    };
    const onTime = () => {
      const v = timeInput.value || defTime;
      if (model.time === v && model.touched) return;
      model.time = v;
      model.touched = true;
      sync(true);
    };
    dateInput.addEventListener('change', onDate);
    dateInput.addEventListener('input', onDate);
    timeInput.addEventListener('change', onTime);
    timeInput.addEventListener('input', onTime);

    function setError(message) {
      setText(err, message || '');
      if (message) dateInput.setAttribute('aria-invalid', 'true');
      else dateInput.removeAttribute('aria-invalid');
    }

    function sync(byUser) {
      chips.forEach((c, i) => c.setAttribute('aria-pressed', String(quick[i].date === model.date)));
      setText(caption, model.date
        ? own(humanDate(model.date)) + ', ' + model.time
        : 'Выберите дату кнопкой или в календаре');
      if (byUser) {
        setError('');
        if (opts.onChange) opts.onChange();
      }
    }
    sync(false);

    return {
      el: h('div', { class: 'field' },
        h('label', { for: id }, h('span', null, opts.label || 'Срок', opts.optional ? null : h('span', { class: 'req', 'aria-hidden': 'true' }, '*'))),
        chips.length ? h('div', { class: 'chips wrap', role: 'group', 'aria-label': 'Быстрый выбор срока' }, chips) : null,
        h('div', { class: 'pair-dt' }, dateInput, timeInput),
        caption, err),
      /** «YYYY-MM-DD», если время по умолчанию и не трогали; иначе «YYYY-MM-DDTHH:MM»; null — нет даты. */
      value() {
        if (!model.date) return null;
        return model.touched || model.time !== defTime ? model.date + 'T' + model.time : model.date;
      },
      setError,
      validate() {
        if (!model.date) {
          setError('Укажите срок');
          return false;
        }
        return true;
      },
      focus: () => dateInput.focus(),
    };
  }

  /** Вес задачи: чипы weight_options (значок «внимание» — неделя перегружена) + своё число 1..100; model.weight. */
  function weightPicker(model, o) {
    const opts = o || {};
    const options = cfg().weight_options || [];
    let load = null;
    let week = '';
    let over = {};
    const id = uid('w');
    const errId = id + '-err';
    const chipsBox = h('div', { class: 'chips wrap', role: 'group', 'aria-label': 'Вес задачи' });
    const custom = h('input', {
      id, class: 'input num', type: 'text', inputmode: 'numeric', placeholder: '1–100', autocomplete: 'off',
      value: isNum(model.weight) && options.indexOf(model.weight) < 0 ? String(model.weight) : '',
      'aria-describedby': errId,
    });
    const hint = h('p', { class: 'field-hint', 'aria-live': 'polite' });
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });

    custom.addEventListener('input', () => {
      const raw = custom.value.trim();
      const n = Number(raw.replace(',', '.'));
      model.weight = raw ? (Number.isInteger(n) ? n : NaN) : null;
      setError('');
      paint();
      if (opts.onChange) opts.onChange();
    });

    function setError(message) {
      setText(err, message || '');
      if (message) custom.setAttribute('aria-invalid', 'true');
      else custom.removeAttribute('aria-invalid');
    }

    function paint() {
      chipsBox.replaceChildren.apply(chipsBox, options.map((w) => chip(
        [w + ' %', over[w] ? icon('alert', 'ic-sm tone-warn') : null],
        model.weight === w,
        () => {
          model.weight = w;
          custom.value = '';
          setError('');
          paint();
          if (opts.onChange) opts.onChange();
        },
        over[w] ? w + ' %, неделя сотрудника будет перегружена' : null)));
      const parts = [];
      if (load !== null) {
        parts.push(tr('Сейчас на неделе: ' + load + ' %. Рекомендуется, чтобы сумма весов за неделю была ≈100 %.'));
        if (isNum(model.weight) && model.weight > 0 && load + model.weight > 100) {
          parts.push(tr('С этой задачей будет ' + (load + model.weight) + ' %.'));
        }
        if (week) parts.push(own(week));
      } else {
        parts.push(tr('Доля задачи в оценке эффективности сотрудника, 1–100 %.'));
      }
      setText(hint, parts.join(' '));
    }
    paint();

    return {
      el: h('div', { class: 'field' },
        h('div', { class: 'label' }, h('span', null, 'Вес задачи', h('span', { class: 'req', 'aria-hidden': 'true' }, '*'))),
        chipsBox,
        h('div', { class: 'inline-input' }, h('label', { for: id, class: 'muted small' }, 'Своё значение, %'), custom),
        hint, err),
      setLoad(data) {
        load = data && isNum(data.load) ? data.load : null;
        week = data && data.week_label ? data.week_label : '';
        over = {};
        if (data && Array.isArray(data.options)) data.options.forEach((opt) => { over[opt.weight] = Boolean(opt.over); });
        paint();
      },
      filled: () => isNum(model.weight),
      setError,
      validate() {
        const w = model.weight;
        if (w === null || w === undefined) {
          setError('Выберите вес задачи');
          return false;
        }
        if (!Number.isInteger(w) || w < 1 || w > 100) {
          setError('Вес — целое число от 1 до 100');
          return false;
        }
        setError('');
        return true;
      },
      focus: () => custom.focus(),
    };
  }

  /**
   * План: число + единица (необязательно) и «Убрать план»; model.plan_value / model.plan_unit — строки.
   * Пустой план свёрнут в одну кнопку «Добавить план (число)»: поля раскрываются по нажатию, а также когда план
   * приходит сам (подсказка AI, диктовка, черновик, правка задачи с планом) или у поля появляется ошибка.
   */
  function planFields(model, o) {
    const opts = o || {};
    const id = uid('plan');
    const errId = id + '-err';
    const value = h('input', {
      // Заглушки — явно примеры («напр. …»), чтобы пустое поле не читалось как уже заданный план.
      id, class: 'input num', type: 'text', inputmode: 'decimal', placeholder: 'напр. 100', autocomplete: 'off',
      value: model.plan_value || '', 'aria-describedby': errId,
    });
    const unit = h('input', {
      class: 'input', type: 'text', placeholder: 'напр. договоров', autocomplete: 'off', maxlength: '64',
      value: model.plan_unit || '', 'aria-label': 'Единица измерения плана',
    });
    const clear = h('button', { type: 'button', class: 'link-btn', onclick: () => {
      model.plan_value = '';
      model.plan_unit = '';
      value.value = '';
      unit.value = '';
      changed();
      value.focus();
    } }, icon('x', 'ic-sm'), 'Убрать план');
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });
    value.addEventListener('input', () => {
      model.plan_value = value.value;
      changed();
    });
    unit.addEventListener('input', () => {
      model.plan_unit = unit.value;
      changed();
    });

    function changed() {
      clear.hidden = !String(model.plan_value || '').trim() && !String(model.plan_unit || '').trim();
      setError('');
      if (opts.onChange) opts.onChange();
    }

    function setError(message) {
      setText(err, message || '');
      if (message) {
        expand();
        value.setAttribute('aria-invalid', 'true');
      } else value.removeAttribute('aria-invalid');
    }
    clear.hidden = !String(model.plan_value || '').trim() && !String(model.plan_unit || '').trim();

    const body = h('div', { class: 'plan-body', hidden: clear.hidden },
      h('label', { for: id }, h('span', null, 'План (число)'), h('span', { class: 'counter' }, 'необязательно')),
      h('div', { class: 'pair' }, value, unit),
      h('p', { class: 'field-hint' }, 'Например: 100 договоров. Оставьте пустым, если числового плана нет.'),
      clear, err);
    const more = h('button', { type: 'button', class: 'link-btn', hidden: !clear.hidden, onclick: () => {
      expand();
      value.focus();
    } }, icon('plus', 'ic-sm'), 'Добавить план (число)');

    function expand() {
      body.hidden = false;
      more.hidden = true;
    }

    return {
      el: h('div', { class: 'field' }, more, body),
      set(v, u) {
        model.plan_value = v;
        model.plan_unit = u;
        value.value = v;
        unit.value = u;
        clear.hidden = !v && !u;
        if (v || u) expand();
      },
      setError,
      validate() {
        const raw = String(model.plan_value || '').trim();
        if (!raw) {
          if (String(model.plan_unit || '').trim()) {
            setError('Укажите плановое число или уберите единицу');
            return false;
          }
          return true;
        }
        const n = parseLooseNumber(raw);
        if (n === null) {
          setError('Не понял число. Введите, например: 100');
          return false;
        }
        if (n <= 0) {
          setError('Плановое число должно быть больше нуля');
          return false;
        }
        if (n > 1e15) {
          setError('Слишком большое число — введите реальное плановое значение');
          return false;
        }
        if (String(model.plan_unit || '').trim().length > 64) {
          setError('Единица измерения — до 64 символов');
          return false;
        }
        return true;
      },
      focus: () => value.focus(),
    };
  }

  /** Кнопка «Сделать измеримым» (§11.9): запрос к AI и лист с вариантом. */
  function aiHelper(scr, o) {
    let busy = false;
    let raw0 = '';
    const btn = button('Сделать измеримым', () => run(null), 'btn-sm', null, 'ai');
    const status = h('span', { class: 'ai-status', role: 'status' });

    function refresh() {
      btn.disabled = busy || !(o.getTitle().trim() && o.getRaw().trim().length >= 3);
    }

    async function run(previous) {
      if (busy) return;
      const title = o.getTitle().trim();
      if (!previous) raw0 = o.getRaw().trim();
      if (!title || raw0.length < 3) return;
      busy = true;
      refresh();
      setText(status, 'Формулирую измеримый результат…');
      try {
        const body = { title, raw_result: raw0 };
        if (previous) body.previous = previous.slice(0, 1000);
        const suggestion = await api(EP.formulate, { body, timeout: AI_TIMEOUT_MS });
        if (scr.alive) showSuggestion(suggestion);
      } catch (err) {
        if (scr.alive && !err.handled) {
          haptic.err();
          toast(err.code === 'timeout' ? T.aiTimeout : err.message, 'alert');
        }
      } finally {
        busy = false;
        setText(status, '');
        if (scr.alive) refresh();
      }
    }

    function showSuggestion(sg) {
      const hasPlan = isNum(sg.plan_value);
      sheet.show({
        title: sg.source === 'ai' ? 'Измеримая формулировка' : 'Подсказка без AI',
        body: h('div', null,
          sg.notice ? note(sg.notice, 'warn') : null,
          h('p', { class: 'muted small' }, 'Вы написали: ', h('i', null, own(raw0))),
          h('p', { class: 'suggest' }, own(sg.expected_result)),
          hasPlan ? line('План:', h('b', { class: 'num' }, own(fmtNum(sg.plan_value) + (sg.plan_unit ? ' ' + sg.plan_unit : ''))), '', 'target') : null,
          sg.note ? line('', h('i', null, own(sg.note)), '', 'bulb') : null,
          sg.source === 'rules' ? h('p', { class: 'line' }, h('span', { class: 'tag' }, 'по правилам, без AI')) : null,
          h('div', { class: 'btn-row mt12' },
            button('Другой вариант', () => {
              sheet.close(true);
              run(sg.expected_result);
            }, '', null, 'refresh'),
            button('Оставить как написал', () => sheet.close(false)))),
        primary: {
          text: 'Принять',
          onClick: () => {
            o.onAccept(sg, raw0);
            sheet.close(false);
            haptic.ok();
            toast('Формулировка подставлена', 'done');
          },
        },
      });
    }

    refresh();
    return { el: h('div', { class: 'ai-row' }, btn, status), refresh };
  }

  /** Сопоставить текст ошибки сервера с полем формы: [[/срок/i, поле], …] -> true, если нашли. */
  function errorToField(err, pairs) {
    for (const pair of pairs) {
      if (pair[1] && pair[0].test(err.ru || '')) {
        pair[1].setError(err.message);
        safely(() => pair[1].focus());
        return true;
      }
    }
    return false;
  }

  function showFormError(box, err) {
    box.replaceChildren(note(err.message || T.generic, 'bad'));
    safely(() => box.scrollIntoView({ block: 'center', behavior: scrollBehavior() }));
  }

  function firstInvalid(checks) {
    let first = null;
    for (const item of checks) {
      if (!item) continue;
      if (!item.validate() && !first) first = item;
    }
    if (first) {
      haptic.err();
      safely(() => first.focus());
      return false;
    }
    return true;
  }

  /**
   * «Осталось заполнить: …» — строка в конце формы. Основная кнопка формы активна всегда: нажатие с пустыми
   * обязательными полями подсвечивает их (firstInvalid) и называет в подсказке (warn).
   * items: [{label, ok: () => boolean}].
   */
  function formGuide(items) {
    const el = h('p', { class: 'form-left', 'aria-live': 'polite' });
    const left = () => items.filter((item) => !item.ok()).map((item) => item.label);
    let shown = null;
    function refresh() {
      const names = left();
      if (names.join() === shown) return;  // без перемен строку не трогаем: её читает экранный диктор
      shown = names.join();
      el.classList.toggle('is-ok', !names.length);
      el.replaceChildren();
      appendKids(el, names.length
        ? [icon('info', 'ic-sm'), h('span', null, 'Осталось заполнить: ', names.map((name, i) => (i ? [', ', name] : name)))]
        : [icon('check', 'ic-sm'), h('span', null, 'Всё заполнено')]);
    }
    refresh();
    return {
      el,
      refresh,
      warn() {
        const names = left();
        if (names.length) toast(tr('Осталось заполнить:') + ' ' + names.map((name) => tr(name)).join(', '), 'alert');
      },
    };
  }

  // --- Строки списков -----------------------------------------------------------------------

  const STATUS_ICONS = { proposed: 'inbox', active: 'work', submitted: 'review', rework: 'undo', done: 'done', cancelled: 'ban', rejected: 'reject' };

  /** Значок статуса — по полям status и overdue (эмодзи из подписи сервера приложению не нужен). */
  function statusIcon(t) {
    return t.overdue ? 'clock' : STATUS_ICONS[t.status] || 'info';
  }

  function statusText(t) {
    return stripLeadingEmoji(t.status_label);
  }

  /** Цвет статуса: синий — в работе, жёлтый — ждёт решения или доработки, красный — просрочено, зелёный — выполнено. */
  function statusTone(t) {
    if (t.overdue) return 'bad';
    return { done: 'good', submitted: 'warn', rework: 'warn', active: 'info', proposed: 'warn' }[t.status] || '';
  }

  /** Метка статуса задачи: цвет, значок и подпись. */
  function statusPill(t) {
    return pill(t.status_label, statusTone(t), statusIcon(t));
  }

  /** Приоритет: цветная точка и подпись сервера («Высокий») без эмодзи. */
  function priorityMark(t) {
    return h('span', { class: 'prio' }, h('span', { class: 'dot ' + (PRIORITY_TONES[t.priority] || ''), 'aria-hidden': 'true' }),
      own(stripLeadingEmoji(t.priority_label)));
  }

  /** Стрелка «дальше» в конце строки сотрудника (строки задач — без неё: справа у них оценка). */
  function chevron() {
    return h('span', { class: 'chev' }, icon('next', 'ic-sm'));
  }

  /** Части подписи через « · » — отдельными узлами (значки и точки внутри строки не склеить в текст). */
  function dotted(parts) {
    return parts.filter(Boolean).map((part, i) => (i ? [' · ', part] : part));
  }

  /**
   * Строка списка: «#12 Название» во всю ширину; под ним — строка состояния (метки, срок, справа оценка),
   * ниже — подробности. o: {to, id, title, state, meta}.
   */
  function listRow(o) {
    return h('li', { class: rowOpen(o.to) }, h('button', { type: 'button', class: 'row-main', 'data-to': o.to, onclick: (event) => openRow(event, o.to) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + o.id), own(o.title)),
        o.state ? h('span', { class: 'row-state' }, o.state) : null,
        o.meta ? h('span', { class: 'row-sub' }, o.meta) : null)));
  }

  /** Класс строки, чьи подробности сейчас открыты в панели (сайт, большой экран). */
  function rowOpen(to) {
    return under && nav.current && nav.current.hash === to ? 'is-open' : null;
  }

  /** Оценка у правого края строки состояния. */
  function rowScore(content, bad) {
    return h('span', { class: 'row-score' + (bad ? ' is-bad' : '') }, content);
  }

  /**
   * Строка задачи. Состояние: метка статуса (у просроченной — насколько просрочена), затем срок или оценка;
   * подробности: исполнитель · приоритет · вес. o: {assignee, onAccept, to, queue, returns}.
   */
  function taskRow(t, o) {
    const opts = o || {};
    const unaccepted = t.status === 'active' && !t.accepted;
    const open = t.status === 'active' || t.status === 'rework' || t.status === 'proposed';
    const wait = unaccepted ? pill('Не принята', 'warn', 'wait') : null;
    // У просроченной подпись сервера — «Просрочена»: что она ещё и на доработке, видно только по полю status.
    const status = t.overdue ? (t.status === 'rework' ? pill('на доработке', 'warn', 'undo') : null)
      : opts.queue ? null : statusPill(t);  // в очереди поручений статус у всех один — метка не нужна
    if (status && unaccepted) status.hidden = true;  // «В работе» появится после «Принять»
    const past = t.overdue || (open && Date.parse(t.deadline) <= serverNow());
    const due = opts.queue ? 'срок ' + own(t.deadline_local) : t.tail;
    const meta = [];
    if (opts.assignee && t.assignee) meta.push(own(t.assignee.short_name));
    if (t.status !== 'proposed') meta.push(priorityMark(t), 'вес ' + t.weight + ' %');
    else if (!opts.queue) meta.push('ждёт подтверждения начальника');
    if (opts.returns && t.rework_count > 1) meta.push('возвратов: ' + t.rework_count);
    const li = listRow({
      to: opts.to || '#/task/' + t.id, id: t.id, title: t.title,
      state: [
        t.overdue ? pill(t.tail, 'bad', 'clock') : null,
        wait, status,
        t.status === 'done' ? rowScore(t.tail) : null,
        !t.overdue && (open || (t.status === 'submitted' && t.submitted_at))
          ? h('span', { class: 'row-due' + (past ? ' tone-bad' : '') }, open ? icon('calendar', 'ic-sm') : null, due) : null,
      ],
      meta: dotted(meta),
    });
    if (opts.onAccept && unaccepted) {
      const mark = (accepted) => {
        wait.hidden = accepted;
        if (status) status.hidden = !accepted;
      };
      const accept = h('button', {
        type: 'button', class: 'row-action', 'aria-label': 'Принять задачу #' + t.id + ' в работу',
        onclick: () => opts.onAccept(t, accept, mark),
      }, icon('check', 'ic-sm'), 'Принять');
      li.classList.add('has-action');
      li.appendChild(accept);
    }
    return li;
  }

  /**
   * Таблица большого экрана сайта. cols: [{label, cls, sort}] — sort: ключ сортировки (заголовок становится кнопкой);
   * state: {key, desc} — как отсортировано сейчас; onSort(key) — нажат заголовок. Строки добавляет tableRow.
   */
  function dataTable(cols, state, onSort) {
    const body = h('tbody');
    const head = h('tr', null, cols.map((c) => {
      const on = Boolean(c.sort && state && state.key === c.sort);
      const mark = on ? (state.desc ? 'sortdown' : 'sortup') : 'sort';
      return h('th', { scope: 'col', class: c.cls || null, 'aria-sort': on ? (state.desc ? 'descending' : 'ascending') : null },
        c.sort ? h('button', { type: 'button', class: 'th-sort' + (on ? ' is-on' : ''), onclick: () => onSort(c.sort) }, c.label, icon(mark, 'ic-sm')) : c.label);
    }));
    return { el: h('div', { class: 'tbl-wrap' }, h('table', { class: 'tbl' }, h('thead', null, head), body)), body, cols };
  }

  /** Строка таблицы: вся строка открывает to (кнопка-название — для клавиатуры); cells — по одному на столбец. */
  function tableRow(table, to, cells) {
    const tr = h('tr', { class: rowOpen(to) }, cells.map((cell, i) => h('td', { class: table.cols[i].cls || null }, cell)));
    tr.addEventListener('click', (event) => {
      if (event.target.closest('button, a, input, select')) return;  // у кнопок в строке своё действие
      const link = tr.querySelector('[data-to]');
      if (link) link.click();
    });
    return tr;
  }

  /** Название в таблице — кнопка, которая открывает строку. */
  function cellLink(to, text) {
    return h('button', { type: 'button', class: 'cell-link', 'data-to': to, onclick: (event) => openRow(event, to) }, own(text));
  }

  /** Следующее состояние сортировки по нажатию на заголовок: по возрастанию -> по убыванию -> как было. */
  function nextSort(now, key) {
    return now === key ? '-' + key : now === '-' + key ? '' : key;
  }

  function sortState(value) {
    return value ? { key: value.replace(/^-/, ''), desc: value.charAt(0) === '-' } : null;
  }

  /**
   * Таблица из уже загруженных строк: сортируется на месте. cols: [{label, cls, sort, val(row), cell(row)}];
   * mem.sort — выбранная сортировка (помнится, пока открыт сайт); to(row) — куда ведёт строка.
   */
  function listTable(cols, rows, mem, to) {
    const wrap = h('div');
    function paint() {
      const state = sortState(mem.sort);
      const table = dataTable(cols, state, (key) => {
        mem.sort = nextSort(mem.sort, key);
        paint();
      });
      const col = state ? cols.find((c) => c.sort === state.key) : null;
      const empty = (v) => v === null || v === undefined || v === '';
      const sorted = !col ? rows : rows.slice().sort((a, b) => {
        const x = col.val(a);
        const y = col.val(b);
        if (empty(x) || empty(y)) return empty(x) === empty(y) ? 0 : empty(x) ? 1 : -1;  // без значения — в конце
        const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'ru');
        return state.desc ? -order : order;
      });
      sorted.forEach((row) => table.body.appendChild(tableRow(table, to(row), cols.map((c) => c.cell(row)))));
      wrap.replaceChildren(table.el);
      markOpenRows();
    }
    paint();
    return wrap;
  }

  const TASK_COLS = {
    id: { label: '№', sort: 'id', cls: 'c-id' },
    title: { label: 'Задача', sort: 'title', cls: 'c-title' },
    assignee: { label: 'Исполнитель', sort: 'assignee', cls: 'c-who' },
    status: { label: 'Статус', cls: 'c-status' },
    due: { label: 'Срок', sort: 'deadline', cls: 'c-due' },
    priority: { label: 'Приоритет', sort: 'priority', cls: 'c-opt' },
    weight: { label: 'Вес', sort: 'weight', cls: 'c-num c-opt' },
    score: { label: 'Оценка', sort: 'score', cls: 'c-num c-opt' },
    returns: { label: 'Возвратов', cls: 'c-num c-opt' },
    act: { label: '', cls: 'c-act' },
  };

  /** Строка таблицы задач: те же метки и те же действия, что у строки списка (taskRow). */
  function taskTableRow(table, keys, t, o) {
    const opts = o || {};
    const to = opts.to || '#/task/' + t.id;
    const unaccepted = t.status === 'active' && !t.accepted;
    const open = t.status === 'active' || t.status === 'rework' || t.status === 'proposed';
    const wait = unaccepted ? pill('Не принята', 'warn', 'wait') : null;
    const status = t.overdue && t.status === 'rework' ? pill('на доработке', 'warn', 'undo') : t.overdue ? null : statusPill(t);
    if (status && unaccepted) status.hidden = true;
    const past = t.overdue || (open && Date.parse(t.deadline) <= serverNow());
    let act = null;
    if (opts.onAccept && unaccepted) {
      act = h('button', {
        type: 'button', class: 'row-action', 'aria-label': 'Принять задачу #' + t.id + ' в работу',
        onclick: () => opts.onAccept(t, act, (accepted) => {
          wait.hidden = accepted;
          if (status) status.hidden = !accepted;
        }),
      }, icon('check', 'ic-sm'), 'Принять');
    }
    const cell = {
      id: h('span', { class: 'id' }, '#' + t.id),
      title: cellLink(to, t.title),
      assignee: t.assignee ? own(t.assignee.short_name) : null,
      status: [t.overdue ? pill(t.tail, 'bad', 'clock') : null, wait, status],
      due: h('span', { class: past ? 'tone-bad' : null }, own(t.deadline_local)),
      priority: t.status === 'proposed' ? null : priorityMark(t),
      weight: t.status === 'proposed' ? null : t.weight + ' %',
      score: t.status === 'done' ? own(t.final_score_text) : null,
      returns: t.rework_count ? String(t.rework_count) : null,
      act,
    };
    return tableRow(table, to, keys.map((key) => cell[key]));
  }

  /** «Принять» прямо в строке списка — оптимистично: кнопка сразу сменяется отметкой, метка — на «В работе». */
  async function acceptInline(t, btn, mark) {
    const done = h('span', { class: 'row-done', role: 'status' }, icon('check', 'ic-sm'), 'Принята');
    btn.replaceWith(done);
    mark(true);
    haptic.sel();
    try {
      await api(EP.acceptTask, { params: { task_id: t.id } });
      haptic.ok();
      toast('Задача #' + t.id + ' принята в работу', 'done');
      afterMutation();
    } catch (err) {
      done.replaceWith(btn);
      mark(false);
      reportError(err);
    }
  }

  /** Меню действий — лист со списком: редкие и необратимые действия не стоят рядом с частыми. items: {icon, text, danger, run}. */
  function menuSheet(title, items) {
    sheet.show({
      title,
      body: h('div', { class: 'menu' }, items.map((item) => h('button', {
        type: 'button', class: 'menu-item' + (item.danger ? ' is-danger' : ''),
        onclick: () => {
          sheet.close(false);  // фокус — обратно на «⋯»: туда же он вернётся из следующего листа
          item.run();
        },
      }, icon(item.icon), item.text))),
    });
  }

  function statusChips(value, onChange) {
    let selected = value;
    const items = STATUS_FILTERS.map((f) => {
      const cnt = h('span', { class: 'cnt' });
      const b = chip([f.label, cnt], f.value === selected, () => {
        if (selected === f.value) return;
        selected = f.value;
        items.forEach((x, i) => x.b.setAttribute('aria-pressed', String(STATUS_FILTERS[i].value === selected)));
        safely(() => b.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: scrollBehavior() }));
        onChange(selected);
      });
      return { b, cnt };
    });
    const el = h('div', { class: 'chips', role: 'group', 'aria-label': 'Фильтр по статусу' }, items.map((x) => x.b));

    /** Выбранный чип — целиком в видимой части полосы (прокручивается только полоса, не страница). */
    function revealSelected() {
      const on = items.find((x, i) => STATUS_FILTERS[i].value === selected);
      if (!on || !el.isConnected) return;
      const strip = el.getBoundingClientRect();
      const box = on.b.getBoundingClientRect();
      if (box.right > strip.right - 8) el.scrollLeft += box.right - strip.right + 16;
      else if (box.left < strip.left + 8) el.scrollLeft -= strip.left - box.left + 16;
    }

    requestAnimationFrame(revealSelected);
    return {
      el,
      showAll: () => items[items.length - 1].b.click(),  // «Все» — последний чип
      setCounts(c) {
        items.forEach((x, i) => {
          const n = c ? c[STATUS_FILTERS[i].value] : undefined;
          setText(x.cnt, isNum(n) ? String(n) : '');
        });
        // Счётчики расширили чипы — выбранный (например, «Все» из карточки сотрудника) мог уехать за край.
        requestAnimationFrame(revealSelected);
      },
    };
  }

  /** Список задач с «Показать ещё»: spec.query(page) -> query для GET /api/tasks. */
  function taskList(scr, box, spec) {
    let seq = 0;
    let page = 0;
    let list = null;
    let table = null;  // сайт, большой экран: список — таблицей (spec.table — её столбцы)

    function paint(data, append) {
      if (data.counts && spec.onCounts) spec.onCounts(data.counts);
      const q = spec.searchText ? spec.searchText() : '';
      const notes = data.truncated ? note('Показаны первые 2000 задач — уточните запрос.', 'info') : null;
      if (!append) {
        box.dataset.state = 'ok';
        page = 0;  // список рисуется заново с первой страницы (и свежим ответом поверх прежнего)
        if (!data.items.length) {
          // Пусто — с действием: сбросить поиск, показать все статусы или (начальнику) поставить задачу.
          const all = data.counts && data.counts.all;
          const act = q ? spec.clearSearch && button('Очистить поиск', spec.clearSearch, '', null, 'x')
            : all ? button('Все задачи', spec.showAll, '', null, 'tasks')
              : spec.create && button('Поставить задачу', () => go('#/new'), '', null, 'add');
          box.replaceChildren.apply(box, nodes([
            emptyState(q ? 'По запросу «' + own(q) + '» ничего не найдено.' : 'Задач нет.', q ? 'search' : 'empty', act),
            notes]));
          return;
        }
        table = spec.table && isWide()
          ? dataTable(spec.table.map((key) => TASK_COLS[key]), sortState(spec.sort()), spec.onSort) : null;
        list = table ? table.body : h('ul', { class: 'list' });
        box.replaceChildren.apply(box, nodes([notes, table ? table.el : list]));
      }
      data.items.forEach((t) => list.appendChild(table ? taskTableRow(table, spec.table, t, spec.row) : taskRow(t, spec.row)));
      if (data.page + 1 < data.pages) {
        const more = button('Показать ещё', () => loadMore(more), 'more');
        box.appendChild(more);
      }
      markOpenRows();
    }

    async function reload(fresh) {
      const my = ++seq;
      const gen = cacheGen;
      page = 0;
      const query = spec.query(0);
      const hit = fresh ? null : peek(EP.tasks, { query });
      if (hit) {
        scr.loadedAt = hit.at;
        box.removeAttribute('aria-busy');
        paint(hit.data, false);
        return;
      }
      box.setAttribute('aria-busy', 'true');
      // Прежний список — сразу, свежий придёт следом (те же правила, что у loadInto со spec.stale).
      const old = fresh ? null : stale(EP.tasks, { query });
      const snap = old ? JSON.stringify(old.data) : '';
      if (old) {
        scr.loadedAt = old.at;
        paint(old.data, false);
      } else if (box.dataset.state !== 'ok') box.replaceChildren(skelList(5));
      try {
        const data = await api(EP.tasks, { query, fresh: Boolean(fresh) });
        while (posting) await settled();
        if (!scr.alive || my !== seq) return;
        if (gen !== cacheGen) {
          reload(true);
          return;
        }
        scr.loadedAt = Date.now();
        if (old && snap === JSON.stringify(data)) return;
        paint(data, false);
      } catch (err) {
        if (!scr.alive || my !== seq || err.handled) return;
        haptic.err();
        box.dataset.state = 'error';
        box.replaceChildren(errorState(err, () => reload(true)));
      } finally {
        if (scr.alive && my === seq) box.removeAttribute('aria-busy');
      }
    }

    async function loadMore(more) {
      const my = seq;
      more.disabled = true;
      setText(more, 'Загрузка…');
      try {
        const data = await api(EP.tasks, { query: spec.query(page + 1) });
        // Кнопки уже нет на экране (список перерисован свежим ответом) — её страница не нужна.
        if (!scr.alive || my !== seq || !more.isConnected) return;
        page += 1;
        more.remove();
        paint(data, true);
      } catch (err) {
        more.disabled = false;
        setText(more, 'Показать ещё');
        reportError(err);
      }
    }

    return { reload };
  }

  function searchBox(scr, initial, onSearch) {
    const id = uid('q');
    let timer = 0;
    const input = h('input', {
      id, class: 'input', type: 'search', value: initial || '', maxlength: '100', autocomplete: 'off',
      enterkeyhint: 'search', placeholder: 'Название, сотрудник или #номер',
    });
    const clear = h('button', { type: 'button', class: 'clear', 'aria-label': 'Очистить поиск', hidden: !initial, onclick: () => {
      input.value = '';
      clear.hidden = true;
      clearTimeout(timer);
      onSearch('');
      input.focus();
    } }, icon('x', 'ic-sm'));
    input.addEventListener('input', () => {
      clear.hidden = !input.value;
      clearTimeout(timer);
      timer = setTimeout(() => onSearch(input.value), SEARCH_DELAY_MS);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      clearTimeout(timer);
      onSearch(input.value);
      input.blur();
    });
    scr.onDestroy(() => clearTimeout(timer));
    return {
      el: h('div', { class: 'search', role: 'search' },
        h('label', { for: id, class: 'sr-only' }, 'Поиск задач'),
        h('span', { class: 's-ico' }, icon('search')),
        input, clear),
      input,
      /** Поиск свернули — запрос сбрасывается: скрытый фильтр не должен молча сужать список. */
      reset() {
        if (!input.value) return;
        input.value = '';
        clear.hidden = true;
        clearTimeout(timer);
        onSearch('');
      },
    };
  }

  /**
   * Период — один блок: виды «Неделя · Месяц · Квартал · Год» (одно нажатие) и под ними строка «назад — даты —
   * вперёд». Блок стоит вне блока с данными box: работает и при загрузке, и при ошибке; пока грузится выбранный
   * период, прежние числа в box бледнеют (класс is-switch — до update с новыми данными).
   */
  function periodControl(ps, onChange, box) {
    const label = h('span', { class: 'label', 'aria-live': 'polite' }, '…');
    const arrow = (name, text, by) => h('button', { type: 'button', class: 'icon-btn', 'aria-label': text, onclick: () => {
      if (ps.offset + by > 0 || ps.offset + by < -500) return;
      ps.offset += by;
      box.classList.add('is-switch');
      haptic.sel();
      onChange();
    } }, icon(name));
    const prev = arrow('back', 'Предыдущий период', -1);
    const next = arrow('next', 'Следующий период', 1);
    next.disabled = ps.offset >= 0;
    const seg = segmented(PERIOD_KINDS, ps.kind, (kind) => {
      ps.kind = kind;
      ps.offset = 0;
      setText(label, '…');
      box.classList.add('is-switch');
      onChange();
    }, 'Период');
    return {
      el: h('div', { class: 'period-nav' }, seg.el, h('div', { class: 'period-row' }, prev, label, next)),
      update(period) {
        box.classList.remove('is-switch');
        if (!period) return;
        const short = period.short + ' ';  // «Неделя 12.10–18.10.2026»: вид уже выбран сегментом выше
        setText(label, period.label.indexOf(short) ? period.label : period.label.slice(short.length));
        prev.disabled = !period.has_prev;
        next.disabled = !period.has_next;
      },
    };
  }

  // ---------------------------------------------------------------------------------------------
  // 11. Экраны начальника: команда и карточка сотрудника
  // ---------------------------------------------------------------------------------------------

  const teamPeriod = { kind: 'week', offset: 0 };
  const teamSort = { sort: '' };

  function TeamView(scr) {
    const head = screenHead('Команда');
    const box = h('div');
    const pc = periodControl(teamPeriod, () => load(), box);
    const exportBtn = button('Excel-отчёт в чат', () => exportReport(), 'btn-block', null, 'table');
    scr.el.classList.add('v-wide', 'v-team');
    box.classList.add('dash');
    // nodes(): заявок нет — pendingBanner() даёт null, а append(null) вставил бы текст «null».
    scr.el.append.apply(scr.el, nodes([head.el, pendingBanner(), pc.el, box, exportBtn]));

    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.dashboard, opts: { query: { kind: teamPeriod.kind, offset: teamPeriod.offset } } }),
      skeleton: skelDashboard,
      stale: true,
      render(d) {
        pc.update(d.period);
        return renderTeam(d);
      },
    });

    async function exportReport() {
      exportBtn.disabled = true;
      setLabel(exportBtn, 'wait', 'Готовлю отчёт…');
      try {
        const r = await api(EP.exportReport, { query: { kind: teamPeriod.kind, offset: teamPeriod.offset } });
        haptic.ok();
        toast(r.message || 'Отчёт придёт в чат с ботом через несколько секунд.', 'send');
      } catch (err) {
        reportError(err);
      } finally {
        exportBtn.disabled = false;
        setLabel(exportBtn, 'table', 'Excel-отчёт в чат');
      }
    }

    scr.onCounts = () => {
      const old = scr.el.querySelector('.pending-banner');
      const fresh = pendingBanner();
      if (old && fresh) old.replaceWith(fresh);
      else if (old) old.remove();
      else if (fresh) head.el.after(fresh);
    };
    scr.refresh = () => load(true);
    load();
  }

  /** Заявки на доступ подтверждаются в чате — плашка-кнопка возвращает туда (приложение закрывается). */
  function pendingBanner() {
    const n = counts().pending_users || 0;
    if (!n) return null;
    return h('button', { type: 'button', class: 'note info pending-banner', onclick: toChat }, icon('adduser'),
      h('span', { class: 'note-body' }, h('b', null, 'Заявок на доступ: ' + n), h('br'), 'Перейти в чат и нажать «Сотрудники»'),
      chevron());
  }

  async function toChat() {
    if (WEB) {  // сайт остаётся открытым: чат с ботом — в соседней вкладке (или в приложении Telegram)
      if (CONFIG.bot) safely(() => window.open(CONFIG.bot, '_blank', 'noopener'));
      return;
    }
    // Черновик задачи живёт только в памяти: закрыть приложение с ним можно лишь с согласия.
    if (isAnythingDirty() && !(await confirmDialog(T.leave))) return;
    safely(() => tg.disableClosingConfirmation());
    safely(() => tg.close());
  }

  /**
   * Сводная карточка периода — одна на «Команде» и на экране KPI: число, полоса и под чертой четыре счётчика
   * статусов (те же значки и цвета, что у строк и меток). k — {kpi, kpi_text}, st — счётчики периода.
   */
  function heroCard(title, k, max, side, st) {
    return h('section', { class: 'card hero', 'aria-label': title },
      h('div', { class: 'hero-top' }, kpiNumber(k.kpi_text, k.kpi), h('div', { class: 'hero-side' }, side)),
      h('p', { class: 'hero-cap' }, st.total
        ? [isNum(k.kpi) ? title : 'Оценённых задач в периоде пока нет', ' · ', plural(st.total, 'задача', 'задачи', 'задач')]
        : 'Задач в этом периоде нет.'),
      kpiBar(k.kpi, max, k.kpi_text),
      st.total ? h('div', { class: 'totals', role: 'list', 'aria-label': 'Итоги периода' },
        totalTile('done', 'good', st.done, 'выполнено'),
        totalTile('clock', 'bad', st.overdue_total, 'просрочено'),
        totalTile('work', 'info', st.in_progress, 'в работе'),
        totalTile('review', 'warn', st.on_review, 'на проверке')) : null);
  }

  /** Сверху — то, что зависит от выбранного периода (сводка, сотрудники); график последних недель — ниже. */
  function renderTeam(d) {
    const rows = d.rows || [];
    const max = scaleMax(rows.map((r) => r.kpi).concat([d.team.kpi]));
    const t = d.totals || {};
    return [
      heroCard('Эффективность команды', d.team, max, plural(t.employees || rows.length, 'сотрудник', 'сотрудника', 'сотрудников'), t),
      sec('Сотрудники', rows.length ? String(rows.length) : null),
      rows.length
        ? isWide() ? teamTable(rows, max) : h('ul', { class: 'list' }, rows.map((r) => teamRow(r, max)))
        : emptyState('Активных сотрудников пока нет. Подтвердите заявки в чате с ботом: «Сотрудники».', 'users'),
      h('section', { class: 'card chart-card' }, h('h2', { class: 'card-title' }, trendTitle()), trendChart(d.trend)),
    ];
  }

  /** «Команда» на большом экране: сотрудники — таблицей с сортировкой. */
  function teamTable(rows, max) {
    const stat = (key) => ({
      sort: key, cls: 'c-num',
      val: (r) => (r.stats || {})[key],
      cell: (r) => h('span', { class: key === 'overdue_total' && (r.stats || {})[key] ? 'tone-bad' : null }, String((r.stats || {})[key] || 0)),
    });
    return listTable([
      { label: 'Сотрудник', sort: 'name', cls: 'c-title', val: (r) => r.user.short_name,
        cell: (r) => cellLink('#/team/user/' + r.user.id + '?kind=' + teamPeriod.kind + '&offset=' + teamPeriod.offset, r.user.short_name) },
      { label: 'Должность', cls: 'c-who c-opt', cell: (r) => own(r.user.position) },
      { label: 'KPI', sort: 'kpi', cls: 'c-num', val: (r) => r.kpi,
        cell: (r) => h('b', { class: isNum(r.kpi) ? null : 'muted' }, r.kpi_text) },
      { label: '', cls: 'c-bar c-opt', cell: (r) => kpiBar(r.kpi, max, r.kpi_text, true) },
      Object.assign({ label: 'Выполнено' }, stat('done')),
      Object.assign({ label: 'Просрочено' }, stat('overdue_total')),
      Object.assign({ label: 'В работе' }, stat('in_progress')),
      Object.assign({ label: 'На проверке' }, stat('on_review')),
    ], rows, teamSort, (r) => '#/team/user/' + r.user.id + '?kind=' + teamPeriod.kind + '&offset=' + teamPeriod.offset);
  }

  /** Окно тренда фиксировано (последние недели до сегодня, §8.8) — заголовок говорит об этом прямо,
   *  иначе под «2025 год» график 2026 года выглядел бы как данные выбранного периода. */
  function trendTitle() {
    const n = cfg().trend_weeks || 8;
    return 'Последние ' + plural(n, 'неделя', 'недели', 'недель') + ' (до сегодня)';
  }

  /** Счётчик статуса в сводной карточке: значок в цвете своего статуса, число, подпись. */
  function totalTile(name, tone, value, label) {
    return h('div', { class: 'total', role: 'listitem' },
      h('span', { class: 'v' }, icon(name, 'tone-' + tone), isNum(value) ? String(value) : '0'),
      h('span', { class: 'l' }, label));
  }

  /** Счётчики сотрудника в одну строку: выполнено · просрочено · в работе · на проверке (значки те же, что в сводке); просрочки — красным. */
  function statsLine(st) {
    if (!st || !st.total) return 'задач нет';
    const cell = (name, n, tone) => h('span', { class: 'mini' + (tone && n ? ' tone-' + tone : '') }, icon(name, 'ic-sm'), String(n));
    return dotted([cell('done', st.done), cell('clock', st.overdue_total, 'bad'), cell('work', st.in_progress), cell('review', st.on_review)]);
  }

  function teamRow(r, max) {
    const u = r.user;
    const hash = '#/team/user/' + u.id + '?kind=' + teamPeriod.kind + '&offset=' + teamPeriod.offset;
    return h('li', null, h('button', { type: 'button', class: 'row-main kpi-row', onclick: () => go(hash) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-top' },
          h('span', { class: 'row-title' }, own(u.short_name)),
          h('span', { class: 'kpi-val' + (isNum(r.kpi) ? '' : ' is-empty') }, r.kpi_text)),
        u.position ? h('span', { class: 'row-sub' }, own(u.position)) : null,
        kpiBar(r.kpi, max, r.kpi_text, true),
        h('span', { class: 'meta' },
          h('span', { 'aria-hidden': 'true' }, statsLine(r.stats)),
          h('span', { class: 'sr-only' }, r.stats && r.stats.total
            ? 'Выполнено ' + r.stats.done + ', просрочено ' + r.stats.overdue_total + ', в работе ' + r.stats.in_progress + ', на проверке ' + r.stats.on_review
            : 'Задач нет'))),
      chevron()));
  }

  function normKind(kind) {
    return PERIOD_KINDS.some((k) => k.value === kind) ? kind : null;
  }

  function normOffset(raw) {
    const n = Number(raw);
    return raw !== null && raw !== '' && Number.isInteger(n) ? Math.max(-500, Math.min(0, n)) : null;
  }

  function UserCardView(scr, args, query) {
    const userId = Number(args[0]);
    const ps = {
      kind: normKind(query.get('kind')) || teamPeriod.kind,
      offset: normOffset(query.get('offset')) !== null ? normOffset(query.get('offset')) : teamPeriod.offset,
    };
    kpiView(scr, userId, ps, false);
  }

  const myPeriod = { kind: 'week', offset: 0 };

  function MyKpiView(scr) {
    kpiView(scr, state.me.user.id, myPeriod, true);
  }

  /** Общий вид KPI (§11.6.1): карточка сотрудника у начальника и «Мой KPI» у сотрудника. */
  function kpiView(scr, userId, ps, self) {
    const head = screenHead(self ? 'Мой KPI' : 'Сотрудник', self ? own(state.me.user.full_name) : null);
    const box = h('div', { class: 'dash' });
    const pc = periodControl(ps, () => {
      if (!self) replaceQuery({ kind: ps.kind, offset: ps.offset });
      load();
    }, box);
    scr.el.classList.add('v-wide', 'v-kpi');
    scr.el.append(head.el, pc.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.userKpi, opts: { params: { user_id: userId }, query: { kind: ps.kind, offset: ps.offset } } }),
      skeleton: () => [skelHero(), sk('sk-block skel-on-page'), skelList(3)],
      stale: true,
      render(d) {
        pc.update(d.period);
        if (!self && d.user) {
          head.setTitle(own(d.user.full_name));
          head.setSub([own(d.user.position), d.user.status === 'blocked' ? tr('заблокирован') : null].filter(Boolean).join(' · '));
        }
        return renderKpi(scr, d, ps, self, userId);
      },
    });
    scr.refresh = () => load(true);
    load();
  }

  function renderKpi(scr, d, ps, self, userId) {
    const cur = d.current || { stats: {} };
    const st = cur.stats || {};
    const max = scaleMax([cur.kpi, d.week && d.week.kpi, d.month && d.month.kpi]);
    // Главное — сводная карточка (та же, что на «Команде»); остальные четыре показателя — тихим списком под ней.
    const out = [heroCard('Эффективность за период', cur, max, [
      'Неделя: ', h('b', null, d.week ? d.week.kpi_text : '—'), h('br'),
      'Месяц: ', h('b', null, d.month ? d.month.kpi_text : '—')], st)];
    if (st.total) {
      out.push(h('div', { class: 'stats', role: 'list', 'aria-label': 'Показатели периода' },
        statCell('Выполнение в срок', pct(st.on_time_pct)),
        statCell('Средняя оценка', pct(st.avg_score)),
        statCell('Перевыполнено', String(st.overperformed)),
        statCell('Внесено самостоятельно', String(st.self_initiated))));
    }
    if (!self && isManager()) {
      // Как «Задачи сотрудника» в чате (ListCB scope=emp, status=all): все задачи, включая выполненные.
      out.push(button('Задачи сотрудника', () => go('#/tasks?user=' + userId + '&status=all'), 'more', null, 'tasks'));
    }

    const items = cur.items || [];
    if (items.length) {
      out.push(sec('Вошли в расчёт', String(items.length)));
      out.push(h('ul', { class: 'list' }, items.map((it) => listRow({
        to: '#/task/' + it.task_id, id: it.task_id, title: it.title,
        state: [
          h('span', { class: 'row-due' }, 'вес ' + it.weight + ' % × ' + pct(it.score) + (it.zero_overdue ? ' · просрочена' : '')),
          rowScore(pct(it.score), it.zero_overdue),
        ],
      }))));
    }

    // График последних недель не зависит от выбранного периода — он ниже того, что от периода зависит.
    out.push(h('section', { class: 'card chart-card' }, h('h2', { class: 'card-title' }, trendTitle()), trendChart(d.trend)));
    out.push(historySection(scr, d, ps, userId, self));
    return out;
  }

  function statCell(label, value) {
    return h('div', { class: 'stat', role: 'listitem' }, h('span', { class: 'l' }, label), h('span', { class: 'v' }, value));
  }

  function historySection(scr, d, ps, userId, self) {
    const hist = d.history || { items: [], page: 0, pages: 1, total: 0 };
    const wrap = h('div', null, sec('История оценок', hist.total ? String(hist.total) : null));
    if (!hist.items.length) {
      wrap.appendChild(emptyState('Оценённых задач пока нет.', 'history',
        self && counts().open ? button('Сдать результат', () => go('#/submit'), '', null, 'send') : null));
      return wrap;
    }
    const list = h('ul', { class: 'list' }, hist.items.map(historyRow));
    wrap.appendChild(list);
    let page = hist.page || 0;
    const pages = hist.pages || 1;
    if (page + 1 < pages) {
      const more = button('Показать ещё', async () => {
        more.disabled = true;
        setText(more, 'Загрузка…');
        try {
          const next = await api(EP.userKpi, { params: { user_id: userId }, query: { kind: ps.kind, offset: ps.offset, page: page + 1 } });
          if (!scr.alive) return;
          page = next.history.page;
          next.history.items.forEach((it) => list.appendChild(historyRow(it)));
          if (page + 1 >= next.history.pages) more.remove();
          else {
            more.disabled = false;
            setText(more, 'Показать ещё');
          }
        } catch (err) {
          more.disabled = false;
          setText(more, 'Показать ещё');
          reportError(err);
        }
      }, 'more');
      wrap.appendChild(more);
    }
    return wrap;
  }

  /**
   * Строка истории оценок: слева словами — «Подтверждена» или «Изменена», справа — итоговая оценка;
   * у изменённой перед ней — предварительная («100 % → 90 %»: её дал AI или расчёт по правилам).
   */
  function historyRow(it) {
    const meta = [it.completed_local || '—', 'вес ' + it.weight + ' %'];
    if (it.is_late) meta.push('с опозданием');
    if (it.rework_count) meta.push('доработок: ' + it.rework_count);
    const changed = it.decision === 'changed';
    return listRow({
      to: '#/task/' + it.task_id, id: it.task_id, title: it.title,
      state: [
        changed ? pill('изменена', 'info', 'edit') : it.decision === 'approved' ? pill('подтверждена', '', 'check') : null,
        rowScore([changed && isNum(it.ai_score) ? h('span', { class: 'row-due' }, pct(it.ai_score) + ' →') : null, it.final_score_text]),
      ],
      meta: dotted(meta),
    });
  }

  // ---------------------------------------------------------------------------------------------
  // 12. Списки задач: начальник (#/tasks) и сотрудник (#/my, #/submit)
  // ---------------------------------------------------------------------------------------------

  const tasksFilter = { status: 'open', q: '', userId: '', sort: '' };

  function TasksView(scr, args, query) {
    // ?user=ID&status=… — переход из карточки сотрудника. Адрес потом следует за выбором фильтров
    // (syncAddress), поэтому «Назад» из карточки задачи возвращает выбранное, а не исходный ?user.
    const fromCard = query.get('user');
    if (fromCard && /^\d+$/.test(fromCard)) tasksFilter.userId = fromCard;
    const status = query.get('status');
    if (status && STATUS_FILTERS.some((f) => f.value === status)) tasksFilter.status = status;
    const search = searchBox(scr, tasksFilter.q, (q) => {
      if (q === tasksFilter.q) return;
      tasksFilter.q = q;
      list.reload();
    });
    // Поиск свёрнут в значок у заголовка: строка поиска открыта, пока ею пользуются или в ней есть запрос.
    // На большом экране сайта она открыта всегда — в панели фильтров, рядом с сотрудником и статусами.
    search.el.hidden = !tasksFilter.q && !isWide();
    const toggle = h('button', {
      type: 'button', class: 'icon-btn', hidden: isWide(), 'aria-label': 'Поиск задач', 'aria-expanded': String(!search.el.hidden),
      onclick: () => {
        const show = search.el.hidden;
        search.el.hidden = !show;
        toggle.setAttribute('aria-expanded', String(show));
        haptic.sel();
        if (show) search.input.focus();
        else search.reset();
      },
    }, icon('search'));
    const head = screenHead('Задачи', null, toggle);
    const select = h('select', { class: 'input', id: uid('emp'), 'aria-label': 'Сотрудник' },
      employeeOptions(null, tasksFilter.userId));
    select.value = tasksFilter.userId;
    select.addEventListener('change', () => {
      tasksFilter.userId = select.value;
      syncAddress();
      haptic.sel();
      list.reload();
    });
    const chips = statusChips(tasksFilter.status, (v) => {
      tasksFilter.status = v;
      syncAddress();
      list.reload();
    });

    function syncAddress() {
      if (query.has('user') || query.has('status')) replaceQuery({ user: tasksFilter.userId || null, status: tasksFilter.status });
    }
    const box = h('div');
    scr.el.classList.add('v-wide', 'v-tasks');
    scr.el.append(head.el, search.el, h('div', { class: 'filter-row' }, select), chips.el, box);

    api(EP.employees).then((data) => {
      if (!scr.alive) return;
      const keep = tasksFilter.userId;
      // replaceChildren не раскрывает массивы — список узлов передаётся плоским (nodes).
      select.replaceChildren.apply(select, employeeOptions(data.items || [], keep));
      select.value = keep;
    }).catch(() => { /* фильтр по сотруднику просто останется коротким */ });

    const list = taskList(scr, box, {
      query: (page) => ({
        scope: tasksFilter.userId ? 'emp' : 'all', user_id: tasksFilter.userId || null,
        status: tasksFilter.status, q: tasksFilter.q.trim() || null, page, limit: pageSize(), counts: page === 0 ? 1 : null,
        sort: isWide() ? tasksFilter.sort || null : null,
      }),
      row: { assignee: true },
      table: ['id', 'title', 'assignee', 'status', 'due', 'priority', 'weight', 'score'],
      sort: () => tasksFilter.sort,
      onSort(key) {
        tasksFilter.sort = nextSort(tasksFilter.sort, key);
        list.reload();
      },
      onCounts: chips.setCounts,
      searchText: () => tasksFilter.q.trim(),
      clearSearch: () => search.reset(),
      showAll: chips.showAll,
      create: true,
    });
    scr.refresh = () => list.reload(true);
    list.reload();
  }

  /** Варианты фильтра «Сотрудник»: «Все сотрудники», затем сотрудники (null — список ещё не загружен);
   *  выбранного нет в списке (заблокирован и т. п.) — отдельный вариант «Сотрудник #ID». */
  function employeeOptions(items, selected) {
    const known = (items || []).some((u) => String(u.id) === selected);
    return nodes([
      h('option', { value: '' }, 'Все сотрудники'),
      (items || []).map((u) => h('option', { value: String(u.id) }, own(u.short_name + (u.position ? ' — ' + u.position : '')))),
      selected && !known ? h('option', { value: selected }, 'Сотрудник #' + selected) : null,
    ]);
  }

  const myFilter = { status: 'open', sort: '' };

  function MyTasksView(scr) {
    const head = screenHead('Мои задачи');
    const chips = statusChips(myFilter.status, (v) => {
      myFilter.status = v;
      list.reload();
    });
    const box = h('div');
    scr.el.classList.add('v-wide');
    scr.el.append(head.el, chips.el, box);
    const list = taskList(scr, box, {
      query: (page) => ({
        scope: 'my', status: myFilter.status, page, limit: pageSize(), counts: page === 0 ? 1 : null,
        sort: isWide() ? myFilter.sort || null : null,
      }),
      row: { onAccept: acceptInline },
      table: ['id', 'title', 'status', 'due', 'priority', 'weight', 'score', 'act'],
      sort: () => myFilter.sort,
      onSort(key) {
        myFilter.sort = nextSort(myFilter.sort, key);
        list.reload();
      },
      onCounts: chips.setCounts,
      showAll: chips.showAll,
    });
    scr.refresh = () => list.reload(true);
    list.reload();
  }

  function SubmitListView(scr) {
    const head = screenHead('Сдать результат', 'Выберите задачу, по которой сдаёте результат');
    const box = h('div');
    scr.el.classList.add('v-wide');
    scr.el.append(head.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.tasks, opts: { query: { scope: 'my', status: 'open', limit: 50 } } }),
      skeleton: () => skelList(4),
      stale: true,
      render: (d) => (d.items.length
        ? isWide() ? submitTable(d.items) : h('ul', { class: 'list' }, d.items.map(submitRow))
        // Сдавать нечего, но задачи есть (на проверке, на подтверждении) — кнопка покажет их все.
        : emptyState('Нет задач для сдачи. Здесь появятся задачи в работе и на доработке.', 'inbox',
          counts().review || counts().proposed ? button('Мои задачи', () => {
            myFilter.status = 'all';
            go('#/my');
          }, '', null, 'tasks') : null)),
    });
    scr.refresh = () => load(true);
    load();
  }

  /** «Сдать» на большом экране: те же задачи таблицей, строка ведёт на форму сдачи. */
  function submitTable(items) {
    const keys = ['id', 'title', 'status', 'due', 'priority', 'weight', 'returns'];
    const table = dataTable(keys.map((key) => Object.assign({}, TASK_COLS[key], { sort: null })), null, null);
    items.forEach((t) => table.body.appendChild(taskTableRow(table, keys, t, { to: '#/submit/' + t.id })));
    return table.el;
  }

  /** Строка списка «Сдать»: та же строка задачи, ведёт на форму сдачи. */
  function submitRow(t) {
    return taskRow(t, { to: '#/submit/' + t.id, returns: true });
  }

  // ---------------------------------------------------------------------------------------------
  // 13. Карточка задачи (#/task/{id}) — общая для ролей
  // ---------------------------------------------------------------------------------------------

  function TaskView(scr, args) {
    const taskId = Number(args[0]);
    // Меню «⋯» в шапке: редкие и необратимые действия (отменить, отклонить) — пункты задаёт actionsBlock.
    let menu = [];
    const more = h('button', {
      type: 'button', class: 'icon-btn', hidden: true, 'aria-label': 'Ещё действия', 'aria-haspopup': 'dialog',
      onclick: () => menuSheet('Задача #' + taskId, menu),
    }, icon('more'));
    const head = screenHead('Задача #' + taskId, null, more);
    const box = h('div');
    scr.el.append(head.el, box);
    const poll = pendingPoller(scr, () => load(true));
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelCard,
      stale: true,
      render: (card) => {
        poll((card.submissions || []).some((sb) => sb.ai_pending));
        return renderCard(card);
      },
      onError: () => {  // вместо карточки — ошибка: меню с действиями над ней прячется
        menu = [];
        more.hidden = true;
      },
    });

    function repaint(card) {
      box.replaceChildren.apply(box, nodes(renderCard(card)));
    }

    function renderCard(card) {
      const t = card.task;
      const isM = card.viewer === 'manager';
      const out = [];
      out.push(h('section', { class: 'card' },
        h('h2', { class: 'task-title' }, own(t.title)),
        h('div', { class: 'pills' },
          statusPill(t),
          t.status === 'done' && t.final_score_text ? pill(t.final_score_text, 'good', 'flag') : null),
        line('', t.deadline_label, 'deadline-line', 'calendar'),
        acceptanceLine(t, isM),
        t.auto_note ? autoNote(t.auto_note) : null));
      out.push(actionsBlock(card, isM));
      out.push(h('section', { class: 'card' },
        kvBlock('Ожидаемый результат', t.expected_result),
        t.plan_text ? line('План:', h('b', { class: 'num' }, own(t.plan_text)), '', 'target') : null,
        t.description ? kvBlock('Описание', t.description) : null));
      out.push(h('section', { class: 'card' },
        peopleLines(t, isM).map((p) => line(p[0], p[1], '', p[2])),
        t.weight_pending
          ? line('', 'Вес и приоритет: назначит начальник при подтверждении', '', 'weight')
          : [line('Приоритет:', priorityMark(t), '', 'bolt'), line('Вес:', t.weight + ' %', '', 'weight')],
        t.rework_count ? line('Возвратов на доработку:', String(t.rework_count), '', 'undo') : null));
      out.push(submissionsBlock(card, isM));
      out.push(eventsBlock(card.events));
      return out;
    }

    function actionsBlock(card, isM) {
      const t = card.task;
      const a = t.actions || {};
      const main = [];
      const extra = [];
      if (a.accept) {
        const acceptBtn = button('Принял в работу', () => acceptOnCard(acceptBtn), 'btn-primary btn-block', null, 'check');
        main.push(acceptBtn);
      }
      if (a.submit) main.push(button('Сдать результат', () => go('#/submit/' + t.id), (a.accept ? '' : 'btn-primary ') + 'btn-block', null, 'send'));
      if (a.review) main.push(button('Проверить результат', () => go('#/review/' + t.id), 'btn-primary btn-block', null, 'review'));
      if (a.approve) main.push(button('Подтвердить', () => go('#/proposal/' + t.id), 'btn-primary btn-block', null, 'check'));
      if (a.revise) {
        // Оценку подтвердил бот (начальник не ответил вовремя) — её ещё можно изменить.
        const subs = card.submissions || [];
        const last = subs.length ? subs[subs.length - 1] : null;
        if (last && last.id === a.revise_submission_id) {
          main.push(button('Изменить оценку' + (a.revise_until_text ? ' (до ' + a.revise_until_text + ')' : ''),
            () => scoreSheet(scr, last, { revise: true, onDone: () => load(true) }), 'btn-block', null, 'edit'));
        }
      }
      if (a.edit) extra.push(button('Изменить', () => go('#/task/' + t.id + '/edit'), '', null, 'edit'));
      menu = [];
      if (a.reject) {
        menu.push({ icon: 'reject', text: 'Отклонить поручение', danger: true,
          run: () => rejectSheet(scr, t, () => go('#/review?tab=proposals', { force: true, reset: true })) });
      }
      if (a.cancel) {
        menu.push({ icon: 'ban', text: 'Отменить задачу', danger: true,
          run: () => cancelSheet(scr, t, (task) => {
            card.task = task;
            repaint(card);
          }) });
      }
      more.hidden = !menu.length;
      if (!main.length && !extra.length) return null;
      return h('div', { class: 'actions' }, main, extra.length ? h('div', { class: 'btn-row' }, extra) : null);

      async function acceptOnCard(btn) {
        btn.disabled = true;
        setLabel(btn, 'wait', 'Принимаю…');
        try {
          const r = await api(EP.acceptTask, { params: { task_id: t.id } });
          haptic.ok();
          toast('Задача #' + t.id + ' принята в работу', 'done');
          afterMutation();
          if (scr.alive && r && r.task) {
            card.task = r.task;
            repaint(card);
          }
        } catch (err) {
          if (!scr.alive) return;
          btn.disabled = false;
          setLabel(btn, 'check', 'Принял в работу');
          reportError(err);
        }
      }
    }

    scr.refresh = () => load(true);
    load();
  }

  /** Оценка AI ещё считается: перечитывать экран каждые 5 с, но не дольше 3 мин (один таймер на экран). */
  function pendingPoller(scr, reload) {
    let since = 0;
    let timer = 0;
    return (pending) => {
      if (timer) scr.cancel(timer);
      timer = 0;
      if (!pending) {
        since = 0;
        return;
      }
      if (!since) since = Date.now();
      if (Date.now() - since < PENDING_MAX_MS) timer = scr.later(reload, PENDING_POLL_MS);
    };
  }

  function acceptanceLine(t, isM) {
    if (t.status !== 'active') return null;
    if (!t.accepted) {
      return line('', isM ? 'Исполнитель ещё не подтвердил получение' : 'Вы ещё не подтвердили получение', 'tone-warn', 'wait');
    }
    return line('', 'Принята в работу: ' + fmtDmHm(t.accepted_at), 'muted', 'check');
  }

  /** Пояснение сервера об автоподтверждении: эмодзи в его начале заменяется значком того же смысла. */
  function autoNote(text, cls) {
    return line('', stripLeadingEmoji(text), 'small muted' + (cls ? ' ' + cls : ''), markIcon(text) || 'clock');
  }

  /** Строки «кто»: [подпись, значение, значок]. */
  function peopleLines(t, isM) {
    const out = [];
    if (isM && t.assignee) out.push(['Исполнитель:', own(t.assignee.short_name + (t.assignee.position ? ', ' + t.assignee.position : '')), 'user']);
    if (t.source === 'employee') {
      out.push(['', 'Внесена сотрудником (устное поручение)', 'message']);
      if (t.manager) out.push(['Ответственный начальник:', own(t.manager.short_name), 'boss']);
    } else {
      if (t.created_by) out.push(['Постановщик:', own(t.created_by.short_name), 'boss']);
      if (t.manager && t.created_by && t.manager.id !== t.created_by.id) out.push(['Ответственный начальник:', own(t.manager.short_name), 'boss']);
    }
    return out;
  }

  function submissionsBlock(card, isM) {
    const subs = card.submissions || [];
    if (!subs.length) return null;
    const last = subs[subs.length - 1];
    const out = [
      sec(subs.length > 1 ? 'Сдачи результата' : 'Сдача результата', subs.length > 1 ? String(subs.length) : null),
      h('section', { class: 'card' }, submissionBody(card.task, last, isM)),
    ];
    if (subs.length > 1) {
      out.push(h('details', { class: 'fold', 'data-key': 'prev-subs' },
        h('summary', null, h('span', null, 'Предыдущие попытки · ' + (subs.length - 1))),
        h('div', { class: 'fold-body' }, subs.slice(0, -1).reverse().map((sb) => h('div', { class: 'prev-sub' }, submissionBody(card.task, sb, isM))))));
    }
    return out;
  }

  function submissionBody(task, sb, isM) {
    return [
      h('div', { class: 'sub-head' },
        h('span', { class: 't' }, 'Попытка ' + sb.attempt + ' · ' + sb.created_local),
        pill(sb.late_text, sb.is_late ? 'bad' : 'good', sb.is_late ? 'clock' : 'check')),
      kvBlock('Что сделано', sb.fact_text),
      sb.result_text ? kvBlock('Результат', sb.result_text) : null,
      factLine(sb),
      filesBlock(sb, isM),
      aiBlock(sb, isM),
      decisionBlock(sb),
    ];
  }

  /** «План: 100 → Факт: 110 (110 %)» — готовая строка сервера, без эмодзи в начале. */
  function factLine(sb) {
    return sb.fact_line ? line('', own(stripLeadingEmoji(sb.fact_line)), 'num', 'calc') : null;
  }

  /** Подпись предварительной оценки: AI или расчёт по правилам (значок — по источнику). */
  function aiLabel(ai) {
    return h('div', { class: 'ai-label' }, icon(ai.source === 'ai' ? 'ai' : 'calc', 'ic-sm'), stripLeadingEmoji(ai.label));
  }

  /** Файлы сдачи на экране проверки — свёрнутый блок «Файлы · N»: список и «Прислать файлы в чат». */
  function filesFold(sb) {
    const files = sb.attachments || [];
    if (!files.length) return null;
    return h('details', { class: 'fold', 'data-key': 'files' },
      h('summary', null, h('span', null, icon('clip'), 'Файлы · ' + files.length)),
      h('div', { class: 'fold-body' }, filesBlock(sb, true, true)));
  }

  /** Файлы сдачи: список и (начальнику) «Прислать файлы в чат»; bare — без строки-заголовка «Файлы (N)». */
  function filesBlock(sb, isM, bare) {
    const files = sb.attachments || [];
    if (!files.length) return null;
    const list = h('ul', { class: 'files' }, files.map((f) => h('li', { class: 'file' },
      h('span', { class: 'thumb' }, icon(KIND_ICONS[f.kind] || 'clip')),
      h('span', { class: 'fname' }, own(f.name)),
      isNum(f.size) ? h('span', { class: 'fsize' }, fileSize(f.size)) : null)));
    let send = null;
    if (isM) {
      send = button('Прислать файлы в чат', async () => {
        send.disabled = true;
        try {
          await api(EP.sendFiles, { params: { sub_id: sb.id } });
          haptic.ok();
          toast('Файлы придут в чат с ботом', 'send');
        } catch (err) {
          reportError(err);
        } finally {
          send.disabled = false;
        }
      }, 'btn-block', null, 'send');
    }
    return h('div', { class: 'files-block' },
      bare ? null : line('', 'Файлы (' + files.length + ')' + (isM ? '' : ' — сохранены в чате с ботом'), 'muted', 'clip'),
      list, send);
  }

  function aiBlock(sb, isM) {
    if (isM) {
      if (sb.ai) {
        return h('div', { class: 'ai-box' },
          aiLabel(sb.ai),
          h('div', { class: 'ai-score' }, sb.ai.score_text),
          sb.ai.rationale ? h('p', { class: 'ai-why' }, own(sb.ai.rationale)) : null);
      }
      if (sb.ai_pending) {
        return h('div', { class: 'ai-box', role: 'status' },
          h('div', { class: 'ai-label' }, icon('wait', 'ic-sm'), 'Предварительная оценка ещё рассчитывается'),
          h('p', { class: 'small muted' }, 'Экран обновится сам — обычно это занимает до пары минут.'));
      }
      return null;
    }
    if (sb.ai_hidden) return note('Результат на проверке у начальника. Решение придёт в чат с ботом.', 'info', null, 'review');
    if (sb.ai) return h('p', { class: 'line muted' }, stripLeadingEmoji(sb.ai.label) + ': ' + sb.ai.score_text);
    return null;
  }

  function decisionBlock(sb) {
    if (!sb.decision) return null;
    const out = [];
    if (sb.decision === 'rework') {
      out.push(line('', h('b', null, stripLeadingEmoji(sb.decision_label || 'Возвращено на доработку')), '', 'undo'));
    } else {
      out.push(line('Итоговая оценка:', [h('b', { class: 'num' }, sb.final_score_text || pct(sb.final_score)),
        sb.decision_label ? ' — ' + stripLeadingEmoji(sb.decision_label) : ''], '', 'flag'));
    }
    if (sb.reviewer) {
      out.push(line('', 'Проверка: ' + own(sb.reviewer.short_name) + (sb.reviewed_at ? ', ' + fmtDmHm(sb.reviewed_at) : ''), 'small muted', 'boss'));
    }
    if (sb.review_comment) out.push(kvBlock('Комментарий начальника', sb.review_comment));
    return h('div', { class: 'decision' }, out);
  }

  const EVENT_ICONS = {
    created: 'plus', proposed: 'inbox', approved: 'check', rejected: 'reject', accepted: 'check', edited: 'edit',
    submitted: 'send', ai_evaluated: 'ai', score_confirmed: 'done', score_changed: 'edit', rework: 'undo',
    cancelled: 'ban', reminder: 'clock', weight_suggested: 'weight',
  };

  /**
   * Журнал задачи — лента событий: значок по виду события, что произошло, ниже — когда и кто.
   * Строки приходят с сервера; его эмодзи (у автора-бота, кружок приоритета) не показываются.
   */
  function eventsBlock(events) {
    if (!events || !events.length) return null;
    return h('details', { class: 'fold', 'data-key': 'history' },
      h('summary', null, h('span', null, icon('history'), 'История · ' + events.length)),
      h('div', { class: 'fold-body' }, h('ul', { class: 'events' }, events.map((ev) => h('li', null,
        h('span', { class: 'ev-ico' }, icon(ev.type === 'ai_evaluated' && !/AI/.test(ev.text || '') ? 'calc' : EVENT_ICONS[ev.type] || 'info', 'ic-sm')),
        h('span', { class: 'ev-body' },
          h('span', { class: 'ev-text' }, own(String(ev.text || '').replace(PRIORITY_DOTS, ''))),
          h('span', { class: 'when' }, ev.at_local + ' · ' + own(ev.actor ? ev.actor_name : stripLeadingEmoji(ev.actor_name)))))))));
  }

  // --- Листы действий над задачей -----------------------------------------------------------

  function cancelSheet(scr, t, after) {
    const reason = field({ label: 'Причина (необязательно)', multiline: true, rows: 3, max: 1000, placeholder: 'Например: задача больше не актуальна' });
    sheet.show({
      title: 'Отменить задачу #' + t.id,
      body: h('div', null, h('p', { class: 'muted' }, 'Сотрудник получит уведомление об отмене.'), reason.el),
      dirty: () => reason.get().trim() !== '',
      primary: {
        text: 'Отменить задачу',
        danger: true,
        onClick: async () => {
          if (!reason.validate()) return;
          if (!(await confirmDialog('Отменить задачу #' + t.id + '? Сотрудник получит уведомление.'))) return;
          primary.update({ progress: true }, 'sheet');
          try {
            const r = await api(EP.cancelTask, { params: { task_id: t.id }, body: { reason: reason.get().trim() || null } });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('Задача #' + t.id + ' отменена', r.notice), 'done');
            afterMutation();
            if (scr.alive && r.task) after(r.task);
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
            reportError(err);
          }
        },
      },
    });
  }

  function rejectSheet(scr, t, after) {
    const reason = field({ label: 'Причина (необязательно)', multiline: true, rows: 3, max: 1000, placeholder: 'Например: эту работу уже выполняет другой сотрудник' });
    sheet.show({
      title: 'Отклонить поручение #' + t.id,
      body: h('div', null, h('p', { class: 'muted' }, 'Сотрудник получит уведомление' + ' и увидит причину, если вы её укажете.'), reason.el),
      dirty: () => reason.get().trim() !== '',
      primary: {
        text: 'Отклонить',
        danger: true,
        onClick: async () => {
          if (!reason.validate()) return;
          primary.update({ progress: true }, 'sheet');
          try {
            const r = await api(EP.rejectTask, { params: { task_id: t.id }, body: { reason: reason.get().trim() || null } });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('Поручение #' + t.id + ' отклонено', r.notice), 'done');
            afterMutation();
            if (scr.alive) after(r);
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
            reportError(err);
          }
        },
      },
    });
  }

  // ---------------------------------------------------------------------------------------------
  // 14. Проверка: очередь, сдача, поручение
  // ---------------------------------------------------------------------------------------------

  const reviewState = { seg: 'results', sort: '', proposalsSort: '' };

  function segLabel(text, n) {
    return isNum(n) ? text + ' (' + n + ')' : text;
  }

  function ReviewQueueView(scr, args, query) {
    if (query.get('tab') === 'proposals' || query.get('tab') === 'results') reviewState.seg = query.get('tab');
    const head = screenHead('Проверка');
    const c = counts();
    const pick = (v) => {
      reviewState.seg = v;
      replaceQuery({ tab: v });
      box.dataset.state = '';
      load();
    };
    const seg = segmented([
      { value: 'results', label: segLabel('Результаты', c.review) },
      { value: 'proposals', label: segLabel('Поручения', c.proposals) },
    ], reviewState.seg, pick, 'Что проверять');
    const box = h('div', { role: 'tabpanel' });
    scr.el.classList.add('v-wide', 'v-review');
    scr.el.append(head.el, seg.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: reviewState.seg === 'proposals' ? EP.proposals : EP.review }),
      skeleton: () => skelList(4),
      stale: true,
      render(d) {
        const items = d.items || [];
        const mine = reviewState.seg === 'proposals';
        seg.setLabel(reviewState.seg, mine ? segLabel('Поручения', items.length) : segLabel('Результаты', items.length));
        if (items.length && isWide()) return mine ? proposalsTable(items) : reviewTable(items);
        if (items.length) return h('ul', { class: 'list' }, mine ? items.map((it) => proposalRow(it.task)) : items.map(reviewRow));
        // Пусто здесь, а в соседней очереди что-то ждёт (бейдж вкладки считает обе) — кнопка ведёт туда.
        const to = mine ? 'results' : 'proposals';
        const n = mine ? counts().review : counts().proposals;
        return emptyState(mine ? 'Новых поручений нет.' : 'Нечего проверять', mine ? 'inbox' : 'done',
          n ? button(mine ? segLabel('Результаты', n) : segLabel('Поручения', n), () => {
            seg.set(to);
            pick(to);
          }, '', null, mine ? 'review' : 'inbox') : null);
      },
    });
    scr.onCounts = () => {
      const fresh = counts();
      seg.setLabel('results', segLabel('Результаты', fresh.review));
      seg.setLabel('proposals', segLabel('Поручения', fresh.proposals));
      // Пустое состояние: его кнопка в соседнюю очередь считается по этим же счётчикам.
      if (box.dataset.state === 'ok' && !box.querySelector('.list')) load();
    };
    scr.refresh = () => load(true);
    load();
  }

  /** Строка очереди проверки: сдано в срок или с опозданием, справа — предварительная оценка. */
  function reviewRow(item) {
    const t = item.task;
    const sb = item.submission;
    const ai = sb && sb.ai;
    // Значок у оценки: AI, расчёт по правилам или «ещё считается».
    const mark = ai ? (ai.source === 'ai' ? 'ai' : 'calc') : 'wait';
    return listRow({
      to: '#/review/' + t.id, id: t.id, title: t.title,
      state: [
        sb ? pill(sb.late_text, sb.is_late ? 'bad' : 'good', sb.is_late ? 'clock' : 'check') : null,
        rowScore([
          h('span', { 'aria-hidden': 'true' }, icon(mark, 'ic-sm'), ai ? ai.score_text : null),
          h('span', { class: 'sr-only' }, ai ? 'Предварительная оценка ' + ai.score_text : 'Оценка рассчитывается'),
        ]),
      ],
      meta: dotted([
        own(t.assignee ? t.assignee.short_name : ''),
        sb ? 'сдано ' + sb.created_local : null,
        sb && sb.attempt > 1 ? 'попытка ' + sb.attempt : null,
      ]),
    });
  }

  /** Очередь проверки на большом экране: таблица с сортировкой (вся очередь уже загружена). */
  function reviewTable(items) {
    const who = (it) => (it.task.assignee ? it.task.assignee.short_name : '');
    return listTable([
      { label: '№', sort: 'id', cls: 'c-id', val: (it) => it.task.id, cell: (it) => h('span', { class: 'id' }, '#' + it.task.id) },
      { label: 'Задача', sort: 'title', cls: 'c-title', val: (it) => it.task.title, cell: (it) => cellLink('#/review/' + it.task.id, it.task.title) },
      { label: 'Сотрудник', sort: 'who', cls: 'c-who', val: who, cell: (it) => own(who(it)) },
      { label: 'Сдано', sort: 'sent', cls: 'c-due c-sent', val: (it) => (it.submission ? it.submission.created_at : ''),
        cell: (it) => (it.submission ? own(it.submission.created_local) : null) },
      { label: 'Срок', cls: 'c-status', cell: (it) => {
        const sb = it.submission;
        return sb ? pill(sb.late_text, sb.is_late ? 'bad' : 'good', sb.is_late ? 'clock' : 'check') : null;
      } },
      { label: 'Попытка', sort: 'attempt', cls: 'c-num c-opt', val: (it) => (it.submission ? it.submission.attempt : null),
        cell: (it) => (it.submission ? String(it.submission.attempt) : null) },
      { label: 'Оценка', sort: 'score', cls: 'c-num', val: (it) => (it.submission && it.submission.ai ? it.submission.ai.score : null),
        cell: (it) => {
          const ai = it.submission && it.submission.ai;
          return h('span', { class: 'row-score' },
            h('span', { 'aria-hidden': 'true' }, icon(ai ? (ai.source === 'ai' ? 'ai' : 'calc') : 'wait', 'ic-sm'), ai ? ' ' + ai.score_text : null),
            h('span', { class: 'sr-only' }, ai ? 'Предварительная оценка ' + ai.score_text : 'Оценка рассчитывается'));
        } },
    ], items, reviewState, (it) => '#/review/' + it.task.id);
  }

  function proposalsTable(items) {
    const mem = { get sort() { return reviewState.proposalsSort; }, set sort(v) { reviewState.proposalsSort = v; } };
    const who = (it) => (it.task.assignee ? it.task.assignee.short_name : '');
    return listTable([
      { label: '№', sort: 'id', cls: 'c-id', val: (it) => it.task.id, cell: (it) => h('span', { class: 'id' }, '#' + it.task.id) },
      { label: 'Поручение', sort: 'title', cls: 'c-title', val: (it) => it.task.title, cell: (it) => cellLink('#/proposal/' + it.task.id, it.task.title) },
      { label: 'Сотрудник', sort: 'who', cls: 'c-who', val: who, cell: (it) => own(who(it)) },
      { label: 'Срок', sort: 'due', cls: 'c-due', val: (it) => it.task.deadline, cell: (it) => own(it.task.deadline_local) },
    ], items, mem, (it) => '#/proposal/' + it.task.id);
  }

  /** Строка очереди поручений: та же строка задачи, ведёт на подтверждение. */
  function proposalRow(t) {
    return taskRow(t, { assignee: true, to: '#/proposal/' + t.id, queue: true });
  }

  function backToQueue(tab) {
    go(tab === 'proposals' ? '#/review?tab=proposals' : '#/review', { force: true, reset: true });
  }

  function isAlreadyProcessed(err) {
    return err.status === 400 && /уже обработан/i.test(err.ru || '');
  }

  function ReviewView(scr, args) {
    const taskId = Number(args[0]);
    const head = screenHead('Проверка результата', 'Задача #' + taskId);
    const box = h('div');
    scr.el.append(head.el, box);
    const poll = pendingPoller(scr, () => load(true));
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelCard,
      render: renderReview,
      anchor: true,
    });

    function renderReview(card) {
      const t = card.task;
      const subs = card.submissions || [];
      const sb = subs.length ? subs[subs.length - 1] : null;
      poll(Boolean(sb && sb.ai_pending && t.actions && t.actions.review));
      if (!t.actions || !t.actions.review || !sb) {
        primary.hide();
        return [
          h('section', { class: 'card' }, h('h2', { class: 'task-title' }, own(t.title)), h('div', { class: 'pills' }, statusPill(t))),
          emptyState('Результат уже обработан или ещё не сдан.', 'done', button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true }))),
        ];
      }
      const canConfirm = Boolean(sb.ai && isNum(sb.ai.score));
      primary.set(canConfirm ? { text: 'Подтвердить ' + sb.ai.score_text, onClick: () => confirmScore(t, sb) } : null);
      // Сверху — то, ради чего экран открыли: что сдано, предварительная оценка и решение.
      // Основание (план и факт) — ниже; файлы и журнал свёрнуты.
      return [
        h('section', { class: 'card' },
          h('h2', { class: 'task-title' }, '#' + t.id + ' ' + own(t.title)),
          h('div', { class: 'pills' }, pill(sb.late_text, sb.is_late ? 'bad' : 'good', sb.is_late ? 'clock' : 'check')),
          line('', own(t.assignee ? t.assignee.short_name : '—') + ' · попытка ' + sb.attempt + ' · вес ' + t.weight + ' %', 'muted', 'user')),
        aiReviewCard(sb),
        h('div', { class: 'actions' },
          button('Изменить оценку', () => scoreSheet(scr, sb), 'btn-block', null, 'edit'),
          button('На доработку', () => reworkSheet(scr, t, sb), 'btn-block', null, 'undo')),
        t.auto_note ? autoNote(t.auto_note) : null,
        h('p', { class: 'muted small center decide-note' }, 'Окончательное решение — за начальником.'),
        h('section', { class: 'card' },
          kvBlock('План', t.expected_result, t.plan_text ? line('', h('b', { class: 'num' }, own(t.plan_text)), '', 'target') : null),
          kvBlock('Факт', sb.fact_text),
          sb.result_text ? kvBlock('Результат', sb.result_text) : null,
          // «План → Факт» числом: у оценки AI — в её карточке; у расчёта по правилам (его пояснение само
          // начинается с этих чисел) и без оценки — здесь.
          sb.ai && sb.ai.source === 'ai' ? null : factLine(sb)),
        h('section', { class: 'card' },
          line('Срок:', fmtFull(sb.deadline_at_submit), '', 'calendar'),
          line('Сдано:', [sb.created_local + ' — ', h('span', { class: sb.is_late ? 'tone-bad' : 'tone-good' }, sb.late_text)], '', 'send')),
        filesFold(sb),
        eventsBlock(card.events),
        h('button', { type: 'button', class: 'link-btn center-block', onclick: () => go('#/task/' + t.id) }, 'Открыть карточку задачи'),
      ];
    }

    async function confirmScore(t, sb) {
      primary.update({ progress: true });
      try {
        const r = await api(EP.confirm, { params: { sub_id: sb.id } });
        haptic.ok();
        toast(withNotice('Подтверждено: ' + sb.ai.score_text, r.notice), 'done');
        afterMutation();
        backToQueue('results');
      } catch (err) {
        if (!scr.alive) return;
        primary.update({ progress: false });
        reviewFailed(err);
      }
    }

    scr.refresh = () => load(true);
    load();
  }

  function reviewFailed(err) {
    reportError(err);
    if (isAlreadyProcessed(err)) {
      afterMutation();
      backToQueue('results');
    }
  }

  /** Предварительная оценка на экране проверки. */
  function aiReviewCard(sb) {
    if (sb.ai) {
      // Цвет карточки — по оценке (как у полос KPI): зелёный от 100 %, жёлтый от 80 %, ниже — красный.
      return h('section', { class: 'card ai-card ' + toneOf(sb.ai.score), 'aria-label': 'Предварительная оценка' },
        aiLabel(sb.ai),
        h('div', { class: 'kpi-num' }, String(Math.floor(sb.ai.score + 0.5)), h('span', { class: 'unit' }, '%')),
        sb.ai.source === 'ai' ? factLine(sb) : null,
        sb.ai.rationale ? h('p', { class: 'ai-why' }, own(sb.ai.rationale)) : null,
        sb.ai.model ? h('p', { class: 'small muted mt8' }, 'Модель: ' + own(sb.ai.model)) : null);
    }
    if (sb.ai_pending) {
      return h('section', { class: 'card ai-card', role: 'status' },
        h('div', { class: 'ai-label' }, icon('wait', 'ic-sm'), 'Предварительная оценка ещё рассчитывается'),
        h('p', { class: 'small muted' }, 'Экран обновится сам. Можно не ждать — изменить оценку или вернуть на доработку.'));
    }
    return h('section', { class: 'card ai-card' }, h('div', { class: 'ai-label' }, icon('ai', 'ic-sm'), 'Предварительной оценки нет — выставьте оценку сами.'));
  }

  /** Лист оценки. o.revise — изменить оценку, подтверждённую автоматически (o.onDone — что сделать после). */
  function scoreSheet(scr, sb, o) {
    const revise = Boolean(o && o.revise);
    const maxScore = cfg().max_score || 150;
    const options = (cfg().score_options || []).slice();
    let marked = null;
    if (sb.ai && isNum(sb.ai.score)) {
      marked = Math.max(0, Math.min(maxScore, Math.floor(sb.ai.score + 0.5)));
      if (options.indexOf(marked) < 0) options.push(marked);
      options.sort((a, b) => a - b);
    }
    let value = null;
    const chipsBox = h('div', { class: 'chips wrap grid', role: 'group', 'aria-label': 'Быстрые оценки' });
    const custom = field({
      label: 'Своя оценка, %', inputmode: 'decimal', placeholder: '0–' + maxScore,
      check: (v) => {
        if (!v) return 'Выберите оценку кнопкой или введите число';
        const n = parseLooseNumber(v);
        if (n === null || n < 0 || n > maxScore) return 'Оценка — число от 0 до ' + maxScore;
        return '';
      },
      onInput: (v) => {
        const n = parseLooseNumber(v);
        // Оценка хранится целой (как в чате): «95,5» -> 96 — кнопка сразу показывает то, что запишется.
        value = v.trim() && n !== null && n >= 0 && n <= maxScore ? Math.floor(n + 0.5) : null;
        paintChips();
        sync();
      },
    });
    const comment = field({ label: 'Комментарий для сотрудника (необязательно)', multiline: true, rows: 3, max: 2000, placeholder: 'Например: не хватает расчёта по двум договорам' });

    function paintChips() {
      chipsBox.replaceChildren.apply(chipsBox, options.map((o) => {
        const byAi = sb.ai && sb.ai.source === 'ai';
        const el = chip([o === marked ? icon(byAi ? 'ai' : 'calc', 'ic-sm tone-info') : null, o + ' %'], value === o, () => {
          value = o;
          custom.set('');
          custom.setError('');
          paintChips();
          sync();
        }, o === marked && byAi ? o + ' % — предлагает AI' : null);
        if (o === marked) el.classList.add('is-ai');  // предложенная оценка обведена — её видно и без значка
        return el;
      }));
    }

    function valid() {
      return isNum(value) && value >= 0 && value <= maxScore;
    }

    function sync() {
      primary.update({ text: valid() ? 'Поставить ' + fmtNum(value) + ' %' : 'Выберите оценку', enabled: valid() }, 'sheet');
    }

    paintChips();
    sheet.show({
      title: 'Изменить оценку',
      body: h('div', null,
        revise ? h('p', { class: 'muted' }, 'Сейчас: ', h('b', { class: 'num' }, sb.final_score_text || pct(sb.final_score)), ' — подтверждена автоматически') : null,
        sb.ai ? h('p', { class: 'muted' }, stripLeadingEmoji(sb.ai.label) + ': ', h('b', { class: 'num' }, sb.ai.score_text)) : h('p', { class: 'muted' }, 'Предварительной оценки AI нет.'),
        chipsBox, custom.el, comment.el),
      dirty: () => value !== null || comment.get().trim() !== '',
      primary: {
        text: 'Выберите оценку',
        enabled: false,
        onClick: async () => {
          if (!valid()) {
            custom.validate();
            return;
          }
          if (!comment.validate()) return;
          primary.update({ progress: true }, 'sheet');
          try {
            const r = await api(revise ? EP.reviseScore : EP.setScore, { params: { sub_id: sb.id }, body: { score: value, comment: comment.get().trim() || null } });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('Оценка: ' + fmtNum(value) + ' %', r.notice), 'done');
            afterMutation();
            if (revise) {
              if (o.onDone) o.onDone();
            } else {
              backToQueue('results');
            }
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
            if (revise) {
              // Срок вышел или оценку уже изменил другой начальник: показать причину и обновить карточку.
              reportError(err);
              if (err.status === 400) {
                sheet.close(true);
                if (o.onDone) o.onDone();
              }
              return;
            }
            if (isAlreadyProcessed(err)) sheet.close(true);
            reviewFailed(err);
          }
        },
      },
    });
  }

  function reworkSheet(scr, t, sb) {
    const past = Date.parse(t.deadline) <= serverNow();
    let mode = past ? 'new' : 'keep';
    const dl = { date: '', time: '', touched: false };
    const comment = field({
      label: 'Что нужно доработать?', required: true, multiline: true, rows: 4, max: 2000,
      requiredText: 'Напишите, что нужно доработать',
      placeholder: 'Например: добавьте расчёт по 12 договорам с нарушениями',
    });
    const picker = deadlinePicker(dl, { label: 'Новый срок' });
    const pickerWrap = h('div', { hidden: mode !== 'new' }, picker.el);
    const groupName = uid('rw');
    const keep = h('input', { type: 'radio', name: groupName, value: 'keep', checked: mode === 'keep', disabled: past });
    const fresh = h('input', { type: 'radio', name: groupName, value: 'new', checked: mode === 'new' });
    [keep, fresh].forEach((r) => r.addEventListener('change', () => {
      mode = r.value;
      pickerWrap.hidden = mode !== 'new';
      haptic.sel();
    }));

    sheet.show({
      title: 'Вернуть на доработку',
      body: h('div', null,
        comment.el,
        h('div', { class: 'field', role: 'radiogroup', 'aria-label': 'Срок доработки' },
          h('div', { class: 'label' }, 'Срок доработки'),
          h('label', { class: 'radio-line' + (past ? ' is-disabled' : '') }, keep,
            h('span', null, 'Оставить текущий (' + own(t.deadline_local) + ')',
              past ? h('span', { class: 'why' }, 'срок уже прошёл — укажите новый') : null)),
          h('label', { class: 'radio-line' }, fresh, h('span', null, 'Новый срок'))),
        pickerWrap),
      dirty: () => comment.get().trim() !== '',
      primary: {
        text: 'Вернуть на доработку',
        onClick: async () => {
          if (!firstInvalid([comment, mode === 'new' ? picker : null])) return;
          primary.update({ progress: true }, 'sheet');
          try {
            const body = { comment: comment.get().trim() };
            if (mode === 'new') body.deadline = picker.value();
            const r = await api(EP.rework, { params: { sub_id: sb.id }, body });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('Возвращено на доработку', r.notice), 'undo');
            afterMutation();
            backToQueue('results');
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
            if (!err.handled && /срок/i.test(err.ru || '') && !isAlreadyProcessed(err)) {
              haptic.err();
              if (mode !== 'new') {
                mode = 'new';
                fresh.checked = true;
                pickerWrap.hidden = false;
              }
              picker.setError(err.message);
              return;
            }
            if (isAlreadyProcessed(err)) sheet.close(true);
            reviewFailed(err);
          }
        },
      },
    });
  }

  function ProposalView(scr, args) {
    const taskId = Number(args[0]);
    const head = screenHead('Поручение сотрудника', 'Задача #' + taskId);
    const box = h('div');
    scr.el.append(head.el, box);
    const model = { weight: null, priority: 'medium' };
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelCard,
      render: renderProposal,
    });

    function renderProposal(card) {
      const t = card.task;
      if (!t.actions || !t.actions.approve) {
        primary.hide();
        return [
          h('section', { class: 'card' }, h('h2', { class: 'task-title' }, own(t.title)), h('div', { class: 'pills' }, statusPill(t))),
          emptyState('Поручение уже обработано.', 'done', button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true }))),
        ];
      }
      const expired = Date.parse(t.deadline) <= serverNow();
      const formErr = h('div', { class: 'form-error', role: 'alert' });
      // Вес у поручения пока временный — это подсказка (AI или по умолчанию): он выбран заранее.
      const suggested = isNum(t.weight) && t.weight >= 1 && t.weight <= 100 ? t.weight : null;
      if (model.weight === null && suggested !== null) model.weight = suggested;
      const wp = weightPicker(model);
      const pr = segmented(PRIORITIES, model.priority, (v) => {
        model.priority = v;
      }, 'Приоритет', 'radio');
      api(EP.weightLoad, { params: { user_id: t.assignee.id }, query: { deadline: t.deadline, exclude_task_id: t.id } })
        .then((data) => {
          if (scr.alive) wp.setLoad(data);
        })
        .catch(() => { /* без подсказки о загрузке недели */ });

      async function approve() {
        if (!wp.validate()) {
          haptic.err();
          wp.focus();
          return;
        }
        formErr.replaceChildren();
        primary.update({ progress: true });
        try {
          const r = await api(EP.approveTask, { params: { task_id: t.id }, body: { weight: model.weight, priority: model.priority } });
          haptic.ok();
          toast(withNotice('Поручение #' + t.id + ' подтверждено — задача в работе', r.notice), 'done');
          afterMutation();
          backToQueue('proposals');
        } catch (err) {
          if (!scr.alive) return;
          primary.update({ progress: false });
          if (err.handled) return;
          haptic.err();
          if (/уже обработан/i.test(err.ru || '')) {
            toast(err.message, 'alert');
            afterMutation();
            backToQueue('proposals');
            return;
          }
          formErr.replaceChildren(note(err.message, 'bad', /срок/i.test(err.ru || '')
            ? h('div', { class: 'mt8' }, button('Изменить срок', () => go('#/task/' + t.id + '/edit'), 'btn-sm', null, 'edit'))
            : null));
          safely(() => formErr.scrollIntoView({ block: 'center', behavior: scrollBehavior() }));
        }
      }

      primary.set({ text: 'Подтвердить поручение', onClick: approve });
      return [
        h('section', { class: 'card' },
          h('h2', { class: 'task-title' }, own(t.title)),
          line('', own(t.assignee ? t.assignee.short_name : '—') + ' · внесено сотрудником (устное поручение)', 'muted mt8', 'user'),
          line('', t.deadline_label, 'deadline-line', 'calendar')),
        expired ? note('Срок поручения уже прошёл — сначала измените срок.', 'bad',
          h('div', { class: 'mt8' }, button('Изменить срок', () => go('#/task/' + t.id + '/edit'), 'btn-sm', null, 'edit'))) : null,
        h('section', { class: 'card' },
          kvBlock('Ожидаемый результат', t.expected_result),
          t.plan_text ? line('План:', h('b', { class: 'num' }, own(t.plan_text)), '', 'target') : null,
          t.description ? kvBlock('Описание', t.description) : null),
        sec('Подтвердить'),
        h('section', { class: 'card' },
          h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), pr.el),
          wp.el,
          suggested !== null ? h('p', { class: 'field-hint' }, 'Предлагаемый вес: ' + suggested + ' %') : null),
        t.auto_note && !expired ? autoNote(t.auto_note, 'center') : null,
        formErr,
        h('div', { class: 'btn-row' },
          button('Изменить', () => go('#/task/' + t.id + '/edit'), '', null, 'edit'),
          button('Отклонить', () => rejectSheet(scr, t, () => backToQueue('proposals')), 'btn-danger', null, 'reject')),
      ];
    }

    load();
  }

  // ---------------------------------------------------------------------------------------------
  // 15. Формы: новая задача, поручение, правка
  // ---------------------------------------------------------------------------------------------

  /** Черновики форм на корневых вкладках живут, пока открыто приложение (переключение вкладок их не теряет). */
  const drafts = { newTask: null, propose: null };

  function newDraft() {
    return {
      assignee_id: '', title: '', expected_result: '', description: null, plan_value: '', plan_unit: '',
      deadline: { date: '', time: '', touched: false }, priority: 'medium', weight: null,
    };
  }

  function draftDirty(d) {
    return Boolean(d && (d.assignee_id || d.title.trim() || d.expected_result.trim() || String(d.plan_value).trim()
      || d.deadline.date || (d.weight !== null && d.weight !== undefined)));
  }

  function NewTaskView(scr) {
    if (!drafts.newTask) drafts.newTask = newDraft();
    const d = drafts.newTask;
    const head = screenHead('Новая задача', 'Сотрудник получит её в чате с ботом');
    const box = h('div');
    scr.el.classList.add('v-form2');
    scr.el.append(head.el, box);
    scr.dirty = () => draftDirty(d);
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.employees }),
      skeleton: skelForm,
      keep: true,  // список сотрудников меняется редко, а перерисовка сорвала бы форму под руками
      render: (data) => newTaskForm(scr, d, data.items || [], () => load()),
    });
    load();
  }

  /** Поля задачи из голосового сообщения (ответ POST /api/voice?mode=task) -> в черновик формы. */
  function applyDictation(d, data) {
    const t = (data && data.task) || {};
    if (t.assignee_id) d.assignee_id = String(t.assignee_id);
    if (t.title) d.title = t.title;
    if (t.expected_result) {
      d.expected_result = t.expected_result;
      d.description = data.text && data.text.trim() !== t.expected_result.trim() ? data.text.trim() : null;
      d.plan_value = isNum(t.plan_value) ? fmtNum(t.plan_value) : '';
      d.plan_unit = isNum(t.plan_value) && t.plan_unit ? t.plan_unit : '';
    } else if (!t.title && data && data.text) {
      d.expected_result = data.text; // поля не разобраны — хотя бы сам текст не пропадёт
    }
    if (t.deadline_date) {
      const defTime = cfg().default_deadline_time || '18:00';
      d.deadline = { date: t.deadline_date, time: t.deadline_time || defTime, touched: Boolean(t.deadline_time && t.deadline_time !== defTime) };
    }
  }

  function dictateBar(text, onResult) {
    if (!voiceSupported()) return null;
    return h('div', { class: 'dictate-bar' },
      micButton({ mode: 'task', text, label: text, onResult }),
      h('p', { class: 'field-hint center' }, 'Нажмите, скажите задачу по-русски или по-узбекски и нажмите ещё раз — поля заполнятся сами.'));
  }

  function applySuggestion(d, sg, raw, resultField, plan) {
    d.expected_result = sg.expected_result;
    resultField.set(sg.expected_result);
    if (isNum(sg.plan_value)) plan.set(fmtNum(sg.plan_value), sg.plan_unit || '');
    d.description = raw && raw.trim() !== String(sg.expected_result).trim() ? raw.trim() : null;
  }

  function newTaskForm(scr, d, employees, rerender) {
    if (!employees.length) {
      primary.hide();
      return emptyState(T.noEmployees, 'users', counts().pending_users ? button('Перейти в чат', toChat, '', null, 'message') : null);
    }
    if (d.assignee_id && !employees.some((u) => String(u.id) === d.assignee_id)) d.assignee_id = '';
    const formErr = h('div', { class: 'form-error', role: 'alert' });
    let loadTimer = 0;
    scr.onDestroy(() => clearTimeout(loadTimer));

    const who = selectField({
      label: 'Сотрудник', required: true, value: d.assignee_id, placeholder: 'Выберите сотрудника',
      requiredText: 'Выберите сотрудника',
      options: employees.map((u) => ({ value: String(u.id), label: own(u.short_name + (u.position ? ' — ' + u.position : '')) })),
      onChange: (v) => {
        d.assignee_id = v;
        refreshLoad();
        changed();
      },
    });
    const title = field({
      label: 'Задача', required: true, max: 255, value: d.title, requiredText: 'Напишите, что нужно сделать',
      placeholder: 'Например: Анализ договоров поставщиков', enterkeyhint: 'next',
      onInput: (v) => {
        d.title = v;
        changed();
      },
    });
    const result = field({
      label: 'Ожидаемый результат', required: true, multiline: true, rows: 4, max: 2000, value: d.expected_result,
      requiredText: 'Опишите ожидаемый результат',
      placeholder: 'Своими словами: что должно получиться. Например: проверить 100 договоров и подготовить отчёт о нарушениях',
      onInput: (v) => {
        d.expected_result = v;
        changed();
      },
    });
    const plan = planFields(d, { onChange: changed });
    const ai = aiHelper(scr, {
      getTitle: () => d.title,
      getRaw: () => d.expected_result,
      onAccept: (sg, raw) => {
        applySuggestion(d, sg, raw, result, plan);
        changed();
      },
    });
    result.el.insertBefore(ai.el, result.el.lastChild);
    const deadline = deadlinePicker(d.deadline, {
      onChange: () => {
        refreshLoad();
        changed();
      },
    });
    const priority = segmented(PRIORITIES, d.priority, (v) => {
      d.priority = v;
      changed();
    }, 'Приоритет', 'radio');
    const weight = weightPicker(d, { onChange: changed });

    const guide = formGuide([
      { label: 'Сотрудник', ok: () => Boolean(d.assignee_id) },
      { label: 'Задача', ok: () => Boolean(d.title.trim()) },
      { label: 'Ожидаемый результат', ok: () => Boolean(d.expected_result.trim()) },
      { label: 'Срок', ok: () => Boolean(d.deadline.date) },
      { label: 'Вес задачи', ok: () => weight.filled() },
    ]);

    function changed() {
      ai.refresh();
      guide.refresh();
      syncClosingSoon();
    }

    function refreshLoad() {
      clearTimeout(loadTimer);
      loadTimer = setTimeout(() => {
        const when = deadline.value();
        if (!d.assignee_id || !when) {
          weight.setLoad(null);
          return;
        }
        api(EP.weightLoad, { params: { user_id: d.assignee_id }, query: { deadline: when } })
          .then((data) => {
            if (scr.alive) weight.setLoad(data);
          })
          .catch(() => {
            if (scr.alive) weight.setLoad(null);
          });
      }, 250);
    }

    async function submit() {
      formErr.replaceChildren();
      if (!firstInvalid([who, title, result, plan, deadline, weight])) {
        guide.warn();
        return;
      }
      const body = {
        assignee_id: Number(d.assignee_id), title: d.title.trim(), expected_result: d.expected_result.trim(),
        deadline: deadline.value(), priority: d.priority, weight: d.weight,
      };
      if (d.description && d.description !== body.expected_result) body.description = d.description.slice(0, 2000);
      const pv = String(d.plan_value || '').trim();
      if (pv) {
        body.plan_value = pv;
        if (String(d.plan_unit || '').trim()) body.plan_unit = d.plan_unit.trim();
      }
      primary.update({ progress: true });
      try {
        const r = await api(EP.createTask, { body });
        drafts.newTask = null;
        haptic.ok();
        toast(withNotice('Задача #' + r.task.id + ' поставлена', r.notice), 'done');
        afterMutation();
        syncClosing();
        go('#/task/' + r.task.id, { force: true });
      } catch (err) {
        if (!scr.alive) return;
        primary.update({ progress: false });
        if (err.handled) return;
        haptic.err();
        if (!errorToField(err, [[/срок/i, deadline], [/назван/i, title], [/вес/i, weight], [/план|единиц/i, plan],
          [/результат/i, result], [/сотрудник|исполнител/i, who]])) showFormError(formErr, err);
      }
    }

    primary.set({ text: 'Поставить задачу', onClick: submit });
    refreshLoad();
    const dictate = dictateBar('Надиктовать задачу целиком', (data) => {
      applyDictation(d, data);
      toast('Готово — проверьте поля. Вес и приоритет выберите сами.', 'mic');
      if (rerender) rerender();
    });
    return h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
      dictate,
      formErr,
      // Три блока: что и кому поручаем · к какому сроку · насколько это важно.
      h('section', { class: 'card' }, who.el, title.el, result.el, plan.el),
      h('section', { class: 'card' }, deadline.el),
      h('section', { class: 'card' }, h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), priority.el), weight.el),
      guide.el);
  }

  function ProposeView(scr) {
    if (!drafts.propose) drafts.propose = newDraft();
    const d = drafts.propose;
    const head = screenHead('Поручение', 'Внесите поручение, полученное устно: начальник подтвердит его.');
    const holder = h('div');
    scr.el.classList.add('v-form2');
    scr.el.append(head.el, holder);
    scr.dirty = () => draftDirty(d);
    const build = () => holder.replaceChildren(proposeForm(scr, d, build));
    build();
  }

  function proposeForm(scr, d, rerender) {
    const formErr = h('div', { class: 'form-error', role: 'alert' });
    const title = field({
      label: 'Задача', required: true, max: 255, value: d.title, requiredText: 'Напишите, что нужно сделать',
      placeholder: 'Например: Анализ договоров поставщиков', enterkeyhint: 'next',
      onInput: (v) => {
        d.title = v;
        changed();
      },
    });
    const result = field({
      label: 'Ожидаемый результат', required: true, multiline: true, rows: 4, max: 2000, value: d.expected_result,
      requiredText: 'Опишите ожидаемый результат',
      placeholder: 'Своими словами: что нужно получить. Например: проверить 100 договоров и представить отчёт с нарушениями',
      onInput: (v) => {
        d.expected_result = v;
        changed();
      },
    });
    const plan = planFields(d, { onChange: changed });
    const ai = aiHelper(scr, {
      getTitle: () => d.title,
      getRaw: () => d.expected_result,
      onAccept: (sg, raw) => {
        applySuggestion(d, sg, raw, result, plan);
        changed();
      },
    });
    result.el.insertBefore(ai.el, result.el.lastChild);
    const deadline = deadlinePicker(d.deadline, { onChange: changed });

    const guide = formGuide([
      { label: 'Задача', ok: () => Boolean(d.title.trim()) },
      { label: 'Ожидаемый результат', ok: () => Boolean(d.expected_result.trim()) },
      { label: 'Срок', ok: () => Boolean(d.deadline.date) },
    ]);

    function changed() {
      ai.refresh();
      guide.refresh();
      syncClosingSoon();
    }

    async function submit() {
      formErr.replaceChildren();
      if (!firstInvalid([title, result, plan, deadline])) {
        guide.warn();
        return;
      }
      const body = { title: d.title.trim(), expected_result: d.expected_result.trim(), deadline: deadline.value() };
      if (d.description && d.description !== body.expected_result) body.description = d.description.slice(0, 2000);
      const pv = String(d.plan_value || '').trim();
      if (pv) {
        body.plan_value = pv;
        if (String(d.plan_unit || '').trim()) body.plan_unit = d.plan_unit.trim();
      }
      primary.update({ progress: true });
      try {
        const r = await api(EP.propose, { body });
        drafts.propose = null;
        haptic.ok();
        toast(r.notice || 'Поручение #' + r.task.id + ' отправлено начальнику на подтверждение', r.notice ? null : 'send');
        afterMutation();
        syncClosing();
        go('#/task/' + r.task.id, { force: true });
      } catch (err) {
        if (!scr.alive) return;
        primary.update({ progress: false });
        if (err.handled) return;
        haptic.err();
        if (!errorToField(err, [[/срок/i, deadline], [/назван/i, title], [/план|единиц/i, plan], [/результат/i, result]])) {
          showFormError(formErr, err);
        }
      }
    }

    primary.set({ text: 'Отправить начальнику', onClick: submit });
    const dictate = dictateBar('Надиктовать поручение целиком', (data) => {
      applyDictation(d, data);
      toast('Готово — проверьте поля перед отправкой.', 'mic');
      rerender();
    });
    return h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
      dictate, formErr,
      h('section', { class: 'card' }, title.el, result.el, plan.el),
      h('section', { class: 'card' }, deadline.el),
      guide.el);
  }

  function EditTaskView(scr, args) {
    const taskId = Number(args[0]);
    const head = screenHead('Изменение задачи', 'Задача #' + taskId);
    const box = h('div');
    scr.el.append(head.el, box);
    let form = null;
    scr.guard = () => Boolean(form && form.changed());
    scr.dirty = scr.guard;
    loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelForm,
      render: (card) => {
        const t = card.task;
        if (!t.actions || !t.actions.edit) {
          primary.hide();
          return emptyState('Эту задачу сейчас изменить нельзя: ' + own(statusText(t).toLowerCase()) + '.', 'lock', button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true })));
        }
        form = editForm(scr, t);
        return form.el;
      },
    })();
  }

  function editForm(scr, t) {
    const fields = new Set((t.actions && t.actions.edit_fields) || []);
    const m = {
      title: t.title, expected_result: t.expected_result,
      plan_value: isNum(t.plan_value) ? fmtNum(t.plan_value) : '', plan_unit: t.plan_unit || '',
      deadline: { date: localDate(t.deadline), time: localTime(t.deadline), touched: true },
      priority: t.priority, weight: t.weight,
    };
    const init = JSON.parse(JSON.stringify(m));
    const formErr = h('div', { class: 'form-error', role: 'alert' });
    const main = [];  // что сделать
    const when = [];  // срок
    const rank = [];  // приоритет и вес
    const parts = [];
    let title = null;
    let result = null;
    let plan = null;
    let deadline = null;
    let weight = null;

    if (fields.has('title')) {
      title = field({ label: 'Задача', required: true, max: 255, value: m.title, requiredText: 'Название не может быть пустым', onInput: (v) => { m.title = v; changed(); } });
      main.push(title.el);
    }
    if (fields.has('expected_result')) {
      result = field({ label: 'Ожидаемый результат', required: true, multiline: true, rows: 4, max: 2000, value: m.expected_result, requiredText: 'Опишите ожидаемый результат', onInput: (v) => { m.expected_result = v; changed(); } });
      main.push(result.el);
    }
    if (fields.has('plan')) {
      plan = planFields(m, { onChange: changed });
      main.push(plan.el);
    }
    if (fields.has('deadline')) {
      deadline = deadlinePicker(m.deadline, { onChange: () => { changed(); refreshLoad(); } });
      when.push(deadline.el);
    }
    if (fields.has('priority')) {
      const pr = segmented(PRIORITIES, m.priority, (v) => { m.priority = v; changed(); }, 'Приоритет', 'radio');
      rank.push(h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), pr.el));
    }
    if (fields.has('weight')) {
      weight = weightPicker(m, { onChange: changed });
      rank.push(weight.el);
    }
    if (t.status === 'proposed') parts.push(note('Вес и приоритет назначаются при подтверждении поручения.', 'info', null, 'weight'));

    let loadTimer = 0;
    scr.onDestroy(() => clearTimeout(loadTimer));
    function refreshLoad() {
      if (!weight || !t.assignee) return;
      clearTimeout(loadTimer);
      loadTimer = setTimeout(() => {
        const when = deadline ? deadline.value() : t.deadline;
        api(EP.weightLoad, { params: { user_id: t.assignee.id }, query: { deadline: when, exclude_task_id: t.id } })
          .then((data) => {
            if (scr.alive) weight.setLoad(data);
          })
          .catch(() => { /* без подсказки */ });
      }, 250);
    }

    function changes() {
      // Обе стороны сравнения — без пробелов по краям: после сохранения init берётся из m как есть
      // («7 »), и без обрезки форма считала бы себя изменённой и спрашивала «Выйти без сохранения?».
      const body = {};
      if (title && m.title.trim() !== init.title.trim()) body.title = m.title.trim();
      if (result && m.expected_result.trim() !== init.expected_result.trim()) body.expected_result = m.expected_result.trim();
      if (plan) {
        const pv = String(m.plan_value || '').trim();
        const pu = String(m.plan_unit || '').trim();
        if (pv !== String(init.plan_value || '').trim() || pu !== String(init.plan_unit || '').trim()) {
          if (!pv) body.plan_value = null;
          else {
            body.plan_value = pv;
            body.plan_unit = pu || null;
          }
        }
      }
      if (deadline && (m.deadline.date !== init.deadline.date || m.deadline.time !== init.deadline.time)) {
        body.deadline = m.deadline.date ? m.deadline.date + 'T' + m.deadline.time : null;
      }
      if (fields.has('priority') && m.priority !== init.priority) body.priority = m.priority;
      if (weight && m.weight !== init.weight) body.weight = m.weight;
      return body;
    }

    function hasChanges() {
      return Object.keys(changes()).length > 0;
    }

    function changed() {
      syncClosingSoon();
    }

    async function save() {
      formErr.replaceChildren();
      if (!firstInvalid([title, result, plan, deadline, weight])) return;
      const body = changes();
      if (!Object.keys(body).length) {  // кнопка активна всегда: без правок — просто говорим об этом
        toast('Изменений нет', 'info');
        return;
      }
      primary.update({ progress: true });
      try {
        const r = await api(EP.updateTask, { params: { task_id: t.id }, body });
        Object.assign(init, JSON.parse(JSON.stringify(m)));
        haptic.ok();
        toast(withNotice(r.changed && r.changed.length ? 'Сохранено' : 'Изменений нет', r.notice), r.changed && r.changed.length ? 'done' : 'info');
        afterMutation();
        syncClosing();
        await goBack();
        // Остались на экране (переход отменён) — форма снова рабочая, а не «сохраняется» навсегда.
        if (scr.alive) primary.update({ progress: false });
      } catch (err) {
        if (!scr.alive) return;
        primary.update({ progress: false });
        if (err.handled) return;
        haptic.err();
        if (!errorToField(err, [[/срок/i, deadline], [/назван/i, title], [/вес/i, weight], [/план|единиц/i, plan], [/результат/i, result]])) {
          showFormError(formErr, err);
        }
      }
    }

    primary.set({ text: 'Сохранить', onClick: save });
    refreshLoad();
    return {
      el: h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
        formErr, [main, when, rank].map((kids) => (kids.length ? h('section', { class: 'card' }, kids) : null)), parts),
      changed: hasChanges,
    };
  }

  // ---------------------------------------------------------------------------------------------
  // 16. Сдача результата (#/submit/{id})
  // ---------------------------------------------------------------------------------------------

  function isImageFile(file) {
    return /^image\/(jpeg|png|webp|gif)$/i.test(file.type || '') || /\.(jpe?g|png|webp|gif)$/i.test(file.name || '');
  }

  /** Выбор файлов с превью и проверкой лимитов на клиенте (те же лимиты, что на сервере). */
  function filePicker(scr, items, onChange) {
    const maxCount = cfg().max_files || 10;
    const maxOne = (cfg().max_file_mb || 20) * MB;
    const maxTotal = (cfg().max_total_mb || 50) * MB;
    const id = uid('files');
    const errId = id + '-err';
    const input = h('input', { id, type: 'file', multiple: true, class: 'sr-only', tabindex: '-1', 'aria-hidden': 'true' });
    const pick = button('Выбрать файлы', () => input.click(), 'btn-block', { 'aria-describedby': errId }, 'clip');
    const list = h('ul', { class: 'files' });
    const summary = h('p', { class: 'field-hint' });
    const err = h('p', { class: 'field-error', id: errId, 'aria-live': 'polite' });

    scr.onDestroy(() => items.forEach((it) => it.url && URL.revokeObjectURL(it.url)));

    input.addEventListener('change', () => {
      const picked = Array.from(input.files || []);
      input.value = '';
      let skipped = 0;
      picked.forEach((file) => {
        if (items.length >= maxCount) {
          skipped += 1;
          return;
        }
        items.push({ file, url: isImageFile(file) ? URL.createObjectURL(file) : null });
      });
      paint();
      validate();
      if (skipped) setError('Можно приложить не более ' + maxCount + ' файлов — лишние (' + skipped + ') не добавлены.');
      onChange();
    });

    function problemOf(file) {
      if (file.size === 0) return 'Пустой файл «' + own(file.name) + '»';
      if (file.size > maxOne) return 'Файл «' + own(file.name) + '» больше ' + (cfg().max_file_mb || 20) + ' МБ.';
      return '';
    }

    function paint() {
      list.replaceChildren.apply(list, items.map((it, index) => {
        const bad = problemOf(it.file);
        return h('li', { class: 'file' + (bad ? ' is-bad' : '') },
          it.url ? h('img', { class: 'thumb', src: it.url, alt: '' }) : h('span', { class: 'thumb' }, icon('file')),
          h('span', { class: 'fname' }, it.file.name),
          h('span', { class: 'fsize' }, fileSize(it.file.size)),
          h('button', { type: 'button', class: 'x', 'aria-label': 'Убрать файл «' + own(it.file.name) + '»', onclick: () => {
            const [gone] = items.splice(index, 1);
            if (gone && gone.url) URL.revokeObjectURL(gone.url);
            paint();
            validate();
            onChange();
            pick.focus();
          } }, icon('x', 'ic-sm')));
      }));
      const total = items.reduce((sum, it) => sum + it.file.size, 0);
      setText(summary, items.length
        ? plural(items.length, 'файл', 'файла', 'файлов') + ' · ' + own(fileSize(total)) + ' из ' + (cfg().max_total_mb || 50) + ' МБ'
        : 'До ' + maxCount + ' файлов, каждый до ' + (cfg().max_file_mb || 20) + ' МБ, всего до ' + (cfg().max_total_mb || 50) + ' МБ. Файлы сохранятся в чате с ботом.');
      setLabel(pick, items.length ? 'plus' : 'clip', items.length ? 'Добавить ещё' : 'Выбрать файлы');
      pick.disabled = items.length >= maxCount;
    }

    function setError(message) {
      setText(err, message || '');
    }

    function validate() {
      let message = '';
      const firstBad = items.map((it) => problemOf(it.file)).find(Boolean);
      const total = items.reduce((sum, it) => sum + it.file.size, 0);
      if (items.length > maxCount) message = 'Можно приложить не более ' + maxCount + ' файлов.';
      else if (firstBad) message = firstBad;
      else if (total > maxTotal) message = 'Все файлы вместе — не больше ' + (cfg().max_total_mb || 50) + ' МБ.';
      setError(message);
      return !message;
    }

    paint();
    return {
      el: h('div', { class: 'field' },
        h('div', { class: 'label' }, h('span', null, 'Подтверждающие материалы'), h('span', { class: 'counter' }, 'необязательно')),
        input, list, pick, summary, err),
      validate,
      setError,
      focus: () => pick.focus(),
    };
  }

  function SubmitFormView(scr, args) {
    const taskId = Number(args[0]);
    const head = screenHead('Сдача результата', 'Задача #' + taskId);
    const box = h('div');
    scr.el.append(head.el, box);
    let form = null;
    scr.guard = () => (form ? form.guard() : false);
    scr.dirty = () => Boolean(form && form.guard());
    loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelForm,
      render: (card) => {
        const t = card.task;
        if (!t.actions || !t.actions.submit) {
          primary.hide();
          return [
            h('section', { class: 'card' }, h('h2', { class: 'task-title' }, own(t.title)), h('div', { class: 'pills' }, statusPill(t))),
            emptyState(NOT_OPEN_TEXTS[t.status] || 'Задача не в работе — сдать результат нельзя.', 'info',
              button('К моим задачам', () => go('#/my', { reset: true }), '', null, 'tasks')),
          ];
        }
        form = submitForm(scr, t, box);
        return form.el;
      },
    })();
  }

  function submitForm(scr, t, box) {
    const m = { fact: '', result: '', value: '', materials: '', files: [] };
    let uploading = false;
    let sent = false;
    const formErr = h('div', { class: 'form-error', role: 'alert' });
    const bar = h('i');
    const progress = h('div', { class: 'progress', hidden: true, role: 'progressbar', 'aria-label': 'Отправка', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0' }, bar);

    const fact = field({
      label: 'Что фактически сделано?', required: true, multiline: true, rows: 4, max: 3000, min: 3,
      requiredText: 'Опишите, что сделано', minText: 'Опишите чуть подробнее, пожалуйста.',
      placeholder: 'Например: Проверено 110 договоров, в 12 выявлены нарушения',
      onInput: (v) => {
        m.fact = v;
        changed();
      },
    });
    const result = field({
      label: 'Какой получен результат?', multiline: true, rows: 3, max: 3000,
      placeholder: 'Например: Подготовлен отчёт и рекомендации по нарушениям',
      onInput: (v) => {
        m.result = v;
        changed();
      },
    });
    const value = isNum(t.plan_value) ? field({
      label: 'Фактическое значение', inputmode: 'decimal', placeholder: 'Например: 110',
      hint: 'План: ' + own(t.plan_text || fmtNum(t.plan_value)),
      check: (v) => {
        if (!v) return '';
        const n = parseLooseNumber(v);
        if (n === null) return 'Не понял число. Введите, например: 110';
        if (n < 0) return 'Число не может быть отрицательным';
        return '';
      },
      onInput: (v) => {
        m.value = v;
        changed();
      },
    }) : null;
    const files = filePicker(scr, m.files, () => changed());
    const materials = field({
      label: 'Где лежат материалы', multiline: true, rows: 2, max: 1500,
      placeholder: 'Например: ссылка на папку или «отправил по почте»',
      onInput: (v) => {
        m.materials = v;
        changed();
      },
    });

    function dirty() {
      return !sent && Boolean(m.fact.trim() || m.result.trim() || m.value.trim() || m.materials.trim() || m.files.length);
    }

    const guide = formGuide([{ label: 'Что фактически сделано?', ok: () => m.fact.trim().length >= 3 }]);

    function changed() {
      guide.refresh();
      syncClosingSoon();
    }

    function setProgress(fraction) {
      const n = Math.max(0, Math.min(100, Math.floor(fraction * 100)));
      bar.style.width = n + '%';
      progress.setAttribute('aria-valuenow', String(n));
      primary.update({ progressText: n >= 100 ? 'Передаю файлы в Telegram…' : 'Отправка… ' + n + ' %' });
    }

    async function send() {
      formErr.replaceChildren();
      if (!firstInvalid([fact, result, value, files, materials])) {
        guide.warn();
        return;
      }
      const fd = new FormData();
      fd.append('fact_text', m.fact.trim());
      if (m.result.trim()) fd.append('result_text', m.result.trim());
      if (value && m.value.trim()) fd.append('fact_value', m.value.trim());
      if (m.materials.trim()) fd.append('materials_text', m.materials.trim());
      m.files.forEach((it) => fd.append('files', it.file, it.file.name));
      uploading = true;
      progress.hidden = false;
      primary.update({ progress: true, progressText: 'Отправка… 0 %' });
      try {
        const r = await upload(EP.submitTask, { task_id: t.id }, fd, setProgress);
        uploading = false;
        sent = true;
        haptic.ok();
        afterMutation();
        syncClosing();
        if (!scr.alive) {
          toast('Результат по задаче #' + t.id + ' отправлен начальнику на проверку.', 'done');
          return;
        }
        primary.hide();
        const doneHead = h('h2', { tabindex: '-1' }, 'Результат отправлен');
        box.replaceChildren(h('div', { class: 'done-screen', role: 'status' },
          h('div', { class: 'big' }, icon('done', 'ic-xl')),
          doneHead,
          h('p', null, 'Результат отправлен начальнику на проверку. Решение придёт в чат с ботом.'),
          r.files ? line('', 'Файлы сохранены в чате с ботом', 'center', 'clip') : null,
          button('К моим задачам', () => go('#/my', { reset: true }), 'btn-primary btn-block mt12', null, 'tasks')));
        safely(() => doneHead.focus({ preventScroll: true }));
        window.scrollTo(0, 0);
      } catch (err) {
        uploading = false;
        if (!scr.alive) return;
        progress.hidden = true;
        primary.update({ progress: false, progressText: null });
        if (err.handled) return;
        haptic.err();
        if (err.status === 413 || /файл/i.test(err.ru || '')) {
          files.setError(err.message);
          safely(() => files.focus());
          return;
        }
        if (!errorToField(err, [[/подробнее|сделано|факт/i, fact], [/числ|значени/i, value], [/материал/i, materials]])) {
          showFormError(formErr, err);
        }
      }
    }

    primary.set({ text: 'Отправить', onClick: send });

    const intro = h('section', { class: 'card' },
      h('h2', { class: 'task-title' }, 'Задача #' + t.id + ': ' + own(t.title)),
      h('div', { class: 'mt12' }, kvBlock('Ожидаемый результат', t.expected_result,
        t.plan_text ? line('План:', h('b', { class: 'num' }, own(t.plan_text)), '', 'target') : null)),
      line('Срок:', t.deadline_label, '', 'calendar'),
      t.attempts > 0 ? line('', 'Попытка сдачи №' + (t.attempts + 1), '', 'refresh') : null);
    const reworkNote = t.status === 'rework'
      ? note('Задача возвращена на доработку', 'warn', t.rework_comment ? h('p', { class: 'line pre mt8' }, 'Комментарий начальника: ' + own(t.rework_comment)) : null, 'undo')
      : null;
    const lateNote = t.overdue ? note('Срок уже прошёл — результат будет отмечен как сданный с опозданием.', 'bad', null, 'clock') : null;

    return {
      el: [intro, reworkNote, lateNote,
        h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
          formErr,
          // Два блока: что сделано · чем это подтверждается.
          h('section', { class: 'card' }, fact.el, result.el, value ? value.el : null),
          h('section', { class: 'card' }, files.el, materials.el),
          guide.el, progress)],
      guard: () => (uploading ? T.leaveUpload : dirty()),
    };
  }

  // ---------------------------------------------------------------------------------------------
  // 17. Полноэкранные состояния: вне Telegram, доступ, сессия, ошибка запуска
  // ---------------------------------------------------------------------------------------------

  const BLOCK_SCREENS = {
    outside: { icon: 'phone', title: 'Откройте из Telegram', action: null },
    session: { icon: 'clock', title: 'Сессия устарела', action: 'Закрыть' },
    not_registered: { icon: 'adduser', title: 'Нужна регистрация', action: 'Перейти в чат' },
    unregistered: { icon: 'adduser', title: 'Нужна регистрация', action: 'Перейти в чат' },
    pending: { icon: 'wait', title: 'Заявка на рассмотрении', action: 'Перейти в чат' },
    blocked: { icon: 'ban', title: 'Доступ закрыт', action: 'Перейти в чат' },
  };

  function teardown() {
    dropScreens();
    current = null;
    document.body.classList.remove('has-detail');
    sheet.close(true);
    primary.hide('sheet');
    primary.hide('screen');
    back.set(false);
    shell = null;
    if (dock) dock.remove();
    dock = null;
    setCssPx('--dock-h', 0);
    drafts.newTask = null;
    drafts.propose = null;
    closingOn = true;  // принудительно выключить подтверждение закрытия
    syncClosing();
  }

  function showBlocking(kind, message) {
    teardown();
    const conf = BLOCK_SCREENS[kind] || BLOCK_SCREENS.session;
    setDocTitle(conf.title);
    const heading = h('h1', { tabindex: '-1' }, conf.title);
    const action = conf.action && insideTelegram
      ? button(conf.action, () => safely(() => tg.close()), 'btn-primary')
      : null;
    // Сайт: регистрация и заявка — в чате с ботом; под другим человеком можно войти после выхода.
    const webActions = WEB ? [
      CONFIG.bot ? h('a', { class: 'btn btn-primary', href: CONFIG.bot, target: '_blank', rel: 'noopener noreferrer' }, icon('send'), 'Открыть чат с ботом') : null,
      button('Выйти', logout, '', null, 'logout'),
    ] : null;
    appRoot.replaceChildren(h('div', { class: 'fullscreen', role: 'alert' },
      h('div', { class: 'big' }, icon(conf.icon, 'ic-xl')),
      heading,
      h('p', null, stripLeadingEmoji(message || T.generic)),
      action, webActions));
    appRoot.removeAttribute('aria-busy');
    safely(() => heading.focus({ preventScroll: true }));
  }

  function showBootError(err) {
    teardown();
    const heading = h('h1', { tabindex: '-1' }, 'Не удалось загрузить');
    appRoot.replaceChildren(h('div', { class: 'fullscreen', role: 'alert' },
      h('div', { class: 'big' }, icon(err.status === 0 ? 'offline' : 'alert', 'ic-xl')),
      heading,
      h('p', null, stripLeadingEmoji(err.message || T.generic)),
      button(T.retry, () => {
        appRoot.replaceChildren(bootSkeleton());
        start();
      }, 'btn-primary', null, 'refresh')));
    appRoot.removeAttribute('aria-busy');
    safely(() => heading.focus({ preventScroll: true }));
  }

  function bootSkeleton() {
    return h('div', { class: 'screen', 'aria-busy': 'true' },
      h('div', { class: 'screen-head' }, sk('sk-line w40 sk-title')),
      skelHero(),
      skelList(4));
  }

  // ---------------------------------------------------------------------------------------------
  // 17b. Сайт: вход через подтверждение в Telegram и выход (MINIAPP_SPEC §15)
  // ---------------------------------------------------------------------------------------------

  const AUTH = { start: '/auth/start', status: '/auth/status', cancel: '/auth/cancel', logout: '/auth/logout' };
  const LOGIN_POLL_MS = 2000;
  const LOGIN_NOTES = {
    out: 'Вы вышли. Чтобы продолжить работу, войдите снова.',
    ended: 'Вход завершён. Войдите снова.',
  };
  let loginOn = false;   // показан экран входа (любой из них)
  let loginWait = null;  // идущее ожидание подтверждения: {data, timer, clock, paint}
  let loginLang = null;  // язык, выбранный на экране входа: после входа он становится языком человека

  /** Язык до входа: выбранный здесь раньше, иначе язык браузера. */
  function storedLang() {
    const saved = safely(() => window.localStorage.getItem('kpi_lang'));
    if (saved === 'uz' || saved === 'ru') return saved;
    return String(navigator.language || '').toLowerCase().slice(0, 2) === 'uz' ? 'uz' : 'ru';
  }

  function stopLoginWait() {
    if (!loginWait) return;
    clearTimeout(loginWait.timer);
    clearInterval(loginWait.clock);
    loginWait = null;
  }

  /** Каркас экранов входа: язык — справа вверху, содержимое — по центру. redraw рисует тот же экран на другом языке. */
  function loginFrame(redraw, kids) {
    teardown();
    loginOn = true;
    setDocTitle('Вход');
    const next = i18n.lang === 'uz' ? 'ru' : 'uz';
    const lang = h('button', {
      type: 'button', class: 'lang-btn', lang: next,
      onclick: () => {
        safely(() => window.localStorage.setItem('kpi_lang', next));
        loginLang = next;
        setLang(next);
        redraw();
      },
    }, icon('globe', 'ic-sm'), own(next === 'uz' ? 'Oʻzbekcha' : 'Русский'));
    const box = h('div', { class: 'fullscreen login-box' }, kids);
    appRoot.replaceChildren(h('div', { class: 'login' }, h('div', { class: 'login-top' }, lang), box));
    appRoot.removeAttribute('aria-busy');
    const heading = box.querySelector('h1');
    if (heading) safely(() => heading.focus({ preventScroll: true }));
  }

  function showLogin(reason) {
    stopLoginWait();
    loginFrame(() => showLogin(reason), [
      h('div', { class: 'big' }, icon('lock', 'ic-xl')),
      h('h1', { tabindex: '-1' }, own(appName())),
      h('p', { class: 'muted' }, 'Задачи, сдача результатов и KPI — в браузере, как в Telegram.'),
      LOGIN_NOTES[reason] ? note(LOGIN_NOTES[reason], 'info') : null,
      button('Войти через Telegram', beginLogin, 'btn-primary', null, 'send'),
      h('p', { class: 'small muted' }, 'Пароль не нужен: вход подтверждается в боте.'),
    ]);
  }

  async function beginLogin(event) {
    const btn = event && event.currentTarget;
    if (btn) btn.disabled = true;
    let data;
    try {
      data = await send('POST', AUTH.start, undefined, POST_TIMEOUT_MS, true);
    } catch (err) {
      if (btn) btn.disabled = false;
      toast(err.status === 429 ? 'Слишком много попыток входа подряд. Повторите через минуту.'
        : err.status === 503 ? 'Не удалось связаться с Telegram. Попробуйте ещё раз через минуту.'
          : err.message, 'alert');
      return;
    }
    data.opened = false;
    enterLoginWait(data);
  }

  /** Экран ожидания: data — {code, expires_in, opened} и, если вход начат в этой вкладке, link и qr. */
  function enterLoginWait(data) {
    stopLoginWait();
    data.until = Date.now() + data.expires_in * 1000;
    loginWait = { data, timer: 0, clock: 0, paint: null };
    drawLoginWait();
    const wait = loginWait;
    wait.clock = setInterval(() => wait.paint && wait.paint(), 1000);
    pollLogin();
  }

  /** Вход подтверждён: открыть приложение. */
  function enterApp() {
    stopLoginWait();
    loginOn = false;
    appRoot.replaceChildren(bootSkeleton());
    start();
  }

  /**
   * Сессии нет, но вход мог быть начат в этой вкладке до перезагрузки (или в соседней): спросить сервер один
   * раз. Подтверждённый вход не теряется; идущая попытка продолжается с тем же числом; иначе — экран входа.
   */
  async function resumeLogin() {
    let res = null;
    try {
      res = await send('GET', AUTH.status, undefined, GET_TIMEOUT_MS, true);
    } catch (_) {
      res = null;
    }
    if (res && res.state === 'ok') enterApp();
    else if (res && res.state === 'waiting' && res.code) enterLoginWait({ code: res.code, opened: Boolean(res.opened), expires_in: res.expires_in });
    else showLogin();
  }

  /** «Отмена»: попытка закрывается и на сервере — её кнопки в боте больше ничего не подтверждают. */
  function cancelLogin() {
    send('POST', AUTH.cancel, undefined, POST_TIMEOUT_MS, true).catch(() => {});
    showLogin();
  }

  /** QR-код ссылки: тёмные модули — одним контуром; поля в 4 модуля нужны камере. */
  function qrImage(rows) {
    const n = rows.length;
    let d = '';
    rows.forEach((row, y) => {
      for (let x = 0; x < n; x += 1) {
        if (row.charAt(x) !== '1') continue;
        let len = 1;
        while (x + len < n && row.charAt(x + len) === '1') len += 1;
        d += 'M' + (x + 4) + ' ' + (y + 4) + 'h' + len + 'v1h-' + len + 'z';
        x += len - 1;
      }
    });
    const side = n + 8;
    return s('svg', { class: 'qr', viewBox: '0 0 ' + side + ' ' + side, role: 'img', 'aria-label': tr('QR-код для входа через Telegram') },
      s('rect', { width: side, height: side }), s('path', { d }));
  }

  function drawLoginWait() {
    const wait = loginWait;
    if (!wait) return;
    const d = wait.data;
    const status = h('p', { class: 'login-status', role: 'status' });
    wait.paint = () => {
      status.replaceChildren();
      appendKids(status, [
        icon(d.opened ? 'phone' : 'wait', 'ic-sm'),
        h('span', null, d.opened ? 'Бот открыт — нажмите в нём число с этого экрана.' : 'Ждём подтверждения в Telegram.'),
        h('span', { class: 'num' }, own(fmtClock(Math.max(0, d.until - Date.now())))),
      ]);
    };
    loginFrame(drawLoginWait, [
      h('h1', { tabindex: '-1' }, 'Подтвердите вход в Telegram'),
      h('div', { class: 'login-steps' },
        // Сначала число: в Telegram человек уходит, уже зная, что нажать.
        h('section', { class: 'card login-step' },
          h('h2', { class: 'card-title' }, 'Шаг 1. Запомните число'),
          h('div', { class: 'login-code num' }, own(d.code)),
          h('p', { class: 'small muted' }, 'Бот покажет три числа — нажмите это.')),
        d.link ? h('section', { class: 'card login-step' },
          h('h2', { class: 'card-title' }, 'Шаг 2. Откройте бота и нажмите это число'),
          h('a', { class: 'btn btn-primary', href: d.link, target: '_blank', rel: 'noopener noreferrer' }, icon('send'), 'Открыть Telegram'),
          h('p', { class: 'small muted' }, 'Telegram только на телефоне? Наведите камеру телефона на этот код:'),
          qrImage(d.qr))
          // Страницу перезагрузили: ссылку сервер второй раз не выдаёт — сообщение бота у человека уже есть.
          : h('section', { class: 'card login-step' },
            h('h2', { class: 'card-title' }, 'Шаг 2. Нажмите это число в чате с ботом'),
            h('p', null, 'Сообщение от бота уже должно быть в Telegram. Если его нет — начните заново.'),
            button('Начать заново', beginLogin, '', null, 'refresh'))),
      status,
      h('button', { type: 'button', class: 'link-btn', onclick: cancelLogin }, 'Отмена'),
    ]);
    wait.paint();
  }

  function pollLogin() {
    const wait = loginWait;
    if (!wait) return;
    wait.timer = setTimeout(async () => {
      let res = null;
      try {
        res = await send('GET', AUTH.status, undefined, GET_TIMEOUT_MS, true);
      } catch (_) {
        res = null;  // связь моргнула — спросим ещё раз
      }
      if (loginWait !== wait) return;  // вход отменён или начат заново
      const st = res && res.state;
      if (st === 'ok') {
        enterApp();
      } else if (st === 'denied') {
        loginResult('denied');
      } else if (st === 'expired' || st === 'none' || Date.now() > wait.data.until + 5000) {
        // Знак страницы уже есть — вход завершён в соседней вкладке этого браузера.
        if (pageToken() !== '1') enterApp();
        else loginResult('expired');
      } else if (st === 'waiting' && res.code && res.code !== wait.data.code) {
        showLogin();  // вход начат заново в соседней вкладке: это число уже не действует
      } else {
        if (st === 'waiting' && res.opened !== wait.data.opened) {
          wait.data.opened = res.opened;
          wait.paint();
        }
        pollLogin();
      }
    }, LOGIN_POLL_MS);
  }

  function loginResult(kind) {
    stopLoginWait();
    const denied = kind === 'denied';
    loginFrame(() => loginResult(kind), [
      h('div', { class: 'big' }, icon(denied ? 'reject' : 'clock', 'ic-xl')),
      h('h1', { tabindex: '-1' }, denied ? 'Вход не подтверждён' : 'Время вышло'),
      h('p', null, denied
        ? 'Бот не подтвердил вход: нажато другое число или «Это не я», либо доступа пока нет. Подробности — в чате с ботом.'
        : 'Подтвердить вход нужно за 5 минут. Начните заново.'),
      button('Начать заново', beginLogin, 'btn-primary', null, 'refresh'),
    ]);
  }

  async function logout() {
    if (isAnythingDirty() && !(await confirmDialog(T.leave))) return;
    try {
      await send('POST', AUTH.logout, undefined, POST_TIMEOUT_MS, true);
    } catch (err) {
      toast(err.message, 'alert');  // связи нет: сессия на сервере цела — так и остаёмся в приложении
      return;
    }
    dropCache();
    state.me = null;
    state.role = null;
    safely(() => history.replaceState(null, '', window.location.pathname));  // следующий вход начнёт с главного экрана
    showLogin('out');
  }

  /** «Выйти» в шапке главных экранов сайта (на узком экране — только значок). */
  function logoutButton() {
    return h('button', { type: 'button', class: 'lang-btn out-btn', 'aria-label': 'Выйти', onclick: logout },
      icon('logout', 'ic-sm'), h('span', null, 'Выйти'));
  }

  // ---------------------------------------------------------------------------------------------
  // 17a. Словарь узбекского языка: «скелет» русской строки -> узбекская строка (см. раздел 0a)
  // ---------------------------------------------------------------------------------------------

  const UZ = {
    /*UZ-BEGIN*/
    ", требуют внимания: {}": ", eʼtibor talab qiladi: {}",
    "KPI по неделям:": "Haftalar boʻyicha KPI:",
    "KPI: {u}, отметка — {} %": "KPI: {u}, belgi — {} %",
    "{} % — предлагает AI": "{} % — AI taklifi",
    "{} %, неделя сотрудника будет перегружена": "{} %, xodimning haftasi ortiqcha yuklanadi",
    "{} задач": "{} ta vazifa",
    "{} задача": "{} ta vazifa",
    "{} задачи": "{} ta vazifa",
    "{} сотрудник": "{} ta xodim",
    "{} сотрудника": "{} ta xodim",
    "{} сотрудников": "{} ta xodim",
    "{} файл · {u} из {} МБ": "{} ta fayl · {u} / {} MB",
    "{} файла · {u} из {} МБ": "{} ta fayl · {u} / {} MB",
    "{} файлов · {u} из {} МБ": "{} ta fayl · {u} / {} MB",
    "Не принята": "Qabul qilinmagan",
    "попытка {}": "{}-urinish",
    "Активных сотрудников пока нет. Подтвердите заявки в чате с ботом: «Сотрудники».": "Faol xodimlar hali yoʻq. Arizalarni bot bilan chatda tasdiqlang: «Xodimlar».",
    "Быстрые оценки": "Tezkor baholar",
    "Быстрый выбор срока": "Muddatni tez tanlash",
    "В работе": "Bajarilmoqda",
    "Вернуть на доработку": "Qayta ishlashga qaytarish",
    "Вес задачи": "Vazifa vazni",
    "Вес и приоритет: назначит начальник при подтверждении": "Vazn va ustuvorlik: boshliq tasdiqlashda belgilaydi",
    "Вес — целое число от {} до {}": "Vazn — {} dan {} gacha butun son",
    "Внесена сотрудником (устное поручение)": "Xodim kiritgan (ogʻzaki topshiriq)",
    "Внесено самостоятельно": "Oʻzi kiritgan",
    "Внесите поручение, полученное устно: начальник подтвердит его.": "Ogʻzaki olingan topshiriqni kiriting: boshliq uni tasdiqlaydi.",
    "Время": "Vaqt",
    "Все": "Hammasi",
    "Все задачи": "Barcha vazifalar",
    "Все сотрудники": "Barcha xodimlar",
    "Все файлы вместе — не больше {} МБ.": "Barcha fayllar birgalikda {} MB dan oshmasin.",
    "Вы написали:": "Siz yozdingiz:",
    "Выберите вес задачи": "Vazifa vaznini tanlang",
    "Выберите дату кнопкой или в календаре": "Sanani tugma bilan yoki taqvimdan tanlang",
    "Выберите задачу, по которой сдаёте результат": "Natija topshiriladigan vazifani tanlang",
    "Выберите значение": "Qiymatni tanlang",
    "Выберите оценку": "Bahoni tanlang",
    "Выберите оценку кнопкой или введите число": "Bahoni tugma bilan tanlang yoki son kiriting",
    "Выберите сотрудника": "Xodimni tanlang",
    "Выйти без сохранения?": "Saqlamasdan chiqilsinmi?",
    "Выполнение в срок": "Muddatida bajarish",
    "Выполнено {}, просрочено {}, в работе {}, на проверке {}": "Bajarildi {}, muddati oʻtgan {}, bajarilmoqda {}, tekshiruvda {}",
    "Выполнены": "Bajarilgan",
    "Где лежат материалы": "Materiallar qayerda",
    "Год": "Yil",
    "До {} файлов, каждый до {} МБ, всего до {} МБ. Файлы сохранятся в чате с ботом.": "{} tagacha fayl, har biri {} MB gacha, jami {} MB gacha. Fayllar bot bilan chatda saqlanadi.",
    "Доля задачи в оценке эффективности сотрудника, {}–{} %.": "Vazifaning xodim samaradorligi bahosidagi ulushi, {}–{} %.",
    "Доступ закрыт": "Kirish yopiq",
    "Единица измерения плана": "Reja oʻlchov birligi",
    "Единица измерения — до {} символов": "Oʻlchov birligi — {} belgigacha",
    "Загрузка…": "Yuklanmoqda…",
    "Задач в этом периоде нет.": "Bu davrda vazifalar yoʻq.",
    "Задач нет": "Vazifalar yoʻq",
    "Задач нет.": "Vazifalar yoʻq.",
    "Задача": "Vazifa",
    "Задача #{}": "Vazifa #{}",
    "Задача не в работе — сдать результат нельзя.": "Vazifa bajarilayotgan holatda emas — natijani topshirib boʻlmaydi.",
    "Задачи": "Vazifalar",
    "Закрыть": "Yopish",
    "Заполните это поле": "Bu maydonni toʻldiring",
    "Заявка на рассмотрении": "Ariza koʻrib chiqilmoqda",
    "Изменение задачи": "Vazifani oʻzgartirish",
    "Изменений нет": "Oʻzgarish yoʻq",
    "Итоги периода": "Davr yakunlari",
    "Какой получен результат?": "Qanday natija olindi?",
    "Квартал": "Chorak",
    "Команда": "Jamoa",
    "Комментарий для сотрудника (необязательно)": "Xodim uchun izoh (ixtiyoriy)",
    "Месяц": "Oy",
    "Месяц:": "Oy:",
    "Модель: {u}": "Model: {u}",
    "Можно приложить не более {} файлов — лишние ({}) не добавлены.": "{} tadan koʻp fayl biriktirib boʻlmaydi — ortiqchalari ({}) qoʻshilmadi.",
    "Можно приложить не более {} файлов.": "{} tadan koʻp fayl biriktirib boʻlmaydi.",
    "Мои задачи": "Vazifalarim",
    "Мой KPI": "Mening KPI",
    "На подтверждении": "Tasdiqlashda",
    "На проверке": "Tekshiruvda",
    "Надиктовать голосом": "Ovoz bilan aytib yozdirish",
    "Надиктовать:": "Aytib yozdirish:",
    "Нажмите, скажите задачу по-русски или по-узбекски и нажмите ещё раз — поля заполнятся сами.": "Tugmani bosing, vazifani oʻzbekcha yoki ruscha ayting va yana bosing — maydonlar oʻzi toʻladi.",
    "Название не может быть пустым": "Nom boʻsh boʻlishi mumkin emas",
    "Название, сотрудник или #номер": "Nom, xodim yoki #raqam",
    "Напишите, что нужно доработать": "Nimani qayta ishlash kerakligini yozing",
    "Напишите, что нужно сделать": "Nima qilish kerakligini yozing",
    "Например: {}": "Masalan: {}",
    "Например: {} договоров. Оставьте пустым, если числового плана нет.": "Masalan: {} ta shartnoma. Sonli reja boʻlmasa, boʻsh qoldiring.",
    "Например: Анализ договоров поставщиков": "Masalan: Yetkazib beruvchilar shartnomalari tahlili",
    "Например: Подготовлен отчёт и рекомендации по нарушениям": "Masalan: Hisobot va kamchiliklar boʻyicha tavsiyalar tayyorlandi",
    "Например: Проверено {} договоров, в {} выявлены нарушения": "Masalan: {} ta shartnoma tekshirildi, {} tasida kamchilik aniqlandi",
    "Например: добавьте расчёт по {} договорам с нарушениями": "Masalan: kamchiligi bor {} ta shartnoma boʻyicha hisobni qoʻshing",
    "Например: задача больше не актуальна": "Masalan: vazifa endi dolzarb emas",
    "Например: не хватает расчёта по двум договорам": "Masalan: ikkita shartnoma boʻyicha hisob yetishmaydi",
    "Например: ссылка на папку или «отправил по почте»": "Masalan: papkaga havola yoki «pochta orqali yubordim»",
    "Например: эту работу уже выполняет другой сотрудник": "Masalan: bu ishni boshqa xodim bajarmoqda",
    "Не понял число. Введите, например: {}": "Son tushunarsiz. Masalan, shunday kiriting: {}",
    "Не удалось загрузить": "Yuklab boʻlmadi",
    "Неделя": "Hafta",
    "Неделя:": "Hafta:",
    "Недостаточно данных для динамики": "Dinamika uchun maʼlumot yetarli emas",
    "Нет активных сотрудников. Подтвердите заявки в чате: «Сотрудники».": "Faol xodimlar yoʻq. Arizalarni chatda tasdiqlang: «Xodimlar».",
    "Нет задач для сдачи. Здесь появятся задачи в работе и на доработке.": "Topshiriladigan vazifalar yoʻq. Bu yerda bajarilayotgan va qayta ishlashdagi vazifalar chiqadi.",
    "Нет связи с сервером. Проверьте интернет и нажмите «Повторить».": "Server bilan aloqa yoʻq. Internetni tekshirib, «Qayta urinish» tugmasini bosing.",
    "Нечего проверять": "Tekshiradigan narsa yoʻq",
    "Новая": "Yangi",
    "Новая задача": "Yangi vazifa",
    "Новый срок": "Yangi muddat",
    "Новых поручений нет.": "Yangi topshiriqlar yoʻq.",
    "Нужна регистрация": "Roʻyxatdan oʻtish kerak",
    "Обновить": "Yangilash",
    "Ожидаемый результат": "Kutilayotgan natija",
    "Окончательное решение — за начальником.": "Yakuniy qaror — boshliqda.",
    "Опишите ожидаемый результат": "Kutilayotgan natijani yozing",
    "Опишите чуть подробнее, пожалуйста.": "Iltimos, biroz batafsilroq yozing.",
    "Опишите, что сделано": "Nima qilinganini yozing",
    "Остановить запись": "Yozishni toʻxtatish",
    "Отклонить": "Rad etish",
    "Откройте из Telegram": "Telegramdan oching",
    "Откройте приложение из Telegram: кнопка «Открыть» в чате с ботом.": "Ilovani Telegramdan oching: bot bilan chatdagi «Ochish» tugmasi.",
    "Открыть карточку задачи": "Vazifa kartochkasini ochish",
    "Отменить задачу": "Vazifani bekor qilish",
    "Отменить задачу #{}? Сотрудник получит уведомление.": "#{} vazifa bekor qilinsinmi? Xodim xabar oladi.",
    "Отправка": "Yuborish",
    "Отправка заняла слишком много времени. Проверьте интернет и попробуйте ещё раз.": "Yuborish juda uzoq davom etdi. Internetni tekshirib, qayta urinib koʻring.",
    "Отправка… {} %": "Yuborilmoqda… {} %",
    "Оценка рассчитывается": "Baho hisoblanmoqda",
    "Оценка — число от {} до {}": "Baho — {} dan {} gacha son",
    "Оценённых задач в периоде пока нет": "Davrda baholangan vazifalar hali yoʻq",
    "Оценённых задач пока нет.": "Baholangan vazifalar hali yoʻq.",
    "Очистить поиск": "Qidiruvni tozalash",
    "Перевыполнено": "Ortigʻi bilan bajarilgan",
    "Передаю файлы в Telegram…": "Fayllar Telegramga uzatilmoqda…",
    "Перейти в чат": "Chatga oʻtish",
    "Перейти на русский язык": "Перейти на русский язык",
    "Период": "Davr",
    "План (число)": "Reja (son)",
    "План: {u}": "Reja: {u}",
    "Плановое число должно быть больше нуля": "Reja soni noldan katta boʻlishi kerak",
    "По запросу «{u}» ничего не найдено.": "«{u}» soʻrovi boʻyicha hech narsa topilmadi.",
    "Подтвердить": "Tasdiqlash",
    "Подтверждающие материалы": "Tasdiqlovchi materiallar",
    "Поиск задач": "Vazifalarni qidirish",
    "Показаны первые {} задач — уточните запрос.": "Dastlabki {} ta vazifa koʻrsatildi — soʻrovni aniqlashtiring.",
    "Показатели периода": "Davr koʻrsatkichlari",
    "Показать ещё": "Yana koʻrsatish",
    "Попытка {} · {}.{} {}:{}": "{}-urinish · {}.{} {}:{}",
    "Попытка сдачи №{}": "{}-topshirish urinishi",
    "Поручение": "Topshiriq",
    "Поручение сотрудника": "Xodim topshirigʻi",
    "Поручение уже обработано.": "Topshiriq allaqachon koʻrib chiqilgan.",
    "Поручения": "Topshiriqlar",
    "Поручения ({})": "Topshiriqlar ({})",
    "Последние {} недели (до сегодня)": "Oxirgi {} hafta (bugungacha)",
    "Последние {} недель (до сегодня)": "Oxirgi {} hafta (bugungacha)",
    "Последние {} неделя (до сегодня)": "Oxirgi {} hafta (bugungacha)",
    "Поставить {} %": "{} % qoʻyish",
    "Поставить задачу": "Vazifa qoʻyish",
    "Предварительная оценка": "Dastlabki baho",
    "Предварительная оценка {} %": "Dastlabki baho {} %",
    "Предыдущие попытки · {}": "Oldingi urinishlar · {}",
    "Предыдущий период": "Oldingi davr",
    "Принять задачу #{} в работу": "#{} vazifani ishga qabul qilish",
    "Приоритет": "Ustuvorlik",
    "Причина (необязательно)": "Sabab (ixtiyoriy)",
    "Проверка": "Tekshiruv",
    "Проверка результата": "Natijani tekshirish",
    "Просрочены": "Muddati oʻtgan",
    "Пустой файл «{u}»": "Boʻsh fayl: «{u}»",
    "Разделы": "Boʻlimlar",
    "Распознаю речь": "Nutq aniqlanmoqda",
    "Результат ещё отправляется. Уйти с этого экрана?": "Natija hali yuborilmoqda. Bu ekrandan chiqilsinmi?",
    "Результат отправлен": "Natija yuborildi",
    "Результат уже обработан или ещё не сдан.": "Natija allaqachon koʻrib chiqilgan yoki hali topshirilmagan.",
    "Результаты": "Natijalar",
    "Результаты ({})": "Natijalar ({})",
    "Своими словами: что должно получиться. Например: проверить {} договоров и подготовить отчёт о нарушениях": "Oʻz soʻzlaringiz bilan: yakunda nima boʻlishi kerak. Masalan: {} ta shartnomani tekshirib, kamchiliklar haqida hisobot tayyorlash",
    "Своими словами: что нужно получить. Например: проверить {} договоров и представить отчёт с нарушениями": "Oʻz soʻzlaringiz bilan: nimaga erishish kerak. Masalan: {} ta shartnomani tekshirib, kamchiliklar koʻrsatilgan hisobot taqdim etish",
    "Своя оценка, %": "Oʻz bahongiz, %",
    "Своё значение, %": "Oʻz qiymatingiz, %",
    "Сдать": "Topshirish",
    "Сдать результат": "Natijani topshirish",
    "Сдача результата": "Natijani topshirish",
    "Сдачи результата": "Natija topshirishlar",
    "Сейчас на неделе: {} %. Рекомендуется, чтобы сумма весов за неделю была ≈{} %.": "Hozir haftada: {} %. Haftalik vaznlar yigʻindisi ≈{} % boʻlishi tavsiya etiladi.",
    "Сервер долго не отвечает. Проверьте интернет и нажмите «Повторить».": "Server uzoq javob bermayapti. Internetni tekshirib, «Qayta urinish» tugmasini bosing.",
    "Сессия устарела": "Seans eskirgan",
    "Следующий период": "Keyingi davr",
    "Слишком большое число — введите реальное плановое значение": "Son juda katta — haqiqiy reja qiymatini kiriting",
    "Слишком длинно: {} из {} символов. Сократите, пожалуйста.": "Juda uzun: {} / {} belgi. Iltimos, qisqartiring.",
    "Слишком коротко": "Juda qisqa",
    "Сотрудник": "Xodim",
    "Сотрудник #{}": "Xodim #{}",
    "Сотрудник получит её в чате с ботом": "Xodim uni bot bilan chatda oladi",
    "Сотрудник получит уведомление и увидит причину, если вы её укажете.": "Xodim xabar oladi va sababni koʻradi (agar koʻrsatsangiz).",
    "Сотрудник получит уведомление об отмене.": "Xodim bekor qilinganligi haqida xabar oladi.",
    "Сотрудники": "Xodimlar",
    "Сохранить": "Saqlash",
    "Средняя оценка": "Oʻrtacha baho",
    "Срок": "Muddat",
    "Срок доработки": "Qayta ishlash muddati",
    "Убрать файл «{u}»": "«{u}» faylini olib tashlash",
    "Укажите плановое число или уберите единицу": "Reja sonini kiriting yoki birlikni olib tashlang",
    "Укажите срок": "Muddatni koʻrsating",
    "Файл «{u}» больше {} МБ.": "«{u}» fayli {} MB dan katta.",
    "Фактическое значение": "Amaldagi qiymat",
    "Фильтр по статусу": "Holat boʻyicha filtr",
    "Число не может быть отрицательным": "Son manfiy boʻlishi mumkin emas",
    "Что нужно доработать?": "Nimani qayta ishlash kerak?",
    "Что проверять": "Nimani tekshirish",
    "Что фактически сделано?": "Amalda nima qilindi?",
    "Экран обновится сам — обычно это занимает до пары минут.": "Ekran oʻzi yangilanadi — odatda bu bir-ikki daqiqa davom etadi.",
    "Экран обновится сам. Можно не ждать — изменить оценку или вернуть на доработку.": "Ekran oʻzi yangilanadi. Kutmasdan bahoni oʻzgartirish yoki qayta ishlashga qaytarish mumkin.",
    "Эту задачу сейчас изменить нельзя: {u}.": "Bu vazifani hozir oʻzgartirib boʻlmaydi: {u}.",
    "Эффективность за период": "Davr samaradorligi",
    "Эффективность команды": "Jamoa samaradorligi",
    "в работе": "bajarilmoqda",
    "вес {} %": "vazn {} %",
    "вес {} % × {} %": "vazn {} % × {} %",
    "вес {} % × {} % · просрочена": "vazn {} % × {} % · muddati oʻtgan",
    "вес {} % × —": "vazn {} % × —",
    "вес {} % × — · просрочена": "vazn {} % × — · muddati oʻtgan",
    "выполнено": "bajarildi",
    "ждёт подтверждения начальника": "boshliq tasdigʻini kutmoqda",
    "задач нет": "vazifalar yoʻq",
    "на проверке": "tekshiruvda",
    "напр. {}": "mas. {}",
    "напр. договоров": "mas. shartnoma",
    "необязательно": "ixtiyoriy",
    "нет данных": "maʼlumot yoʻq",
    "по правилам, без AI": "qoidalar boʻyicha, AIsiz",
    "просрочено": "muddati oʻtgan",
    "сдано {}.{} {}:{}": "{}.{} {}:{} da topshirilgan",
    "срок {u}": "muddat: {u}",
    "срок уже прошёл — укажите новый": "muddat oʻtib ketgan — yangisini koʻrsating",
    "— подтверждена автоматически": "— avtomatik tasdiqlangan",
    "Назад": "Orqaga",
    "Возвращено на доработку": "Qayta ishlashga qaytarildi",
    "На доработку": "Qayta ishlashga",
    "Возвратов на доработку:": "Qayta ishlashga qaytarishlar:",
    "Задача возвращена на доработку": "Vazifa qayta ishlashga qaytarildi",
    "доработок: {}": "qayta ishlash: {}",
    "на доработке": "qayta ishlashda",
    "возвратов: {}": "qaytarishlar: {}",
    "Вы ещё не подтвердили получение": "Siz hali qabul qilganingizni tasdiqlamadingiz",
    "Готовлю отчёт…": "Hisobot tayyorlanmoqda…",
    "Исполнитель ещё не подтвердил получение": "Ijrochi hali qabul qilganini tasdiqlamadi",
    "Предварительная оценка ещё рассчитывается": "Dastlabki baho hali hisoblanmoqda",
    "Принимаю…": "Qabul qilinmoqda…",
    "Распознаю…": "Aniqlanmoqda…",
    "Срок:": "Muddat:",
    "Формулирую измеримый результат…": "Oʻlchanadigan natija taʼriflanmoqda…",
    "Вес и приоритет назначаются при подтверждении поручения.": "Vazn va ustuvorlik topshiriqni tasdiqlashda belgilanadi.",
    "Вес:": "Vazn:",
    "AI не ответил вовремя. Попробуйте ещё раз или сформулируйте результат сами.": "AI oʻz vaqtida javob bermadi. Qayta urinib koʻring yoki natijani oʻzingiz taʼriflang.",
    "Произошла ошибка, попробуйте ещё раз": "Xatolik yuz berdi, qayta urinib koʻring",
    "С этой задачей будет {} %.": "Bu vazifa bilan {} % boʻladi.",
    "Срок поручения уже прошёл — сначала измените срок.": "Topshiriq muddati oʻtib ketgan — avval muddatni oʻzgartiring.",
    "Срок уже прошёл — результат будет отмечен как сданный с опозданием.": "Muddat oʻtib ketgan — natija kechikib topshirilgan deb belgilanadi.",
    "с опозданием": "kechikib",
    "с опозданием {} kun": "{} kun kechikib",
    "Приоритет:": "Ustuvorlik:",
    "заблокирован": "bloklangan",
    "Задача #{} поставлена": "#{} vazifa qoʻyildi",
    "Задача #{} принята в работу": "#{} vazifa ishga qabul qilindi",
    "Задача уже выполнена и оценена — сдавать результат не нужно.": "Vazifa allaqachon bajarilgan va baholangan — natija topshirish shart emas.",
    "Оценка: {} %": "Baho: {} %",
    "Подтвердить {} %": "{} % ni tasdiqlash",
    "Подтвердить поручение": "Topshiriqni tasdiqlash",
    "Подтверждено: {} %": "Tasdiqlandi: {} %",
    "Поручение #{} подтверждено — задача в работе": "#{} topshiriq tasdiqlandi — vazifa ishga olindi",
    "Принял в работу": "Ishga qabul qildim",
    "Принять": "Qabul qilish",
    "Результат отправлен начальнику на проверку. Решение придёт в чат с ботом.": "Natija tekshirish uchun boshliqqa yuborildi. Qaror bot bilan chatga keladi.",
    "Результат по задаче #{} отправлен начальнику на проверку.": "#{} vazifa boʻyicha natija tekshirish uchun boshliqqa yuborildi.",
    "Сохранено": "Saqlandi",
    "Факт": "Fakt",
    "Формулировка подставлена": "Taʼrif qoʻyildi",
    "Что сделано": "Nima qilindi",
    "Изменить": "Oʻzgartirish",
    "Изменить оценку": "Bahoni oʻzgartirish",
    "Изменить оценку (до {}.{} в {}:{})": "Bahoni oʻzgartirish ({}.{} {}:{} gacha)",
    "Изменить срок": "Muddatni oʻzgartirish",
    "Принята": "Qabul qilindi",
    "Принята в работу: {}.{} {}:{}": "Ishga qabul qilingan: {}.{} {}:{}",
    "Убрать план": "Rejani olib tashlash",
    "Сделать измеримым": "Oʻlchanadigan qilish",
    "Отклонить поручение #{}": "#{} topshiriqni rad etish",
    "Отклонить поручение": "Topshiriqni rad etish",
    "Осталось заполнить:": "Toʻldirish kerak:",
    "Всё заполнено": "Hammasi toʻldirildi",
    "Ещё действия": "Boshqa amallar",
    "Поручение #{} отклонено": "#{} topshiriq rad etildi",
    "Поручение отклонено начальником — сдавать результат не нужно.": "Topshiriq boshliq tomonidan rad etilgan — natija topshirish shart emas.",
    "Готово — проверьте поля перед отправкой.": "Tayyor — yuborishdan oldin maydonlarni tekshiring.",
    "Готово — проверьте поля. Вес и приоритет выберите сами.": "Tayyor — maydonlarni tekshiring. Vazn va ustuvorlikni oʻzingiz tanlang.",
    "Запись слишком короткая. Нажмите, скажите фразу и нажмите ещё раз.": "Yozuv juda qisqa. Tugmani bosing, gapni ayting va yana bosing.",
    "Микрофон не найден.": "Mikrofon topilmadi.",
    "Надиктовать задачу целиком": "Vazifani toʻliq aytib yozdirish",
    "Надиктовать поручение целиком": "Topshiriqni toʻliq aytib yozdirish",
    "Не удалось включить микрофон. Попробуйте ещё раз или введите текст с клавиатуры.": "Mikrofonni yoqib boʻlmadi. Qayta urinib koʻring yoki matnni klaviaturadan kiriting.",
    "Нет доступа к микрофону. Разрешите микрофон для Telegram в настройках телефона и попробуйте ещё раз.": "Mikrofonga ruxsat yoʻq. Telefon sozlamalarida Telegram uchun mikrofonga ruxsat bering va qayta urinib koʻring.",
    "План": "Reja",
    "подтверждена": "tasdiqlangan",
    "изменена": "oʻzgartirilgan",
    "Итоговая оценка:": "Yakuniy baho:",
    "Сейчас:": "Hozir:",
    "{u} · попытка {} · вес {} %": "{u} · {}-urinish · vazn {} %",
    "{u} · внесено сотрудником (устное поручение)": "{u} · xodim kiritgan (ogʻzaki topshiriq)",
    "Исполнитель:": "Ijrochi:",
    "Предлагаемый вес: {} %": "Taklif qilingan vazn: {} %",
    "Комментарий начальника": "Boshliq izohi",
    "Комментарий начальника: {u}": "Boshliq izohi: {u}",
    "Описание": "Tavsif",
    "Результат": "Natija",
    "План:": "Reja:",
    "Задачи сотрудника": "Xodim vazifalari",
    "К моим задачам": "Vazifalarimga",
    "Задача #{}: {u}": "Vazifa #{}: {u}",
    "Оставить текущий ({u})": "Joriy muddatni qoldirish ({u})",
    "Выбрать файлы": "Fayllarni tanlash",
    "Добавить ещё": "Yana qoʻshish",
    "Добавить план (число)": "Reja (son) qoʻshish",
    "Прислать файлы в чат": "Fayllarni chatga yuborish",
    "Файлы ({})": "Fayllar ({})",
    "Файлы · {}": "Fayllar · {}",
    "Файлы ({}) — сохранены в чате с ботом": "Fayllar ({}) — bot bilan chatda saqlangan",
    "Файлы придут в чат с ботом": "Fayllar bot bilan chatga keladi",
    "Файлы сохранены в чате с ботом": "Fayllar bot bilan chatda saqlangan",
    "Подсказка без AI": "AIsiz maslahat",
    "История · {}": "Tarix · {}",
    "История оценок": "Baholar tarixi",
    "Оставить как написал": "Yozganimcha qoldirish",
    "Результат на проверке у начальника. Решение придёт в чат с ботом.": "Natija boshliq tekshiruvida. Qaror bot bilan chatga keladi.",
    "Результат уже отправлен и ждёт проверки начальника.": "Natija allaqachon yuborilgan va boshliq tekshiruvini kutmoqda.",
    "Excel-отчёт в чат": "Excel hisobotini chatga",
    "Отправить": "Yuborish",
    "Отправить начальнику": "Boshliqqa yuborish",
    "Отчёт придёт в чат с ботом через несколько секунд.": "Hisobot bir necha soniyada bot bilan chatga keladi.",
    "Поручение #{} отправлено начальнику на подтверждение": "#{} topshiriq tasdiqlash uchun boshliqqa yuborildi",
    "Сдано:": "Topshirildi:",
    "Заявок на доступ: {}": "Kirish uchun arizalar: {}",
    "Перейти в чат и нажать «Сотрудники»": "Chatga oʻtib, «Xodimlar» tugmasini bosing",
    "Поручение ещё не подтверждено начальником — сдать результат можно после подтверждения.": "Topshiriq hali boshliq tomonidan tasdiqlanmagan — natijani tasdiqlangandan keyin topshirish mumkin.",
    "Другой вариант": "Boshqa variant",
    "Повторить": "Qayta urinish",
    "Приложение обновилось": "Ilova yangilandi",
    "Проверить результат": "Natijani tekshirish",
    "Высокий": "Yuqori",
    "Задача #{} отменена": "#{} vazifa bekor qilindi",
    "Задача отменена начальником — сдавать результат не нужно.": "Vazifa boshliq tomonidan bekor qilingan — natija topshirish shart emas.",
    "Отменить задачу #{}": "#{} vazifani bekor qilish",
    "Средний": "Oʻrta",
    "Низкий": "Past",
    "Измеримая формулировка": "Oʻlchanadigan taʼrif",
    "Предварительной оценки AI нет.": "AIning dastlabki bahosi yoʻq.",
    "Предварительной оценки нет — выставьте оценку сами.": "Dastlabki baho yoʻq — bahoni oʻzingiz qoʻying.",
    "Ответственный начальник:": "Masʼul boshliq:",
    "Постановщик:": "Vazifa qoʻyuvchi:",
    "Проверка: {u}": "Tekshirdi: {u}",
    "Проверка: {u}, {}.{} {}:{}": "Tekshirdi: {u}, {}.{} {}:{}",
    "Вошли в расчёт": "Hisobga kirganlar",
    "Вход": "Kirish",
    "Войти через Telegram": "Telegram orqali kirish",
    "Задачи, сдача результатов и KPI — в браузере, как в Telegram.": "Vazifalar, natijalarni topshirish va KPI — brauzerda, xuddi Telegramdagidek.",
    "Пароль не нужен: вход подтверждается в боте.": "Parol kerak emas: kirish botda tasdiqlanadi.",
    "Вы вышли. Чтобы продолжить работу, войдите снова.": "Siz chiqdingiz. Ishni davom ettirish uchun qayta kiring.",
    "Вход завершён. Войдите снова.": "Seans tugadi. Qayta kiring.",
    "Подтвердите вход в Telegram": "Kirishni Telegramda tasdiqlang",
    "Шаг {}. Запомните число": "{}-qadam. Sonni eslab qoling",
    "Открыть Telegram": "Telegramni ochish",
    "Telegram только на телефоне? Наведите камеру телефона на этот код:": "Telegram faqat telefondami? Telefon kamerasini shu kodga qarating:",
    "Шаг {}. Откройте бота и нажмите это число": "{}-qadam. Botni oching va shu sonni bosing",
    "Бот покажет три числа — нажмите это.": "Bot uchta son koʻrsatadi — shu sonni bosing.",
    "Ждём подтверждения в Telegram.": "Telegramda tasdiqlash kutilmoqda.",
    "Бот открыт — нажмите в нём число с этого экрана.": "Bot ochildi — unda shu ekrandagi sonni bosing.",
    "Отмена": "Bekor qilish",
    "Вход не подтверждён": "Kirish tasdiqlanmadi",
    "Время вышло": "Vaqt tugadi",
    "Бот не подтвердил вход: нажато другое число или «Это не я», либо доступа пока нет. Подробности — в чате с ботом.": "Bot kirishni tasdiqlamadi: boshqa son yoki «Bu men emas» bosilgan, yoki hozircha ruxsat yoʻq. Tafsilotlar — bot bilan chatda.",
    "Подтвердить вход нужно за {} минут. Начните заново.": "Kirishni {} daqiqa ichida tasdiqlash kerak. Qaytadan boshlang.",
    "Начать заново": "Qaytadan boshlash",
    "Слишком много попыток входа подряд. Повторите через минуту.": "Ketma-ket juda koʻp kirish urinishi. Bir daqiqadan soʻng takrorlang.",
    "Не удалось связаться с Telegram. Попробуйте ещё раз через минуту.": "Telegram bilan bogʻlanib boʻlmadi. Bir daqiqadan soʻng qayta urinib koʻring.",
    "QR-код для входа через Telegram": "Telegram orqali kirish uchun QR-kod",
    "Выйти": "Chiqish",
    "Открыть чат с ботом": "Bot bilan chatni ochish",
    "Нет доступа к микрофону. Разрешите микрофон для этого сайта в браузере и попробуйте ещё раз.": "Mikrofonga ruxsat yoʻq. Brauzerda shu sayt uchun mikrofonga ruxsat bering va qayta urinib koʻring.",
    "Шаг {}. Нажмите это число в чате с ботом": "{}-qadam. Bot bilan chatda shu sonni bosing",
    "Сообщение от бота уже должно быть в Telegram. Если его нет — начните заново.": "Botdan xabar Telegramda boʻlishi kerak. Agar u boʻlmasa — qaytadan boshlang.",
    "Вес": "Vazn",
    "Возвратов": "Qaytarishlar",
    "Выполнено": "Bajarildi",
    "Должность": "Lavozim",
    "Исполнитель": "Ijrochi",
    "Начальник": "Boshliq",
    "Оценка": "Baho",
    "Подробности": "Tafsilotlar",
    "Попытка": "Urinish",
    "Просрочено": "Muddati oʻtgan",
    "Сдано": "Topshirildi",
    "Статус": "Holat",
    /*UZ-END*/
  };

  // ---------------------------------------------------------------------------------------------
  // 18. Запуск
  // ---------------------------------------------------------------------------------------------

  /** initData: из Telegram; в режиме отладки — из ?tg_debug_init= или sessionStorage (§5.4). */
  function resolveInitData() {
    if (tg && tg.initData) return tg.initData;
    if (!CONFIG.debug) return '';
    let value = '';
    safely(() => {
      const url = new URL(window.location.href);
      const fromUrl = url.searchParams.get('tg_debug_init');
      if (fromUrl) {
        value = fromUrl;
        safely(() => window.sessionStorage.setItem('kpi_debug_init', fromUrl));
        url.searchParams.delete('tg_debug_init');
        safely(() => history.replaceState(null, '', url.pathname + url.search + url.hash));
      } else {
        value = safely(() => window.sessionStorage.getItem('kpi_debug_init')) || '';
      }
    });
    return value;
  }

  function initialHash() {
    const target = resolve(window.location.hash);
    if (target) return target.hash;
    const startParam = tg && tg.initDataUnsafe ? tg.initDataUnsafe.start_param : '';
    const match = /^task_(\d+)$/.exec(startParam || '');
    return match ? '#/task/' + match[1] : startHash();
  }

  async function start() {
    let me;
    try {
      me = await api(EP.me, { fresh: true });
    } catch (err) {
      if (!err.handled) showBootError(err);
      return;
    }
    if (WEB && loginLang && me && me.access === 'active' && me.lang !== loginLang) {
      // Язык выбран на экране входа — он и остаётся языком человека (как кнопка языка в приложении).
      const picked = loginLang;
      loginLang = null;
      try {
        await api(EP.setLang, { body: { lang: picked } });
        dropCache();
        me = await api(EP.me, { fresh: true });
      } catch (_) {
        // не получилось — остаётся прежний язык человека
      }
    }
    if (me && me.lang) setLang(me.lang);
    if (!me || me.access !== 'active') {
      showBlocking((me && me.access) || 'session', (me && me.message) || T.generic);
      return;
    }
    applyMe(me);
    buildShell();
    go(initialHash(), { force: true, reset: true });
  }

  let lastResume = 0;

  function onResume() {
    if (!shell || Date.now() - lastResume < 1000) return;
    lastResume = Date.now();
    if (Date.now() - state.meAt > STALE_MS) scheduleMeRefresh();
    if (current && current.refresh && Date.now() - current.loadedAt > STALE_MS) current.refresh();
  }

  function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && ['button', 'checkbox', 'radio', 'file', 'submit', 'range', 'color'].indexOf(el.type) < 0;
  }

  function watchKeyboard() {
    if (!coarsePointer) return;
    // На телефоне клавиатура сжимает экран: прячем нижнюю панель и держим поле в центре видимой части.
    document.addEventListener('focusin', (event) => {
      if (!isTypingTarget(event.target)) return;
      document.body.classList.add('kb-open');
      setTimeout(() => {
        if (document.activeElement === event.target) safely(() => event.target.scrollIntoView({ block: 'center', behavior: scrollBehavior() }));
      }, 320);
    });
    document.addEventListener('focusout', () => {
      setTimeout(() => {
        if (!isTypingTarget(document.activeElement)) document.body.classList.remove('kb-open');
      }, 80);
    });
  }

  function boot() {
    appRoot = document.getElementById('app');
    if (!appRoot) return;
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    primary.mount();
    back.mount();
    applyTheme();
    if (tg) {
      safely(() => tg.expand());
      if (tgv('7.7')) safely(() => tg.disableVerticalSwipes());
      safely(() => tg.onEvent('themeChanged', applyTheme));
      if (tgv('8.0')) safely(() => tg.onEvent('activated', onResume));
    }
    if (!insideTelegram && darkQuery) {
      const onScheme = () => applyTheme();
      if (darkQuery.addEventListener) darkQuery.addEventListener('change', onScheme);
      else if (darkQuery.addListener) darkQuery.addListener(onScheme);
    }
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') onResume();
      else {
        // Приложение свернули: запись останавливаем, микрофон отпускаем.
        if (voice.active) voice.active.stop();
        voiceRelease(true);
      }
    });
    watchKeyboard();
    // Сайт: окно расширили или сузили через границу раскладки — экран рисуется заново в новом виде.
    // Незаконченная форма при этом не трогается: её раскладку подправят стили, а ввод не пропадёт.
    [wideQuery, paneQuery].forEach((query) => {
      if (query && query.addEventListener) query.addEventListener('change', () => {
        if (!shell || !nav.current || isAnythingDirty()) return;
        buildShell();
        render(nav.current);
      });
    });
    if (WEB) {
      document.addEventListener('keydown', (event) => {  // Esc закрывает панель подробностей
        if (event.key === 'Escape' && under && !sheet.isOpen() && !isTypingTarget(event.target)) goBack();
      });
    }

    // До ответа сервера язык — по Telegram; дальше — выбранный человеком (Me.lang).
    const tgUser = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;
    setLang(WEB ? storedLang() : tgUser && String(tgUser.language_code || '').toLowerCase().slice(0, 2) === 'uz' ? 'uz' : 'ru');
    auth.initData = resolveInitData();
    if (!auth.initData && !WEB) {
      showBlocking('outside', T.openFromTelegram);
      if (tg) safely(() => tg.ready());
      return;
    }
    if (WEB && pageToken() === '1') {
      resumeLogin();  // знака страницы нет — нет и сессии: экран входа (или продолжение начатого входа)
      return;
    }
    appRoot.replaceChildren(bootSkeleton());
    if (tg) safely(() => tg.ready());
    start();
  }

  boot();
}());
