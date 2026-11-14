"""Shared test helpers. Nothing here touches the network."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import pytest

from assistant.config import load_settings

OWNER_ID = 111111
STRANGER_ID = 222222
BOT_TOKEN = "123456:TEST-token"
PROFILE_TEXT = """<!-- template comment -->
# Men haqimda
- Ism: Bobur
- Shahar: Toshkent
"""


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGenerator:
    """Returns queued replies (or raises queued exceptions) and records calls."""

    def __init__(self, default: str = "Salom! Hozir bandman, keyinroq yozaman.") -> None:
        self.default = default
        self.responses: Deque[Any] = deque()
        self.calls: List[Tuple[str, str]] = []
        self.hook: Optional[Callable[[], Awaitable[None]]] = None

    def queue(self, *items: Any) -> None:
        self.responses.extend(items)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.hook is not None:
            hook, self.hook = self.hook, None
            await hook()
        await asyncio.sleep(0)
        item = self.responses.popleft() if self.responses else self.default
        if isinstance(item, BaseException):
            raise item
        return item


try:
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetBusinessConnection, SendMessage
    from aiogram.types import BusinessConnection, Chat, Message, Update

    HAVE_AIOGRAM = True
except ImportError:  # pragma: no cover - only when dev dependencies are missing
    HAVE_AIOGRAM = False


if HAVE_AIOGRAM:

    class RecordingSession(BaseSession):
        """Fake Telegram API: records every request, never opens a connection."""

        def __init__(self) -> None:
            super().__init__()
            self.requests: List[Any] = []
            self.connections: Dict[str, BusinessConnection] = {}
            self._next_message_id = 10_000

        async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
            self.requests.append(method)
            if isinstance(method, GetBusinessConnection):
                connection = self.connections.get(method.business_connection_id)
                if connection is None:
                    raise TelegramBadRequest(method=method, message="Bad Request: BUSINESS_CONNECTION_INVALID")
                return connection
            if isinstance(method, SendMessage):
                self._next_message_id += 1
                return Message(
                    message_id=self._next_message_id,
                    date=datetime.now(),
                    chat=Chat(id=method.chat_id, type="private"),
                    text=method.text,
                    business_connection_id=method.business_connection_id,
                )
            return True

        async def stream_content(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            raise AssertionError("tests must not download anything")
            yield b""  # pragma: no cover

        async def close(self) -> None:
            return None

    def connection_payload(
        connection_id: str = "conn-1",
        *,
        user_id: int = OWNER_ID,
        enabled: bool = True,
        can_reply: bool = True,
    ) -> Dict[str, Any]:
        return {
            "id": connection_id,
            "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
            "user_chat_id": user_id,
            "date": 1_700_000_000,
            # Both the new (Bot API 9.0) and the old field, for any aiogram 3.x.
            "rights": {"can_reply": can_reply},
            "can_reply": can_reply,
            "is_enabled": enabled,
        }

    class Harness:
        def __init__(self, tmp_path: Path, **overrides: str) -> None:
            self.tmp_path = tmp_path
            env_file = tmp_path / ".env"
            env_file.write_text("", encoding="utf-8")
            profile = tmp_path / "profile.md"
            profile.write_text(PROFILE_TEXT, encoding="utf-8")
            values = {
                "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
                "OWNER_ID": str(OWNER_ID),
                "DATA_DIR": str(tmp_path / "data"),
                "PROFILE_PATH": str(profile),
                "CLAUDE_BIN": str(tmp_path / "no-such-claude"),
            }
            values.update(overrides)
            self.settings = load_settings(env_file, overrides=values)
            self.session = RecordingSession()
            self.bot = Bot(BOT_TOKEN, session=self.session)
            self.generator = FakeGenerator()
            self.clock = Clock()
            self.previews: List[Any] = []
            self._update_id = 0
            self._message_id = 0
            self._build()

        def _build(self) -> None:
            from assistant.handlers import build_router
            from assistant.service import build_assistant

            self.assistant = build_assistant(
                self.settings,
                generator=self.generator,
                clock=self.clock,
                preview_sink=self.previews.append,
            )
            self.dp = Dispatcher()
            self.dp.include_router(build_router(self.assistant, self.settings))

        def restart(self) -> None:
            """New Assistant with the same data directory."""
            self._build()

        # -------------------------------------------------------------- helpers
        def _next_update(self) -> int:
            self._update_id += 1
            return self._update_id

        async def feed(self, payload: Dict[str, Any]) -> Any:
            payload = dict(payload)
            payload.setdefault("update_id", self._next_update())
            update = Update.model_validate(payload, context={"bot": self.bot})
            return await self.dp.feed_update(self.bot, update)

        def set_connection(self, connection_id: str = "conn-1", **kwargs: Any) -> BusinessConnection:
            """Change what Telegram answers to getBusinessConnection (no update)."""
            connection = BusinessConnection.model_validate(
                connection_payload(connection_id, **kwargs), context={"bot": self.bot}
            )
            self.session.connections[connection_id] = connection
            return connection

        async def connect(self, connection_id: str = "conn-1", **kwargs: Any) -> None:
            self.set_connection(connection_id, **kwargs)
            await self.feed({"business_connection": connection_payload(connection_id, **kwargs)})

        async def business_message(
            self,
            text: Optional[str] = "Salom, qalesan?",
            *,
            from_id: int = STRANGER_ID,
            chat_id: Optional[int] = None,
            message_id: Optional[int] = None,
            connection_id: str = "conn-1",
            is_bot: bool = False,
            via_business_bot: bool = False,
            first_name: str = "Ali",
            extra: Optional[Dict[str, Any]] = None,
        ) -> Any:
            if message_id is None:
                self._message_id += 1
                message_id = self._message_id
            if chat_id is None:
                chat_id = STRANGER_ID if from_id == OWNER_ID else from_id
            message: Dict[str, Any] = {
                "message_id": message_id,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "private", "first_name": first_name},
                "from": {"id": from_id, "is_bot": is_bot, "first_name": first_name},
                "business_connection_id": connection_id,
            }
            if text is not None:
                message["text"] = text
            if via_business_bot:
                message["sender_business_bot"] = {
                    "id": 999_999,
                    "is_bot": True,
                    "first_name": "Assistant",
                    "username": "assistant_bot",
                }
            if extra:
                message.update(extra)
            before = len(self.assistant.outcomes)
            await self.feed({"business_message": message})
            if len(self.assistant.outcomes) > before:
                return self.assistant.outcomes[-1]
            return None

        async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
            self._message_id += 1
            message: Dict[str, Any] = {
                "message_id": self._message_id,
                "date": 1_700_000_000,
                "chat": {"id": from_id, "type": "private", "first_name": "User"},
                "from": {"id": from_id, "is_bot": False, "first_name": "User"},
                "text": text,
            }
            if text.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
            await self.feed({"message": message})

        # ------------------------------------------------------------ inspection
        def sent(self) -> List[Any]:
            return [r for r in self.session.requests if isinstance(r, SendMessage)]

        def sent_to(self, chat_id: int) -> List[Any]:
            return [r for r in self.sent() if r.chat_id == chat_id]

        def lookups(self) -> List[Any]:
            return [r for r in self.session.requests if isinstance(r, GetBusinessConnection)]


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Any]:
    if not HAVE_AIOGRAM:
        pytest.skip("aiogram is not installed")

    def factory(**overrides: str) -> "Harness":
        return Harness(tmp_path, **overrides)

    return factory
