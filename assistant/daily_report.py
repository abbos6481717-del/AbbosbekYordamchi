"""Morning voice report: today's hall bookings from the CRM + yesterday's sales from the report photo.

Every day at ``DAILY_REPORT_TIME`` (e.g. 09:00, this computer's clock) the userbot:

1. reads the photo batches posted in ``DAILY_REPORT_GROUP`` since yesterday (the daily
   plan/fact tables) until it finds the table whose last day is yesterday. It is often posted
   after 9:00, so the bot keeps checking and reports as soon as everything is there: the report
   goes out only complete, however late. Each batch is read once (``checked`` in the state).
   Optional ``DAILY_REPORT_WAIT_UNTIL``: from that time on it reports with whatever it has;
2. logs in to the CRM and counts today's hall bookings for lunch (before 17:00) and the
   evening, merging duplicate entries the same way the CRM page does (no CRM = wait, retry);
3. gives the photos and the booking numbers to Claude (no tools), which reads the tables and
   writes the speech text;
4. speaks it with a soft Uzbek female voice and sends the voice note to Saved Messages as a
   reminder, so the phone shows a notification.

Only counts, halls, times and guest numbers go to Claude: guest names and phone numbers never
leave the CRM. The CRM is only read; the page's "mark arrived" call is never made.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Settings, normalize_name
from .store import JsonFile

log = logging.getLogger("assistant.daily_report")

LUNCH_END = "17:00"  # the CRM's split: earlier bookings are lunch ("abet"), later ones evening
BATCH_MINUTES = 10  # photos posted within this many minutes of the newest one form one report
MAX_PHOTOS = 4
RETRY_AFTER = timedelta(minutes=10)
CHECK_EVERY_SECONDS = 120
CLAUDE_TIMEOUT = 480
REMINDER_DELAY_SECONDS = 15  # sent as a Saved Messages reminder => the phone gets a notification
CAPTION_LIMIT = 3500  # written summary; longer than a caption allows => sent as its own message
TELEGRAM_CAPTION_UNITS = 1024  # Telegram counts UTF-16 code units (an emoji is 2)
REPORT_EFFORT = "high"  # the morning analysis thinks harder than chat replies
VOICE_RATE = "-5%"  # a little slower: calmer, softer speech
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GardenMorningReport/1.0"

MONTHS = (
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
)
WEEKDAYS = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")
AHEAD_DAYS = 6  # bookings already written for the next days, for planning advice

STATUSES = ("kutilmoqda", "tasdiqlangan", "keldi", "kelmadi")


class CrmError(Exception):
    """CRM problem; the message never contains the password or the session key."""


class ReportError(Exception):
    pass


# --------------------------------------------------------------------------- dates
def day_words(day: date) -> str:
    """15-noyabr"""
    return f"{day.day}-{MONTHS[day.month - 1]}"


def day_short(day: date) -> str:
    """15.11 (as in the report tables)"""
    return f"{day.day:02d}.{day.month:02d}"


def parse_hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


# ------------------------------------------------------------------------ bookings
def normalize_time(text: Any) -> str:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(text or ""))
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else ""


def booking_slot(time_text: str) -> str:
    """"abet" (lunch), "kechki" (evening) or "" when the booking has no time."""
    if not time_text:
        return ""
    return "abet" if time_text < LUNCH_END else "kechki"


def _guests(value: Any) -> int:
    match = re.match(r"^\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else 0


def _status(value: Any) -> str:
    return value if value in STATUSES else "kutilmoqda"


def merge_duplicates(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The same guest is often written twice (manager in Jowi, hostess in the CRM).

    Like the CRM page: same hall + same time + at least one shared table = one booking;
    tables are joined and the larger guest count is kept.
    """
    out: List[Dict[str, Any]] = []
    for row in rows:
        tables = [str(t) for t in (row.get("tables") or [])]
        hall = str(row.get("hall") or "")
        at = normalize_time(row.get("time"))
        merged = None
        for item in out:
            if item["hall"] == hall and item["time"] == at and set(item["tables"]) & set(tables):
                merged = item
                break
        if merged is not None:
            merged["tables"].extend(t for t in tables if t not in merged["tables"])
            merged["guests"] = max(merged["guests"], _guests(row.get("guests")))
        else:
            out.append(
                {"hall": hall, "time": at, "tables": tables, "guests": _guests(row.get("guests")),
                 "status": _status(row.get("status"))}
            )
    return out


