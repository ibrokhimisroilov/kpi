"""QR-код ссылки для входа на сайт — своим кодом, без библиотек (docs/MINIAPP_SPEC.md §15).

Ровно то, что нужно сайту: байтовый режим, уровень коррекции M, версии 1–6 (до 106 байт; ссылка на бота с
кодом входа — около 70). По ISO/IEC 18004: данные → коды Рида — Соломона по блокам → размещение «змейкой» →
лучшая из восьми масок по штрафам → служебные биты формата. Версии 7+ (со своим блоком «информация о
версии») не поддерживаются — ``encode`` откажет слишком длинной строке.
"""

from __future__ import annotations

__all__ = ["MAX_BYTES", "encode"]

# версия -> (всего кодовых слов, слов коррекции на блок, блоков) для уровня M; блоки внутри версии равны.
_VERSIONS: dict[int, tuple[int, int, int]] = {
    1: (26, 10, 1),
    2: (44, 16, 1),
    3: (70, 26, 1),
    4: (100, 18, 2),
    5: (134, 24, 2),
    6: (172, 16, 4),
}
_ALIGN_CENTER = {2: 18, 3: 22, 4: 26, 5: 30, 6: 34}  # центр единственного выравнивающего узора (строка = столбец)
_FORMAT_GENERATOR = 0x537
_FORMAT_MASK = 0x5412
_LEVEL_M = 0b00


def _capacity(version: int) -> int:
    total, ec, blocks = _VERSIONS[version]
    return total - ec * blocks - 2  # 4 бита режима + 8 бит длины + данные; остаток — завершитель и заполнение


MAX_BYTES = _capacity(max(_VERSIONS))

