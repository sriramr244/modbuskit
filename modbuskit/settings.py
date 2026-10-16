from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

from .tags import Tag

SETTINGS_PATH = Path(os.environ.get("MODBUSKIT_SETTINGS", Path.home() / ".modbuskit"/"settings.json"))

def load_settings(path: Path = SETTINGS_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2, default=str))
    tmp_path.replace(path)

def delete_settings(path: Path = SETTINGS_PATH) -> None:
    try:
        path.unlink()
    except OSError:
        pass

def tag_to_dicts(tag: list[Tag]) -> list[dict]:
    out = []
    for t in tag:
        d =dataclasses.asdict(t)
        if d.get("enum"):
            d["enum"] = {str(k): v for k, v in d["enum"].items()}
        out.append(d)
    return out

def dicts_to_tag(dicts: list[dict]) -> list[Tag]:
    out = []
    names = {f.name for f in dataclasses.fields(Tag)}
    for d in dicts or []:
        try:
            d = {k:v for k,v in d.items() if k in names}
            if d.get("enum"):
                d["enum"] = {int(k): v for k, v in d["enum"].items()}
            out.append(Tag(**d))
        except (TypeError, ValueError, AttributeError):
            continue
    return out