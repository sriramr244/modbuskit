"""
Headless CLI on the same core (field laptop, no browser needed).

  python -m modbuskit.cli sweep --port COM5 --profile profiles/ge_multilin_rrtd.yaml
  python -m modbuskit.cli sweep --port COM5 --units 12 254 1 --full
  python -m modbuskit.cli read  --tcp 10.0.0.50 --unit 1 --fc 3 --addr 0 --count 10
  python -m modbuskit.cli read  --rtu-tcp 10.0.0.60:10001 --unit 254 --profile profiles/ge_multilin_rrtd.yaml
  python -m modbuskit.cli read  --port COM5 --baud 19200 --parity N --unit 254 --addr 0x1010 --count 9
"""
from __future__ import annotations

import argparse
import sys

from . import make_client, scanner
from .poller import Poller
from .profiles import load_profile, parse_address
from .scanner import COMMON_BAUDS


def _hostport(s: str, default: int):
    host, _, port = s.partition(":")
    return host, int(port or default)


def cmd_sweep(a):
    prof = load_profile(a.profile) if a.profile else None
    ser = prof.serial if prof else {}
    probe = prof.probe if prof else {}
    units = a.units or ([prof.defaults["unit_id"]] if prof and "unit_id" in prof.defaults else [1])
    if a.full:
        units += [u for u in range(1, 248 if not prof else 255) if u not in units]
    bauds = a.bauds or ser.get("bauds", COMMON_BAUDS)
    parities = a.parities or ser.get("parities", ["N", "E", "O"])
    stopbits = ser.get("stopbits", [1, 2]) if not a.stopbits else a.stopbits
    total = len(bauds) * len(parities) * len(stopbits) * len(units)
    print(f"{a.port}: {total} combinations, read-only FC{probe.get('fc', 3):02d}")

    def prog(done, tot, msg):
        print(f"\r  [{done}/{tot}] {msg:<40}", end="", flush=True)

    try:
        hits, garbled, _ = scanner.sweep_serial(
            a.port, bauds, parities, stopbits, units, ser.get("bytesize", 8),
            probe.get("fc", 3), probe.get("address", 0), probe.get("count", 1),
            a.timeout, a.strip_echo, not a.all, prog)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 1
    print()
    if not hits:
        print("NO RESPONSE.")
        if garbled:
            print("Garbled replies at:", ", ".join(sorted(garbled)), "-> baud likely right; check parity/polarity.")
        print("Next: swap A/B, check converter is in 2-wire RS-485 mode, device powered, try --full.")
        return 1
    for h in hits:
        print(f"FOUND: {h['baud']} {h['bytesize']}{h['parity']}{h['stopbits']}  unit {h['unit']}  {h['status']} {h['detail']}")
    if prof:
        h = hits[0]
        c = make_client("rtu", port=a.port, baudrate=h["baud"], parity=h["parity"], stopbits=h["stopbits"],
                        timeout=max(a.timeout, 0.5))
        c.connect()
        _print_tags(c, prof.tags, h["unit"])
        c.close()
    return 0


def _print_tags(client, tags, unit):
    p = Poller(client, tags, unit)
    p.cycle()
    latest, _ = p.snapshot()
    for t in tags:
        v = latest[t.name]
        print(f"  {t.name:<22} {t.ref:>7}  {v['text'] if v['status'] == 'OK' else v['status']:<14} {t.units}")


def cmd_read(a):
    if a.tcp:
        host, port = _hostport(a.tcp, 502)
        c = make_client("tcp", host=host, tcp_port=port, timeout=a.timeout)
    elif a.rtu_tcp:
        host, port = _hostport(a.rtu_tcp, 10001)
        c = make_client("rtu_tcp", host=host, tcp_port=port, timeout=a.timeout)
    else:
        c = make_client("rtu", port=a.port, baudrate=a.baud, parity=a.parity, stopbits=a.stopbits, timeout=a.timeout)
    c.connect()
    try:
        if a.profile:
            _print_tags(c, load_profile(a.profile).tags, a.unit)
            return 0
        r = c.read(a.unit, a.fc, parse_address(a.addr), a.count)
        print(f"{r.status.value} {r.detail}  ({r.rtt_ms:.1f} ms)")
        if r.ok:
            base = parse_address(a.addr)
            for i, v in enumerate(r.data):
                print(f"  {base + i:>5} (0x{base + i:04X})  {int(v):>6}  0x{int(v):04X}")
        return 0 if r.ok else 1
    finally:
        c.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="modbuskit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sweep", help="find baud/parity/stop bits/unit ID on a serial port (read-only)")
    s.add_argument("--port", required=True)
    s.add_argument("--profile")
    s.add_argument("--units", type=int, nargs="+")
    s.add_argument("--full", action="store_true", help="also try every unit ID")
    s.add_argument("--bauds", type=int, nargs="+")
    s.add_argument("--parities", nargs="+", choices=["N", "E", "O"])
    s.add_argument("--stopbits", type=int, nargs="+", choices=[1, 2])
    s.add_argument("--timeout", type=float, default=0.4)
    s.add_argument("--strip-echo", action="store_true")
    s.add_argument("--all", action="store_true", help="keep going after the first hit")
    s.set_defaults(func=cmd_sweep)

    r = sub.add_parser("read", help="one read (or all profile tags)")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="serial port (Modbus RTU)")
    g.add_argument("--tcp", metavar="HOST[:PORT]", help="Modbus TCP/IP")
    g.add_argument("--rtu-tcp", metavar="HOST[:PORT]", help="Modbus RTU over TCP")
    r.add_argument("--baud", type=int, default=9600)
    r.add_argument("--parity", default="N", choices=["N", "E", "O"])
    r.add_argument("--stopbits", type=int, default=1)
    r.add_argument("--unit", type=int, default=1)
    r.add_argument("--fc", type=int, default=3, choices=[1, 2, 3, 4])
    r.add_argument("--addr", default="0")
    r.add_argument("--count", type=int, default=10)
    r.add_argument("--timeout", type=float, default=1.0)
    r.add_argument("--profile")
    r.set_defaults(func=cmd_read)

    a = ap.parse_args(argv)
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
