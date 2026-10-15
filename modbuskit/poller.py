"""
Background poller: reads a tag list on a fixed interval, keeps latest values,
a history ring buffer for trending, comm statistics, and an optional CSV log.
"""
from __future__ import annotations

import csv
import datetime as dt
import threading
import time
from collections import deque
from dataclasses import replace

from .client import ModbusClient, Status
from .tags import Block, Tag, plan_blocks


class Poller:
    def __init__(self, client: ModbusClient, tags: list[Tag], default_unit: int,
                 interval: float = 1.0, history: int = 36000, csv_path: str | None = None,
                 max_gap: int = 10):
        self.client = client
        self.tags = [replace(t) for t in tags]
        self.default_unit = default_unit
        self.interval = max(0.05, float(interval))
        self.blocks: list[Block] = plan_blocks(self.tags, default_unit, max_gap=max_gap)
        self.signature = tag_signature(tags, default_unit)
        self.history: deque[tuple[float, dict]] = deque(maxlen=history)
        self.latest: dict[str, dict] = {
            t.name: {"value": None, "text": "", "raw": None, "status": "-", "ts": None} for t in self.tags}
        self.stats = {"cycles": 0, "requests": 0, "ok": 0, "timeouts": 0, "exceptions": 0,
                      "garbled": 0, "errors": 0, "last_rtt_ms": 0.0, "last_cycle_ms": 0.0, "last_error": ""}
        self.csv_path = csv_path
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._csv = self._csv_writer = None

    # ---------- lifecycle ----------
    def start(self) -> None:
        if self.running:
            return
        if self.csv_path:
            self._csv = open(self.csv_path, "w", newline="")
            self._csv_writer = csv.writer(self._csv)
            self._csv_writer.writerow(["timestamp"] + [t.name for t in self.tags])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="modbus-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + self.client.timeout * (len(self.blocks) + 1) + 1)
        self._thread = None
        if self._csv:
            self._csv.close()
            self._csv = self._csv_writer = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            self.cycle()
            next_t += self.interval
            delay = next_t - time.monotonic()
            if delay < 0:                     # cycle took longer than the interval
                next_t = time.monotonic()
                delay = 0
            self._stop.wait(delay)

    # ---------- one scan ----------
    def cycle(self) -> None:
        t0 = time.monotonic()
        now = time.time()
        row: dict[str, float | None] = {}
        split: list[Block] = []
        for block in list(self.blocks):
            if self._stop.is_set():
                return
            r = self.client.read(block.unit_id, block.fc, block.start, block.count)
            with self._lock:
                s = self.stats
                s["requests"] += 1
                s["last_rtt_ms"] = r.rtt_ms
                key = {Status.OK: "ok", Status.TIMEOUT: "timeouts", Status.EXCEPTION: "exceptions",
                       Status.GARBLED: "garbled"}.get(r.status, "errors")
                s[key] += 1
                if r.ok:
                    for name, tv in block.decode(r.data).items():
                        self.latest[name] = {"value": tv.value, "text": tv.text, "raw": tv.raw,
                                             "status": "OK", "ts": now}
                        row[name] = tv.value
                else:
                    s["last_error"] = f"unit {block.unit_id} FC{block.fc:02d} @{block.start} x{block.count}: " \
                                      f"{r.status.value} {r.detail}"
                    for t in block.tags:          # keep last good value, flag it stale
                        self.latest[t.name]["status"] = r.status.value
                        row[t.name] = None
            # one bad address poisons a merged block -> fall back to per-tag reads for it
            if r.status is Status.EXCEPTION and r.exception_code in (2, 3) and len(block.tags) > 1:
                split.append(block)
        if split:
            for b in split:
                i = self.blocks.index(b)
                self.blocks[i:i + 1] = [Block(b.unit_id, b.fc, t.address, t.width, [t]) for t in b.tags]
        with self._lock:
            self.history.append((now, row))
            self.stats["cycles"] += 1
            self.stats["last_cycle_ms"] = (time.monotonic() - t0) * 1000
        if self._csv_writer:
            self._csv_writer.writerow([dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds")]
                                      + ["" if row.get(t.name) is None else row[t.name] for t in self.tags])
            self._csv.flush()

    # ---------- thread-safe views for the UI ----------
    def snapshot(self) -> tuple[dict, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self.latest.items()}, dict(self.stats)

    def history_rows(self, names: list[str], seconds: float | None = None) -> list[dict]:
        with self._lock:
            items = list(self.history)
        if seconds:
            cutoff = time.time() - seconds
            items = [it for it in items if it[0] >= cutoff]
        return [{"time": ts, **{n: row.get(n) for n in names}} for ts, row in items]


def tag_signature(tags: list[Tag], default_unit: int) -> str:
    return repr((default_unit, [(t.name, t.address, t.fc, t.dtype, t.order, t.scale, t.offset,
                                 t.length, t.unit_id, t.enum) for t in tags]))
