"""
Modbus PDU encode/decode + CRC. Pure functions, no I/O.

Layering:
    transport.py  moves bytes (serial port or TCP socket)
    framing.py    builds/parses PDUs (this file)
    client.py     wraps PDUs in RTU or MBAP frames and runs request/response
"""
from __future__ import annotations

import struct

READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING, READ_INPUT = 1, 2, 3, 4
WRITE_COIL, WRITE_REGISTER, WRITE_COILS, WRITE_REGISTERS = 5, 6, 15, 16
READ_DEVICE_ID = 43
MEI_DEVICE_ID = 0x0E

BIT_FCS = (1, 2)
REG_FCS = (3, 4)
READ_FCS = BIT_FCS + REG_FCS
MAX_READ = {1: 2000, 2: 2000, 3: 125, 4: 125}

FC_NAMES = {
    1: "01 Read Coils (0x)",
    2: "02 Read Discrete Inputs (1x)",
    3: "03 Read Holding Registers (4x)",
    4: "04 Read Input Registers (3x)",
    5: "05 Write Single Coil",
    6: "06 Write Single Register",
    15: "15 Write Multiple Coils",
    16: "16 Write Multiple Registers",
    43: "43/14 Read Device Identification",
}
FC_REF_PREFIX = {1: 0, 2: 1, 3: 4, 4: 3}  # PLC-style reference prefix (40001 etc.)

EXCEPTION_TEXT = {
    1: "Illegal function",
    2: "Illegal data address",
    3: "Illegal data value",
    4: "Slave device failure",
    5: "Acknowledge",
    6: "Slave device busy",
    8: "Memory parity error",
    10: "Gateway path unavailable",
    11: "Gateway target device failed to respond",
}

DEVICE_ID_OBJECTS = {
    0: "VendorName", 1: "ProductCode", 2: "MajorMinorRevision", 3: "VendorUrl",
    4: "ProductName", 5: "ModelName", 6: "UserApplicationName",
}


class FrameError(Exception):
    """A reply arrived but is malformed or does not match the request."""


class ModbusException(Exception):
    """The device answered with a Modbus exception response."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"exception {code:02X}: {EXCEPTION_TEXT.get(code, 'unknown')}")


def crc16(data: bytes) -> bytes:
    """Modbus CRC-16 (poly 0xA001, init 0xFFFF), returned low byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, crc >> 8])


def _check_addr(address: int, count: int = 1) -> None:
    if not 0 <= address <= 0xFFFF or address + count - 1 > 0xFFFF:
        raise ValueError(f"address range {address}..{address + count - 1} outside 0..65535")


# ---------- request builders ----------

def pdu_read(fc: int, address: int, count: int) -> bytes:
    if fc not in READ_FCS:
        raise ValueError(f"FC{fc} is not a read function")
    if not 1 <= count <= MAX_READ[fc]:
        raise ValueError(f"FC{fc:02d} quantity must be 1..{MAX_READ[fc]}")
    _check_addr(address, count)
    return struct.pack(">BHH", fc, address, count)


def pdu_write_coil(address: int, value: bool) -> bytes:
    _check_addr(address)
    return struct.pack(">BHH", WRITE_COIL, address, 0xFF00 if value else 0x0000)


def pdu_write_register(address: int, value: int) -> bytes:
    _check_addr(address)
    return struct.pack(">BHH", WRITE_REGISTER, address, int(value) & 0xFFFF)


def pdu_write_coils(address: int, values) -> bytes:
    values = [bool(v) for v in values]
    n = len(values)
    if not 1 <= n <= 1968:
        raise ValueError("FC15 quantity must be 1..1968")
    _check_addr(address, n)
    packed = bytearray((n + 7) // 8)
    for i, v in enumerate(values):
        if v:
            packed[i // 8] |= 1 << (i % 8)
    return struct.pack(">BHHB", WRITE_COILS, address, n, len(packed)) + bytes(packed)


def pdu_write_registers(address: int, values) -> bytes:
    values = [int(v) & 0xFFFF for v in values]
    n = len(values)
    if not 1 <= n <= 123:
        raise ValueError("FC16 quantity must be 1..123")
    _check_addr(address, n)
    return struct.pack(f">BHHB{n}H", WRITE_REGISTERS, address, n, 2 * n, *values)


def pdu_read_device_id(read_code: int = 1, object_id: int = 0) -> bytes:
    return bytes([READ_DEVICE_ID, MEI_DEVICE_ID, read_code, object_id])


# ---------- response handling ----------

def rtu_frame_length(buf: bytes) -> int | None:
    """Total RTU frame length (unit + PDU + CRC) once the header is in buf.
    None = not known yet, or variable (FC43) -> caller falls back to the silence gap."""
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc & 0x80:
        return 5
    if fc in READ_FCS:
        return 5 + buf[2] if len(buf) >= 3 else None
    if fc in (WRITE_COIL, WRITE_REGISTER, WRITE_COILS, WRITE_REGISTERS):
        return 8
    return None


def parse_response(req: bytes, resp: bytes):
    """Validate a reply PDU against its request PDU and return the decoded data."""
    if not resp:
        raise FrameError("empty PDU")
    fc = req[0]
    if resp[0] == (fc | 0x80):
        if len(resp) < 2:
            raise FrameError("short exception reply")
        raise ModbusException(resp[1])
    if resp[0] != fc:
        raise FrameError(f"function code mismatch: sent {fc}, got {resp[0]}")

    if fc in READ_FCS:
        count = struct.unpack(">H", req[3:5])[0]
        if len(resp) < 2 or len(resp) != 2 + resp[1]:
            raise FrameError("byte count does not match payload")
        payload = resp[2:]
        if fc in BIT_FCS:
            if len(payload) != (count + 7) // 8:
                raise FrameError("wrong number of bit bytes")
            return [bool((payload[i // 8] >> (i % 8)) & 1) for i in range(count)]
        if len(payload) != 2 * count:
            raise FrameError(f"expected {count} registers, got {len(payload) // 2}")
        return list(struct.unpack(f">{count}H", payload))

    if fc in (WRITE_COIL, WRITE_REGISTER):
        if resp != req:
            raise FrameError("write echo does not match request")
        return True
    if fc in (WRITE_COILS, WRITE_REGISTERS):
        if resp[:5] != req[:5]:
            raise FrameError("write confirmation does not match request")
        return True
    if fc == READ_DEVICE_ID:
        return parse_device_id(resp)
    return resp[1:]


def parse_device_id(resp: bytes) -> dict:
    if len(resp) < 7 or resp[1] != MEI_DEVICE_ID:
        raise FrameError("not a device-identification reply")
    more, next_id, n = resp[4], resp[5], resp[6]
    objects, i = {}, 7
    for _ in range(n):
        if i + 2 > len(resp):
            raise FrameError("truncated device-identification object")
        oid, ln = resp[i], resp[i + 1]
        val = resp[i + 2:i + 2 + ln]
        if len(val) < ln:
            raise FrameError("truncated device-identification value")
        objects[DEVICE_ID_OBJECTS.get(oid, f"Object 0x{oid:02X}")] = val.decode("latin-1", "replace")
        i += 2 + ln
    return {"conformity": resp[3], "more": more == 0xFF, "next_id": next_id, "objects": objects}
