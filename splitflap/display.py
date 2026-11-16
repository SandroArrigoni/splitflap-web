"""Core split-flap logic.

Python port of g000ze's ``Raspberry_Pi/split-flap.php``: text sanitising,
mapping text onto the module grid, the stagger animations and the timed
I2C send loop. No web code in here, so the CLI, the web app and future
scripts (flights overhead, now playing, ...) can all reuse it.
"""
from __future__ import annotations

import random
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Flap order on the carousel. A character's flap position is its index + 1,
# exactly like ``char_positions[]`` in Split_Flap.ino. Keep both in sync.
DEFAULT_CHARSET = "0ABCD1EFGH2IJKL3MNOP4QRST5UVWX6YZ+-7?!.,8@:;#9*$/ "

# Sent to modules that should keep their current flap. The Arduino ignores
# any character that is not on the carousel.
PLACEHOLDER = "~"

# Status byte the Arduino returns while a module is still turning.
MOVING = 255

ANIMATIONS = {
    "start": "All at once",
    "stop": "Land together",
    "left": "From left",
    "right": "From right",
    "random": "Random",
    "top": "From top",
    "bottom": "From bottom",
    "diagonal": "Diagonal",
    "cross": "Cross",
    "round": "Round",
    "snake1": "Snake 1",
    "snake2": "Snake 2",
    "snake3": "Snake 3",
    "snake4": "Snake 4",
}
# Animations that only differ from "start"/"left"/"right" on a multi-row grid.
MULTI_ROW_ANIMATIONS = {"top", "bottom", "diagonal", "cross", "round",
                        "snake1", "snake2", "snake3", "snake4"}

# What iconv //TRANSLIT did for the PHP version, for the common cases that
# NFKD decomposition does not already handle.
TRANSLIT = {
    "ß": "SS", "ẞ": "SS", "æ": "AE", "Æ": "AE", "œ": "OE", "Œ": "OE",
    "ø": "O", "Ø": "O", "ł": "L", "Ł": "L", "đ": "D", "Đ": "D",
    "þ": "TH", "Þ": "TH", "–": "-", "—": "-", "‐": "-", "‑": "-",
    "−": "-", "·": ".", "€": "EUR", "×": "*",
}


class Charset:
    def __init__(self, chars: str = DEFAULT_CHARSET):
        if len(set(chars)) != len(chars):
            raise ValueError("charset contains duplicate characters")
        if PLACEHOLDER in chars:
            raise ValueError("charset must not contain the placeholder '~'")
        self.chars = chars
        self.flaps = len(chars)
        self._pos = {c: i + 1 for i, c in enumerate(chars)}

    def position(self, ch: str) -> Optional[int]:
        """1-based flap position of a character, None if not on the carousel."""
        return self._pos.get(ch)

    def char_for_status(self, status: int) -> Optional[str]:
        """Character shown for a status byte read back from the Arduino."""
        if status is None or status >= self.flaps:
            return None
        # status 0 is the last flap (position == flaps), like PHP's 0 -> 50
        return self.chars[status - 1]


@dataclass
class Board:
    address: int
    size: int
    row: int
    first_col: int


@dataclass
class Module:
    row: int
    col: int
    board: Board
    index: int  # module index on its board


class Geometry:
    """The physical grid: rows of Arduino boards, each driving N modules."""

    def __init__(self, layout: Sequence[Sequence[dict]]):
        self.boards: List[Board] = []
        self.modules: List[Module] = []
        cols = None
        for r, row in enumerate(layout):
            c = 0
            for spec in row:
                board = Board(int(str(spec["address"]), 0), int(spec["modules"]), r, c)
                self.boards.append(board)
                for i in range(board.size):
                    self.modules.append(Module(r, c, board, i))
                    c += 1
            if cols is None:
                cols = c
            elif c != cols:
                raise ValueError("every row needs the same number of modules")
        if not self.modules:
            raise ValueError("layout has no modules")
        self.rows = len(layout)
        self.cols = cols or 0

    @property
    def count(self) -> int:
        return len(self.modules)


