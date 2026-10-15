"""
Tags: named points (address + type + scaling), and the block planner that turns
a tag list into as few Modbus reads as possible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import datatypes as dt
from .framing import BIT_FCS, FC_REF_PREFIX, MAX_READ


@dataclass
class Tag:
    name: str
    address: int                  # 0-based protocol address
    fc: int = 3                   # read function: 1, 2, 3, 4
    dtype: str = "uint16"
    order: str = "ABCD"
    scale: float = 1.0
    offset: float = 0.0
    units: str = ""
    length: int = 1               # registers, only for dtype "string"
    unit_id: int | None = None    # None -> connection default
    enum: dict[int, str] | None = None
    description: str = ""

    @property
    def width(self) -> int:
        if self.fc in BIT_FCS:
            return 1
        if self.dtype == "string":
            return max(1, int(self.length))
        return dt.width(self.dtype)

    @property
    def ref(self) -> str:
        """PLC-style reference, e.g. FC3 address 0 -> 40001."""
        n = self.address + 1
        return f"{FC_REF_PREFIX.get(self.fc, '?')}{n:04d}" if n <= 9999 else f"{FC_REF_PREFIX.get(self.fc, '?')}{n:05d}"

    def decode(self, words) -> "TagValue":
        if self.fc in BIT_FCS:
            b = int(bool(words[0]))
            text = self.enum.get(b, str(b)) if self.enum else ("ON" if b else "OFF")
            return TagValue(b, float(b), text)
        raw = dt.decode(words, self.dtype, self.order)
        if isinstance(raw, str):
            return TagValue(raw, None, raw)
        value = raw * self.scale + self.offset if (self.scale != 1 or self.offset) else raw
        if self.enum:
            text = self.enum.get(int(raw), f"? ({raw})")
        elif self.dtype in ("hex", "binary"):
            text = dt.fmt(raw, self.dtype)
        else:
            text = dt.fmt(value, self.dtype)
        return TagValue(raw, float(value), text)


@dataclass
class TagValue:
    raw: Any
    value: float | None     # numeric engineering value (None for strings) - what gets trended
    text: str


@dataclass
class Block:
    unit_id: int
    fc: int
    start: int
    count: int
    tags: list[Tag] = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.start + self.count

    def decode(self, data) -> dict[str, TagValue]:
        out = {}
        for t in self.tags:
            off = t.address - self.start
            out[t.name] = t.decode(data[off:off + t.width])
        return out


def plan_blocks(tags: list[Tag], default_unit: int, max_gap: int = 10,
                max_regs: int = 120, max_bits: int = 1968) -> list[Block]:
    """Group tags by (unit, fc), sort by address, merge neighbours when the hole
    between them is <= max_gap. max_gap=0 -> only merge contiguous tags."""
    groups: dict[tuple[int, int], list[Tag]] = {}
    for t in tags:
        unit = t.unit_id if t.unit_id is not None else default_unit
        groups.setdefault((unit, t.fc), []).append(t)
    blocks: list[Block] = []
    for (unit, fc), ts in sorted(groups.items()):
        limit = min(MAX_READ[fc], max_bits if fc in BIT_FCS else max_regs)
        cur: Block | None = None
        for t in sorted(ts, key=lambda t: t.address):
            end = t.address + t.width
            if cur and t.address <= cur.end + max_gap and max(cur.end, end) - cur.start <= limit:
                cur.count = max(cur.end, end) - cur.start
                cur.tags.append(t)
            else:
                cur = Block(unit, fc, t.address, t.width, [t])
                blocks.append(cur)
    return blocks


def tags_from_range(fc: int, start: int, quantity: int, dtype: str = "uint16",
                    order: str = "ABCD", unit_id: int | None = None, prefix: str = "") -> list[Tag]:
    """Modbus-Poll-style window: `quantity` consecutive values starting at `start`."""
    step = 1 if fc in BIT_FCS else dt.width(dtype)
    prefix = prefix or {1: "coil", 2: "di", 3: "hr", 4: "ir"}[fc]
    return [Tag(name=f"{prefix}_{start + i * step}", address=start + i * step, fc=fc,
                dtype="uint16" if fc in BIT_FCS else dtype, order=order, unit_id=unit_id)
            for i in range(quantity)]
