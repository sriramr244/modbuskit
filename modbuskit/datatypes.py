"""
Register <-> value conversion, with the four word/byte orders you meet in the field.

    ABCD  big-endian, high word first (Modbus "standard")
    CDAB  word swap        (very common: Schneider, many meters)
    BADC  byte swap
    DCBA  little-endian
"""
from __future__ import annotations

import struct

DTYPES = {           # name: (registers, struct code)
    "uint16": (1, "H"), "int16": (1, "h"),
    "uint32": (2, "I"), "int32": (2, "i"), "float32": (2, "f"),
    "uint64": (4, "Q"), "int64": (4, "q"), "float64": (4, "d"),
    "hex": (1, "H"), "binary": (1, "H"),
    "string": (1, None),     # width = tag.length registers, 2 chars per register
}
WORD_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")


def width(dtype: str) -> int:
    return DTYPES[dtype][0]


def _transform(words: list[bytes], order: str) -> list[bytes]:
    # both operations are their own inverse, so this works for encode and decode
    if order in ("CDAB", "DCBA"):
        words = words[::-1]
    if order in ("BADC", "DCBA"):
        words = [w[::-1] for w in words]
    return words


def regs_to_bytes(regs, order: str = "ABCD") -> bytes:
    return b"".join(_transform([int(r).to_bytes(2, "big") for r in regs], order))


def bytes_to_regs(data: bytes, order: str = "ABCD") -> list[int]:
    words = [data[i:i + 2] for i in range(0, len(data), 2)]
    return [int.from_bytes(w, "big") for w in _transform(words, order)]


def decode(regs, dtype: str, order: str = "ABCD"):
    if dtype == "string":
        return regs_to_bytes(regs, order).decode("latin-1").rstrip("\x00 ")
    n, code = DTYPES[dtype]
    if len(regs) < n:
        raise ValueError(f"{dtype} needs {n} registers, got {len(regs)}")
    return struct.unpack(">" + code, regs_to_bytes(regs[:n], order))[0]


def encode(value, dtype: str, order: str = "ABCD", length: int = 1) -> list[int]:
    if dtype == "string":
        raw = str(value).encode("latin-1")[:2 * length].ljust(2 * length, b"\x00")
        return bytes_to_regs(raw, order)
    n, code = DTYPES[dtype]
    if code in "fd":
        value = float(value)
    else:
        value = int(value, 0) if isinstance(value, str) else int(value)
    return bytes_to_regs(struct.pack(">" + code, value), order)


def fmt(raw, dtype: str) -> str:
    if dtype == "hex":
        return f"0x{int(raw):04X}"
    if dtype == "binary":
        b = f"{int(raw):016b}"
        return f"{b[:4]} {b[4:8]} {b[8:12]} {b[12:]}"
    if isinstance(raw, float):
        return f"{raw:.6g}"
    return str(raw)
