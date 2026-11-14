"""Core logic: decide whether to answer a business message, draft, preview or send."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import LANGUAGE_NAMES, detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, append_private_line, ensure_private_dir

log = logging.getLogger("assistant")

MAX_CHUNK = 4000
ERROR_NOTICE_INTERVAL = 600  # seconds
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_EMPTY_FIELD_RE = re.compile(r"^[-*]\s*[^:]+:\s*$")


class Outcome(str, Enum):
    previewed = "previewed"
    sent = "sent"
    duplicate = "duplicate"
    ignored_bot = "ignored_bot"
    no_connection = "no_connection"
    foreign_connection = "foreign_connection"
    connection_disabled = "connection_disabled"
    unsupported = "unsupported"
    owner_message = "owner_message"
    owner_active = "owner_active"
    paused = "paused"
    limited = "limited"
    skipped = "skipped"
    permission_denied = "permission_denied"
    error = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: str = ""


def print_preview(preview: Preview) -> None:
    lines = [
        "",
        "=" * 20 + " PREVIEW, not sent " + "=" * 20,
        f"Chat:     {preview.chat_name} (id {preview.chat_id})",
        f"Language: {LANGUAGE_NAMES.get(preview.language or '', 'unknown')}",
        f"Incoming: {preview.incoming}",
        f"Reply:    {preview.reply}",
    ]
    if preview.note:
        lines.append(f"Note:     {preview.note}")
    lines.append("=" * 59)
    log.info("\n".join(lines))


def describe(message: Message) -> Optional[str]:
    """Text for the history; media gets a short tag; service messages give None."""
    caption = (message.caption or "").strip()

    def tag(name: str, extra: str = "") -> str:
        extra = extra.strip()
        return f"[{name}] {extra}" if extra else f"[{name}]"

    if message.text:
        return message.text
    if message.voice:
        return "[voice message]"
    if message.video_note:
        return "[video message]"
    if message.sticker:
        return tag("sticker", message.sticker.emoji or "")
    if message.photo:
        return tag("photo", caption)
    if message.animation:
        return tag("GIF", caption)
    if message.video:
        return tag("video", caption)
    if message.audio:
        title = " - ".join(p for p in (message.audio.performer, message.audio.title) if p)
        return tag("audio", " ".join(p for p in (title, caption) if p))
    if message.document:
        return tag("file " + (message.document.file_name or ""), caption)
    if message.location:
        return "[location]"
    if message.contact:
        return "[contact]"
    if message.poll:
        return tag("poll", message.poll.question)
    if message.dice:
        return tag("dice", message.dice.emoji)
    return None


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if isinstance(rights, dict):  # older aiogram keeps unknown fields as plain dicts
        return bool(rights.get("can_reply", False))
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Path) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def profile_is_filled(text: str) -> bool:
    """True when the profile has anything beyond the empty template."""
    for line in _COMMENT_RE.sub("", text).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or _EMPTY_FIELD_RE.match(line):
            continue
        return True
    return False


def _chat_name(message: Message) -> str:
    chat = message.chat
    if chat.title:
        return chat.title
    name = " ".join(p for p in (chat.first_name, chat.last_name) if p)
    if name:
        return name
    if chat.username:
        return "@" + chat.username
    return str(chat.id)


def _sender_name(message: Message) -> str:
    user = message.from_user
    if user is None:
        return _chat_name(message)
    return user.full_name or (user.username or str(user.id))


def split_text(text: str, size: int = MAX_CHUNK) -> list:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.previews_path = data_dir / "previews.jsonl"
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.connections: Dict[str, BusinessConnection] = {}
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._takeover: Dict[Tuple[str, int], float] = {}
        self._locks: Dict[Tuple[str, int], asyncio.Lock] = {}
        self._last_error_notice: Optional[float] = None

    # ----------------------------------------------------------------- helpers
    def _lock(self, key: Tuple[str, int]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _done(self, outcome: Outcome) -> Outcome:
        self.outcomes.append(outcome)
        return outcome

    def _blocked(self, key: Tuple[str, int]) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.paused
        if self.state.limit_until is not None and self.state.limit_until > now:
            return Outcome.limited
        until = self._takeover.get(key)
        if until is not None and until > now:
            return Outcome.owner_active
        return None

    def _check_connection(self, connection: BusinessConnection) -> Optional[Outcome]:
        if connection.user.id != self.settings.owner_id:
            return Outcome.foreign_connection
        if not connection.is_enabled:
            return Outcome.connection_disabled
        return None

    def _show_preview(self, preview: Preview) -> None:
        try:
            self.preview_sink(preview)
        except Exception:  # pragma: no cover - logging must never break the flow
            log.exception("Preview sink failed")
        record = asdict(preview)
        record["ts"] = self.clock()
        try:
            append_private_line(self.previews_path, json.dumps(record, ensure_ascii=False))
        except OSError:
            log.exception("Could not write %s", self.previews_path)

    async def _notify_owner(self, bot: Bot, text: str) -> None:
        if not self.settings.auto_send or self.settings.owner_id is None:
            return
        try:
            await bot.send_message(self.settings.owner_id, text)
        except TelegramAPIError as exc:
            log.warning("Could not notify the owner: %s", exc)

    # ------------------------------------------------------------ connections
    async def on_business_connection(self, connection: BusinessConnection) -> None:
        self.connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to another account (user=%s); it will be ignored",
                connection.id,
                connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id,
            connection.user.id,
            connection.is_enabled,
            can_reply(connection),
        )

    async def _get_connection(self, bot: Bot, connection_id: str) -> Optional[BusinessConnection]:
        cached = self.connections.get(connection_id)
        if cached is not None:
            return cached
        try:
            connection = await bot.get_business_connection(business_connection_id=connection_id)
        except TelegramAPIError as exc:
            log.warning("Could not look up business connection %s: %s", connection_id, exc)
            return None
        self.connections[connection_id] = connection
        return connection

    # --------------------------------------------------------------- messages
    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        connection_id = message.business_connection_id
        if not connection_id:
            return self._done(Outcome.no_connection)

        chat_id = message.chat.id
        # Mark before any await so parallel duplicates are caught.
        if not self.seen.first_time(f"{connection_id}:{chat_id}:{message.message_id}"):
            return self._done(Outcome.duplicate)

        if message.sender_business_bot is not None or (message.from_user is not None and message.from_user.is_bot):
            return self._done(Outcome.ignored_bot)

        connection = await self._get_connection(bot, connection_id)
        if connection is None:
            return self._done(Outcome.no_connection)
        problem = self._check_connection(connection)
        if problem is not None:
            return self._done(problem)

        text = describe(message)
        if text is None:
            return self._done(Outcome.unsupported)

        key = (connection_id, chat_id)
        now = self.clock()
        if message.from_user is not None and message.from_user.id == self.settings.owner_id:
            self.history.add(connection_id, chat_id, role="owner", name="You", text=text, lang=None, ts=now)
            minutes = self.settings.owner_takeover_minutes
            if minutes > 0:
                self._takeover[key] = now + minutes * 60
            return self._done(Outcome.owner_message)

        language = detect_language(message.text or message.caption)
        if language is None:
            language = self.history.last_language(connection_id, chat_id)
        self.history.add(
            connection_id, chat_id, role="other", name=_sender_name(message), text=text, lang=language, ts=now
        )

        async with self._lock(key):
            blocked = self._blocked(key)
            if blocked is not None:
                return self._done(blocked)
            return self._done(await self._reply(bot, message, connection_id, chat_id, text, language))

    async def _reply(
        self,
        bot: Bot,
        message: Message,
        connection_id: str,
        chat_id: int,
        incoming: str,
        language: Optional[str],
    ) -> Outcome:
        key = (connection_id, chat_id)
        system_prompt = build_system_prompt(
            read_profile(self.settings.profile_path), datetime.fromtimestamp(self.clock()).astimezone()
        )
        user_prompt = build_user_prompt(self.history.get(connection_id, chat_id), language)

        try:
            reply = await self.generator.generate(system_prompt, user_prompt)
        except ClaudeLimitError as exc:
            now = self.clock()
            if exc.resets_at is not None and exc.resets_at > now:
                until = exc.resets_at
            else:
                until = now + self.settings.limit_cooldown_minutes * 60
            self.state.set_limit_until(until)
            when = datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            log.warning("%s. No replies until %s. Detail: %s", exc, when, exc.detail)
            await self._notify_owner(bot, f"Assistant paused: {exc}. Replies resume after {when}.")
            return Outcome.limited
        except ClaudeError as exc:
            log.error("Claude failed for chat %s: %s", chat_id, exc)
            now = self.clock()
            if self._last_error_notice is None or now - self._last_error_notice >= ERROR_NOTICE_INTERVAL:
                if self.settings.auto_send:
                    self._last_error_notice = now
                await self._notify_owner(bot, f"Assistant could not draft a reply: {exc}")
            return Outcome.error
        except Exception:
            log.exception("Unexpected error while drafting a reply for chat %s", chat_id)
            return Outcome.error

        reply = (reply or "").strip()
        if not reply or reply == SKIP_TOKEN or reply.startswith(SKIP_TOKEN):
            log.info("No reply needed for chat %s", chat_id)
            return Outcome.skipped

        preview = Preview(
            connection_id=connection_id,
            chat_id=chat_id,
            chat_name=_chat_name(message),
            language=language,
            incoming=incoming,
            reply=reply,
        )

        if not self.settings.auto_send:
            self._show_preview(preview)
            return Outcome.previewed

        # Auto-send: re-check everything, the world may have changed while drafting.
        blocked = self._blocked(key)
        if blocked is not None:
            preview.note = {
                Outcome.owner_active: "not sent: the owner wrote in this chat while the reply was drafted",
                Outcome.paused: "not sent: the assistant was paused",
                Outcome.limited: "not sent: Claude usage limit",
            }[blocked]
            self._show_preview(preview)
            return blocked

        try:
            fresh = await bot.get_business_connection(business_connection_id=connection_id)
        except TelegramAPIError as exc:
            preview.note = f"not sent: could not re-check the business connection ({exc})"
            self._show_preview(preview)
            return Outcome.permission_denied
        self.connections[connection_id] = fresh
        reason = None
        if fresh.user.id != self.settings.owner_id:
            reason = "the connection belongs to another account"
        elif not fresh.is_enabled:
            reason = "the business connection is disabled"
        elif not can_reply(fresh):
            reason = "the bot has no permission to reply in this chat"
        if reason:
            preview.note = "not sent: " + reason
            self._show_preview(preview)
            return Outcome.permission_denied

        try:
            for chunk in split_text(reply):
                await bot.send_message(chat_id, chunk, business_connection_id=connection_id)
        except TelegramAPIError as exc:
            log.error("Sending the reply to chat %s failed: %s", chat_id, exc)
            return Outcome.error

        self.history.add(connection_id, chat_id, role="owner", name="You", text=reply, lang=None, ts=self.clock())
        log.info("Reply sent to chat %s (%s)", chat_id, _chat_name(message))
        return Outcome.sent

    # --------------------------------------------------------- owner controls
    def pause(self) -> None:
        self.state.set_paused(True)

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self.state.save()
        self._takeover.clear()

    def _limit_text(self) -> Optional[str]:
        until = self.state.limit_until
        if until is None or until <= self.clock():
            return None
        return datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    def status_text(self) -> str:
        if self.settings.auto_send:
            mode = "AUTO-SEND (replies are sent from your account)"
        else:
            mode = "PREVIEW (replies are only shown in the logs)"
        state = "paused" if self.state.paused else "running"
        limit = self._limit_text()
        lines = [
            f"Mode: {mode}",
            f"State: {state}",
            f"Claude usage limit: until {limit}" if limit else "Claude usage limit: none",
            f"Owner takeover: {self.settings.owner_takeover_minutes} min",
            f"Chats in history: {self.history.chat_count()}",
        ]
        return "\n".join(lines)

    def start_text(self) -> str:
        if self.settings.auto_send:
            mode = "AVTO-YUBORISH: javoblar sizning nomingizdan yuboriladi"
        else:
            mode = "PREVIEW: javoblar faqat logda ko'rsatiladi, hech kimga yuborilmaydi"
        state = "pauzada (/resume bilan yoqing)" if self.state.paused else "ishlayapti"
        limit = self._limit_text()
        minutes = self.settings.owner_takeover_minutes
        if minutes > 0:
            takeover = f"Chatda o'zingiz yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi."
        else:
            takeover = "Chatda o'zingiz yozsangiz ham bot jim turmaydi (OWNER_TAKEOVER_MINUTES=0)."
        lines = [
            "Salom! Men sizning shaxsiy yordamchingizman.",
            "Sizga kimdir yozsa, profile.md asosida sizning uslubingizda javob tayyorlayman.",
            "",
            "Hozirgi holat:",
            f"• Rejim: {mode}",
            f"• Holat: {state}",
        ]
        if limit:
            lines.append(f"• Claude limiti: {limit} gacha javob bermayman")
        lines += [
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots",
            "2. Shu botning username'ini kiriting",
            "3. Bot qaysi chatlarni ko'rishini tanlang",
            "4. Javob berish ruxsatini yoqing",
            "Buning uchun Telegram Premium kerak.",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — qayta yoqish (limit va jimlik taymerlari ham tozalanadi)",
            "/status — holat (ingliz tilida)",
            "",
            "Bilib qo'ying:",
            f"• {takeover}",
            "• Pul, qarz, uchrashuv va rejalar bo'yicha hech narsaga rozilik bermaydi.",
            "• profile.md ni to'ldirish kerak, aks holda javoblar umumiy bo'ladi.",
        ]
        if not self.settings.auto_send:
            lines.append("• Javoblar haqiqatan yuborilishi uchun AUTO_SEND=true qiling.")
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.claude_workdir,
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    generator = kwargs.pop("generator", None)
    if generator is None:
        generator = make_claude_cli(settings)
    return Assistant(settings, generator, **kwargs)
