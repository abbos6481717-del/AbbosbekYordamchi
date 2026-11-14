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
from typing import Dict, Mapping, Optional

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
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")

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
    overrides: Optional[Mapping[str, str]] = None,
    source: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load and validate settings.

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

    auto_text = get("AUTO_SEND").lower()
    if not auto_text or auto_text in _FALSE:
        auto_send = False
    elif auto_text in _TRUE:
        auto_send = True
    else:
        raise ConfigError(f"AUTO_SEND must be true or false (check {where})")

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
    )


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
