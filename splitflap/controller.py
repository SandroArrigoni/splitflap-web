"""Display controller: owns the bus, runs one display job at a time,
keeps presets/history and the clock mode. Used by the web server and CLI."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import List, Optional, Sequence

from . import display
from .bus import BusError, LinuxI2C, SimulatedI2C

log = logging.getLogger("splitflap")

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8080,
    "i2c_bus": 1,
    "simulate": "auto",
    "layout": [[{"address": "0x0a", "modules": 6}]],
    "charset": display.DEFAULT_CHARSET,
    "motor": {"microsteps": 16, "motorsteps": 200, "speed_us": 1800},
    "clock": {"format": "%H:%M", "animation": "right"},
}


def load_config(path: Optional[str]) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for key, value in user.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


class Controller:
    def __init__(self, config: dict, data_dir: str, simulate: Optional[bool] = None):
        self.config = config
        self.geo = display.Geometry(config["layout"])
        self.charset = display.Charset(config["charset"])
        motor = config["motor"]
        steps_per_flap = motor["motorsteps"] * motor["microsteps"] / self.charset.flaps
        self.flap_seconds = steps_per_flap * motor["speed_us"] / 1e6

        self.bus = None
        self.bus_error: Optional[str] = None
        self._open_bus(simulate)

        self.targets: List[Optional[str]] = [None] * self.geo.count
        self.last_error: Optional[str] = None
        self._job: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._job_lock = threading.Lock()
        self._status_cache = (0.0, None, None)
        self._status_lock = threading.Lock()

        self.mode = "text"
        self.clock_format = config["clock"]["format"]
        self._mode_cancel = threading.Event()

        self.history: deque = deque(maxlen=12)
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.presets: List[str] = self._load_presets()

    # ----- hardware ---------------------------------------------------------

    def _open_bus(self, simulate: Optional[bool]) -> None:
        if simulate is None:
            setting = str(self.config.get("simulate", "auto")).lower()
            if setting == "auto":
                simulate = not os.path.exists("/dev/i2c-%d" % self.config["i2c_bus"])
            else:
                simulate = setting in ("1", "true", "yes", "on")
        if simulate:
            boards = {b.address: b.size for b in self.geo.boards}
            self.bus = SimulatedI2C(boards, self.charset.chars, self.flap_seconds)
            log.info("using the simulator (no real I2C bus)")
            return
        try:
            self.bus = LinuxI2C(self.config["i2c_bus"])
            log.info("using %s", self.bus.path)
        except BusError as e:
            self.bus_error = str(e)
            log.error("%s", e)

    @property
    def hardware_kind(self) -> str:
        return self.bus.kind if self.bus else "offline"

    def read_statuses(self, max_age: float = 0.15):
        """Status byte per module (None where unreadable) and an error text."""
        with self._status_lock:
            stamp, statuses, error = self._status_cache
            if statuses is not None and time.monotonic() - stamp < max_age:
                return statuses, error
            if not self.bus:
                return [None] * self.geo.count, self.bus_error
            statuses, errors = [], []
            for board in self.geo.boards:
                try:
                    statuses += self.bus.read(board.address, board.size)
                except BusError as e:
                    statuses += [None] * board.size
                    errors.append(str(e))
            error = "; ".join(errors) or None
            self._status_cache = (time.monotonic(), statuses, error)
            return statuses, error

    def scan(self) -> dict:
        if not self.bus:
            return {"ok": False, "error": self.bus_error, "found": []}
        found = self.bus.scan()
        expected = [b.address for b in self.geo.boards]
        return {
            "ok": True,
            "found": ["0x%02x" % a for a in found],
            "expected": ["0x%02x" % a for a in expected],
            "missing": ["0x%02x" % a for a in expected if a not in found],
        }

    # ----- showing things ---------------------------------------------------

    def show(self, text: Optional[str] = None, lines: Optional[Sequence[str]] = None,
             animation: str = "start", same: bool = False, wipe: bool = False,
             slowdown: int = 1, source: str = "user") -> str:
        if animation not in display.ANIMATIONS:
            raise ValueError("unknown animation: %s" % animation)
        slowdown = max(1, min(10, int(slowdown)))
        if source == "user":
            self.stop_mode()
        targets = display.compose(self.geo, self.charset, text=text, lines=lines, wipe=wipe)
        self._start_job(targets, animation, same, slowdown)
        shown = "".join(t or display.PLACEHOLDER for t in targets)
        if source == "user":
            label = " / ".join(lines) if lines is not None else (text or "")
            if label.strip():
                if label in self.history:
                    self.history.remove(label)
                self.history.appendleft(label)
        return shown

    def show_targets(self, targets: List[Optional[str]], animation: str, same: bool, slowdown: int = 1):
        self._start_job(targets, animation, same, slowdown)

    def clear(self, animation: str = "start", slowdown: int = 1) -> None:
        self.show(text="", wipe=True, animation=animation, slowdown=slowdown, same=True)

    def resync(self) -> None:
        """Every module does one full turn to its current character, so the
        Hall sensors re-home all carousels."""
        self.stop_mode()
        statuses, _ = self.read_statuses(max_age=0)
        targets = []
        for i, status in enumerate(statuses):
            targets.append(self.targets[i] or self.charset.char_for_status(status) or " ")
        self._start_job(targets, "start", False, 1)

    def stop(self) -> None:
        self.stop_mode()
        with self._job_lock:
            self._cancel.set()

    def _start_job(self, targets, animation, same, slowdown) -> None:
        with self._job_lock:
            self._cancel.set()
            if self._job and self._job.is_alive():
                self._job.join(timeout=3)
            self._cancel = threading.Event()
            for i, t in enumerate(targets):
                if t is not None:
                    self.targets[i] = t
            self._job = threading.Thread(
                target=self._run_job, args=(list(targets), animation, same, slowdown, self._cancel),
                daemon=True)
            self._job.start()

    def _run_job(self, targets, animation, same, slowdown, cancel) -> None:
        try:
            if not self.bus:
                raise BusError(self.bus_error or "no I2C bus")
            statuses, _ = self.read_statuses(max_age=0)
            schedule = display.plan(self.geo, self.charset, targets, statuses, animation, same)
            display.run_schedule(self.bus, self.geo, schedule, self.flap_seconds, slowdown, cancel)
            self.last_error = None
        except Exception as e:  # report to the UI instead of dying silently
            self.last_error = str(e)
            log.error("display job failed: %s", e)
        with self._status_lock:  # force a fresh read on the next poll
            self._status_cache = (0.0, None, None)

    @property
    def busy(self) -> bool:
        return bool(self._job and self._job.is_alive())

    def wait(self, timeout: float = 30) -> None:
        """Block until the job is sent and every module stopped turning."""
        end = time.monotonic() + timeout
        if self._job:
            self._job.join(timeout)
        while time.monotonic() < end:
            statuses, _ = self.read_statuses(max_age=0)
            if not any(s is not None and s >= display.MOVING for s in statuses):
                return
            time.sleep(self.flap_seconds)

    # ----- clock mode -------------------------------------------------------

    def set_mode(self, mode: str, fmt: Optional[str] = None) -> None:
        if mode not in ("text", "clock"):
            raise ValueError("unknown mode: %s" % mode)
        self.stop_mode()
        if mode == "clock":
            if fmt:
                self.clock_format = fmt
            self.mode = "clock"
            self._mode_cancel = threading.Event()
            threading.Thread(target=self._clock_loop, args=(self._mode_cancel,), daemon=True).start()

    def stop_mode(self) -> None:
        self._mode_cancel.set()
        self.mode = "text"

    def _clock_loop(self, cancel: threading.Event) -> None:
        last = None
        animation = self.config["clock"].get("animation", "right")
        while not cancel.is_set():
            now = time.strftime(self.clock_format)
            if now != last:
                line = now[:self.geo.cols].center(self.geo.cols)
                self.show(lines=[line], animation=animation, same=True, wipe=True, source="clock")
                last = now
            cancel.wait(1.0)

    # ----- presets ----------------------------------------------------------

    def _presets_path(self) -> str:
        return os.path.join(self.data_dir, "presets.json")

    def _load_presets(self) -> List[str]:
        try:
            with open(self._presets_path(), encoding="utf-8") as f:
                return [str(p) for p in json.load(f)]
        except (OSError, ValueError):
            return ["HELLO", "HALLO", "ZURICH"]

    def _save_presets(self) -> None:
        tmp = self._presets_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.presets, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._presets_path())

    def add_preset(self, text: str) -> None:
        text = (text or "").strip("\n")
        if text.strip() and text not in self.presets:
            self.presets.append(text)
            self._save_presets()

    def remove_preset(self, text: str) -> None:
        if text in self.presets:
            self.presets.remove(text)
            self._save_presets()

    # ----- state for the UI -------------------------------------------------

    def state(self) -> dict:
        statuses, read_error = self.read_statuses()
        modules = []
        for i, status in enumerate(statuses):
            modules.append({
                "char": self.charset.char_for_status(status),
                "moving": status is not None and status >= display.MOVING,
                "target": self.targets[i],
                "ok": status is not None,
            })
        boards = []
        for b in self.geo.boards:
            ok = all(m["ok"] for m, mod in zip(modules, self.geo.modules) if mod.board is b)
            boards.append({"address": "0x%02x" % b.address, "modules": b.size, "row": b.row, "ok": ok})
        return {
            "rows": self.geo.rows,
            "cols": self.geo.cols,
            "charset": self.charset.chars,
            "flap_ms": round(self.flap_seconds * 1000, 1),
            "modules": modules,
            "busy": self.busy,
            "mode": self.mode,
            "clock_format": self.clock_format,
            "hardware": {
                "kind": self.hardware_kind,
                "ok": self.bus is not None and read_error is None,
                "error": self.bus_error or read_error,
                "boards": boards,
            },
            "last_error": self.last_error,
            "animations": [
                {"id": k, "label": v, "multi_row": k in display.MULTI_ROW_ANIMATIONS}
                for k, v in display.ANIMATIONS.items()
            ],
            "presets": self.presets,
            "history": list(self.history),
        }
