"""Settings loading.

Settings come from exactly one source and are never mixed:

* local development: only the ``.env`` file (read with ``dotenv_values``);
  variables exported in the shell are ignored;
* container (Railway): ``bot.py --settings-from-env`` reads the process
  environment and passes it here as ``source``; ``.env`` is not read at all.

Nothing is ever written to ``os.environ`` so secrets do not leak into child
processes. Error messages never echo secret values back.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform.startswith("win")

SETTING_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OWNER_ID",
    "AUTO_SEND",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_BIN",
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CLAUDE_TIMEOUT_SECONDS",
    "LIMIT_COOLDOWN_MINUTES",
    "OWNER_TAKEOVER_MINUTES",
    "HISTORY_LIMIT",
    "DATA_DIR",
    "PROFILE_PATH",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "USERBOT_CONTACTS_ONLY",
    "USERBOT_MAX_REPLIES_PER_HOUR",
    "USERBOT_REPLY_DELAY_SECONDS",
    "USERBOT_GREETING_TEXT",
    "USERBOT_GREETING_FOLLOWUP",
    "USERBOT_GREETING_SEND",
    "USERBOT_GREETING_MODE",
    "USERBOT_VOICE",
    "USERBOT_INTRO_TEXT",
    "USERBOT_FAST_CONTACTS",
    "USERBOT_IGNORE_CONTACTS",
    "USERBOT_SCHEDULE_TO",
    "USERBOT_SCHEDULE_TEXT",
    "USERBOT_SCHEDULE_EVERY_MINUTES",
    "DAILY_REPORT_TIME",
    "DAILY_REPORT_WAIT_UNTIL",
    "DAILY_REPORT_GROUP",
    "DAILY_REPORT_VOICE",
    "DAILY_REPORT_PLACE",
    "DAILY_REPORT_OWNER",
    "CRM_URL",
    "CRM_LOGIN",
    "CRM_PASSWORD",
)

GREETING_MODES = ("voice", "text", "both")

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

AUTH_LOCAL_LOGIN = "this machine's claude login"
AUTH_TOKEN_ENV_FILE = "subscription token from .env"
AUTH_TOKEN_ENVIRONMENT = "subscription token from environment variables"


class ConfigError(Exception):
    """Invalid or missing configuration. The message never contains secrets."""


@dataclass(frozen=True)
class Settings:
    bot_token: Optional[str] = field(repr=False)
    owner_id: Optional[int]
    auto_send: bool
    claude_oauth_token: Optional[str] = field(repr=False)
    claude_bin: str
    claude_model: Optional[str]
    claude_effort: str
    claude_timeout: int
    limit_cooldown_minutes: int
    owner_takeover_minutes: int
    history_limit: int
    data_dir: Path
    profile_path: Path
    env_file: Optional[Path] = None
    from_environment: bool = False
    # Userbot mode (personal account through the Telegram client API, no Premium needed).
    api_id: Optional[int] = None
    api_hash: Optional[str] = field(default=None, repr=False)
    userbot_contacts_only: bool = True
    userbot_max_replies_per_hour: int = 20
    userbot_reply_delay_seconds: int = 0
    userbot_greeting_text: Optional[str] = None
    userbot_greeting_followup: Optional[str] = None  # text sent right after the greeting (menu link)
    userbot_greeting_send: bool = False  # send the greeting even in PREVIEW mode
    userbot_greeting_mode: str = "voice"
    userbot_voice: str = "uz-UZ-SardorNeural"
    # "I am X's online assistant": put before the bot's first message in each chat; when set,
    # Claude speaks as the owner's assistant instead of as the owner.
    userbot_intro_text: Optional[str] = None
    # Contacts (display names or usernames) answered at once, without the reply delay.
    userbot_fast_contacts: Tuple[str, ...] = ()
    # Contacts the bot never answers (names, @usernames or ids).
    userbot_ignore_contacts: Tuple[str, ...] = ()
    # One recurring message: to whom (name, @username or id), what, and how often.
    userbot_schedule_to: Optional[str] = None
    userbot_schedule_text: Optional[str] = None
    userbot_schedule_every_minutes: int = 0
    # Morning voice report to Saved Messages (see daily_report.py). Off when the time is empty.
    daily_report_time: Optional[str] = None  # "HH:MM", local time of this computer
    daily_report_wait_until: Optional[str] = None  # wait for the new report photo until this time
    daily_report_group: Optional[str] = None  # group title or numeric id with the report photos
    daily_report_voice: str = "uz-UZ-MadinaNeural"
    daily_report_place: str = "Olimp Garden"
    daily_report_owner: Optional[str] = None  # name used in the greeting ("Assalomu alaykum, X!")
    crm_url: Optional[str] = None
    crm_login: Optional[str] = None
    crm_password: Optional[str] = field(default=None, repr=False)

    @property
    def userbot_session(self) -> Path:
        """Telethon session file (without the .session suffix). Gives full account access."""
        return self.data_dir / "userbot"

    @property
    def claude_auth_mode(self) -> str:
        if not self.claude_oauth_token:
            return AUTH_LOCAL_LOGIN
        if self.from_environment:
            return AUTH_TOKEN_ENVIRONMENT
        return AUTH_TOKEN_ENV_FILE

    @property
    def claude_workdir(self) -> Path:
        return self.data_dir / "claude-run"


def _where(env_file: Path, from_environment: bool) -> str:
    if from_environment:
        return "the environment variables"
    return str(env_file)


def default_claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    name = "claude.exe" if IS_WINDOWS else "claude"
    return str(Path.home() / ".local" / "bin" / name)


def load_settings(
    env_file: Path,
    *,
    require_telegram: bool = True,
    require_userbot: bool = False,
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load and validate settings.

    ``require_telegram`` asks for the bot token and OWNER_ID (Business bot mode);
    ``require_userbot`` asks for TELEGRAM_API_ID / TELEGRAM_API_HASH instead.
    ``source`` replaces the ``.env`` file (container mode); ``env_file`` is then
    only used as the base for relative paths. ``overrides`` is for tests.
    """
    env_file = Path(env_file)
    from_environment = source is not None
    where = _where(env_file, from_environment)

    if source is not None:
        raw: Dict[str, Optional[str]] = dict(source)
    else:
        if not env_file.is_file():
            raise ConfigError(
                f"{env_file} not found. Create it with: "
                "cp .env.example .env && chmod 600 .env"
            )
        raw = dict(dotenv_values(env_file))
    if overrides:
        raw.update(overrides)

    def get(key: str) -> str:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) else ""

    def get_int(key: str, default: int, minimum: int) -> int:
        text = get(key)
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            raise ConfigError(f"{key} must be a whole number (check {where})") from None
        if value < minimum:
            raise ConfigError(f"{key} must be at least {minimum} (check {where})")
        return value

    base_dir = env_file.parent

    # Telegram
    bot_token = get("TELEGRAM_BOT_TOKEN") or None
    owner_text = get("OWNER_ID")
    owner_id: Optional[int] = None
    if owner_text:
        if not owner_text.isdigit():
            raise ConfigError(f"OWNER_ID must contain only digits (check {where})")
        owner_id = int(owner_text)
    if bot_token and not _BOT_TOKEN_RE.match(bot_token):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN does not look like a bot token from @BotFather "
            f"(expected <number>:<text>; check {where})"
        )
    if require_telegram:
        if not bot_token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN is not set (check {where})")
        if owner_id is None:
            raise ConfigError(f"OWNER_ID is not set (check {where})")

    def get_bool(key: str, default: bool) -> bool:
        text = get(key).lower()
        if not text:
            return default
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(f"{key} must be true or false (check {where})")

    auto_send = get_bool("AUTO_SEND", False)

    # Userbot (Telegram client API)
    api_id_text = get("TELEGRAM_API_ID")
    api_id: Optional[int] = None
    if api_id_text:
        if not api_id_text.isdigit():
            raise ConfigError(f"TELEGRAM_API_ID must contain only digits (check {where})")
        api_id = int(api_id_text)
    api_hash = get("TELEGRAM_API_HASH") or None
    if api_hash and not _API_HASH_RE.match(api_hash):
        raise ConfigError(
            "TELEGRAM_API_HASH does not look like an api_hash from my.telegram.org "
            f"(expected 32 hex characters; check {where})"
        )
    greeting_mode = (get("USERBOT_GREETING_MODE") or "voice").lower()
    if greeting_mode not in GREETING_MODES:
        raise ConfigError(f"USERBOT_GREETING_MODE must be one of {', '.join(GREETING_MODES)} (check {where})")
    if require_userbot:
        if api_id is None:
            raise ConfigError(f"TELEGRAM_API_ID is not set (get it at my.telegram.org; check {where})")
        if not api_hash:
            raise ConfigError(f"TELEGRAM_API_HASH is not set (get it at my.telegram.org; check {where})")

    # Claude
    token = get("CLAUDE_CODE_OAUTH_TOKEN") or None
    if token:
        if token.startswith("sk-ant-api"):
            raise ConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN is an Anthropic API key, not a subscription "
                "token. API keys are not used here: run `claude setup-token` and use "
                f"its sk-ant-oat... token instead (check {where})"
            )
        if not token.startswith("sk-ant-oat") or any(ch.isspace() for ch in token):
            raise ConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN is not a valid `claude setup-token` token "
                "(it must start with sk-ant-oat and be on a single line without "
                f"spaces; check {where})"
            )

    claude_bin_text = get("CLAUDE_BIN")
    claude_bin = str(Path(claude_bin_text).expanduser()) if claude_bin_text else default_claude_bin()

    effort = (get("CLAUDE_EFFORT") or "low").lower()
    if effort not in EFFORT_LEVELS:
        raise ConfigError(
            f"CLAUDE_EFFORT must be one of {', '.join(EFFORT_LEVELS)} (check {where})"
        )

    def get_time(key: str) -> Optional[str]:
        text = get(key)
        if not text:
            return None
        match = _TIME_RE.match(text)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ConfigError(f"{key} must be a time like 09:00 (check {where})")
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    daily_report_time = get_time("DAILY_REPORT_TIME")
    daily_report_wait_until = get_time("DAILY_REPORT_WAIT_UNTIL")
    crm_url = get("CRM_URL").rstrip("/") or None
    if crm_url and not crm_url.startswith(("https://", "http://")):
        raise ConfigError(f"CRM_URL must start with https:// (check {where})")

    data_text = get("DATA_DIR")
    data_dir = Path(data_text).expanduser() if data_text else Path("data")
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    profile_text = get("PROFILE_PATH")
    profile_path = Path(profile_text).expanduser() if profile_text else Path("profile.md")
    if not profile_path.is_absolute():
        profile_path = base_dir / profile_path

    return Settings(
        bot_token=bot_token,
        owner_id=owner_id,
        auto_send=auto_send,
        claude_oauth_token=token,
        claude_bin=claude_bin,
        claude_model=get("CLAUDE_MODEL") or None,
        claude_effort=effort,
        claude_timeout=get_int("CLAUDE_TIMEOUT_SECONDS", 120, 1),
        limit_cooldown_minutes=get_int("LIMIT_COOLDOWN_MINUTES", 30, 1),
        owner_takeover_minutes=get_int("OWNER_TAKEOVER_MINUTES", 15, 0),
        history_limit=get_int("HISTORY_LIMIT", 30, 1),
        data_dir=data_dir,
        profile_path=profile_path,
        env_file=None if from_environment else env_file,
        from_environment=from_environment,
        api_id=api_id,
        api_hash=api_hash,
        userbot_contacts_only=get_bool("USERBOT_CONTACTS_ONLY", True),
        userbot_max_replies_per_hour=get_int("USERBOT_MAX_REPLIES_PER_HOUR", 20, 1),
        userbot_reply_delay_seconds=get_int("USERBOT_REPLY_DELAY_SECONDS", 0, 0),
        userbot_greeting_text=get("USERBOT_GREETING_TEXT") or None,
        userbot_greeting_followup=get("USERBOT_GREETING_FOLLOWUP") or None,
        userbot_greeting_send=get_bool("USERBOT_GREETING_SEND", False),
        userbot_greeting_mode=greeting_mode,
        userbot_voice=get("USERBOT_VOICE") or "uz-UZ-SardorNeural",
        userbot_intro_text=get("USERBOT_INTRO_TEXT") or None,
        userbot_fast_contacts=_names(get("USERBOT_FAST_CONTACTS")),
        userbot_ignore_contacts=_names(get("USERBOT_IGNORE_CONTACTS")),
        userbot_schedule_to=normalize_name(get("USERBOT_SCHEDULE_TO")) or None,
        userbot_schedule_text=get("USERBOT_SCHEDULE_TEXT") or None,
        userbot_schedule_every_minutes=get_int("USERBOT_SCHEDULE_EVERY_MINUTES", 0, 0),
        daily_report_time=daily_report_time,
        daily_report_wait_until=daily_report_wait_until,
        daily_report_group=get("DAILY_REPORT_GROUP") or None,
        daily_report_voice=get("DAILY_REPORT_VOICE") or "uz-UZ-MadinaNeural",
        daily_report_place=get("DAILY_REPORT_PLACE") or "Olimp Garden",
        daily_report_owner=get("DAILY_REPORT_OWNER") or None,
        crm_url=crm_url,
        crm_login=get("CRM_LOGIN") or None,
        crm_password=get("CRM_PASSWORD") or None,
    )


def _names(text: str) -> Tuple[str, ...]:
    return tuple(name for name in (normalize_name(part) for part in text.split(",")) if name)


def normalize_name(name: Optional[str]) -> str:
    """Compare contact names loosely: "Banket Garden" == "BanketGarden" == "@banketgarden"."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def env_file_warning(path: Path) -> Optional[str]:
    """Return a warning when ``.env`` is readable by group/others (POSIX only)."""
    if IS_WINDOWS:
        return None
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{path} is readable by other users (mode {stat.S_IMODE(mode):o}). "
            f"Run: chmod 600 {path}"
        )
    return None
