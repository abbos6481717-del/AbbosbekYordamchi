from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("aiogram")

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout  # noqa: E402
from assistant.handlers import STRANGER_START_TEXT  # noqa: E402
from assistant.service import Outcome  # noqa: E402

from .conftest import OWNER_ID, STRANGER_ID  # noqa: E402

POSIX = not sys.platform.startswith("win")


# ------------------------------------------------------------------ preview
async def test_preview_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message("Salom, qalesan?")
    assert outcome == Outcome.previewed
    assert h.sent() == []
    assert len(h.previews) == 1
    preview = h.previews[0]
    assert preview.reply == h.generator.default
    assert preview.incoming == "Salom, qalesan?"
    assert preview.language == "uz"

    path = h.settings.data_dir / "previews.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["reply"] == h.generator.default
    if POSIX:
        assert os.stat(path).st_mode & 0o777 == 0o600

    history = h.assistant.history.get("conn-1", STRANGER_ID)
    assert [item["role"] for item in history] == ["other"]  # unsent reply not stored


async def test_preview_with_known_connection_makes_no_telegram_requests(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Salom")
    await h.business_message("Qayerdasan?")
    assert h.session.requests == []


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()  # Telegram knows it, but no business_connection update arrived
    assert await h.business_message("Salom") == Outcome.previewed
    assert await h.business_message("Qalesan") == Outcome.previewed
    assert len(h.lookups()) == 1
    assert h.sent() == []


async def test_lookup_failure_is_no_connection(make_harness):
    h = make_harness()
    assert await h.business_message("Salom", connection_id="unknown") == Outcome.no_connection
    assert h.generator.calls == []


async def test_foreign_and_disabled_connections_do_not_call_claude(make_harness):
    h = make_harness()
    await h.connect("foreign", user_id=999)
    assert await h.business_message("Salom", connection_id="foreign") == Outcome.foreign_connection
    await h.connect("off", enabled=False)
    assert await h.business_message("Salom", connection_id="off") == Outcome.connection_disabled
    assert h.generator.calls == []
    assert h.sent() == []


async def test_any_chat_gets_a_reply_without_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat in (1001, 2002, 3003):
        assert await h.business_message("Salom", from_id=chat) == Outcome.previewed
    assert len(h.previews) == 3


# --------------------------------------------------------------------- owner
async def test_owner_message_is_recorded_and_never_answered(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message("Hozir o'zim yozaman", from_id=OWNER_ID, chat_id=STRANGER_ID)
    assert outcome == Outcome.owner_message
    assert h.generator.calls == []
    history = h.assistant.history.get("conn-1", STRANGER_ID)
    assert history[-1]["role"] == "owner"
    assert history[-1]["text"] == "Hozir o'zim yozaman"


async def test_owner_takeover_silences_chat_until_it_expires(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Men o'zim javob beraman", from_id=OWNER_ID, chat_id=STRANGER_ID)
    assert await h.business_message("Salom") == Outcome.owner_active
    assert await h.business_message("Salom", from_id=4444) == Outcome.previewed  # other chats unaffected
    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Salom yana") == Outcome.previewed


async def test_bot_messages_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("Auto reply", via_business_bot=True, from_id=OWNER_ID) == Outcome.ignored_bot
    assert await h.business_message("I am a bot", is_bot=True) == Outcome.ignored_bot
    assert h.generator.calls == []


# ----------------------------------------------------------------- duplicates
async def test_duplicates_parallel_sequential_and_after_restart(make_harness):
    h = make_harness()
    await h.connect()
    await asyncio.gather(h.business_message("Salom", message_id=7), h.business_message("Salom", message_id=7))
    assert sorted(o.value for o in h.assistant.outcomes) == ["duplicate", "previewed"]
    assert await h.business_message("Salom", message_id=7) == Outcome.duplicate
    h.restart()
    assert await h.business_message("Salom", message_id=7) == Outcome.duplicate
    assert len(h.generator.calls) == 1


# ------------------------------------------------------------ message kinds
async def test_service_message_is_unsupported(make_harness):
    h = make_harness()
    await h.connect()
    outcome = await h.business_message(None, extra={"new_chat_title": "New title"})
    assert outcome == Outcome.unsupported
    assert h.generator.calls == []


async def test_sticker_and_voice_are_described(make_harness):
    h = make_harness()
    await h.connect()
    sticker = {
        "file_id": "s1", "file_unique_id": "su1", "type": "regular", "width": 512, "height": 512,
        "is_animated": False, "is_video": False, "emoji": "😀",
    }
    voice = {"file_id": "v1", "file_unique_id": "vu1", "duration": 3}
    await h.business_message(None, extra={"sticker": sticker})
    await h.business_message(None, extra={"voice": voice})
    texts = [item["text"] for item in h.assistant.history.get("conn-1", STRANGER_ID)]
    assert texts == ["[sticker] 😀", "[voice message]"]


# ------------------------------------------------------------------- history
async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect("conn-1")
    await h.connect("conn-2")
    await h.business_message("Birinchi chat siri", from_id=1001)
    await h.business_message("Ikkinchi chat siri", from_id=2002)
    await h.business_message("Boshqa connection siri", from_id=1001, connection_id="conn-2")

    prompt_chat1, prompt_chat2, prompt_conn2 = (call[1] for call in h.generator.calls)
    assert "Birinchi chat siri" in prompt_chat1
    assert "Ikkinchi chat siri" in prompt_chat2
    assert "Birinchi" not in prompt_chat2 and "Boshqa" not in prompt_chat2
    assert "Birinchi" not in prompt_conn2 and "Boshqa connection siri" in prompt_conn2
    assert len(h.assistant.history.get("conn-1", 1001)) == 1
    assert len(h.assistant.history.get("conn-2", 1001)) == 1


async def test_language_hints(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=1)
    await h.business_message("Hi, are you free tonight?", from_id=2)
    await h.business_message("Bugun bo'shmisan?", from_id=3)
    await h.business_message("👍", from_id=1)
    prompts = [call[1] for call in h.generator.calls]
    assert "Their latest message is in Russian." in prompts[0]
    assert "Their latest message is in English." in prompts[1]
    assert "Their latest message is in Uzbek." in prompts[2]
    assert "Their latest message is in Russian." in prompts[3]  # emoji: last language of the chat


async def test_skip_is_not_shown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue("[SKIP]")
    assert await h.business_message("ok") == Outcome.skipped
    assert h.previews == []
    assert h.sent() == []


# ----------------------------------------------------------------- auto-send
async def test_auto_send_rechecks_connection_then_sends(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    assert await h.business_message("Salom") == Outcome.sent
    kinds = [type(r).__name__ for r in h.session.requests]
    assert kinds == ["GetBusinessConnection", "SendMessage"]
    sent = h.sent()[0]
    assert sent.chat_id == STRANGER_ID
    assert sent.business_connection_id == "conn-1"
    assert sent.text == h.generator.default
    history = h.assistant.history.get("conn-1", STRANGER_ID)
    assert [i["role"] for i in history] == ["other", "owner"]


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue("x" * 9000)
    assert await h.business_message("Uzun yoz") == Outcome.sent
    assert [len(r.text) for r in h.sent()] == [4000, 4000, 1000]


@pytest.mark.parametrize("change", [{"can_reply": False}, {"enabled": False}])
async def test_auto_send_refuses_without_permission(make_harness, change):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection(**change)  # owner changed the settings in Telegram, no update yet
    assert await h.business_message("Salom") == Outcome.permission_denied
    assert h.sent() == []
    assert h.previews and h.previews[-1].note.startswith("not sent")


async def test_auto_send_does_not_send_when_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()

    async def owner_writes() -> None:
        await h.business_message("Men o'zim yozdim", from_id=OWNER_ID, chat_id=STRANGER_ID)

    h.generator.hook = owner_writes
    assert await h.business_message("Salom") == Outcome.owner_active
    assert h.sent() == []
    assert "owner wrote" in h.previews[-1].note


# ------------------------------------------------------------ owner commands
async def test_pause_and_resume_only_for_owner_and_pause_survives_restart(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause", from_id=STRANGER_ID)
    assert h.assistant.state.paused is False
    assert h.sent() == []

    await h.direct_message("/pause")
    assert h.assistant.state.paused is True
    assert await h.business_message("Salom") == Outcome.paused
    h.restart()
    assert h.assistant.state.paused is True
    assert await h.business_message("Salom yana") == Outcome.paused

    await h.direct_message("/resume", from_id=STRANGER_ID)
    assert h.assistant.state.paused is True
    await h.direct_message("/resume")
    assert h.assistant.state.paused is False
    assert await h.business_message("Endi-chi?") == Outcome.previewed
    # Only the owner's commands got answers.
    assert {r.chat_id for r in h.sent()} == {OWNER_ID}


async def test_strangers_plain_messages_and_commands_get_nothing(make_harness):
    h = make_harness()
    await h.direct_message("Salom bot", from_id=STRANGER_ID)
    await h.direct_message("/status", from_id=STRANGER_ID)
    await h.direct_message("/pause", from_id=STRANGER_ID)
    assert h.sent() == []
    assert h.generator.calls == []


async def test_status_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    text = h.sent_to(OWNER_ID)[-1].text
    assert "PREVIEW" in text and "replies are only shown in the logs" in text


async def test_start_for_owner_explains_setup(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent_to(OWNER_ID)[-1].text
    for part in ("PREVIEW", "ishlayapti", "Chatbots", "/pause", "/resume", "/status", "AUTO_SEND=true", "Premium"):
        assert part in text


async def test_start_shows_auto_send_and_paused(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.direct_message("/pause")
    await h.direct_message("/start")
    text = h.sent_to(OWNER_ID)[-1].text
    assert "AVTO-YUBORISH" in text
    assert "pauzada" in text
    assert "AUTO_SEND=true" not in text


async def test_start_for_stranger_is_short_and_reveals_nothing(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=STRANGER_ID)
    sent = h.sent_to(STRANGER_ID)
    assert len(sent) == 1
    assert sent[0].text == STRANGER_START_TEXT
    assert "Bobur" not in sent[0].text and "Toshkent" not in sent[0].text
    assert h.generator.calls == []


# --------------------------------------------------------------------- limits
async def test_limit_waits_until_reset_then_continues(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit", resets_at=h.clock.now + 3600, reset_text="resets 3pm"))
    assert await h.business_message("Salom") == Outcome.limited
    assert await h.business_message("Salom 2") == Outcome.limited
    assert len(h.generator.calls) == 1
    h.clock.advance(3601)
    assert await h.business_message("Salom 3") == Outcome.previewed


async def test_limit_without_reset_uses_cooldown_and_survives_restart(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit"))
    assert await h.business_message("Salom") == Outcome.limited
    assert h.assistant.state.limit_until == pytest.approx(h.clock.now + 30 * 60)
    h.restart()
    await h.connect()
    h.clock.advance(29 * 60)
    assert await h.business_message("Salom 2") == Outcome.limited
    h.clock.advance(2 * 60)
    assert await h.business_message("Salom 3") == Outcome.previewed


async def test_limit_notifies_owner_in_auto_send_and_resume_clears_it(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit"))
    assert await h.business_message("Salom") == Outcome.limited
    notices = h.sent_to(OWNER_ID)
    assert len(notices) == 1 and "limit" in notices[0].text
    assert h.sent_to(STRANGER_ID) == []
    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    assert await h.business_message("Salom 2") == Outcome.sent


async def test_limit_in_preview_mode_does_not_message_owner(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit"))
    assert await h.business_message("Salom") == Outcome.limited
    assert h.sent() == []


async def test_timeout_and_auth_errors_send_nothing_to_chat_and_notify_owner_once(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(ClaudeTimeout("too slow"), ClaudeAuthError("not logged in", "401"))
    assert await h.business_message("Salom") == Outcome.error
    assert await h.business_message("Salom 2") == Outcome.error
    assert h.sent_to(STRANGER_ID) == []
    assert len(h.sent_to(OWNER_ID)) == 1
