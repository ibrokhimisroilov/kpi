"""QR-код ссылки для входа (bot/webapp/qr.py): код читается обратно независимым разбором по стандарту.

Читатель в этом файле написан отдельно от кодировщика (другая сторона того же стандарта ISO/IEC 18004):
берёт биты формата, снимает маску, обходит модули «змейкой», собирает блоки, проверяет коды
Рида — Соломона (синдромы равны нулю) и достаёт строку. Настоящим сканером код проверен отдельно.
"""

from __future__ import annotations

import pytest

from bot.webapp import qr

LINK = "https://t.me/UzAuto_Administration_bot?start=login_AbCdEfGhIjKlMnOpQrStUv"
BLOCKS = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2), 5: (134, 24, 2), 6: (172, 16, 4)}
FORMAT_M = {  # таблица стандарта: уровень M, маска -> 15 бит формата
    0: "101010000010010", 1: "101000100100101", 2: "101111001111100", 3: "101101101001011",
    4: "100010111111001", 5: "100000011001110", 6: "100111110010111", 7: "100101010100000",
}
MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _gf_tables() -> tuple[list[int], list[int]]:
    exp, log = [0] * 510, [0] * 256
    value = 1
    for power in range(255):
        exp[power] = exp[power + 255] = value
        log[value] = power
        value = (value << 1) ^ (0x11D if value & 0x80 else 0)
    return exp, log


EXP, LOG = _gf_tables()


def _syndromes_are_zero(block: list[int], ec: int) -> bool:
    for power in range(ec):
        acc = 0
        for word in block:  # схема Горнера: значение многочлена блока в точке α^power
            acc = (0 if acc == 0 else EXP[LOG[acc] + power]) ^ word
        if acc:
            return False
    return True


def _is_function(row: int, col: int, size: int, version: int) -> bool:
    if row == 6 or col == 6:
        return True
    if (row <= 8 and col <= 8) or (row <= 8 and col >= size - 8) or (row >= size - 8 and col <= 8):
        return True
    if version >= 2:
        center = size - 7
        return abs(row - center) <= 2 and abs(col - center) <= 2
    return False


def read(rows: list[str]) -> str:
    size = len(rows)
    version = (size - 17) // 4
    total, ec, blocks = BLOCKS[version]
    dark = [[ch == "1" for ch in row] for row in rows]

    first = [dark[8][c] for c in (0, 1, 2, 3, 4, 5, 7, 8)] + [dark[r][8] for r in (7, 5, 4, 3, 2, 1, 0)]
    fmt = "".join("1" if bit else "0" for bit in first)
    second = [dark[r][8] for r in range(size - 1, size - 8, -1)] + [dark[8][c] for c in range(size - 8, size)]
    assert fmt == "".join("1" if bit else "0" for bit in second), "две копии битов формата различаются"
    mask = next(number for number, bits in FORMAT_M.items() if bits == fmt)
    assert dark[size - 8][8], "нет обязательного тёмного модуля"

    bits: list[int] = []
    col, upward = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for row in range(size - 1, -1, -1) if upward else range(size):
            for current in (col, col - 1):
                if not _is_function(row, current, size, version):
                    bits.append(int(dark[row][current] ^ MASKS[mask](row, current)))
        upward = not upward
        col -= 2
    words = [int("".join(map(str, bits[at : at + 8])), 2) for at in range(0, total * 8, 8)]
    assert len(bits) - total * 8 == (0 if version == 1 else 7), "число битов остатка"

    data_len = (total - ec * blocks) // blocks
    data_part, ec_part = words[: data_len * blocks], words[data_len * blocks :]
    data: list[int] = []
    for number in range(blocks):
        block = data_part[number::blocks] + ec_part[number::blocks]
        assert _syndromes_are_zero(block, ec), f"блок {number}: коды Рида — Соломона не сходятся"
        data += block[:data_len]
    stream = "".join(f"{word:08b}" for word in data)
    assert stream[:4] == "0100", "режим не байтовый"
    length = int(stream[4:12], 2)
    return bytes(int(stream[12 + 8 * i : 20 + 8 * i], 2) for i in range(length)).decode("utf-8")


def test_reed_solomon_matches_the_textbook_example() -> None:
    """«HELLO WORLD», версия 1-M: слова данных и слова коррекции из учебного разбора стандарта."""
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr._rs_remainder(data, 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


def test_login_link_reads_back() -> None:
    rows = qr.encode(LINK)
    assert len(rows) == 37 and all(len(row) == 37 and set(row) <= {"0", "1"} for row in rows)
    assert read(rows) == LINK


@pytest.mark.parametrize("length", [1, 14, 15, 26, 27, 42, 43, 62, 63, 84, 85, 106])
def test_every_supported_size_reads_back(length: int) -> None:
    """Границы версий 1–6: строка на байт длиннее вместимости уходит в следующую версию."""
    text = ("https://t.me/bot?start=login_" + "Zx9-_" * 30)[:length]
    rows = qr.encode(text)
    expected = next(v for v, (total, ec, blocks) in BLOCKS.items() if length <= total - ec * blocks - 2)
    assert len(rows) == 17 + 4 * expected
    assert read(rows) == text


def test_finder_patterns_and_timing_lines() -> None:
    rows = qr.encode(LINK)
    size = len(rows)
    finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert [rows[top + i][left : left + 7] for i in range(7)] == finder
    assert rows[7][:8] == "0" * 8 and rows[7][size - 8 :] == "0" * 8 and rows[size - 8][:8] == "0" * 8
    assert rows[6][8 : size - 8] == "".join("1" if c % 2 == 0 else "0" for c in range(8, size - 8))
    assert "".join(rows[r][6] for r in range(8, size - 8)) == "".join("1" if r % 2 == 0 else "0" for r in range(8, size - 8))


def test_non_latin_text_and_too_long_text() -> None:
    assert read(qr.encode("Вход: 47")) == "Вход: 47"
    assert qr.MAX_BYTES == 106
    with pytest.raises(ValueError):
        qr.encode("x" * 107)


def test_same_text_gives_same_code() -> None:
    assert qr.encode(LINK) == qr.encode(LINK)
