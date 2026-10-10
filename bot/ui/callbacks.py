"""Фабрики callback-данных для inline-кнопок.

Ограничение Telegram — 64 байта на callback_data, поэтому префиксы и значения короткие.
В строковых значениях нельзя использовать символ ":" (это разделитель aiogram).
Какой модуль обрабатывает какое действие — см. таблицу в SPEC.md, раздел «Callback-данные».

Числа ограничены диапазоном INTEGER SQLite (64 бита): кнопку с подделанным id вроде 2^63 aiogram
просто не сопоставит ни одному хендлеру («Кнопка устарела»), вместо OverflowError в запросе к БД.
"""

from typing import Annotated

from aiogram.filters.callback_data import CallbackData
from pydantic import Field

DbInt = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]


class TaskCB(CallbackData, prefix="t"):
    """Действие над задачей.

    action: open | accept | history | edit | cancel   -> handlers/task_view.py
            submit                                    -> handlers/task_submit.py
            approve | reject | pedit                  -> handlers/task_propose.py
            review                                    -> handlers/task_review.py
    """

    action: str
    task_id: DbInt


class SubCB(CallbackData, prefix="s"):
    """Проверка сданного результата начальником (handlers/task_review.py).

    action: ok (подтвердить оценку AI) | change (изменить оценку) | rework (вернуть) | files (показать файлы)
            | revise (изменить оценку, подтверждённую автоматически)
    """

    action: str
    sub_id: DbInt


class UserCB(CallbackData, prefix="u"):
    """Действия с сотрудником.

    action: approve | reject | block | unblock | role_mgr | role_emp | manage -> handlers/users_admin.py
            card | history                                                  -> handlers/dashboard.py
    """

    action: str
    user_id: DbInt
    page: DbInt = 0


class PeriodCB(CallbackData, prefix="p"):
    """Выбор периода для отчётов (handlers/dashboard.py).

    scope: team (дашборд команды) | emp (карточка сотрудника) | me (моя эффективность) | export
    kind:  week | month | quarter | year
    offset: 0 = текущий период, -1 = предыдущий, ...
    """

    scope: str
    kind: str
    offset: DbInt = 0
    user_id: DbInt = 0


class ListCB(CallbackData, prefix="l"):
    """Списки задач с пагинацией.

    scope: my (мои задачи сотрудника) | all (все задачи, начальник) | emp (задачи одного сотрудника)
           -> handlers/task_view.py
           review -> handlers/task_review.py ; proposals -> handlers/task_propose.py
    status: open | overdue | review | done | all
    """

    scope: str
    status: str = "open"
    page: DbInt = 0
    user_id: DbInt = 0


class LangCB(CallbackData, prefix="g"):
    """Выбор языка интерфейса (handlers/language.py): lang — «ru» | «uz». Работает в любом состоянии диалога."""

    lang: str


class PickCB(CallbackData, prefix="k"):
    """Выбор варианта внутри FSM-диалога (приоритет, вес, срок, подтверждение ...).

    Хендлеры PickCB ОБЯЗАТЕЛЬНО фильтруются по состоянию FSM своего модуля.
    Исключение: field == "cancel" — глобальная отмена любого диалога (handlers/start.py).
    """

    field: str
    value: str = ""
