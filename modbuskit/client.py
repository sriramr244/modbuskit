"""
Modbus master. One class covers all three link types:

    Modbus RTU (serial)   SerialTransport + framing="rtu"
    Modbus RTU over TCP   TcpTransport    + framing="rtu"
    Modbus TCP/IP         TcpTransport    + framing="tcp"

Every call returns a Response with a diagnostic Status instead of raising,
so scanners and pollers can treat "no reply" / "garbled" as data.
"""
from __future__ import annotations

import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import framing as f
from .transport import Transport


class Status(str, Enum):
    OK = "OK"
    EXCEPTION = "EXCEPTION"      # device answered with a Modbus exception
    TIMEOUT = "TIMEOUT"          # silence
    GARBLED = "GARBLED"          # bytes came back, CRC/format bad
    OTHER_UNIT = "OTHER_UNIT"    # valid frame, different unit ID
    ERROR = "ERROR"              # port/socket error, bad request, not connected


DIAG = {
    Status.TIMEOUT: "no reply: wrong baud/unit ID, wiring/polarity, or device off",
    Status.GARBLED: "bytes came back but CRC/format bad: often right baud, wrong parity/framing",
    Status.OTHER_UNIT: "valid reply from a different unit ID (shared bus or gateway remap)",
}


@dataclass
class Response:
    status: Status
    data: Any = None
    exception_code: int | None = None
    detail: str = ""
    tx: bytes = b""
    rx: bytes = b""
    rtt_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    @property
    def alive(self) -> bool:
        """A device is there at these settings (it answered, even if with an exception)."""
        return self.status in (Status.OK, Status.EXCEPTION)


@dataclass
class TrafficEntry:
    ts: float
    direction: str   # TX / RX / ERR
    data: bytes
    note: str = ""


