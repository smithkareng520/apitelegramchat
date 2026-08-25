"""Persistent authorization storage for Telegram user identifiers."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Iterable

from apitelegramchat.workspace_paths import data_root

_AUTH_FILENAME = "authorized_users.json"
_AUTH_SCHEMA_VERSION = 1


class AuthorizationStore:
    """Keep authorized user IDs/usernames in a private, atomically written file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path or (data_root() / _AUTH_FILENAME)

    @staticmethod
    def _normalize(values: Iterable[object]) -> set[str]:
        normalized: set[str] = set()
        for value in values:
            if value is None:
                continue
            identifier = str(value).strip().lstrip("@")
            if identifier:
                normalized.add(identifier)
        return normalized

    def load_sync(self) -> set[str]:
        path = self.path
        if not path.exists():
            return set()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取授权白名单: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != _AUTH_SCHEMA_VERSION:
            raise RuntimeError("授权白名单文件格式无效")
        users = payload.get("users", [])
        if not isinstance(users, list):
            raise RuntimeError("授权白名单 users 字段必须为列表")
        return self._normalize(users)

    async def load(self) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self.load_sync)

    def save_sync(self, users: Iterable[object]) -> set[str]:
        normalized = self._normalize(users)
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        payload = {
            "version": _AUTH_SCHEMA_VERSION,
            "users": sorted(normalized),
        }
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"无法写入授权白名单: {exc}") from exc
        return normalized

    async def save(self, users: Iterable[object]) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self.save_sync, list(users))


authorization_store = AuthorizationStore()
