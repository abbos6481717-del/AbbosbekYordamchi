"""Userbot mode: core logic with a fake transport, plus config and Telethon helpers. Offline."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import pytest

from assistant.claude_cli import ClaudeLimitError
from assistant.config import ConfigError, load_settings
from assistant.service import Outcome
from assistant.userbot import (
    HELP_TEXT,
    IncomingMessage,
    SendFailed,
    SendRateLimited,
    Transport,
    UserbotAssistant,
    describe_telethon,
    typing_seconds,
)

from .conftest import PROFILE_TEXT, Clock, FakeGenerator

ME = 500
FRIEND = 600
API_HASH = "0123456789abcdef0123456789abcdef"


class FakeTransport(Transport):
    def __init__(self) -> None:
        self.sent: List[Tuple[int, str]] = []
        self.notes: List[str] = []
        self.read: List[int] = []
        self.typed: List[Tuple[int, float]] = []
        self.next_id = 9000
        self.link_previews: List[bool] = []
        self.fail: Optional[BaseException] = None
        self.on_typing: Any = None

    async def send(self, chat_id: int, text: str, *, link_preview: bool = False) -> int:
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))
        self.link_previews.append(link_preview)
        self.next_id += 1
        return self.next_id

    async def mark_read(self, chat_id: int) -> None:
        self.read.append(chat_id)

    async def typing(self, chat_id: int, seconds: float) -> None:
        self.typed.append((chat_id, seconds))
        if self.on_typing is not None:
            await self.on_typing()

    async def recording(self, chat_id: int, seconds: float) -> None:
        self.typed.append((chat_id, -seconds))  # negative = "recording voice"

    async def send_voice(self, chat_id: int, path: Path, duration: float) -> int:
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, f"VOICE:{Path(path).name}"))
        self.next_id += 1
        return self.next_id

    async def note_to_self(self, text: str) -> None:
        self.notes.append(text)


class Rig:
    def __init__(self, tmp_path: Path, **overrides: str) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        profile = tmp_path / "profile.md"
        profile.write_text(PROFILE_TEXT, encoding="utf-8")
        values = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": API_HASH,
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(profile),
            "CLAUDE_BIN": str(tmp_path / "no-such-claude"),
        }
        values.update(overrides)
        self.settings = load_settings(env_file, require_telegram=False, require_userbot=True, overrides=values)
        self.transport = FakeTransport()
        self.generator = FakeGenerator()
        self.clock = Clock()
        self.previews: List[Any] = []
        self._id = 0
        self.assistant = UserbotAssistant(
            self.settings,
            self.generator,
            self.transport,
            me_id=ME,
            clock=self.clock,
            preview_sink=self.previews.append,
            greeting_voice=(tmp_path / "greeting.ogg", 6.0),
        )

    async def incoming(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        chat_id: int = FRIEND,
        contact: bool = True,
        bot: bool = False,
        message_id: Optional[int] = None,
    ) -> Outcome:
        if message_id is None:
            self._id += 1
            message_id = self._id
        return await self.assistant.on_message(
            IncomingMessage(
                chat_id=chat_id,
                message_id=message_id,
                outgoing=False,
                text=text,
                raw_text=text,
                chat_name="Ali",
                sender_name="Ali",
                sender_is_bot=bot,
                is_contact=contact,
            )
        )

    async def outgoing(self, text: str, *, chat_id: int = FRIEND, message_id: Optional[int] = None) -> Outcome:
        if message_id is None:
            self._id += 1
            message_id = self._id
        return await self.assistant.on_message(
            IncomingMessage(chat_id=chat_id, message_id=message_id, outgoing=True, text=text, raw_text=text)
        )


@pytest.fixture
def rig(tmp_path: Path):
    def factory(**overrides: str) -> Rig:
        return Rig(tmp_path, **overrides)

    return factory


# ------------------------------------------------------------------ config
def test_userbot_settings_required(tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="TELEGRAM_API_ID"):
        load_settings(env, require_telegram=False, require_userbot=True)
    with pytest.raises(ConfigError, match="TELEGRAM_API_HASH is not set"):
        load_settings(env, require_telegram=False, require_userbot=True, overrides={"TELEGRAM_API_ID": "1"})
    with pytest.raises(ConfigError, match="32 hex"):
        load_settings(env, require_telegram=False, overrides={"TELEGRAM_API_HASH": "secret-value"})
    with pytest.raises(ConfigError, match="USERBOT_CONTACTS_ONLY"):
        load_settings(env, require_telegram=False, overrides={"USERBOT_CONTACTS_ONLY": "maybe"})


def test_userbot_settings_defaults_and_secret_hidden(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH={API_HASH}\n", encoding="utf-8")
    s = load_settings(env, require_telegram=False, require_userbot=True)
    assert s.api_id == 12345 and s.api_hash == API_HASH
    assert s.userbot_contacts_only is True and s.userbot_max_replies_per_hour == 20
    assert s.bot_token is None  # no bot token needed in userbot mode
    assert API_HASH not in repr(s)
    assert s.userbot_session == s.data_dir / "userbot"


def test_api_hash_is_not_passed_to_claude():
    from assistant.claude_cli import build_env

    child = build_env(None, base={"TELEGRAM_API_HASH": API_HASH, "PATH": "x"})
    assert "TELEGRAM_API_HASH" not in child and child["PATH"] == "x"


# ------------------------------------------------------------- preview mode
async def test_preview_mode_never_sends(rig):
    r = rig()
    assert await r.incoming() == Outcome.previewed
    assert r.transport.sent == [] and r.transport.read == []
    assert len(r.previews) == 1 and r.previews[0].chat_id == FRIEND
    assert "Ism: Bobur" in r.generator.calls[0][0]


async def test_ignores_bots_service_accounts_and_strangers(rig):
    r = rig()
    assert await r.incoming(bot=True) == Outcome.ignored_bot
    assert await r.incoming(chat_id=777000) == Outcome.ignored_bot
    assert await r.incoming(contact=False) == Outcome.not_contact
    assert r.generator.calls == []
    everyone = rig(USERBOT_CONTACTS_ONLY="false")
    # same data directory, so use a message id the first rig did not see
    assert await everyone.incoming(contact=False, message_id=100) == Outcome.previewed


async def test_duplicates_and_service_messages(rig):
    r = rig()
    assert await r.incoming(message_id=7) == Outcome.previewed
    assert await r.incoming(message_id=7) == Outcome.duplicate
    assert await r.incoming(None) == Outcome.unsupported
    assert len(r.generator.calls) == 1


# --------------------------------------------------------------- auto-send
async def test_auto_send_reads_types_and_sends(rig):
    r = rig(AUTO_SEND="true")
    assert await r.incoming() == Outcome.sent
    assert r.transport.read == [FRIEND]
    assert r.transport.typed and r.transport.typed[0][0] == FRIEND
    assert r.transport.sent == [(FRIEND, r.generator.default)]
    history = r.assistant.history.get("userbot", FRIEND)
    assert [item["role"] for item in history] == ["other", "owner"]


async def test_own_reply_echo_is_not_treated_as_owner(rig):
    r = rig(AUTO_SEND="true")
    await r.incoming()
    own_id = r.transport.next_id
    assert await r.outgoing(r.generator.default, message_id=own_id) == Outcome.ignored_bot
    # no takeover: the next message is still answered
    assert await r.incoming("Yana bir savol") == Outcome.sent


async def test_owner_takeover(rig):
    r = rig(AUTO_SEND="true")
    assert await r.outgoing("o'zim yozaman") == Outcome.owner_message
    assert await r.incoming() == Outcome.owner_active
    r.clock.advance(15 * 60 + 1)
    assert await r.incoming() == Outcome.sent


async def test_owner_writes_while_typing(rig):
    r = rig(AUTO_SEND="true")

    async def owner_types() -> None:
        r.transport.on_typing = None
        await r.outgoing("men o'zim javob beraman")

    r.transport.on_typing = owner_types
    assert await r.incoming() == Outcome.owner_active
    assert r.transport.sent == []
    assert "not sent" in r.previews[-1].note


async def test_hourly_limit(rig):
    r = rig(AUTO_SEND="true", USERBOT_MAX_REPLIES_PER_HOUR="2")
    assert await r.incoming() == Outcome.sent
    assert await r.incoming() == Outcome.sent
    assert await r.incoming() == Outcome.rate_limited
    assert "per hour" in r.previews[-1].note
    r.clock.advance(3601)
    assert await r.incoming() == Outcome.sent


async def test_flood_wait_stops_sending(rig):
    r = rig(AUTO_SEND="true")
    r.transport.fail = SendRateLimited(120)
    assert await r.incoming() == Outcome.rate_limited
    assert any("120" in note for note in r.transport.notes)
    r.transport.fail = None
    assert await r.incoming() == Outcome.rate_limited  # still waiting
    r.clock.advance(121)
    assert await r.incoming() == Outcome.sent


async def test_send_failure_is_an_error(rig):
    r = rig(AUTO_SEND="true")
    r.transport.fail = SendFailed("USER_IS_BLOCKED")
    assert await r.incoming() == Outcome.error


async def test_claude_limit_pauses(rig):
    r = rig(AUTO_SEND="true")
    r.generator.queue(ClaudeLimitError(detail="usage limit", resets_at=None))
    assert await r.incoming() == Outcome.limited
    assert await r.incoming() == Outcome.limited
    assert r.transport.sent == []


async def test_skip_token(rig):
    r = rig(AUTO_SEND="true")
    r.generator.queue("[SKIP]")
    assert await r.incoming("ok") == Outcome.skipped
    assert r.transport.sent == []


# -------------------------------------------------------------- reply delay
async def test_reply_delay_answers_once_after_the_last_message(rig):
    import asyncio

    r = rig(AUTO_SEND="true", USERBOT_REPLY_DELAY_SECONDS="120")
    gate = asyncio.Event()
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        await gate.wait()

    r.assistant.sleep = fake_sleep
    first = asyncio.create_task(r.incoming("Salom"))
    await asyncio.sleep(0)
    second = asyncio.create_task(r.incoming("Banket narxi qancha?"))
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(first, second) == [Outcome.superseded, Outcome.sent]
    assert slept == [120, 120]
    assert len(r.generator.calls) == 1 and len(r.transport.sent) == 1
    prompt = r.generator.calls[0][1]
    assert "Salom" in prompt and "Banket narxi" in prompt


async def test_reply_delay_owner_answers_first(rig):
    r = rig(AUTO_SEND="true", USERBOT_REPLY_DELAY_SECONDS="120")

    async def owner_answers(seconds: float) -> None:
        await r.outgoing("Assalomu alaykum, hozir aytaman")

    r.assistant.sleep = owner_answers
    assert await r.incoming() == Outcome.owner_active
    assert r.generator.calls == [] and r.transport.sent == []


# ----------------------------------------------------------------- greeting
GREETING = "Assalomu alaykum! Olimp Garden restorani."


def test_greeting_detection():
    from assistant.voice import is_greeting_only

    for text in (
        "Salom", "salom!", "Assalomu alaykum", "assalom aleykum", "Assalomu aleykum 😊", "Ассалому алайкум",
        "Салом", "Salom aka", "slm", "Asalomu alaykum!!!", "Salomlar", "Xayrli kun", "Hayrli kech",
        "Assalomu alaykum, yaxshimisiz?", "Salom qalesiz", "Привет, как дела?", "Здравствуйте",
        "Добрый день", "Hello", "Hi there", "Good morning",
    ):
        assert is_greeting_only(text), text
    for text in (
        "Salom, banket narxi qancha?", "Banket bormi", "", None, "Rahmat", "Assalomu alaykum, menyu bormi?",
        "Hi, can I book a table?", "Здравствуйте, сколько стоит банкет?", "Salomatlik",
    ):
        assert not is_greeting_only(text), text


async def test_greeting_voice_without_claude(rig):
    r = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING)
    assert await r.incoming("Assalomu alaykum") == Outcome.sent
    assert r.transport.sent == [(FRIEND, "VOICE:greeting.ogg")]
    assert r.transport.typed[-1][1] < 0  # "recording voice..." was shown
    assert r.generator.calls == []
    history = r.assistant.history.get("userbot", FRIEND)
    assert history[-1]["role"] == "owner" and history[-1]["text"] == GREETING


async def test_greeting_once_then_claude(rig):
    r = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING)
    assert await r.incoming("Salom") == Outcome.sent
    assert await r.incoming("Salom") == Outcome.sent  # second "Salom" goes to Claude
    assert len(r.generator.calls) == 1
    r.clock.advance(13 * 3600)
    assert await r.incoming("Salom") == Outcome.sent
    assert len(r.generator.calls) == 1  # greeted again after 12 hours
    # greeting + question always goes to Claude
    assert await r.incoming("Salom, banket narxi qancha?") == Outcome.sent
    assert len(r.generator.calls) == 2


async def test_greeting_modes_and_preview(rig, tmp_path):
    text_mode = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING, USERBOT_GREETING_MODE="text")
    assert await text_mode.incoming("Salom", message_id=1) == Outcome.sent
    assert text_mode.transport.sent == [(FRIEND, GREETING)]

    both = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING, USERBOT_GREETING_MODE="both")
    assert await both.incoming("Salom", chat_id=FRIEND + 1, message_id=2) == Outcome.sent
    assert both.transport.sent == [(FRIEND + 1, "VOICE:greeting.ogg"), (FRIEND + 1, GREETING)]

    preview = rig(USERBOT_GREETING_TEXT=GREETING)
    assert await preview.incoming("Salom", chat_id=FRIEND + 2, message_id=3) == Outcome.previewed
    assert preview.transport.sent == []
    assert preview.previews[-1].reply == "[voice] " + GREETING

    no_voice = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING)
    no_voice.assistant.greeting_voice = None  # voice synthesis failed -> text greeting
    assert await no_voice.incoming("Salom", chat_id=FRIEND + 3, message_id=4) == Outcome.sent
    assert no_voice.transport.sent == [(FRIEND + 3, GREETING)]


MENU = "Bizning menyu: https://menu.olimpgarden.uz/"
INTRO = "🤖 Men Abbosbekning online yordamchisiman."


async def test_intro_once_per_chat_and_assistant_persona(rig):
    r = rig(AUTO_SEND="true", USERBOT_INTRO_TEXT=INTRO)
    r.generator.queue("Abbosbek hozir band, tez orada yozadi 😄", "Ha, albatta aytaman!")
    assert await r.incoming("Abbosbek qayerda?") == Outcome.sent
    assert r.transport.sent[-1] == (FRIEND, INTRO + "\n" + "Abbosbek hozir band, tez orada yozadi 😄")
    assert "online assistant" in r.generator.calls[0][0]  # assistant persona in the system prompt
    assert await r.incoming("Unga ayting, men yozdim") == Outcome.sent
    assert r.transport.sent[-1] == (FRIEND, "Ha, albatta aytaman!")  # no second intro
    r.clock.advance(13 * 3600)
    assert await r.incoming("Yana salom") == Outcome.sent
    assert r.transport.sent[-1][1].startswith(INTRO)  # introduced again after 12 hours


async def test_fast_contacts_skip_delay(rig):
    r = rig(
        AUTO_SEND="true",
        USERBOT_REPLY_DELAY_SECONDS="300",
        USERBOT_FAST_CONTACTS="BanketGarden, Asad Ish Mj, 777",
    )
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    r.assistant.sleep = fake_sleep

    async def from_name(name: str, chat_id: int) -> Outcome:
        r._id += 1
        return await r.assistant.on_message(
            IncomingMessage(chat_id=chat_id, message_id=r._id, outgoing=False, text="Qalesan?",
                            raw_text="Qalesan?", chat_name=name, sender_name=name, is_contact=True)
        )

    assert await from_name("Asad Ish Mj", 1001) == Outcome.sent
    assert await from_name("Banket Garden", 1002) == Outcome.sent  # spaces / case ignored
    assert await from_name("Anyone", 777) == Outcome.sent  # by user id
    assert slept == []
    assert all(seconds <= 1.0 for _, seconds in r.transport.typed)
    assert await from_name("Boshqa odam", 1003) == Outcome.sent
    assert slept == [300]  # everyone else waits 5 minutes


async def test_ignored_contacts_never_answered(rig):
    r = rig(AUTO_SEND="true", USERBOT_IGNORE_CONTACTS="Muzaffar ogo, myFather, 4242")

    async def from_name(name: str, chat_id: int, text: str = "Qayerdasan?") -> Outcome:
        r._id += 1
        return await r.assistant.on_message(
            IncomingMessage(chat_id=chat_id, message_id=r._id, outgoing=False, text=text, raw_text=text,
                            chat_name=name, sender_name=name, is_contact=True)
        )

    assert await from_name("Muzaffar Ogo", 2001) == Outcome.ignored_contact
    assert await from_name("myFather", 2002) == Outcome.ignored_contact
    assert await from_name("Dadam", 4242) == Outcome.ignored_contact  # by id
    assert r.generator.calls == [] and r.transport.sent == []
    assert r.assistant.history.get("userbot", 2001) == []  # nothing stored either
    assert await from_name("Boshqa", 2003) == Outcome.sent


async def test_scheduled_message_every_20_minutes(rig):
    text = "ishingni qil rells go'ravarmin"
    r = rig(USERBOT_SCHEDULE_TO="Asadbek ish mj", USERBOT_SCHEDULE_TEXT=text, USERBOT_SCHEDULE_EVERY_MINUTES="20")
    assert await r.assistant.scheduled_tick() is None  # target not resolved yet
    r.assistant.schedule_chat_id = 7307
    assert await r.assistant.scheduled_tick() == Outcome.sent  # first one right away
    assert r.transport.sent == [(7307, text)]
    r.clock.advance(19 * 60)
    assert await r.assistant.scheduled_tick() is None
    r.clock.advance(60)
    assert await r.assistant.scheduled_tick() == Outcome.sent
    assert len(r.transport.sent) == 2
    # the echo of our own scheduled message is not treated as "the owner wrote"
    assert await r.outgoing(text, chat_id=7307, message_id=r.transport.next_id) == Outcome.ignored_bot
    # paused -> nothing is sent; the timer survives a restart (data/schedule.json)
    r.assistant.pause()
    r.clock.advance(20 * 60)
    assert await r.assistant.scheduled_tick() == Outcome.paused
    r.assistant.resume()
    again = rig(USERBOT_SCHEDULE_TO="7307", USERBOT_SCHEDULE_TEXT=text, USERBOT_SCHEDULE_EVERY_MINUTES="20")
    again.clock.now = r.clock.now - 60  # a restart 19 minutes after the last real send
    again.assistant.schedule_chat_id = 7307
    assert await again.assistant.scheduled_tick() is None  # remembers the last send: no extra message


async def test_no_delay_when_zero(rig):
    r = rig(AUTO_SEND="true", USERBOT_REPLY_DELAY_SECONDS="0")
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    r.assistant.sleep = fake_sleep
    assert await r.incoming("Qalesan?") == Outcome.sent
    assert slept == []


def test_owner_persona_unchanged():
    from datetime import datetime

    from assistant.prompts import build_system_prompt

    prompt = build_system_prompt("x", datetime(2026, 1, 1))
    assert "write as them, in the first person" in prompt and "online assistant" not in prompt


async def test_greeting_text_with_menu_immediately_even_in_preview(rig):
    r = rig(
        USERBOT_GREETING_TEXT=GREETING,
        USERBOT_GREETING_FOLLOWUP=MENU,
        USERBOT_GREETING_MODE="text",
        USERBOT_GREETING_SEND="true",
        USERBOT_REPLY_DELAY_SECONDS="120",
    )
    slept: List[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    r.assistant.sleep = fake_sleep
    assert await r.incoming("Assalomu alaykum") == Outcome.sent  # PREVIEW mode, but greeting is sent
    assert slept == []  # no 2-minute wait for a greeting
    assert r.transport.sent == [(FRIEND, GREETING + "\n" + MENU)]
    assert r.transport.link_previews == [True]
    assert r.transport.typed[-1][1] <= 1.5  # short "typing..."
    assert r.generator.calls == []
    # a real question still waits 2 minutes and stays a preview (AUTO_SEND=false)
    assert await r.incoming("Banket narxi qancha?") == Outcome.previewed
    assert slept == [120] and len(r.transport.sent) == 1


async def test_greeting_voice_then_menu_text(rig):
    r = rig(AUTO_SEND="true", USERBOT_GREETING_TEXT=GREETING, USERBOT_GREETING_FOLLOWUP=MENU)
    assert await r.incoming("Salom") == Outcome.sent
    assert r.transport.sent == [(FRIEND, "VOICE:greeting.ogg"), (FRIEND, MENU)]
    history = r.assistant.history.get("userbot", FRIEND)
    assert history[-1]["text"] == GREETING + "\n" + MENU  # Claude later knows the menu was sent


async def test_greeting_send_respects_pause_and_takeover(rig):
    r = rig(USERBOT_GREETING_TEXT=GREETING, USERBOT_GREETING_MODE="text", USERBOT_GREETING_SEND="true")
    r.assistant.pause()
    assert await r.incoming("Salom") == Outcome.paused
    r.assistant.resume()
    await r.outgoing("o'zim javob beraman", chat_id=FRIEND + 5)
    assert await r.incoming("Salom", chat_id=FRIEND + 5) == Outcome.owner_active
    assert r.transport.sent == []


async def test_no_greeting_configured_uses_claude(rig):
    r = rig(AUTO_SEND="true")
    assert await r.incoming("Salom") == Outcome.sent
    assert len(r.generator.calls) == 1


def test_greeting_mode_validation(tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="USERBOT_GREETING_MODE"):
        load_settings(env, require_telegram=False, overrides={"USERBOT_GREETING_MODE": "video"})


# ---------------------------------------------------------- owner commands
async def test_saved_messages_commands(rig):
    r = rig(AUTO_SEND="true")
    assert await r.outgoing("/pause", chat_id=ME) == Outcome.owner_command
    assert "To'xtatildi" in r.transport.notes[-1]
    assert await r.incoming() == Outcome.paused
    assert await r.outgoing("/status", chat_id=ME) == Outcome.owner_command
    assert "paused" in r.transport.notes[-1] and "contacts only" in r.transport.notes[-1]
    assert await r.outgoing("/resume", chat_id=ME) == Outcome.owner_command
    assert await r.incoming() == Outcome.sent
    assert await r.outgoing("/help", chat_id=ME) == Outcome.owner_command
    assert r.transport.notes[-1] == HELP_TEXT
    # plain notes to self are not commands and never reach Claude
    calls = len(r.generator.calls)
    assert await r.outgoing("xarid ro'yxati", chat_id=ME) == Outcome.unsupported
    assert len(r.generator.calls) == calls


# ------------------------------------------------------- Telethon helpers
def _msg(**kwargs: Any) -> Any:
    base = dict(
        action=None, message="", file=None, voice=None, video_note=None, sticker=None, photo=None,
        gif=None, video=None, audio=None, document=None, geo=None, contact=None, poll=None, dice=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_describe_telethon():
    assert describe_telethon(_msg(message="Salom")) == "Salom"
    assert describe_telethon(_msg(action=object())) is None
    assert describe_telethon(_msg(voice=object(), document=object())) == "[voice message]"
    assert describe_telethon(_msg(sticker=object(), document=object(), file=SimpleNamespace(emoji="😀"))) == "[sticker] 😀"
    assert describe_telethon(_msg(photo=object(), message="qara")) == "[photo] qara"
    assert describe_telethon(_msg(document=object(), file=SimpleNamespace(name="a.pdf"))) == "[file a.pdf]"
    assert describe_telethon(_msg()) is None


def test_login_error_text():
    from telethon import errors

    from assistant.userbot import login_error_text

    assert "API_ID" in login_error_text(errors.ApiIdInvalidError(request=None))
    assert "+998" in login_error_text(errors.PhoneNumberInvalidError(request=None))
    assert "eskirib" in login_error_text(errors.PhoneCodeExpiredError(request=None))
    assert "120" in login_error_text(errors.FloodWaitError(request=None, capture=120))
    assert "Kutilmagan" in login_error_text(ValueError("x"))


class FakeLoginClient:
    def __init__(self, sent_types: List[Any], *, password_needed: bool = False) -> None:
        self.sent_types = list(sent_types)
        self.password_needed = password_needed
        self.code_requests = 0
        self.sign_ins: List[dict] = []

    async def is_user_authorized(self) -> bool:
        return False

    async def send_code_request(self, phone: str) -> Any:
        from telethon.tl import types

        self.code_requests += 1
        return types.auth.SentCode(type=self.sent_types.pop(0), phone_code_hash=f"hash{self.code_requests}")

    async def sign_in(self, **kwargs: Any) -> None:
        from telethon import errors

        self.sign_ins.append(kwargs)
        if "code" in kwargs and kwargs["code"] != "12345":
            raise errors.PhoneCodeInvalidError(request=None)
        if "code" in kwargs and self.password_needed:
            raise errors.SessionPasswordNeededError(request=None)


async def test_interactive_login_resend_and_password():
    from telethon.tl import types

    from assistant.userbot import interactive_login

    client = FakeLoginClient(
        [types.auth.SentCodeTypeApp(length=5), types.auth.SentCodeTypeSms(length=5)], password_needed=True
    )
    answers = iter(["+998 90 000 00 00", "", "11111", "12 345"])
    shown: List[str] = []
    secrets: List[str] = []

    def ask_secret(prompt: str) -> str:
        secrets.append(prompt)
        return "pw"

    await interactive_login(client, ask=lambda p: next(answers), ask_secret=ask_secret, show=shown.append)
    assert client.code_requests == 2  # empty Enter asked for a resend
    assert "ILOVANGIZGA" in shown[0] and "SMS" in shown[1] and "noto'g'ri" in shown[2]
    assert client.sign_ins[-2]["code"] == "12345" and client.sign_ins[-2]["phone_code_hash"] == "hash2"
    assert client.sign_ins[-1] == {"password": "pw"} and len(secrets) == 1


class FakeQR:
    def __init__(self, outcomes: List[Any]) -> None:
        self.outcomes = list(outcomes)
        self.token = 1
        self.urls_seen: List[str] = []

    @property
    def url(self) -> str:
        return f"tg://login?token=TOKEN{self.token}"

    async def wait(self) -> Any:
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def recreate(self) -> None:
        self.token += 1


class FakeQRClient:
    def __init__(self, qr: FakeQR) -> None:
        self.qr = qr
        self.passwords: List[str] = []

    async def is_user_authorized(self) -> bool:
        return False

    async def qr_login(self) -> FakeQR:
        return self.qr

    async def sign_in(self, **kwargs: Any) -> None:
        self.passwords.append(kwargs["password"])


async def test_qr_login_refreshes_and_cleans_up(tmp_path):
    import asyncio

    from assistant.userbot import interactive_login

    page = tmp_path / "login-qr.html"
    qr = FakeQR([asyncio.TimeoutError(), "user"])
    client = FakeQRClient(qr)
    opened: List[str] = []
    snapshots: List[str] = []

    original_wait = qr.wait

    async def wait_and_snapshot() -> Any:
        snapshots.append(page.read_text(encoding="utf-8"))
        return await original_wait()

    qr.wait = wait_and_snapshot  # type: ignore[assignment]
    from assistant import userbot

    await userbot.qr_login(client, page, show=lambda t: None, open_page=opened.append)
    assert opened == [str(page)]  # opened once, then the page refreshes itself
    assert "<svg" in snapshots[0] and "refresh" in snapshots[0]
    assert snapshots[0] != snapshots[1]  # a new token after the timeout
    final = page.read_text(encoding="utf-8")
    assert "<svg" not in final and "refresh" not in final
    # Enter (no choice) picks the QR login in interactive_login
    qr2 = FakeQR(["user"])
    opened2: List[str] = []
    await interactive_login(
        FakeQRClient(qr2),
        ask=lambda p: "",
        show=lambda t: None,
        qr_page_path=tmp_path / "p2.html",
        open_page=opened2.append,
    )
    assert opened2 == [str(tmp_path / "p2.html")]


async def test_qr_login_with_2fa(tmp_path):
    from telethon import errors

    from assistant import userbot

    client = FakeQRClient(FakeQR([errors.SessionPasswordNeededError(request=None)]))
    await userbot.qr_login(
        client, tmp_path / "q.html", ask_secret=lambda p: "pw", show=lambda t: None, open_page=lambda p: None
    )
    assert client.passwords == ["pw"]


def test_code_destination_email():
    from telethon.tl import types

    from assistant.userbot import code_destination

    sent = types.auth.SentCode(
        type=types.auth.SentCodeTypeEmailCode(email_pattern="a***@gmail.com", length=6),
        phone_code_hash="h",
        next_type=types.auth.CodeTypeSms(),
        timeout=60,
    )
    text = code_destination(sent)
    assert "a***@gmail.com" in text and "SMS orqali" in text and "60" in text


def test_archive_account_data(tmp_path):
    from assistant.userbot import archive_account_data

    (tmp_path / "history.json").write_text("{}", encoding="utf-8")
    (tmp_path / "seen.json").write_text("[]", encoding="utf-8")
    (tmp_path / "voice-abc.ogg").write_bytes(b"x")  # caches stay
    target = archive_account_data(tmp_path, "20261115")
    assert target == tmp_path / "archive-20261115"
    assert (target / "history.json").exists() and (target / "seen.json").exists()
    assert not (tmp_path / "history.json").exists() and (tmp_path / "voice-abc.ogg").exists()
    assert archive_account_data(tmp_path, "again") is None


def test_typing_seconds_bounds():
    assert typing_seconds("") == 2.0
    assert typing_seconds("x" * 1000) == 8.0


def test_no_internet_at_start_is_retried_not_treated_as_a_login_problem():
    from telethon import errors

    from assistant.userbot import EXIT_NEEDS_PERSON, EXIT_RETRY, login_exit_code

    for exc in (ConnectionError("Connection to Telegram failed 5 time(s)"), TimeoutError(),
                OSError(1231, "The network location cannot be reached")):
        assert login_exit_code(exc) == EXIT_RETRY
    for exc in (errors.AuthKeyUnregisteredError(request=None), errors.PasswordHashInvalidError(request=None),
                RuntimeError("other")):
        assert login_exit_code(exc) == EXIT_NEEDS_PERSON
