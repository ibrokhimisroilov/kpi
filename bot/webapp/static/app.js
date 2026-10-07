/*
 * Mini App «Эффективность» — одностраничное приложение без сборки (docs/MINIAPP_SPEC.md §11).
 *
 * Устройство:
 *  • h()/s() строят DOM; данные попадают в страницу только текстовыми узлами (никакой HTML-разметки
 *    из строк — так требует CSP и §11.1);
 *  • api() ходит в /api/* с заголовком X-Telegram-Init-Data, кэширует GET в памяти на 30 с и
 *    превращает ответы-ошибки в ApiError (401/403 доступа/429 обрабатываются глобально);
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
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const CACHE_TTL_MS = 30 * 1000;      // GET-ответы живут в памяти 30 с (§11.5)
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
    review: ['GET', '/api/review'],
    confirm: ['POST', '/api/submissions/{sub_id}/confirm'],
    setScore: ['POST', '/api/submissions/{sub_id}/score'],
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
    generic: '⚠️ Произошла ошибка, попробуйте ещё раз',
    openFromTelegram: 'Откройте приложение из Telegram: кнопка «Открыть» в чате с ботом.',
    leave: 'Выйти без сохранения?',
    leaveUpload: 'Результат ещё отправляется. Уйти с этого экрана?',
    retry: '🔄 Повторить',
    uploadTimeout: 'Отправка заняла слишком много времени. Проверьте интернет и попробуйте ещё раз.',
    aiTimeout: '⚠️ AI не ответил вовремя. Попробуйте ещё раз или сформулируйте результат сами.',
    noEmployees: 'Нет активных сотрудников. Подтвердите заявки в чате: «👥 Сотрудники».',
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
    { value: 'high', label: '🔴 Высокий' },
    { value: 'medium', label: '🟡 Средний' },
    { value: 'low', label: '🟢 Низкий' },
  ];
  const NOT_OPEN_TEXTS = {
    submitted: '📝 Результат уже отправлен и ждёт проверки руководителя.',
    done: '✅ Задача уже выполнена и оценена — сдавать результат не нужно.',
    cancelled: '🚫 Задача отменена руководителем — сдавать результат не нужно.',
    proposed: '📥 Поручение ещё не подтверждено руководителем — сдать результат можно после подтверждения.',
    rejected: '❌ Поручение отклонено руководителем — сдавать результат не нужно.',
  };
  const KIND_ICONS = { document: '📄', photo: '🖼️', video: '🎬', other: '📎' };
  const MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
    'сентября', 'октября', 'ноября', 'декабря'];
  const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

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
      else if (PROP_KEYS.has(key)) el[key] = v;
      else el.setAttribute(key, v === true ? '' : String(v));
    }
  }

  function appendKids(el, kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false || kid === '') continue;
      if (Array.isArray(kid)) appendKids(el, kid);
      else if (kid instanceof Node) el.appendChild(kid);
      else el.appendChild(document.createTextNode(keepTogether(String(kid))));
    }
  }

  /** Неразрывные пробелы в подписях: «100 %», «на 1 дн.», «до 09.10» не рвутся на две строки. */
  function keepTogether(text) {
    if (text.indexOf(' ') < 0) return text;
    return text
      .replace(/(\d) (?=%)/g, '$1 ')
      .replace(/(\d) (?=(?:дн|ч|мин)\.)/g, '$1 ')
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
    if (bytes < 1024) return bytes + ' Б';
    if (bytes < MB) return Math.max(1, Math.round(bytes / 1024)) + ' КБ';
    return fmtNum(Math.round(bytes / MB * 10) / 10) + ' МБ';
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
    return Number(m[3]) + ' ' + MONTHS_GEN[Number(m[2]) - 1] + year + ' (' + WEEKDAYS[day.getUTCDay()] + ')';
  }

  function serverNow() {
    return Date.now() + state.skew;
  }

  function withNotice(text, notice) {
    return notice ? text + '\n' + notice : text;
  }

  function stripLeadingEmoji(text) {
    return String(text || '').replace(/^(?:\p{Extended_Pictographic}|️|‍)+\s*/u, '');
  }

  // ---------------------------------------------------------------------------------------------
  // 3. Состояние приложения
  // ---------------------------------------------------------------------------------------------

  const state = { me: null, role: null, cfg: Object.assign({}, DEFAULT_CFG), skew: 0, meAt: 0 };
  const auth = { initData: '' };
  let appRoot = null;
  let shell = null;      // каркас с вкладками (только у активного пользователя)
  let current = null;    // активный экран
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
    state.me = me;
    state.role = me.role;
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

  function confirmDialog(message) {
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
        const params = { text: c.progress && c.progressText ? c.progressText : c.text, is_visible: true, is_active: enabled };
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
      if (!c) {
        box.hidden = true;
        setCssPx('--primary-h', 0);
        return;
      }
      box.hidden = false;
      box.classList.toggle('over-sheet', Boolean(layers.sheet));
      box.classList.toggle('is-danger', Boolean(c.danger));
      button.textContent = c.progress && c.progressText ? c.progressText : c.text;
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

  /** BackButton Telegram (6.1+); вне Telegram — ссылка «‹ Назад» в шапке экрана. */
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

  function toast(text) {
    const box = document.getElementById('toast');
    if (!box || !text) return;
    const msg = h('div', { class: 'toast-msg' }, text);
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
      h('span', null, '🔄 Приложение обновилось'),
      h('button', { type: 'button', class: 'btn', onclick: () => window.location.reload() }, 'Обновить'));
    const slot = shell ? shell.bannerSlot : appRoot;
    if (slot) slot.prepend(bar);
  }

  // ---------------------------------------------------------------------------------------------
  // 6. Клиент API
  // ---------------------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.handled = false;
    }
  }

  const cache = new Map();     // адрес -> {at, data}
  const inflight = new Map();  // адрес -> Promise (одинаковые GET не дублируются)
  let cacheGen = 0;

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

  /** api(EP.task, {params: {task_id: 5}, query, body, fresh, timeout}) -> данные ответа или ApiError. */
  function api(ep, opts) {
    const o = opts || {};
    const method = ep[0];
    const url = urlOf(ep, o);
    if (method !== 'GET') return send(method, url, o.body, o.timeout || POST_TIMEOUT_MS);
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
      })
      .finally(() => inflight.delete(url));
    inflight.set(url, promise);
    return promise;
  }

  async function send(method, url, body, timeoutMs) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (ctrl) ctrl.abort();
    }, timeoutMs);
    const headers = { 'X-Telegram-Init-Data': auth.initData, Accept: 'application/json' };
    const init = { method, headers, credentials: 'same-origin', cache: 'no-store' };
    if (ctrl) init.signal = ctrl.signal;
    if (body !== undefined) {
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
      throw routeError(new ApiError(resp.status, (data && data.code) || 'internal', (data && data.error) || T.generic));
    }
    return data;
  }

  /** Сдача результата: XMLHttpRequest ради прогресса загрузки (§11.5). */
  function upload(ep, params, form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(ep[0], fillPath(ep[1], params));
      xhr.setRequestHeader('X-Telegram-Init-Data', auth.initData);
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
        else reject(routeError(new ApiError(xhr.status, (data && data.code) || 'internal', (data && data.error) || T.generic)));
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
      showBlocking('session', err.message);
    } else if (err.status === 403 && ACCESS_CODES.indexOf(err.code) >= 0) {
      err.handled = true;
      showBlocking(err.code, err.message);
    } else if (err.status === 429) {
      err.handled = true;
      haptic.warn();
      toast(err.message);
    }
    return err;
  }

  function invalidate(prefixes) {
    cacheGen += 1;
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
  }

  function reportError(err) {
    if (err.handled) return;
    haptic.err();
    toast(err.message || T.generic);
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
          h('button', { type: 'button', class: 'sheet-close', 'aria-label': 'Закрыть', onclick: () => requestClose() }, '✕')),
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
      { id: 'team', hash: '#/team', icon: '📊', label: 'Команда' },
      { id: 'tasks', hash: '#/tasks', icon: '📋', label: 'Задачи' },
      { id: 'review', hash: '#/review', icon: '📝', label: 'Проверка', badge: (c) => (c.review || 0) + (c.proposals || 0) },
      { id: 'new', hash: '#/new', icon: '➕', label: 'Новая' },
    ],
    employee: [
      { id: 'my', hash: '#/my', icon: '📋', label: 'Мои задачи', badge: (c) => (c.unaccepted || 0) + (c.rework || 0) },
      { id: 'submit', hash: '#/submit', icon: '📤', label: 'Сдать', badge: (c) => c.overdue || 0 },
      { id: 'kpi', hash: '#/kpi', icon: '📈', label: 'Мой KPI' },
      { id: 'propose', hash: '#/propose', icon: '➕', label: 'Поручение' },
    ],
  };

  const ROUTES = [
    { pat: /^\/team$/, role: 'manager', tab: 'team', root: true, view: TeamView },
    { pat: /^\/team\/user\/(\d+)$/, role: 'manager', tab: 'team', view: UserCardView },
    { pat: /^\/tasks$/, role: 'manager', tab: 'tasks', root: true, view: TasksView },
    { pat: /^\/task\/(\d+)$/, role: '*', view: TaskView },
    { pat: /^\/task\/(\d+)\/edit$/, role: 'manager', view: EditTaskView },
    { pat: /^\/review$/, role: 'manager', tab: 'review', root: true, view: ReviewQueueView },
    { pat: /^\/review\/(\d+)$/, role: 'manager', tab: 'review', view: ReviewView },
    { pat: /^\/proposal\/(\d+)$/, role: 'manager', tab: 'review', view: ProposalView },
    { pat: /^\/new$/, role: 'manager', tab: 'new', root: true, view: NewTaskView },
    { pat: /^\/my$/, role: 'employee', tab: 'my', root: true, view: MyTasksView },
    { pat: /^\/submit$/, role: 'employee', tab: 'submit', root: true, view: SubmitListView },
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
    else nav.stack.push(target.hash);
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
    if (nav.current) scrollMemo.set(nav.current.hash, window.scrollY);
    if (current) current.destroy();
    sheet.close(true);
    primary.hide('screen');
    target.tab = target.route.tab || (nav.current && nav.current.tab) || defaultTab();
    nav.current = target;
    if (window.location.hash !== target.hash) setHash(target.hash);
    const scr = createScreen(target);
    current = scr;
    shell.main.replaceChildren(scr.el);
    renderTabbar();
    updateBack();
    try {
      target.route.view(scr, target.args, target.query);
    } catch (err) {
      if (window.console) console.error(err);
      scr.el.append(errorState(new ApiError(0, 'internal', T.generic), () => render(target)));
    }
    window.scrollTo(0, o.back ? scrollMemo.get(target.hash) || 0 : 0);
    const heading = scr.el.querySelector('h1');
    if (heading) safely(() => heading.focus({ preventScroll: true }));
    syncClosing();
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
    setCssPx('--dock-h', dock && dock.isConnected ? dock.offsetHeight : 0);
  }

  function buildShell() {
    const bannerSlot = h('div', { class: 'banner-slot' });
    const main = h('main', { id: 'main', class: 'main' });
    const tabbar = h('div', { class: 'tabbar' });
    if (dock) dock.remove();
    dock = h('nav', { class: 'dock', 'aria-label': 'Разделы' }, tabbar);
    appRoot.replaceChildren(bannerSlot, main);
    appRoot.removeAttribute('aria-busy');
    document.body.appendChild(dock);
    shell = { bannerSlot, main, tabbar };
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
      h('span', { class: 't-ico', 'aria-hidden': 'true' }, tab.icon),
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

  function setDocTitle(title) {
    document.title = title ? title + ' · Эффективность' : 'Эффективность';
  }

  /** Шапка экрана: заголовок h1 (на него переходит фокус), подзаголовок, «‹ Назад» вне Telegram. */
  function screenHead(title, sub) {
    setDocTitle(title);
    const nested = Boolean(nav.current && (!nav.current.route.root || nav.stack.length > 1));
    const h1 = h('h1', { tabindex: '-1' }, title);
    const subEl = h('p', { class: 'sub', hidden: !sub }, sub || '');
    const el = h('header', { class: 'screen-head' },
      nested && !back.native ? h('button', { type: 'button', class: 'back-link', onclick: () => goBack() }, '‹ Назад') : null,
      h1, subEl);
    return {
      el,
      setTitle(text) {
        h1.textContent = text;
        setDocTitle(text);
      },
      setSub(text) {
        subEl.textContent = text || '';
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
      rows.push(h('div', { class: 'sk-row' }, sk('sk-ico'), h('div', { class: 'sk-lines' }, sk('sk-line w70'), sk('sk-line w40'))));
    }
    return h('div', { class: 'list', 'aria-hidden': 'true' }, rows);
  }

  function skelHero() {
    return h('div', { class: 'card hero', 'aria-hidden': 'true' }, sk('sk-big'), sk('sk-line w60'), h('div', { class: 'mt12' }, sk('sk-line w90')));
  }

  function skelDashboard() {
    return [
      skelHero(),
      h('div', { class: 'totals', 'aria-hidden': 'true' }, [0, 1, 2, 3].map(() => h('div', { class: 'total' }, sk('sk-line w40 mx-auto'), h('div', { class: 'mt8' }, sk('sk-line w70 mx-auto'))))),
      sk('sk-block skel-on-page'),
      skelList(4),
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

  function emptyState(text, icon) {
    return h('div', { class: 'state' }, h('div', { class: 'state-ico', 'aria-hidden': 'true' }, icon || '🗂️'), h('p', null, text));
  }

  function errorState(err, retry) {
    const offline = err.status === 0;
    const final = err.status === 404 || err.status === 403;
    return h('div', { class: 'state state-error', role: 'alert' },
      h('div', { class: 'state-ico', 'aria-hidden': 'true' }, offline ? '📡' : err.status === 404 ? '🔍' : '⚠️'),
      h('p', null, stripLeadingEmoji(err.message || T.generic)),
      final
        ? h('button', { type: 'button', class: 'btn', onclick: () => goBack() }, '‹ Назад')
        : retry ? h('button', { type: 'button', class: 'btn btn-primary', onclick: retry }, T.retry) : null);
  }

  function note(text, tone, extra) {
    return h('div', { class: 'note' + (tone ? ' ' + tone : '') }, text, extra || null);
  }

  function pill(text, tone) {
    return h('span', { class: 'pill' + (tone ? ' ' + tone : '') }, h('span', { class: 'dot', 'aria-hidden': 'true' }), text);
  }

  function line(label, value, cls) {
    return h('p', { class: 'line' + (cls ? ' ' + cls : '') }, label ? h('span', { class: 'muted' }, label + ' ') : null, value);
  }

  function kvBlock(label, value, extra) {
    return h('div', { class: 'kvb' }, h('div', { class: 'kvb-k' }, label), h('div', { class: 'kvb-v pre' }, value), extra || null);
  }

  function button(text, onClick, cls, attrs) {
    return h('button', Object.assign({ type: 'button', class: 'btn' + (cls ? ' ' + cls : ''), onclick: onClick }, attrs || {}), text);
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
    }, item.label));
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
        if (i >= 0) btns[i].textContent = text;
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
      : { class: 'kbar', width: '100%', height: '10', role: 'img', 'aria-label': 'KPI: ' + (text || pct(kpi)) + ', отметка — 100 %', focusable: 'false' });
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
    const label = 'KPI по неделям: ' + points.map((p) => p.label + ' — ' + (isNum(p.kpi) ? pct(p.kpi) : 'нет данных')).join(', ');
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
   * loadInto(scr, box, {request: () => ({ep, opts}), skeleton, render}) -> run(fresh).
   * Кэш свежий — рисуем сразу; иначе скелетон (если показывать нечего), затем данные или ошибка
   * с кнопкой «Повторить». Поздние ответы (экран сменился, запрос устарел) отбрасываются.
   */
  function loadInto(scr, box, spec) {
    let seq = 0;

    function paint(data) {
      // Перерисовка на месте (опрос «оценка рассчитывается», обновление после действия) не должна
      // сворачивать открытые блоки и сбрасывать прокрутку: раскрытые <details data-key> и позиция — как были.
      const again = box.dataset.state === 'ok';
      const opened = again ? openFolds(box) : null;
      const y = window.scrollY;
      box.dataset.state = 'ok';
      box.replaceChildren.apply(box, nodes(spec.render(data)));
      if (!again) return;
      box.querySelectorAll('details[data-key]').forEach((d) => {
        if (opened.has(d.dataset.key)) d.open = true;
      });
      if (window.scrollY !== y) window.scrollTo(0, y);
    }

    async function run(fresh) {
      const my = ++seq;
      const req = spec.request();
      const hit = fresh ? null : peek(req.ep, req.opts);
      if (hit) {
        scr.loadedAt = hit.at;
        paint(hit.data);
        return;
      }
      box.setAttribute('aria-busy', 'true');
      if (box.dataset.state !== 'ok') box.replaceChildren.apply(box, nodes(spec.skeleton()));
      try {
        const data = await api(req.ep, Object.assign({}, req.opts, { fresh: Boolean(fresh) }));
        if (!scr.alive || my !== seq) return;
        scr.loadedAt = Date.now();
        paint(data);
      } catch (err) {
        // 401/403 доступа уже сменили экран; 429 показан тостом, но здесь нужна кнопка «Повторить».
        if (!scr.alive || my !== seq || (err.handled && err.status !== 429)) return;
        if (!err.handled) haptic.err();
        box.dataset.state = 'error';
        box.replaceChildren(errorState(err, () => run(true)));
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
    const el = h('div', { class: 'field' },
      h('label', { for: id }, h('span', null, o.label, o.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null), counter),
      input, hint, err);

    function updateCounter() {
      if (!counter) return;
      const n = input.value.length;
      counter.textContent = n >= o.max * 0.8 ? n + ' / ' + o.max : '';
      counter.classList.toggle('over', n > o.max);
    }

    function setError(message) {
      err.textContent = message || '';
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
      err.textContent = message || '';
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
      err.textContent = message || '';
      if (message) dateInput.setAttribute('aria-invalid', 'true');
      else dateInput.removeAttribute('aria-invalid');
    }

    function sync(byUser) {
      chips.forEach((c, i) => c.setAttribute('aria-pressed', String(quick[i].date === model.date)));
      caption.textContent = model.date
        ? '📅 ' + humanDate(model.date) + ', ' + model.time
        : 'Выберите дату кнопкой или в календаре';
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

  /** Вес задачи: чипы weight_options (⚠️ — неделя перегружена) + своё число 1..100; model.weight. */
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
      err.textContent = message || '';
      if (message) custom.setAttribute('aria-invalid', 'true');
      else custom.removeAttribute('aria-invalid');
    }

    function paint() {
      chipsBox.replaceChildren.apply(chipsBox, options.map((w) => chip(
        w + ' %' + (over[w] ? ' ⚠️' : ''),
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
        parts.push('Сейчас на неделе: ' + load + ' %. Рекомендуется, чтобы сумма весов за неделю была ≈100 %.');
        if (isNum(model.weight) && model.weight > 0 && load + model.weight > 100) {
          parts.push('⚠️ С этой задачей будет ' + (load + model.weight) + ' %.');
        }
        if (week) parts.push(week);
      } else {
        parts.push('Доля задачи в оценке эффективности сотрудника, 1–100 %.');
      }
      hint.textContent = parts.join(' ');
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

  /** План: число + единица (необязательно) и «Убрать план»; model.plan_value / model.plan_unit — строки. */
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
    } }, '✕ Убрать план');
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
      err.textContent = message || '';
      if (message) value.setAttribute('aria-invalid', 'true');
      else value.removeAttribute('aria-invalid');
    }
    clear.hidden = !String(model.plan_value || '').trim() && !String(model.plan_unit || '').trim();

    return {
      el: h('div', { class: 'field' },
        h('label', { for: id }, h('span', null, 'План (число)'), h('span', { class: 'counter' }, 'необязательно')),
        h('div', { class: 'pair' }, value, unit),
        h('p', { class: 'field-hint' }, 'Например: 100 договоров. Оставьте пустым, если числового плана нет.'),
        clear, err),
      set(v, u) {
        model.plan_value = v;
        model.plan_unit = u;
        value.value = v;
        unit.value = u;
        clear.hidden = !v && !u;
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

  /** Кнопка «✨ Сделать измеримым» (§11.9): запрос к AI и лист с вариантом. */
  function aiHelper(scr, o) {
    let busy = false;
    let raw0 = '';
    const btn = h('button', { type: 'button', class: 'btn', onclick: () => run(null) }, '✨ Сделать измеримым');
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
      status.textContent = '⏳ Формулирую измеримый результат…';
      try {
        const body = { title, raw_result: raw0 };
        if (previous) body.previous = previous.slice(0, 1000);
        const suggestion = await api(EP.formulate, { body, timeout: AI_TIMEOUT_MS });
        if (scr.alive) showSuggestion(suggestion);
      } catch (err) {
        if (scr.alive && !err.handled) {
          haptic.err();
          toast(err.code === 'timeout' ? T.aiTimeout : err.message);
        }
      } finally {
        busy = false;
        status.textContent = '';
        if (scr.alive) refresh();
      }
    }

    function showSuggestion(sg) {
      const hasPlan = isNum(sg.plan_value);
      sheet.show({
        title: sg.source === 'ai' ? '🤖 Измеримая формулировка' : '📐 Подсказка без AI',
        body: h('div', null,
          sg.notice ? note(sg.notice, 'warn') : null,
          h('p', { class: 'muted small' }, 'Вы написали: ', h('i', null, raw0)),
          h('p', { class: 'suggest' }, sg.expected_result),
          hasPlan ? line('📊 План:', h('b', { class: 'num' }, fmtNum(sg.plan_value) + (sg.plan_unit ? ' ' + sg.plan_unit : ''))) : null,
          sg.note ? h('p', { class: 'line' }, h('i', null, '💡 ' + sg.note)) : null,
          sg.source === 'rules' ? h('p', { class: 'line' }, h('span', { class: 'tag' }, 'по правилам, без AI')) : null,
          h('div', { class: 'btn-row mt12' },
            button('🔁 Другой вариант', () => {
              sheet.close(true);
              run(sg.expected_result);
            }),
            button('📝 Оставить как написал', () => sheet.close(false)))),
        primary: {
          text: '✅ Принять',
          onClick: () => {
            o.onAccept(sg, raw0);
            sheet.close(false);
            haptic.ok();
            toast('✅ Формулировка подставлена');
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
      if (pair[1] && pair[0].test(err.message || '')) {
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

  // --- Строки списков -----------------------------------------------------------------------

  function statusIcon(t) {
    return String(t.status_label || '•').split(' ')[0];
  }

  function statusText(t) {
    const label = String(t.status_label || '');
    const i = label.indexOf(' ');
    return i > 0 ? label.slice(i + 1) : label;
  }

  function statusTone(t) {
    if (t.overdue) return 'bad';
    return { done: 'good', submitted: 'info', rework: 'warn', active: 'info', proposed: 'warn' }[t.status] || '';
  }

  /** Строка задачи: «#12 Название», справа tail, ниже исполнитель · приоритет · вес. */
  function taskRow(t, o) {
    const opts = o || {};
    const unaccepted = t.status === 'active' && !t.accepted;
    const meta = [];
    if (opts.assignee && t.assignee) meta.push(t.assignee.short_name);
    if (t.status !== 'proposed') meta.push(t.priority_label, 'вес ' + t.weight + ' %');
    else meta.push('ждёт подтверждения руководителя');
    const warn = unaccepted ? h('span', { class: 'warn' }, ' · не принята') : null;
    const tailClass = 'row-tail' + (t.overdue ? ' is-bad' : '') + (t.status === 'done' ? ' is-score' : '');
    const li = h('li', null, h('button', { type: 'button', class: 'row-main', onclick: () => go('#/task/' + t.id) },
      h('span', { class: 'ico', 'aria-hidden': 'true' }, statusIcon(t)),
      h('span', { class: 'row-body' },
        h('span', { class: 'row-top' },
          h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + t.id), t.title),
          h('span', { class: tailClass }, t.tail)),
        h('span', { class: 'row-sub' }, h('span', { class: 'sr-only' }, statusText(t) + '. '), meta.join(' · '), warn))));
    if (opts.onAccept && unaccepted) {
      const accept = h('button', {
        type: 'button', class: 'row-action', 'aria-label': 'Принять задачу #' + t.id + ' в работу',
        onclick: () => opts.onAccept(t, accept, warn),
      }, '✅ Принять');
      li.classList.add('has-action');
      li.appendChild(accept);
    }
    return li;
  }

  /** «Принять» прямо в строке списка — оптимистично: кнопка сразу сменяется отметкой. */
  async function acceptInline(t, btn, warn) {
    const done = h('span', { class: 'row-done', role: 'status' }, '✔️ Принята');
    btn.replaceWith(done);
    if (warn) warn.hidden = true;
    haptic.sel();
    try {
      await api(EP.acceptTask, { params: { task_id: t.id } });
      haptic.ok();
      toast('✅ Задача #' + t.id + ' принята в работу');
      afterMutation();
    } catch (err) {
      done.replaceWith(btn);
      if (warn) warn.hidden = false;
      reportError(err);
    }
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
      setCounts(c) {
        items.forEach((x, i) => {
          const n = c ? c[STATUS_FILTERS[i].value] : undefined;
          x.cnt.textContent = isNum(n) ? String(n) : '';
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

    function paint(data, append) {
      if (data.counts && spec.onCounts) spec.onCounts(data.counts);
      const q = spec.searchText ? spec.searchText() : '';
      const notes = data.truncated ? note('Показаны первые 2000 задач — уточните запрос.', 'info') : null;
      if (!append) {
        box.dataset.state = 'ok';
        if (!data.items.length) {
          box.replaceChildren.apply(box, nodes([
            emptyState(q ? 'По запросу «' + q + '» ничего не найдено.' : spec.emptyText || 'Задач нет.', q ? '🔍' : '🗂️'),
            notes]));
          return;
        }
        list = h('ul', { class: 'list' });
        box.replaceChildren.apply(box, nodes([notes, list]));
      }
      data.items.forEach((t) => list.appendChild(taskRow(t, spec.row)));
      if (data.page + 1 < data.pages) {
        const more = button('Показать ещё', () => loadMore(more), 'more');
        box.appendChild(more);
      }
    }

    async function reload(fresh) {
      const my = ++seq;
      page = 0;
      const query = spec.query(0);
      const hit = fresh ? null : peek(EP.tasks, { query });
      if (hit) {
        scr.loadedAt = hit.at;
        paint(hit.data, false);
        return;
      }
      box.setAttribute('aria-busy', 'true');
      if (box.dataset.state !== 'ok') box.replaceChildren(skelList(5));
      try {
        const data = await api(EP.tasks, { query, fresh: Boolean(fresh) });
        if (!scr.alive || my !== seq) return;
        scr.loadedAt = Date.now();
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
      more.textContent = 'Загрузка…';
      try {
        const data = await api(EP.tasks, { query: spec.query(page + 1) });
        if (!scr.alive || my !== seq) return;
        page += 1;
        more.remove();
        paint(data, true);
      } catch (err) {
        more.disabled = false;
        more.textContent = 'Показать ещё';
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
    } }, '✕');
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
    return h('div', { class: 'search', role: 'search' },
      h('label', { for: id, class: 'sr-only' }, 'Поиск задач'),
      h('span', { class: 's-ico', 'aria-hidden': 'true' }, '🔍'),
      input, clear);
  }

  /** Переключатель периода: «Неделя · Месяц · Квартал · Год» и «◀ {label} ▶». */
  function periodControl(ps, onChange) {
    const label = h('div', { class: 'label', 'aria-live': 'polite' }, '…');
    const prev = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Предыдущий период', onclick: () => {
      if (ps.offset <= -500) return;
      ps.offset -= 1;
      haptic.sel();
      onChange();
    } }, '◀');
    const next = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Следующий период', disabled: ps.offset >= 0, onclick: () => {
      if (ps.offset >= 0) return;
      ps.offset += 1;
      haptic.sel();
      onChange();
    } }, '▶');
    const seg = segmented(PERIOD_KINDS, ps.kind, (kind) => {
      ps.kind = kind;
      ps.offset = 0;
      label.textContent = '…';
      onChange();
    }, 'Период');
    return {
      el: h('div', null, seg.el, h('div', { class: 'period-nav' }, prev, label, next)),
      update(period) {
        if (!period) return;
        label.textContent = period.label;
        prev.disabled = !period.has_prev;
        next.disabled = !period.has_next;
      },
    };
  }

  // ---------------------------------------------------------------------------------------------
  // 11. Экраны руководителя: команда и карточка сотрудника
  // ---------------------------------------------------------------------------------------------

  const teamPeriod = { kind: 'week', offset: 0 };

  function TeamView(scr) {
    const head = screenHead('Команда');
    const pc = periodControl(teamPeriod, () => load());
    const box = h('div');
    const exportBtn = button('📤 Excel-отчёт в чат', () => exportReport(), 'btn-block');
    // nodes(): заявок нет — pendingBanner() даёт null, а append(null) вставил бы текст «null».
    scr.el.append.apply(scr.el, nodes([head.el, pendingBanner(), pc.el, box, exportBtn]));

    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.dashboard, opts: { query: { kind: teamPeriod.kind, offset: teamPeriod.offset } } }),
      skeleton: skelDashboard,
      render(d) {
        pc.update(d.period);
        return renderTeam(d);
      },
    });

    async function exportReport() {
      exportBtn.disabled = true;
      exportBtn.textContent = '⏳ Готовлю отчёт…';
      try {
        const r = await api(EP.exportReport, { query: { kind: teamPeriod.kind, offset: teamPeriod.offset } });
        haptic.ok();
        toast(r.message || '📤 Отчёт придёт в чат с ботом через несколько секунд.');
      } catch (err) {
        reportError(err);
      } finally {
        exportBtn.disabled = false;
        exportBtn.textContent = '📤 Excel-отчёт в чат';
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

  function pendingBanner() {
    const n = counts().pending_users || 0;
    if (!n) return null;
    const el = note('📥 Заявок на доступ: ' + n + ' — подтвердите в чате («👥 Сотрудники»)', 'info');
    el.classList.add('pending-banner');
    return el;
  }

  function renderTeam(d) {
    const rows = d.rows || [];
    const max = scaleMax(rows.map((r) => r.kpi).concat([d.team.kpi]));
    const t = d.totals || {};
    const hero = h('section', { class: 'card hero', 'aria-label': 'Эффективность команды' },
      h('div', { class: 'hero-top' },
        kpiNumber(d.team.kpi_text, d.team.kpi),
        h('div', { class: 'hero-side' }, plural(t.employees || rows.length, 'сотрудник', 'сотрудника', 'сотрудников'), h('br'), plural(t.total || 0, 'задача', 'задачи', 'задач'))),
      h('p', { class: 'hero-cap' }, isNum(d.team.kpi) ? 'Эффективность команды' : 'Оценённых задач в периоде пока нет'),
      kpiBar(d.team.kpi, max, d.team.kpi_text));
    const totals = h('div', { class: 'totals', role: 'list', 'aria-label': 'Итоги периода' },
      totalTile('✅', t.done, 'выполнено'),
      totalTile('⏰', t.overdue_total, 'просрочено'),
      totalTile('🔄', t.in_progress, 'в работе'),
      totalTile('📝', t.on_review, 'на проверке'));
    const trend = h('section', { class: 'card' }, h('h2', { class: 'card-title' }, trendTitle()), trendChart(d.trend));
    const people = rows.length
      ? h('ul', { class: 'list' }, rows.map((r) => teamRow(r, max)))
      : emptyState('Активных сотрудников пока нет. Подтвердите заявки в чате с ботом: «👥 Сотрудники».', '👥');
    return [hero, totals, trend, sec('Сотрудники', rows.length ? String(rows.length) : null), people];
  }

  /** Окно тренда фиксировано (последние недели до сегодня, §8.8) — заголовок говорит об этом прямо,
   *  иначе под «2025 год» график 2026 года выглядел бы как данные выбранного периода. */
  function trendTitle() {
    const n = cfg().trend_weeks || 8;
    return 'Последние ' + plural(n, 'неделя', 'недели', 'недель') + ' (до сегодня)';
  }

  function totalTile(icon, value, label) {
    return h('div', { class: 'total', role: 'listitem' },
      h('span', { class: 'v' }, h('span', { 'aria-hidden': 'true' }, icon + ' '), isNum(value) ? String(value) : '0'),
      h('span', { class: 'l' }, label));
  }

  function statsLine(st) {
    if (!st || !st.total) return 'задач нет';
    return '✅ ' + st.done + ' · ⏰ ' + st.overdue_total + ' · 🔄 ' + st.in_progress + ' · 📝 ' + st.on_review;
  }

  function teamRow(r, max) {
    const u = r.user;
    const hash = '#/team/user/' + u.id + '?kind=' + teamPeriod.kind + '&offset=' + teamPeriod.offset;
    return h('li', null, h('button', { type: 'button', class: 'row-main kpi-row', onclick: () => go(hash) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-top' },
          h('span', { class: 'row-title' }, u.short_name),
          h('span', { class: 'kpi-val' + (isNum(r.kpi) ? '' : ' is-empty') }, r.kpi_text)),
        u.position ? h('span', { class: 'row-sub' }, u.position) : null,
        kpiBar(r.kpi, max, r.kpi_text, true),
        h('span', { class: 'meta' },
          h('span', { 'aria-hidden': 'true' }, statsLine(r.stats)),
          h('span', { class: 'sr-only' }, r.stats && r.stats.total
            ? 'Выполнено ' + r.stats.done + ', просрочено ' + r.stats.overdue_total + ', в работе ' + r.stats.in_progress + ', на проверке ' + r.stats.on_review
            : 'Задач нет'))),
      h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
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

  /** Общий вид KPI (§11.6.1): карточка сотрудника у руководителя и «Мой KPI» у сотрудника. */
  function kpiView(scr, userId, ps, self) {
    const head = screenHead(self ? 'Мой KPI' : 'Сотрудник', self ? state.me.user.full_name : null);
    const pc = periodControl(ps, () => {
      if (!self) replaceQuery({ kind: ps.kind, offset: ps.offset });
      load();
    });
    const box = h('div');
    scr.el.append(head.el, pc.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.userKpi, opts: { params: { user_id: userId }, query: { kind: ps.kind, offset: ps.offset } } }),
      skeleton: () => [skelHero(), sk('sk-block skel-on-page'), skelList(3)],
      render(d) {
        pc.update(d.period);
        if (!self && d.user) {
          head.setTitle(d.user.full_name);
          head.setSub([d.user.position, d.user.status === 'blocked' ? '⛔ заблокирован' : null].filter(Boolean).join(' · '));
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
    const out = [];
    out.push(h('section', { class: 'card hero', 'aria-label': 'Эффективность за период' },
      h('div', { class: 'hero-top' },
        kpiNumber(cur.kpi_text, cur.kpi),
        h('div', { class: 'hero-side' },
          'Неделя: ', h('b', null, d.week ? d.week.kpi_text : '—'), h('br'),
          'Месяц: ', h('b', null, d.month ? d.month.kpi_text : '—'))),
      h('p', { class: 'hero-cap' }, isNum(cur.kpi) ? 'Эффективность за период' : 'Оценённых задач в периоде пока нет'),
      kpiBar(cur.kpi, max, cur.kpi_text)));

    if (!st.total) {
      out.push(emptyState('Задач в этом периоде нет.', '🗓️'));
    } else {
      out.push(h('div', { class: 'stats', role: 'list', 'aria-label': 'Показатели периода' },
        statCell(st.done + ' из ' + st.total, 'Выполнено'),
        statCell(String(st.overdue_total), 'Просрочено'),
        statCell(pct(st.on_time_pct), 'Выполнение в срок'),
        statCell(String(st.overperformed), 'Перевыполнено'),
        statCell(String(st.self_initiated), 'Внесено самостоятельно'),
        statCell(String(st.in_progress), 'В работе'),
        statCell(String(st.on_review), 'На проверке'),
        statCell(pct(st.avg_score), 'Средняя оценка')));
    }

    out.push(h('section', { class: 'card' }, h('h2', { class: 'card-title' }, trendTitle()), trendChart(d.trend)));

    const items = cur.items || [];
    if (items.length) {
      out.push(sec('🧮 Вошли в расчёт', String(items.length)));
      out.push(h('ul', { class: 'list' }, items.map((it) => h('li', null,
        h('button', { type: 'button', class: 'row-main', onclick: () => go('#/task/' + it.task_id) },
          h('span', { class: 'row-body' },
            h('span', { class: 'row-top' },
              h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + it.task_id), it.title),
              h('span', { class: 'row-tail is-score' + (it.zero_overdue ? ' is-bad' : '') }, pct(it.score))),
            h('span', { class: 'row-sub' }, 'вес ' + it.weight + ' % × ' + pct(it.score) + (it.zero_overdue ? ' · ⏰ просрочена' : ''))),
          h('span', { class: 'chev', 'aria-hidden': 'true' }, '›'))))));
    }

    out.push(historySection(scr, d, ps, userId));

    if (!self && isManager()) {
      // Как «📋 Задачи сотрудника» в чате (ListCB scope=emp, status=all): все задачи, включая выполненные.
      out.push(button('📋 Задачи сотрудника', () => go('#/tasks?user=' + userId + '&status=all'), 'btn-block mt12'));
    }
    return out;
  }

  function statCell(value, label) {
    return h('div', { class: 'stat', role: 'listitem' }, h('span', { class: 'v' }, value), h('span', { class: 'l' }, label));
  }

  function historySection(scr, d, ps, userId) {
    const hist = d.history || { items: [], page: 0, pages: 1, total: 0 };
    const wrap = h('div', null, sec('📜 История оценок', hist.total ? String(hist.total) : null));
    if (!hist.items.length) {
      wrap.appendChild(emptyState('Оценённых задач пока нет.', '📜'));
      return wrap;
    }
    const list = h('ul', { class: 'list' }, hist.items.map(historyRow));
    wrap.appendChild(list);
    let page = hist.page || 0;
    const pages = hist.pages || 1;
    if (page + 1 < pages) {
      const more = button('Показать ещё', async () => {
        more.disabled = true;
        more.textContent = 'Загрузка…';
        try {
          const next = await api(EP.userKpi, { params: { user_id: userId }, query: { kind: ps.kind, offset: ps.offset, page: page + 1 } });
          if (!scr.alive) return;
          page = next.history.page;
          next.history.items.forEach((it) => list.appendChild(historyRow(it)));
          if (page + 1 >= next.history.pages) more.remove();
          else {
            more.disabled = false;
            more.textContent = 'Показать ещё';
          }
        } catch (err) {
          more.disabled = false;
          more.textContent = 'Показать ещё';
          reportError(err);
        }
      }, 'more');
      wrap.appendChild(more);
    }
    return wrap;
  }

  function historyRow(it) {
    const meta = [it.completed_local || '—', 'вес ' + it.weight + ' %'];
    if (it.is_late) meta.push('⚠️ с опозданием');
    if (it.rework_count) meta.push('↩️ доработок: ' + it.rework_count);
    let score = '🏁 ' + it.final_score_text;
    if (it.decision === 'changed') score = (isNum(it.ai_score) ? '🤖 ' + pct(it.ai_score) + ' → ' : '') + '🏁 ' + it.final_score_text + ' ✏️ изменена';
    else if (it.decision === 'approved') score = '🏁 ' + it.final_score_text + ' ✅ подтверждена';
    return h('li', null, h('button', { type: 'button', class: 'row-main', onclick: () => go('#/task/' + it.task_id) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + it.task_id), it.title),
        h('span', { class: 'hist-line' }, meta.join(' · ')),
        h('span', { class: 'hist-score' }, score)),
      h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
  }

  // ---------------------------------------------------------------------------------------------
  // 12. Списки задач: руководитель (#/tasks) и сотрудник (#/my, #/submit)
  // ---------------------------------------------------------------------------------------------

  const tasksFilter = { status: 'open', q: '', userId: '' };

  function TasksView(scr, args, query) {
    // ?user=ID&status=… — переход из карточки сотрудника. Адрес потом следует за выбором фильтров
    // (syncAddress), поэтому «Назад» из карточки задачи возвращает выбранное, а не исходный ?user.
    const fromCard = query.get('user');
    if (fromCard && /^\d+$/.test(fromCard)) tasksFilter.userId = fromCard;
    const status = query.get('status');
    if (status && STATUS_FILTERS.some((f) => f.value === status)) tasksFilter.status = status;
    const head = screenHead('Задачи');
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
    scr.el.append(head.el,
      searchBox(scr, tasksFilter.q, (q) => {
        if (q === tasksFilter.q) return;
        tasksFilter.q = q;
        list.reload();
      }),
      h('div', { class: 'filter-row' }, select),
      chips.el, box);

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
        status: tasksFilter.status, q: tasksFilter.q.trim() || null, page, limit: 20, counts: page === 0 ? 1 : null,
      }),
      row: { assignee: true },
      onCounts: chips.setCounts,
      searchText: () => tasksFilter.q.trim(),
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
      (items || []).map((u) => h('option', { value: String(u.id) }, u.short_name + (u.position ? ' — ' + u.position : ''))),
      selected && !known ? h('option', { value: selected }, 'Сотрудник #' + selected) : null,
    ]);
  }

  const myFilter = { status: 'open' };

  function MyTasksView(scr) {
    const head = screenHead('Мои задачи');
    const chips = statusChips(myFilter.status, (v) => {
      myFilter.status = v;
      list.reload();
    });
    const box = h('div');
    scr.el.append(head.el, chips.el, box);
    const list = taskList(scr, box, {
      query: (page) => ({ scope: 'my', status: myFilter.status, page, limit: 20, counts: page === 0 ? 1 : null }),
      row: { onAccept: acceptInline },
      onCounts: chips.setCounts,
    });
    scr.refresh = () => list.reload(true);
    list.reload();
  }

  function SubmitListView(scr) {
    const head = screenHead('Сдать результат', 'Выберите задачу, по которой сдаёте результат');
    const box = h('div');
    scr.el.append(head.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.tasks, opts: { query: { scope: 'my', status: 'open', limit: 50 } } }),
      skeleton: () => skelList(4),
      render: (d) => (d.items.length
        ? h('ul', { class: 'list' }, d.items.map(submitRow))
        : emptyState('Нет задач для сдачи. Здесь появятся задачи в работе и на доработке.', '📭')),
    });
    scr.refresh = () => load(true);
    load();
  }

  function submitRow(t) {
    const icon = t.overdue ? '⏰' : t.status === 'rework' ? '↩️' : '📌';
    const sub = t.status === 'rework' ? '↩️ на доработке' + (t.rework_count > 1 ? ' · возвратов: ' + t.rework_count : '') : 'вес ' + t.weight + ' %';
    return h('li', null, h('button', { type: 'button', class: 'row-main', onclick: () => go('#/submit/' + t.id) },
      h('span', { class: 'ico', 'aria-hidden': 'true' }, icon),
      h('span', { class: 'row-body' },
        h('span', { class: 'row-top' },
          h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + t.id), t.title),
          h('span', { class: 'row-tail' + (t.overdue ? ' is-bad' : '') }, t.tail)),
        h('span', { class: 'row-sub' }, h('span', { class: 'sr-only' }, statusText(t) + '. '), sub)),
      h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
  }

  // ---------------------------------------------------------------------------------------------
  // 13. Карточка задачи (#/task/{id}) — общая для ролей
  // ---------------------------------------------------------------------------------------------

  function TaskView(scr, args) {
    const taskId = Number(args[0]);
    const head = screenHead('Задача #' + taskId);
    const box = h('div');
    scr.el.append(head.el, box);
    const poll = pendingPoller(scr, () => load(true));
    const load = loadInto(scr, box, {
      request: () => ({ ep: EP.task, opts: { params: { task_id: taskId } } }),
      skeleton: skelCard,
      render: (card) => {
        poll((card.submissions || []).some((sb) => sb.ai_pending));
        return renderCard(card);
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
        h('h2', { class: 'task-title' }, t.title),
        h('div', { class: 'pills' },
          pill(t.status_label, statusTone(t)),
          t.status === 'done' && t.final_score_text ? pill('🏁 ' + t.final_score_text, 'good') : null),
        h('p', { class: 'deadline-line' }, '📅 ', t.deadline_label),
        acceptanceLine(t, isM)));
      out.push(actionsBlock(card, isM));
      out.push(h('section', { class: 'card' },
        kvBlock('🎯 Ожидаемый результат', t.expected_result),
        t.plan_text ? line('📊 План:', h('b', { class: 'num' }, t.plan_text)) : null,
        t.description ? kvBlock('💬 Описание', t.description) : null));
      out.push(h('section', { class: 'card' },
        peopleLines(t, isM).map((p) => line(p[0], p[1])),
        t.weight_pending
          ? line('⚖️', 'Вес и приоритет: назначит руководитель при подтверждении')
          : [line('⚡ Приоритет:', t.priority_label), line('⚖️ Вес:', t.weight + ' %')],
        t.rework_count ? line('↩️ Возвратов на доработку:', String(t.rework_count)) : null));
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
        const acceptBtn = button('✅ Принял в работу', () => acceptOnCard(acceptBtn), 'btn-primary btn-block');
        main.push(acceptBtn);
      }
      if (a.submit) main.push(button('📤 Сдать результат', () => go('#/submit/' + t.id), (a.accept ? '' : 'btn-primary ') + 'btn-block'));
      if (a.review) main.push(button('🔍 Проверить результат', () => go('#/review/' + t.id), 'btn-primary btn-block'));
      if (a.approve) main.push(button('✅ Подтвердить', () => go('#/proposal/' + t.id), 'btn-primary btn-block'));
      if (a.edit) extra.push(button('✏️ Изменить', () => go('#/task/' + t.id + '/edit')));
      if (a.reject) extra.push(button('❌ Отклонить', () => rejectSheet(scr, t, () => go('#/review?tab=proposals', { force: true, reset: true })), 'btn-danger'));
      if (a.cancel) extra.push(button('🚫 Отменить', () => cancelSheet(scr, t, (task) => {
        card.task = task;
        repaint(card);
      }), 'btn-danger'));
      if (!main.length && !extra.length) return null;
      return h('div', { class: 'actions' }, main, extra.length ? h('div', { class: 'btn-row' }, extra) : null);

      async function acceptOnCard(btn) {
        btn.disabled = true;
        btn.textContent = '⏳ Принимаю…';
        try {
          const r = await api(EP.acceptTask, { params: { task_id: t.id } });
          haptic.ok();
          toast('✅ Задача #' + t.id + ' принята в работу');
          afterMutation();
          if (scr.alive && r && r.task) {
            card.task = r.task;
            repaint(card);
          }
        } catch (err) {
          if (!scr.alive) return;
          btn.disabled = false;
          btn.textContent = '✅ Принял в работу';
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
      return h('p', { class: 'line tone-warn' }, isM ? '⏳ Исполнитель ещё не подтвердил получение' : '⏳ Вы ещё не подтвердили получение');
    }
    return h('p', { class: 'line muted' }, '✔️ Принята в работу: ' + fmtDmHm(t.accepted_at));
  }

  function peopleLines(t, isM) {
    const out = [];
    if (isM && t.assignee) out.push(['👤 Исполнитель:', t.assignee.short_name + (t.assignee.position ? ', ' + t.assignee.position : '')]);
    if (t.source === 'employee') {
      out.push(['✋', 'Внесена сотрудником (устное поручение)']);
      if (t.manager) out.push(['🧑‍💼 Ответственный руководитель:', t.manager.short_name]);
    } else {
      if (t.created_by) out.push(['🧑‍💼 Постановщик:', t.created_by.short_name]);
      if (t.manager && t.created_by && t.manager.id !== t.created_by.id) out.push(['🧑‍💼 Ответственный руководитель:', t.manager.short_name]);
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
        pill(sb.late_text, sb.is_late ? 'bad' : 'good')),
      kvBlock('✅ Что сделано', sb.fact_text),
      sb.result_text ? kvBlock('📈 Результат', sb.result_text) : null,
      sb.fact_line ? h('p', { class: 'line num' }, sb.fact_line) : null,
      filesBlock(sb, isM),
      aiBlock(sb, isM),
      decisionBlock(sb),
    ];
  }

  function filesBlock(sb, isM) {
    const files = sb.attachments || [];
    if (!files.length) return null;
    const list = h('ul', { class: 'files' }, files.map((f) => h('li', { class: 'file' },
      h('span', { class: 'thumb', 'aria-hidden': 'true' }, KIND_ICONS[f.kind] || '📎'),
      h('span', { class: 'fname' }, f.name),
      isNum(f.size) ? h('span', { class: 'fsize' }, fileSize(f.size)) : null)));
    let send = null;
    if (isM) {
      send = button('📎 Прислать файлы в чат', async () => {
        send.disabled = true;
        try {
          await api(EP.sendFiles, { params: { sub_id: sb.id } });
          haptic.ok();
          toast('📎 Файлы придут в чат с ботом');
        } catch (err) {
          reportError(err);
        } finally {
          send.disabled = false;
        }
      }, 'btn-block');
    }
    return h('div', { class: 'files-block' },
      h('p', { class: 'line muted' }, '📎 Файлы (' + files.length + ')' + (isM ? '' : ' — сохранены в чате с ботом')),
      list, send);
  }

  function aiBlock(sb, isM) {
    if (isM) {
      if (sb.ai) {
        return h('div', { class: 'ai-box' },
          h('div', { class: 'ai-label' }, sb.ai.label),
          h('div', { class: 'ai-score' }, sb.ai.score_text),
          sb.ai.rationale ? h('p', { class: 'ai-why' }, sb.ai.rationale) : null);
      }
      if (sb.ai_pending) {
        return h('div', { class: 'ai-box', role: 'status' },
          h('div', { class: 'ai-label' }, '⏳ Предварительная оценка ещё рассчитывается'),
          h('p', { class: 'small muted' }, 'Экран обновится сам — обычно это занимает до пары минут.'));
      }
      return null;
    }
    if (sb.ai_hidden) return note('📝 Результат на проверке у руководителя. Решение придёт в чат с ботом.', 'info');
    if (sb.ai) return h('p', { class: 'line muted' }, sb.ai.label + ': ' + sb.ai.score_text);
    return null;
  }

  function decisionBlock(sb) {
    if (!sb.decision) return null;
    const out = [];
    if (sb.decision === 'rework') {
      out.push(h('p', { class: 'line' }, h('b', null, sb.decision_label || '↩️ Возвращено на доработку')));
    } else {
      out.push(h('p', { class: 'line' }, '🏁 Итоговая оценка: ', h('b', { class: 'num' }, sb.final_score_text || pct(sb.final_score)),
        sb.decision_label ? ' — ' + sb.decision_label : ''));
    }
    if (sb.reviewer) {
      out.push(h('p', { class: 'line small muted' }, '🧑‍💼 Проверка: ' + sb.reviewer.short_name + (sb.reviewed_at ? ', ' + fmtDmHm(sb.reviewed_at) : '')));
    }
    if (sb.review_comment) out.push(kvBlock('💬 Комментарий руководителя', sb.review_comment));
    return h('div', { class: 'decision' }, out);
  }

  function eventsBlock(events) {
    if (!events || !events.length) return null;
    return h('details', { class: 'fold', 'data-key': 'history' },
      h('summary', null, h('span', null, '📜 История · ' + events.length)),
      h('div', { class: 'fold-body' }, h('ul', { class: 'events' }, events.map((ev) => h('li', null,
        h('span', { class: 'when' }, ev.at_local + ' — ' + ev.actor_name),
        ev.text)))));
  }

  // --- Листы действий над задачей -----------------------------------------------------------

  function cancelSheet(scr, t, after) {
    const reason = field({ label: 'Причина (необязательно)', multiline: true, rows: 3, max: 1000, placeholder: 'Например: задача больше не актуальна' });
    sheet.show({
      title: '🚫 Отменить задачу #' + t.id,
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
            toast(withNotice('🚫 Задача #' + t.id + ' отменена', r.notice));
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
      title: '❌ Отклонить поручение #' + t.id,
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
            toast(withNotice('❌ Поручение #' + t.id + ' отклонено', r.notice));
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

  const reviewState = { seg: 'results' };

  function segLabel(text, n) {
    return isNum(n) ? text + ' (' + n + ')' : text;
  }

  function ReviewQueueView(scr, args, query) {
    if (query.get('tab') === 'proposals' || query.get('tab') === 'results') reviewState.seg = query.get('tab');
    const head = screenHead('Проверка');
    const c = counts();
    const seg = segmented([
      { value: 'results', label: segLabel('Результаты', c.review) },
      { value: 'proposals', label: segLabel('Поручения', c.proposals) },
    ], reviewState.seg, (v) => {
      reviewState.seg = v;
      replaceQuery({ tab: v });
      box.dataset.state = '';
      load();
    }, 'Что проверять');
    const box = h('div', { role: 'tabpanel' });
    scr.el.append(head.el, seg.el, box);
    const load = loadInto(scr, box, {
      request: () => ({ ep: reviewState.seg === 'proposals' ? EP.proposals : EP.review }),
      skeleton: () => skelList(4),
      render(d) {
        const items = d.items || [];
        if (reviewState.seg === 'proposals') {
          seg.setLabel('proposals', segLabel('Поручения', items.length));
          return items.length ? h('ul', { class: 'list' }, items.map((it) => proposalRow(it.task))) : emptyState('Новых поручений нет.', '📭');
        }
        seg.setLabel('results', segLabel('Результаты', items.length));
        return items.length ? h('ul', { class: 'list' }, items.map(reviewRow)) : emptyState('Нечего проверять 🎉', '✨');
      },
    });
    scr.onCounts = () => {
      const fresh = counts();
      seg.setLabel('results', segLabel('Результаты', fresh.review));
      seg.setLabel('proposals', segLabel('Поручения', fresh.proposals));
    };
    scr.refresh = () => load(true);
    load();
  }

  function reviewRow(item) {
    const t = item.task;
    const sb = item.submission;
    const ai = sb && sb.ai;
    const tail = ai ? (ai.source === 'ai' ? '🤖 ' : '📐 ') + ai.score_text : '⏳';
    return h('li', null, h('button', { type: 'button', class: 'row-main', onclick: () => go('#/review/' + t.id) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-sub row-kicker' }, t.assignee ? t.assignee.short_name : ''),
        h('span', { class: 'row-top' },
          h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + t.id), t.title),
          h('span', { class: 'row-tail is-score' },
            h('span', { 'aria-hidden': 'true' }, tail),
            h('span', { class: 'sr-only' }, ai ? 'Предварительная оценка ' + ai.score_text : 'Оценка рассчитывается'))),
        sb ? h('span', { class: 'row-sub' }, 'сдано ' + sb.created_local + ' · ',
          h('span', { class: sb.is_late ? 'tone-bad' : null }, sb.late_text),
          sb.attempt > 1 ? ' · попытка ' + sb.attempt : '') : null),
      h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
  }

  function proposalRow(t) {
    return h('li', null, h('button', { type: 'button', class: 'row-main', onclick: () => go('#/proposal/' + t.id) },
      h('span', { class: 'row-body' },
        h('span', { class: 'row-sub row-kicker' }, t.assignee ? t.assignee.short_name : ''),
        h('span', { class: 'row-top' }, h('span', { class: 'row-title' }, h('span', { class: 'id' }, '#' + t.id), t.title)),
        h('span', { class: 'row-sub' + (t.overdue || Date.parse(t.deadline) <= serverNow() ? ' tone-bad' : '') }, 'срок ' + t.deadline_local)),
      h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
  }

  function backToQueue(tab) {
    go(tab === 'proposals' ? '#/review?tab=proposals' : '#/review', { force: true, reset: true });
  }

  function isAlreadyProcessed(err) {
    return err.status === 400 && /уже обработан/i.test(err.message || '');
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
    });

    function renderReview(card) {
      const t = card.task;
      const subs = card.submissions || [];
      const sb = subs.length ? subs[subs.length - 1] : null;
      poll(Boolean(sb && sb.ai_pending && t.actions && t.actions.review));
      if (!t.actions || !t.actions.review || !sb) {
        primary.hide();
        return [
          h('section', { class: 'card' }, h('h2', { class: 'task-title' }, t.title), h('div', { class: 'pills' }, pill(t.status_label, statusTone(t)))),
          emptyState('Результат уже обработан или ещё не сдан.', '✔️'),
          button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true }), 'btn-block'),
        ];
      }
      const canConfirm = Boolean(sb.ai && isNum(sb.ai.score));
      primary.set(canConfirm ? { text: '✅ Подтвердить ' + sb.ai.score_text, onClick: () => confirmScore(t, sb) } : null);
      return [
        h('section', { class: 'card' },
          h('h2', { class: 'task-title' }, '#' + t.id + ' ' + t.title),
          h('p', { class: 'line muted mt8' }, '👤 ' + (t.assignee ? t.assignee.short_name : '—') + ' · попытка ' + sb.attempt + ' · вес ' + t.weight + ' %')),
        h('section', { class: 'card' },
          kvBlock('🎯 План', t.expected_result, t.plan_text ? line('📊', h('b', { class: 'num' }, t.plan_text)) : null),
          kvBlock('✅ Факт', sb.fact_text),
          sb.result_text ? kvBlock('📈 Результат', sb.result_text) : null,
          sb.fact_line ? h('p', { class: 'line num' }, sb.fact_line) : null),
        h('section', { class: 'card' },
          line('📅 Срок:', fmtFull(sb.deadline_at_submit)),
          h('p', { class: 'line' }, h('span', { class: 'muted' }, '📤 Сдано: '), sb.created_local + ' — ', h('span', { class: sb.is_late ? 'tone-bad' : 'tone-good' }, sb.late_text)),
          filesBlock(sb, true)),
        aiReviewCard(sb),
        h('p', { class: 'muted small center' }, 'Окончательное решение — за руководителем.'),
        h('div', { class: 'btn-row' },
          button('✏️ Изменить оценку', () => scoreSheet(scr, sb)),
          button('↩ На доработку', () => reworkSheet(scr, t, sb))),
        h('button', { type: 'button', class: 'link-btn center-block', onclick: () => go('#/task/' + t.id) }, 'Открыть карточку задачи'),
      ];
    }

    async function confirmScore(t, sb) {
      primary.update({ progress: true });
      try {
        const r = await api(EP.confirm, { params: { sub_id: sb.id } });
        haptic.ok();
        toast(withNotice('✅ Подтверждено: ' + sb.ai.score_text, r.notice));
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

  function aiReviewCard(sb) {
    if (sb.ai) {
      return h('section', { class: 'card ai-card', 'aria-label': 'Предварительная оценка' },
        h('div', { class: 'ai-label' }, sb.ai.label),
        h('div', { class: 'kpi-num' }, String(Math.floor(sb.ai.score + 0.5)), h('span', { class: 'unit' }, '%')),
        sb.ai.rationale ? h('p', { class: 'ai-why' }, sb.ai.rationale) : null,
        sb.ai.model ? h('p', { class: 'small muted mt8' }, 'Модель: ' + sb.ai.model) : null);
    }
    if (sb.ai_pending) {
      return h('section', { class: 'card ai-card', role: 'status' },
        h('div', { class: 'ai-label' }, '⏳ Предварительная оценка ещё рассчитывается'),
        h('p', { class: 'small muted' }, 'Экран обновится сам. Можно не ждать — изменить оценку или вернуть на доработку.'));
    }
    return h('section', { class: 'card ai-card' }, h('div', { class: 'ai-label' }, '🤖 Предварительной оценки нет — выставьте оценку сами.'));
  }

  function scoreSheet(scr, sb) {
    const maxScore = cfg().max_score || 150;
    const options = (cfg().score_options || []).slice();
    let marked = null;
    if (sb.ai && isNum(sb.ai.score)) {
      marked = Math.max(0, Math.min(maxScore, Math.floor(sb.ai.score + 0.5)));
      if (options.indexOf(marked) < 0) options.push(marked);
      options.sort((a, b) => a - b);
    }
    let value = null;
    const chipsBox = h('div', { class: 'chips wrap', role: 'group', 'aria-label': 'Быстрые оценки' });
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
      chipsBox.replaceChildren.apply(chipsBox, options.map((o) => chip((o === marked ? '🤖 ' : '') + o + ' %', value === o, () => {
        value = o;
        custom.set('');
        custom.setError('');
        paintChips();
        sync();
      }, o === marked ? o + ' % — предлагает AI' : null)));
    }

    function valid() {
      return isNum(value) && value >= 0 && value <= maxScore;
    }

    function sync() {
      primary.update({ text: valid() ? 'Поставить ' + fmtNum(value) + ' %' : 'Выберите оценку', enabled: valid() }, 'sheet');
    }

    paintChips();
    sheet.show({
      title: '✏️ Изменить оценку',
      body: h('div', null,
        sb.ai ? h('p', { class: 'muted' }, sb.ai.label + ': ', h('b', { class: 'num' }, sb.ai.score_text)) : h('p', { class: 'muted' }, '🤖 Предварительной оценки AI нет.'),
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
            const r = await api(EP.setScore, { params: { sub_id: sb.id }, body: { score: value, comment: comment.get().trim() || null } });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('✅ Оценка: ' + fmtNum(value) + ' %', r.notice));
            afterMutation();
            backToQueue('results');
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
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
      placeholder: 'Например: добавьте расчёт по 12 договорам с нарушениями', onInput: () => sync(),
    });
    const picker = deadlinePicker(dl, { label: 'Новый срок', onChange: () => sync() });
    const pickerWrap = h('div', { hidden: mode !== 'new' }, picker.el);
    const groupName = uid('rw');
    const keep = h('input', { type: 'radio', name: groupName, value: 'keep', checked: mode === 'keep', disabled: past });
    const fresh = h('input', { type: 'radio', name: groupName, value: 'new', checked: mode === 'new' });
    [keep, fresh].forEach((r) => r.addEventListener('change', () => {
      mode = r.value;
      pickerWrap.hidden = mode !== 'new';
      haptic.sel();
      sync();
    }));

    function ready() {
      return comment.get().trim() !== '' && (mode === 'keep' || Boolean(picker.value()));
    }

    function sync() {
      primary.update({ enabled: ready() }, 'sheet');
    }

    sheet.show({
      title: '↩ Вернуть на доработку',
      body: h('div', null,
        comment.el,
        h('div', { class: 'field', role: 'radiogroup', 'aria-label': 'Срок доработки' },
          h('div', { class: 'label' }, 'Срок доработки'),
          h('label', { class: 'radio-line' + (past ? ' is-disabled' : '') }, keep,
            h('span', null, '📌 Оставить текущий (' + t.deadline_local + ')',
              past ? h('span', { class: 'why' }, 'срок уже прошёл — укажите новый') : null)),
          h('label', { class: 'radio-line' }, fresh, h('span', null, '📅 Новый срок'))),
        pickerWrap),
      dirty: () => comment.get().trim() !== '',
      primary: {
        text: 'Вернуть на доработку',
        enabled: false,
        onClick: async () => {
          const okComment = comment.validate();
          const okDate = mode === 'keep' || picker.validate();
          if (!okComment || !okDate) {
            haptic.err();
            return;
          }
          primary.update({ progress: true }, 'sheet');
          try {
            const body = { comment: comment.get().trim() };
            if (mode === 'new') body.deadline = picker.value();
            const r = await api(EP.rework, { params: { sub_id: sb.id }, body });
            sheet.close(true);
            haptic.ok();
            toast(withNotice('↩ Возвращено на доработку', r.notice));
            afterMutation();
            backToQueue('results');
          } catch (err) {
            primary.update({ progress: false }, 'sheet');
            if (!err.handled && /срок/i.test(err.message || '') && !isAlreadyProcessed(err)) {
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
          h('section', { class: 'card' }, h('h2', { class: 'task-title' }, t.title), h('div', { class: 'pills' }, pill(t.status_label, statusTone(t)))),
          emptyState('Поручение уже обработано.', '✔️'),
          button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true }), 'btn-block'),
        ];
      }
      const expired = Date.parse(t.deadline) <= serverNow();
      const formErr = h('div', { class: 'form-error', role: 'alert' });
      const wp = weightPicker(model, { onChange: () => primary.update({ enabled: wp.filled() }) });
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
          toast(withNotice('✅ Поручение #' + t.id + ' подтверждено — задача в работе', r.notice));
          afterMutation();
          backToQueue('proposals');
        } catch (err) {
          if (!scr.alive) return;
          primary.update({ progress: false });
          if (err.handled) return;
          haptic.err();
          if (/уже обработан/i.test(err.message || '')) {
            toast(err.message);
            afterMutation();
            backToQueue('proposals');
            return;
          }
          formErr.replaceChildren(note(err.message, 'bad', /срок/i.test(err.message || '')
            ? h('div', { class: 'mt8' }, button('✏️ Изменить срок', () => go('#/task/' + t.id + '/edit')))
            : null));
          safely(() => formErr.scrollIntoView({ block: 'center', behavior: scrollBehavior() }));
        }
      }

      primary.set({ text: '✅ Подтвердить поручение', enabled: wp.filled(), onClick: approve });
      return [
        h('section', { class: 'card' },
          h('h2', { class: 'task-title' }, t.title),
          h('p', { class: 'line muted mt8' }, '👤 ' + (t.assignee ? t.assignee.short_name : '—') + ' · ✋ внесено сотрудником (устное поручение)'),
          h('p', { class: 'deadline-line' }, '📅 ', t.deadline_label)),
        expired ? note('⚠️ Срок поручения уже прошёл — сначала измените срок.', 'bad',
          h('div', { class: 'mt8' }, button('✏️ Изменить срок', () => go('#/task/' + t.id + '/edit')))) : null,
        h('section', { class: 'card' },
          kvBlock('🎯 Ожидаемый результат', t.expected_result),
          t.plan_text ? line('📊 План:', h('b', { class: 'num' }, t.plan_text)) : null,
          t.description ? kvBlock('💬 Описание', t.description) : null),
        sec('Подтвердить'),
        h('section', { class: 'card' },
          h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), pr.el),
          wp.el),
        formErr,
        h('div', { class: 'btn-row' },
          button('✏️ Изменить', () => go('#/task/' + t.id + '/edit')),
          button('❌ Отклонить', () => rejectSheet(scr, t, () => backToQueue('proposals')), 'btn-danger')),
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
    scr.el.append(head.el, box);
    scr.dirty = () => draftDirty(d);
    loadInto(scr, box, {
      request: () => ({ ep: EP.employees }),
      skeleton: skelForm,
      render: (data) => newTaskForm(scr, d, data.items || []),
    })();
  }

  function applySuggestion(d, sg, raw, resultField, plan) {
    d.expected_result = sg.expected_result;
    resultField.set(sg.expected_result);
    if (isNum(sg.plan_value)) plan.set(fmtNum(sg.plan_value), sg.plan_unit || '');
    d.description = raw && raw.trim() !== String(sg.expected_result).trim() ? raw.trim() : null;
  }

  function newTaskForm(scr, d, employees) {
    if (!employees.length) {
      primary.hide();
      return emptyState(T.noEmployees, '👥');
    }
    if (d.assignee_id && !employees.some((u) => String(u.id) === d.assignee_id)) d.assignee_id = '';
    const formErr = h('div', { class: 'form-error', role: 'alert' });
    let loadTimer = 0;
    scr.onDestroy(() => clearTimeout(loadTimer));

    const who = selectField({
      label: 'Сотрудник', required: true, value: d.assignee_id, placeholder: 'Выберите сотрудника',
      requiredText: 'Выберите сотрудника',
      options: employees.map((u) => ({ value: String(u.id), label: u.short_name + (u.position ? ' — ' + u.position : '') })),
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

    function filled() {
      return Boolean(d.assignee_id && d.title.trim() && d.expected_result.trim() && d.deadline.date && weight.filled());
    }

    function changed() {
      ai.refresh();
      primary.update({ enabled: filled() });
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
      if (!firstInvalid([who, title, result, plan, deadline, weight])) return;
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
        toast(withNotice('✅ Задача #' + r.task.id + ' поставлена', r.notice));
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

    primary.set({ text: 'Поставить задачу', enabled: filled(), onClick: submit });
    refreshLoad();
    return h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
      formErr,
      who.el, title.el, result.el, plan.el, deadline.el,
      h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), priority.el),
      weight.el);
  }

  function ProposeView(scr) {
    if (!drafts.propose) drafts.propose = newDraft();
    const d = drafts.propose;
    const head = screenHead('Поручение', 'Внесите поручение, полученное устно: руководитель подтвердит его.');
    scr.el.append(head.el);
    scr.dirty = () => draftDirty(d);
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

    function filled() {
      return Boolean(d.title.trim() && d.expected_result.trim() && d.deadline.date);
    }

    function changed() {
      ai.refresh();
      primary.update({ enabled: filled() });
      syncClosingSoon();
    }

    async function submit() {
      formErr.replaceChildren();
      if (!firstInvalid([title, result, plan, deadline])) return;
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
        toast(r.notice || '📤 Поручение #' + r.task.id + ' отправлено руководителю на подтверждение');
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

    primary.set({ text: '📤 Отправить руководителю', enabled: filled(), onClick: submit });
    scr.el.append(h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
      formErr, title.el, result.el, plan.el, deadline.el));
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
          return [emptyState('Эту задачу сейчас изменить нельзя: ' + statusText(t).toLowerCase() + '.', '🔒'),
            button('Открыть карточку задачи', () => go('#/task/' + t.id, { replace: true }), 'btn-block')];
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
    const parts = [formErr];
    let title = null;
    let result = null;
    let plan = null;
    let deadline = null;
    let weight = null;

    if (fields.has('title')) {
      title = field({ label: 'Задача', required: true, max: 255, value: m.title, requiredText: 'Название не может быть пустым', onInput: (v) => { m.title = v; changed(); } });
      parts.push(title.el);
    }
    if (fields.has('expected_result')) {
      result = field({ label: 'Ожидаемый результат', required: true, multiline: true, rows: 4, max: 2000, value: m.expected_result, requiredText: 'Опишите ожидаемый результат', onInput: (v) => { m.expected_result = v; changed(); } });
      parts.push(result.el);
    }
    if (fields.has('plan')) {
      plan = planFields(m, { onChange: changed });
      parts.push(plan.el);
    }
    if (fields.has('deadline')) {
      deadline = deadlinePicker(m.deadline, { onChange: () => { changed(); refreshLoad(); } });
      parts.push(deadline.el);
    }
    if (fields.has('priority')) {
      const pr = segmented(PRIORITIES, m.priority, (v) => { m.priority = v; changed(); }, 'Приоритет', 'radio');
      parts.push(h('div', { class: 'field' }, h('div', { class: 'label' }, 'Приоритет'), pr.el));
    }
    if (fields.has('weight')) {
      weight = weightPicker(m, { onChange: changed });
      parts.push(weight.el);
    }
    if (t.status === 'proposed') parts.push(note('⚖️ Вес и приоритет назначаются при подтверждении поручения.', 'info'));

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
      primary.update({ enabled: hasChanges() });
      syncClosingSoon();
    }

    async function save() {
      formErr.replaceChildren();
      if (!firstInvalid([title, result, plan, deadline, weight])) return;
      const body = changes();
      if (!Object.keys(body).length) return;
      primary.update({ progress: true });
      try {
        const r = await api(EP.updateTask, { params: { task_id: t.id }, body });
        Object.assign(init, JSON.parse(JSON.stringify(m)));
        haptic.ok();
        toast(withNotice(r.changed && r.changed.length ? '✅ Сохранено' : 'Изменений нет', r.notice));
        afterMutation();
        syncClosing();
        await goBack();
        // Остались на экране (переход отменён) — форма снова рабочая, а не «сохраняется» навсегда.
        if (scr.alive) primary.update({ progress: false, enabled: hasChanges() });
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

    primary.set({ text: 'Сохранить', enabled: false, onClick: save });
    refreshLoad();
    return {
      el: h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() }, parts),
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
    const pick = button('📎 Выбрать файлы', () => input.click(), 'btn-block', { 'aria-describedby': errId });
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
      if (file.size === 0) return 'Пустой файл «' + file.name + '»';
      if (file.size > maxOne) return 'Файл «' + file.name + '» больше ' + (cfg().max_file_mb || 20) + ' МБ.';
      return '';
    }

    function paint() {
      list.replaceChildren.apply(list, items.map((it, index) => {
        const bad = problemOf(it.file);
        return h('li', { class: 'file' + (bad ? ' is-bad' : '') },
          it.url ? h('img', { class: 'thumb', src: it.url, alt: '' }) : h('span', { class: 'thumb', 'aria-hidden': 'true' }, '📄'),
          h('span', { class: 'fname' }, it.file.name),
          h('span', { class: 'fsize' }, fileSize(it.file.size)),
          h('button', { type: 'button', class: 'x', 'aria-label': 'Убрать файл «' + it.file.name + '»', onclick: () => {
            const [gone] = items.splice(index, 1);
            if (gone && gone.url) URL.revokeObjectURL(gone.url);
            paint();
            validate();
            onChange();
            pick.focus();
          } }, '✕'));
      }));
      const total = items.reduce((sum, it) => sum + it.file.size, 0);
      summary.textContent = items.length
        ? plural(items.length, 'файл', 'файла', 'файлов') + ' · ' + fileSize(total) + ' из ' + (cfg().max_total_mb || 50) + ' МБ'
        : 'До ' + maxCount + ' файлов, каждый до ' + (cfg().max_file_mb || 20) + ' МБ, всего до ' + (cfg().max_total_mb || 50) + ' МБ. Файлы сохранятся в чате с ботом.';
      pick.textContent = items.length ? '📎 Добавить ещё' : '📎 Выбрать файлы';
      pick.disabled = items.length >= maxCount;
    }

    function setError(message) {
      err.textContent = message || '';
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
            h('section', { class: 'card' }, h('h2', { class: 'task-title' }, t.title), h('div', { class: 'pills' }, pill(t.status_label, statusTone(t)))),
            emptyState(NOT_OPEN_TEXTS[t.status] || 'Задача не в работе — сдать результат нельзя.', 'ℹ️'),
            button('📋 К моим задачам', () => go('#/my', { reset: true }), 'btn-block'),
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
      hint: 'План: ' + (t.plan_text || fmtNum(t.plan_value)),
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

    function changed() {
      primary.update({ enabled: m.fact.trim().length > 0 });
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
      if (!firstInvalid([fact, result, value, files, materials])) return;
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
          toast('✅ Результат по задаче #' + t.id + ' отправлен руководителю на проверку.');
          return;
        }
        primary.hide();
        const doneHead = h('h2', { tabindex: '-1' }, 'Результат отправлен');
        box.replaceChildren(h('div', { class: 'done-screen', role: 'status' },
          h('div', { class: 'big', 'aria-hidden': 'true' }, '✅'),
          doneHead,
          h('p', null, '✅ Результат отправлен руководителю на проверку. Решение придёт в чат с ботом.'),
          r.files ? h('p', null, '📎 Файлы сохранены в чате с ботом') : null,
          button('📋 К моим задачам', () => go('#/my', { reset: true }), 'btn-primary btn-block mt12')));
        safely(() => doneHead.focus({ preventScroll: true }));
        window.scrollTo(0, 0);
      } catch (err) {
        uploading = false;
        if (!scr.alive) return;
        progress.hidden = true;
        primary.update({ progress: false, progressText: null });
        if (err.handled) return;
        haptic.err();
        if (err.status === 413 || /файл/i.test(err.message || '')) {
          files.setError(err.message);
          safely(() => files.focus());
          return;
        }
        if (!errorToField(err, [[/подробнее|сделано|факт/i, fact], [/числ|значени/i, value], [/материал/i, materials]])) {
          showFormError(formErr, err);
        }
      }
    }

    primary.set({ text: '📤 Отправить', enabled: false, onClick: send });

    const intro = h('section', { class: 'card' },
      h('h2', { class: 'task-title' }, '📌 Задача #' + t.id + ': ' + t.title),
      h('div', { class: 'mt12' }, kvBlock('🎯 Ожидаемый результат', t.expected_result,
        t.plan_text ? line('📊 План:', h('b', { class: 'num' }, t.plan_text)) : null)),
      line('⏳ Срок:', t.deadline_label),
      t.attempts > 0 ? line('🔁', 'Попытка сдачи №' + (t.attempts + 1)) : null);
    const reworkNote = t.status === 'rework'
      ? note('↩️ Задача возвращена на доработку', 'warn', t.rework_comment ? h('p', { class: 'line pre mt8' }, '💬 Комментарий руководителя: ' + t.rework_comment) : null)
      : null;
    const lateNote = t.overdue ? note('⚠️ Срок уже прошёл — результат будет отмечен как сданный с опозданием.', 'bad') : null;

    return {
      el: [intro, reworkNote, lateNote,
        h('form', { class: 'form', novalidate: true, onsubmit: (e) => e.preventDefault() },
          formErr, fact.el, result.el, value ? value.el : null, files.el, materials.el, progress)],
      guard: () => (uploading ? T.leaveUpload : dirty()),
    };
  }

  // ---------------------------------------------------------------------------------------------
  // 17. Полноэкранные состояния: вне Telegram, доступ, сессия, ошибка запуска
  // ---------------------------------------------------------------------------------------------

  const BLOCK_SCREENS = {
    outside: { icon: '📱', title: 'Откройте из Telegram', action: null },
    session: { icon: '⌛', title: 'Сессия устарела', action: 'Закрыть' },
    not_registered: { icon: '👋', title: 'Нужна регистрация', action: 'Перейти в чат' },
    unregistered: { icon: '👋', title: 'Нужна регистрация', action: 'Перейти в чат' },
    pending: { icon: '⏳', title: 'Заявка на рассмотрении', action: 'Перейти в чат' },
    blocked: { icon: '⛔', title: 'Доступ закрыт', action: 'Перейти в чат' },
  };

  function teardown() {
    if (current) current.destroy();
    current = null;
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
    appRoot.replaceChildren(h('div', { class: 'fullscreen', role: 'alert' },
      h('div', { class: 'big', 'aria-hidden': 'true' }, conf.icon),
      heading,
      h('p', null, stripLeadingEmoji(message || T.generic)),
      action));
    appRoot.removeAttribute('aria-busy');
    safely(() => heading.focus({ preventScroll: true }));
  }

  function showBootError(err) {
    teardown();
    const heading = h('h1', { tabindex: '-1' }, 'Не удалось загрузить');
    appRoot.replaceChildren(h('div', { class: 'fullscreen', role: 'alert' },
      h('div', { class: 'big', 'aria-hidden': 'true' }, err.status === 0 ? '📡' : '⚠️'),
      heading,
      h('p', null, err.message || T.generic),
      button(T.retry, () => {
        appRoot.replaceChildren(bootSkeleton());
        start();
      }, 'btn-primary')));
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
    });
    watchKeyboard();

    auth.initData = resolveInitData();
    if (!auth.initData) {
      showBlocking('outside', T.openFromTelegram);
      if (tg) safely(() => tg.ready());
      return;
    }
    appRoot.replaceChildren(bootSkeleton());
    if (tg) safely(() => tg.ready());
    start();
  }

  boot();
}());
