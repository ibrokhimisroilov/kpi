class DomainError(Exception):
    """Ошибка бизнес-логики. Текст сообщения на русском показывается пользователю как есть."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
