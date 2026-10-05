"""ORM-модели.

Все даты хранятся как naive UTC (без tzinfo). Перевод в местное время — bot.utils.dates.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Role(enum.StrEnum):
    MANAGER = "manager"    # руководитель
    EMPLOYEE = "employee"  # сотрудник


class UserStatus(enum.StrEnum):
    PENDING = "pending"  # ждёт подтверждения руководителем
    ACTIVE = "active"
    BLOCKED = "blocked"


class TaskStatus(enum.StrEnum):
    PROPOSED = "proposed"    # внесена сотрудником, ждёт подтверждения руководителя
    ACTIVE = "active"        # в работе
    SUBMITTED = "submitted"  # результат сдан, ждёт проверки руководителем
    REWORK = "rework"        # возвращена на доработку (ведёт себя как «в работе»)
    DONE = "done"            # оценена руководителем, оценка окончательная
    CANCELLED = "cancelled"  # отменена руководителем
    REJECTED = "rejected"    # предложение сотрудника отклонено


OPEN_STATUSES = (TaskStatus.ACTIVE, TaskStatus.REWORK)
# Статусы, которые никогда не входят в KPI.
EXCLUDED_FROM_KPI = (TaskStatus.PROPOSED, TaskStatus.REJECTED, TaskStatus.CANCELLED)


class Priority(enum.StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskSource(enum.StrEnum):
    MANAGER = "manager"    # поставлена руководителем
    EMPLOYEE = "employee"  # внесена сотрудником (устное поручение)


class ReviewDecision(enum.StrEnum):
    APPROVED = "approved"  # руководитель подтвердил оценку AI
    CHANGED = "changed"    # руководитель изменил оценку
    REWORK = "rework"      # возвращено на доработку


class AttachmentKind(enum.StrEnum):
    DOCUMENT = "document"
    PHOTO = "photo"
    VIDEO = "video"
    OTHER = "other"


class EventType(enum.StrEnum):
    CREATED = "created"              # руководитель поставил задачу
    PROPOSED = "proposed"            # сотрудник внёс поручение
    APPROVED = "approved"            # руководитель подтвердил предложение
    REJECTED = "rejected"            # руководитель отклонил предложение
    ACCEPTED = "accepted"            # сотрудник принял задачу в работу
    EDITED = "edited"                # изменены поля задачи (data = {"changes": {field: [old, new]}})
    SUBMITTED = "submitted"          # сотрудник сдал результат
    AI_EVALUATED = "ai_evaluated"    # AI/правила предложили оценку
    SCORE_CONFIRMED = "score_confirmed"  # руководитель подтвердил оценку
    SCORE_CHANGED = "score_changed"      # руководитель изменил оценку
    REWORK = "rework"                # возвращено на доработку
    CANCELLED = "cancelled"          # задача отменена
    REMINDER = "reminder"            # отправлено напоминание (data = {"kind": ...})


def _enum(enum_cls: type[enum.Enum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        length=20,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.EMPLOYEE)
    status: Mapped[UserStatus] = mapped_column(_enum(UserStatus), default=UserStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def is_manager(self) -> bool:
        return self.role == Role.MANAGER and self.status == UserStatus.ACTIVE

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE

    @property
    def short_name(self) -> str:
        """«Иванов Иван Иванович» -> «Иванов И. И.»."""
        parts = self.full_name.split()
        if len(parts) <= 1:
            return self.full_name
        return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:] if p)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} tg={self.tg_id} {self.full_name!r} {self.role}/{self.status}>"


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_assignee_status", "assignee_id", "status"),
        Index("ix_tasks_deadline", "deadline"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)        # исходная формулировка
    expected_result: Mapped[str] = mapped_column(Text)           # измеримый ожидаемый результат
    plan_value: Mapped[float | None] = mapped_column(Float)      # плановое число (100 договоров)
    plan_unit: Mapped[str | None] = mapped_column(String(64))    # единица («договоров»)
    deadline: Mapped[datetime] = mapped_column(DateTime)          # UTC
    priority: Mapped[Priority] = mapped_column(_enum(Priority), default=Priority.MEDIUM)
    weight: Mapped[int] = mapped_column(Integer, default=10)      # вес задачи, 1..100 (%)
    status: Mapped[TaskStatus] = mapped_column(_enum(TaskStatus), default=TaskStatus.ACTIVE)
    source: Mapped[TaskSource] = mapped_column(_enum(TaskSource), default=TaskSource.MANAGER)

    assignee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Ответственный руководитель: кто поставил или подтвердил задачу. Ему уходят результаты.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)   # сотрудник принял в работу
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)   # предложение подтверждено
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)  # последняя сдача результата
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)  # окончательная оценка

    ai_score: Mapped[float | None] = mapped_column(Float)     # последняя предложенная оценка
    final_score: Mapped[float | None] = mapped_column(Float)  # окончательная оценка руководителя
    rework_count: Mapped[int] = mapped_column(Integer, default=0)

    assignee: Mapped[User] = relationship(foreign_keys=[assignee_id], lazy="selectin")
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id], lazy="selectin")
    manager: Mapped[User | None] = relationship(foreign_keys=[manager_id], lazy="selectin")
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="task",
        order_by="Submission.id",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    @property
    def is_open(self) -> bool:
        """Задача в работе (в т.ч. на доработке)."""
        return self.status in OPEN_STATUSES

    @property
    def last_submission(self) -> Submission | None:
        return self.submissions[-1] if self.submissions else None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task #{self.id} {self.title!r} {self.status} w={self.weight}>"


class Submission(Base):
    """Сдача фактического результата (по задаче может быть несколько попыток)."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)

    fact_text: Mapped[str] = mapped_column(Text)               # что фактически сделано
    result_text: Mapped[str | None] = mapped_column(Text)      # какой получен результат
    fact_value: Mapped[float | None] = mapped_column(Float)    # фактическое число (110 договоров)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    deadline_at_submit: Mapped[datetime] = mapped_column(DateTime)  # срок на момент сдачи
    is_late: Mapped[bool] = mapped_column(default=False)
    late_days: Mapped[float] = mapped_column(Float, default=0.0)

    ai_score: Mapped[float | None] = mapped_column(Float)
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(String(64))
    ai_source: Mapped[str | None] = mapped_column(String(16))  # "ai" | "rules"

    final_score: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[ReviewDecision | None] = mapped_column(_enum(ReviewDecision))
    review_comment: Mapped[str | None] = mapped_column(Text)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    task: Mapped[Task] = relationship(back_populates="submissions", lazy="selectin")
    reviewer: Mapped[User | None] = relationship(lazy="selectin")
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="submission",
        order_by="Attachment.id",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class Attachment(Base):
    """Файл-подтверждение. Сам файл хранится в Telegram, у нас — только file_id."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[AttachmentKind] = mapped_column(_enum(AttachmentKind))
    file_id: Mapped[str] = mapped_column(String(255))
    file_unique_id: Mapped[str | None] = mapped_column(String(128))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    submission: Mapped[Submission] = relationship(back_populates="attachments", lazy="selectin")


class TaskEvent(Base):
    """Журнал изменений: каждое действие по задаче фиксируется здесь."""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # None = система
    type: Mapped[EventType] = mapped_column(_enum(EventType))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship(lazy="selectin")


class ReminderLog(Base):
    """Отправленные напоминания — чтобы не слать одно и то же дважды."""

    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("task_id", "kind", name="uq_reminder_task_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DigestLog(Base):
    """Отправленные еженедельные сводки (по одной на неделю).

    Бот мог быть выключен в момент сводки: после запуска пропущенную сводку догоняем, а по этой
    записи не шлём её второй раз, если бота просто перезапустили. Таблица новая — init_db
    (create_all) создаёт её в существующей базе при первом запуске, данные не трогаются.
    """

    __tablename__ = "digest_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, unique=True)  # начало недели сводки, UTC
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobLog(Base):
    """Выполненные разовые задания по расписанию (резервная копия за день и т.п.).

    В режиме webhook задания запускает фоновый цикл бота (bot.web.BackgroundLoop, необязательный
    резерв — /tick), и при обновлении на хостинге он идёт в двух экземплярах бота одновременно —
    уникальность (job, key) не даёт выполнить задание дважды.
    """

    __tablename__ = "job_log"
    __table_args__ = (UniqueConstraint("job", "key", name="uq_job_log_job_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32))   # "backup", ...
    key: Mapped[str] = mapped_column(String(64))   # период, например дата «2026-10-03»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FsmState(Base):
    """Состояние незавершённых диалогов (FSM aiogram) — переживает перезапуск бота."""

    __tablename__ = "fsm_state"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)  # bot:chat:user[:thread][:destiny]
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
