"""Crash-safe file helpers.

All JSON state is written atomically (temp file, fsync, rename) and logs are
append-only JSON lines, so a crash loses at most the record being written
(architecture §13).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

import numpy as np


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def dumps(obj: Any, indent: int | None = None) -> str:
    return json.dumps(obj, default=_json_default, indent=indent, allow_nan=False)


def fsync_dir(path: Path) -> None:
    """Make renames in a folder durable."""
    _fsync_dir(path)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: str | Path, obj: Any, indent: int | None = 2) -> None:
    atomic_write_bytes(path, dumps(obj, indent=indent).encode("utf-8"))


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except FileNotFoundError:
        return default


class JsonlLog:
    """Append-only JSON-lines log. Thread-safe."""

    def __init__(self, path: str | Path, fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = dumps(record) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                if self._fsync:
                    os.fsync(f.fileno())

    def read(self) -> list[dict]:
        return list(iter_jsonl(self.path))


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    """Yield records; a truncated or corrupt line (e.g. from a crash) is skipped."""
    try:
        f = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def dir_size(path: str | Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def remove_tree(path: str | Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
