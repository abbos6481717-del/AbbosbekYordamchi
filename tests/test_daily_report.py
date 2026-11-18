"""Morning voice report: bookings summary, photo batches, timing and the /hisobot command. Offline."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from assistant.claude_cli import ClaudeCLI
from assistant.config import ConfigError, load_settings
from assistant.daily_report import (
    DailyReporter,
    PhotoRef,
    ReportError,
    booking_slot,
    build_report_prompt,
    day_short,
    day_words,
    merge_duplicates,
    newest_batch,
    parse_report,
    photo_batches,
    summarize_bookings,
)

from .test_userbot import Rig

TZ = timezone(timedelta(hours=5))  # Tashkent
API_HASH = "0123456789abcdef0123456789abcdef"


def make_settings(tmp_path: Path, **overrides: str):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    values = {
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": API_HASH,
        "DATA_DIR": str(tmp_path / "data"),
        "CLAUDE_BIN": str(tmp_path / "no-such-claude"),
        "DAILY_REPORT_TIME": "09:00",
        "DAILY_REPORT_WAIT_UNTIL": "",
        "DAILY_REPORT_GROUP": "-100123",
        "DAILY_REPORT_OWNER": "Abbosbek",
    }
    values.update(overrides)
    return load_settings(env_file, require_telegram=False, require_userbot=True, overrides=values)


# ------------------------------------------------------------------------ config
def test_config_reads_report_settings(tmp_path):
    s = make_settings(
        tmp_path, DAILY_REPORT_TIME="9:00", CRM_URL="https://crm.example.uz/", CRM_LOGIN="m", CRM_PASSWORD="0000"
    )
    assert s.daily_report_time == "09:00"
    assert s.daily_report_wait_until is None  # wait for complete data, however late
    assert s.daily_report_voice == "uz-UZ-MadinaNeural"
    assert s.crm_url == "https://crm.example.uz"
    assert "0000" not in repr(s)


@pytest.mark.parametrize("key,value", [("DAILY_REPORT_TIME", "9am"), ("DAILY_REPORT_TIME", "25:00"),
                                       ("CRM_URL", "crm.example.uz")])
def test_config_rejects_bad_report_values(tmp_path, key, value):
    with pytest.raises(ConfigError) as info:
        make_settings(tmp_path, **{key: value})
    assert key in str(info.value)


def test_crm_password_is_not_passed_to_claude():
    from assistant.claude_cli import build_env

    env = build_env(base={"CRM_PASSWORD": "0000", "PATH": "x"})
    assert "CRM_PASSWORD" not in env


def test_images_go_through_stream_json_with_tools_off(tmp_path):
    cmd = ClaudeCLI("claude", workdir=tmp_path).build_command("prompt.txt", "stream-json", "stream-json")
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--verbose" in cmd


# ---------------------------------------------------------------------- bookings
def test_dates_in_words():
    assert day_words(date(2026, 11, 15)) == "15-noyabr"
    assert day_short(date(2026, 1, 5)) == "05.01"


def test_booking_slots_split_at_17():
    assert booking_slot("12:30") == "abet"
    assert booking_slot("16:59") == "abet"
    assert booking_slot("17:00") == "kechki"
    assert booking_slot("") == ""


def test_duplicates_are_merged_like_the_crm():
    rows = [
        {"hall": "1-qavat", "time": "19:00", "tables": [5, 6], "guests": "8", "status": "tasdiqlangan"},
        {"hall": "1-qavat", "time": "19:00", "tables": ["6"], "guests": "10"},  # same guest, hostess entry
        {"hall": "1-qavat", "time": "19:00", "tables": [7], "guests": "4"},  # other table: other guest
        {"hall": "2-qavat", "time": "19:00", "tables": [5], "guests": "6"},  # other hall
    ]
    merged = merge_duplicates(rows)
    assert len(merged) == 3
    assert merged[0]["tables"] == ["5", "6"] and merged[0]["guests"] == 10


def test_summary_counts_today_only_without_names_or_phones():
    day = date(2026, 11, 15)
    rows = [
        {"date": "2026-11-15", "hall": "1-qavat", "time": "13:00", "tables": [1], "guests": "6",
         "name": "Akmal aka", "phone": "+998901112233"},
        {"date": "2026-11-15", "hall": "2-qavat", "time": "9:30", "tables": [2], "guests": 4},
        {"date": "2026-11-15", "hall": "1-qavat", "time": "19:00", "tables": [3], "guests": "12 kishi"},
        {"date": "2026-11-15", "hall": "1-qavat", "time": "20:00", "tables": [4], "guests": "5", "status": "kelmadi"},
        {"date": "2026-11-15", "hall": "1-qavat", "time": "20:00", "tables": [9], "deleted": True},
        {"date": "2026-11-16", "hall": "1-qavat", "time": "13:00", "tables": [1], "guests": "6"},
        "junk",
    ]
    summary = summarize_bookings(rows, day)
    assert summary["jami"]["bronlar"] == 3 and summary["jami"]["mehmonlar"] == 22
    assert summary["abet"]["bronlar"] == 2 and summary["abet"]["mehmonlar"] == 10
    assert summary["kechki"] == {"bronlar": 1, "mehmonlar": 12, "zallar": {"1-qavat": 1},
                                 "holat": {"kutilmoqda": 1}}
    assert summary["kelmadi"] == 1
    text = json.dumps(summary, ensure_ascii=False)
    assert "Akmal" not in text and "998" not in text


# ------------------------------------------------------------------------ photos
async def _bytes() -> bytes:
    return b"jpeg"


def photo(message_id: int, posted: datetime) -> PhotoRef:
    return PhotoRef(message_id, posted, _bytes)


def test_newest_batch_takes_photos_posted_together():
    base = datetime(2026, 11, 15, 7, 21, tzinfo=timezone.utc)
    photos = [
        photo(1, base - timedelta(days=1)),
        photo(2, base - timedelta(minutes=2)),
        photo(3, base),
        photo(4, base - timedelta(minutes=1)),
    ]
    assert [p.message_id for p in newest_batch(photos)] == [2, 4, 3]
    assert newest_batch([]) == []
    assert [[p.message_id for p in b] for b in photo_batches(photos)] == [[2, 4, 3], [1]]
    assert [[p.message_id for p in b] for b in photo_batches(photos, since=base - timedelta(hours=1))] == [[2, 4, 3]]


# ------------------------------------------------------------------------ Claude
GOOD = {
    "report_day": "14.11",
    "yesterday": {"tushum": 153907495, "xaridor": 899, "ortacha_chek": 171199},
    "speech": "Assalomu alaykum, Abbosbek! O'n beshinchi noyabr holatiga ko'ra...",
    "caption": "14.11: tushum 153,9 mln",
}


def test_parse_report_accepts_json_with_extra_text():
    report = parse_report("```json\n" + json.dumps(GOOD) + "\n```")
    assert report.report_day == "14.11"
    assert report.speech.startswith("Assalomu")
    assert report.caption == "14.11: tushum 153,9 mln"


def test_parse_report_rejects_missing_speech():
    with pytest.raises(ReportError):
        parse_report('{"report_day": "14.11"}')
    with pytest.raises(ReportError):
        parse_report("no json here")


def test_prompt_mentions_dates_bookings_and_missing_crm(tmp_path):
    s = make_settings(tmp_path)
    now = datetime(2026, 11, 15, 9, 0, tzinfo=TZ)
    photos = [photo(1, datetime(2026, 11, 15, 7, 21, tzinfo=timezone.utc))]
    text = build_report_prompt(s, now, photos, None, "no connection", "REJA BAJARILISHI GARDEN")
    assert "15-noyabr" in text and "14-noyabr" in text and "14.11" in text
    assert "Assalomu alaykum, Abbosbek!" in text
    assert "bugun soat 12:21" in text
    assert "CRM'dan ma'lumot olib bo'lmadi (no connection)" in text


# ---------------------------------------------------------------------- reporter
class FakeClaude:
    def __init__(self, *replies: Dict[str, Any]) -> None:
        self.replies = list(replies) or [GOOD]
        self.calls: List[Tuple[str, int]] = []

    async def generate_with_images(self, system: str, text: str, images, *, timeout=None) -> str:
        self.calls.append((text, len(images)))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return json.dumps(reply)

    async def generate(self, system: str, text: str) -> str:
        self.calls.append((text, 0))
        return json.dumps(self.replies[0])


class ReporterRig:
    def __init__(self, tmp_path: Path, *replies: Dict[str, Any], **overrides: str) -> None:
        self.settings = make_settings(tmp_path, **overrides)
        self.now = datetime(2026, 11, 15, 8, 0, tzinfo=TZ)
        self.photos: List[PhotoRef] = []
        self.claude = FakeClaude(*replies)
        self.sent: List[Tuple[str, str]] = []
        self.spoken: List[str] = []
        self.crm_error: Optional[Exception] = None
        self.reporter = DailyReporter(
            self.settings,
            list_photos=self.list_photos,
            fetch_bookings=self.fetch_bookings,
            claude=self.claude,
            synthesize=self.synthesize,
            deliver=self.deliver,
            group_title="REJA BAJARILISHI GARDEN",
            now=lambda: self.now,
        )

    async def list_photos(self) -> List[PhotoRef]:
        return list(self.photos)

    async def fetch_bookings(self, day: date) -> Dict[str, Any]:
        if self.crm_error is not None:
            raise self.crm_error
        return summarize_bookings([], day)

    async def synthesize(self, text: str, out: Path) -> float:
        self.spoken.append(text)
        return 42.0

    async def deliver(self, path: Path, duration: float, caption: str) -> None:
        self.sent.append((Path(path).name, caption))

    def post_photos(self, hour: int, minute: int, first_id: int = 10, day: int = 15) -> None:
        posted = datetime(2026, 11, day, hour, minute, tzinfo=TZ)
        self.photos += [photo(first_id, posted), photo(first_id + 1, posted)]


async def test_waits_until_report_time(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.post_photos(8, 50)
    assert await rig.reporter.tick() == "not yet"
    assert rig.sent == []


async def test_sends_at_nine_when_todays_photos_are_there(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.post_photos(8, 50)
    rig.now = rig.now.replace(hour=9, minute=1)
    assert await rig.reporter.tick() == "sent"
    assert rig.sent == [("daily-report.ogg", GOOD["caption"])]
    assert rig.spoken == [GOOD["speech"]]
    assert rig.claude.calls[0][1] == 2  # both photos of the batch
    # once a day
    rig.now = rig.now.replace(hour=10)
    assert await rig.reporter.tick() == "already sent"
    assert len(rig.sent) == 1


WAITING = "waiting for yesterday's report table in the group"
OLD = dict(GOOD, report_day="13.11")


async def test_waits_for_the_table_however_late_and_sends_when_it_arrives(tmp_path):
    rig = ReporterRig(tmp_path, OLD, GOOD)
    rig.post_photos(9, 50, day=14)  # posted yesterday: the table for the day before
    rig.post_photos(9, 50, day=12, first_id=1)  # too old to be read at all
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == WAITING
    assert len(rig.claude.calls) == 1  # yesterday's batch read once
    rig.now = rig.now.replace(hour=13)
    assert await rig.reporter.tick() == WAITING
    assert len(rig.claude.calls) == 1 and rig.sent == []
    rig.post_photos(22, 40, first_id=20)  # very late
    rig.now = rig.now.replace(hour=22, minute=41)
    assert await rig.reporter.tick() == "sent"
    assert len(rig.sent) == 1 and len(rig.claude.calls) == 2


async def test_table_posted_late_yesterday_evening_is_used(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.post_photos(23, 50, day=14)
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "sent"


async def test_newest_unread_batch_is_tried_first(tmp_path):
    rig = ReporterRig(tmp_path, GOOD)
    rig.post_photos(9, 50, day=14)
    rig.post_photos(8, 40, first_id=20)
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "sent"
    assert len(rig.claude.calls) == 1


async def test_several_unread_batches_in_one_check(tmp_path):
    rig = ReporterRig(tmp_path, dict(GOOD, report_day=None), GOOD)  # newest = another picture
    rig.post_photos(8, 40, first_id=20)
    rig.post_photos(10, 30, first_id=30)
    rig.now = rig.now.replace(hour=10, minute=31)
    assert await rig.reporter.tick() == "sent"
    assert len(rig.claude.calls) == 2


async def test_gives_up_waiting_at_the_optional_deadline(tmp_path):
    rig = ReporterRig(tmp_path, OLD, DAILY_REPORT_WAIT_UNTIL="14:00")
    rig.post_photos(9, 50, day=14)
    rig.now = rig.now.replace(hour=13, minute=58)
    assert await rig.reporter.tick() == WAITING
    rig.now = rig.now.replace(hour=14, minute=0)
    assert await rig.reporter.tick() == "sent"  # with the newest photos it has
    assert "bugun" not in rig.claude.calls[-1][0].split("Rasmlar:")[1].split("\n")[0]


async def test_crm_failure_means_waiting_not_an_incomplete_report(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.crm_error = RuntimeError("no connection to the CRM")
    rig.post_photos(8, 50)
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "waiting for the CRM (no connection to the CRM)"
    assert rig.claude.calls == [] and rig.sent == []
    rig.now = rig.now.replace(minute=5)
    assert await rig.reporter.tick() == "retry later"
    rig.crm_error = None
    rig.now = rig.now.replace(minute=11)
    assert await rig.reporter.tick() == "sent"


async def test_crm_failure_at_the_deadline_sends_and_says_so(tmp_path):
    rig = ReporterRig(tmp_path, DAILY_REPORT_WAIT_UNTIL="14:00")
    rig.crm_error = RuntimeError("CRM login failed: wrong login or password")
    rig.post_photos(8, 50)
    rig.now = rig.now.replace(hour=14, minute=0)
    assert await rig.reporter.tick() == "sent"
    assert "CRM login failed" in rig.claude.calls[0][0]


async def test_failure_retries_after_ten_minutes(tmp_path):
    rig = ReporterRig(tmp_path, {"report_day": "14.11"})  # no speech => ReportError
    rig.post_photos(8, 50)
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "failed"
    rig.now = rig.now.replace(minute=5)
    assert await rig.reporter.tick() == "retry later"
    rig.claude.replies = [GOOD]
    rig.now = rig.now.replace(minute=11)
    assert await rig.reporter.tick() == "sent"


async def test_sent_state_survives_a_restart(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.post_photos(8, 50)
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "sent"
    again = ReporterRig(tmp_path)
    again.now = rig.now.replace(hour=11)
    assert await again.reporter.tick() == "already sent"


async def test_off_without_time(tmp_path):
    rig = ReporterRig(tmp_path, DAILY_REPORT_TIME="")
    assert await rig.reporter.tick() == "off"


async def test_run_now_before_report_time_does_not_count_as_todays(tmp_path):
    rig = ReporterRig(tmp_path)  # 08:00
    rig.post_photos(7, 50)
    report = await rig.reporter.run_now()
    assert report.report_day == "14.11" and len(rig.sent) == 1
    rig.now = rig.now.replace(hour=9, minute=0)
    assert await rig.reporter.tick() == "sent"


async def test_complete_run_now_counts_as_todays_report(tmp_path):
    rig = ReporterRig(tmp_path)
    rig.post_photos(9, 50)
    rig.now = rig.now.replace(hour=10)
    await rig.reporter.run_now()
    assert await rig.reporter.tick() == "already sent"
    assert len(rig.sent) == 1


async def test_incomplete_run_now_does_not_count(tmp_path):
    rig = ReporterRig(tmp_path, OLD, GOOD)
    rig.post_photos(9, 50, day=14)
    rig.now = rig.now.replace(hour=10)
    report = await rig.reporter.run_now()
    assert report.report_day == "13.11"
    rig.post_photos(10, 30, first_id=40)
    rig.now = rig.now.replace(hour=10, minute=31)
    assert await rig.reporter.tick() == "sent"
    assert len(rig.sent) == 2


# ---------------------------------------------------------------- /hisobot command
async def _outgoing_saved(rig: Rig, text: str):
    from assistant.userbot import IncomingMessage

    from .test_userbot import ME

    return await rig.assistant.on_message(
        IncomingMessage(chat_id=ME, message_id=777, outgoing=True, text=text, raw_text=text,
                        chat_name="Saved", sender_name="Me", sender_is_bot=False, is_contact=False)
    )


async def test_hisobot_command_runs_the_report(tmp_path):
    rig = Rig(tmp_path)
    calls: List[str] = []

    async def report() -> None:
        calls.append("run")

    rig.assistant.report_command = report
    await _outgoing_saved(rig, "/hisobot")
    await rig.assistant._report_task
    assert calls == ["run"]
    assert rig.transport.notes[-1].startswith("Hisobot tayyorlanyapti")


async def test_hisobot_command_reports_errors_and_missing_setup(tmp_path):
    rig = Rig(tmp_path)
    await _outgoing_saved(rig, "/hisobot")
    assert "sozlanmagan" in rig.transport.notes[-1]

    async def broken() -> None:
        raise RuntimeError("no photos")

    rig.assistant.report_command = broken
    await _outgoing_saved(rig, "/hisobot")
    await rig.assistant._report_task
    assert rig.transport.notes[-1] == "Hisobotni tayyorlab bo'lmadi: no photos"


# ------------------------------------------------------------- CEO-level report
def test_overview_adds_the_next_days():
    from assistant.daily_report import bookings_overview

    rows = [
        {"date": "2026-11-18", "hall": "1-qavat", "time": "19:00", "tables": [1], "guests": "10"},
        {"date": "2026-11-19", "hall": "1-qavat", "time": "13:00", "tables": [1], "guests": "6"},
        {"date": "2026-11-19", "hall": "1-qavat", "time": "19:00", "tables": [2], "guests": "20"},
        {"date": "2026-11-30", "hall": "1-qavat", "time": "19:00", "tables": [2], "guests": "20"},
    ]
    overview = bookings_overview(rows, date(2026, 11, 18))
    assert overview["jami"]["bronlar"] == 1
    upcoming = overview["keyingi_kunlar"]
    assert len(upcoming) == 6
    assert upcoming[0] == {"sana": "2026-11-19", "hafta_kuni": "payshanba", "bronlar": 2, "mehmonlar": 26}
    assert all(day["sana"] != "2026-11-30" for day in upcoming)


def test_prompt_asks_for_analysis_forecast_and_three_steps(tmp_path):
    s = make_settings(tmp_path)
    now = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)
    text = build_report_prompt(s, now, [], {"jami": {}}, None, "REJA BAJARILISHI GARDEN")
    assert "18-noyabr 2026, chorshanba" in text and "17-noyabr, seshanba" in text
    assert "uch qadam" in text and "kuniga qancha" in text and '"advice"' in text


def test_long_written_summary_does_not_fit_a_caption():
    from assistant.daily_report import caption_fits

    assert caption_fits("a" * 1024)
    assert not caption_fits("a" * 1025)
    assert not caption_fits("📊" * 513)  # an emoji counts twice


async def test_stalled_text_to_speech_is_cut_off_and_retried(tmp_path, monkeypatch):
    import asyncio
    import sys
    import types

    from assistant import voice

    calls: List[str] = []

    class Communicate:
        def __init__(self, text, voice_name, rate="+0%"):
            self.text = text

        async def save(self, path):
            calls.append(path)
            if len(calls) == 1:
                await asyncio.sleep(3600)  # the service stalls mid-stream
            Path(path).write_bytes(b"mp3")

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    monkeypatch.setattr(voice, "tts_timeout", lambda text: 0.05)
    await voice.save_speech("Salom", "uz-UZ-MadinaNeural", tmp_path / "a.mp3", pause=0)
    assert len(calls) == 2 and (tmp_path / "a.mp3").read_bytes() == b"mp3"

    calls.clear()

    class Broken(Communicate):
        async def save(self, path):
            calls.append(path)
            raise ConnectionError("no internet")

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Broken))
    with pytest.raises(ConnectionError):
        await voice.save_speech("Salom", "uz-UZ-MadinaNeural", tmp_path / "b.mp3", attempts=3, pause=0)
    assert len(calls) == 3