def sanitize(text: str, length: int, charset: Charset, wipe: bool = False) -> List[Optional[str]]:
    """Turn free text into one target per module (None = leave module alone).

    Same steps as sanitize_string() in the PHP version: Unicode NFKD,
    whitespace to spaces, strip accents, transliterate, truncate, uppercase,
    optionally pad with spaces ("wipe"), unsupported characters -> keep flap.
    """
    s = unicodedata.normalize("NFKD", text or "")
    s = re.sub(r"[\r\n\t\v\f]", " ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = "".join(TRANSLIT.get(ch, ch) for ch in s)
    s = s.upper()[:length]
    if wipe:
        s = s.ljust(length)
    out: List[Optional[str]] = [ch if charset.position(ch) else None for ch in s]
    out += [None] * (length - len(out))
    return out


def compose(geometry: Geometry, charset: Charset, text: Optional[str] = None,
            lines: Optional[Sequence[str]] = None, wipe: bool = False) -> List[Optional[str]]:
    """Targets for the whole grid, either from flat text or one string per row."""
    if lines is None:
        return sanitize(text or "", geometry.count, charset, wipe)
    targets: List[Optional[str]] = []
    for r in range(geometry.rows):
        line = lines[r] if r < len(lines) else ""
        targets += sanitize(line, geometry.cols, charset, wipe)
    return targets


def flaps_to_walk(status: Optional[int], new_pos: Optional[int], flaps: int, same: bool) -> int:
    """How many flap positions a module turns (port of pos_to_walk())."""
    if new_pos is None:
        return 0
    if status is None or status >= MOVING:
        return flaps  # unknown / still moving: assume a long way
    cur = status if status else flaps
    if new_pos == cur:
        return 0 if same else flaps
    if new_pos > cur:
        return new_pos - cur
    return flaps - cur + new_pos


def stagger(animation: str, walks: Sequence[int], rows: int, cols: int) -> List[int]:
    """Per-module start timer for each animation (port of set_delay()).

    Higher timer = starts earlier; one timer step = the time of one flap.
    """
    n = rows * cols
    timers = [0] * n
    timer = 0
    counter = 0
    shuffled = list(range(n))
    random.shuffle(shuffled)
    for row in range(rows):
        for col in range(cols):
            i = row * cols + col
            if animation == "start":
                timer = 0
            elif animation == "stop":
                timer = walks[i]
            elif animation == "left":
                timer = cols if col == 0 else timer - 1
            elif animation == "right":
                timer = 0 if col == 0 else timer + 1
            elif animation == "top":
                timer = (rows - 1) - row
            elif animation == "bottom":
                timer = row
            elif animation == "random":
                timer = shuffled[counter]
            elif animation == "snake1":
                timer = (n - 1) - (((row + 1) * cols - col - 1) if row % 2 == 0 else counter)
            elif animation == "snake2":
                timer = (n - 1) - (((row + 1) * cols - col - 1) if row % 2 == 1 else counter)
            elif animation == "snake3":
                timer = ((row + 1) * cols - col - 1) if row % 2 == 0 else counter
            elif animation == "snake4":
                timer = ((row + 1) * cols - col - 1) if row % 2 == 1 else counter
            elif animation == "cross":
                timer = timer + 1 if row % 2 == 0 else timer - 1
            elif animation == "round":
                if row == 0:
                    timer += 1
                elif row == rows - 1:
                    timer = (cols - 1) + (rows - 1) + (cols - col)
                elif col == 0:
                    timer = (cols - 1) + (rows - 1) + (cols + row)
                elif col == cols - 1:
                    timer = cols + row
            elif animation == "diagonal":
                timer = row * 2 + col
            timers[i] = timer
            counter += 1
    return timers


# timer -> board address -> module index -> character
Schedule = Dict[int, Dict[int, Dict[int, str]]]


def plan(geometry: Geometry, charset: Charset, targets: Sequence[Optional[str]],
         statuses: Optional[Sequence[Optional[int]]], animation: str = "start",
         same: bool = False) -> Schedule:
    """Work out which module gets which character at which timer step."""
    if animation not in ANIMATIONS:
        raise ValueError("unknown animation: %s" % animation)
    statuses = statuses or [None] * geometry.count
    walks = [
        flaps_to_walk(statuses[i], charset.position(t) if t else None, charset.flaps, same)
        for i, t in enumerate(targets)
    ]
    timers = stagger(animation, walks, geometry.rows, geometry.cols)
    schedule: Schedule = {}
    for i, module in enumerate(geometry.modules):
        if walks[i] > 0:
            slot = schedule.setdefault(timers[i], {}).setdefault(module.board.address, {})
            slot[module.index] = targets[i]
    return schedule


def run_schedule(bus, geometry: Geometry, schedule: Schedule, flap_seconds: float,
                 slowdown: int = 1, cancel: Optional[threading.Event] = None) -> bool:
    """Send the schedule over I2C with the right pauses (port of run_carrousel()).

    Returns False if it was cancelled before every module got its character.
    """
    cancel = cancel or threading.Event()
    sizes = {b.address: b.size for b in geometry.boards}
    prev = None
    for t in sorted(schedule, reverse=True):
        if prev is not None:
            if cancel.wait((prev - t) * flap_seconds * slowdown):
                return False
        elif cancel.is_set():
            return False
        prev = t
        for address, mods in schedule[t].items():
            data = [ord(mods.get(i, PLACEHOLDER)) for i in range(sizes[address])]
            bus.write(address, data)
    return True
