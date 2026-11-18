"""Userbot mode: the assistant runs inside your own Telegram account (Telethon).

Unlike the Business bot this needs no Telegram Premium: it logs in with your phone
number like a new device (Settings -> Devices shows it) and sees your private chats.

The core logic (``UserbotAssistant``) does not import Telethon; it talks to a small
``Transport`` so it can be tested offline. ``run_userbot`` wires it to Telethon.

Safety rules on top of the Business bot ones:

* only private chats with real people; bots, Telegram service accounts, groups and
  channels are ignored;
* by default only people in your contacts get a reply (``USERBOT_CONTACTS_ONLY``);
* at most ``USERBOT_MAX_REPLIES_PER_HOUR`` replies are sent per hour, and a Telegram
  FloodWait stops sending until it is over;
* before sending, the chat is marked as read and "typing..." is shown for a few
  seconds, then pause / takeover are checked again;
* owner commands (/pause, /resume, /status, /help) are typed in Saved Messages.
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import platform
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set, Tuple

from .claude_cli import ClaudeError, ClaudeLimitError
from .config import Settings, normalize_name
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .service import (
    ERROR_NOTICE_INTERVAL,
    Assistant,
    Outcome,
    Preview,
    make_claude_cli,
    profile_is_filled,
    read_profile,
    split_text,
)
from .store import JsonFile
from .voice import is_greeting_only

log = logging.getLogger("assistant.userbot")

CONNECTION_ID = "userbot"
# 777000 = "Telegram" (login codes, notifications), 42777 = Telegram support.
TELEGRAM_SERVICE_IDS = frozenset({777000, 42777})
HOUR = 3600
OWN_IDS_CAPACITY = 2000
GREETING_REPEAT_SECONDS = 12 * HOUR
QUICK_PAUSE = 1.0  # "typing..." before a greeting / a fast-contact reply


class SendRateLimited(Exception):
    """Telegram asked us to wait (FloodWait)."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram asked to wait {seconds} seconds")
        self.seconds = seconds


class SendFailed(Exception):
    """Any other error while sending."""


@dataclass
class IncomingMessage:
    """A private-chat message, already converted from Telethon."""

    chat_id: int
    message_id: int
    outgoing: bool  # written from the owner's account (any device)
    text: Optional[str]  # history text, media as [tags]; None for service messages
    raw_text: Optional[str] = None  # plain text / caption, for language detection
    chat_name: str = ""
    sender_name: str = ""
    sender_is_bot: bool = False  # bots and deleted accounts
    is_contact: bool = False  # the other person is in the owner's contacts
    username: str = ""  # the other person's @username, if any


