from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _default_value(default: Any) -> Any:
    return copy.deepcopy(default)


def _backup_corrupt_file(path: Path) -> None:
    if not path.exists():
        return

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.stem}.corrupt-{timestamp}{path.suffix}")
    try:
        path.replace(backup_path)
    except OSError:
        return


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return _default_value(default)

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _backup_corrupt_file(path)
        return _default_value(default)


def write_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}-",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