class ModbusClient:
    def __init__(self, transport: Transport, framing: str = "rtu", timeout: float = 1.0,
                 retries: int = 0, traffic_size: int = 2000):
        if framing not in ("rtu", "tcp"):
            raise ValueError("framing must be 'rtu' or 'tcp'")
        self.transport, self.framing = transport, framing
        self.timeout, self.retries = float(timeout), int(retries)
        self.lock = threading.RLock()            # one transaction on the wire at a time
        self.traffic: deque[TrafficEntry] = deque(maxlen=traffic_size)
        self.log_traffic = True
        self._tid = 0
        self._want_open = False

    # ---------- connection ----------
    def connect(self) -> None:
        with self.lock:
            self._want_open = True
            if not self.transport.is_open:
                self.transport.open()

    def close(self) -> None:
        with self.lock:
            self._want_open = False
            self.transport.close()

    @property
    def connected(self) -> bool:
        return self.transport.is_open

    def describe(self) -> str:
        kind = {"rtu": "RTU", "tcp": "TCP"}[self.framing]
        return f"{kind} @ {self.transport.describe()}"

    # ---------- core transaction ----------
    def _log(self, direction: str, data: bytes, note: str = "") -> None:
        if self.log_traffic:
            self.traffic.append(TrafficEntry(time.time(), direction, bytes(data), note))

    def request(self, unit: int, pdu: bytes) -> Response:
        with self.lock:
            resp = Response(Status.ERROR, detail="not attempted")
            for _ in range(self.retries + 1):
                try:
                    if not self.transport.is_open:
                        if not self._want_open:
                            return Response(Status.ERROR, detail="not connected")
                        self.transport.open()       # auto-reconnect after a dropped socket
                    resp = self._transact(unit, pdu)
                except (OSError, ConnectionError) as e:   # SerialException is an OSError
                    self.transport.close()
                    resp = Response(Status.ERROR, detail=str(e))
                    self._log("ERR", b"", str(e))
                if resp.status not in (Status.TIMEOUT, Status.GARBLED, Status.ERROR):
                    break
            return resp

    def _transact(self, unit: int, pdu: bytes) -> Response:
        if self.framing == "tcp":
            self._tid = (self._tid + 1) & 0xFFFF
            adu = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, unit) + pdu
        else:
            head = bytes([unit]) + pdu
            adu = head + f.crc16(head)

        self.transport.flush_input()
        if self.transport.pre_tx_delay:
            time.sleep(self.transport.pre_tx_delay)
        t0 = time.monotonic()
        self.transport.write(adu)
        self._log("TX", adu)

        if self.framing == "rtu" and unit == 0:          # RTU broadcast: no reply by design
            time.sleep(0.1)
            return Response(Status.OK, data=True, detail="broadcast (no reply expected)", tx=adu)

        if self.framing == "tcp":
            status, rx_unit, rx_pdu, raw = self._recv_tcp()
        else:
            status, rx_unit, rx_pdu, raw = self._recv_rtu(adu)
        rtt = (time.monotonic() - t0) * 1000

        def done(st: Status, **kw) -> Response:
            if raw:
                self._log("RX", raw, "" if st is Status.OK else st.value)
            kw.setdefault("detail", DIAG.get(st, ""))
            return Response(st, tx=adu, rx=raw, rtt_ms=rtt, **kw)

        if status is not None:
            if status is Status.TIMEOUT:
                self._log("ERR", b"", "timeout")
            return done(status)
        if rx_unit != unit:
            return done(Status.OTHER_UNIT, data=rx_unit, detail=f"reply from unit {rx_unit}")
        try:
            data = f.parse_response(pdu, rx_pdu)
        except f.ModbusException as e:
            return done(Status.EXCEPTION, exception_code=e.code, detail=str(e))
        except f.FrameError as e:
            return done(Status.GARBLED, detail=str(e))
        return done(Status.OK, data=data)

    def _recv_rtu(self, adu: bytes):
        t = self.transport
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        last_rx = None
        echo_pending = t.strip_echo
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            chunk = t.read(256)
            now = time.monotonic()
            if chunk:
                buf += chunk
                last_rx = now
                if echo_pending:
                    if buf.startswith(adu):
                        del buf[:len(adu)]
                        echo_pending = False
                        if not buf:
                            last_rx = None
                        continue
                    if adu.startswith(bytes(buf)):
                        continue                          # partial echo, keep reading
                    echo_pending = False                  # not an echo after all
                need = f.rtu_frame_length(buf)
                if need is not None and len(buf) >= need:
                    break
            elif last_rx is not None and now - last_rx > t.gap_time:
                break
        if not buf:
            return Status.TIMEOUT, None, None, b""
        raw = bytes(buf)
        need = f.rtu_frame_length(raw) or len(raw)
        frame = raw[:need]
        if len(frame) < 4 or f.crc16(frame[:-2]) != frame[-2:]:
            return Status.GARBLED, None, None, raw
        return None, frame[0], frame[1:-2], raw

    def _recv_tcp(self):
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = self.transport.read(1024)
            if not chunk:
                continue
            buf += chunk
            while len(buf) >= 7:
                tid, pid, length = struct.unpack(">HHH", buf[:6])
                if pid != 0 or not 2 <= length <= 254:
                    return Status.GARBLED, None, None, bytes(buf)
                total = 6 + length
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                del buf[:total]
                if tid != self._tid:
                    self._log("RX", frame, "stale transaction id, ignored")
                    continue
                return None, frame[6], frame[7:], frame
        return (Status.GARBLED if buf else Status.TIMEOUT), None, None, bytes(buf)

    # ---------- convenience API ----------
    def _safe(self, unit: int, build, *args) -> Response:
        try:
            pdu = build(*args)
        except ValueError as e:
            return Response(Status.ERROR, detail=str(e))
        return self.request(unit, pdu)

    def read(self, unit: int, fc: int, address: int, count: int) -> Response:
        return self._safe(unit, f.pdu_read, fc, address, count)

    def read_coils(self, unit, address, count):            return self.read(unit, 1, address, count)
    def read_discrete_inputs(self, unit, address, count):  return self.read(unit, 2, address, count)
    def read_holding(self, unit, address, count):          return self.read(unit, 3, address, count)
    def read_input(self, unit, address, count):            return self.read(unit, 4, address, count)
    def write_coil(self, unit, address, value):            return self._safe(unit, f.pdu_write_coil, address, value)
    def write_register(self, unit, address, value):        return self._safe(unit, f.pdu_write_register, address, value)
    def write_coils(self, unit, address, values):          return self._safe(unit, f.pdu_write_coils, address, values)
    def write_registers(self, unit, address, values):      return self._safe(unit, f.pdu_write_registers, address, values)

    def read_device_id(self, unit: int, read_code: int = 1) -> Response:
        """FC43/14. Follows 'more follows' across several requests."""
        objects, obj, last = {}, 0, None
        for _ in range(8):
            r = self.request(unit, f.pdu_read_device_id(read_code, obj))
            last = r
            if not r.ok:
                if objects:
                    break
                return r
            objects.update(r.data["objects"])
            if not r.data["more"]:
                break
            obj = r.data["next_id"]
        return Response(Status.OK, data=objects, tx=last.tx, rx=last.rx, rtt_ms=last.rtt_ms)