class Transport:
    """What the assistant needs from Telegram. Implemented by ``TelethonTransport``."""

    async def send(self, chat_id: int, text: str, *, link_preview: bool = False) -> int:  # pragma: no cover
        raise NotImplementedError

    async def mark_read(self, chat_id: int) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def typing(self, chat_id: int, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def recording(self, chat_id: int, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send_voice(self, chat_id: int, path: Path, duration: float) -> int:  # pragma: no cover
        raise NotImplementedError

    async def note_to_self(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def typing_seconds(text: str) -> float:
    """How long "typing..." is shown before a reply: 2-8 seconds by length."""
    return min(2.0 + len(text) / 20.0, 8.0)


HELP_TEXT = """Yordamchi buyruqlari (shu "Saved Messages" chatiga yozing):
/pause — javob berishni to'xtatish
/resume — qayta yoqish (limit va jimlik taymerlari ham tozalanadi)
/status — holat
/hisobot — ertalabki ovozli hisobotni hozir tayyorlab yuborish
/help — shu yordam

Bilib qo'ying:
• Faqat shaxsiy chatlarga javob beriladi; guruh, kanal va botlarga javob berilmaydi.
• Chatda o'zingiz yozsangiz, bot o'sha chatda bir muddat jim turadi.
• Pul, qarz, uchrashuv va rejalar bo'yicha hech narsaga rozilik bermaydi."""


class UserbotAssistant(Assistant):
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        transport: Transport,
        *,
        me_id: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        greeting_voice: Optional[Tuple[Path, float]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(settings, generator, **kwargs)
        self.transport = transport
        self.me_id = me_id
        self.sleep = sleep
        self.greeting_voice = greeting_voice  # (ogg file, seconds) for the fixed greeting
        self.schedule_chat_id: Optional[int] = None  # resolved from USERBOT_SCHEDULE_TO at startup
        self._schedule_file = JsonFile(settings.data_dir / "schedule.json", dict)
        # /hisobot in Saved Messages: builds and sends the morning report now (set by run_userbot).
        self.report_command: Optional[Callable[[], Awaitable[Any]]] = None
        self._report_task: Optional["asyncio.Future[Any]"] = None
        self._latest: Dict[int, int] = {}  # chat -> id of the newest incoming message
        self._own_ids: Set[Tuple[int, int]] = set()
        self._own_order: Deque[Tuple[int, int]] = deque()
        self._pending: Dict[int, List[str]] = {}
        self._sent_times: Deque[float] = deque()
        self._flood_until: Optional[float] = None

    # ----------------------------------------------------------------- helpers
    def _remember_own(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        self._own_ids.add(key)
        self._own_order.append(key)
        while len(self._own_order) > OWN_IDS_CAPACITY:
            self._own_ids.discard(self._own_order.popleft())

    def _is_own_reply(self, msg: IncomingMessage) -> bool:
        if (msg.chat_id, msg.message_id) in self._own_ids:
            return True
        return msg.text is not None and msg.text in self._pending.get(msg.chat_id, [])

    def _replies_last_hour(self) -> int:
        cutoff = self.clock() - HOUR
        while self._sent_times and self._sent_times[0] <= cutoff:
            self._sent_times.popleft()
        return len(self._sent_times)

    async def _notify(self, text: str) -> None:
        if not self.settings.auto_send:
            return
        try:
            await self.transport.note_to_self(text)
        except Exception as exc:  # noqa: BLE001 - a notice must never break the flow
            log.warning("Could not write to Saved Messages: %s", exc)

    def _not_sent(self, preview: Preview, note: str) -> None:
        preview.note = "not sent: " + note
        self._show_preview(preview)

    # --------------------------------------------------------------- messages
    async def on_message(self, msg: IncomingMessage) -> Outcome:
        if msg.chat_id == self.me_id:
            return await self._saved_messages(msg)

        if not self.seen.first_time(f"{CONNECTION_ID}:{msg.chat_id}:{msg.message_id}"):
            return self._done(Outcome.duplicate)
        if self._is_ignored(msg):
            return self._done(Outcome.ignored_contact)

        key = (CONNECTION_ID, msg.chat_id)
        if msg.outgoing:
            if self._is_own_reply(msg):
                return self._done(Outcome.ignored_bot)
            if msg.text is None:
                return self._done(Outcome.unsupported)
            now = self.clock()
            self.history.add(CONNECTION_ID, msg.chat_id, role="owner", name="You", text=msg.text, lang=None, ts=now)
            minutes = self.settings.owner_takeover_minutes
            if minutes > 0:
                self._takeover[key] = now + minutes * 60
            return self._done(Outcome.owner_message)

        if msg.sender_is_bot or msg.chat_id in TELEGRAM_SERVICE_IDS:
            return self._done(Outcome.ignored_bot)
        if self.settings.userbot_contacts_only and not msg.is_contact:
            return self._done(Outcome.not_contact)
        if msg.text is None:
            return self._done(Outcome.unsupported)

        language = detect_language(msg.raw_text)
        if language is None:
            language = self.history.last_language(CONNECTION_ID, msg.chat_id)
        self.history.add(
            CONNECTION_ID,
            msg.chat_id,
            role="other",
            name=msg.sender_name or msg.chat_name,
            text=msg.text,
            lang=language,
            ts=self.clock(),
        )

        # Reply delay: give the owner time to answer first; when several messages
        # arrive in a row, only the newest one gets a (single) reply to all of them.
        # A greeting is answered at once: the customer must not wait for the menu.
        self._latest[msg.chat_id] = msg.message_id
        delay = self.settings.userbot_reply_delay_seconds
        if delay > 0 and not self._should_greet(msg) and not self._is_fast(msg):
            await self.sleep(delay)
            if self._latest.get(msg.chat_id) != msg.message_id:
                return self._done(Outcome.superseded)

        async with self._lock(key):
            blocked = self._blocked(key)
            if blocked is not None:
                return self._done(blocked)
            if self._should_greet(msg):
                return self._done(await self._greet(msg, language))
            return self._done(await self._reply_to(msg, language))

    # ------------------------------------------------------ fast contacts, intro
    @staticmethod
    def _matches(msg: IncomingMessage, names: Tuple[str, ...]) -> bool:
        """Saved contact name, @username or numeric id is in ``names`` (already normalized)."""
        if not names:
            return False
        return (
            normalize_name(msg.chat_name) in names
            or str(msg.chat_id) in names
            or (bool(msg.username) and normalize_name(msg.username) in names)
        )

    def _is_fast(self, msg: IncomingMessage) -> bool:
        """Contacts from USERBOT_FAST_CONTACTS are answered at once (no reply delay)."""
        return self._matches(msg, self.settings.userbot_fast_contacts)

    def _is_ignored(self, msg: IncomingMessage) -> bool:
        """Contacts from USERBOT_IGNORE_CONTACTS never get a reply."""
        return self._matches(msg, self.settings.userbot_ignore_contacts)

    # ------------------------------------------------------- scheduled message
    async def scheduled_tick(self) -> Optional[Outcome]:
        """Send USERBOT_SCHEDULE_TEXT to the scheduled chat when it is due. Call it every ~30 s."""
        s = self.settings
        if self.schedule_chat_id is None or not s.userbot_schedule_text or s.userbot_schedule_every_minutes <= 0:
            return None
        now = self.clock()
        state = self._schedule_file.read()
        last = state.get("last") if isinstance(state, dict) and state.get("to") == self.schedule_chat_id else None
        if isinstance(last, (int, float)) and now - last < s.userbot_schedule_every_minutes * 60:
            return None
        if self.state.paused:
            return Outcome.paused
        if self._flood_until is not None and self._flood_until > now:
            return Outcome.rate_limited
        chat_id = self.schedule_chat_id
        text = s.userbot_schedule_text
        self._pending[chat_id] = [text]
        try:
            message_id = await self.transport.send(chat_id, text)
            self._remember_own(chat_id, message_id)
        except SendRateLimited as exc:
            self._flood_until = now + exc.seconds
            log.warning("Telegram FloodWait: scheduled message postponed for %s seconds", exc.seconds)
            return Outcome.rate_limited
        except SendFailed as exc:
            log.error("Scheduled message to chat %s failed: %s", chat_id, exc)
            return Outcome.error
        finally:
            self._pending.pop(chat_id, None)
        self._schedule_file.write({"to": chat_id, "last": now})
        self.history.add(CONNECTION_ID, chat_id, role="owner", name="You", text=text, lang=None, ts=now)
        log.info("Scheduled message sent to chat %s", chat_id)
        return Outcome.sent

    def _needs_intro(self, chat_id: int) -> bool:
        """The intro goes before the bot's first message in a chat (again after 12 hours)."""
        intro = self.settings.userbot_intro_text
        if not intro:
            return False
        cutoff = self.clock() - GREETING_REPEAT_SECONDS
        for item in self.history.get(CONNECTION_ID, chat_id):
            if (
                item.get("role") == "owner"
                and str(item.get("text") or "").startswith(intro)
                and float(item.get("ts") or 0) > cutoff
            ):
                return False
        return True

    # ---------------------------------------------------------------- greeting
    def _should_greet(self, msg: IncomingMessage) -> bool:
        """A bare "Salom" / "Assalomu alaykum" gets the fixed greeting, once per 12 hours per chat."""
        text = self.settings.userbot_greeting_text
        if not text or not is_greeting_only(msg.raw_text):
            return False
        cutoff = self.clock() - GREETING_REPEAT_SECONDS
        for item in self.history.get(CONNECTION_ID, msg.chat_id):
            if (
                item.get("role") == "owner"
                and str(item.get("text") or "").startswith(text)
                and float(item.get("ts") or 0) > cutoff
            ):
                return False
        return True

    async def _greet(self, msg: IncomingMessage, language: Optional[str]) -> Outcome:
        greeting = self.settings.userbot_greeting_text or ""
        followup = self.settings.userbot_greeting_followup
        mode = self.settings.userbot_greeting_mode
        voice = self.greeting_voice if mode in ("voice", "both") else None
        greeting_as_text = mode in ("text", "both") or voice is None  # no voice file -> text
        parts = ([greeting] if greeting_as_text else []) + ([followup] if followup else [])
        text = "\n".join(parts) or None
        full = "\n".join([greeting] + ([followup] if followup else []))
        preview = Preview(
            connection_id=CONNECTION_ID,
            chat_id=msg.chat_id,
            chat_name=msg.chat_name or str(msg.chat_id),
            language=language,
            incoming=msg.text or "",
            reply=("[voice] " if voice is not None else "") + full,
            note="fixed greeting",
        )
        return await self._deliver(
            msg,
            preview,
            text=text,
            voice=voice,
            history_text=full,
            force=self.settings.userbot_greeting_send,
            quick=True,
            link_preview=True,
        )

    # ------------------------------------------------------------------ Claude
    async def _reply_to(self, msg: IncomingMessage, language: Optional[str]) -> Outcome:
        chat_id = msg.chat_id
        system_prompt = build_system_prompt(
            read_profile(self.settings.profile_path),
            datetime.fromtimestamp(self.clock()).astimezone(),
            persona="assistant" if self.settings.userbot_intro_text else "owner",
        )
        user_prompt = build_user_prompt(self.history.get(CONNECTION_ID, chat_id), language)

        try:
            reply = await self.generator.generate(system_prompt, user_prompt)
        except ClaudeLimitError as exc:
            now = self.clock()
            until = exc.resets_at if exc.resets_at is not None and exc.resets_at > now else (
                now + self.settings.limit_cooldown_minutes * 60
            )
            self.state.set_limit_until(until)
            when = datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            log.warning("%s. No replies until %s. Detail: %s", exc, when, exc.detail)
            await self._notify(f"Yordamchi to'xtadi: {exc}. {when} dan keyin davom etadi.")
            return Outcome.limited
        except ClaudeError as exc:
            log.error("Claude failed for chat %s: %s", chat_id, exc)
            now = self.clock()
            if self._last_error_notice is None or now - self._last_error_notice >= ERROR_NOTICE_INTERVAL:
                if self.settings.auto_send:
                    self._last_error_notice = now
                await self._notify(f"Yordamchi javob tayyorlay olmadi: {exc}")
            return Outcome.error
        except Exception:
            log.exception("Unexpected error while drafting a reply for chat %s", chat_id)
            return Outcome.error

        reply = (reply or "").strip()
        if not reply or reply.startswith(SKIP_TOKEN):
            log.info("No reply needed for chat %s", chat_id)
            return Outcome.skipped

        preview = Preview(
            connection_id=CONNECTION_ID,
            chat_id=chat_id,
            chat_name=msg.chat_name or str(chat_id),
            language=language,
            incoming=msg.text or "",
            reply=reply,
        )
        return await self._deliver(
            msg, preview, text=reply, voice=None, history_text=reply, quick=self._is_fast(msg)
        )

    # ---------------------------------------------------------------- sending
    async def _deliver(
        self,
        msg: IncomingMessage,
        preview: Preview,
        *,
        text: Optional[str],
        voice: Optional[Tuple[Path, float]],
        history_text: str,
        force: bool = False,
        quick: bool = False,
        link_preview: bool = False,
    ) -> Outcome:
        """Preview, or (auto-send / ``force``) re-check, mark read, show typing and send.

        ``force`` sends even in PREVIEW mode (the fixed greeting); ``quick`` keeps the
        "typing..." pause short so the customer does not wait.
        """
        chat_id = msg.chat_id
        key = (CONNECTION_ID, chat_id)
        if self._needs_intro(chat_id):
            intro = self.settings.userbot_intro_text or ""
            text = f"{intro}\n{text}" if text else intro
            history_text = f"{intro}\n{history_text}"
            preview.reply = f"{intro}\n{preview.reply}"
        if not self.settings.auto_send and not force:
            self._show_preview(preview)
            return Outcome.previewed

        problem = self._send_problem(key)
        if problem is not None:
            outcome, note = problem
            self._not_sent(preview, note)
            return outcome

        # Look like a person: read the chat, "typing..." / "recording...", then check again.
        try:
            await self.transport.mark_read(chat_id)
            if voice is not None and text is None:
                await self.transport.recording(chat_id, QUICK_PAUSE if quick else min(max(voice[1], 2.0), 8.0))
            else:
                await self.transport.typing(chat_id, QUICK_PAUSE if quick else typing_seconds(text or ""))
        except Exception as exc:  # noqa: BLE001 - cosmetic steps must not block the reply
            log.warning("Could not mark chat %s as read / show typing: %s", chat_id, exc)
        problem = self._send_problem(key)
        if problem is not None:
            outcome, note = problem
            self._not_sent(preview, note)
            return outcome

        chunks = split_text(text) if text else []
        self._pending[chat_id] = list(chunks)
        try:
            if voice is not None:
                message_id = await self.transport.send_voice(chat_id, voice[0], voice[1])
                self._remember_own(chat_id, message_id)
                self._sent_times.append(self.clock())
            for chunk in chunks:
                message_id = await self.transport.send(chat_id, chunk, link_preview=link_preview)
                self._remember_own(chat_id, message_id)
                self._sent_times.append(self.clock())
        except SendRateLimited as exc:
            self._flood_until = self.clock() + exc.seconds
            log.warning("Telegram FloodWait: no replies for %s seconds", exc.seconds)
            await self._notify(f"Telegram cheklov qo'ydi: {exc.seconds} soniya javob yuborilmaydi.")
            return Outcome.rate_limited
        except SendFailed as exc:
            log.error("Sending the reply to chat %s failed: %s", chat_id, exc)
            return Outcome.error
        finally:
            self._pending.pop(chat_id, None)

        self.history.add(
            CONNECTION_ID, chat_id, role="owner", name="You", text=history_text, lang=None, ts=self.clock()
        )
        log.info("Reply sent to chat %s (%s)%s", chat_id, preview.chat_name, " [voice]" if voice else "")
        return Outcome.sent

    def _send_problem(self, key: Tuple[str, int]) -> Optional[Tuple[Outcome, str]]:
        blocked = self._blocked(key)
        if blocked is not None:
            return blocked, {
                Outcome.owner_active: "the owner wrote in this chat while the reply was drafted",
                Outcome.paused: "the assistant was paused",
                Outcome.limited: "Claude usage limit",
            }[blocked]
        now = self.clock()
        if self._flood_until is not None and self._flood_until > now:
            return Outcome.rate_limited, "Telegram asked to slow down (FloodWait)"
        limit = self.settings.userbot_max_replies_per_hour
        if self._replies_last_hour() >= limit:
            return Outcome.rate_limited, f"limit of {limit} replies per hour reached"
        return None

    # --------------------------------------------------------- owner controls
    async def _saved_messages(self, msg: IncomingMessage) -> Outcome:
        """Commands typed by the owner in Saved Messages."""
        words = (msg.raw_text or "").strip().split()
        command = words[0].lower() if words else ""
        # Only the owner can write in Saved Messages, so the sender is not checked.
        if command not in ("/pause", "/resume", "/status", "/help", "/hisobot"):
            return self._done(Outcome.unsupported)
        if command == "/hisobot":
            if self.report_command is None:
                answer = "Ertalabki hisobot sozlanmagan (.env: DAILY_REPORT_TIME, DAILY_REPORT_GROUP)."
            elif self._report_task is not None and not self._report_task.done():
                answer = "Hisobot hozir tayyorlanyapti, biroz kuting."
            else:
                self._report_task = asyncio.ensure_future(self._run_report_command())
                answer = "Hisobot tayyorlanyapti (1-2 daqiqa)..."
        elif command == "/pause":
            self.pause()
            log.info("Paused by the owner")
            answer = "To'xtatildi. Qayta yoqish uchun /resume yozing."
        elif command == "/resume":
            self.resume()
            log.info("Resumed by the owner")
            answer = "Yoqildi.\n\n" + self.status_text()
        elif command == "/status":
            answer = self.status_text()
        else:
            answer = HELP_TEXT
        try:
            await self.transport.note_to_self(answer)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not answer in Saved Messages: %s", exc)
        return self._done(Outcome.owner_command)

    async def _run_report_command(self) -> None:
        assert self.report_command is not None
        try:
            await self.report_command()
        except Exception as exc:  # noqa: BLE001 - tell the owner instead of failing silently
            log.warning("/hisobot failed: %s: %s", type(exc).__name__, exc)
            try:
                await self.transport.note_to_self(f"Hisobotni tayyorlab bo'lmadi: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def resume(self) -> None:
        super().resume()
        self._flood_until = None

    def status_text(self) -> str:
        lines = [super().status_text()]
        lines.append(
            "Replies to: " + ("contacts only" if self.settings.userbot_contacts_only else "everyone in private chats")
        )
        lines.append(
            f"Replies sent in the last hour: {self._replies_last_hour()} / {self.settings.userbot_max_replies_per_hour}"
        )
        lines.append(f"Reply delay: {self.settings.userbot_reply_delay_seconds} s")
        if self._flood_until is not None and self._flood_until > self.clock():
            when = datetime.fromtimestamp(self._flood_until).astimezone().strftime("%H:%M:%S")
            lines.append(f"Telegram FloodWait until {when}")
        return "\n".join(lines)


def build_userbot_assistant(settings: Settings, transport: Transport, *, me_id: int, **kwargs: Any) -> UserbotAssistant:
    generator = kwargs.pop("generator", None)
    if generator is None:
        generator = make_claude_cli(settings)
    return UserbotAssistant(settings, generator, transport, me_id=me_id, **kwargs)


# ======================================================================= Telethon
def _entity_name(entity: Any) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    name = " ".join(p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p)
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return "@" + username
    return str(getattr(entity, "id", ""))


def describe_telethon(message: Any) -> Optional[str]:
    """Text for the history from a Telethon message; media gets a short tag."""
    if getattr(message, "action", None) is not None:
        return None
    caption = (getattr(message, "message", None) or "").strip()

    def tag(name: str, extra: str = "") -> str:
        extra = (extra or "").strip()
        return f"[{name}] {extra}" if extra else f"[{name}]"

    file = getattr(message, "file", None)
    if getattr(message, "voice", None):
        return "[voice message]"
    if getattr(message, "video_note", None):
        return "[video message]"
    if getattr(message, "sticker", None):
        return tag("sticker", getattr(file, "emoji", "") or "")
    if getattr(message, "photo", None):
        return tag("photo", caption)
    if getattr(message, "gif", None):
        return tag("GIF", caption)
    if getattr(message, "video", None):
        return tag("video", caption)
    if getattr(message, "audio", None):
        title = " - ".join(p for p in (getattr(file, "performer", None), getattr(file, "title", None)) if p)
        return tag("audio", " ".join(p for p in (title, caption) if p))
    if getattr(message, "document", None):
        return tag("file " + (getattr(file, "name", None) or ""), caption)
    if getattr(message, "geo", None):
        return "[location]"
    if getattr(message, "contact", None):
        return "[contact]"
    poll = getattr(message, "poll", None)
    if poll is not None:
        question = getattr(getattr(poll, "poll", None), "question", "")
        return tag("poll", str(getattr(question, "text", question) or ""))
    dice = getattr(message, "dice", None)
    if dice is not None:
        return tag("dice", getattr(dice, "emoticon", ""))
    return caption or None


async def incoming_from_event(event: Any) -> IncomingMessage:
    message = event.message
    chat = await event.get_chat()  # in a private chat: the other person (or yourself)
    sender = await event.get_sender()
    return IncomingMessage(
        chat_id=int(event.chat_id),
        message_id=int(message.id),
        outgoing=bool(message.out),
        text=describe_telethon(message),
        raw_text=(message.message or None),
        chat_name=_entity_name(chat),
        sender_name=_entity_name(sender) or _entity_name(chat),
        sender_is_bot=bool(getattr(chat, "bot", False) or getattr(chat, "deleted", False) or getattr(chat, "support", False)),
        is_contact=bool(getattr(chat, "contact", False)),
        username=getattr(chat, "username", None) or "",
    )


class TelethonTransport(Transport):
    def __init__(self, client: Any) -> None:
        self.client = client

    async def send(self, chat_id: int, text: str, *, link_preview: bool = False) -> int:
        from telethon import errors

        try:
            message = await self.client.send_message(chat_id, text, link_preview=link_preview)
        except errors.FloodWaitError as exc:
            raise SendRateLimited(int(exc.seconds)) from None
        except errors.RPCError as exc:
            raise SendFailed(str(exc)) from None
        return int(message.id)

    async def mark_read(self, chat_id: int) -> None:
        await self.client.send_read_acknowledge(chat_id)

    async def typing(self, chat_id: int, seconds: float) -> None:
        async with self.client.action(chat_id, "typing"):
            await asyncio.sleep(seconds)

    async def recording(self, chat_id: int, seconds: float) -> None:
        async with self.client.action(chat_id, "record-audio"):
            await asyncio.sleep(seconds)

    async def send_voice(self, chat_id: int, path: Path, duration: float) -> int:
        from telethon import errors, types

        try:
            message = await self.client.send_file(
                chat_id,
                str(path),
                voice_note=True,
                attributes=[types.DocumentAttributeAudio(duration=max(1, round(duration)), voice=True)],
            )
        except errors.FloodWaitError as exc:
            raise SendRateLimited(int(exc.seconds)) from None
        except errors.RPCError as exc:
            raise SendFailed(str(exc)) from None
        return int(message.id)

    async def note_to_self(self, text: str) -> None:
        for chunk in split_text(text):
            await self.client.send_message("me", chunk, link_preview=False)


def code_destination(sent: Any) -> str:
    """Uzbek explanation of where Telegram sent the login code (auth.SentCode)."""
    kind = type(getattr(sent, "type", None)).__name__
    sent_type = getattr(sent, "type", None)
    length = getattr(sent_type, "length", None)
    digits = f" ({length} xonali)" if length else ""
    if kind == "SentCodeTypeApp":
        text = (
            f"Kod{digits} Telegram ILOVANGIZGA yuborildi: telefoningiz yoki kompyuteringizdagi Telegram'da "
            "ko'k belgili rasmiy \"Telegram\" chatini oching (Saqlangan xabarlar emas)."
        )
    elif kind == "SentCodeTypeEmailCode":
        pattern = getattr(sent_type, "email_pattern", "") or "login email"
        text = f"Kod{digits} EMAILINGIZGA yuborildi: {pattern}. Pochtani (Spam papkasini ham) tekshiring."
    elif kind == "SentCodeTypeSetUpEmailRequired":
        text = (
            "Telegram login uchun email talab qilyapti. Rasmiy Telegram ilovasida "
            "Sozlamalar -> Maxfiylik va xavfsizlik -> Login email ni sozlang, keyin qayta urinib ko'ring."
        )
    elif kind in ("SentCodeTypeSms", "SentCodeTypeFirebaseSms", "SentCodeTypeSmsWord", "SentCodeTypeSmsPhrase"):
        text = f"Kod{digits} SMS orqali telefon raqamingizga yuborildi."
    elif kind == "SentCodeTypeFragmentSms":
        url = getattr(sent_type, "url", "") or "fragment.com"
        text = f"Kod{digits} Fragment orqali yuborildi: {url}"
    elif kind in ("SentCodeTypeCall", "SentCodeTypeFlashCall", "SentCodeTypeMissedCall"):
        text = f"Kod{digits} QO'NG'IROQ orqali keladi: qo'ng'iroq qilgan raqamning oxirgi raqamlari yoki aytilgan kod."
    else:
        text = f"Kod yuborildi ({kind})."
    next_kind = type(getattr(sent, "next_type", None)).__name__ if getattr(sent, "next_type", None) else ""
    if next_kind:
        how = {
            "CodeTypeSms": "SMS orqali",
            "CodeTypeCall": "qo'ng'iroq orqali",
            "CodeTypeFlashCall": "qo'ng'iroq orqali",
            "CodeTypeMissedCall": "qo'ng'iroq orqali",
            "CodeTypeFragmentSms": "Fragment orqali",
        }.get(next_kind, "boshqa usulda")
        timeout = getattr(sent, "timeout", None)
        wait = f" ({timeout} soniyadan keyin)" if timeout else ""
        text += f"\nKelmasa, kodni bo'sh qoldirib Enter bosing: kod {how} qayta yuboriladi{wait}."
    return text


QR_STEPS = (
    "Telefoningizdagi Telegram'da: Sozlamalar -> Qurilmalar -> \"Kompyuterni ulash\" "
    "(Link Desktop Device) -> brauzerda ochilgan QR kodni skanerlang."
)


def qr_page(svg: Optional[str], message: str) -> str:
    """Local HTML page with the login QR; reloads itself every 3 seconds while waiting."""
    refresh = '<meta http-equiv="refresh" content="3">' if svg else ""
    body = f'<div class="qr">{svg}</div>' if svg else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{refresh}<title>Telegram login</title>"
        "<style>body{font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#111;"
        "text-align:center;padding:24px}.qr svg{width:320px;height:320px}"
        "p{max-width:520px;margin:12px auto;font-size:17px}</style></head><body>"
        f"<h2>Telegram login</h2>{body}<p>{message}</p></body></html>"
    )


def write_qr_page(path: Any, url: Optional[str], message: str) -> None:
    svg = None
    if url:
        import qrcode
        import qrcode.image.svg

        svg = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2).to_string(encoding="unicode")
    path.write_text(qr_page(svg, message), encoding="utf-8")


async def qr_login(
    client: Any,
    page: Any,
    *,
    ask_secret: Callable[[str], str] = getpass.getpass,
    show: Callable[[str], None] = print,
    open_page: Callable[[str], None] = lambda p: os.startfile(p),  # type: ignore[attr-defined]
    rounds: int = 10,
) -> None:
    """Log in by scanning a QR code with the Telegram app on your phone (no code needed)."""
    from telethon import errors

    qr = await client.qr_login()
    opened = False
    try:
        for _ in range(rounds):
            write_qr_page(page, qr.url, QR_STEPS + " QR kod har 30 soniyada yangilanadi.")
            if not opened:
                open_page(str(page))
                opened = True
                show(f"QR kod brauzerda ochildi ({page}). {QR_STEPS}")
            try:
                await qr.wait()
                return
            except asyncio.TimeoutError:
                await qr.recreate()
            except errors.SessionPasswordNeededError:
                write_qr_page(page, None, "QR skanerlandi. Endi login oynasiga 2FA parolni yozing.")
                await _password_login(client, ask_secret=ask_secret, show=show)
                return
        raise asyncio.TimeoutError("QR code was not scanned in time")
    finally:
        # The token is useless after login, but do not leave it on disk.
        write_qr_page(page, None, "Bu sahifani yopishingiz mumkin.")


async def _password_login(
    client: Any, *, ask_secret: Callable[[str], str], show: Callable[[str], None]
) -> None:
    from telethon import errors

    for attempt in range(3):
        password = ask_secret("Ikki bosqichli (2FA) parol: ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            if attempt == 2:
                raise
            show("Parol noto'g'ri. Qayta yozing.")


async def interactive_login(
    client: Any,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    show: Callable[[str], None] = print,
    qr_page_path: Any = None,
    open_page: Optional[Callable[[str], None]] = None,
) -> None:
    """Log in: QR code (default) or phone number + code. The owner types everything secret."""
    from telethon import errors

    if await client.is_user_authorized():
        return
    if qr_page_path is not None:
        choice = ask("Login usuli: 1 = QR kod (tavsiya), 2 = telefon raqam va kod [1]: ").strip()
        if choice != "2":
            kwargs = {"open_page": open_page} if open_page is not None else {}
            await qr_login(client, qr_page_path, ask_secret=ask_secret, show=show, **kwargs)
            return
    phone = ""
    while not phone:
        phone = ask("Telefon raqamingiz (+998...): ").strip()
    sent = await client.send_code_request(phone)
    show(code_destination(sent))
    bad_codes = 0
    while True:
        code = ask("Kod (kelmasa bo'sh qoldirib Enter bosing): ").strip().replace(" ", "")
        if not code:
            try:
                sent = await client.send_code_request(phone)  # second call = resend via the next method
            except errors.SendCodeUnavailableError:
                show("Telegram kodni boshqa usulda yubora olmaydi. Birinchi kelgan kodni kuting.")
                continue
            show(code_destination(sent))
            continue
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            break
        except errors.PhoneCodeInvalidError:
            bad_codes += 1
            if bad_codes >= 3:
                raise
            show("Kod noto'g'ri. Qayta yozing.")
    await _password_login(client, ask_secret=ask_secret, show=show)


EXIT_RETRY = 1  # the restart loop (scripts/run-userbot-loop.ps1) starts the bot again
EXIT_NEEDS_PERSON = 2  # settings or login problem: the loop stops


def login_exit_code(exc: BaseException) -> int:
    """No internet / Telegram unreachable is temporary: exit so the loop retries.

    Only real login problems (revoked session, wrong password, ...) stop the loop; retrying
    those would only ask Telegram for new login codes again and again.
    """
    return EXIT_RETRY if isinstance(exc, (OSError, asyncio.TimeoutError)) else EXIT_NEEDS_PERSON


def login_error_text(exc: BaseException) -> str:
    """Plain Uzbek explanation of a login error. Never contains codes or passwords."""
    name = type(exc).__name__
    seconds = getattr(exc, "seconds", None)
    known = {
        "ApiIdInvalidError": "TELEGRAM_API_ID yoki TELEGRAM_API_HASH noto'g'ri. my.telegram.org -> "
        "API development tools sahifasidan ikkalasini qayta nusxalab .env ga yozing.",
        "ApiIdPublishedFloodError": "Bu api_id bloklangan. my.telegram.org da yangi ilova yarating.",
        "PhoneNumberInvalidError": "Telefon raqam noto'g'ri. To'liq yozing: +998XXXXXXXXX.",
        "PhoneNumberBannedError": "Bu telefon raqam Telegram tomonidan bloklangan.",
        "PhoneNumberFloodError": "Bu raqamga juda ko'p kod so'raldi. Birozdan keyin urinib ko'ring.",
        "PhoneCodeInvalidError": "Kod noto'g'ri. Qayta ishga tushiring va yangi kodni yozing.",
        "PhoneCodeExpiredError": "Kod eskirib qoldi (yoki uni boshqa joyga yuborgansiz). "
        "Qayta ishga tushiring va yangi kodni darhol yozing.",
        "PhoneCodeEmptyError": "Kod bo'sh qoldi. Qayta ishga tushiring.",
        "PasswordHashInvalidError": "Ikki bosqichli (2FA) parol noto'g'ri.",
        "SessionPasswordNeededError": "Hisobda 2FA parol bor, uni ham kiritish kerak.",
        "SendCodeUnavailableError": "Telegram kodni boshqa usulda yubora olmaydi. Birozdan keyin qayta urinib ko'ring.",
        "AuthRestartError": "Telegram loginni qaytadan boshlashni so'radi. Qayta ishga tushiring.",
    }
    if name == "FloodWaitError" and seconds:
        return f"Juda ko'p urinish bo'ldi. Telegram {seconds} soniya (~{int(seconds) // 60 + 1} daqiqa) kutishni so'radi."
    if name in known:
        return known[name]
    if isinstance(exc, RuntimeError) and "attempts" in str(exc):
        return "Kod yoki parol bir necha marta noto'g'ri kiritildi. Qayta ishga tushiring."
    if isinstance(exc, asyncio.TimeoutError):
        return "QR kod vaqtida skanerlanmadi. Qayta ishga tushiring."
    if isinstance(exc, (ConnectionError, OSError)):
        return "Telegram serveriga ulanib bo'lmadi. Internetni tekshiring."
    return f"Kutilmagan xato: {name}: {exc}"


def _log_to_file(settings: Settings) -> None:
    """Also write the userbot log to data/userbot.log (data/ is private and not committed)."""
    path = settings.data_dir / "userbot.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


async def _find_contacts(client: Any, wanted: Tuple[str, ...], label: str) -> Dict[str, Dict[int, str]]:
    """Match names / @usernames / ids against private chats and log the result, so typos are visible."""
    found: Dict[str, Dict[int, str]] = {name: {} for name in wanted}
    if not wanted:
        return found
    try:
        async for dialog in client.iter_dialogs(limit=1000):
            entity = dialog.entity
            if getattr(entity, "first_name", None) is None and getattr(entity, "username", None) is None:
                continue  # groups and channels
            keys = (
                normalize_name(_entity_name(entity)),
                normalize_name(getattr(entity, "username", None)),
                str(entity.id),
            )
            for key in keys:
                if key in found:
                    found[key][int(entity.id)] = _entity_name(entity)
    except Exception as exc:  # noqa: BLE001 - only a diagnostic
        log.warning("Could not check %s contacts: %s", label, exc)
        return found
    for name, matches in found.items():
        if matches:
            log.info("%s '%s': %s", label, name, ", ".join(f"{n} (id {i})" for i, n in sorted(matches.items())))
        else:
            log.warning("%s '%s' not found among your chats (check the name)", label, name)
    return found


async def _schedule_loop(assistant: "UserbotAssistant") -> None:
    while True:
        try:
            await assistant.scheduled_tick()
        except Exception:  # noqa: BLE001 - keep the timer alive
            log.exception("Scheduled message failed")
        await asyncio.sleep(30)


ACCOUNT_DATA_FILES = ("history.json", "seen.json", "previews.jsonl", "state.json")


def archive_account_data(data_dir: Path, stamp: str) -> Optional[Path]:
    """Move the old account's chat history etc. to data/archive-<stamp>/ (nothing is deleted)."""
    target = Path(data_dir) / f"archive-{stamp}"
    moved = False
    for name in ACCOUNT_DATA_FILES:
        source = Path(data_dir) / name
        if source.exists():
            target.mkdir(parents=True, exist_ok=True)
            source.replace(target / name)
            moved = True
    return target if moved else None


async def logout_userbot(settings: Settings) -> int:
    """End the userbot session on Telegram, delete the session file, archive the account's data."""
    from telethon import TelegramClient

    logging.getLogger("telethon").setLevel(logging.WARNING)
    _log_to_file(settings)
    session_file = settings.userbot_session.with_suffix(".session")
    client = TelegramClient(str(settings.userbot_session), settings.api_id, settings.api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            name = f"{_entity_name(me)} (id {me.id})"
            await client.log_out()  # removes "Telegram agent" from Settings -> Devices of that account
            log.info("Logged out of %s", name)
        else:
            log.info("No account was logged in.")
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - log_out already disconnected
            pass
    if session_file.exists():
        session_file.unlink()
    archived = archive_account_data(settings.data_dir, datetime.now().strftime("%Y%m%d-%H%M%S"))
    if archived:
        log.info("Old chat history moved to %s", archived)
    log.info("Log in to another account with: python bot.py --userbot-login")
    return 0


async def run_userbot(settings: Settings, *, login_only: bool = False) -> int:
    from telethon import TelegramClient, events

    logging.getLogger("telethon").setLevel(logging.WARNING)
    _log_to_file(settings)
    client = TelegramClient(
        str(settings.userbot_session),
        settings.api_id,
        settings.api_hash,
        device_model="Telegram agent (assistant)",
        system_version=platform.system() or "PC",
        app_version="1.0",
    )
    try:
        # First run: Telegram sends a login code; you type it here yourself.
        await client.connect()
        await interactive_login(
            client,
            show=lambda text: log.info("%s", text),
            qr_page_path=settings.data_dir / "login-qr.html",
        )
    except (EOFError, KeyboardInterrupt):
        log.error("Login cancelled.")
        await client.disconnect()
        return 2
    except Exception as exc:  # noqa: BLE001 - every login error gets a plain explanation
        code = login_exit_code(exc)
        if code == EXIT_RETRY:
            log.warning("No connection to Telegram (%s); will try again: %s", type(exc).__name__, login_error_text(exc))
        else:
            log.error("Login failed (%s): %s", type(exc).__name__, login_error_text(exc))
        await client.disconnect()
        return code
    session_file = settings.userbot_session.with_suffix(".session")
    if not sys.platform.startswith("win") and session_file.exists():
        os.chmod(session_file, 0o600)

    try:
        me = await client.get_me()
        log.info("Logged in as %s (id %s)", _entity_name(me), me.id)
        if login_only:
            log.info("Login saved to %s. Start the assistant with: python bot.py --userbot", session_file)
            return 0

        greeting_voice = None
        if settings.userbot_greeting_text and settings.userbot_greeting_mode in ("voice", "both"):
            try:
                from .voice import ensure_voice

                greeting_voice = await ensure_voice(
                    settings.data_dir, settings.userbot_greeting_text, settings.userbot_voice
                )
            except Exception as exc:  # noqa: BLE001 - fall back to a text greeting
                log.warning("Could not create the greeting voice note (%s); the greeting will be sent as text.", exc)
        assistant = build_userbot_assistant(
            settings, TelethonTransport(client), me_id=me.id, greeting_voice=greeting_voice
        )

        @client.on(events.NewMessage(func=lambda e: e.is_private))
        async def on_new_message(event: Any) -> None:
            try:
                msg = await incoming_from_event(event)
                outcome = await assistant.on_message(msg)
                log.debug("Message %s in chat %s: %s", msg.message_id, msg.chat_id, outcome.value)
            except Exception:
                log.exception("Failed to handle message %s", getattr(event, "id", "?"))

        if settings.auto_send:
            log.warning("Mode: AUTO-SEND. Replies are sent from your account.")
        else:
            log.info("Mode: PREVIEW (replies are only shown in the logs; set AUTO_SEND=true to send)")
        log.info(
            "Replies to: %s; at most %s replies per hour",
            "contacts only" if settings.userbot_contacts_only else "everyone in private chats",
            settings.userbot_max_replies_per_hour,
        )
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Reply delay: %s s", settings.userbot_reply_delay_seconds)
        if settings.userbot_intro_text:
            log.info("Speaks as your assistant; first message in a chat starts with: %s", settings.userbot_intro_text)
        await _find_contacts(client, settings.userbot_fast_contacts, "Fast reply contact")
        await _find_contacts(client, settings.userbot_ignore_contacts, "Never-reply contact")
        schedule_task = None
        if settings.userbot_schedule_to and settings.userbot_schedule_text and settings.userbot_schedule_every_minutes:
            target = (await _find_contacts(client, (settings.userbot_schedule_to,), "Scheduled message to"))[
                settings.userbot_schedule_to
            ]
            if len(target) == 1:
                assistant.schedule_chat_id = next(iter(target))
                log.info(
                    "Every %s min to %s: %s",
                    settings.userbot_schedule_every_minutes,
                    next(iter(target.values())),
                    settings.userbot_schedule_text,
                )
                schedule_task = asyncio.ensure_future(_schedule_loop(assistant))
            elif len(target) > 1:
                log.warning("Scheduled message NOT started: several chats match; use the numeric id")
        report_task = None
        if settings.daily_report_time and settings.daily_report_group:
            from .daily_report import REPORT_EFFORT, build_telethon_reporter, report_loop, resolve_group

            group = await resolve_group(client, settings.daily_report_group)
            if group is None:
                log.warning("Daily report NOT started: group %r not found among your chats", settings.daily_report_group)
            else:
                title = getattr(group, "title", None) or settings.daily_report_group
                reporter = build_telethon_reporter(
                    settings, client, group, title, make_claude_cli(settings, effort=REPORT_EFFORT)
                )
                assistant.report_command = reporter.run_now
                log.info(
                    "Daily voice report from %s%s to Saved Messages: photos from %r, bookings from %s, voice %s",
                    settings.daily_report_time,
                    f" (complete, or with what it has at {settings.daily_report_wait_until})"
                    if settings.daily_report_wait_until
                    else " as soon as all data is there",
                    title,
                    "the CRM" if reporter.fetch_bookings else "NOWHERE (CRM_URL / CRM_LOGIN / CRM_PASSWORD not set)",
                    settings.daily_report_voice,
                )
                report_task = asyncio.ensure_future(report_loop(reporter))
        if settings.userbot_greeting_text:
            log.info(
                "Greeting reply (%s%s, %s, no delay): %s%s",
                settings.userbot_greeting_mode,
                f", voice {settings.userbot_voice}" if greeting_voice else "",
                "SENT even in PREVIEW mode" if settings.userbot_greeting_send else "follows AUTO_SEND",
                settings.userbot_greeting_text,
                f" + {settings.userbot_greeting_followup}" if settings.userbot_greeting_followup else "",
            )
        if assistant.state.paused:
            log.warning("The assistant starts PAUSED. Write /resume in Saved Messages to continue.")
        try:
            profile_text = settings.profile_path.read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
        if not profile_is_filled(profile_text):
            log.warning("%s is not filled in yet; replies will be generic.", settings.profile_path.name)
        log.info("Userbot is running. Commands: /pause /resume /status /hisobot /help in Saved Messages.")
        try:
            await client.run_until_disconnected()
        finally:
            for task in (schedule_task, report_task):
                if task is not None:
                    task.cancel()
        return 0
    finally:
        await client.disconnect()