# --- Поле GF(256), многочлен x^8 + x^4 + x^3 + x^2 + 1 ----------------------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _power in range(255, 512):
    _EXP[_power] = _EXP[_power - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Порождающий многочлен (x − α^0)…(x − α^(degree−1)), коэффициенты от старшей степени."""
    gen = [1]
    for power in range(degree):
        nxt = [0] * (len(gen) + 1)
        for index, coefficient in enumerate(gen):
            nxt[index] ^= coefficient
            nxt[index + 1] ^= _mul(coefficient, _EXP[power])
        gen = nxt
    return gen


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    """Слова коррекции: остаток от деления data·x^degree на порождающий многочлен."""
    gen = _rs_generator(degree)
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for index in range(degree):
            remainder[index] ^= _mul(gen[index + 1], factor)
    return remainder


# --- Данные -> кодовые слова -------------------------------------------------------------------------------


def _codewords(payload: bytes, version: int) -> list[int]:
    total, ec, blocks = _VERSIONS[version]
    data_len = total - ec * blocks
    bits: list[int] = [0, 1, 0, 0]  # байтовый режим
    bits += [(len(payload) >> shift) & 1 for shift in range(7, -1, -1)]
    for byte in payload:
        bits += [(byte >> shift) & 1 for shift in range(7, -1, -1)]
    bits += [0] * min(4, data_len * 8 - len(bits))  # завершитель
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[at : at + 8])), 2) for at in range(0, len(bits), 8)]
    for pad in range(data_len - len(data)):
        data.append(0xEC if pad % 2 == 0 else 0x11)

    size = data_len // blocks
    chunks = [data[at : at + size] for at in range(0, data_len, size)]
    corrections = [_rs_remainder(chunk, ec) for chunk in chunks]
    out: list[int] = []
    for column in range(size):  # слова блоков — по очереди: первое слово каждого, второе каждого, …
        out += [chunk[column] for chunk in chunks]
    for column in range(ec):
        out += [correction[column] for correction in corrections]
    return out


# --- Матрица -------------------------------------------------------------------------------------------


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]  # служебные модули: данные и маска их не трогают
        self._draw_function_patterns()

    def _set(self, row: int, col: int, dark: bool) -> None:
        if 0 <= row < self.size and 0 <= col < self.size:
            self.dark[row][col] = dark
            self.fixed[row][col] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for index in range(size):  # линии синхронизации
            self._set(6, index, index % 2 == 0)
            self._set(index, 6, index % 2 == 0)
        for row, col in ((3, 3), (3, size - 4), (size - 4, 3)):  # три поисковых узора с белой рамкой
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    distance = max(abs(dx), abs(dy))
                    self._set(row + dy, col + dx, distance not in (2, 4))
        center = _ALIGN_CENTER.get(self.version)
        if center is not None:
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self._set(center + dy, center + dx, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)  # место под биты формата занято до размещения данных

    def _draw_format(self, mask: int) -> None:
        data = (_LEVEL_M << 3) | mask
        remainder = data << 10
        for shift in range(14, 9, -1):
            if (remainder >> shift) & 1:
                remainder ^= _FORMAT_GENERATOR << (shift - 10)
        bits = ((data << 10) | remainder) ^ _FORMAT_MASK
        size = self.size

        def bit(index: int) -> bool:
            return (bits >> index) & 1 == 1

        for index in range(6):  # первая копия — вокруг левого верхнего узора
            self._set(index, 8, bit(index))
        self._set(7, 8, bit(6))
        self._set(8, 8, bit(7))
        self._set(8, 7, bit(8))
        for index in range(9, 15):
            self._set(8, 14 - index, bit(index))
        for index in range(8):  # вторая копия — у правого верхнего и левого нижнего
            self._set(8, size - 1 - index, bit(index))
        for index in range(8, 15):
            self._set(size - 15 + index, 8, bit(index))
        self._set(size - 8, 8, True)  # всегда тёмный модуль

    def place(self, codewords: list[int]) -> None:
        bits = [(word >> shift) & 1 == 1 for word in codewords for shift in range(7, -1, -1)]
        size = self.size
        at = 0
        upward = True
        col = size - 1
        while col > 0:
            if col == 6:  # столбец линии синхронизации пропускается
                col -= 1
            for row in range(size - 1, -1, -1) if upward else range(size):
                for current in (col, col - 1):
                    if self.fixed[row][current]:
                        continue
                    self.dark[row][current] = bits[at] if at < len(bits) else False  # хвост — биты остатка (нули)
                    at += 1
            upward = not upward
            col -= 2

    def apply_mask(self, mask: int) -> None:
        """Наложить (или снять — операция обратима) маску на модули данных и записать биты формата."""
        condition = _MASKS[mask]
        for row in range(self.size):
            for col in range(self.size):
                if not self.fixed[row][col] and condition(row, col):
                    self.dark[row][col] = not self.dark[row][col]
        self._draw_format(mask)

    def penalty(self) -> int:
        size, dark = self.size, self.dark
        score = 0
        lines = [list(row) for row in dark] + [[dark[row][col] for row in range(size)] for col in range(size)]
        for line in lines:
            run = 1
            for index in range(1, size):  # одноцветные полосы от 5 модулей
                if line[index] == line[index - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += run - 2
                run = 1
            if run >= 5:
                score += run - 2
            text = "".join("1" if value else "0" for value in line)
            for pattern in ("10111010000", "00001011101"):  # похоже на поисковый узор
                start = text.find(pattern)
                while start >= 0:
                    score += 40
                    start = text.find(pattern, start + 1)
        for row in range(size - 1):  # одноцветные квадраты 2×2
            for col in range(size - 1):
                if dark[row][col] == dark[row][col + 1] == dark[row + 1][col] == dark[row + 1][col + 1]:
                    score += 3
        total = size * size
        dark_count = sum(sum(row) for row in dark)
        score += 10 * (abs(dark_count * 20 - total * 10) // total)  # перекос тёмных/светлых, шаг 5 %
        return score


_MASKS = (
    lambda row, col: (row + col) % 2 == 0,
    lambda row, col: row % 2 == 0,
    lambda row, col: col % 3 == 0,
    lambda row, col: (row + col) % 3 == 0,
    lambda row, col: (row // 2 + col // 3) % 2 == 0,
    lambda row, col: (row * col) % 2 + (row * col) % 3 == 0,
    lambda row, col: ((row * col) % 2 + (row * col) % 3) % 2 == 0,
    lambda row, col: ((row + col) % 2 + (row * col) % 3) % 2 == 0,
)


def encode(text: str) -> list[str]:
    """Строка -> QR-код: квадрат строк из «1» (тёмный модуль) и «0». Поля вокруг кода рисует страница.
    Длиннее ``MAX_BYTES`` байт в UTF-8 — ValueError."""
    payload = text.encode("utf-8")
    version = next((v for v in sorted(_VERSIONS) if len(payload) <= _capacity(v)), None)
    if version is None:
        raise ValueError(f"QR: строка длиннее {MAX_BYTES} байт")
    matrix = _Matrix(version)
    matrix.place(_codewords(payload, version))
    best_mask, best_score = 0, -1
    for mask in range(8):
        matrix.apply_mask(mask)
        score = matrix.penalty()
        if best_score < 0 or score < best_score:
            best_mask, best_score = mask, score
        matrix.apply_mask(mask)  # снять
    matrix.apply_mask(best_mask)
    return ["".join("1" if value else "0" for value in row) for row in matrix.dark]
