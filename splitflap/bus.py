"""I2C access to the Arduino boards, plus a simulator for development.

The real bus talks to /dev/i2c-N through the kernel's i2c-dev interface,
so no extra Python packages are needed on the Pi. The protocol is the one
the PHP version used with php-i2c-extension:

* write: one register byte (0, ignored by the Arduino) + one ASCII byte
  per module on that board
* read:  one status byte per module: 255 while turning, otherwise the
  flap position the module is resting on
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from typing import Dict, List, Sequence

I2C_SLAVE = 0x0703  # from <linux/i2c-dev.h>


class BusError(IOError):
    pass


class LinuxI2C:
    kind = "i2c"

    def __init__(self, bus: int = 1, register_byte: bool = True, retries: int = 2):
        self.path = "/dev/i2c-%d" % bus
        self.register_byte = register_byte
        self.retries = retries
        self._lock = threading.Lock()
        try:
            self._fd = os.open(self.path, os.O_RDWR)
        except OSError as e:
            raise BusError("cannot open %s: %s (is I2C enabled in raspi-config, "
                           "and is this user in the i2c group?)" % (self.path, e.strerror))

    def _io(self, address: int, fn):
        last = None
        for attempt in range(self.retries + 1):
            try:
                with self._lock:
                    fcntl.ioctl(self._fd, I2C_SLAVE, address)
                    return fn()
            except OSError as e:
                last = e
                time.sleep(0.005 * (attempt + 1))
        raise BusError("board 0x%02x not responding (%s)" % (address, last.strerror if last else "?"))

    def write(self, address: int, data: Sequence[int]) -> None:
        payload = bytes(([0] if self.register_byte else []) + list(data))
        self._io(address, lambda: os.write(self._fd, payload))

    def read(self, address: int, length: int) -> List[int]:
        return list(self._io(address, lambda: os.read(self._fd, length)))

    def scan(self) -> List[int]:
        found = []
        for address in range(0x08, 0x78):
            try:
                with self._lock:
                    fcntl.ioctl(self._fd, I2C_SLAVE, address)
                    os.read(self._fd, 1)
                found.append(address)
            except OSError:
                pass
        return found

    def close(self) -> None:
        os.close(self._fd)


class _SimModule:
    """One simulated carousel, turning at the real motor speed."""

    def __init__(self, flaps: int, flap_seconds: float):
        self.flaps = flaps
        self.flap_seconds = flap_seconds
        self.start_pos = 0  # status-style position (0..flaps-1)
        self.distance = 0
        self.started = 0.0

    def _progress(self, now: float) -> int:
        if self.distance == 0:
            return 0
        return min(self.distance, int((now - self.started) / self.flap_seconds))

    def status(self, now: float) -> int:
        if self._progress(now) < self.distance:
            return 255
        return (self.start_pos + self.distance) % self.flaps

    def go_to(self, position: int, now: float) -> None:
        cur = (self.start_pos + self._progress(now)) % self.flaps
        target = position % self.flaps
        distance = (target - cur) % self.flaps
        self.start_pos, self.started = cur, now
        self.distance = distance or self.flaps  # same flap -> full turn, like the firmware


class SimulatedI2C:
    """Pretends to be the Arduino boards, so the app can be tried on a laptop."""

    kind = "simulator"

    def __init__(self, boards: Dict[int, int], charset: str, flap_seconds: float):
        self.charset = charset
        self._lock = threading.Lock()
        self._boards = {
            address: [_SimModule(len(charset), flap_seconds) for _ in range(size)]
            for address, size in boards.items()
        }

    def _board(self, address: int):
        if address not in self._boards:
            raise BusError("board 0x%02x not responding (simulator)" % address)
        return self._boards[address]

    def write(self, address: int, data: Sequence[int]) -> None:
        now = time.monotonic()
        with self._lock:
            for module, byte in zip(self._board(address), data):
                ch = chr(byte).upper()
                if ch in self.charset:
                    module.go_to(self.charset.index(ch) + 1, now)

    def read(self, address: int, length: int) -> List[int]:
        now = time.monotonic()
        with self._lock:
            return [m.status(now) for m in self._board(address)[:length]]

    def scan(self) -> List[int]:
        return sorted(self._boards)

    def close(self) -> None:
        pass
