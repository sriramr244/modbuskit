"""
Device profiles: YAML files that describe one device type (register map, enums,
default unit ID, the register to probe during a scan). This is where all
device-specific knowledge lives - the core never hard-codes a device.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .tags import Tag


@dataclass
class Profile:
    name: str
    description: str = ""
    source: str = ""
    defaults: dict = field(default_factory=dict)   # unit_id, timeout ...
    serial: dict = field(default_factory=dict)     # bauds / parities / stopbits / bytesize
    probe: dict = field(default_factory=dict)      # fc, address, count
    tags: list[Tag] = field(default_factory=list)
    path: str = ""


def parse_address(v) -> int:
    """Accept 4112, 0x1010, '0x1010', '1010h'."""
    if isinstance(v, int):
        return v
    s = str(v).strip().lower()
    if s.endswith("h"):
        return int(s[:-1], 16)
    return int(s, 0)


def load_profile(path: str | Path | None = None, text: str | None = None) -> Profile:
    doc = yaml.safe_load(text if text is not None else Path(path).read_text())
    enums = {k: {int(a): str(b) for a, b in v.items()} for k, v in (doc.get("enums") or {}).items()}
    tags = []
    for t in doc.get("tags") or []:
        enum = t.get("enum")
        if isinstance(enum, str):
            if enum not in enums:
                raise ValueError(f"tag {t['name']}: unknown enum '{enum}'")
            enum = enums[enum]
        elif isinstance(enum, dict):
            enum = {int(a): str(b) for a, b in enum.items()}
        tags.append(Tag(
            name=str(t["name"]), address=parse_address(t["address"]), fc=int(t.get("fc", doc.get("defaults", {}).get("fc", 3))),
            dtype=t.get("dtype", "uint16"), order=t.get("order", doc.get("defaults", {}).get("order", "ABCD")),
            scale=float(t.get("scale", 1)), offset=float(t.get("offset", 0)), units=str(t.get("units", "")),
            length=int(t.get("length", 1)), unit_id=t.get("unit_id"), enum=enum,
            description=str(t.get("description", ""))))
    probe = doc.get("probe") or {}
    if "address" in probe:
        probe["address"] = parse_address(probe["address"])
    return Profile(name=doc.get("name", Path(path).stem if path else "profile"),
                   description=doc.get("description", ""), source=doc.get("source", ""),
                   defaults=doc.get("defaults") or {}, serial=doc.get("serial") or {},
                   probe=probe, tags=tags, path=str(path or ""))


def profile_to_yaml(name: str, tags: list[Tag], **meta) -> str:
    out = {"name": name, **meta, "tags": []}
    for t in tags:
        d = {"name": t.name, "address": t.address, "fc": t.fc, "dtype": t.dtype}
        if t.order != "ABCD": d["order"] = t.order
        if t.scale != 1: d["scale"] = t.scale
        if t.offset: d["offset"] = t.offset
        if t.units: d["units"] = t.units
        if t.dtype == "string": d["length"] = t.length
        if t.unit_id is not None: d["unit_id"] = t.unit_id
        if t.enum: d["enum"] = dict(t.enum)
        if t.description: d["description"] = t.description
        out["tags"].append(d)
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True)


def list_profiles(directory: str | Path) -> list[Path]:
    d = Path(directory)
    return sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))) if d.is_dir() else []