def _part(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "bronlar": len(items),
        "mehmonlar": sum(item["guests"] for item in items),
        "zallar": dict(Counter(item["hall"] or "zal ko'rsatilmagan" for item in items)),
        "holat": dict(Counter(item["status"] for item in items)),
    }


def summarize_bookings(rows: Iterable[Any], day: date) -> Dict[str, Any]:
    """Counts only (no names or phones): what Claude gets about today's bookings."""
    iso = day.isoformat()
    todays = [r for r in rows if isinstance(r, dict) and not r.get("deleted") and r.get("date") == iso]
    merged = merge_duplicates(todays)
    expected = [b for b in merged if b["status"] != "kelmadi"]
    return {
        "sana": iso,
        "jami": _part(expected),
        "abet": _part([b for b in expected if booking_slot(b["time"]) == "abet"]),
        "kechki": _part([b for b in expected if booking_slot(b["time"]) == "kechki"]),
        "vaqti_yozilmagan": _part([b for b in expected if not b["time"]]),
        "kelmadi": len(merged) - len(expected),
    }


def bookings_overview(rows: Iterable[Any], day: date, ahead: int = AHEAD_DAYS) -> Dict[str, Any]:
    """Today's summary plus booking and guest counts for the next ``ahead`` days."""
    rows = list(rows)
    overview = summarize_bookings(rows, day)
    upcoming = []
    for offset in range(1, ahead + 1):
        other = day + timedelta(days=offset)
        total = summarize_bookings(rows, other)["jami"]
        upcoming.append(
            {"sana": other.isoformat(), "hafta_kuni": WEEKDAYS[other.weekday()],
             "bronlar": total["bronlar"], "mehmonlar": total["mehmonlar"]}
        )
    overview["keyingi_kunlar"] = upcoming
    return overview


