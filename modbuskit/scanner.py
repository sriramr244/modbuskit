"""
Discovery tools. All read-only. Each takes optional callbacks:
    progress(done, total, message)    cancel() -> bool
"""
from __future__ import annotations

import ipaddress
import itertools
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed

from .client import ModbusClient, Status
from .framing import BIT_FCS
from .transport import SerialTransport, TcpTransport

COMMON_BAUDS = [19200, 9600, 38400, 57600, 115200, 4800, 2400, 1200]


class ScanCancelled(Exception):
    pass


def _tick(progress, done, total, msg=""):
    if progress:
        progress(done, total, msg)


def _check(cancel):
    if cancel and cancel():
        raise ScanCancelled()


# ---------- 1. serial line settings ----------

def sweep_serial(port: str, bauds, parities, stopbits=(1,), units=(1,), bytesize: int = 8,
                 probe_fc: int = 3, probe_addr: int = 0, probe_count: int = 1, timeout: float = 0.3,
                 strip_echo: bool = False, stop_on_first: bool = True, progress=None, cancel=None):
    """Try every baud x parity x stop-bits x unit combination with one read.
    Returns (hits, garbled_settings, log). A hit = OK or EXCEPTION (device answered)."""
    combos = list(itertools.product(bauds, parities, stopbits))
    total = len(combos) * len(units)
    hits, garbled, log, done = [], set(), [], 0
    for baud, parity, sb in combos:
        client = ModbusClient(SerialTransport(port, baud, parity, bytesize, sb, strip_echo), "rtu", timeout)
        client.log_traffic = False
        client.connect()
        try:
            for unit in units:
                _check(cancel)
                r = client.read(unit, probe_fc, probe_addr, probe_count)
                done += 1
                setting = f"{baud} {bytesize}{parity}{sb}"
                _tick(progress, done, total, f"{setting} unit {unit}")
                if r.status is Status.ERROR:
                    raise OSError(r.detail)
                if r.status is Status.TIMEOUT:
                    continue
                entry = {"baud": baud, "parity": parity, "stopbits": sb, "bytesize": bytesize,
                         "unit": unit, "status": r.status.value, "detail": r.detail,
                         "data": r.data, "rx_hex": r.rx.hex(" ")}
                log.append(entry)
                if r.status is Status.GARBLED:
                    garbled.add(setting)
                if r.alive:
                    hits.append(entry)
                    if stop_on_first:
                        return hits, garbled, log
        finally:
            client.close()
    return hits, garbled, log


# ---------- 2. unit IDs on an open connection ----------

def scan_units(client: ModbusClient, units, probe_fc: int = 3, probe_addr: int = 0, probe_count: int = 1,
               timeout: float | None = None, progress=None, cancel=None) -> list[dict]:
    units = list(units)
    found = []
    old = client.timeout
    if timeout:
        client.timeout = timeout
    try:
        for i, u in enumerate(units, 1):
            _check(cancel)
            r = client.read(u, probe_fc, probe_addr, probe_count)
            _tick(progress, i, len(units), f"unit {u}")
            # gateways answer exception 0x0B for absent units: that is "not there"
            if r.status is Status.EXCEPTION and r.exception_code in (10, 11):
                continue
            if r.status not in (Status.TIMEOUT, Status.ERROR):
                found.append({"unit": u, "status": r.status.value, "detail": r.detail,
                              "data": r.data, "rtt_ms": round(r.rtt_ms, 1)})
    finally:
        client.timeout = old
    return found


# ---------- 3. register map discovery ----------

def scan_registers(client: ModbusClient, unit: int, fc: int, start: int, end: int, chunk: int | None = None,
                   resolution: int = 1, progress=None, cancel=None):
    """Read [start, end] in chunks; when a chunk is refused, bisect to find the readable addresses.
    resolution: stop bisecting below this size (1 = exact map; 8-16 = much faster over big holes,
    at the cost of missing isolated registers).
    Returns (values {addr: value}, ranges [(first, last)], request_count)."""
    chunk = chunk or (100 if fc not in BIT_FCS else 1000)
    values: dict[int, object] = {}
    requests = 0

    def probe(a: int, n: int):
        nonlocal requests
        _check(cancel)
        r = client.read(unit, fc, a, n)
        requests += 1
        if r.ok:
            for i, v in enumerate(r.data):
                values[a + i] = v
        elif n > max(1, resolution) and r.status in (Status.EXCEPTION, Status.TIMEOUT):
            h = n // 2
            probe(a, h)
            probe(a + h, n - h)

    total = end - start + 1
    a = start
    while a <= end:
        n = min(chunk, end - a + 1)
        probe(a, n)
        a += n
        _tick(progress, a - start, total, f"address {a} ({len(values)} readable, {requests} requests)")
    return values, compress_ranges(sorted(values)), requests


def compress_ranges(addrs: list[int]) -> list[tuple[int, int]]:
    out = []
    for a in addrs:
        if out and a == out[-1][1] + 1:
            out[-1] = (out[-1][0], a)
        else:
            out.append((a, a))
    return out


# ---------- 4. IP subnet: who has port 502 open ----------

def scan_ip(cidr: str, port: int = 502, timeout: float = 0.3, probe: bool = True, probe_unit: int = 1,
            workers: int = 64, progress=None) -> list[dict]:
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts()) or [net.network_address]
    if len(hosts) > 4096:
        raise ValueError("subnet too large (max 4096 hosts) - use /20 or smaller")

    def check(ip):
        ip = str(ip)
        try:
            socket.create_connection((ip, port), timeout=timeout).close()
        except OSError:
            return None
        res = {"host": ip, "port": port, "probe": "", "device_id": ""}
        if probe:
            c = ModbusClient(TcpTransport(ip, port, max(timeout, 1.0)), "tcp", timeout=max(timeout, 0.5))
            c.log_traffic = False
            try:
                c.connect()
                r = c.read(probe_unit, 3, 0, 1)
                res["probe"] = f"{r.status.value} {r.detail}".strip()
                d = c.read_device_id(probe_unit)
                if d.ok:
                    res["device_id"] = "; ".join(f"{k}={v}" for k, v in d.data.items())
            except OSError as e:
                res["probe"] = f"ERROR {e}"
            finally:
                c.close()
        return res

    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(check, h) for h in hosts]
        for i, fut in enumerate(as_completed(futs), 1):
            _tick(progress, i, len(hosts), f"{len(out)} found")
            if (res := fut.result()) is not None:
                out.append(res)
    return sorted(out, key=lambda r: ipaddress.ip_address(r["host"]))
