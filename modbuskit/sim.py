"""
Bench simulator (a minimal "Modbus Slave") so you can test the tool without hardware.

    python -m modbuskit.sim --tcp 5020                      # Modbus TCP
    python -m modbuskit.sim --tcp 5021 --framing rtu        # RTU over TCP (like a Lantronix raw port)
    python -m modbuskit.sim --serial COM7 --baud 19200 --parity E --units 254   # with a null-modem / com0com pair

Valid addresses: 0..999 and 0x1000..0x10FF (anything else -> exception 02),
so the register-map scan has something to find.
  HR 100      int16   sine * 1000
  HR 101-102  float32 25 + 5*sin (ABCD)
  HR 103-104  float32 same value, CDAB
  IR 0        uint16  counter
  DI 0        bool    sine > 0
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import threading
import time

from .datatypes import encode
from .framing import MAX_READ, crc16


class DataStore:
    def __init__(self):
        self.coils = [False] * 65536
        self.discrete = [False] * 65536
        self.holding = [0] * 65536
        self.input = [0] * 65536
        self.valid = [range(0, 1000), range(0x1000, 0x1100)]
        self.lock = threading.Lock()
        self.holding[0] = 0x0050
        self.holding[2] = 0x0151

    def ok(self, a: int, n: int) -> bool:
        return any(a in r and a + n - 1 in r for r in self.valid)

    def tick(self, t: float) -> None:
        s = math.sin(t / 5)
        with self.lock:
            self.holding[100] = int(s * 1000) & 0xFFFF
            self.holding[101:103] = encode(25 + 5 * s, "float32", "ABCD")
            self.holding[103:105] = encode(25 + 5 * s, "float32", "CDAB")
            self.input[0] = (self.input[0] + 1) & 0xFFFF
            self.discrete[0] = s > 0


def handle_pdu(store: DataStore, pdu: bytes) -> bytes:
    fc = pdu[0]

    def exc(code):
        return bytes([fc | 0x80, code])

    try:
        with store.lock:
            if fc in (1, 2, 3, 4):
                a, n = struct.unpack(">HH", pdu[1:5])
                if not 1 <= n <= MAX_READ[fc]:
                    return exc(3)
                if not store.ok(a, n):
                    return exc(2)
                if fc in (1, 2):
                    src = store.coils if fc == 1 else store.discrete
                    out = bytearray((n + 7) // 8)
                    for i in range(n):
                        if src[a + i]:
                            out[i // 8] |= 1 << (i % 8)
                    return bytes([fc, len(out)]) + bytes(out)
                src = store.holding if fc == 3 else store.input
                return bytes([fc, 2 * n]) + struct.pack(f">{n}H", *src[a:a + n])
            if fc == 5:
                a, v = struct.unpack(">HH", pdu[1:5])
                if v not in (0, 0xFF00):
                    return exc(3)
                if not store.ok(a, 1):
                    return exc(2)
                store.coils[a] = v == 0xFF00
                return pdu[:5]
            if fc == 6:
                a, v = struct.unpack(">HH", pdu[1:5])
                if not store.ok(a, 1):
                    return exc(2)
                store.holding[a] = v
                return pdu[:5]
            if fc == 15:
                a, n, _ = struct.unpack(">HHB", pdu[1:6])
                if not store.ok(a, n):
                    return exc(2)
                for i in range(n):
                    store.coils[a + i] = bool((pdu[6 + i // 8] >> (i % 8)) & 1)
                return pdu[:5]
            if fc == 16:
                a, n, _ = struct.unpack(">HHB", pdu[1:6])
                if not store.ok(a, n):
                    return exc(2)
                store.holding[a:a + n] = struct.unpack(f">{n}H", pdu[6:6 + 2 * n])
                return pdu[:5]
            if fc == 43 and pdu[1] == 0x0E:
                objs = {0: b"ModbusKit", 1: b"SIM-1", 2: b"1.0"}
                body = b"".join(bytes([k, len(v)]) + v for k, v in objs.items())
                return bytes([0x2B, 0x0E, pdu[2], 0x01, 0x00, 0x00, len(objs)]) + body
            return exc(1)
    except (struct.error, IndexError):
        return exc(3)


def rtu_serve(read, write, store: DataStore, units: set[int], stop: threading.Event, gap: float = 0.01):
    buf, last = bytearray(), None
    while not stop.is_set():
        chunk = read()
        now = time.monotonic()
        if chunk:
            buf += chunk
            last = now
            continue
        if buf and now - last > gap:
            frame, buf = bytes(buf), bytearray()
            if len(frame) < 4 or crc16(frame[:-2]) != frame[-2:]:
                continue
            unit = frame[0]
            if unit == 0 or unit not in units:
                if unit == 0:
                    handle_pdu(store, frame[1:-2])
                continue
            head = bytes([unit]) + handle_pdu(store, frame[1:-2])
            write(head + crc16(head))


def tcp_client_thread(conn: socket.socket, framing: str, store: DataStore, units: set[int], stop):
    conn.settimeout(0.005)

    def read():
        try:
            d = conn.recv(1024)
        except socket.timeout:
            return b""
        if not d:
            raise ConnectionError
        return d

    try:
        if framing == "rtu":
            rtu_serve(read, conn.sendall, store, units, stop)
            return
        buf = bytearray()
        while not stop.is_set():
            buf += read()
            while len(buf) >= 7:
                tid, pid, ln = struct.unpack(">HHH", buf[:6])
                if len(buf) < 6 + ln:
                    break
                frame, buf = bytes(buf[:6 + ln]), buf[6 + ln:]
                unit, pdu = frame[6], frame[7:]
                resp = handle_pdu(store, pdu) if unit in units else bytes([pdu[0] | 0x80, 0x0B])
                conn.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
    except (ConnectionError, OSError):
        pass
    finally:
        conn.close()


def _ticker(store: DataStore):
    while True:
        store.tick(time.time())
        time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser(description="ModbusKit bench simulator")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tcp", type=int, metavar="PORT", help="listen on this TCP port")
    g.add_argument("--serial", metavar="PORT", help="serve RTU on this serial port")
    ap.add_argument("--framing", choices=["tcp", "rtu"], default="tcp", help="for --tcp: tcp (MBAP) or rtu (RTU over TCP)")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="N", choices=["N", "E", "O"])
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--units", type=int, nargs="+", default=[1])
    args = ap.parse_args()

    store, units, stop = DataStore(), set(args.units), threading.Event()
    store.holding[0x1010] = args.units[0]
    threading.Thread(target=_ticker, args=(store,), daemon=True).start()

    if args.serial:
        import serial
        ser = serial.Serial(args.serial, args.baud, 8, args.parity, args.stopbits, timeout=0.002)
        print(f"RTU slave on {args.serial} {args.baud} 8{args.parity}{args.stopbits}, units {sorted(units)}")
        rtu_serve(lambda: ser.read(ser.in_waiting or 1), ser.write, store, units, stop,
                  gap=max(3.5 * 11 / args.baud, 0.005))
        return

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.tcp))
    srv.listen(8)
    print(f"{'Modbus TCP' if args.framing == 'tcp' else 'RTU over TCP'} slave on {args.bind}:{args.tcp}, units {sorted(units)}")
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=tcp_client_thread, args=(conn, args.framing, store, units, stop),
                             daemon=True).start()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