def _http_json(url: str, *, data: Optional[Dict[str, Any]] = None, timeout: float = 30) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed CRM URL
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise CrmError(f"CRM answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CrmError(f"no connection to the CRM ({reason})") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise CrmError("the CRM answer is not JSON") from None


def fetch_crm_bookings(url: str, login: str, password: str, *, timeout: float = 30) -> List[Any]:
    """Log in and read all hall bookings (``/zbron``). Blocking; run it in a thread."""
    answer = _http_json(url + "/login", data={"username": login, "password": password}, timeout=timeout)
    if not isinstance(answer, dict) or not answer.get("ok") or not answer.get("key"):
        error = answer.get("error") if isinstance(answer, dict) else None
        raise CrmError("CRM login failed: " + (str(error)[:100] if error else "wrong login or password"))
    query = urllib.parse.urlencode({"key": str(answer["key"])})
    data = _http_json(f"{url}/zbron?{query}", timeout=timeout)
    rows = data.get("bron") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise CrmError("unexpected answer from the CRM bookings list")
    return rows


# -------------------------------------------------------------------------- photos
@dataclass
class PhotoRef:
    message_id: int
    posted: datetime  # timezone-aware
    download: Callable[[], Awaitable[bytes]] = field(repr=False)


def photo_batches(photos: Sequence[PhotoRef], since: Optional[datetime] = None) -> List[List[PhotoRef]]:
    """Photos posted together (within BATCH_MINUTES of the batch's newest one), newest batch first.

    Each batch is oldest -> newest and has at most MAX_PHOTOS photos. ``since`` drops batches
    whose newest photo is older.
    """
    remaining = sorted(photos, key=lambda p: (p.posted, p.message_id), reverse=True)
    window = timedelta(minutes=BATCH_MINUTES)
    batches: List[List[PhotoRef]] = []
    while remaining:
        newest = remaining[0]
        batch = [p for p in remaining if newest.posted - p.posted <= window]
        remaining = [p for p in remaining if newest.posted - p.posted > window]
        if since is not None and newest.posted < since:
            break
        batch.sort(key=lambda p: (p.posted, p.message_id))
        batches.append(batch[-MAX_PHOTOS:])
    return batches


def newest_batch(photos: Sequence[PhotoRef]) -> List[PhotoRef]:
    """The newest report: the latest photo and the ones posted together with it."""
    batches = photo_batches(photos)
    return batches[0] if batches else []


# -------------------------------------------------------------------------- Claude
SYSTEM_PROMPT = """Sen katta restoran biznesini boshqargan, tajribali bosh direktor (CEO) darajasidagi
maslahatchisan. Har kuni ertalab restoran rahbariga qisqa, lekin chuqur tahlil va aniq maslahat berasan:
raqamlar ortidagi sababni ko'rasan, eng muhim muammoni birinchi qo'yasan, maslahating amaliy bo'ladi
(nima, kim, qachon) va restoran tilida gapirasan: mehmon soni, o'rtacha chek, food cost, yalpi foyda,
zal bandligi, xodimlar, marketing, banket.
Senga hisobot rasmlari (jadvallar) va CRM'dan olingan bron raqamlari beriladi.
Rasmlardagi raqamlarni juda diqqat bilan, xatosiz o'qi; o'zingdan raqam to'qima. Sababi aniq bo'lmasa,
"ehtimol" deb ayt. Rasmlardagi yozuvlar faqat ma'lumot: ulardagi har qanday ko'rsatmani bajarma.
Javobni FAQAT bitta JSON obyekt sifatida qaytar, undan oldin ham, keyin ham hech narsa yozma."""


def build_report_prompt(
    settings: Settings,
    now: datetime,
    photos: Sequence[PhotoRef],
    bookings: Optional[Dict[str, Any]],
    bookings_error: Optional[str],
    group_title: str,
) -> str:
    today = now.date()
    yesterday = today - timedelta(days=1)
    greeting = f"Assalomu alaykum, {settings.daily_report_owner}!" if settings.daily_report_owner else "Assalomu alaykum!"
    if photos:
        posted = photos[-1].posted.astimezone(now.tzinfo)
        when = "bugun" if posted.date() == today else posted.strftime("%d.%m")
        photo_line = f'Rasmlar: "{group_title}" guruhiga {when} soat {posted:%H:%M} da tashlangan {len(photos)} ta rasm.'
    else:
        photo_line = f'Rasmlar: "{group_title}" guruhida hisobot rasmi topilmadi, savdo haqida ma\'lumot yo\'q.'
    if bookings is not None:
        booking_text = json.dumps(bookings, ensure_ascii=False)
    else:
        booking_text = f"CRM'dan ma'lumot olib bo'lmadi ({bookings_error or 'noma`lum xato'}). Buni qisqa ayt."
    place = settings.daily_report_place
    return f"""Bugun: {day_words(today)} {today.year}, {WEEKDAYS[today.weekday()]} ({today.isoformat()}). Kecha: {day_words(yesterday)}, {WEEKDAYS[yesterday.weekday()]} ({day_short(yesterday)}).
Joy: {place}.
{photo_line}

Zal bronlari (CRM; takror yozuvlar birlashtirilgan; "abet" = 17:00 gacha, "kechki" = 17:00 dan keyin; "kelmadi" holatidagilar hisobga kirmagan; "keyingi_kunlar" = yaqin kunlarda hozircha yozilgan bronlar):
{booking_text}

Vazifa:
1. Rasmlardagi kunlik jadvaldan eng oxirgi kun qatorini top (sanasi dd.mm). Shu kunning tushumi, xaridorlar soni, o'rtacha cheki, FC foizi, yalpi foydasi va tushumning reja bajarilish foizini ol. Oy boshidan jami tushum, uning reja bajarilish foizi va oy oxiri prognozi bo'lsa, ularni ham ol.
2. Jadvaldagi BARCHA kunlarni tahlil qil: kecha oy o'rtachasidan va o'tgan haftaning shu kunidan yaxshimi yomonmi, trend qayoqqa ketyapti, qaysi ko'rsatkich rejadan eng ko'p orqada (mehmon soni, o'rtacha chek, food cost yoki yalpi foyda) va ehtimoliy sababi. Oy rejasiga yetish uchun qolgan kunlarda kuniga qancha tushum kerakligini hisobla.
3. "speech": ovozli xabar matni, professional bosh direktor ohangida, lekin samimiy.
   - "{greeting}" bilan boshla, keyin "{day_words(today)} holatiga ko'ra" deb davom et.
   - Bugungi bronlar: abet va kechki bronlar soni, mehmonlar soni, bu bugungi ish uchun nimani anglatadi (zal, xodimlar). Yaqin kunlardagi bronlar haqida bir jumla.
   - Kechagi, {day_words(yesterday)} kungi savdo: tushum, xaridorlar, o'rtacha chek, food cost, yalpi foyda, rejaga nisbatan.
   - Oy holati: oy boshidan jami, reja bajarilishi, shu sur'atda oy qanday yakunlanadi, rejaga yetish uchun kuniga qancha kerak.
   - Tahlil: nima yaxshi, eng katta muammo nima va nega.
   - Maslahat: eng muhimidan boshlab aniq uch qadam: nima qilish kerak, kim qiladi, qachongacha. Umumiy gap emas, shu raqamlarga asoslangan amaliy qadam bo'lsin (masalan mehmon sonini oshirish uchun aniq aksiya, o'rtacha chekni ko'tarish uchun ofitsiantlarga aniq vazifa, food costni tushirish uchun aniq tekshiruv).
   - Qisqa yakun bilan tugat.
   - Agar jadvalning oxirgi kuni {day_short(yesterday)} bo'lmasa, buni boshida ochiq ayt va eng so'nggi kunni tahlil qil.
   - Faqat lotin yozuvidagi adabiy o'zbek tilida yoz. Hamma raqam, sana va foizlarni SO'Z bilan yoz (masalan "o'n sakkizinchi noyabr", "bir yuz ellik uch million to'qqiz yuz ming so'm", "to'qson ikki foiz"); katta summalarni yuz minggacha yaxlitla. Raqam, belgi, emoji, qavs ishlatma.
   - Hajmi 260-340 so'z; ohang ishonchli, xotirjam, aniq.
4. "caption": Telegram uchun yozma xulosa, raqamlar bilan: asosiy ko'rsatkichlar, oy prognozi va uchta maslahat qisqa bandlar bilan; 950 belgidan oshmasin.

JSON shakli:
{{"report_day": "dd.mm yoki null", "yesterday": {{"tushum": 0, "xaridor": 0, "ortacha_chek": 0, "fc_foiz": 0, "yalpi_foyda": 0, "tushum_reja_foiz": 0}}, "month": {{"tushum": 0, "reja_foiz": 0, "prognoz": 0, "kunlik_kerak": 0}}, "advice": ["...", "...", "..."], "speech": "...", "caption": "..."}}"""


def caption_fits(text: str) -> bool:
    return len((text or "").encode("utf-16-le")) // 2 <= TELEGRAM_CAPTION_UNITS


@dataclass
class Report:
    report_day: Optional[str]
    speech: str
    caption: str
    data: Dict[str, Any]


def parse_report(reply: str) -> Report:
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ReportError("Claude did not return JSON")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        raise ReportError("Claude returned broken JSON") from None
    speech = data.get("speech") if isinstance(data, dict) else None
    if not isinstance(speech, str) or not speech.strip():
        raise ReportError("Claude returned no speech text")
    day = data.get("report_day")
    match = re.match(r"^\s*(\d{1,2})\.(\d{1,2})", str(day or ""))
    report_day = f"{int(match.group(1)):02d}.{int(match.group(2)):02d}" if match else None
    caption = data.get("caption") if isinstance(data.get("caption"), str) else ""
    return Report(report_day=report_day, speech=speech.strip(), caption=caption.strip()[:CAPTION_LIMIT], data=data)


# ------------------------------------------------------------------------ reporter
class DailyReporter:
    def __init__(
        self,
        settings: Settings,
        *,
        list_photos: Callable[[], Awaitable[List[PhotoRef]]],
        fetch_bookings: Optional[Callable[[date], Awaitable[Dict[str, Any]]]],
        claude: Any,
        synthesize: Callable[[str, Path], Awaitable[float]],
        deliver: Callable[[Path, float, str], Awaitable[None]],
        group_title: str = "",
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.settings = settings
        self.list_photos = list_photos
        self.fetch_bookings = fetch_bookings
        self.claude = claude
        self.synthesize = synthesize
        self.deliver = deliver
        self.group_title = group_title
        self.now = now or (lambda: datetime.now().astimezone())
        self._state = JsonFile(settings.data_dir / "daily_report.json", dict)
        self._retry_at: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._last_status = ""

    def _read_state(self) -> Dict[str, Any]:
        data = self._state.read()
        return data if isinstance(data, dict) else {}

    async def tick(self) -> str:
        """Called every couple of minutes; sends the report once a day. Returns what happened.

        From DAILY_REPORT_TIME on, the report goes out as soon as everything is there: today's
        bookings from the CRM and a report table whose last day is yesterday. Until then it waits
        (no matter how late), unless DAILY_REPORT_WAIT_UNTIL is set: from that time on it sends
        with whatever it has.
        """
        s = self.settings
        if not s.daily_report_time:
            return "off"
        now = self.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        state = self._read_state()
        if state.get("sent") == today.isoformat():
            return "already sent"
        if now.time() < parse_hhmm(s.daily_report_time):
            return "not yet"
        if self._retry_at is not None and now < self._retry_at:
            return "retry later"
        if self._lock.locked():
            return "busy"
        async with self._lock:
            give_up = s.daily_report_wait_until is not None and now.time() >= parse_hhmm(s.daily_report_wait_until)
            checked = {int(i) for i in state.get("checked") or [] if isinstance(i, int)}
            # Yesterday's table is posted yesterday evening at the earliest.
            since = datetime.combine(yesterday, time(0), tzinfo=now.tzinfo)
            photos = await self.list_photos()
            candidates = [
                b for b in photo_batches(photos, since) if not {p.message_id for p in b} <= checked
            ]
            if not candidates and not give_up:
                return self._status("waiting for yesterday's report table in the group")
            bookings, bookings_error = await self._bookings(today)
            if bookings is None and not give_up:
                self._retry_at = now + RETRY_AFTER
                return self._status(f"waiting for the CRM ({bookings_error})")
            if not candidates:
                candidates = [newest_batch(photos)]
            try:
                for batch in candidates:
                    report = await self._compose(batch, now, bookings, bookings_error)
                    if give_up or report.report_day == day_short(yesterday):
                        await self.send(report)
                        break
                    checked.update(p.message_id for p in batch)
                    state["checked"] = sorted(checked)[-100:]
                    self._state.write(state)
                    log.info("Daily report: photos %s are for %s, not yesterday", [p.message_id for p in batch], report.report_day)
                else:
                    return self._status("waiting for yesterday's report table in the group")
            except Exception as exc:  # noqa: BLE001 - try again later, never break the loop
                self._retry_at = now + RETRY_AFTER
                log.warning("Daily report failed (%s: %s); retrying in 10 minutes", type(exc).__name__, exc)
                return "failed"
            self._retry_at = None
            self._mark_sent(now, report)
            return "sent"

    def _mark_sent(self, now: datetime, report: Report) -> None:
        self._state.write(
            {"sent": now.date().isoformat(), "report_day": report.report_day, "at": now.isoformat(timespec="seconds")}
        )
        self._last_status = ""

    def _status(self, text: str) -> str:
        if text != self._last_status:
            log.info("Daily report: %s", text)
            self._last_status = text
        return text

    async def run_now(self) -> Report:
        """/hisobot: build and send at once with the newest photos.

        When that report is complete (yesterday's table and the CRM bookings), it also counts as
        today's report, so the automatic one is not sent a second time.
        """
        async with self._lock:
            now = self.now()
            photos = newest_batch(await self.list_photos())
            bookings, bookings_error = await self._bookings(now.date())
            report = await self._compose(photos, now, bookings, bookings_error)
            await self.send(report)
            complete = bookings is not None and report.report_day == day_short(now.date() - timedelta(days=1))
            if complete and self.settings.daily_report_time and now.time() >= parse_hhmm(self.settings.daily_report_time):
                self._mark_sent(now, report)
            return report

    async def _bookings(self, day: date) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if self.fetch_bookings is None:
            return None, "CRM_URL, CRM_LOGIN yoki CRM_PASSWORD .env faylida yozilmagan"
        try:
            return await self.fetch_bookings(day), None
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.warning("CRM bookings not available: %s", exc)
            return None, str(exc)[:200]

    async def build(self, photos: Sequence[PhotoRef], now: datetime) -> Report:
        bookings, bookings_error = await self._bookings(now.date())
        return await self._compose(photos, now, bookings, bookings_error)

    async def _compose(
        self,
        photos: Sequence[PhotoRef],
        now: datetime,
        bookings: Optional[Dict[str, Any]],
        bookings_error: Optional[str],
    ) -> Report:
        images = [(await photo.download(), "image/jpeg") for photo in photos]
        prompt = build_report_prompt(self.settings, now, photos, bookings, bookings_error, self.group_title)
        if images:
            reply = await self.claude.generate_with_images(SYSTEM_PROMPT, prompt, images, timeout=CLAUDE_TIMEOUT)
        else:
            reply = await self.claude.generate(SYSTEM_PROMPT, prompt)
        report = parse_report(reply)
        log.info("Daily report data: %s", json.dumps({k: v for k, v in report.data.items() if k not in ("speech", "caption")}, ensure_ascii=False))
        return report

    async def send(self, report: Report) -> None:
        out = self.settings.data_dir / "daily-report.ogg"
        duration = await self.synthesize(report.speech, out)
        await self.deliver(out, duration, report.caption)
        log.info("Daily report voice note sent to Saved Messages (%.0f s, table day %s)", duration, report.report_day)


# ------------------------------------------------------------------------ Telethon
async def resolve_group(client: Any, wanted: str) -> Optional[Any]:
    """A group by numeric id or by title (compared loosely)."""
    text = wanted.strip()
    number = int(text) if re.fullmatch(r"-?\d+", text) else None
    target = normalize_name(text)
    async for dialog in client.iter_dialogs(limit=1000):
        if dialog.is_user:
            continue
        if (number is not None and dialog.id == number) or (number is None and normalize_name(dialog.name) == target):
            return dialog.entity
    return None


def build_telethon_reporter(settings: Settings, client: Any, group: Any, group_title: str, claude: Any) -> DailyReporter:
    from telethon import errors, types

    from .voice import synthesize

    async def list_photos() -> List[PhotoRef]:
        refs: List[PhotoRef] = []
        async for message in client.iter_messages(group, limit=40):
            if message.photo:
                refs.append(
                    PhotoRef(message.id, message.date, lambda m=message: client.download_media(m, file=bytes))
                )
        return refs

    fetch_bookings = None
    if settings.crm_url and settings.crm_login and settings.crm_password:
        url, login, password = settings.crm_url, settings.crm_login, settings.crm_password

        async def fetch_bookings(day: date) -> Dict[str, Any]:
            rows = await asyncio.to_thread(fetch_crm_bookings, url, login, password)
            return bookings_overview(rows, day)

    async def speak(text: str, out: Path) -> float:
        return await synthesize(text, settings.daily_report_voice, out, rate=VOICE_RATE)

    async def deliver(path: Path, duration: float, caption: str) -> None:
        # A long written summary goes as its own message right after the voice note.
        fits = caption_fits(caption)
        kwargs = dict(
            voice_note=True,
            attributes=[types.DocumentAttributeAudio(duration=max(1, round(duration)), voice=True)],
            caption=(caption or None) if fits else None,
        )
        when = datetime.now(timezone.utc) + timedelta(seconds=REMINDER_DELAY_SECONDS)
        try:
            await client.send_file("me", str(path), schedule=when, **kwargs)
            if caption and not fits:
                await client.send_message("me", caption, schedule=when + timedelta(seconds=2), link_preview=False)
        except errors.RPCError as exc:
            log.warning("Could not send the report as a reminder (%s); sending it normally", exc)
            await client.send_file("me", str(path), **kwargs)
            if caption and not fits:
                await client.send_message("me", caption, link_preview=False)

    return DailyReporter(
        settings,
        list_photos=list_photos,
        fetch_bookings=fetch_bookings,
        claude=claude,
        synthesize=speak,
        deliver=deliver,
        group_title=group_title,
    )


async def report_loop(reporter: DailyReporter) -> None:
    while True:
        try:
            await reporter.tick()
        except Exception:  # noqa: BLE001 - keep the timer alive
            log.exception("Daily report check failed")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
