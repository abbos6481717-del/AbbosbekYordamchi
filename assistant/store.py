"""Small JSON-file stores kept under ``data/`` (a Railway Volume in production)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

IS_WINDOWS = sys.platform.startswith("win")


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(path, 0o700)
    return path


def append_private_line(path: Path, line: str) -> None:
    """Append one line to a file that is created with mode 0600."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


class JsonFile:
    """Atomic JSON file: write to a 0600 temp file, then ``os.replace``."""

    def __init__(self, path: Path, default: Callable[[], Any]) -> None:
        self.path = Path(path)
        self.default = default

    def read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return self.default()

    def write(self, data: Any) -> None:
        ensure_private_dir(self.path.parent)
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            if not IS_WINDOWS:
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HistoryStore:
    """Per business connection *and* per chat conversation history."""

    def __init__(self, path: Path, limit: int) -> None:
        self.file = JsonFile(path, dict)
        self.limit = limit
        data = self.file.read()
        self._data: Dict[str, List[Dict[str, Any]]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return list(self._data.get(self.key(connection_id, chat_id), []))

    def add(
        self,
        connection_id: str,
        chat_id: int,
        *,
        role: str,
        name: str,
        text: str,
        lang: Optional[str],
        ts: float,
    ) -> None:
        key = self.key(connection_id, chat_id)
        items = self._data.setdefault(key, [])
        items.append({"role": role, "name": name, "text": text, "lang": lang, "ts": ts})
        if len(items) > self.limit:
            del items[: len(items) - self.limit]
        self.file.write(self._data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._data.get(self.key(connection_id, chat_id), [])):
            if item.get("role") == "other" and item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._data)


class SeenMessages:
    """Remembers processed message keys (also across restarts)."""

    def __init__(self, path: Path, capacity: int = 5000) -> None:
        self.file = JsonFile(path, list)
        self.capacity = capacity
        data = self.file.read()
        self._keys: "OrderedDict[str, None]" = OrderedDict(
            (str(key), None) for key in (data if isinstance(data, list) else [])
        )

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        self.file.write(list(self._keys))
        return True


class RuntimeState:
    """``paused`` and ``limit_until``, persisted across restarts."""

    def __init__(self, path: Path) -> None:
        self.file = JsonFile(path, dict)
        data = self.file.read()
        data = data if isinstance(data, dict) else {}
        self.paused = bool(data.get("paused", False))
        limit = data.get("limit_until")
        self.limit_until: Optional[float] = float(limit) if isinstance(limit, (int, float)) else None

    def save(self) -> None:
        self.file.write({"paused": self.paused, "limit_until": self.limit_until})

    def set_paused(self, value: bool) -> None:
        self.paused = value
        self.save()

    def set_limit_until(self, value: Optional[float]) -> None:
        self.limit_until = value
        self.save()
