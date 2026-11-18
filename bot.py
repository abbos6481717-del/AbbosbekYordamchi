"""Entry point.

    python bot.py                     run the bot (settings from .env)
    python bot.py --settings-from-env run the bot in a container (Railway)
    python bot.py --check-claude      check the Claude CLI setup, no Telegram
    python bot.py --try "text"        draft a reply to a made-up message, no Telegram
    python bot.py --userbot-login     log in to your own account once (userbot mode)
    python bot.py --userbot           run inside your own account, no Premium needed

Exit codes: 0 ok, 1 Claude check failed, 2 configuration / Telegram token / login error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from assistant.claude_cli import ClaudeError
from assistant.config import (
    PROJECT_DIR,
    SETTING_KEYS,
    ConfigError,
    Settings,
    env_file_warning,
    load_settings,
)
from assistant.store import ensure_private_dir

ENV_FILE = PROJECT_DIR / ".env"
BAD_KEY_SOURCES = {"ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key"}

log = logging.getLogger("bot")


def setup_logging() -> None:
    # Uzbek/Russian text and emoji must not crash a non-UTF-8 console (Windows).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def settings_from_process_env(*, require_telegram: bool = True, require_userbot: bool = False) -> Settings:
    """Container mode: read the settings from os.environ, then remove them from it.

    The variables are removed in ``finally`` (even on errors) so the ``claude``
    child process never inherits the bot token.
    """
    try:
        source = {key: os.environ[key] for key in SETTING_KEYS if key in os.environ}
        return load_settings(
            ENV_FILE, require_telegram=require_telegram, require_userbot=require_userbot, source=source
        )
    finally:
        for key in SETTING_KEYS:
            os.environ.pop(key, None)


def get_settings(from_env: bool, require_telegram: bool, require_userbot: bool = False) -> Settings:
    if from_env:
        return settings_from_process_env(require_telegram=require_telegram, require_userbot=require_userbot)
    settings = load_settings(ENV_FILE, require_telegram=require_telegram, require_userbot=require_userbot)
    warning = env_file_warning(ENV_FILE)
    if warning:
        log.warning(warning)
    return settings


def prepare_dirs(settings: Settings) -> None:
    ensure_private_dir(settings.data_dir)
    ensure_private_dir(settings.claude_workdir)


def inspection_problems(info: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    init = info.get("init")
    if not init:
        return ["the CLI did not report an init event"]
    for key in ("tools", "mcp_servers", "plugins", "skills"):
        if init.get(key):
            problems.append(f"{key} must be empty, got {init.get(key)!r}")
    if init.get("apiKeySource") in BAD_KEY_SOURCES:
        problems.append(f"apiKeySource is {init.get('apiKeySource')!r}: an API key would be used, not the subscription")
    if init.get("apiProvider") not in ("firstParty", None):
        problems.append(f"apiProvider is {init.get('apiProvider')!r}, expected firstParty")
    return problems


async def check_claude(settings: Settings) -> int:
    from assistant.service import make_claude_cli

    cli = make_claude_cli(settings)
    try:
        info = await cli.inspect()
    except ClaudeError as exc:
        print(f"Claude check FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, indent=2, ensure_ascii=False))
    problems = inspection_problems(info)
    if problems:
        for problem in problems:
            print(f"Claude check FAILED: {problem}", file=sys.stderr)
        return 1
    print(f"Claude check OK. Auth: {info['auth']} (binary: {settings.claude_bin})", file=sys.stderr)
    return 0


async def try_reply(settings: Settings, text: str) -> int:
    from assistant.language import LANGUAGE_NAMES, detect_language
    from assistant.prompts import build_system_prompt, build_user_prompt
    from assistant.service import make_claude_cli, read_profile

    cli = make_claude_cli(settings)
    language = detect_language(text)
    system_prompt = build_system_prompt(
        read_profile(settings.profile_path),
        datetime.now().astimezone(),
        persona="assistant" if settings.userbot_intro_text else "owner",
    )
    user_prompt = build_user_prompt([{"role": "other", "name": "Friend", "text": text}], language)
    try:
        reply = await cli.generate(system_prompt, user_prompt)
    except ClaudeError as exc:
        print(f"Claude FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Language: {LANGUAGE_NAMES.get(language or '', 'unknown')}")
    print(f"Message:  {text}")
    print(f"Reply:    {reply}")
    return 0


async def run_bot(settings: Settings) -> int:
    from aiogram import Bot, Dispatcher
    from aiogram.exceptions import TelegramUnauthorizedError

    from assistant.handlers import ALLOWED_UPDATES, build_router
    from assistant.service import build_assistant, profile_is_filled

    bot = Bot(token=settings.bot_token or "")
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram rejected TELEGRAM_BOT_TOKEN. Get a valid token from @BotFather.")
            return 2

        assistant = build_assistant(settings)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(assistant, settings))

        log.info("Bot @%s is running", me.username)
        if settings.auto_send:
            log.warning("Mode: AUTO-SEND. Replies are sent from your account.")
        else:
            log.info("Mode: PREVIEW (replies are only shown in the logs; set AUTO_SEND=true to send)")
        log.info("Claude auth: %s", settings.claude_auth_mode)
        log.info("Claude binary: %s", settings.claude_bin)
        if assistant.state.paused:
            log.warning("The assistant starts PAUSED. Send /resume to the bot to continue.")
        try:
            profile_text = settings.profile_path.read_text(encoding="utf-8")
        except OSError:
            profile_text = ""
        if not profile_is_filled(profile_text):
            log.warning("%s is not filled in yet; replies will be generic.", settings.profile_path.name)

        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
        return 0
    finally:
        await bot.session.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram Business personal assistant (Claude Code CLI).")
    parser.add_argument("--settings-from-env", action="store_true", help="read settings from environment variables (container / Railway)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-claude", action="store_true", help="check the Claude CLI setup without Telegram")
    group.add_argument("--try", dest="try_text", metavar="TEXT", help="draft a reply to a made-up message without Telegram")
    group.add_argument("--userbot", action="store_true", help="run inside your own Telegram account (no Premium needed)")
    group.add_argument("--userbot-login", action="store_true", help="log in to your own account once and exit")
    group.add_argument("--userbot-logout", action="store_true", help="log out of the userbot account and archive its data")
    args = parser.parse_args(argv)

    setup_logging()
    offline = args.check_claude or args.try_text is not None
    userbot = args.userbot or args.userbot_login or args.userbot_logout
    try:
        settings = get_settings(
            args.settings_from_env,
            require_telegram=not offline and not userbot,
            require_userbot=userbot,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    prepare_dirs(settings)

    if args.check_claude:
        return asyncio.run(check_claude(settings))
    if args.try_text is not None:
        return asyncio.run(try_reply(settings, args.try_text))
    try:
        if userbot:
            try:
                from assistant.userbot import logout_userbot, run_userbot
            except ImportError as exc:  # telethon missing
                print(f"Userbot mode needs Telethon: pip install -r requirements.txt ({exc})", file=sys.stderr)
                return 2
            if args.userbot_logout:
                return asyncio.run(logout_userbot(settings))
            return asyncio.run(run_userbot(settings, login_only=args.userbot_login))
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
